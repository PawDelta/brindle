"""After an app restart the daemon brings its sessions back (resume_after_start)
while the app shows them as resuming. That took 28 s on a machine with six
sessions: four came back at a time, and a session's workers launched one
after another, so the session with three workers took 26 s by itself.
Launches that can overlap must overlap: recovery takes about one launch,
however many sessions or workers there are."""
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
