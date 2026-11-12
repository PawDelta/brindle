import fcntl
import os
import pty
import shlex
import shutil
import struct
import subprocess
import termios
import threading
import time
from unittest import mock

import pytest

from brindle import (agents, company_identity, git, knowledge, login_shell_env, pipeline, procs, providers, sessions,
                     signin_pause, spawn, tmux, workspaces)
from brindle.daemon import lifecycle
from brindle.db import DB, Agent
from git_snapshots import fast_host_tools, repo, workspaces_once  # noqa: F401  (fixtures)


def add(db, ws, aid, mode="interactive", parent=None, status="paused", since=None, result=None):
    a = Agent(aid, ws.id, "supervisor" if mode == "interactive" else "developer", "claude", parent,
              mode, status, "", result, time.time(), since or time.time(), "task", f"sess-{aid}")
    db.add_agent(a)
    return a


@pytest.fixture
def root(db, repo):
    return workspaces.adopt_root(db, str(repo))


@pytest.fixture(autouse=True)
def no_timing_backend(monkeypatch):
    from brindle import debuglog

    monkeypatch.setattr(debuglog, "staff", lambda: False)
    monkeypatch.setattr(debuglog, "_publish_timings", debuglog._write_timings)


@pytest.mark.variant("live legacy --no-autopilot sessions have no driver row")
def test_continue_backfills_a_live_legacy_root_without_enabling_workers(db, root, monkeypatch):
    from brindle import autopilot
    from brindle.service import sessions as service

    boss = add(db, root, "boss", status="idle")
    add(db, root, "w1", mode="assign", parent="boss", status="idle")
    monkeypatch.setattr(agents, "find_running", lambda *a, **k: boss)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss")
    assert db.conn.execute("SELECT root_id FROM autopilot").fetchall() == []

    continued = service.continue_(db, root)

    assert continued.root.id == boss.id
    assert [r[0] for r in db.conn.execute("SELECT root_id FROM autopilot")] == [boss.id]
    assert db.get_autopilot(boss.id).state == "running"
    assert autopilot.goal_reminder(db, boss.id) is not None
    assert db.get_autopilot("w1") is None


@pytest.mark.parametrize("own_dirty", [False, True])
def test_forget_removes_clean_worktrees_keeps_dirty_ones_and_all_branches_and_own_worktree(
    db, root, repo, own_dirty, tmp_path, tmp_path_factory
):
    def build():
        made = tuple(workspaces.create(db, str(repo), b).workspace for b in ("brindle/session-1", "clean-work", "dirty-work"))
        open(os.path.join(made[1].path, "done.txt"), "w").write("x")
        git.commit_all(made[1].path, "finished")
        return made

    own, clean, dirty = workspaces_once("sessions:forget", db, tmp_path, tmp_path_factory, build)
    open(os.path.join(dirty.path, "wip.txt"), "w").write("x")  # uncommitted
    if own_dirty:
        open(os.path.join(own.path, "wip.txt"), "w").write("x")
    add(db, own, "boss", since=1)
    add(db, clean, "w1", mode="assign", parent="boss", result="ok")
    add(db, dirty, "w2", mode="assign", parent="boss")
    for i in range(3):
        add(db, root, f"new{i}", since=100 + i)  # push "boss" past KEEP

    sessions.enforce(db, root.repo_root, now=200)
    assert db.get_agent("boss") is None and db.get_agent("w1") is None
    assert not os.path.exists(clean.path) and os.path.exists(dirty.path)
    assert os.path.exists(own.path) == own_dirty and os.path.exists(root.path)
    assert git.branch_exists(str(repo), "clean-work") and git.branch_exists(str(repo), "dirty-work")
    assert git.branch_exists(str(repo), "brindle/session-1")
    assert git.out(["log", "-1", "--format=%s", "main"], str(repo)) == "init"  # nothing merged


def test_pause_then_resume_uses_the_saved_claude_session(db, root, monkeypatch, tmp_path):
    saved = tmp_path / "claude" / "projects" / "p"
    saved.mkdir(parents=True)
    (saved / "sess-boss.jsonl").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    launched, real_launch = [], agents._launch
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: launched.append((a.id, kw)))
    add(db, root, "boss")
    add(db, root, "w1", mode="assign", parent="boss")
    add(db, root, "w2", mode="assign", parent="boss", status="done", result="ok")
    # A fix round the update's own pause left done (the old daemon's code) comes back; one someone closed doesn't.
    for aid, closed in (("w3", None), ("w4", time.time())):
        add(db, root, aid, mode="assign", parent="boss", status="done", result="ok")
        db.update_agent(aid, pipeline="fixing", dismissed_at=closed)
    agents.resume(db, "boss")
    assert db.get_autopilot("boss").enabled
    assert [a for a, _ in launched] == ["boss", "w1", "w3"]  # finished workers stay done
    assert launched[0][1]["resume"] == "sess-boss" and launched[0][1]["prompt"] is None
    assert launched[0][1]["watch_pane"] and not launched[1][1]["watch_pane"]
    # The app's daemon pauses what it runs as the app goes, and the next one resumes it.
    monkeypatch.setattr(agents, "_launch", real_launch)
    _the_app_daemon_pauses_on_its_way_out_and_the_next_start_resumes(db, root, monkeypatch)


DEAD_PID = 2**22 + 1    # above any pid_max: no process has it (an app that has gone)


def _the_app_daemon_pauses_on_its_way_out_and_the_next_start_resumes(db, root, monkeypatch):
    """The app's daemon (lifecycle.serve, --idle-exit) on the suite's private
    tmux server, stopped as the app stops it (SIGTERM) and left behind by an
    app that was force quit, pausing through the real agents.pause; then the
    next start (resume_after_start)."""
    import dataclasses
    import signal
    import threading

    from brindle import company_identity, debuglog, procs, spawn, upgrade
    from brindle.daemon import lifecycle
    from brindle.service import sessions as svc

    here = tmux.current_server()

    def window(session):
        return tmux._tmux("new-session", "-d", "-s", session, "-P", "-F", "#{window_id}", "cat").stdout.strip()

    def session(aid, ws, status, server=here, parent=None, win=""):
        db.add_agent(Agent(aid, ws.id, "supervisor", "claude", parent, "assign" if parent else "interactive",
                           status, win, None, 1000.0, task="t", tmux_server=server))

    session("chat", root, "idle", win=window(root.tmux_session))
    session("helper", root, "running", parent="chat")
    # Its sidebar, in whichever session it followed the person to.
    sidebar = tmux._tmux("new-session", "-d", "-s", "side", "-P", "-F", "#{pane_id}", "cat").stdout.strip()
    tmux.set_pane_tag(sidebar, tmux.SIDEBAR_TAG, "chat")
    db.set_sidebar_pane("chat", sidebar)
    # Another server's session (a demo's, another home's daemon's): its pane id names ours
    # here, and its workspace's tmux session name is a session on this server too.
    other = dataclasses.replace(root, id="ws-other", name="other", tmux_session="shared-name")
    db.add_workspace(other)
    window("shared-name")
    session("foreign", other, "idle", server="elsewhere", win=db.get_agent("chat").tmux_window)
    events, saying = [], []
    # The cull never runs; the daemon still says it's pausing when the pause is over and the cull starts.
    monkeypatch.setattr(svc, "cull_detached", lambda repo_root=None: saying.append(
        lifecycle._pausing_path().read_text()))
    monkeypatch.setattr(procs, "stop", lambda ids, *a, **kw: events.append(("stop", sorted(ids))) or 0)
    # Launches run for real up to their window, which is only counted (and their identity probe,
    # which can be held up below).
    probing, holding, go = [], [], threading.Event()

    def hold(reached):
        """Held in flight until the way out has written its record, or has to wait for this."""
        reached.set()
        go.wait(10)

    monkeypatch.setattr(agents, "_open_window", lambda db_, a, *args, **kw: events.append(("launch", a.id)) or "%0")
    monkeypatch.setattr(agents, "_detach", lambda args: None)
    real_start = spawn.start
    monkeypatch.setattr(spawn, "start", lambda argv, **kw: None if "_after-launch" in argv else real_start(argv, **kw))

    def launch_check(provider, home=None):
        if probing:
            hold(probing.pop())
        return None, None, None

    monkeypatch.setattr(company_identity, "launch_check", launch_check)
    # What serve starts beside its watch, unrelated here; and its process-wide settings, restored after.
    monkeypatch.setattr(spawn, "_gated", spawn._gated)
    monkeypatch.setattr(upgrade, "on_stale_launch", upgrade.on_stale_launch)
    monkeypatch.setattr(debuglog.Uploader, "start", lambda self: None)
    # A sweep still at work in a thread when the app stops the daemon (a slow git on a busy machine).
    held, swept = threading.Event(), threading.Event()
    monkeypatch.setattr(lifecycle, "_upgrade_sweep", lambda: held.wait(5) or swept.set())
    monkeypatch.setattr(lifecycle, "_reset_window_sizes", lambda: None)
    hook_kept = threading.Event()
    monkeypatch.setattr(lifecycle, "_keep_codex_hook", lambda settle=0.0: hook_kept.set())
    monkeypatch.setattr(lifecycle, "WATCH_INTERVAL", 0.05)
    servers = []
    real_make = lifecycle.make_server
    monkeypatch.setattr(lifecycle, "make_server", lambda app: servers.append(real_make(app)) or servers[-1])

    def serve(app_gone, after=None):
        """Run the app's daemon until the app stops it (SIGTERM, as daemon.rs
        stop_daemon does; once ``after`` is set) or, the app gone, the daemon
        notices by itself."""
        servers.clear()
        done, noticed = threading.Event(), [True]
        if app_gone:
            monkeypatch.setenv(lifecycle.APP_PID_ENV, str(DEAD_PID))

        def the_app():
            while not done.is_set() and not (servers and servers[0].started):
                done.wait(0.01)
            if after is not None:
                after.wait(10)
            if app_gone and done.wait(10):
                return
            noticed[0] = not app_gone       # a daemon that never noticed is stopped, and fails below
            if not done.is_set():
                os.kill(os.getpid(), signal.SIGTERM)

        prev = signal.getsignal(signal.SIGTERM)
        app = threading.Thread(target=the_app)
        app.start()
        try:
            try:
                lifecycle.serve(unix=False, tcp=True, idle_exit=True)
            except SystemExit:      # serve's way out of a SIGTERM
                pass
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)   # a SIGTERM sent late lands nowhere
            done.set()
            app.join()
            signal.signal(signal.SIGTERM, prev)
        assert noticed[0], "the daemon didn't notice the app had gone"

    # No record, no pause: sessions that couldn't be recorded for the next start keep running.
    lifecycle._resume_path().mkdir()
    assert lifecycle.pause_for_exit() == []
    assert db.get_agent("chat").status == "idle" and tmux.has_session(root.tmux_session)
    lifecycle._resume_path().rmdir()

    # The app's daemon is stopped: it pauses this server's session and records it; the other's is left.
    # Nothing else holds up its exit: not the sweep still running, and not a second stop while it
    # pauses (it outlives the app: the next app, `brindle daemon stop` or a logout can each stop it).
    real_close, stopped_again = agents.close_paused, []

    def close_paused(db_, closings):
        if not stopped_again:
            stopped_again.append(os.kill(os.getpid(), signal.SIGTERM))
        real_close(db_, closings)

    monkeypatch.setattr(agents, "close_paused", close_paused)
    # Its start asks the person's login shell for their proxy settings, once (login_shell_env).
    # Only this first daemon's shell exports any: the launches further down run without them.
    exported = [b"HTTPS_PROXY=http://proxy.corp.example:8080\0"]
    monkeypatch.setattr(login_shell_env, "_run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, exported.pop() if exported else b"", b""))
    serve(app_gone=False)
    assert not exported and login_shell_env.for_launch("codex", environ={}) == {
        "HTTPS_PROXY": "http://proxy.corp.example:8080"}
    assert stopped_again and lifecycle.read_info() is None, "a second stop cut the way out short"
    assert not swept.is_set(), "the daemon waited for the sweep before it paused and exited"
    assert hook_kept.wait(5), "the Codex hook's trust wasn't looked at when the daemon started"
    held.set()
    assert [db.get_agent(a).status for a in ("chat", "helper", "foreign")] == ["paused", "paused", "idle"]
    assert not tmux.has_session(root.tmux_session) and tmux.has_session("shared-name")
    assert not tmux.has_session("side") and db.get_sidebar_pane("chat") is None
    assert lifecycle._read_resume() == [{"id": "chat", "server": here}]
    # It told the app to wait for its pause (daemon.rs stop_daemon), and not past it.
    assert saying == [f"{os.getpid()}\n"] and not lifecycle._pausing_path().exists()
    recoveries = []

    class Recovery(lifecycle.Recovery):
        def __init__(self):
            super().__init__()
            recoveries.append(self)

    real_record = lifecycle._record_resume

    def record(entries):
        written = real_record(entries)
        if recoveries[-1].stopping.is_set():    # the way out's, not the start's counting a try
            go.set()
        return written

    monkeypatch.setattr(lifecycle, "Recovery", Recovery)
    monkeypatch.setattr(lifecycle, "RESUME_WAIT", 0)
    monkeypatch.setattr(lifecycle, "_record_resume", record)
    # The next is stopped while it brings the session back (having stopped what the cull never
    # did), its launch held up in its identity probe: the way out records and pauses without
    # waiting for that probe, nothing is launched past its pause, and the session is recorded again.
    probed = threading.Event()
    probing.append(probed)
    serve(app_gone=False, after=probed)
    assert recoveries[-1].idle.wait(10), "the held resume never ended"
    assert events == [("stop", ["chat", "helper"])] and db.get_agent("chat").status == "paused"
    assert lifecycle._read_resume() == [{"id": "chat", "server": here, "tries": 1}]     # still under way when recorded
    # The start after that brings it back, once, having stopped what the cull never did first.
    events.clear()
    db.set_status("helper", "processing")   # launched past the pause (a queued task's helper): comes back too
    assert lifecycle.resume_after_start() == ["chat"]
    assert events == [("stop", ["chat", "helper"]), ("launch", "chat"), ("launch", "helper")]
    lifecycle._resume_path().unlink()       # these counted windows can't confirm it to tmux

    # The app is force quit, and the tmux server stops answering: the pause is cut short at its
    # budget (the server answers with a failure once the daemon has gone).
    events.clear()
    for aid in ("chat", "helper"):
        db.set_status(aid, "idle")
    db.update_agent("chat", tmux_window=window(root.tmux_session))
    tmux.set_pane_tag(db.get_agent("chat").tmux_window, tmux.AGENT_TAG, "chat")
    answer = threading.Event()

    def close(**targets):
        answer.wait()
        raise tmux.TmuxError("tmux kill-window: server busy")

    monkeypatch.setattr(tmux, "close", close)
    monkeypatch.setattr(lifecycle, "PAUSE_BUDGET", 0.2)
    serve(app_gone=True)
    answer.set()
    assert [db.get_agent(a).status for a in ("chat", "helper", "foreign")] == ["paused", "paused", "idle"]
    assert tmux.has_session(root.tmux_session) and tmux.has_session("shared-name")
    assert lifecycle._read_resume() == [{"id": "chat", "server": here}]
    # The next start closes what that pause left and stops what is left first.
    assert lifecycle.resume_after_start() == ["chat"]
    assert not tmux.has_session(root.tmux_session)
    assert events == [("stop", ["chat", "helper"]), ("launch", "chat"), ("launch", "helper")]

    # A record made on another tmux server isn't resumed here.
    lifecycle._record_resume([{"id": "chat", "server": "elsewhere"}])
    assert lifecycle.resume_after_start() == [] and len(events) == 3


def _recorded_sessions(db, root, monkeypatch, sessions):
    """Paused sessions on the resume record, given as (id, tmux session) most
    recently active first. All paused at the same moment and recorded in the
    opposite order: only what they did before the pause tells them apart."""
    import dataclasses

    from brindle import procs
    from brindle.daemon import lifecycle

    here, paused_at = tmux.current_server(), time.time() + 1000
    for rank, (aid, session) in enumerate(sessions):
        if db.get_workspace(f"ws-{session}") is None:
            db.add_workspace(dataclasses.replace(root, id=f"ws-{session}", name=session, tmux_session=session))
        db.add_agent(Agent(aid, f"ws-{session}", "supervisor", "claude", None, "interactive", "paused", "", None,
                           time.time(), paused_at, "t", tmux_server=here))
        db.conn.execute("INSERT INTO events (ts, kind, root_id) VALUES (?, 'agent.status', ?)",
                        (paused_at - rank - 1, aid))
    lifecycle._record_resume([{"id": aid, "server": here} for aid, _ in reversed(sessions)])
    monkeypatch.setattr(procs, "stop", lambda ids, *a, **kw: 0)


def test_recovery_lists_queued_sessions_as_resuming_and_failed_ones_as_paused(db, monkeypatch):
    from types import SimpleNamespace

    from starlette.requests import Request

    from brindle.daemon import routes_read
    from brindle.db import Workspace

    ws = Workspace("proj", "/proj", "proj", "root", "main", None, "/proj", None, "proj", 1)
    db.add_workspace(ws)
    for aid in ("first", "queued", "exhausted", "foreign", "manual"):
        add(db, ws, aid)
    db.update_agent("first", created_at=2, status_since=20)
    db.update_agent("queued", created_at=1, status_since=10)
    for aid, stopped in (("exhausted", 50), ("foreign", 40), ("manual", 30)):
        db.update_agent(aid, status_since=stopped)
    panes = tmux.PaneSnapshot()
    monkeypatch.setattr(tmux, "current_server", lambda: None)
    monkeypatch.setattr(tmux, "list_panes", lambda: panes)
    monkeypatch.setattr(agents, "same_server", lambda *a: True)
    monkeypatch.setattr(procs, "stop", lambda *a, **kw: 0)
    monkeypatch.setattr(spawn, "wait_for_room", lambda *a, **kw: None)
    monkeypatch.setattr(lifecycle, "RESUME_PARALLEL", 1)
    monkeypatch.setattr(lifecycle, "_read_viewed", lambda: "first")
    lifecycle._record_resume([{"id": "first", "server": None}, {"id": "queued", "server": None},
                              {"id": "exhausted", "server": None, "tries": lifecycle.RESUME_TRIES},
                              {"id": "foreign", "server": "elsewhere"}])
    recovery = lifecycle.Recovery()
    request = Request({"type": "http", "query_string": b"repo=/proj",
                       "app": SimpleNamespace(state=SimpleNamespace(recovery=recovery))})

    def statuses(db_):
        return {row["id"]: row["status"] for row in routes_read.list_sessions.__wrapped__(db_, request)}

    seen, launching, orders = [], [], []

    def resume(db_, root_id, **kw):
        seen.append(statuses(db_))
        transitions = [list(seen[-1])]
        # During launch it leaves both the paused list and the live-pane list.
        db_.set_status(root_id, "starting")
        starting = statuses(db_)
        launching.append((root_id, starting.get(root_id)))
        transitions.append(list(starting))
        # Its position stays the same when tmux can see the pane, too.
        pane = f"%{root_id}"
        db_.update_agent(root_id, tmux_window=pane)
        panes[pane] = True
        db_.set_status(root_id, "idle")
        transitions.append(list(statuses(db_)))
        orders.append(transitions)
        panes.pop(pane)
        db_.set_status(root_id, "paused")
        raise agents.AgentError("launch failed")

    monkeypatch.setattr(agents, "resume", resume)
    assert lifecycle.resume_after_start(recovery) == []
    assert seen[0] == {"first": "resuming", "queued": "resuming", "exhausted": "paused",
                       "foreign": "paused", "manual": "paused"}
    assert seen[1]["first"] == "paused" and seen[1]["queued"] == "resuming"
    assert launching == [("first", "resuming"), ("queued", "resuming")]
    assert orders[0] == [["first", "queued", "exhausted", "foreign", "manual"]] * 3
    assert set(statuses(db).values()) == {"paused"}


@pytest.mark.variant("concurrent recovery must overlap distinct tmux sessions while serializing chats sharing one session")
def test_a_start_brings_sessions_back_at_once_most_recently_active_first(db, root, monkeypatch):
    import threading

    from brindle.daemon import lifecycle

    # Two chats in one tmux session: one's pause would close the other's windows.
    _recorded_sessions(db, root, monkeypatch, [("recent", "a"), ("same-tmux", "a"), ("older", "b"), ("oldest", "c")])
    monkeypatch.setattr(lifecycle, "RESUME_PARALLEL", 2)
    together, began = threading.Barrier(2), []

    def resume(db_, root_id, *, gate, **kw):
        began.append(db_.get_agent(root_id).id)     # each thread on its own connection, or sqlite refuses
        with gate():                                # launches overlap too: the gate doesn't line them up
            together.wait(5)

    monkeypatch.setattr(agents, "resume", resume)
    assert sorted(lifecycle.resume_after_start()) == ["older", "oldest", "recent", "same-tmux"]
    assert set(began[:2]) == {"recent", "older"} and set(began[2:]) == {"same-tmux", "oldest"}


@pytest.mark.parametrize("case", ["back", "hung", pytest.param("switched", marks=pytest.mark.variant("reported wait: switching sessions during the head start kept the new one waiting")), pytest.param("unreadable", marks=pytest.mark.variant(
    "a viewed-session file that isn't UTF-8 raised past the OSError handler and stopped every session coming back"))])
def test_viewed_session_comes_back_first(db, root, monkeypatch, case):
    """The session the app showed last comes back first and alone, though it
    was the least recently at work; if it hangs, the others start once its
    head start is over rather than wait on it, and at once if the person opens
    another meanwhile. A record of it that can't be read loses nothing."""
    import threading

    from brindle.daemon import lifecycle

    _recorded_sessions(db, root, monkeypatch, [("recent", "a"), ("older", "b"), ("viewed", "c")])
    lifecycle.record_viewed("viewed")
    hangs = case in ("hung", "switched")
    monkeypatch.setattr(lifecycle, "VIEWED_HEAD_START", 0 if case == "hung" else 60)
    if case == "unreadable":
        lifecycle._viewed_path().write_bytes(b"\xff\xfe")
    recovery, lock = lifecycle.Recovery(), threading.Lock()
    others_begun, viewed_done = threading.Event(), threading.Event()
    waiting, began = [], []

    def resume(db_, root_id, **kw):
        if root_id == "viewed":
            waiting.append(len(recovery.lanes))     # the others' lanes, not begun yet
            if case == "switched":
                recovery.prioritize("older")
            if hangs:
                others_begun.wait(10)
            waiting.append(len(recovery.lanes))
            viewed_done.set()
            return
        with lock:
            began.append((root_id, viewed_done.is_set()))
            if len(began) == 2:
                others_begun.set()

    monkeypatch.setattr(agents, "resume", resume)
    assert sorted(lifecycle.resume_after_start(recovery)) == ["older", "recent", "viewed"]
    if hangs:
        assert sorted(began) == [("older", False), ("recent", False)]  # begun while it hung
    elif case == "back":
        assert waiting == [2, 2] and all(done for _, done in began)     # none begun beside it


@pytest.mark.variant("stop racing concurrent recovery must finish an in-flight launch and retain preflight and unstarted sessions")
def test_stopped_during_concurrent_recovery_a_launch_finishes_and_the_rest_stay_pending(db, root, monkeypatch, caplog):
    import re
    import threading

    from brindle.daemon import lifecycle

    from brindle import debuglog

    monkeypatch.setattr(debuglog, "staff", lambda: True)
    monkeypatch.setattr(debuglog, "_publish_timings", debuglog._write_timings)
    _recorded_sessions(db, root, monkeypatch, [("launching", "a"), ("preflight", "b"), ("unstarted", "c")])
    monkeypatch.setattr(lifecycle, "RESUME_PARALLEL", 2)
    monkeypatch.setattr(lifecycle, "RESUME_WAIT", 0)    # only the launch in flight holds stop up
    recovery = lifecycle.Recovery()
    under_way, finish, launched = threading.Barrier(3), threading.Event(), []

    def resume(db_, root_id, *, gate, **kw):
        if root_id == "launching":
            with gate():
                under_way.wait(5)
                finish.wait(5)
                launched.append(root_id)
        else:
            under_way.wait(5)
            recovery.stopping.wait(5)
            with gate():                # past the stop: agents.Cancelled, nothing launched
                launched.append(root_id)

    monkeypatch.setattr(agents, "resume", resume)
    result = []
    start = threading.Thread(target=lambda: result.extend(lifecycle.resume_after_start(recovery)))
    start.start()
    under_way.wait(5)
    stopped = []
    stop = threading.Thread(target=lambda: stopped.append((recovery.stop(), list(launched))))
    stop.start()
    assert recovery.stopping.wait(5)
    finish.set()
    stop.join(5)
    start.join(5)
    pending, seen = stopped[0]
    assert seen == ["launching"] and result == ["launching"]    # stop returned after the launch under way, none later
    assert {"preflight", "unstarted"} <= {e["id"] for e in pending}     # handed to the way out to record again
    assert sorted(e["id"] for e in recovery.pending) == ["preflight", "unstarted"]
    assert not any(e.get("tries") for e in recovery.pending)    # stopped: that's no failed try
    # The log says how long the step it was cancelled in took: that may be the slow one.
    assert re.search(r"session preflight recovery: .*resume \d+\.\d+s \(cancelled\)", caplog.text)


def test_recovery_progress_counts_sessions_as_they_finish(db, root, monkeypatch):
    from brindle.daemon import lifecycle

    _recorded_sessions(db, root, monkeypatch, [("back", "a"), ("bad", "a"), ("later", "a")])   # one lane, in turn
    recovery, seen = lifecycle.Recovery(), []

    def resume(db_, root_id, **kw):
        seen.append(recovery.progress())
        if root_id == "bad":
            raise agents.AgentError("no")
        if root_id == "back":
            _open_pane(db_, db_.get_agent(root_id))

    monkeypatch.setattr(agents, "resume", resume)
    lifecycle.resume_after_start(recovery)
    assert [(p["done"], p["total"]) for p in seen] == [(0, 3), (1, 3), (2, 3)]
    assert sorted(seen[2]["pending"]) == ["bad", "later"]   # confirmed back: gone at once; failed: still to come
    assert recovery.progress() is None      # over: the app's line goes


class Crash(BaseException):
    """The daemon dying at this point: nothing in brindle handles it."""


def _real_launches(monkeypatch):        # on the suite's tmux server, with a harmless program for the CLI
    monkeypatch.setattr(providers.ClaudeCode, "command", lambda self, ctx: ["/bin/sh", "-c", "exec cat"])
    monkeypatch.setattr(providers.ClaudeCode, "can_resume", staticmethod(lambda ref: True))
    monkeypatch.setattr(company_identity, "launch_check", lambda provider, home=None: (None, None, None))
    monkeypatch.setattr(agents, "_detach", lambda args: None)
    monkeypatch.setattr(agents, "_ensure_sidebar", lambda *a, **kw: None)
    monkeypatch.setattr(spawn, "start", lambda argv, **kw: None)


def _live_windows(agent_id=None):
    return [p for p in tmux.agent_panes() if p.alive and agent_id in (None, p.agent)]


def _open_pane(db, a, launched=True):   # what a launch leaves in tmux
    ws = db.get_workspace(a.workspace_id)
    tmux.ensure_session(ws.tmux_session, ws.path, {})
    pane = tmux.new_window(ws.tmux_session, a.id, ws.path, ["cat"], {}, tag=(tmux.AGENT_TAG, a.id))
    if launched:
        tmux.mark_launched(pane)
    db.set_status(a.id, "starting")
    db.update_agent(a.id, tmux_window=pane, tmux_server=tmux.current_server())
    return pane


@pytest.mark.parametrize("step, who", [
    pytest.param("status", "w1", marks=pytest.mark.variant("worker launch dies before its status write, after its chat has launched")),
    pytest.param("session", "w1", marks=pytest.mark.variant("worker has a starting status but dies before ensuring its tmux session")),
    pytest.param("tag", "w1", marks=pytest.mark.variant("worker pane exists when the parent dies before writing its ownership tag")),
    pytest.param("self_tag", "w1", marks=pytest.mark.variant("untagged child wakes after recovery and must not become a second worker")),
    pytest.param("record", "w1", marks=pytest.mark.variant("owned worker pane exists before its window is recorded in the database")),
    pytest.param("helper", "w1", marks=pytest.mark.variant("worker window is recorded but its after-launch helper has not started")),
    pytest.param("mark", "w1", marks=pytest.mark.variant("worker helper started but the pane has no launch completion mark")),
    pytest.param("finished", "w1", marks=pytest.mark.variant("all launches finished before the crash so recovery must reuse their panes")),
    pytest.param("status", "chat", marks=pytest.mark.variant("root launch dies before its status write, leaving no live chat to recover around")),
    pytest.param("gate", "chat", marks=pytest.mark.variant("root pane exists but its child has not been released through the launch gate")),
])
def test_recovery_reconciles_from_tmux_after_a_crash_at_each_step_of_a_launch(db, root, monkeypatch, step, who):
    _recorded_sessions(db, root, monkeypatch, [("chat", "a")])
    add(db, root, "w1", mode="assign", parent="chat")
    _real_launches(monkeypatch)
    armed, current, real_launch, real_self_tag = [True], [None], agents._launch, tmux._tags_itself

    def crash_before(owner, attr, when=lambda *a, **kw: True):
        real = getattr(owner, attr)

        def wrapped(*a, **kw):
            if armed[0] and current[0] == who and when(*a, **kw):
                armed[0] = False
                raise Crash
            return real(*a, **kw)

        monkeypatch.setattr(owner, attr, wrapped)

    def held_self_tag(command, tag, launch):    # the pane's own tagging hasn't run when recovery looks
        wait = shlex.join([*tmux._base(), "wait-for", "held"])
        tagged = real_self_tag(command, tag, launch)
        return ["/bin/sh", "-c", f'{wait}; exec "$@"', "x", *tagged] if armed[0] else tagged

    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws, **kw: current.__setitem__(0, a.id) or real_launch(db_, a, ws, **kw))
    monkeypatch.setattr(tmux, "_tags_itself", held_self_tag if step == "self_tag" else real_self_tag)
    agent_tag = (tmux, "_set_for_launch", lambda pane, launch, key, value: key == tmux.AGENT_TAG)
    points = {"status": (DB, "set_status", lambda s, aid, status, *a, **kw: status != "paused"),
              "session": (tmux, "ensure_session"), "tag": agent_tag, "self_tag": agent_tag,
              "record": (DB, "update_agent", lambda s, aid, **kw: "tmux_window" in kw), "gate": (tmux, "open_gate"),
              "helper": (spawn, "start", lambda argv, **kw: "_after-launch" in argv),
              "mark": (tmux, "mark_launched"),
              "finished": (knowledge, "note")}      # every launch done, the first thing after
    crash_before(*points[step])
    with pytest.raises(Crash):
        agents.resume(db, "chat", settle=True)
    assert lifecycle.resume_after_start() == ["chat"]
    if step == "self_tag":
        tmux._tmux("wait-for", "-S", "held")    # the old child, let go now: it must have been closed
    panes = _live_windows()
    assert sorted(p.agent for p in panes) == ["chat", "w1"] and all(p.launched for p in panes)
    assert sum("w1" in name for name in tmux.windows(root.tmux_session)) == 1 and lifecycle._read_resume() is None


@pytest.mark.parametrize("hangs_in", [
    pytest.param("commit", marks=pytest.mark.variant("committed launch wakes after shutdown before its starting status was written")),
    pytest.param("ensure_session", marks=pytest.mark.variant("launch wakes after shutdown while ensuring its tmux session, before any pane exists")),
    "new_window",
])
def test_recovery_reconciles_from_tmux_after_a_launch_the_way_out_gave_up_on(db, root, monkeypatch, hangs_in):
    from brindle.service import sessions as svc

    _recorded_sessions(db, root, monkeypatch, [("chat", "a")])
    _real_launches(monkeypatch)
    monkeypatch.setattr(lifecycle, "RESUME_WAIT", 0)
    monkeypatch.setattr(lifecycle, "LAUNCH_WAIT", 0)
    monkeypatch.setattr(svc, "cull_detached", lambda repo_root=None: None)
    inside, release, real_commit = threading.Event(), threading.Event(), agents._gate_commit

    def hang(step):
        if step == hangs_in and not inside.is_set():
            inside.set()
            release.wait(10)

    def held(owner, attr):
        real = getattr(owner, attr)
        monkeypatch.setattr(owner, attr, lambda *a, **kw: hang(attr) or real(*a, **kw))

    monkeypatch.setattr(agents, "_gate_commit", lambda hold: real_commit(hold) or hang("commit"))   # status not yet written
    held(tmux, "ensure_session")
    held(tmux, "new_window")
    recovery = lifecycle.Recovery()
    start = threading.Thread(target=lifecycle.resume_after_start, args=(recovery,))
    start.start()
    assert inside.wait(5)
    leaving = threading.Thread(target=lifecycle.pause_for_exit, args=(recovery,))
    leaving.start()
    leaving.join(10)
    assert not leaving.is_alive() and [e["id"] for e in lifecycle._read_resume()] == ["chat"]
    release.set()           # it wakes past the pause, and writes and opens what it likes
    start.join(10)
    assert lifecycle.resume_after_start() == ["chat"]
    assert len(_live_windows("chat")) == 1 and lifecycle._read_resume() is None


def test_a_resume_that_hangs_ends_the_start_and_stays_on_the_record(db, root, monkeypatch):
    _recorded_sessions(db, root, monkeypatch, [("hung", "a")])
    monkeypatch.setattr(lifecycle, "RESUME_STALL", 0)
    recovery, release = lifecycle.Recovery(), threading.Event()
    monkeypatch.setattr(agents, "resume", lambda db_, root_id, **kw: release.wait(10))
    assert lifecycle.resume_after_start(recovery) == [] and recovery.progress() is None
    assert [e["id"] for e in lifecycle._read_resume()] == ["hung"]
    release.set()
    [t.join(5) for t in threading.enumerate() if t.name.startswith("brindle-resume-")]


@pytest.mark.parametrize("clean_stops", [
    pytest.param(False, marks=pytest.mark.variant("repeated daemon crashes must preserve the accumulated failed recovery attempts")),
    pytest.param(True, marks=pytest.mark.variant("clean shutdown between failed recoveries must not reset the attempt cap")),
])
def test_a_session_that_cant_come_back_is_tried_a_few_times_then_left_paused(db, root, monkeypatch, clean_stops):
    from brindle.service import sessions as svc

    _recorded_sessions(db, root, monkeypatch, [("chat", "a")])
    tried = []      # each try as a launch does it, the daemon dying before its window
    monkeypatch.setattr(agents, "resume", lambda db_, root_id, **kw: tried.append(root_id) or db_.set_status(root_id, "starting"))
    monkeypatch.setattr(svc, "cull_detached", lambda repo_root=None: None)
    for _ in range(lifecycle.RESUME_TRIES):
        lifecycle.resume_after_start()
        clean_stops and lifecycle.pause_for_exit()
        assert lifecycle._read_resume()
    assert lifecycle.resume_after_start() == [] and len(tried) == lifecycle.RESUME_TRIES
    assert db.get_agent("chat").status == "paused" and lifecycle._read_resume() is None


@pytest.mark.parametrize("tries, launched, chat_stays, status", [
    pytest.param(0, ["w1", "w2"], True, "idle", marks=pytest.mark.variant("live root is kept while missing workers return and the sign-in-held worker stays paused")),
    pytest.param(2, [], False, "idle", marks=pytest.mark.variant("exhausted recovery attempts must close a live root rather than leave a half-running session")),
    pytest.param(0, ["chat", "w1", "w2"], False, "failed", marks=pytest.mark.variant("failed root still owns an unfinished pane that must be replaced before the session returns")),
])
def test_recovery_of_a_half_running_session(db, root, monkeypatch, tries, launched, chat_stays, status):
    _recorded_sessions(db, root, monkeypatch, [("chat", "a")])
    add(db, root, "w1", mode="assign", parent="chat")
    add(db, root, "w2", mode="assign", parent="chat", status="starting")    # its launch died before its window
    add(db, root, "w3", mode="assign", parent="chat")       # held for sign-in: that sweep's to bring back
    state = signin_pause._load()
    state["paused"]["w3"] = {"provider": "claude", "reason": signin_pause.NO_IDENTITY, "identity": ""}
    signin_pause._save(state)
    chat = _open_pane(db, db.get_agent("chat"), launched=status != "failed")
    db.set_status("chat", status)
    lifecycle._record_resume([{"id": "chat", "server": tmux.current_server(), "tries": tries}])
    opened = []
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws, **kw: opened.append(a.id) or _open_pane(db_, a))
    lifecycle.resume_after_start()
    assert sorted(opened) == launched and tmux.window_alive(chat) == chat_stays
    assert db.get_agent("w2").status == ("starting" if launched else "paused") and lifecycle._read_resume() is None


@pytest.mark.parametrize("kill, stray, launches, status", [
    pytest.param("raises", "chat", [], "starting", marks=pytest.mark.variant("tmux refuses root-pane teardown, so recovery must retain the record even past the attempt cap")),
    pytest.param("does nothing", "chat", [], "starting", marks=pytest.mark.variant("tmux reports successful teardown but the root pane stays alive, so recovery must check liveness")),
    pytest.param("does nothing", "chat", [], "failed", marks=pytest.mark.variant("failed database status must not hide a root pane that survives teardown")),
    pytest.param("does nothing", "w1", ["w1"], "starting", marks=pytest.mark.variant("worker launch fails after opening a pane that survives cleanup while its root is already live")),
])
def test_a_pane_that_wont_close_holds_its_session_back_however_long_that_takes(db, root, monkeypatch, kill, stray, launches, status):
    _recorded_sessions(db, root, monkeypatch, [("chat", "a")])
    opened, real_kill = [], tmux.kill_window

    def launch(db_, a, ws, **kw):
        opened.append(a.id)
        _open_pane(db_, a, launched=False)
        raise agents.AgentError("no helper")

    if stray == "w1":
        add(db, root, "w1", mode="assign", parent="chat")
    _open_pane(db, db.get_agent("chat"), launched=stray == "w1")
    db.set_status("chat", status)
    monkeypatch.setattr(agents, "_launch", launch)
    monkeypatch.setattr(tmux, "kill_window", mock.Mock(side_effect=tmux.TmuxError("busy") if kill == "raises" else None))
    for _ in range(lifecycle.RESUME_TRIES + 1):      # past giving up on it, too
        lifecycle.resume_after_start()
        assert opened == launches and _live_windows(stray) and lifecycle._read_resume()
    monkeypatch.setattr(tmux, "kill_window", real_kill)
    assert lifecycle.resume_after_start() == [] and opened == launches
    assert not _live_windows() and lifecycle._read_resume() is None


@pytest.mark.variant("an old launch writes into a reused pane id; only the new launch token counts, and duplicate owned panes invalidate confirmation")
def test_only_one_pane_whose_own_launch_finished_confirms_a_session(db, root):
    add(db, root, "chat", status="idle")
    pane = _open_pane(db, db.get_agent("chat"), launched=False)
    older = tmux.NewPane(pane)      # an older launch, waking after a tmux restart gave this id to the new pane
    older.launch = "older"
    tmux.mark_launched(older)
    tmux._set_for_launch(pane, "older", tmux.AGENT_TAG, "someone-else")     # its ownership write, likewise refused
    assert tmux.get_pane_tag(pane, tmux.LAUNCHED_TAG) is None and tmux.get_pane_tag(pane, tmux.AGENT_TAG) == "chat"
    tmux.set_pane_tag(pane, tmux.LAUNCHED_TAG, "older")     # and should its mark land anyway, it counts for nothing
    tmux.set_pane_tag(pane, tmux.AGENT_TAG, "someone-else")     # nor does its claim to the pane
    assert not agents.panes_match(db, "chat", set()) and _live_windows("chat") and not _live_windows("someone-else")
    tmux.mark_launched(pane)
    assert agents.panes_match(db, "chat", set()) and agents.settle_panes(db, [db.get_agent("chat")]) == {"chat"}
    _open_pane(db, db.get_agent("chat"))
    assert not agents.panes_match(db, "chat", set())


def test_a_try_that_cant_be_counted_launches_nothing(db, root, monkeypatch):
    _recorded_sessions(db, root, monkeypatch, [("chat", "a")])
    real, tried = lifecycle._record_resume, []
    monkeypatch.setattr(lifecycle, "_record_resume", lambda entries: False)
    monkeypatch.setattr(agents, "resume", lambda db_, root_id, **kw: tried.append(root_id))
    lifecycle.resume_after_start()
    assert tried == [] and [e["id"] for e in lifecycle._read_resume()] == ["chat"]
    monkeypatch.setattr(lifecycle, "_record_resume", real)
    lifecycle.resume_after_start()
    assert tried == ["chat"]


def test_a_worker_the_sign_in_sweep_has_brought_back_is_left_to_it(db, root, monkeypatch):
    _recorded_sessions(db, root, monkeypatch, [("chat", "a")])
    add(db, root, "w1", mode="assign", parent="chat")
    state = signin_pause._load()
    state["paused"]["w1"] = {"provider": "claude", "reason": "signed out", "identity": "me"}
    signin_pause._save(state)
    stopped, launched, inside, release = [], [], threading.Event(), threading.Event()
    committed, real_held = threading.Event(), signin_pause.paused_for_signin
    monkeypatch.setattr(procs, "stop", lambda ids, *a, **kw: stopped.extend(ids) or 0)

    def launch(db_, a, ws, **kw):
        _open_pane(db_, a)
        launched.append(a.id)
        if a.id == "w1":
            inside.set()
            release.wait(10)

    def sweep():                # to its end: its record of the worker goes with it
        state, base = signin_pause._load(), signin_pause._load()
        signin_pause._resume(DB(), state, "claude", signin_pause.Check("ok", ident="me"))
        signin_pause._commit(state, base)
        committed.set()

    monkeypatch.setattr(agents, "_launch", launch)
    # What recovery reads, it reads once that record has gone.
    monkeypatch.setattr(signin_pause, "paused_for_signin", lambda: committed.wait(10) and real_held())
    sweeping = threading.Thread(target=sweep)
    sweeping.start()
    assert inside.wait(5)
    recovering = threading.Thread(target=lifecycle.resume_after_start)
    recovering.start()
    release.set()
    sweeping.join(10)
    recovering.join(10)
    assert launched == ["w1", "chat"] and "w1" not in stopped and len(_live_windows("w1")) == 1


@pytest.mark.parametrize("lookup_fails, status", [
    pytest.param(False, "idle", marks=pytest.mark.variant("pausing one live chat must preserve a second chat sharing its tmux session")),
    pytest.param(False, "failed", marks=pytest.mark.variant("a second chat marked failed still has a live pane that prevents shared-session teardown")),
    pytest.param(True, "failed", marks=pytest.mark.variant("failed pane-placement lookup must preserve the shared session even when its other chat is marked failed")),
])
def test_pausing_a_chat_leaves_another_chat_in_its_tmux_session_running(db, root, monkeypatch, lookup_fails, status):
    def chat(aid, *make):
        pane = tmux._tmux(*make, "-P", "-F", "#{pane_id}", "cat").stdout.strip()
        tmux.set_pane_tag(pane, tmux.AGENT_TAG, aid)
        add(db, root, aid, status=status)       # marked failed, its pane can still be alive
        db.update_agent(aid, tmux_window=pane, tmux_server=tmux.current_server())
        return pane

    mine = chat("one", "new-session", "-d", "-s", root.tmux_session)
    other = chat("two", "new-window", "-t", f"={root.tmux_session}:")
    if lookup_fails:
        monkeypatch.setattr(tmux, "pane_places", mock.Mock(side_effect=tmux.TmuxTimeout("no answer")))
    agents.pause(db, "one", stop_procs=False, stop_local_models=False)
    assert not tmux.window_alive(mine) and tmux.window_alive(other)


@pytest.mark.parametrize("failing, error, status, carries_on, live_chat, collected, opens", [
    pytest.param("boss", agents.AgentError, "paused", False, False, False, False, marks=pytest.mark.variant("root failure before a pane opens restores paused status and prevents worker launches")),
    pytest.param("boss", OSError, "paused", False, False, False, True, marks=pytest.mark.variant("root launch raises an operating-system error after opening its pane, which must be cleaned up")),
    pytest.param("w1", agents.AgentError, "failed", True, False, False, False, marks=pytest.mark.variant("worker failure after this resume launched its root must not prevent sibling workers returning")),
    pytest.param("w1", agents.LaunchTaken, "starting", True, False, False, False, marks=pytest.mark.variant("a competing path owns the worker launch, so this resume must not mark that worker failed")),
    pytest.param("w1", agents.AgentError, "failed", True, True, False, False, marks=pytest.mark.variant("worker-only resume against an already live root must mark the failed worker and continue its siblings")),
    pytest.param("w1", agents.AgentError, "paused", True, True, True, False, marks=pytest.mark.variant("caller collecting unstarted workers must receive the failed worker paused for a later attempt")),
])
def test_a_launch_that_fails_in_a_resume(db, root, monkeypatch, failing, error, status, carries_on, live_chat, collected, opens):
    add(db, root, "boss")
    add(db, root, "w1", mode="assign", parent="boss")
    add(db, root, "w2", mode="assign", parent="boss")
    launched = []
    monkeypatch.setattr(procs, "stop", lambda ids, *a, **kw: 0)

    def launch(db_, a, ws, **kw):
        db_.set_status(a.id, "starting")    # as a launch does before its window opens
        if a.id == failing:
            opens and _open_pane(db_, a, launched=False)
            raise error("no")
        launched.append(a.id)

    monkeypatch.setattr(agents, "_launch", launch)
    if live_chat:
        db.set_status("boss", "idle")
        monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss")
    kw = {"only": {"w1", "w2"}} if live_chat else {}
    left = set() if collected else None
    with mock.MagicMock() if carries_on else pytest.raises(error):
        agents.resume(db, "boss", unstarted=left, **kw)
    assert db.get_agent(failing).status == status and ("w2" in launched) == carries_on
    assert left == ({"w1"} if collected else None) and not _live_windows(failing)


@pytest.mark.variant("the 2026-11-11 incident: an update restart left resumed sessions paused")
def test_a_sidebar_drawn_during_recovery_leaves_an_older_resumed_session_running(db, root, monkeypatch):
    # The 15:06 restart: a sidebar listed the panes before an older session's
    # chat window opened, with a newer session live, and marked the older one
    # paused as a chat that had gone, after its resume had brought it back.
    from brindle import view

    _recorded_sessions(db, root, monkeypatch, [("older", "a")])
    here = tmux.current_server()
    db.add_agent(Agent("newer", "ws-a", "supervisor", "claude", None, "interactive", "idle", "%live", None,
                       time.time() + 10, time.time(), "t", tmux_server=here))
    panes = tmux.PaneSnapshot(here)     # listed before the older chat's window opened
    panes["%live"] = True

    def launch(db_, a, ws, **kw):
        db_.set_status(a.id, "starting")    # as a launch does before its window opens
        view.snapshot(db_, ws.repo_root, panes=panes)
        db_.update_agent(a.id, tmux_window="%older", tmux_server=here)

    monkeypatch.setattr(agents, "_launch", launch)
    assert lifecycle.resume_after_start() == ["older"]
    assert db.get_agent("older").status == "starting"


def test_a_second_resume_of_a_session_waits_and_never_launches_beside_the_first(db, root, monkeypatch):
    add(db, root, "boss")
    monkeypatch.setattr(agents, "RESUME_LOCK_WAIT", 0.01)
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws, **kw: launched.append(a.id))
    with pipeline._file_lock("resume-boss"):
        with pytest.raises(agents.AgentError, match="still being resumed"):
            agents.resume(db, "boss")
    assert launched == []


@pytest.mark.variant("critical pressure must override the real admission gate's zero-running-children escape")
def test_critical_memory_holds_a_recovery_even_with_nothing_running(monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(spawn, "_gated", True)
    monkeypatch.setattr(spawn, "_running", 0)
    monkeypatch.setattr(spawn, "pressure", lambda: stop.set() or spawn.PRESSURE_CRITICAL)
    spawn.wait_for_room(stop)
    assert stop.is_set()        # it looked at memory, though nothing runs, and held until told to stop


def test_resume_reads_only_needed_keys_and_key_timing(db, root, monkeypatch, caplog):
    from dataclasses import replace
    from brindle import autopilot, debuglog, identity_lock, keystore, profiles, tasks

    pending, checked, answer = [], [], [True]

    def staff():
        checked.append(answer[0])
        if isinstance(answer[0], Exception):
            raise answer[0]
        return answer[0]

    monkeypatch.setattr(debuglog, "staff", staff)
    monkeypatch.setattr(debuglog, "_publish_timings", pending.append, raising=False)
    now, names = [100.0], ["ANTHROPIC_API_KEY", "PROFILE_API_KEY", "GEMINI_API_KEY"]
    real_profile = profiles.load_profile
    monkeypatch.setattr(profiles, "load_profile", lambda name, repo_root=None:
                        replace(real_profile(name, repo_root), api_key_env=names[1]))

    def advance(seconds, result=None):
        now[0] += seconds
        return result

    clock = mock.Mock(wraps=time)
    clock.monotonic = lambda: now[0]
    for module in (agents, keystore, spawn):
        monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(keystore, "_keychain_reads", {})
    stores = {name: keystore.KeychainStore("timing", name) for name in names}
    blob = keystore._encode({"value": "secret-timing-value", "updated_at": 7})
    stores[names[1]]._read.blob, stores[names[1]]._read.expires = blob, 1000.0

    class ReadLock:
        def __enter__(self):
            advance(2)

        def __exit__(self, *args):
            pass

    for name, store in stores.items():
        store._read.lock = ReadLock()
        monkeypatch.setenv(name, "")   # restore the environment changed by resume
    for name, value in zip(names, ("saved-session-value", "fresh-recovery-value")):
        db.set_session_credential("chat", name, "saved-session-value")
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(keystore, "_key_store", lambda name: stores[name])
    monkeypatch.setattr(keystore, "known_names", lambda: advance(1, names))
    monkeypatch.setattr(providers, "invalidate_signed_in", lambda: advance(0.5))
    real_admit = spawn._admit
    monkeypatch.setattr(spawn, "_admit", lambda *a, **kw: advance(0.5, real_admit(*a, **kw)))
    monkeypatch.setattr(spawn, "_run_read", lambda argv, *a: advance(3, subprocess.CompletedProcess(argv, 0, blob, "")))
    real_write = db.set_session_credential
    monkeypatch.setattr(db, "set_session_credential", lambda *a: advance(0.25, real_write(*a)))
    monkeypatch.setattr(identity_lock, "ensure", lambda a: advance(0.75))
    monkeypatch.setattr(agents, "settle_panes", lambda *a: set())
    monkeypatch.setattr(autopilot, "reload_goals_file", lambda *a: advance(0.5))
    monkeypatch.setattr(providers.ClaudeCode, "can_resume", staticmethod(lambda ref: advance(1, True)))
    monkeypatch.setattr(pipeline, "on_resumed", lambda *a: advance(1.25))

    def queued_tasks(*a):
        advance(1.5)
        agents._window_seconds.get().append(0.5)   # a queued launch's window belongs to tasks

    monkeypatch.setattr(tasks, "on_resumed", queued_tasks)

    def launch(*a, **kw):
        advance(7)
        agents._window_seconds.get().append(4)

    monkeypatch.setattr(agents, "_launch", launch)
    add(db, root, "chat")
    agents.resume(db, "chat", gate=lifecycle.Recovery().launching,
                  recovery_env={names[0]: "saved-session-value", names[1]: "fresh-recovery-value"})
    assert db.get_session_credentials("chat") == {
        names[0]: "secret-timing-value", names[1]: "fresh-recovery-value"}
    assert len(pending) == 1 and not checked and not caplog.records
    debuglog._write_timings(pending.pop())
    details = [r.args[1] for r in caplog.records if isinstance(r.args, tuple)
               and len(r.args) == 2 and isinstance(r.args[1], dict)]
    # Two `security` calls: the keychain listing (not understood here, so it rules nothing out), then the key.
    assert details == [{"invalidate_s": 0.5, "known_names_s": 1.0, "key_loads_s": 11.0,
                        "key_reads": 2, "key_cached": 1, "key_lock_s": 4.0, "security_s": 6.0,
                        "credential_writes_s": 0.5, "identity_s": 0.75, "goals_s": 0.5,
                        "provider_prep_s": 1.0, "launch_s": 3.0, "pipeline_s": 1.25, "tasks_s": 1.5}]
    assert "secret-timing-value" not in caplog.text and all(name not in caplog.text for name in names)
    # Exercise both lifecycle timing lines through the real startup dispatcher.
    db.update_agent("chat", tmux_server=tmux.current_server())
    monkeypatch.setattr(lifecycle, "_read_resume", lambda: [{"id": "chat", "server": tmux.current_server()}])
    monkeypatch.setattr(lifecycle, "_record_resume", lambda entries: True)
    monkeypatch.setattr(agents, "panes_match", lambda *a: True)
    monkeypatch.setattr(procs, "stop", lambda *a, **kw: 0)
    caplog.clear()
    assert lifecycle.resume_after_start() == ["chat"]
    assert not caplog.records and len(pending) == 1
    debuglog._write_timings(pending.pop())
    assert len(caplog.records) == 4
    for value in (False, None, RuntimeError("offline")):
        answer[0] = value
        caplog.clear()
        before = len(checked)
        assert lifecycle.resume_after_start() == ["chat"]
        assert len(checked) == before and len(pending) == 1 and not caplog.records
        debuglog._write_timings(pending.pop())
        assert not caplog.records
    real_resume = agents.resume
    monkeypatch.setattr(agents, "resume", mock.Mock(side_effect=agents.AgentError("unavailable")))
    assert lifecycle.resume_after_start() == []
    debuglog._write_timings(pending.pop())
    assert len(caplog.records) == 1 and caplog.records[0].exc_info
    monkeypatch.setattr(agents, "resume", real_resume)
    answer[0] = True
    caplog.clear()
    agents.resume(db, "chat")
    assert not caplog.records


@pytest.mark.variant("each launch step is timed per agent and provider through the real launch path")
def test_resume_launch_step_timing(db, root, monkeypatch, caplog):
    """The real launch, its slow dependencies replaced by ones that move a fake clock."""
    from types import SimpleNamespace

    from brindle import company_identity, debuglog, launch_health, preflight, provider_homes, upgrade

    monkeypatch.setattr(debuglog, "staff", lambda: True)
    now = [100.0]
    clock = mock.Mock(wraps=time)
    clock.monotonic = lambda: now[0]
    monkeypatch.setattr(agents, "time", clock)

    def slow(module, name, seconds, result=None):
        def fn(*a, **kw):
            now[0] += seconds
            return result() if callable(result) else result
        monkeypatch.setattr(module, name, fn)

    monkeypatch.setattr(agents, "settle_panes", lambda *a, **kw: set())
    slow(agents, "_prepare_launch_health", 0.0)
    monkeypatch.setattr(agents, "_profile_for", lambda *a: SimpleNamespace(name="p", model="m"))
    for module, name, result in ((upgrade, "check_launch", None), (preflight, "turned_off", None),
                                 (agents, "model_problem", None), (agents, "launch_blockers", []),
                                 (company_identity, "launch_check", (None, None, None)),
                                 (agents, "launch_identity", ({}, None, ())),
                                 (agents.pane_auth, "key_problem", None), (providers, "trust_folder", None),
                                 (providers, "claude_chrome_chosen", None), (agents, "_open_window", "%1"),
                                 (launch_health, "bind", None), (launch_health, "helper_token", ()),
                                 (spawn, "start", None), (tmux, "mark_launched", None)):
        slow(module, name, 0.0, result)
    slow(provider_homes, "decide", 0.0)
    slow(tmux, "mark_launched", 0.5)
    slow(providers.ClaudeCode, "command", 1.5, ["claude"])
    slow(providers, "trust_codex_project", 0.25)
    slow(providers.Codex, "command", 4.0, ["codex"])
    slow(company_identity, "launch_check", 0.75, (None, None, None))
    monkeypatch.setattr(agents, "managed", lambda root: None)
    monkeypatch.setattr(agents, "_add_dirs_warning", lambda *a: None)
    monkeypatch.setattr(agents, "_window_environment", lambda *a: ({}, [], None, [], None))
    monkeypatch.setattr(signin_pause, "record_launch", lambda a: None)
    add(db, root, "chat")
    add(db, root, "w1", mode="assign", parent="chat")
    db.update_agent("w1", provider="codex")
    resumed = agents.resume(db, "chat", gate=lifecycle.Recovery().launching)
    assert {a.id for a in resumed} == {"chat", "w1"}      # both launches ran to the end
    details = [r.args[1] for r in caplog.records if isinstance(r.args, tuple)
               and len(r.args) == 2 and isinstance(r.args[1], dict)]
    steps = details[0]["launch_steps"]
    assert steps["w1"]["mark_launched_s"] == 0.5          # a step after the window opened
    assert {k: steps["chat"][k] for k in ("provider", "command_s", "company_identity_s")} == {
        "provider": "claude", "command_s": 1.5, "company_identity_s": 0.75}
    assert {k: steps["w1"][k] for k in ("provider", "command_s", "codex_trust_s")} == {
        "provider": "codex", "command_s": 4.0, "codex_trust_s": 0.25}


@pytest.mark.parametrize("early", [False, True], ids=["while-busy", "before-ordered"])
def test_recovery_opened_session_first(db, root, monkeypatch, early):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from brindle.daemon.app import prioritize_recovery

    _recorded_sessions(db, root, monkeypatch, [("busy", "a"), ("second", "b"), ("opened", "c")])
    monkeypatch.setattr(lifecycle, "RESUME_PARALLEL", 1)
    recovery, inside, go, order = lifecycle.Recovery(), threading.Event(), threading.Event(), []
    if early:   # opened while the start still puts its sessions in order (the app already lists them pending)
        real_order = lifecycle._most_recent_first
        monkeypatch.setattr(lifecycle, "_most_recent_first",
                            lambda db_, entries: recovery.prioritize("opened") or real_order(db_, entries))
        go.set()

    def resume(db_, root_id, **kw):
        order.append(root_id)
        if root_id == "busy":
            inside.set()
            go.wait(10)

    monkeypatch.setattr(agents, "resume", resume)
    app = Starlette(routes=[Route("/p", prioritize_recovery, methods=["POST"])])
    app.state.recovery = recovery
    start = threading.Thread(target=lifecycle.resume_after_start, args=(recovery,))
    start.start()
    assert inside.wait(5)
    with TestClient(app) as client:
        for opened in ("opened", "busy", "nobody"):     # already launching, or none such: nothing happens
            assert client.post("/p", json={"root": opened}).status_code == 200
    go.set()
    start.join(10)
    assert order == (["opened", "busy", "second"] if early else ["busy", "opened", "second"])


@pytest.mark.variant("generated child shell must refuse to execute its payload when its tmux ownership write fails")
def test_a_pane_that_cant_say_whose_it_is_runs_nothing(tmp_path, monkeypatch):
    tmux._tmux("new-session", "-d", "-s", "own", "cat")
    ran, real_base = tmp_path / "ran", tmux._base
    monkeypatch.setattr(tmux, "_base", lambda: [real_base()[0], "-L", "no-such-server"])
    command = tmux._tags_itself(["/bin/sh", "-c", f"touch {ran}"], (tmux.AGENT_TAG, "chat"))
    monkeypatch.setattr(tmux, "_base", real_base)
    over = shlex.join([*real_base(), "wait-for", "-S", "over"])
    tmux._tmux("new-window", "-t", "=own:", "--", "/bin/sh", "-c", f'"$@"; {over}', "x", *command)
    tmux._tmux("wait-for", "over")
    assert not ran.exists()


@pytest.mark.parametrize("level, checks", [(1, 1), (spawn.PRESSURE_CRITICAL, 2)])
def test_a_recovery_wait_gives_up_on_busy_cpu_but_holds_for_critical_memory(monkeypatch, level, checks):
    import threading

    stop, seen = threading.Event(), []
    monkeypatch.setattr(spawn, "POLL", 0)
    monkeypatch.setattr(spawn, "_has_room", lambda waits=True: seen.append(1) and False)

    def pressure():         # the wait goes round again only for critical pressure: this ends it then
        stop.set()
        return level

    monkeypatch.setattr(spawn, "pressure", pressure)
    spawn.wait_for_room(stop, busy_limit=0)
    assert len(seen) == checks


def test_a_start_waits_for_room_and_a_stop_ends_the_wait(db, root, monkeypatch):
    from brindle import spawn
    from brindle.daemon import lifecycle

    _recorded_sessions(db, root, monkeypatch, [("chat", "a")])
    recovery, began = lifecycle.Recovery(), []
    monkeypatch.setattr(spawn, "_has_room", lambda waits=True: recovery.stopping.set())
    monkeypatch.setattr(agents, "resume", lambda db_, root_id, **kw: began.append(root_id))
    assert lifecycle.resume_after_start(recovery) == [] and began == []
    assert [e["id"] for e in recovery.stop()] == ["chat"]


@pytest.mark.variant("another session changing the process environment must not replace a failed agent's recovery credential")
def test_a_failed_agent_gets_its_own_sessions_key_while_another_session_resumes(db, root, monkeypatch):
    from brindle import procs

    name = "ANTHROPIC_API_KEY"
    monkeypatch.setenv(name, "unrelated")
    monkeypatch.setattr(procs, "stop", lambda ids, *a, **kw: 0)
    add(db, root, "boss", status="failed")
    seen = []

    def launch(db_, a, ws, **kw):
        os.environ[name] = "another-sessions-choice"
        seen.append(agents.agent_env(ws, a.id, a).get(name))

    monkeypatch.setattr(agents, "_launch", launch)
    agents.resume(db, "boss", recovery_env={name: "this-sessions-choice"})
    assert seen == ["this-sessions-choice"]


@pytest.mark.parametrize("mode, result, stage, session, plan, queued, status, says", [
    ("assign", "reported", "fixing", True, None, False, "processing", "continue your task"),    # agent 44196488's incident
    ("assign", "reported", "fixing", False, None, False, "processing", "missing check for None"),
    ("assign", "reported", "resolving", False, None, False, "processing", "conflicts with its base"),
    ("review", None, None, True, None, False, "processing", "continue your task"),
    ("assign", None, None, True, "proposed", False, "starting", None),      # its plan waits on its supervisor
    ("assign", None, None, True, None, True, "starting", None),     # a queued message starts its turn
    ("assign", "reported", "reviewing", True, None, False, "done", None),   # its report stands
    ("assign", "reported", "fixing", True, "merged", False, "done", None),  # merged by hand during the pause
])
def test_what_was_at_work_carries_on_after_a_pause_and_resume(
        db, root, monkeypatch, mode, result, stage, session, plan, queued, status, says):
    from brindle import company_identity, spawn
    monkeypatch.setattr(providers.ClaudeCode, "can_resume", staticmethod(lambda ref: True))
    opened = {}     # launches run for real up to their window, whose command is only recorded
    monkeypatch.setattr(agents, "_open_window",
                        lambda db_, a, ws, name, argv, *args, **kw: opened.update({a.id: " ".join(argv)}) or "%0")
    monkeypatch.setattr(agents, "_detach", lambda args: None)
    monkeypatch.setattr(spawn, "start", lambda argv, **kw: None)
    monkeypatch.setattr(company_identity, "launch_check", lambda provider, home=None: (None, None, None))
    here = tmux.current_server()
    add(db, root, "boss", status="idle")
    add(db, root, "w1", mode=mode, parent="boss", status="processing", result=result)
    pane = tmux._tmux("new-session", "-d", "-s", "w1-pane", "-P", "-F", "#{pane_id}", "cat").stdout.strip()
    chat = tmux._tmux("new-session", "-d", "-s", root.tmux_session, "-P", "-F", "#{pane_id}", "cat").stdout.strip()
    db.update_agent("boss", tmux_window=chat, tmux_server=here)
    db.update_agent("w1", tmux_window=pane, tmux_server=here, pipeline=stage,
                    plan_state=plan if plan != "merged" else None, session_ref="sess-w1" if session else None)
    head = git.out(["rev-parse", "HEAD"], root.path)
    if stage == "fixing":
        db.add_review(root.id, head, "rev", False, "missing check for None")
    if queued:
        db.enqueue("w1", "the supervisor's follow-up", "boss")
    assert agents.is_alive(db.get_agent("w1"))
    agents.pause(db, "boss", stop_procs=False, stop_local_models=False)
    if plan == "merged":
        db.set_merged(root.id, head)
    agents.resume(db, "boss")
    assert db.get_agent("w1").status == status and ("w1" in opened) == (status != "done")
    if "w1" in opened:
        assert ("--resume" in opened["w1"]) == session
        assert says in opened["w1"] if says else agents.HEADLESS_CONTINUE not in opened["w1"]


def test_a_resumed_chat_is_shown_with_its_conversation_and_sidebar(db, root, monkeypatch, tmp_path):
    status, chat_rows = "on", 39
    # #264: the resumed conversation was drawn in a detached 80x24 window, then
    # resized by the sidebar's split and again by the terminal attaching, and
    # Claude Code, which wipes what it drew when resized, showed a black screen.
    # The stub draws a conversation, wipes it on a resize as Claude Code does,
    # and records the size it drew at.
    from brindle import cli

    saved = tmp_path / "claude-config" / "projects" / "p"
    saved.mkdir(parents=True)
    (saved / "sess-boss.jsonl").write_text("{}")
    drew, tmux_cmd = tmp_path / "drew", [shutil.which(tmux._bin()), *tmux._base()[1:]]
    stub = tmp_path / "claude-stub"
    stub.write_text("#!/bin/sh\n"
                    # The sidebar's `claude -p /usage` must not overwrite what the chat drew.
                    '[ "$1" = -p ] && exit 1\n'
                    "trap 'printf \"\\033[2J\\033[H> \"' WINCH\n"
                    f'echo "$(stty size) $*" > {drew}\n'
                    "printf 'you: where were we\\nclaude: the earlier conversation\\n> '\n"
                    f'{shlex.join([*tmux_cmd, "wait-for", "-S", "stub-drew"])}\n'
                    # A line typed in is answered once the pane's size (tmux sends a
                    # resize a moment later) has reached the stub, and so its wipe.
                    "while :; do read -r line\n"
                    f'  until [ "$(stty size)" = "$({shlex.join([*tmux_cmd, "display-message", "-p", "-t"])}'
                    ' "$TMUX_PANE" "#{pane_height} #{pane_width}")" ]; do :; done\n'
                    f'  {shlex.join([*tmux_cmd, "wait-for", "-S", "stub-read"])}\ndone\n')
    stub.chmod(0o755)
    monkeypatch.setenv("BRINDLE_CLAUDE_BIN", str(stub))
    for var in ("TMUX", "TMUX_PANE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(os, "get_terminal_size", lambda fd=1: os.terminal_size((120, 40)))
    # The sidebar runs this checkout's `brindle watch`, not whichever one is installed.
    monkeypatch.setattr(providers, "_in_worktree", lambda path: False)
    tmux._tmux("set-option", "-g", "status", status)
    add(db, root, "boss")
    agents.resume(db, "boss")
    tmux._tmux("wait-for", "stub-drew")

    # The person's terminal, 120x40, attached by `brindle continue`'s own _attach.
    # CI runners set no TERM, and a tmux client without one exits at once.
    monkeypatch.setenv("TERM", "xterm-256color")
    terminal, tty = pty.openpty()
    fcntl.ioctl(tty, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    clients, run = [], subprocess.run

    def attach(argv, *a, **kw):
        if "attach-session" not in argv:
            return run(argv, *a, **kw)
        clients.append(subprocess.Popen(argv, stdin=tty, stdout=tty, stderr=tty, start_new_session=True))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", attach)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_after_detach", lambda ws: None)
    tmux._tmux("set-hook", "-t", root.tmux_session, "client-attached", "wait-for -S attached")
    cli._attach(root, db.get_agent("boss").tmux_window)
    try:
        tmux._tmux("wait-for", "attached")
        chat, sidebar = db.get_agent("boss").tmux_window, db.get_sidebar_pane("boss")
        tmux.send_keys(chat, "Enter")
        tmux._tmux("wait-for", "stub-read")
        client = tmux._tmux("list-clients", "-t", f"={root.tmux_session}", "-F", "#{client_name}").stdout.split()[0]
        showing = tmux._tmux("display-message", "-p", "-c", client, "#{window_id}").stdout.strip()
        assert tmux.pane_window(chat) == showing == tmux.pane_window(sidebar)
        assert tmux.window_alive(sidebar)
        on_screen = tmux._tmux("capture-pane", "-p", "-t", chat).stdout   # not the scrollback
        assert "claude: the earlier conversation" in on_screen
        rows, cols, *argv = drew.read_text().split()
        assert "--resume sess-boss" in " ".join(argv)
        shown = tmux._tmux("display-message", "-p", "-t", chat, "#{pane_height} #{pane_width}").stdout.split()
        assert [rows, cols] == shown == [str(chat_rows), str(120 - 30 - 1)]  # never resized once drawn
    finally:
        for c in clients:
            c.kill()
            c.wait()
        os.close(tty)
        os.close(terminal)


def test_resume_rebuilds_worker_decoration_when_the_session_cant_be_resumed(db, root, monkeypatch, tmp_path):
    """a.task is stored raw (see agents.decorate_worker_prompt); a fresh
    restart needs the finish line, WORKER_FOOTER and /goal wrapper rebuilt,
    same as at the first launch."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))  # no saved session
    w1 = Agent("w1", root.id, "developer", "claude", "boss", "assign", "paused", "", None,
              time.time(), time.time(), "Add input validation.", "sess-w1",
              done_when="tests/test_x.py passes")
    db.add_agent(w1)
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: launched.append(kw))
    agents.resume(db, "w1")
    assert db.get_autopilot("w1") is None
    prompt = launched[0]["prompt"]
    assert prompt.startswith("/goal Finish line: tests/test_x.py passes")
    assert "report_result" in prompt  # WORKER_FOOTER
    assert prompt.endswith(agents.RESUME_NOTE)


# -- dropping a session must never touch a newer session's windows ----------------
#
# Regression: the first `brindle` after a tmux server restart (a reboot, or the
# previous session's end taking the server with it) got the chat's pane as %1
# and the sidebar as %2, the same ids every earlier session's chat had. The
# detached cull's retention then dropped the oldest paused session and closed
# its windows by those stored ids: the new chat and its sidebar vanished,
# leaving the person on the session's bare shell window.


def _live_pane(root, tag=None):
    tmux.ensure_session(root.tmux_session, root.path, {})
    return tmux.new_window(root.tmux_session, "chat", root.path, ["sleep", "300"], {}, tag=tag)


@pytest.mark.parametrize("tag, survives", [
    ((agents.AGENT_TAG, "launching"), True),   # its row isn't recorded yet
    ((agents.AGENT_TAG, "old"), False),        # its own leftover window
])
def test_dropping_a_session_spares_a_pane_that_now_belongs_to_a_launching_chat(db, root, tag, survives):
    pane = _live_pane(root, tag=tag)
    for i in range(3):
        add(db, root, f"s{i}", since=2000 + i)
    add(db, root, "old", since=1000)
    db.update_agent("old", tmux_window=pane)  # the same id, on the server that's gone
    assert sessions.enforce(db, root.repo_root, now=2100) == 1
    assert db.get_agent("old") is None
    assert tmux.window_alive(pane) == survives


@pytest.mark.parametrize("elsewhere", [False, True])
def test_failed_but_running_session_survives_retention(db, tmp_path, monkeypatch, elsewhere):
    """A chat marked "failed" (its brief's delivery unconfirmed, an auth
    error on a turn) keeps running. Retention once took it for a paused
    session past KEEP: it closed the live chat's window, removed its worktree
    and deleted its records. ``elsewhere``: the chat runs on another tmux
    server, whose pane id this server has given to some other agent."""
    from brindle import pool
    from brindle.db import Workspace

    repo = str(tmp_path / "proj")
    for wid, name, kind in [("proj/root", "root", "main"), ("proj/session-2", "session-2", "worktree")]:
        db.add_workspace(Workspace(wid, repo, name, kind, name, "main", f"{repo}/{name}", None,
                                   f"brindle-{name}", time.time()))
    root = db.get_workspace("proj/root")
    add(db, db.get_workspace("proj/session-2"), "live", status="failed", since=1000)
    server = "elsewhere" if elsewhere else tmux.current_server()
    db.update_agent("live", tmux_window="%9", tmux_server=server)
    for i in range(3):
        add(db, root, f"s{i}", since=2000 + i)
    snaps = {}
    for srv, owner in [(tmux.current_server(), "other" if elsewhere else "live"), (server, "live")]:
        snaps[srv] = tmux.PaneSnapshot(srv)
        snaps[srv]["%9"] = True
        snaps[srv].tags["%9"] = {agents.AGENT_TAG: owner}
    here = tmux.current_server()
    monkeypatch.setattr(tmux, "list_panes", lambda server=here: snaps[server])
    monkeypatch.setattr(tmux, "window_alive", lambda target, server=here: snaps[server].get(target, False))
    killed, removed = [], []
    monkeypatch.setattr(tmux, "kill_window", killed.append)
    monkeypatch.setattr(workspaces, "remove", lambda db_, ws, **kw: removed.append(ws.id))
    monkeypatch.setattr(pool, "fill_in_background", lambda repo_root: None)

    assert sessions.enforce(db, repo, now=2100) == 0
    assert db.get_agent("live") is not None and killed == [] and removed == []


def test_sessions_cmd_lists_a_session_with_workers(db, root, repo, monkeypatch):
    # Issue #34: a paused session with workers crashed `brindle sessions`
    # (workspaces went into a set, and Workspace isn't hashable).
    from typer.testing import CliRunner

    from brindle.cli import app

    sup = add(db, root, "sup")
    ws = workspaces.create(db, str(repo), "feat/x").workspace
    add(db, ws, "w1", mode="assign", parent=sup.id)
    add(db, ws, "r1", mode="review", parent=sup.id)     # shares the worker's workspace
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["sessions"])
    assert res.exit_code == 0, res.output
    assert "MB in worktrees" in res.output
