import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from brindle import git, pool, trash, workspaces
from brindle.db import DB, PoolEntry
from brindle.config import RepoConfig

from conftest import sh
from git_snapshots import fast_host_tools, repo  # noqa: F401  (fixtures)


def write_config(repo, **cfg):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(cfg))


@pytest.mark.parametrize("cleanup", ["sweep", pytest.param("trim", marks=pytest.mark.variant(
    "the same disk-full failure while shrinking the configured pool"))])
def test_pool_cleanup_continues_after_a_failed_removal(db, tmp_path, monkeypatch, cleanup):
    entries = [PoolEntry(str(tmp_path / n), str(tmp_path), "main", "sha", n, "", None, int(cleanup == "trim"), i)
               for i, n in enumerate(("before", "blocked", "after"))]
    for entry in entries:
        Path(entry.path).mkdir()
        Path(entry.path, "setup-artifact").touch()
        db.add_pool_entry(entry)
    def teardown(commands, cwd, env):
        Path(cwd, "setup-artifact").unlink(missing_ok=True)
        return workspaces.SetupResult(True, "")
    def remove(root, path, force=False):
        if path == entries[1].path:
            raise git.GitError("disk full")
        Path(path).rmdir()
    monkeypatch.setattr(git, "remove_worktree", remove)
    monkeypatch.setattr(git, "run", lambda *a, **k: None)
    monkeypatch.setattr(git, "list_branches", lambda *a: [])
    monkeypatch.setattr(git, "list_worktrees", lambda *a: [])
    monkeypatch.setattr(workspaces, "run_commands", teardown)
    monkeypatch.setattr(pool, "load_repo_config", lambda *a: RepoConfig(
        base_branch="main", pool_size=0, teardown=["remove setup artifact"]))
    getattr(pool, cleanup)(db, str(tmp_path))
    assert not Path(entries[1].path, "setup-artifact").exists()
    assert pool.claim(db, str(tmp_path), "main") is None
    entries[1].ready = 0
    assert db.pool_entries(str(tmp_path), ready_only=False) == [entries[1]]
    assert not Path(entries[0].path).exists() and not Path(entries[2].path).exists()


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


def test_failed_claim_falls_back(db, repo, tmp_path, monkeypatch):
    write_config(repo, setup=["false"])
    attempted = []
    clock = [1000.0]

    def failed_setup(commands, cwd, env):
        attempted.append((cwd, git.out(["branch", "--show-current"], cwd)))
        return workspaces.SetupResult(False, "setup failed")

    with monkeypatch.context() as patch:
        patch.setattr(workspaces, "run_commands", failed_setup)
        patch.setattr(pool.time, "time", lambda: clock[0])
        assert pool.fill(db, str(repo)) == 0
        assert db.last_pool_failure(str(repo), "main") == clock[0]
        assert db.pool_entries(str(repo), ready_only=False) == []
        assert len(attempted) == 1
        assert not Path(attempted[0][0]).exists()
        [entry] = trash.entries()
        assert entry["path"] == attempted[0][0]
        assert not git.branch_exists(str(repo), attempted[0][1])
        assert pool.fill(db, str(repo)) == 0
        assert len(attempted) == 1
        clock[0] += pool.FAILURE_BACKOFF
        assert pool.fill(db, str(repo)) == 0
        assert len(attempted) == 2

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


@pytest.mark.variant("Two independent connections racing for one pool entry must never both claim it")
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
    write_config(repo, setup=["true"], copy=[".env"])
    pool.fill(db, str(repo))
    assert db.count_pool_entries(str(repo), "main") == 1
    [entry] = db.pool_entries(str(repo), "main")
    assert (Path(entry.path) / ".env").read_text() == "SECRET=1\n"
    (repo / ".env").write_text("SECRET=2\n")

    created = workspaces.create(db, str(repo), "feature")
    assert created.how == "pool"
    assert (Path(created.workspace.path) / ".env").read_text() == "SECRET=2\n"
    assert ".env" in created.copied
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

