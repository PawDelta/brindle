import json
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from brindle import disk, git, pool, trash, workspaces
from brindle.db import DB, PoolEntry, Workspace
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
    teardown_calls = []

    def teardown(commands, cwd, env):
        teardown_calls.append(cwd)
        Path(cwd, "setup-artifact").unlink(missing_ok=True)
        return workspaces.SetupResult(True, "")

    fail_blocked_removal = True
    removals = []

    def remove(root, path, force=False):
        removals.append(path)
        if path == entries[1].path and fail_blocked_removal:
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
    assert entries[0].path in removals and entries[2].path in removals
    assert not Path(entries[0].path).exists() and not Path(entries[2].path).exists()
    if cleanup == "sweep":
        [reservation] = db.pool_claims(str(tmp_path))
        assert reservation.path == entries[1].path
        assert reservation.state == "remove_pending"
        assert teardown_calls.count(entries[1].path) == 1

        fail_blocked_removal = False
        pool.sweep(db, str(tmp_path))

        assert removals.count(entries[1].path) == 2
        assert not Path(entries[1].path).exists()
        assert db.pool_claims(str(tmp_path)) == []
        assert teardown_calls.count(entries[1].path) == 1
    else:
        entries[1].ready = 0
        assert db.pool_entries(str(tmp_path), ready_only=False) == [entries[1]]


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


@pytest.mark.variant("a session cut from origin's tip must reuse the warm pool entry instead of a fresh worktree")
def test_session_worktree_from_origins_tip_claims_the_pool_entry(db, repo, tmp_path):
    """A new session's worktree starts at origin's tip as just fetched, ahead of
    the local main the entry was built from: it still takes the ready entry
    (moved forward), so its setup isn't run again behind the session's start."""
    counter = tmp_path / "counter"
    write_config(repo, setup=[f'echo x >> "{counter}"'])
    sh("git add -A && git commit -qm setup && git push -q origin main", repo)
    pool.fill(db, str(repo))
    assert counter.read_text().count("x") == 1
    (repo / "new.txt").write_text("from origin\n")
    sh("git add -A && git commit -qm ahead && git push -q origin main && git reset -q --hard HEAD~1", repo)
    tip = git.out(["rev-parse", "origin/main"], str(repo))
    assert tip != git.out(["rev-parse", "main"], str(repo))

    created = workspaces.create(db, str(repo), "brindle/session-2", base="main", start=tip, apply_prefix=False)
    assert created.how == "pool"
    assert git.out(["rev-parse", "HEAD"], created.workspace.path) == tip
    assert git.out(["branch", "--show-current"], created.workspace.path) == "brindle/session-2"
    assert counter.read_text().count("x") == 1


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


def test_sweep_waits_for_claim_lock_and_preserves_registered_workspace(db, repo, monkeypatch):
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    entry = db.pool_entries(str(repo), "main")[0]

    rebind_started = threading.Event()
    release_rebind = threading.Event()
    sweep_lock_attempted = threading.Event()
    sweep_lock_acquired = threading.Event()
    sweep_finished = threading.Event()
    real_rebind = pool.rebind
    real_claim_lock = pool.claim_lock

    @contextmanager
    def observed_claim_lock(repo_root):
        is_sweep_thread = threading.current_thread().name == "sweep-workspaces"
        if is_sweep_thread:
            sweep_lock_attempted.set()
        with real_claim_lock(repo_root):
            if is_sweep_thread:
                sweep_lock_acquired.set()
            yield

    def paused_rebind(repo_root, candidate, branch, start_sha):
        rebind_started.set()
        assert release_rebind.wait(timeout=5)
        return real_rebind(repo_root, candidate, branch, start_sha)

    monkeypatch.setattr(pool, "rebind", paused_rebind)
    monkeypatch.setattr(pool, "claim_lock", observed_claim_lock)
    monkeypatch.setattr(disk, "live_in", lambda *a, **k: False)
    removals = []
    real_remove = git.remove_worktree

    def record_remove(root, path, force=False):
        removals.append(path)
        return real_remove(root, path, force=force)

    monkeypatch.setattr(git, "remove_worktree", record_remove)

    created = []
    failures = []

    def create_workspace():
        connection = DB()
        try:
            created.append(workspaces.create(
                connection, str(repo), "feature", apply_prefix=False
            ))
        except BaseException as error:
            failures.append(error)
        finally:
            connection.conn.close()

    def sweep_workspaces():
        connection = DB()
        try:
            pool.sweep(connection, str(repo))
        except BaseException as error:
            failures.append(error)
        finally:
            connection.conn.close()
            sweep_finished.set()

    create_thread = threading.Thread(target=create_workspace)
    sweep_thread = None

    try:
        create_thread.start()
        assert rebind_started.wait(timeout=5)
        assert db.count_pool_entries(str(repo), "main") == 0
        assert db.workspace_by_path(entry.path) is None
        [claim] = db.pool_claims(str(repo))
        assert claim.path == entry.path
        assert claim.state == "active"

        sweep_thread = threading.Thread(
            target=sweep_workspaces,
            name="sweep-workspaces",
        )
        sweep_thread.start()
        assert sweep_lock_attempted.wait(timeout=5)
        assert not sweep_lock_acquired.wait(timeout=0.1)
        assert not sweep_finished.is_set()
        assert entry.path not in removals
    finally:
        release_rebind.set()
        if create_thread.ident is not None:
            create_thread.join(timeout=10)
        if sweep_thread is not None and sweep_thread.ident is not None:
            sweep_thread.join(timeout=10)

    assert not create_thread.is_alive()
    assert sweep_thread is not None and not sweep_thread.is_alive()
    assert sweep_lock_acquired.is_set()
    assert not failures
    assert len(created) == 1
    assert sweep_finished.is_set()
    assert db.pool_claims(str(repo)) == []
    assert db.workspace_by_path(entry.path) is not None
    assert entry.path not in removals


def test_rebind_and_cleanup_git_errors_do_not_fall_back(db, repo, monkeypatch):
    write_config(repo, setup=["true"])
    entry = PoolEntry(
        str(repo / ".brindle" / "worktrees" / "race" / "_w" / "race"),
        str(repo),
        "main",
        git.out(["rev-parse", "main"], str(repo)),
        "brindle-pool/race",
        "fingerprint",
        workspaces._next_port_base(db),
        1,
        1000.0,
    )
    db.add_pool_entry(entry)

    monkeypatch.setattr(
        pool,
        "rebind",
        lambda *a, **k: (_ for _ in ()).throw(git.GitError("rebind failed")),
    )
    discard_calls = []
    monkeypatch.setattr(pool, "discard", lambda *a, **k: discard_calls.append((a, k)))
    monkeypatch.setattr(
        pool,
        "teardown_entry",
        lambda *a, **k: (_ for _ in ()).throw(git.GitError("cleanup failed")),
    )

    fallback_attempted = []
    monkeypatch.setattr(
        git,
        "add_worktree",
        lambda *a, **k: fallback_attempted.append(True) or "new",
    )

    with pytest.raises(git.GitError, match="rebind failed"):
        workspaces.create(db, str(repo), "feature", apply_prefix=False)

    assert discard_calls == []
    assert fallback_attempted == []
    [claim] = db.pool_claims(str(repo))
    assert claim.state == "cleanup_pending"


def test_sweep_recovers_active_claim(db, repo, monkeypatch):
    write_config(repo, setup=["true"], pool_size=1)
    pool.fill(db, str(repo))
    [entry] = db.pool_entries(str(repo), "main")

    removals = []
    real_remove = git.remove_worktree

    def record_remove(root, path, force=False):
        removals.append(path)
        return real_remove(root, path, force=force)

    monkeypatch.setattr(git, "remove_worktree", record_remove)
    monkeypatch.setattr(disk, "live_in", lambda *a, **k: False)

    claimed = db.take_pool_entry(str(repo), "main")
    assert claimed is not None

    pool.sweep(db, str(repo))

    assert removals == [entry.path]
    assert db.pool_claims(str(repo)) == []
    assert not os.path.exists(entry.path)


def test_abandoned_claim_is_recovered_after_claim_lock_is_released(db, repo, monkeypatch):
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    [entry] = db.pool_entries(str(repo), "main")
    claimed = db.take_pool_entry(str(repo), "main")
    assert claimed is not None
    assert db.pool_claims(str(repo))[0].path == entry.path

    removed = []
    real_remove = git.remove_worktree
    monkeypatch.setattr(git, "remove_worktree", lambda root, path, force=False: (
        removed.append(path), real_remove(root, path, force=True)
    ))

    pool.sweep(db, str(repo))

    assert entry.path in removed
    assert db.pool_claims(str(repo)) == []
    assert not os.path.exists(entry.path)


def test_sweep_recovers_claim_after_rebind_before_workspace_registration(db, repo):
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    [entry] = db.pool_entries(str(repo), "main")
    target_branch = "feature"

    with pool.claim_lock(str(repo)):
        claimed = pool.claim(db, str(repo), "main")
        assert claimed is not None
        db.mark_pool_claim_target(claimed.claim_token, target_branch)
        pool.rebind(str(repo), claimed, target_branch, claimed.base_sha)

    assert db.workspace_by_path(entry.path) is None
    assert git.branch_exists(str(repo), target_branch)
    [claim] = db.pool_claims(str(repo))
    assert claim.target_branch == target_branch

    pool.sweep(db, str(repo))

    assert db.pool_claims(str(repo)) == []
    assert not os.path.exists(entry.path)
    assert not git.branch_exists(str(repo), entry.branch)
    assert not git.branch_exists(str(repo), target_branch)


def test_sweep_does_not_delete_unrelated_branch_at_abandoned_claim_path(db, repo):
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    [entry] = db.pool_entries(str(repo), "main")
    target_branch = "feature"
    unrelated_branch = "unrelated"
    sh(f"git branch {target_branch} main", repo)

    with pool.claim_lock(str(repo)):
        claimed = pool.claim(db, str(repo), "main")
        assert claimed is not None
        db.mark_pool_claim_target(claimed.claim_token, target_branch)
        sh(f"git checkout -b {unrelated_branch}", claimed.path)

    pool.sweep(db, str(repo))

    assert db.pool_claims(str(repo)) == []
    assert not os.path.exists(entry.path)
    assert git.branch_exists(str(repo), target_branch)
    assert git.branch_exists(str(repo), unrelated_branch)
    assert not git.branch_exists(str(repo), entry.branch)


def test_workspace_registration_failure_preserves_claim(db, repo, monkeypatch):
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    [entry] = db.pool_entries(str(repo), "main")

    def registration_failure(connection, ws, claim_token):
        raise git.GitError("registration failed")

    monkeypatch.setattr(DB, "add_pool_claimed_workspace", registration_failure)

    with pytest.raises(git.GitError, match="registration failed"):
        workspaces.create(db, str(repo), "feature", apply_prefix=False)

    assert db.pool_claims(str(repo))[0].path == entry.path
    assert db.workspace_by_path(entry.path) is None


def test_rebind_failure_with_successful_discard_falls_back(db, repo, monkeypatch):
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))

    monkeypatch.setattr(
        pool,
        "rebind",
        lambda *a, **k: (_ for _ in ()).throw(git.GitError("rebind failed")),
    )

    created = workspaces.create(db, str(repo), "feature", apply_prefix=False)

    assert created.how == "new"
    assert db.pool_claims(str(repo)) == []
    assert db.workspace_by_path(created.workspace.path) is not None


def test_failed_rebind_does_not_delete_concurrent_target_branch(db, repo, monkeypatch, tmp_path):
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    [claimed_entry] = db.pool_entries(str(repo), "main")
    target_branch = "feature"
    other_path = tmp_path / "other-feature"

    def failed_rebind(repo_root, candidate, branch, start_sha):
        sh(f"git worktree add -q -b {branch} {other_path} main", repo)
        raise git.GitError("rebind failed")

    monkeypatch.setattr(pool, "rebind", failed_rebind)

    with pytest.raises(git.GitError):
        workspaces.create(db, str(repo), target_branch, apply_prefix=False)

    cached_branch_exists = git.branch_exists(str(repo), target_branch)
    ref_exists = git.run(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{target_branch}"],
        str(repo),
        check=False,
    ).returncode == 0
    assert ref_exists
    assert cached_branch_exists
    assert git.worktree_for_branch(str(repo), target_branch) == str(other_path)
    assert not os.path.exists(claimed_entry.path)
    assert db.pool_claims(str(repo)) == []


def test_same_process_threads_cannot_both_hold_claim_lock(repo):
    attempting = threading.Event()
    entered = threading.Event()
    failures = []

    def contend():
        try:
            attempting.set()
            with pool.claim_lock(str(repo)):
                entered.set()
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=contend)
    blocked_until_release = False
    try:
        with pool.claim_lock(str(repo)):
            thread.start()
            assert attempting.wait(timeout=5)
            blocked_until_release = not entered.wait(timeout=0.1)
    finally:
        if thread.ident is not None:
            thread.join(timeout=5)

    assert not thread.is_alive()
    assert blocked_until_release
    assert entered.is_set()
    assert not failures

