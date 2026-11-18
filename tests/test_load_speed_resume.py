"""After an app restart the daemon brings its sessions back (resume_after_start)
while the app shows them as resuming. That took 28 s on a machine with six
sessions: four came back at a time, and a session's workers launched one
after another, so the session with three workers took 26 s by itself.
Launches that can overlap must overlap: recovery takes about one launch,
however many sessions or workers there are."""
import contextvars
import subprocess
import threading
import time
from concurrent.futures import Future

import pytest

from brindle import (agents, company_identity, company_login, debuglog, pipeline, procs, providers, spawn, tasks,
                     tmux, workspaces)
from brindle.daemon import lifecycle
from brindle.db import Agent, Workspace


@pytest.fixture(autouse=True)
def no_timing_backend(monkeypatch):
    monkeypatch.setattr(debuglog, "staff", lambda: False)
    monkeypatch.setattr(debuglog, "_publish_timings", debuglog._write_timings)


class Launches:
    """A fake launcher whose clock is the launches themselves: each takes one
    tick, and launches under way at once share it. A launch holds until
    ``width`` launches are under way together, then all of them end, so a
    recovery that overlaps them takes one tick and ``peak`` is ``width``; one
    that runs them in turn only ever has ``peak`` below it (each held launch
    gives up after a bound, so the shortfall shows instead of hanging)."""

    def __init__(self, width: int, chats_overlap: bool):
        self.width, self.chats_overlap = width, chats_overlap
        self.under_way = self.peak = 0
        self.launched: list[str] = []
        self.ticked = threading.Condition()

    def launch(self, db, agent, ws, **kw):
        with self.ticked:
            self.under_way += 1
            self.peak = max(self.peak, self.under_way)
            self.ticked.notify_all()
            if agent.parent_id is not None or self.chats_overlap:
                self.ticked.wait_for(lambda: self.peak >= self.width, 2.0)
            self.under_way -= 1
            self.launched.append(agent.id)


def _add(db, tmp_path, aid, parent, here):
    path = tmp_path / aid
    path.mkdir()
    db.add_workspace(Workspace(id=f"ws-{aid}", repo_root=str(tmp_path), name=aid, kind="worktree", branch=aid,
                               base_branch="main", path=str(path), port_base=None, tmux_session=f"t-{aid}",
                               created_at=time.time()))
    mode = "assign" if parent else "interactive"
    db.add_agent(Agent(aid, f"ws-{aid}", "developer" if parent else "supervisor", "claude", parent, mode, "paused",
                       "", None, time.time(), time.time(), "t", tmux_server=here))


@pytest.mark.parametrize("sessions, workers", [
    pytest.param(8, 0, marks=pytest.mark.variant("Many sessions must recover in parallel, not one after another")),
    (1, 6),
], ids=["many sessions", "many workers"])
def test_recovery_time_does_not_grow_with_sessions_or_workers(db, tmp_path, monkeypatch, sessions, workers):
    here = tmux.current_server()
    roots = [f"s{n}" for n in range(sessions)]
    for root in roots:
        _add(db, tmp_path, root, None, here)
        for n in range(workers):
            _add(db, tmp_path, f"{root}-w{n}", root, here)
    lifecycle._record_resume([{"id": root, "server": here} for root in roots])
    monkeypatch.setattr(procs, "stop", lambda ids, *a, **kw: 0)
    monkeypatch.setattr(spawn, "wait_for_room", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "settle_panes", lambda db_, candidates, **kw: set())
    monkeypatch.setattr(agents, "panes_match", lambda *a, **kw: True)
    monkeypatch.setattr(agents, "_fresh_start_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline, "on_resumed", lambda *a, **kw: None)
    monkeypatch.setattr(tasks, "on_resumed", lambda *a, **kw: None)
    # Sessions overlap with each other; within one, the chat comes first and its workers overlap.
    launches = Launches(width=workers or sessions, chats_overlap=not workers)
    monkeypatch.setattr(agents, "_launch", launches.launch)

    assert sorted(lifecycle.resume_after_start()) == roots
    assert len(launches.launched) == sessions * (1 + workers)
    assert launches.peak == launches.width


def test_a_session_is_back_once_its_pane_runs_before_claude_draws_its_prompt(db, tmp_path, monkeypatch):
    """Recovery waited, per session, for Claude to load its conversation and
    draw its input box (6-7 s of a 17 s restart, more for a long transcript)
    before releasing it to the app, and behind all its workers' launches. A
    session is back once its chat's pane runs, its workers still coming; what
    needs the prompt (its queued mail) waits for it on the side."""
    here = tmux.current_server()
    _add(db, tmp_path, "s", None, here)
    _add(db, tmp_path, "w", "s", here)
    db.enqueue("s", "mail from a worker", None)
    lifecycle._record_resume([{"id": "s", "server": here}])
    monkeypatch.setattr(procs, "stop", lambda ids, *a, **kw: 0)
    monkeypatch.setattr(spawn, "wait_for_room", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "settle_panes", lambda db_, candidates, **kw: set())
    monkeypatch.setattr(agents, "panes_match", lambda *a, **kw: True)
    monkeypatch.setattr(agents, "_fresh_start_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline, "on_resumed", lambda *a, **kw: None)
    monkeypatch.setattr(tasks, "on_resumed", lambda *a, **kw: None)

    drawn, flushed, worker_up, worker_go = (threading.Event() for _ in range(4))

    def launch(db_, agent, ws, **kw):      # the pane runs; Claude is still loading in it
        if agent.parent_id:
            worker_up.set()
            worker_go.wait(5)       # a worker slow to launch, held until the chat is seen back
        db_.update_agent(agent.id, tmux_window=f"%{agent.id}")
        db_.set_status(agent.id, "starting")

    monkeypatch.setattr(agents, "_launch", launch)
    monkeypatch.setattr(tmux, "capture", lambda *a, **kw: "❯ \n? for shortcuts" if drawn.is_set() else "Resuming…")
    monkeypatch.setattr(agents, "flush", lambda db_, aid: flushed.set())

    recovery, back = lifecycle.Recovery(), []
    runner = threading.Thread(target=lambda: back.extend(lifecycle.resume_after_start(recovery)), daemon=True)
    runner.start()
    try:
        assert worker_up.wait(5)
        # The chat runs and its worker is still launching: shown back, though kept on the record.
        assert not recovery.resuming() and recovery.progress()["pending"] == []
        assert lifecycle._read_resume() == [{"id": "s", "server": here, "tries": 1}]
        assert not flushed.is_set()     # nothing typed before the prompt is up
        drawn.set()
        assert flushed.wait(5)      # its prompt is followed already, the worker not yet up
        assert not back
        worker_go.set()
        runner.join(5)
        assert back == ["s"] and lifecycle._read_resume() is None
    finally:
        worker_go.set()
        recovery.stopping.set()
        runner.join(5)


@pytest.mark.variant("the session the person last had open must be recovered before any other")
def test_the_session_the_person_had_open_comes_back_before_any_other_starts(db, tmp_path, monkeypatch):
    """Five sessions came back side by side, the one on screen among them:
    its launch shared the machine with four others. It now launches alone
    and is released before any other launch begins, however long it takes:
    the others wait for it, not for a time."""
    here = tmux.current_server()
    roots = [f"s{n}" for n in range(5)]
    for root in roots:
        _add(db, tmp_path, root, None, here)
    lifecycle._record_resume([{"id": root, "server": here} for root in roots])
    lifecycle.record_viewed("s3")
    monkeypatch.setattr(procs, "stop", lambda ids, *a, **kw: 0)
    monkeypatch.setattr(spawn, "wait_for_room", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "settle_panes", lambda db_, candidates, **kw: set())
    monkeypatch.setattr(agents, "panes_match", lambda *a, **kw: True)
    monkeypatch.setattr(agents, "_fresh_start_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline, "on_resumed", lambda *a, **kw: None)
    monkeypatch.setattr(tasks, "on_resumed", lambda *a, **kw: None)
    events, parked = [], threading.Condition()
    recovery = lifecycle.Recovery()

    class Head(threading.Event):
        """The head start, noting each session thread that waits on it."""
        waiting = 0

        def wait(self, *a):
            with parked:
                Head.waiting += 1
                parked.notify_all()
            return super().wait(*a)

    recovery.head = Head()
    held_with = []

    def launch(db_, agent, ws, **kw):
        if agent.id == "s3":
            # Held until every other session's thread is parked behind it, as a launch on a
            # loaded machine might be held for any time (the bound only ends a broken run).
            with parked:
                parked.wait_for(lambda: Head.waiting >= 4, 5)
            held_with.append(len(events))
        events.append(("launch", agent.id))

    monkeypatch.setattr(agents, "_launch", launch)
    release = getattr(recovery, "release", lambda root_id: True)

    def noted(root_id):
        if released := release(root_id):
            events.append(("back", root_id))
        return released

    monkeypatch.setattr(recovery, "release", noted, raising=False)

    assert sorted(lifecycle.resume_after_start(recovery)) == roots
    assert held_with == [0] and Head.waiting == 4      # nothing else launched while it was held
    assert events[:2] == [("launch", "s3"), ("back", "s3")]
    assert sorted(events[2:]) == sorted([*(("launch", r) for r in roots if r != "s3"),
                                         *(("back", r) for r in roots if r != "s3")])


@pytest.mark.variant("chats recovering together must share one supervisor-choice computation, not one each")
def test_chats_coming_back_together_work_out_the_supervisor_choice_once(monkeypatch):
    """Each chat's launch worked out the owner's supervisor choice on its own
    (profiles, policy, provider health probes): 2.3-2.7 s of a 4.4 s launch
    with five chats coming back at once. Within a recovery it is shared per repo."""
    calls, go = [], threading.Event()

    def choice(repo_root):
        calls.append(repo_root)
        go.wait(5)
        return None

    monkeypatch.setattr(agents, "supervisor_choice", choice)
    with agents.shared_launch_checks():
        threads = [threading.Thread(target=contextvars.copy_context().run,
                                    args=(agents._with_supervisor_choice, "profile", "claude", "/repo"))
                   for _ in range(5)]
        for thread in threads:
            thread.start()
        go.set()
        for thread in threads:
            thread.join(5)
    assert calls == ["/repo"]


@pytest.mark.variant("the managed-models read must happen during warm-up, not inside the first launch")
def test_a_recovery_reads_managed_models_beside_key_loads_not_in_its_first_launch(db, tmp_path, monkeypatch):
    """The first launch per repo of a recovery read the org's managed models
    (and the supervisor choice) itself: ~2 s cold inside its profile step.
    Recovery now starts that read per repo as it begins, beside the sessions'
    key loads; the launch takes its result rather than read it again."""
    from types import SimpleNamespace

    from brindle import codemap, preflight, provider_homes, upgrade

    here = tmux.current_server()
    _add(db, tmp_path, "s", None, here)
    lifecycle._record_resume([{"id": "s", "server": here}])
    monkeypatch.setattr(procs, "stop", lambda ids, *a, **kw: 0)
    monkeypatch.setattr(spawn, "wait_for_room", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "panes_match", lambda *a, **kw: True)
    monkeypatch.setattr(agents, "_fresh_start_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline, "on_resumed", lambda *a, **kw: None)
    monkeypatch.setattr(tasks, "on_resumed", lambda *a, **kw: None)
    # The real launch up to its profile, on a provider that starts no process.
    monkeypatch.setattr(upgrade, "check_launch", lambda: None)
    monkeypatch.setattr(preflight, "turned_off", lambda provider: None)
    monkeypatch.setattr(provider_homes, "decide", lambda provider: None)
    monkeypatch.setattr(agents, "supervisor_choice", lambda repo_root: None)
    monkeypatch.setattr(agents, "_airgap_check", lambda profile, repo_root: None)
    monkeypatch.setattr(agents, "get_provider", lambda name: SimpleNamespace(name=name, launches_process=False))
    monkeypatch.setattr(agents, "_model_check_key", lambda *a: ("model",))
    monkeypatch.setattr(agents, "model_problem", lambda *a, **kw: None)

    reads, reading, read_go, overlapped = [], threading.Event(), threading.Event(), []

    def managed(repo_root):      # slow: held until the session's own work before its launch is done
        reads.append(repo_root)
        reading.set()
        read_go.wait(5)

    def settle(db_, candidates, **kw):      # the session's pre-launch work, after its key loads
        overlapped.append(reading.wait(2))
        read_go.set()
        return set()

    # The chat's repo map: a cold render, held until the launch is inside its own profile.
    maps, in_profile = [], threading.Event()
    kind_overrides = agents._with_kind_overrides

    def profile_begins(*a):
        in_profile.set()
        return kind_overrides(*a)

    def supervisor_map(ws):
        maps.append(threading.current_thread().name)
        in_profile.wait(5)
        return "Repo map"

    monkeypatch.setattr(agents, "managed", managed)
    monkeypatch.setattr(agents, "settle_panes", settle)
    monkeypatch.setattr(agents, "_with_kind_overrides", profile_begins)
    monkeypatch.setattr(codemap, "supervisor_map", supervisor_map)

    assert lifecycle.resume_after_start() == ["s"]
    assert overlapped == [True]          # under way while the session loaded its keys
    assert reads == [str(tmp_path)]      # and read once: the launch took the warm-up's result
    assert len(maps) == 1                # one render, the launch waiting for the one under way


class FakeTmux:
    """tmux as brindle's runner (tmux._start) sees it, one record per tmux
    process: a chain (``cmd ; cmd``) runs its commands in turn and stops at
    the first that fails, as tmux does; a command named in ``rejects`` fails
    (an option an older tmux lacks)."""

    def __init__(self, rejects: tuple[str, ...] = ()):
        self.rejects, self.runs = rejects, []
        self.sessions: dict[str, dict[str, str]] = {}

    def one(self, args: list[str]) -> tuple[int, str]:
        if any(r in args for r in self.rejects):
            return 1, ""
        if args[0] == "has-session":
            return (0 if args[-1].lstrip("=") in self.sessions else 1), ""
        if args[0] == "new-session":
            self.sessions[args[args.index("-s") + 1]] = {}
        elif args[0] == "set-environment":
            self.sessions[args[2].lstrip("=")][args[3]] = args[4]
        elif args[0] == "new-window":
            return 0, f"%{len(self.runs)}\n"
        elif args[0] == "show-environment":
            return 0, "HOME=/h\n-UNSET\n"
        return 0, ""

    def start(self, base, args, input=None, **kw):
        self.runs.append(args)
        out, cmd = "", []
        for a in [*args, ";"]:
            if a != ";":
                cmd.append(a)
                continue
            rc, printed = self.one(cmd)
            out, cmd = out + printed, []
            if rc:
                return subprocess.CompletedProcess([*base, *args], rc, out, "")
        return subprocess.CompletedProcess([*base, *args], 0, out, "")


@pytest.mark.variant("Recovery must batch each window's environment into a few tmux calls: a per-variable "
                     "regression runs the same lines (real incident: 110-150 tmux calls per session)")
def test_a_session_recovery_opens_its_windows_in_a_few_tmux_calls(db, tmp_path, monkeypatch):
    """A session's recovery made 110-150 tmux calls: one `set-environment` per
    variable at every window, and the inherited environment asked four times a
    window, each scope on its own. Three agents' windows in one session now
    take a handful of tmux processes each."""
    fake = FakeTmux()
    monkeypatch.setattr(tmux, "_start", fake.start)
    path = tmp_path / "s"
    path.mkdir()
    ws = Workspace(id="ws-s", repo_root=str(tmp_path), name="s", kind="worktree", branch="s", base_branch="main",
                   path=str(path), port_base=None, tmux_session="t-s", created_at=time.time())
    db.add_workspace(ws)
    size = agents._resume_window_size.set((200, 50))      # as a recovery opens them
    try:
        for n in range(3):
            agent = Agent(f"a{n}", ws.id, "developer", "claude", "a0" if n else None, "assign", "starting", "",
                          None, time.time())
            db.add_agent(agent)
            agents._open_window(db, agent, ws, agent.id, ["true"], watch_pane=False)
    finally:
        agents._resume_window_size.reset(size)
    assert len(fake.runs) <= 24     # 60 when every variable and scope was its own process
    assert fake.sessions["t-s"].keys() >= workspaces.workspace_env(ws).keys()


def test_an_older_tmux_still_gets_every_session_update_it_accepts(monkeypatch):
    """ensure_session chains its updates to a running session into one tmux
    call; tmux stops a chain at the first command it rejects, so the ones
    before and after it are applied on their own."""
    fake = FakeTmux(rejects=("REJECTED",))
    fake.sessions["t-s"] = {}
    monkeypatch.setattr(tmux, "_start", fake.start)
    tmux.ensure_session("t-s", "/", {"A": "1", "REJECTED": "2", "C": "3"})
    assert fake.sessions["t-s"] == {"A": "1", "C": "3"}


def _ask_sign_in(monkeypatch, fake):
    for name in providers._ENV_AUTH["claude"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(providers, "_SIGNED_IN", {})
    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: fake())
    return lambda: providers.signed_out("claude")


def _ask_identity(monkeypatch, fake):
    monkeypatch.setattr(company_identity, "_cache", {})
    monkeypatch.setattr(company_login, "which", lambda name: name)
    monkeypatch.setattr(company_login, "run_cmd", lambda argv, *a, **kw: fake())
    return lambda: company_identity._probe(["claude", "auth", "status"])


SIGNED_IN = (0, '{"loggedIn": true}')


def _v(ask, answer, runs, id, why):
    return pytest.param(ask, answer, runs, id=id, marks=pytest.mark.variant(why))


@pytest.mark.parametrize("ask, answer, runs", [
    _v(_ask_sign_in, SIGNED_IN, 1, "sign-in signed in", "Concurrent sign-in checks share one CLI probe"),
    _v(_ask_sign_in, (1, "unreadable"), 1, "sign-in unknown", "An unreadable sign-in answer is shared, not re-probed"),
    _v(_ask_sign_in, None, 1, "sign-in timeout", "A timed-out sign-in probe is shared, not re-probed"),
    _v(_ask_identity, SIGNED_IN, 1, "identity signed in", "Concurrent identity checks share one CLI probe"),
    _v(_ask_identity, (127, ""), 1, "identity unknown", "An identity CLI that cannot run is shared, not re-probed"),
    _v(_ask_identity, (124, ""), 2, "identity timeout", "An identity timeout retries once longer, still shared"),
])
def test_launches_side_by_side_ask_the_cli_once(monkeypatch, ask, answer, runs):
    """The same restart ran `claude auth status` twenty times for nine agents
    signed in alike: each launch beside the others found the cache empty and
    started a CLI of its own. The first asks; the rest take its answer, an
    unknown one or a timeout too (neither is cached for later callers)."""
    width, lock = 6, threading.Condition()
    asked, answers, joined = [], [], []
    real_result = Future.result

    def result(self, *a, **kw):     # a caller taking the answer of the probe under way
        with lock:
            joined.append(1)
            lock.notify_all()
        return real_result(self, *a, **kw)

    def fake():
        with lock:
            asked.append(1)
            lock.notify_all()
            # Answers once every caller has either asked too or is waiting for this answer;
            # the bound only ends a broken run (callers queued behind a plain lock count as neither).
            lock.wait_for(lambda: len(asked) + len(joined) >= width, 5)
        return answer

    monkeypatch.setattr(Future, "result", result)
    probe = ask(monkeypatch, fake)
    threads = [threading.Thread(target=lambda: answers.append(probe())) for _ in range(width)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(asked) == runs
    assert len(answers) == width and len(set(answers)) == 1
