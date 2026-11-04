"""brindle leaves nothing behind: test tmux servers, merged worktrees, stale
locks, orphan sessions and servers, empty worktree folders."""

import os
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import sh
import test_cull
from brindle import cull, tmux, view, workspaces

# These start servers named after a dead pytest run, which every other
# xdist worker's teardown would reap first: run them in the serial pass.
pytestmark = pytest.mark.serial
from brindle.cli import app
from brindle.config import brindle_home, worktree_root, worktrees_dir
from brindle.db import Agent

OLD = time.time() - 3600


def add_agent(db, ws, agent_id, *, status="done", result="did it", window="%999",
              mode="handoff", created_at=OLD):
    db.add_agent(Agent(agent_id, ws.id, "developer", "shell", None, mode, status, window,
                       result, created_at))
    return db.get_agent(agent_id)


def merged_worker(db, repo, name="feat-done"):
    """A worker's workspace whose one commit is merged into main."""
    ws = workspaces.create(db, str(repo), name).workspace
    (Path(ws.path) / f"{name}.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    workspaces.merge_back(db, ws)
    return ws


def unmerged_worker(db, repo, name="feat-open"):
    ws = workspaces.create(db, str(repo), name).workspace
    (Path(ws.path) / f"{name}.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    return ws


def start_server(name: str, home: str) -> None:
    subprocess.run(["tmux", "-L", name, "new-session", "-d", "-s", "brindle_x_y",
                    "-e", f"BRINDLE_HOME={home}"], check=True)


def server_up(name: str) -> bool:
    return subprocess.run(["tmux", "-L", name, "has-session"], capture_output=True).returncode == 0


# -- test servers and detached helpers ------------------------------------------------


def test_a_live_test_runs_server_is_left_alone_even_without_a_brindle_home(tmp_path):
    """Concurrent pytest runs share the socket directory: a sweep in one must
    not stop the other's server because its sessions have no home to show."""
    name = f"brindle-test-{os.getpid()}-other"
    start_server(name, str(tmp_path / "deleted-home"))
    try:
        assert not any(name in line for line in cull.orphan_servers())
        assert server_up(name)
    finally:
        tmux.reap_server(name)


def test_a_server_whose_brindle_home_is_gone_is_reaped(tmp_path):
    name = f"brindle-e2e-remnant-{os.getpid()}"
    start_server(name, str(tmp_path / "deleted-home"))
    try:
        assert any(name in line for line in cull.orphan_servers())
        assert not server_up(name)
    finally:
        tmux.reap_server(name)


def test_detached_helpers_stop_when_their_brindle_home_is_gone(tmp_path, monkeypatch):
    gone = tmp_path / "gone-home"
    monkeypatch.setenv("BRINDLE_HOME", str(gone))
    runner = CliRunner()
    for args in (["_after-launch", "abc"], ["_cull"], ["_review-after-checks", "p/w"],
                 ["_pool-fill", str(tmp_path)], ["_flush", "abc"], ["_close", "abc"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (args, result.output)
    assert not gone.exists()  # nothing recreated it (and nothing opened a session for it)


    # conftest's pytest_runtest_teardown kills it after this test; the next test
    # checks nothing carried over.


# -- merged, finished worktrees ------------------------------------------------------


def test_merged_worktree_with_uncommitted_changes_is_hidden_but_kept(db, repo):
    ws = merged_worker(db, repo)
    add_agent(db, ws, "a1")
    (Path(ws.path) / "wip.py").write_text("y = 2\n")
    assert ws.id not in {e["id"] for e in view.snapshot(db, str(repo), panes={})}
    lines = cull.prune_retired(db)
    assert any("uncommitted" in line for line in lines)
    assert os.path.isdir(ws.path) and db.get_workspace(ws.id)


def test_prune_removes_merged_worktrees_and_their_branches(db, repo):
    done = merged_worker(db, repo)
    add_agent(db, done, "a1")
    open_ = unmerged_worker(db, repo)
    add_agent(db, open_, "a2")
    lines = cull.prune_retired(db)
    assert any(done.path in line and "branch deleted" in line for line in lines)
    assert not os.path.exists(done.path)
    assert db.get_workspace(done.id) is None
    assert not sh(f"git branch --list {done.branch}", repo)  # merged: branch gone too
    assert os.path.isdir(open_.path) and db.get_workspace(open_.id)  # unmerged untouched


# -- stale sidebar locks -------------------------------------------------------------


def test_stale_sidebar_locks_are_removed(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    add_agent(db, ws, "live", status="processing", result=None, mode="interactive")
    add_agent(db, ws, "over", status="done", mode="interactive")
    locks = brindle_home() / "locks"
    locks.mkdir(parents=True)
    for aid in ("live", "over", "gone", "fresh"):
        (locks / f"sidebar-{aid}.lock").write_text("")
        if aid != "fresh":
            os.utime(locks / f"sidebar-{aid}.lock", (OLD, OLD))
    assert cull.clean_locks(db) == 2
    assert sorted(p.name for p in locks.iterdir()) == ["sidebar-fresh.lock", "sidebar-live.lock"]


# -- orphan tmux sessions, empty folders -----------------------------------------------


def test_orphan_brindle_sessions_are_killed(db, repo):
    ws = workspaces.create(db, str(repo), "feat-live").workspace
    tmux.ensure_session(ws.tmux_session, ws.path, {})
    pane = tmux.new_window(ws.tmux_session, "agent", ws.path, ["sleep", "60"], {})
    add_agent(db, ws, "live", status="processing", result=None, window=pane)
    tmux.ensure_session("brindle_proj_feat-orphan", str(repo), {})
    tmux.ensure_session("not-brindle", str(repo), {})
    tmux.ensure_session("brindle_proj_devserver", str(repo), {})
    tmux.new_window("brindle_proj_devserver", "server", str(repo), ["sleep", "60"], {})
    time.sleep(0.3)  # let the shells start

    lines = cull.orphan_sessions(db)
    assert any("brindle_proj_feat-orphan" in line for line in lines)
    assert not tmux.has_session("brindle_proj_feat-orphan")
    assert tmux.has_session(ws.tmux_session)
    assert tmux.has_session("not-brindle")
    assert tmux.has_session("brindle_proj_devserver")  # something the person runs


def test_a_session_whose_worktree_is_gone_is_killed_with_the_cli_in_it(db, tmp_path):
    """#167: prune kept these for the agent CLI (agy) still running in them."""
    gone = tmp_path / "gone"
    gone.mkdir()
    name = "brindle_proj_fix-gone"
    tmux.ensure_session(name, str(gone), {})
    pane = tmux.new_window(name, "agy", str(gone), ["sleep", "61"], {})
    pids = tmux.window_pids(pane)
    gone.rmdir()

    lines = cull.orphan_sessions(db)

    assert any(name in line for line in lines) and not tmux.has_session(name)
    assert not any(test_cull.alive(p) for p in pids)   # terminate() returns once they are gone


@pytest.mark.variant("#199 incident input: parse the full owner PID in per-test server names; startup parsing lives in unmeasured conftest.py")
@pytest.mark.parametrize("name, alive, reaped", [
    ("brindle-test-1234-7", False, True),      # a dead owner's per-test server
    ("brindle-test-12345-7", True, False),     # a live owner whose pid starts with the same digits
])
def test_startup_reaper_parses_the_owner_pid(monkeypatch, name, alive, reaped):
    import conftest
    from brindle import procs

    seen = []
    monkeypatch.setattr(tmux, "other_servers", lambda prefix="brindle-": [name])
    monkeypatch.setattr(procs, "alive", lambda pid: alive and pid == 12345)
    monkeypatch.setattr(tmux, "reap_server", seen.append)
    conftest._reap_dead_test_servers()
    assert seen == ([name] if reaped else [])


@pytest.mark.variant("suite overhead: a test's tmux server starts at its first tmux write, keeper "
                     "first (#199); the lazy start lives in unmeasured conftest.py")
def test_a_tests_tmux_server_starts_at_its_first_write_with_the_keeper(tmp_path):
    socket = tmux.socket_dir() / os.environ["BRINDLE_TMUX_SOCKET"]
    assert not tmux.has_session("brindle-test-keeper") and not socket.exists()   # a read starts nothing
    tmux.ensure_session("first", str(tmp_path), {})
    assert tmux.has_session("brindle-test-keeper") and socket.exists()
