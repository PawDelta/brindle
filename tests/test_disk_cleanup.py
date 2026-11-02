"""Giving disk space back (brindle.disk): build dirs of merged worktrees,
retention of brindle's own files, the free-disk guard, and dead branches."""

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brindle import agents, config, disk, doctor, spawn, tasks, workspaces
from brindle.cli import app
from brindle.db import Agent

from conftest import sh


def _settings(values: dict) -> None:
    path = config.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values))


def _merged_worktree(repo: Path, branch: str = "feat") -> Path:
    """A linked worktree of ``repo`` whose branch merged into main, with a
    tracked file under a folder named like a build dir, and real build dirs."""
    wt = repo.parent / f"wt-{branch}"
    sh(f"git worktree add -q -b {branch} {wt}", repo)
    (wt / ".gitignore").write_text(".env\ntarget/\nnode_modules/\n")
    (wt / "lib" / "target").mkdir(parents=True)
    (wt / "lib" / "target" / "main.rs").write_text("fn main() {}\n")
    sh("git add -A && git add -f lib/target/main.rs && git commit -qm feat", wt)
    sh(f"git merge -q {branch}", repo)
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "app").write_bytes(b"x" * 4096)
    (wt / "app" / "node_modules" / "pkg").mkdir(parents=True)
    (wt / "app" / "node_modules" / "pkg" / "index.js").write_text("1\n")
    return wt


def test_merged_worktree_loses_build_dirs_but_keeps_source(db, repo):
    wt = _merged_worktree(repo)
    ws = workspaces.adopt_root(db, str(wt))

    workspaces.remove(db, ws)

    assert not (wt / "target").exists() and not (wt / "app" / "node_modules").exists()
    assert (wt / "lib" / "target" / "main.rs").read_text() == "fn main() {}\n"
    assert (wt / ".gitignore").is_file()


@pytest.mark.parametrize("state", ["dirty", "live"])
def test_dirty_or_live_worktree_is_untouched(db, repo, monkeypatch, state):
    wt = _merged_worktree(repo)
    ws = workspaces.adopt_root(db, str(wt))
    if state == "dirty":
        (wt / "notes.txt").write_text("unsaved thought\n")
    else:
        db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "processing",
                           "%1", None, time.time()))
        monkeypatch.setattr(agents, "is_alive", lambda a, *rest, **kw: True)

    cleaned = disk.clean_worktree(db, str(wt))

    assert cleaned.removed == [] and cleaned.kept
    assert (wt / "target" / "debug" / "app").is_file()
    assert (wt / "app" / "node_modules" / "pkg" / "index.js").is_file()


def test_retention_caps_backups_archive_and_logs(brindle_home, monkeypatch):
    home = config.brindle_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "brindle.db").write_text("live")
    for i in range(4):
        b = home / f"brindle.db.before-fix-{i}"
        b.write_text("old")
        os.utime(b, (1000 + i, 1000 + i))
    (home / "ollama.log").write_bytes(b"y" * (2 * disk.MB))
    (home / "hooks.log").write_text("small\n")
    old = home / "archive" / "transcripts" / "a1"
    new = home / "archive" / "transcripts" / "a2"
    for d, age in ((old, 40), (new, 1)):
        d.mkdir(parents=True)
        (d / "t.jsonl.gz").write_text("t")
        os.utime(d, (time.time() - age * 86400,) * 2)
    (home / "archive" / "brindle.jsonl").write_text("{}\n")
    _settings({"retention": {"log_mb": 1}})

    disk.retain()

    assert sorted(p.name for p in home.glob("brindle.db.before-*")) == [
        "brindle.db.before-fix-2", "brindle.db.before-fix-3"]
    assert (home / "brindle.db").read_text() == "live"
    assert (home / "ollama.log").stat().st_size == 0
    assert (home / "ollama.log.1").stat().st_size == 2 * disk.MB
    assert (home / "hooks.log").read_text() == "small\n"
    assert not old.exists() and new.is_dir() and (home / "archive" / "brindle.jsonl").is_file()


@pytest.mark.parametrize("held", [
    lambda repo: agents.admission("developer", str(repo)),
    lambda repo: spawn.check_no_room(str(repo)),
])
def test_low_free_disk_holds_a_launch(repo, monkeypatch, held):
    monkeypatch.setattr(disk, "free_bytes", lambda path: 3 * disk.GB)

    why = held(repo)

    assert why is not None and why.startswith("waiting for disk: 3.0 GB free")
    assert tasks.room_wait(str(repo)) == why


def test_merged_and_squash_merged_branches_go_with_their_workspace(db, repo):
    plain = workspaces.create(db, str(repo), "plain").workspace
    squashed = workspaces.create(db, str(repo), "squashed").workspace
    for ws in (plain, squashed):
        Path(ws.path, f"{ws.name}.txt").write_text("work\n")
        sh(f"git add -A && git commit -qm {ws.name} && git commit -q --allow-empty -m more", ws.path)
    sh(f"git merge -q --no-ff {plain.branch}", repo)
    sh(f"git merge -q --squash {squashed.branch} && git commit -qm 'Squash merge'", repo)
    tip = sh(f"git rev-parse {squashed.branch}", repo).strip()

    for ws in (plain, squashed):
        assert workspaces.remove(db, ws).branch_deleted

    assert sh("git branch --list plain squashed", repo).strip() == ""
    assert sh("git rev-parse refs/brindle-archive/squashed", repo).strip() == tip


def test_clean_lists_then_reclaims_and_archives_stale_branches(db, repo, monkeypatch):
    wt = _merged_worktree(repo)
    sh("git checkout -q -b old-idea && git commit -q --allow-empty -m idea && git checkout -q main", repo)
    tip = sh("git rev-parse old-idea", repo).strip()
    _settings({"retention": {"stale_branch_days": 0}})
    monkeypatch.chdir(repo)
    runner = CliRunner()

    dry = runner.invoke(app, ["clean", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "old-idea" in dry.output and str(wt) in dry.output
    assert sh("git branch --list old-idea", repo).strip() == "old-idea"
    assert (wt / "target").is_dir()
    report = {c.name: c.detail for c in doctor.disk_checks(str(repo))}
    assert "old-idea" in report["dead branches"] and "disk use" in report
    sh("git worktree add -q ../noise -b noise", repo)   # a fresh branch with a worktree is not stale

    done = runner.invoke(app, ["clean", "--branches", "--yes"])

    assert done.exit_code == 0, done.output
    assert not (wt / "target").exists() and (wt / "lib" / "target" / "main.rs").is_file()
    assert sh("git branch --list old-idea", repo).strip() == ""
    assert sh("git rev-parse refs/brindle-archive/old-idea", repo).strip() == tip
    assert "noise" in sh("git branch --list noise", repo)


def test_pushed_branch_is_deleted_on_origin_once_its_pr_merged(repo, monkeypatch):
    sh("git checkout -q -b brindle/session-1 && git commit -q --allow-empty -m s"
       " && git push -q origin brindle/session-1 && git checkout -q main", repo)
    sha = sh("git rev-parse brindle/session-1", repo).strip()
    ws = workspaces.Workspace(id="r/s", repo_root=str(repo), name="s", kind="main",
                              branch="brindle/session-1", base_branch="main", path=str(repo),
                              port_base=None, tmux_session="x", created_at=0.0)
    assert disk.delete_pushed_remote(ws, sha) is None   # off by default
    _settings({"delete_pushed_branches": True})

    assert disk.delete_pushed_remote(ws, sha)
    assert sh("git ls-remote --heads origin brindle/session-1", repo).strip() == ""


def test_spotlight_is_kept_out_of_brindle_dirs(db, repo, monkeypatch):
    monkeypatch.setattr(disk.sys, "platform", "darwin")

    root = workspaces.new_worktree_root(str(repo))
    workspaces.new_worktree_root(str(repo))

    assert (root / disk.NEVER_INDEX).is_file()
    assert (config.brindle_home() / disk.NEVER_INDEX).is_file()
