"""brindle leaves nothing behind: test tmux servers, merged worktrees, stale
locks, orphan sessions and servers, empty worktree folders."""

import os
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import sh
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


def test_a_dead_servers_socket_file_is_removed():
    name = f"brindle-e2e-stale-{os.getpid()}"
    sock = tmux.socket_dir() / name
    tmux.socket_dir().mkdir(parents=True, exist_ok=True)
    sock.touch()
    cull.orphan_servers()
    assert not sock.exists()


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


def test_prune_keeps_merged_branches_when_the_repo_says_so(db, repo):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text('{"delete_merged_branches": false}')
    done = merged_worker(db, repo)
    add_agent(db, done, "a1")
    lines = cull.prune_retired(db)
    assert any(f"branch {done.branch} kept" in line for line in lines)
    assert sh(f"git branch --list {done.branch}", repo)


def test_prune_command_cleans_up(db, repo, monkeypatch):
    done = merged_worker(db, repo)
    add_agent(db, done, "a1")
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["prune"])
    assert result.exit_code == 0, result.output
    assert "removed merged worktree" in result.output
    assert not os.path.exists(done.path)
    # its now-empty parent folders went too
    assert not (worktrees_dir() / "proj" / "feat-done").exists()


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


def test_the_periodic_sweep_cleans_locks(db):
    locks = brindle_home() / "locks"
    locks.mkdir(parents=True)
    (locks / "sidebar-gone.lock").write_text("")
    os.utime(locks / "sidebar-gone.lock", (OLD, OLD))
    assert any("lock" in line for line in cull.sweep(db))
    assert not (locks / "sidebar-gone.lock").exists()


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


def test_empty_worktree_folders_are_removed(db, repo):
    ws = workspaces.create(db, str(repo), "feat/real").workspace
    (worktree_root(repo) / "fix" / "gone").mkdir(parents=True)
    (worktrees_dir() / "proj" / "old").mkdir(parents=True)  # from before #182
    assert cull.empty_worktree_dirs(db) == 4
    assert not (worktree_root(repo) / "fix").exists()
    assert not (worktrees_dir() / "proj").exists()
    assert os.path.isdir(ws.path)
    assert worktrees_dir().is_dir()


def _fake_gh(tmp_path, monkeypatch, answer):
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    log = tmp_path / "gh.log"
    gh.write_text(f'#!/bin/sh\necho "$@" >> {log}\necho {answer}\n')
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{gh.parent}{os.pathsep}{os.environ['PATH']}")
    return log


def test_first_pr_offers_to_delete_merged_branches_on_github(repo, tmp_path, monkeypatch):
    from brindle import cli

    log = _fake_gh(tmp_path, monkeypatch, "false")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)
    cli._offer_delete_on_merge(str(repo))
    assert "repo edit --delete-branch-on-merge" in log.read_text()
    log.write_text("")
    cli._offer_delete_on_merge(str(repo))                   # asked once per repo
    assert log.read_text() == ""


