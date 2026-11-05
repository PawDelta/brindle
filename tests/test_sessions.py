import fcntl
import os
import pty
import shlex
import shutil
import struct
import subprocess
import termios
import time

import pytest

from brindle import agents, git, providers, sessions, tmux, workspaces
from brindle.db import Agent
from git_snapshots import fast_host_tools, repo, workspaces_once  # noqa: F401  (fixtures)


def add(db, ws, aid, mode="interactive", parent=None, status="paused", since=None, result=None):
    a = Agent(aid, ws.id, "supervisor" if mode == "interactive" else "developer", "claude", parent,
              mode, status, "", result, time.time(), since or time.time(), "task", f"sess-{aid}")
    db.add_agent(a)
    return a


@pytest.fixture
def root(db, repo):
    return workspaces.adopt_root(db, str(repo))


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
    serve(app_gone=False)
    assert stopped_again and lifecycle.read_info() is None, "a second stop cut the way out short"
    assert not swept.is_set(), "the daemon waited for the sweep before it paused and exited"
    assert hook_kept.wait(5), "the Codex hook's trust wasn't looked at when the daemon started"
    held.set()
    assert [db.get_agent(a).status for a in ("chat", "helper", "foreign")] == ["paused", "paused", "idle"]
    assert not tmux.has_session(root.tmux_session) and tmux.has_session("shared-name")
    assert not tmux.has_session("side") and db.get_sidebar_pane("chat") is None
    assert lifecycle._read_resume() == [{"id": "chat", "server": here, "paused": True}]
    # It told the app to wait for its pause (daemon.rs stop_daemon), and not past it.
    assert saying == [f"{os.getpid()}\n"] and not lifecycle._pausing_path().exists()
    # The next app's daemon is stopped as its start takes that record, past the way out's wait
    # for it (RESUME_WAIT 0): the record the way out writes again isn't taken after it.
    recoveries = []

    class Watched:
        """A Recovery's lock, saying when the way out has to wait for it."""

        def __init__(self):
            self.inner = threading.Lock()

        def __enter__(self):
            if not self.inner.acquire(blocking=False):
                go.set()
                self.inner.acquire()

        def __exit__(self, *exc):
            self.inner.release()

    class Recovery(lifecycle.Recovery):
        def __init__(self):
            super().__init__()
            self.lock = Watched()
            recoveries.append(self)

    real_record, real_path = lifecycle._record_resume, lifecycle._resume_path

    def record(entries):
        written = real_record(entries)
        go.set()
        return written

    class Held(type(real_path())):
        def unlink(self, missing_ok=False):
            if holding:
                hold(holding.pop())
            super().unlink(missing_ok=missing_ok)

    taken = threading.Event()
    holding.append(taken)
    monkeypatch.setattr(lifecycle, "Recovery", Recovery)
    monkeypatch.setattr(lifecycle, "RESUME_WAIT", 0)
    monkeypatch.setattr(lifecycle, "_record_resume", record)
    monkeypatch.setattr(lifecycle, "_resume_path", lambda: Held(real_path()))
    serve(app_gone=False, after=taken)
    assert recoveries[-1].idle.wait(10), "the start never ended"
    assert events == [] and lifecycle._read_resume() == [{"id": "chat", "server": here, "paused": True}]
    # The next is stopped while it brings the session back (having stopped what the cull never
    # did), its launch held up in its identity probe: the way out records and pauses without
    # waiting for that probe, nothing is launched past its pause, and the session is recorded again.
    go.clear()
    probed = threading.Event()
    probing.append(probed)
    serve(app_gone=False, after=probed)
    assert recoveries[-1].idle.wait(10), "the held resume never ended"
    assert events == [("stop", ["chat", "helper"])] and db.get_agent("chat").status == "paused"
    assert lifecycle._read_resume() == [{"id": "chat", "server": here, "paused": True}]
    # The start after that brings it back, once, having stopped what the cull never did first.
    events.clear()
    db.set_status("helper", "processing")   # launched past the pause (a queued task's helper): comes back too
    assert lifecycle.resume_after_start() == ["chat"]
    assert events == [("stop", ["chat", "helper"]), ("launch", "chat"), ("launch", "helper")]

    # The app is force quit, and the tmux server stops answering: the pause is cut short at its
    # budget (the server answers with a failure once the daemon has gone).
    events.clear()
    for aid in ("chat", "helper"):
        db.set_status(aid, "idle")
    db.update_agent("chat", tmux_window=window(root.tmux_session))
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
    assert lifecycle._read_resume() == [{"id": "chat", "server": here, "paused": False}]
    # The next start finishes that pause (its session closed) and stops what is left first.
    assert lifecycle.resume_after_start() == ["chat"]
    assert not tmux.has_session(root.tmux_session)
    assert events == [("stop", ["chat", "helper"]), ("launch", "chat"), ("launch", "helper")]

    # A record made on another tmux server isn't resumed here.
    lifecycle._record_resume([{"id": "chat", "server": "elsewhere", "paused": True}])
    assert lifecycle.resume_after_start() == [] and len(events) == 3


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
