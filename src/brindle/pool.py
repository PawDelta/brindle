"""A pool of pre-built worktrees, so a fresh worker doesn't have to wait for
setup (``pnpm install``, ``uv sync``, ...) inside the ``create`` call.

An entry is a worktree under ``<worktree_root>/_w/<token>`` (before #182,
``~/.brindle/worktrees/<repo-slug>/_w/<token>``),
checked out on a placeholder branch (``brindle-pool/<token>``) at the base
branch's tip, with the repo's ``copy`` files and ``setup`` already applied.
``workspaces.create`` claims one (renaming its branch to the real one)
instead of running ``git worktree add`` plus setup live, whenever the new
branch would start exactly where a pool entry already sits. A claimed entry's
path and port block never change -- setup ran there for good, so a repo's
``setup`` must not depend on the branch name when ``pool_size`` > 0 (see the
README).

Refilling runs in a detached background process (``brindle _pool-fill``),
guarded by an flock so two fills for the same repo don't race. Each fill
first sweeps up anything a crashed fill (or a config change) left behind.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from brindle import disk, git, spawn, workspaces
from brindle.config import RepoConfig, load_repo_config, worktree_root, worktrees_dir
from brindle.db import DB, PoolClaim, PoolEntry, Workspace

POOL_SUBDIR = "_w"
FAILURE_BACKOFF = 600  # seconds to wait before retrying a fill after setup failed

LOCKFILES = ("uv.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
             "Cargo.lock", "poetry.lock", "go.sum")


def pool_dir(repo_root: str, cfg: RepoConfig | None = None) -> Path:
    return worktree_root(repo_root, cfg) / POOL_SUBDIR


def legacy_pool_dir(repo_root: str, db: DB) -> Path:
    """Where this repo's pool was before #182, swept until it's empty."""
    return worktrees_dir() / workspaces._db_slug(db, repo_root) / POOL_SUBDIR


def _slug(repo_root: str, path: str) -> str:
    """The slug an entry's placeholder workspace was built with."""
    if Path(path).parent.parent.parent == worktrees_dir():
        return Path(path).parent.parent.name   # a pre-#182 entry: the slug is in its path
    return workspaces._repo_slug(repo_root)


def fingerprint(repo_root: str, sha: str, cfg: RepoConfig) -> str:
    """A hash of the setup commands plus the contents of any common lockfile
    present at ``sha``, so a dependency change invalidates a pool entry that
    was built at an older commit."""
    h = hashlib.sha256()
    for cmd in cfg.setup:
        h.update(cmd.encode())
        h.update(b"\0")
    for name in LOCKFILES:
        proc = git.run(["show", f"{sha}:{name}"], repo_root, check=False)
        if proc.returncode == 0:
            h.update(name.encode())
            h.update(proc.stdout.encode())
            h.update(b"\0")
    return h.hexdigest()


# -- filling -----------------------------------------------------------------


def fill_one(db: DB, repo_root: str, cfg: RepoConfig | None = None) -> PoolEntry | None:
    """Build one pool entry from the repo's base branch, at its permanent
    path. Returns ``None`` (after cleaning up) if setup fails, or without
    building anything while free disk is low (``disk.low_disk``)."""
    from brindle import disk

    if disk.low_disk(repo_root) is not None:
        return None
    cfg = cfg or load_repo_config(repo_root)
    base = cfg.base_branch or git.default_branch(repo_root)
    base_ref = git.resolve_start_point(repo_root, base, cfg.fetch)
    base_sha = git.out(["rev-parse", base_ref], repo_root)

    token = uuid.uuid4().hex[:8]
    branch = f"brindle-pool/{token}"
    path = str(workspaces.new_worktree_root(repo_root, cfg) / POOL_SUBDIR / token)
    slug = _slug(repo_root, path)
    name = f"pool-{token}"
    port_base = workspaces._next_port_base(db)

    entry = PoolEntry(
        path=path, repo_root=repo_root, base_branch=base, base_sha=base_sha,
        branch=branch, fingerprint=fingerprint(repo_root, base_sha, cfg),
        port_base=port_base, ready=0, created_at=time.time(),
    )
    # Insert before doing any work, so a crash mid-build (worktree add, copy,
    # setup) leaves a row `sweep` can find and clean up.
    db.add_pool_entry(entry)

    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        git.run(["worktree", "add", "--no-track", "-b", branch, path, base_sha], repo_root)
    except git.GitError:
        db.delete_pool_entry(path)
        discard(repo_root, entry, cfg)
        return None

    workspaces._copy_local_files(repo_root, path, cfg.copy)
    placeholder = Workspace(
        id=f"{slug}/{name}", repo_root=repo_root, name=name,
        kind="worktree", branch=branch, base_branch=base, path=path, port_base=port_base,
        tmux_session=workspaces._session_name(repo_root, name, slug), created_at=entry.created_at,
    )
    setup = workspaces.run_setup_commands(placeholder, cfg) if cfg.setup else None
    if setup is not None and not setup.ok:
        db.delete_pool_entry(path)
        discard(repo_root, entry, cfg)
        return None
    db.mark_pool_ready(path)
    entry.ready = 1
    return entry


def fill(db: DB, repo_root: str) -> int:
    """Top the pool back up to the repo's configured ``pool_size``. Returns
    how many entries were added. Backs off for a while after a setup failure
    instead of retrying (and failing) on every trigger."""
    cfg = load_repo_config(repo_root)
    target = cfg.pool_size or 0
    if target <= 0:
        return 0
    base = cfg.base_branch or git.default_branch(repo_root)
    failed_at = db.last_pool_failure(repo_root, base)
    if failed_at is not None and time.time() - failed_at < FAILURE_BACKOFF:
        return 0
    added = 0
    while db.count_pool_entries(repo_root, base) < target:
        if fill_one(db, repo_root, cfg) is None:
            db.record_pool_failure(repo_root, base)
            break
        db.clear_pool_failure(repo_root, base)
        added += 1
    return added


def _lock_path(repo_root: str) -> Path:
    return pool_dir(repo_root).parent / ".pool.lock"


def _claim_lock_path(repo_root: str) -> Path:
    return pool_dir(repo_root).parent / ".claim.lock"


def _cleanup_lock_path(repo_root: str, path: str) -> Path:
    key = hashlib.sha256(os.fsencode(path)).hexdigest()
    return _claim_lock_path(repo_root).parent / ".cleanup-locks" / f"{key}.lock"


@contextmanager
def _cleanup_lock(repo_root: str, path: str) -> Iterator[None]:
    """Serialize cleanup attempts for one path without holding claim_lock."""
    lock_path = _cleanup_lock_path(repo_root, path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def claim_lock(repo_root: str) -> Iterator[None]:
    """Serialize pool claims and destructive worktree operations across
    threads and processes. The lock is released if the process exits."""
    path = _claim_lock_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def fill_locked(db: DB, repo_root: str) -> int:
    """Sweep, trim, then ``fill``, skipping entirely if another fill for this
    repo is already running. Uses an flock rather than a stale-time heuristic,
    so a lock is only ever released by the process (or OS) that holds it."""
    lock = _lock_path(repo_root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        try:
            sweep(db, repo_root)
            trim(db, repo_root)
            return fill(db, repo_root)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def fill_in_background(repo_root: str) -> None:
    """Start ``brindle _pool-fill <repo_root>`` detached, so refilling (and its
    sweep/trim) never blocks the caller (a claim, or supervisor start)."""
    from brindle.providers import brindle_invocation

    try:
        spawn.detach([*brindle_invocation(), "_pool-fill", repo_root])
    except OSError:
        pass


# -- claiming ----------------------------------------------------------------


def claim(db: DB, repo_root: str, base: str) -> PoolEntry | None:
    return db.take_pool_entry(repo_root, base)


def rebind(repo_root: str, entry: PoolEntry, branch: str, start_sha: str) -> None:
    """Turn a claimed entry into workspace ``branch``, in place: the entry's
    path and port never change. Only resets the worktree forward when the
    base moved past the entry's own base_sha, and only ever on the entry's
    own placeholder branch -- callers only claim when ``branch`` doesn't
    already exist locally or on origin, so this can never discard real work.
    """
    if entry.base_sha != start_sha:
        git.run(["checkout", "-B", entry.branch, start_sha], entry.path)
    git.run(["branch", "-m", entry.branch, branch], entry.path)


def teardown_entry(
    repo_root: str,
    entry: PoolEntry,
    cfg: RepoConfig | None = None,
) -> workspaces.SetupResult | None:
    """Run teardown for a pool entry, returning its result when configured."""
    cfg = cfg or load_repo_config(repo_root)
    if os.path.exists(entry.path) and cfg.teardown:
        name = f"pool-{entry.branch.removeprefix('brindle-pool/')}"
        slug = _slug(repo_root, entry.path)
        placeholder = Workspace(
            id=f"{slug}/{name}", repo_root=repo_root, name=name,
            kind="worktree", branch=entry.branch, base_branch=entry.base_branch,
            path=entry.path, port_base=entry.port_base,
            tmux_session=workspaces._session_name(repo_root, name, slug), created_at=entry.created_at,
        )
        return workspaces.run_commands(
            cfg.teardown, entry.path, workspaces.workspace_env(placeholder)
        )
    return None


def remove_entry(
    repo_root: str,
    entry: PoolEntry,
    target_branch: str | None = None,
) -> None:
    """Remove a pool worktree and branches after its teardown has completed."""
    if os.path.lexists(entry.path):
        git.remove_worktree(repo_root, entry.path, force=True)
    live_branches = {
        wt["branch"].removeprefix("refs/heads/")
        for wt in git.list_worktrees(repo_root) if "branch" in wt
    }
    if entry.branch not in live_branches:
        git.run(["branch", "-D", entry.branch], repo_root, check=False)
    if target_branch and target_branch not in live_branches:
        git.run(["branch", "-D", target_branch], repo_root, check=False)
    git.run(["worktree", "prune"], repo_root, check=False)


def discard(repo_root: str, entry: PoolEntry, cfg: RepoConfig | None = None,
            target_branch: str | None = None) -> None:
    """Run teardown and remove an entry, preserving the legacy combined API."""
    teardown_entry(repo_root, entry, cfg)
    remove_entry(repo_root, entry, target_branch)


# -- cleanup ------------------------------------------------------------------


def _discard_row(db: DB, repo_root: str, entry: PoolEntry, cfg: RepoConfig) -> bool:
    """Claim cleanup against concurrent pool claims, retaining failed entries.

    Deleting first is necessary: a claim that already took this row owns its
    checkout. If removal fails, restore the metadata so a later sweep still
    knows its teardown, and so the orphan pass leaves the folder alone.
    Teardown may already have dismantled setup, so a retained entry must
    remain cleanup-only rather than being offered to a new worker.
    """
    if db.delete_pool_entry(entry.path) != 1:
        return False
    try:
        discard(repo_root, entry, cfg)
    except git.GitError:
        entry.ready = 0
        db.add_pool_entry(entry)
        return False
    return True


def sweep(db: DB, repo_root: str, cfg: RepoConfig | None = None) -> None:
    """Clean up anything a crashed fill (or a config change since the last
    one) could have left behind: not-ready rows (safe to call this stale
    only because it runs under this repo's fill lock, which guarantees no
    other fill for this repo is running right now), entries for a base
    branch that's no longer configured, pool directories with no matching
    row, and placeholder branches with no worktree."""
    cfg = cfg or load_repo_config(repo_root)
    current_base = cfg.base_branch or git.default_branch(repo_root)
    reservations: dict[str, PoolClaim] = {}
    with claim_lock(repo_root):
        entries = db.pool_entries(repo_root, ready_only=False)
        claims = db.pool_claims(repo_root)
        workspace_paths = {ws.path for ws in db.find_workspaces()}
        entry_paths = {entry.path for entry in entries}
        claim_paths = {claim.path for claim in claims}

        for entry in entries:
            if entry.ready and entry.base_branch == current_base:
                continue
            if entry.path in claim_paths or entry.path in workspace_paths:
                continue
            reservation = db.reserve_pool_entry_cleanup(repo_root, entry.path)
            if reservation is not None:
                reservations[reservation.claim_token] = reservation

        for claim in claims:
            if claim.state == "active":
                if claim.path in entry_paths or claim.path in workspace_paths:
                    continue
                if not db.reserve_abandoned_pool_claim_cleanup(claim.claim_token):
                    continue
            elif claim.state not in ("cleanup_pending", "remove_pending"):
                continue
            reservation = db.pool_cleanup_reservation(repo_root, claim.path)
            if reservation is not None and reservation.claim_token == claim.claim_token:
                reservations[reservation.claim_token] = reservation

    for reservation in reservations.values():
        try:
            _cleanup_reservation(db, repo_root, reservation, cfg)
        except (git.GitError, OSError):
            continue

    # Everything currently claimed -- by a pool row, cleanup reservation, or
    # live workspace -- is off limits, no matter what directory it happens to
    # sit in. Known paths distinguish these active or reserved checkouts from
    # orphaned directories that can be removed.
    known_paths = {e.path for e in db.pool_entries(repo_root, ready_only=False)}
    known_paths |= {ws.path for ws in db.find_workspaces()}
    known_paths |= {claim.path for claim in db.pool_claims(repo_root)}
    pool_branches = set(git.list_branches(repo_root, "brindle-pool/*"))
    branch_by_path = {
        wt["worktree"]: wt["branch"].removeprefix("refs/heads/")
        for wt in git.list_worktrees(repo_root) if "branch" in wt
    }
    cwds = False
    for base_dir in (pool_dir(repo_root, cfg), legacy_pool_dir(repo_root, db)):
        if not base_dir.is_dir():
            continue
        for child in base_dir.iterdir():
            path = str(child)
            if path in known_paths:
                continue
            branch = branch_by_path.get(path)
            if branch is not None and branch not in pool_branches:
                continue  # not a pool worktree: leave it alone
            if cwds is False:
                cwds = disk._cwds(disk.live_check_ignore(repo_root))  # read once for the whole sweep
            if disk.live_in(db, path, cwds):
                continue  # a process is still using this directory: leave it alone
            try:
                with _cleanup_lock(repo_root, path):
                    with claim_lock(repo_root):
                        current_entries = db.pool_entries(repo_root, ready_only=False)
                        current_claims = db.pool_claims(repo_root)
                        current_workspaces = db.find_workspaces()
                        if (
                            any(entry.path == path for entry in current_entries)
                            or any(claim.path == path for claim in current_claims)
                            or any(ws.path == path for ws in current_workspaces)
                        ):
                            continue
                        current_worktrees = git.list_worktrees(repo_root)
                        # Recheck pool branches under the claim lock immediately before removal.
                        current_pool_branches = set(git.list_branches(repo_root, "brindle-pool/*"))
                        current_branch = next(
                            (
                                wt.get("branch", "").removeprefix("refs/heads/")
                                for wt in current_worktrees
                                if wt.get("worktree") == path and "branch" in wt
                            ),
                            None,
                        )
                        if current_branch is not None and current_branch not in current_pool_branches:
                            continue
                        git.remove_worktree(repo_root, path, force=True)
            except git.GitError:
                continue  # removal failed; leave it for a later sweep

    with claim_lock(repo_root):
        git.run(["worktree", "prune"], repo_root, check=False)

    for branch in git.list_branches(repo_root, "brindle-pool/*"):
        with claim_lock(repo_root):
            current_entries = db.pool_entries(repo_root, ready_only=False)
            current_claims = db.pool_claims(repo_root)
            current_workspaces = db.find_workspaces()
            if (
                any(entry.branch == branch for entry in current_entries)
                or any(claim.branch == branch for claim in current_claims)
                or any(ws.branch == branch for ws in current_workspaces)
            ):
                continue
            live_branches = {
                wt["branch"].removeprefix("refs/heads/")
                for wt in git.list_worktrees(repo_root) if "branch" in wt
            }
            if branch not in live_branches:
                git.run(["branch", "-D", branch], repo_root, check=False)


def _cleanup_reservation(
    db: DB,
    repo_root: str,
    reservation: PoolClaim,
    cfg: RepoConfig,
) -> bool:
    with _cleanup_lock(repo_root, reservation.path):
        current = db.pool_cleanup_reservation(repo_root, reservation.path)
        if current is None or current.claim_token != reservation.claim_token:
            return False
        if (
            any(entry.path == reservation.path for entry in db.pool_entries(repo_root, ready_only=False))
            or any(
                claim.path == reservation.path and claim.claim_token != reservation.claim_token
                for claim in db.pool_claims(repo_root)
            )
            or any(ws.path == reservation.path for ws in db.find_workspaces())
        ):
            return False

        entry = PoolEntry(
            current.path, current.repo_root, current.base_branch, current.base_sha,
            current.branch, current.fingerprint, current.port_base, 0,
            current.claimed_at, current.claim_token,
        )
        if current.state == "cleanup_pending":
            result = teardown_entry(repo_root, entry, cfg)
            if result is not None and not result.ok:
                return False
            if not db.mark_pool_cleanup_remove_pending(current.claim_token):
                return False
            current = db.pool_cleanup_reservation(repo_root, current.path)
            if current is None or current.claim_token != reservation.claim_token:
                return False
        if current.state != "remove_pending":
            return False

        with claim_lock(repo_root):
            entries = db.pool_entries(repo_root, ready_only=False)
            claims = db.pool_claims(repo_root)
            workspaces = db.find_workspaces()
            if (
                any(entry.path == current.path for entry in entries)
                or any(
                    claim.path == current.path and claim.claim_token != current.claim_token
                    for claim in claims
                )
                or any(ws.path == current.path for ws in workspaces)
            ):
                return False
            latest = db.pool_cleanup_reservation(repo_root, current.path)
            if (
                latest is None
                or latest.claim_token != current.claim_token
                or latest.state != "remove_pending"
            ):
                return False

            worktrees = git.list_worktrees(repo_root)
            target_branch = latest.target_branch
            target_is_checked_out_at_path = target_branch is not None and any(
                wt.get("worktree") == latest.path
                and wt.get("branch", "").removeprefix("refs/heads/") == target_branch
                for wt in worktrees
            )
            if target_branch is not None and (
                not target_is_checked_out_at_path
                or any(
                    wt.get("branch", "").removeprefix("refs/heads/") == target_branch
                    and wt.get("worktree") != latest.path
                    for wt in worktrees
                )
            ):
                target_branch = None
            remove_entry(repo_root, entry, target_branch)
            if (
                os.path.lexists(latest.path)
                or any(wt.get("worktree") == latest.path for wt in git.list_worktrees(repo_root))
            ):
                return False

        return db.delete_pool_cleanup_reservation(latest.claim_token, "remove_pending")


def trim(db: DB, repo_root: str) -> int:
    """Drop ready entries beyond the repo's current ``pool_size`` (e.g. after
    it was lowered, or disabled). Returns how many were actually removed."""
    cfg = load_repo_config(repo_root)
    target = max(0, cfg.pool_size or 0)
    base = cfg.base_branch or git.default_branch(repo_root)
    entries = db.pool_entries(repo_root, base)
    excess = entries[: max(0, len(entries) - target)]
    removed = 0
    for e in excess:
        # A concurrent claim (take_pool_entry) may have taken this exact
        # entry between listing it above and deleting it here; only discard
        # the worktree when our delete actually removed the row -- otherwise
        # it's now a live workspace and must be left alone.
        if _discard_row(db, repo_root, e, cfg):
            removed += 1
    return removed
