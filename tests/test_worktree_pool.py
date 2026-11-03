import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from brindle import git, pool, workspaces
from brindle.db import DB

from conftest import sh


def write_config(repo, **cfg):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(cfg))


@pytest.fixture(autouse=True)
def no_background_fill(monkeypatch):
    # create()/start() launch a detached `brindle _pool-fill` subprocess after a
    # claim; that would race these tests' own counter-file assertions since it
    # runs against the same test DB. Refilling itself is covered directly via
    # pool.fill()/fill_locked().
    monkeypatch.setattr(pool, "fill_in_background", lambda repo_root: None)


def test_lockfile_change_triggers_setup_again(db, repo, tmp_path):
    counter = tmp_path / "counter"
    write_config(repo, setup=[f'echo x >> "{counter}"'])
    (repo / "uv.lock").write_text("v1\n")
    sh("git add -A && git commit -qm lock", repo)
    pool.fill(db, str(repo))
    assert counter.read_text().count("x") == 1

    (repo / "uv.lock").write_text("v2\n")
    sh("git add -A && git commit -qm lock2", repo)

    created = workspaces.create(db, str(repo), "feature")
    assert created.how == "pool"
    assert created.setup is not None and created.setup.ok
    assert counter.read_text().count("x") == 2
    assert git.out(["rev-parse", "HEAD"], created.workspace.path) == git.out(
        ["rev-parse", "main"], str(repo)
    )


def test_failed_claim_falls_back(db, repo, tmp_path):
    counter = tmp_path / "counter"
    write_config(repo, setup=[f'echo x >> "{counter}"'])
    entry = pool.fill_one(db, str(repo))
    assert entry is not None
    shutil.rmtree(entry.path, ignore_errors=True)  # corrupt the entry on disk

    created = workspaces.create(db, str(repo), "feature")
    assert created.how != "pool"
    assert created.setup is not None and created.setup.ok
    assert counter.read_text().count("x") == 2  # once during fill, once via fallback
    assert db.count_pool_entries(str(repo), "main") == 0


@pytest.mark.variant("data loss: a claim must never reset an existing branch")
@pytest.mark.parametrize("where", ["local", "remote"])
def test_claim_skipped_when_the_branch_exists(db, repo, tmp_path, where):
    """A pool claim must never `checkout -B` an existing branch (that would
    reset it onto the pool's base sha and discard its commits), nor replace a
    branch only on origin with an entry at the local base commit."""
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    other = tmp_path / "other-checkout"
    sh(f"git worktree add -q -b feature {other} main", repo)
    (other / "own_work.txt").write_text("precious")
    sh("git add -A && git commit -qm 'own work'", other)
    sha = sh("git rev-parse feature", other)
    if where == "remote":
        sh("git push -q origin feature", other)
    sh(f"git worktree remove --force {other}", repo)
    if where == "remote":
        sh("git branch -D feature && git fetch -q origin", repo)
        assert git.remote_branch_exists(str(repo), "feature") and not git.branch_exists(str(repo), "feature")

    created = workspaces.create(db, str(repo), "feature")
    assert created.how != "pool"
    assert git.out(["rev-parse", "HEAD"], created.workspace.path) == sha
    assert (Path(created.workspace.path) / "own_work.txt").exists()
    assert db.count_pool_entries(str(repo), "main") == 1  # entry left untouched


def test_two_concurrent_claims_only_one_wins(db, repo):
    """take_pool_entry uses BEGIN IMMEDIATE, so two connections racing to
    claim the same entry must never both succeed."""
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    assert db.count_pool_entries(str(repo), "main") == 1

    results: list = []
    barrier = threading.Barrier(2)

    def take():
        conn = DB()  # its own connection, created (and used) in this thread
        barrier.wait()
        results.append(conn.take_pool_entry(str(repo), "main"))

    threads = [threading.Thread(target=take) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    won = [r for r in results if r is not None]
    assert len(won) == 1
    assert db.count_pool_entries(str(repo), "main") == 0


def test_sweep_never_deletes_a_worktree_claimed_by_a_workspace(db, repo):
    """CRITICAL regression: once a pool entry is claimed, its pool_entries row
    is gone but its directory stays right where it was, inside the pool dir.
    sweep must recognize it as a live workspace (by path, and by its branch
    no longer being a brindle-pool/* placeholder) and leave it alone."""
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    assert db.count_pool_entries(str(repo), "main") == 1

    created = workspaces.create(db, str(repo), "feature")
    assert created.how == "pool"
    assert db.count_pool_entries(str(repo), "main") == 0  # claimed: row gone

    pool.sweep(db, str(repo))

    assert os.path.isdir(created.workspace.path)
    assert git.branch_exists(str(repo), created.workspace.branch)
    assert db.workspace_by_path(created.workspace.path) is not None


def test_trim_skips_discard_when_a_claim_wins_the_race(db, repo, monkeypatch):
    """If a claim (take_pool_entry) removes an entry's row between trim
    listing it and trim deleting it, trim must not discard the now-claimed
    worktree: delete_pool_entry's rowcount is the only reliable signal."""
    write_config(repo, setup=["true"], pool_size=2)
    pool.fill(db, str(repo))
    assert db.count_pool_entries(str(repo), "main") == 2

    write_config(repo, setup=["true"], pool_size=0)
    victim = db.pool_entries(str(repo), "main")[0]

    real_delete = DB.delete_pool_entry

    def racing_delete(self, path):
        if path == victim.path:
            # A concurrent claim grabs this exact (oldest) entry first.
            DB().take_pool_entry(str(repo), "main")
        return real_delete(self, path)

    monkeypatch.setattr(DB, "delete_pool_entry", racing_delete)

    removed = pool.trim(db, str(repo))

    assert removed == 1  # only the entry that wasn't raced was actually trimmed
    assert db.count_pool_entries(str(repo), "main") == 0
    assert os.path.isdir(victim.path)  # claimed worktree left untouched
    assert git.branch_exists(str(repo), victim.branch)

