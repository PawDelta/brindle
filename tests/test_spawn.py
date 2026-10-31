"""brindle.spawn: the load-aware gate, request deadlines, and reads stopped cleanly."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from conftest import sh

from brindle import git, procs, spawn, status_cache
from brindle.daemon.app import Deadlines


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


@pytest.mark.parametrize("load, level, expected", [
    (2.0, 1, 5.0),       # 8 cores - load 2 - 1 running
    (2.0, 2, 2.5),       # memory-pressure warning: half
    (2.0, 4, -1.0),      # critical: nothing but what's running
])
def test_room(monkeypatch, load, level, expected):
    monkeypatch.setattr(spawn, "_cores", lambda: 8)
    monkeypatch.setattr(spawn, "_load", lambda: load)
    monkeypatch.setattr(spawn, "pressure", lambda: level)
    monkeypatch.setattr(spawn, "_running", 1)
    assert spawn.room() == expected


def test_with_no_room_a_read_is_busy_at_its_deadline_and_a_background_one_skips(overloaded, repo):
    t0 = time.monotonic()
    with spawn.deadline(0.3), pytest.raises(spawn.Busy, match="load 9.0 on 4 cores"):
        git.run(["status"], repo)          # a GitBusy: both a GitError and a Busy
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


def test_past_its_deadline_nothing_starts_even_with_room(tmp_path):
    marker = tmp_path / "ran"
    with spawn.deadline(-1):
        for start in (spawn.read, spawn.action):
            with pytest.raises(spawn.Busy):
                start(["touch", str(marker)])
    assert not marker.exists()


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


def test_an_abandoned_read_leaves_no_process(tmp_path, monkeypatch):
    """The app disconnects mid-request: the read's whole group is stopped,
    SIGKILL for what ignores SIGTERM, and the handler gets Busy."""
    monkeypatch.setattr(spawn, "GRACE", 0.2)
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
    assert got and "client went away" in str(got[0])
    assert time.monotonic() - t0 < 5
    assert _until(lambda: not any(procs.alive(int(p)) for p in pids.read_text().split()))
    assert spawn.running() == 0


def test_a_cancelled_request_stops_the_read_its_thread_left_running():
    got: list[BaseException] = []

    def handler() -> None:
        try:
            spawn.read(["tail", "-f", "/dev/null"])
        except spawn.Busy as e:
            got.append(e)

    t0 = time.monotonic()
    _serve("GET", handler, cancel_when=lambda: spawn.running() == 1)
    assert got and "request ended" in str(got[0])
    assert time.monotonic() - t0 < 5


def test_a_post_whose_client_left_finishes_its_action_but_not_its_reads(tmp_path):
    started, go = tmp_path / "started", tmp_path / "go"
    got: list = []

    def handler() -> None:
        dl = spawn.current()
        # The action may only end after the disconnect has stopped the request.
        threading.Thread(target=lambda: (_until(lambda: dl is None or dl.stopped), go.touch())).start()
        got.append(spawn.action(["sh", "-c", f"touch {started}; while [ ! -e {go} ]; do :; done; echo done"]).stdout)
        try:
            spawn.read(["true"])
        except spawn.Busy as e:
            got.append(e)

    _serve("POST", handler, disconnect_when=started.exists)
    assert got[0] == "done\n"
    assert len(got) == 2 and "client went away" in str(got[1])


def test_warm_fans_out_under_the_request_deadline(monkeypatch):
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


def test_a_hash_whose_clean_filter_stalls_stops_at_the_deadline(repo):
    sh("git config filter.stall.clean 'tail -f /dev/null' && echo '*.txt filter=stall' > .gitattributes", repo)
    t0 = time.monotonic()
    with spawn.deadline(0.5), pytest.raises(git.GitBusy):
        git.hash_blob(repo, b"x\n", "a.txt")
    assert time.monotonic() - t0 < 5
    assert spawn.running() == 0


def test_a_git_status_stopped_partway_leaves_no_index_lock(repo, tmp_path):
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
