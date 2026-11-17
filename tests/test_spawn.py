"""brindle.spawn: the load-aware gate, request deadlines, and reads stopped cleanly."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import run_scenario, sh

from brindle import agents, git, procs, providers, spawn, status_cache, tasks, tmux
from brindle.daemon import routes_actions
from brindle.daemon.app import Deadlines
from brindle.db import Agent, Task, Workspace

REAL_PROBE = providers._auth_probe      # conftest stubs it for every test
REAL_LOAD = spawn._load                 # and this one


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
    from brindle import disk

    monkeypatch.setattr(disk, "low_disk", lambda root=None: None)
    monkeypatch.setattr(spawn, "_cores", lambda: 8)
    monkeypatch.setattr(spawn, "_load", lambda: load)
    monkeypatch.setattr(spawn, "pressure", lambda: level)
    monkeypatch.setattr(spawn, "_running", 1)
    assert spawn.room() == expected
    assert spawn.check_no_room() == (spawn.why() if level >= spawn.PRESSURE_CRITICAL else None)
    if expected >= 1:
        assert spawn.no_room() is None


def _a_worker_keeps_waiting_past_its_room_wait_limit(db, tmp_path, monkeypatch, overloaded, maximum, disk):
    """Past room_wait_max (low disk or load) its supervisor is told once and nothing else
    changes: the task starts on its own only once room returns."""
    from brindle import disk as disk_mod

    low = ["waiting for disk: 1.0 GB free (under 10 GB)" if disk else None]
    monkeypatch.setattr(disk_mod, "low_disk", lambda repo_root=None: low[0])
    cfg = tmp_path / ".brindle"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"room_wait_max": maximum} if maximum else {}))
    limit = maximum or 300
    clock = [100.0]
    monkeypatch.setattr(tasks, "time", SimpleNamespace(time=lambda: clock[0]))
    db.add_workspace(Workspace("w", str(tmp_path), "w", "worktree", "main", "main", str(tmp_path), None, "s", 1.0))
    db.add_agent(Agent("boss", "w", "supervisor", "claude", None, "interactive", "processing", "@0", None, 1.0))
    task = Task("t", str(tmp_path), None, "boss", "w", "developer", "do work", "assign", 0,
                None, None, None, None, "pending", 1.0, waiting=spawn.why(), waiting_since=clock[0])
    db.add_task(task)
    db.set_task_waiting(task.id, task.waiting, clock[0])
    worker = Agent("worker", "w", "developer", "claude", "boss", "assign", "processing", "@1", None, 1.0)
    launched, sent = [], []
    monkeypatch.setattr(agents, "delegate", lambda *a, **kw: (launched.append(worker) or worker, db.get_workspace("w")))
    monkeypatch.setattr(agents, "send_message", lambda db_, recipient, text, **kw: sent.append(recipient))
    monkeypatch.setattr(tasks, "_wait_in_background", lambda _: None)

    polls = []

    def poll(_):
        polls.append(clock[0])
        if len(polls) == 1:
            clock[0] += limit + 1
        elif len(polls) == 2:   # past the limit: a concurrent sweep too, and still nothing started
            assert tasks.start_with_room(db) == 0
            assert db.get_task(task.id).state == "pending" and not launched
            assert sent == ["boss"]   # the one long-wait notice, to the task's caller alone
            low[0] = None
            monkeypatch.setattr(spawn, "_load", lambda: 0.5)   # room returns
        else:
            raise AssertionError("still waiting after room returned")

    monkeypatch.setattr(tasks, "threading", SimpleNamespace(Event=lambda: SimpleNamespace(wait=poll)))
    assert tasks.start_with_room(db) == 0
    assert tasks.start_when_room(db, task.id)
    assert len(polls) == 2 and db.get_task(task.id).state == "started" and launched == [worker]
    assert sent == ["boss", "boss"]   # the notice, then the start report
    assert tasks.start_with_room(db) == 0


def _a_high_load_average_with_idle_cpus_leaves_room(monkeypatch):
    """macOS counts throttled runnable threads in the load average: 96 with
    the CPUs 63% idle once held every check run back."""
    ticks = iter([(0, 0), (37, 100)])        # (busy, total) ticks: 63% idle between the samples
    monkeypatch.setattr(spawn, "_load", REAL_LOAD)
    monkeypatch.setattr(spawn, "_cpu_ticks", lambda: next(ticks))
    monkeypatch.setattr(spawn, "_sample", None)
    monkeypatch.setattr(spawn, "WINDOW", 0)
    monkeypatch.setattr(spawn.os, "getloadavg", lambda: (96.0, 96.0, 96.0))
    monkeypatch.setattr(spawn, "_cores", lambda: 10)
    monkeypatch.setattr(spawn, "_running", 1)
    assert spawn.room() == pytest.approx(10 - 3.7 - 1)
    assert spawn.no_room() is None


def _a_start_whose_room_sample_outlasts_its_deadline_is_refused(monkeypatch):
    """Sampling the CPU can take a moment; a start admitted after it once ran
    past its request's deadline (or its stop) and committed the request."""
    clock = [100.0]
    monkeypatch.setattr(spawn, "time", type("Clock", (), {"monotonic": staticmethod(lambda: clock[0])}))

    def sample() -> float:              # idle CPUs, but the sample takes 0.2 s
        clock[0] += 0.2
        return 0.0

    monkeypatch.setattr(spawn, "_load", sample)
    monkeypatch.setattr(spawn, "_gated", True)
    monkeypatch.setattr(spawn, "_running", 1)
    started: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(argv))
    with spawn.waits_for_room(), spawn.deadline(0.1) as dl, pytest.raises(spawn.Busy):
        spawn.action(["true"])
    assert not started and not dl.committed and spawn.running() == 1


def _checks_and_setup_run_at_normal_priority(monkeypatch, tmp_path):
    """Under the daemon's gate a check run (a forced worker's too) and a
    setup command once ran under ``taskpolicy -b``: on macOS a vitest check
    sat runnable at priority 4 with 0% CPU on a mostly idle machine."""
    from brindle import autopilot, workspaces

    monkeypatch.setattr(spawn, "_gated", True)
    real = subprocess.Popen
    started: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(argv) or real(argv, **kw))
    assert autopilot.run_check("true", str(tmp_path), {}, 30)[0]
    assert workspaces.run_commands(["true"], str(tmp_path), {}).ok
    assert started == ["true", ["/bin/sh", "-c", "true"]]


def _with_no_room_only_waiting_work_waits_and_a_request_read_starts_at_once(overloaded, repo):
    # A request's own read starts at once however busy the machine is: on a
    # machine above its core count, 0.5.8 made these wait and starved every request.
    t0 = time.monotonic()
    with spawn.deadline(0.3):
        git.run(["status"], repo)
    assert time.monotonic() - t0 < 0.3
    # ...at normal priority: at background priority macOS starved the caller
    # check's `ps` on a busy machine and every request waited on it (0.5.9).
    assert spawn._background(["ps"]) == ["ps"]
    with spawn.waits_for_room():
        assert spawn._background(["ps"])[-1] == "ps" and len(spawn._background(["ps"])) > 1

    # Work marked as waiting (the heavy lane, background sweeps) waits, and is
    # busy at its deadline.
    t0 = time.monotonic()
    with spawn.waits_for_room(), spawn.deadline(0.3), pytest.raises(git.GitBusy):  # a GitError and a Busy
        git.run(["status"], repo)
    assert time.monotonic() - t0 >= 0.3

    t0 = time.monotonic()
    with pytest.raises(spawn.Skipped):
        spawn.read(["true"], skip=True)
    assert time.monotonic() - t0 < 0.2

    # Waiting work never starts an action past its deadline: nothing was done.
    with spawn.waits_for_room(), spawn.deadline(0.1), pytest.raises(spawn.Busy):
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


def _an_unreaped_process_is_not_alive(tmp_path, monkeypatch):
    """Linux keeps an exited, unreaped process as a zombie that still answers
    kill(0) (an orphan waiting on a subreaper such as systemd --user)."""
    stat = tmp_path / str(os.getpid()) / "stat"
    stat.parent.mkdir()
    monkeypatch.setattr(procs, "PROC", tmp_path)
    stat.write_text(f"{os.getpid()} (sh (x)) Z 1 1 1 0 -1")
    assert not procs.alive(os.getpid())
    stat.write_text(f"{os.getpid()} (sh (x)) R 1 1 1 0 -1")
    assert procs.alive(os.getpid())


def _a_hung_up_sidebar_leaves_no_read_running(tmp_path):
    """tmux hangs up the sidebar while its costs thread waits on `claude -p
    /usage` (its own session, so the hangup never reaches it): the read is
    stopped as the sidebar exits, not left running for good."""
    started = tmp_path / "started"
    os.mkfifo(started)
    script = (f"import signal, threading\nfrom brindle import spawn\nspawn.exit_on_hangup()\n"
              f"threading.Thread(target=spawn.read, args=(['sh', '-c', 'echo $$ > {started}; "
              f"exec tail -f /dev/null'],), daemon=True).start()\n"
              f"print(open('{started}').read(), end='', flush=True)\nsignal.pause()\n")
    sidebar = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    pid = int(sidebar.stdout.readline())     # (a sidebar that never started reads as "")
    try:
        os.kill(sidebar.pid, signal.SIGHUP)
        sidebar.wait()
        assert not procs.alive(pid)
    finally:
        if procs.alive(pid):
            os.kill(pid, signal.SIGKILL)


def _a_read_is_tracked_as_it_starts(monkeypatch):
    """An exit between a read's start and its registration would miss it and
    leave it running for good: the exit's lock has to cover both."""
    real, held = subprocess.Popen, []

    def popen(*args, **kwargs):
        held.append(spawn._reads_lock.locked())
        return real(*args, **kwargs)

    monkeypatch.setattr(spawn.subprocess, "Popen", popen)
    spawn.read(["true"])
    assert held == [True]


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
    pane (5001) and the tree that outlived its window, listed only after a
    first poll interval (spawn.read keeps waiting)."""
    started: list[list[str]] = []
    pid, returncode = 0, 0

    def __init__(self, argv, **kw):
        self.started.append(argv)
        self.polled = False

    def communicate(self, input=None, timeout=None):
        if not self.polled:
            self.polled = True
            raise subprocess.TimeoutExpired("ps", timeout)
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
    monkeypatch.setattr(procs, "PROC", tmp_path / "no-proc")    # ps, not /proc, on Linux too
    monkeypatch.setattr(subprocess, "Popen", _FakePs)
    _FakePs.started = []
    monkeypatch.setattr(procs, "terminate", lambda pids, grace=3.0: terminated.update(pids) or len(pids))

    with spawn.deadline(30):
        agents.pause_worker(db, db.get_agent("a1"))
    assert closed == ["%7"]
    assert [argv[0] for argv in _FakePs.started] == ["ps", "ps"]   # admitted through spawn.read
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
    _row(_room, "room-pressure-critical", load=2.0, level=4, expected=-1.0),  # nothing but what's running
    _row(_room, "room-pressure-warn", "Warning memory pressure must leave room rather than reject every launch",
         load=5.5, level=2, expected=1.5),
    _row(_a_worker_keeps_waiting_past_its_room_wait_limit, "room-wait-limit",
         "A worker without a configured wait limit must remain queued until capacity returns", maximum=None, disk=False),
    _row(_a_worker_keeps_waiting_past_its_room_wait_limit, "room-wait-configured-limit", maximum=60, disk=False),
    _row(_a_worker_keeps_waiting_past_its_room_wait_limit, "room-wait-not-for-disk",
         "Disk pressure must keep a worker queued independently of CPU admission", maximum=None, disk=True),
    _row(_a_high_load_average_with_idle_cpus_leaves_room, "room-idle-cpu-high-loadavg"),
    _row(_a_start_whose_room_sample_outlasts_its_deadline_is_refused, "room-sample-past-deadline",
         "A deadline expiring during CPU sampling must prevent the action from starting"),
    _row(_checks_and_setup_run_at_normal_priority, "normal-priority-checks",
         "macOS incident: background priority starved foreground checks and setup commands"),
    _row(_with_no_room_only_waiting_work_waits_and_a_request_read_starts_at_once, "overload-no-room"),
    _row(_past_its_deadline_nothing_starts_even_with_room, "deadline-passed"),
    _row(_a_started_helper_commits_its_request_and_a_started_read_does_not, "subprocess-helper-vs-read",
         "only a helper whose request committed may outlive it; a read must not"),
    _row(_an_abandoned_read_leaves_no_process, "abandoned-read"),
    # Quarantined: real processes and signals, so it failed on loaded CI runners. read-tracked-at-start
    # pins the race it kept hitting, without depending on timing.
    pytest.param(_a_hung_up_sidebar_leaves_no_read_running, {}, id="hung-up-sidebar-read",
                 marks=[pytest.mark.skip("quarantined: flaky on loaded CI runners"),
                        pytest.mark.variant("A sidebar hangup must terminate the read process it started")]),
    _row(_a_read_is_tracked_as_it_starts, "read-tracked-at-start",
         "An exit between process start and registration must not leave an untracked read running"),
    _row(_an_unreaped_process_is_not_alive, "zombie-not-alive"),
    _row(_a_cancelled_request_stops_the_read_its_thread_left_running, "cancelled-read",
         "a cancelled request stops the read its worker thread left running"),
    _row(_a_post_whose_client_left_mid_action_runs_to_its_end, "client-left-post",
         "an action whose client left still finishes"),
    _row(_an_edit_whose_client_leaves_as_it_commits_keeps_the_commit, "client-left-edit",
         "an edit whose client left as it commits keeps its commit"),
    _row(_a_stop_whose_client_leaves_once_the_window_is_closed_still_finishes, "client-left-stop",
         "a stop whose client left still finishes"),
    _row(_warm_fans_out_under_the_request_deadline, "deadline-warm",
         "a warm-up fanned out across sessions must still end at the request deadline"),
    _row(_a_hash_whose_clean_filter_stalls_stops_at_the_deadline, "deadline-hash"),
    _row(_a_git_status_stopped_partway_leaves_no_index_lock, "stopped-git-status",
         "a stopped git status leaves no index.lock"),
])
def test_spawn_overload_and_request_deadline(request, scenario, params):
    run_scenario(request, scenario, params)
