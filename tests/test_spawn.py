"""brindle.spawn: the load-aware gate, request deadlines, and reads stopped cleanly."""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from pathlib import Path

import pytest
from conftest import run_scenario, sh

from brindle import agents, git, procs, providers, spawn, status_cache, tmux
from brindle.daemon import routes_actions
from brindle.daemon.app import Deadlines
from brindle.db import Agent, Workspace

REAL_PROBE = providers._auth_probe      # conftest stubs it for every test


@pytest.fixture
def overloaded(monkeypatch):
    """The gate on, a load well above the core count, one of our processes running."""
    monkeypatch.setattr(spawn, "_gated", True)
    monkeypatch.setattr(spawn, "_cores", lambda: 4)
    monkeypatch.setattr(spawn, "_load", lambda: 9.0)
    monkeypatch.setattr(spawn, "pressure", lambda: 1)
    monkeypatch.setattr(spawn, "_running", 1)


def _until(cond, within: float = 5.0) -> bool:
    end = time.monotonic() + within
    while not cond() and time.monotonic() < end:
        threading.Event().wait(0.02)
    return cond()


def _room(monkeypatch, load, level, expected):
    monkeypatch.setattr(spawn, "_cores", lambda: 8)
    monkeypatch.setattr(spawn, "_load", lambda: load)
    monkeypatch.setattr(spawn, "pressure", lambda: level)
    monkeypatch.setattr(spawn, "_running", 1)
    assert spawn.room() == expected


def _with_no_room_a_read_is_busy_at_its_deadline_and_a_background_one_skips(overloaded, repo):
    t0 = time.monotonic()
    with spawn.deadline(0.3), pytest.raises(git.GitBusy):     # both a GitError and a Busy
        git.run(["status"], repo)
    assert time.monotonic() - t0 >= 0.3

    t0 = time.monotonic()
    with pytest.raises(spawn.Skipped):
        spawn.read(["true"], skip=True)
    assert time.monotonic() - t0 < 0.2

    # An action never starts past its deadline: nothing was done.
    with spawn.deadline(0.1), pytest.raises(spawn.Busy):
        git.run(["commit", "--allow-empty", "-qm", "x"], repo)
    assert sh("git rev-list --count HEAD", repo) == "1"
    assert spawn.running() == 1


def _past_its_deadline_nothing_starts_even_with_room(tmp_path):
    marker = tmp_path / "ran"
    with spawn.deadline(-1):
        for start in (spawn.read, spawn.action, spawn.start):
            with pytest.raises(spawn.Busy):
                start(["touch", str(marker)])
        with pytest.raises(spawn.Busy):
            spawn.begin_write()            # a write from Python is refused the same way
        # The real sign-in probe (conftest stubs it) is gated too: no answer is "can't tell".
        assert REAL_PROBE(["touch", str(marker)]) is None
    assert not marker.exists()


def _a_started_helper_commits_its_request_and_a_started_read_does_not():
    with spawn.deadline(30) as dl:
        spawn.start(["true"], write=False).wait()
        assert not dl.committed and spawn.left(100) <= 30      # a read is still held to the deadline
        spawn.detach(["true"]).wait()
        assert dl.committed and spawn.left(100) == 100


def _serve(method: str, handler, disconnect_when=None, cancel_when=None) -> None:
    """One request through Deadlines whose route runs ``handler`` on a thread,
    as Starlette runs a sync route. The client disconnects once
    ``disconnect_when()`` holds (never, without it); the request's task is
    cancelled once ``cancel_when()`` holds."""
    async def inner(scope, receive, send):
        await asyncio.to_thread(handler)      # the deadline reaches the thread

    async def main() -> None:
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            if disconnect_when is None:
                await asyncio.Event().wait()
            await asyncio.to_thread(_until, disconnect_when)
            return {"type": "http.disconnect"}

        task = asyncio.ensure_future(Deadlines(inner, seconds=30)({"type": "http", "method": method},
                                                                   receive, None))
        if cancel_when is not None:
            await asyncio.to_thread(_until, cancel_when)
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


def _an_abandoned_read_leaves_no_process(tmp_path, monkeypatch):
    """The app disconnects mid-request: the read's whole group is stopped,
    SIGKILL for what ignores SIGTERM, and the handler gets Busy."""
    monkeypatch.setattr(spawn, "GRACE", 0.2)
    stopped_before = spawn.stats()["stopped"]
    pids = tmp_path / "pids"
    script = f'trap "" TERM; tail -f /dev/null & echo $$ $! > {pids}; wait'
    got: list[BaseException] = []

    def handler() -> None:
        try:
            spawn.read(["sh", "-c", script])
        except spawn.Busy as e:
            got.append(e)

    t0 = time.monotonic()
    _serve("GET", handler, disconnect_when=lambda: pids.exists() and len(pids.read_text().split()) == 2)
    assert got
    assert time.monotonic() - t0 < 5       # the disconnect, not the 30 second deadline
    assert _until(lambda: not any(procs.alive(int(p)) for p in pids.read_text().split()))
    assert spawn.stats()["stopped"] == stopped_before + 1
    assert spawn.running() == 0


def _a_cancelled_request_stops_the_read_its_thread_left_running():
    got: list[BaseException] = []

    def handler() -> None:
        try:
            spawn.read(["tail", "-f", "/dev/null"])
        except spawn.Busy as e:
            got.append(e)

    t0 = time.monotonic()
    _serve("GET", handler, cancel_when=lambda: spawn.running() == 1)
    assert got
    assert time.monotonic() - t0 < 5       # the cancellation, not the 30 second deadline


def _a_post_whose_client_left_mid_action_runs_to_its_end(tmp_path):
    """Once an action has started the request is a transaction: the action
    and the reads after it (bookkeeping, a rollback) finish, client or not."""
    started, go = tmp_path / "started", tmp_path / "go"
    got: list = []

    def handler() -> None:
        dl = spawn.current()
        # The action may only end after the disconnect has stopped the request.
        threading.Thread(target=lambda: (_until(lambda: dl is None or dl.stopped), go.touch())).start()
        got.append(spawn.action(["sh", "-c", f"touch {started}; while [ ! -e {go} ]; do :; done; echo done"]).stdout)
        got.append(dl is not None and dl.stopped)
        got.append(spawn.read(["echo", "after"]).stdout)

    _serve("POST", handler, disconnect_when=started.exists)
    assert got == ["done\n", True, "after\n"]


def _an_edit_whose_client_leaves_as_it_commits_keeps_the_commit(repo, monkeypatch):
    """The reads after the commit (its sha) once failed on the stopped
    deadline, and the rollback put the old file back under the new commit."""
    ws = Workspace("w", str(repo), "w", "worktree", "main", "main", str(repo), None, "s", 1.0)
    real = git.run

    def run(args, *a, **kw):
        proc = real(args, *a, **kw)
        if "commit" in args:
            spawn.current().stop()         # the client leaves the moment the commit is made
        return proc

    monkeypatch.setattr(git, "run", run)
    base = sh("git rev-parse HEAD:app.py", repo)
    with spawn.deadline(30):
        _, commit = routes_actions.commit_edit(ws, "app.py", b"new\n", base, "edit")
    assert commit == sh("git rev-parse HEAD", repo)
    assert sh("git show HEAD:app.py", repo) == "new"
    assert (repo / "app.py").read_bytes() == b"new\n"


class _FakePs:
    """The ``ps`` child procs.table starts through spawn.read: the agent's
    pane (5001) and the tree that outlived its window."""
    started: list[list[str]] = []
    pid, returncode = 0, 0

    def __init__(self, argv, **kw):
        self.started.append(argv)

    def communicate(self, input=None, timeout=None):
        return "5001 1 cat\n5002 5001 node dev-server\n5003 5002 python -m pytest\n", ""


def _a_stop_whose_client_leaves_once_the_window_is_closed_still_finishes(db, repo, tmp_path, monkeypatch):
    """Stopping the agent's processes, ending its native subagents and
    recording it paused once failed on the stopped deadline, after its window
    was already gone."""
    from brindle import sidebar_follow

    db.add_workspace(Workspace("w", str(repo), "w", "worktree", "main", "main", str(repo), None, "s", 1.0))
    db.add_agent(Agent("a1", "w", profile="developer", provider="claude", parent_id=None, mode="assign",
                       status="running", tmux_window="%7", result=None, created_at=1.0))
    db.start_native_subagent("sub1", "a1", "explore")
    closed, terminated = [], set()
    monkeypatch.setattr(agents, "same_server", lambda agent, panes=None: True)
    monkeypatch.setattr(agents, "owns_pane", lambda db, agent, owners=None: True)
    monkeypatch.setattr(sidebar_follow, "rescue_sidebar_from_window", lambda db, agent: None)
    monkeypatch.setattr(tmux, "window_pids", lambda target, server=None: [5001])

    def kill_window(target):
        closed.append(target)
        spawn.current().stop()             # the client leaves right after the first change

    monkeypatch.setattr(tmux, "kill_window", kill_window)
    monkeypatch.setattr(procs, "Path", lambda p: tmp_path / "no-proc")    # ps, not /proc, on Linux too
    monkeypatch.setattr(subprocess, "Popen", _FakePs)
    _FakePs.started = []
    monkeypatch.setattr(procs, "terminate", lambda pids, grace=3.0: terminated.update(pids) or len(pids))

    with spawn.deadline(30):
        agents.pause_worker(db, db.get_agent("a1"))
    assert closed == ["%7"]
    assert [argv[0] for argv in _FakePs.started] == ["ps"]   # admitted through spawn.read
    assert terminated == {5001, 5002, 5003}
    assert all(s.ended_at is not None for s in db.native_subagents("a1"))
    assert db.get_agent("a1").status == "paused"


def _warm_fans_out_under_the_request_deadline(monkeypatch):
    """status_cache.warm runs its reads on a thread pool, whose threads don't
    copy the request's context."""
    stopped: list[str] = []

    def status(path, base, now=None):
        limit = 5.0
        try:
            spawn.read(["tail", "-f", "/dev/null"], timeout=limit)
        except spawn.Busy:
            stopped.append(path)

    monkeypatch.setattr(status_cache, "cached_status", status)
    with spawn.deadline(30) as dl:
        threading.Thread(target=lambda: (_until(lambda: spawn.running() == 2), dl.stop())).start()
        status_cache.warm([("a", "main"), ("b", "main")])
    assert sorted(stopped) == ["a", "b"]


def _a_hash_whose_clean_filter_stalls_stops_at_the_deadline(repo):
    sh("git config filter.stall.clean 'tail -f /dev/null' && echo '*.txt filter=stall' > .gitattributes", repo)
    t0 = time.monotonic()
    with spawn.deadline(0.5), pytest.raises(git.GitBusy):
        git.hash_blob(repo, b"x\n", "a.txt")
    assert time.monotonic() - t0 < 5
    assert spawn.running() == 0


def _a_git_status_stopped_partway_leaves_no_index_lock(repo, tmp_path):
    """git status holds .git/index.lock while it asks a dirty submodule for
    its own status; a submodule whose fsmonitor hook never answers keeps it
    there until the read is stopped."""
    sub = repo / "sub"
    sub.mkdir()
    sh("git init -q && echo a > a && git add a && git commit -qm s", sub)
    sh("git add sub && git commit -qm sub", repo)
    hook = tmp_path / "fsmonitor"
    hook.write_text("#!/bin/sh\nexec tail -f /dev/null\n")
    hook.chmod(0o755)
    sh(f"git config core.fsmonitor {hook} && echo changed > a", sub)

    limit = 1.0
    with pytest.raises(git.GitError):
        git.run(["status"], repo, timeout=limit)
    assert not (repo / ".git" / "index.lock").exists()


def _row(scenario, id, why=None, **params):
    return pytest.param(scenario, params, id=id, marks=[pytest.mark.variant(why)] if why else [])


@pytest.mark.parametrize("scenario, params", [
    _row(_room, "room-ample", load=2.0, level=1, expected=5.0),            # 8 cores - load 2 - 1 running
    _row(_room, "room-pressure-warning", load=2.0, level=2, expected=2.5),   # memory-pressure warning: half
    _row(_room, "room-pressure-critical", load=2.0, level=4, expected=-1.0),  # nothing but what's running
    _row(_with_no_room_a_read_is_busy_at_its_deadline_and_a_background_one_skips, "overload-no-room"),
    _row(_past_its_deadline_nothing_starts_even_with_room, "deadline-passed"),
    _row(_a_started_helper_commits_its_request_and_a_started_read_does_not, "subprocess-helper-vs-read"),
    _row(_an_abandoned_read_leaves_no_process, "abandoned-read"),
    _row(_a_cancelled_request_stops_the_read_its_thread_left_running, "cancelled-read"),
    _row(_a_post_whose_client_left_mid_action_runs_to_its_end, "client-left-post",
         "an action whose client left still finishes"),
    _row(_an_edit_whose_client_leaves_as_it_commits_keeps_the_commit, "client-left-edit",
         "an edit whose client left as it commits keeps its commit"),
    _row(_a_stop_whose_client_leaves_once_the_window_is_closed_still_finishes, "client-left-stop",
         "a stop whose client left still finishes"),
    _row(_warm_fans_out_under_the_request_deadline, "deadline-warm"),
    _row(_a_hash_whose_clean_filter_stalls_stops_at_the_deadline, "deadline-hash"),
    _row(_a_git_status_stopped_partway_leaves_no_index_lock, "stopped-git-status",
         "a stopped git status leaves no index.lock"),
])
def test_spawn_overload_and_request_deadline(request, scenario, params):
    run_scenario(request, scenario, params)
