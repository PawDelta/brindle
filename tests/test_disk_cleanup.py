"""Giving disk space back (brindle.disk): merged worktrees removed whole,
abandoned ones reported, retention of brindle's own files, the free-disk
guard, and dead branches."""

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brindle import agents, config, disk, doctor, history, spawn, tasks, workspaces
from brindle.cli import app
from brindle.daemon import routes_actions
from brindle.db import Agent

from conftest import sh

DAY = 86400


def _settings(values: dict) -> None:
    path = config.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values))


def _worktree(repo: Path, branch: str, merge: str | None = None, days_old: float = 1) -> Path:
    """A worktree of ``repo`` where brindle puts them, one commit on ``branch``
    made ``days_old`` days ago, merged into main (``merge``: "merge" or
    "squash") or not, with a Rust target dir."""
    wt = workspaces.new_worktree_root(str(repo)) / branch
    sh(f"git worktree add -q -b {branch} {wt}", repo)
    (wt / ".gitignore").write_text(".env\ntarget/\n")
    (wt / f"{branch}.txt").write_text("work\n")
    when = f"{int(time.time() - days_old * DAY)} +0000"
    sh(f"git add -A && GIT_AUTHOR_DATE='{when}' GIT_COMMITTER_DATE='{when}' git commit -qm {branch}", wt)
    if merge == "merge":
        sh(f"git merge -q --no-ff -m m {branch}", repo)
    elif merge == "squash":
        sh(f"git merge -q --squash {branch} && git commit -qm 'Squash merge {branch}'", repo)
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "app").write_bytes(b"x" * 4096)
    reflog = disk._gitdir(str(wt)) / "logs" / "HEAD"
    os.utime(reflog, (time.time() - days_old * DAY,) * 2)
    return wt


def _archived(repo: Path, branch: str) -> str:
    return sh(f"git rev-parse {disk.ARCHIVE_REF}{branch}", repo).strip()


def test_upgrade_sweep_removes_merged_worktrees_whole_once_and_never_a_dirty_one(db, repo):
    done = _worktree(repo, "old-done", merge="squash")
    tip = sh("git rev-parse old-done", repo).strip()
    dirty = _worktree(repo, "old-wip", merge="merge")
    (dirty / "wip.txt").write_text("not committed\n")
    history.record(db, str(repo), "merge", branch="old-done")   # known only from its history

    assert [r["path"] for r in disk.upgrade_sweep(db)] == [str(done)]

    assert not done.exists() and _archived(repo, "old-done") == tip
    assert sh("git branch --list old-done", repo).strip() == ""
    assert (dirty / "wip.txt").is_file() and (dirty / "target" / "debug" / "app").is_file()
    assert disk.clean(db, path=str(dirty))["kept"][0]["reason"].endswith("uncommitted changes")
    assert dirty.is_dir()
    assert disk.upgrade_sweep(db) is None   # recorded: it runs once


@pytest.mark.parametrize("state", ["dirty", "live"])
def test_dirty_or_live_abandoned_worktree_is_never_removed(db, repo, monkeypatch, state):
    wt = _worktree(repo, "idea", days_old=30)
    if state == "dirty":
        (wt / "notes.txt").write_text("unsaved thought\n")
    else:
        ws = workspaces.adopt_root(db, str(wt))
        db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "processing",
                           "%1", None, time.time()))
        monkeypatch.setattr(agents, "is_alive", lambda a, *rest, **kw: True)

    out = disk.clean(db, abandoned=True)

    assert out["removed"] == [] and wt.is_dir() and (wt / "target" / "debug" / "app").is_file()
    [listed] = disk.storage_report(db, str(repo))["worktrees"]
    assert listed["removable"] is False and listed["reason"]


def test_abandoned_worktree_is_only_listed_until_cleaned(db, repo, monkeypatch):
    wt = _worktree(repo, "stalled", days_old=30)
    tip = sh("git rev-parse stalled", repo).strip()
    monkeypatch.chdir(repo)

    assert CliRunner().invoke(app, ["clean"]).exit_code == 0
    [listed] = disk.storage_report(db, str(repo))["worktrees"]
    assert listed["path"] == str(wt) and listed["state"] == "abandoned" and listed["removable"]
    assert listed["size"] >= listed["build_size"] >= 4096
    assert wt.is_dir() and "stalled" in sh("git branch --list stalled", repo)

    out = routes_actions.clean_storage(db, {}, {"path": str(wt)})

    assert [r["archived"] for r in out["removed"]] == [f"{disk.ARCHIVE_REF}stalled"]
    assert not wt.exists() and _archived(repo, "stalled") == tip
    assert sh("git branch --list stalled", repo).strip() == ""


def test_retention_caps_backups_archive_and_logs(brindle_home):
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
        os.utime(d, (time.time() - age * DAY,) * 2)
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
    assert _archived(repo, "squashed") == tip and _archived(repo, "plain")


def test_clean_lists_then_removes_merged_and_archives_stale_branches(db, repo, monkeypatch):
    wt = _worktree(repo, "feat", merge="merge")
    sh("git checkout -q -b old-idea && git commit -q --allow-empty -m idea && git checkout -q main", repo)
    tip = sh("git rev-parse old-idea", repo).strip()
    _settings({"retention": {"stale_branch_days": 0}})
    monkeypatch.chdir(repo)
    runner = CliRunner()

    dry = runner.invoke(app, ["clean", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "old-idea" in dry.output and str(wt) in dry.output
    assert wt.is_dir() and sh("git branch --list old-idea", repo).strip() == "old-idea"
    report = {c.name: c.detail for c in doctor.disk_checks(str(repo))}
    assert "old-idea" in report["dead branches"] and str(wt) in report["merged worktrees"]
    sh("git worktree add -q ../noise -b noise", repo)   # not made by brindle: never removed

    done = runner.invoke(app, ["clean", "--branches", "--yes"])

    assert done.exit_code == 0, done.output
    assert not wt.exists() and _archived(repo, "feat")
    assert sh("git branch --list old-idea", repo).strip() == ""
    assert _archived(repo, "old-idea") == tip
    assert "noise" in sh("git branch --list noise", repo)


def test_pushed_branch_is_deleted_on_origin_once_its_pr_merged(repo):
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
