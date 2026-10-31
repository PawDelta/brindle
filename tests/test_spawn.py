"""brindle.spawn: the load-aware gate, request deadlines, and reads stopped cleanly."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from conftest import sh

from brindle import git, procs, spawn
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

    async def inner(scope, receive, send):
        await asyncio.to_thread(handler)      # the deadline reaches the thread

    async def main() -> None:
        sent = asyncio.Event()

        async def receive():
            if not sent.is_set():
                sent.set()
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.to_thread(_until, lambda: pids.exists() and len(pids.read_text().split()) == 2)
            return {"type": "http.disconnect"}

        app = Deadlines(inner, seconds=30)
        await asyncio.wait_for(app({"type": "http", "method": "GET"}, receive, None), 10)

    t0 = time.monotonic()
    asyncio.run(main())
    assert got and "client went away" in str(got[0])
    assert time.monotonic() - t0 < 5
    assert _until(lambda: not any(procs.alive(int(p)) for p in pids.read_text().split()))
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
