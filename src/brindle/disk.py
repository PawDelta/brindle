"""Giving disk space back.

brindle's worktrees, archive, backups and logs only ever grew. This module
takes back what can be rebuilt or has aged out, and nothing else:

- a merged branch's worktree that stays on disk loses its regenerable build
  dirs (``BUILD_DIRS``: git-ignored, untracked, never a symlink), unless it
  has uncommitted work or a live session (``clean_worktree``);
- ``retain`` keeps the newest ``db_backups`` ``brindle.db.before-*`` copies,
  ages and caps ``archive/transcripts``, and rotates brindle's own top-level
  ``*.log`` files past ``log_mb`` (settings: ``retention`` in
  ~/.brindle/config.json, see ``Retention``). The database itself is never
  touched;
- ``low_disk`` holds a new worker or check run while free space is under the
  repo's ``min_free_disk_gb`` (see ``spawn.no_room``);
- local branches: merged ones (squash merges too, ``merged``) go with their
  workspace (``workspaces.remove``); unmerged ones nothing uses are only
  listed (``stale_branches``) until ``brindle clean --branches``, which keeps
  each tip under ``refs/brindle-archive/<branch>`` first;
- remote branches are left alone unless ``delete_pushed_branches`` is on
  (``delete_pushed_remote``).

``brindle doctor`` reports the space by category (``usage``) and ``brindle
clean`` reclaims it (``--dry-run`` lists it)."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field, fields
from pathlib import Path

from brindle import config, git

log = logging.getLogger(__name__)

GB = 1024 ** 3
MB = 1024 ** 2
DAY = 86400.0
BUILD_DIRS = frozenset({"target", "node_modules", ".venv", "graphify-out", ".uv-cache"})
BACKUP_GLOB = "brindle.db.before-*"
ARCHIVE_REF = "refs/brindle-archive/"
NEVER_INDEX = ".metadata_never_index"   # Spotlight skips a folder holding this file
RETAIN_EVERY = 3600.0                    # the automatic sweep runs retention at most this often
_RETAINED = ".retained"                  # its marker in brindle's home


# -- settings ---------------------------------------------------------------------------


@dataclass
class Retention:
    """``retention`` in ~/.brindle/config.json; 0 turns a cap off."""
    db_backups: int = 2             # brindle.db.before-* copies kept, newest first
    archive_days: float = 30.0      # archived transcripts older than this go
    archive_gb: float = 1.0         # and the oldest go while the archive is bigger
    log_mb: float = 10.0            # a log past this is rotated to <name>.1
    stale_branch_days: float = 14.0  # an unmerged branch with no commit this long is listed


def retention() -> Retention:
    r = Retention()
    try:
        raw = config.user_settings().get("retention")
    except ValueError:
        raw = None
    if isinstance(raw, dict):
        for f in fields(r):
            v = raw.get(f.name)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                setattr(r, f.name, type(getattr(r, f.name))(v))
    return r


# -- sizes ------------------------------------------------------------------------------


def size_of(path: str | Path) -> int:
    """Bytes under ``path``, symlinks not followed."""
    p = Path(path)
    try:
        if p.is_symlink() or p.is_file():
            return p.lstat().st_size
    except OSError:
        return 0
    total = 0
    stack = [p]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                else:
                    total += e.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def human(n: float) -> str:
    return f"{n / GB:.1f} GB" if n >= GB / 10 else f"{n / MB:.0f} MB"


# -- the free-disk guard ----------------------------------------------------------------


def free_bytes(path: str | Path) -> int:
    """Free space on the volume holding ``path`` (or its nearest existing parent)."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def low_disk(repo_root: str | None = None) -> str | None:
    """Why a new worker or check run should wait for disk ("waiting for disk:
    N GB free"), or None. Measured where the repo's worktrees go; the
    threshold is ``min_free_disk_gb`` (0: never wait)."""
    cfg = config.RepoConfig()
    where: Path = config.brindle_home()
    try:
        if repo_root is not None:
            cfg = config.load_repo_config(repo_root)
            where = config.worktree_root(repo_root, cfg)
        else:
            cfg.min_free_disk_gb = config.user_settings().get("min_free_disk_gb", cfg.min_free_disk_gb)
        need = float(cfg.min_free_disk_gb or 0)
    except (ValueError, TypeError):
        need = config.RepoConfig().min_free_disk_gb
    if need <= 0:
        return None
    try:
        free = free_bytes(where)
    except OSError:
        return None
    if free >= need * GB:
        return None
    return (f"waiting for disk: {free / GB:.1f} GB free (under {need:g} GB; "
            "`brindle clean` gives space back)")


# -- build dirs of finished worktrees ---------------------------------------------------


@dataclass
class Cleaned:
    path: str
    removed: list[str] = field(default_factory=list)   # relative to path
    freed: int = 0
    kept: str | None = None                            # why nothing was removed


def regenerable(path: str | Path) -> list[Path]:
    """The build dirs in worktree ``path`` brindle may delete: folders named
    in ``BUILD_DIRS`` that git ignores and that hold no tracked file (git
    lists only wholly untracked folders as such), never a symlink."""
    proc = git.run(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
                   path, check=False)
    if proc.returncode != 0:
        return []
    found = []
    for entry in proc.stdout.split("\0"):
        if not entry.endswith("/"):
            continue
        p = Path(path) / entry.rstrip("/")
        if p.name in BUILD_DIRS and not p.is_symlink() and p.is_dir():
            found.append(p)
    return found


def _inside(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent + os.sep)


def live_in(db, path: str | Path) -> bool:
    """Whether a session is live in ``path``: a running agent of a workspace
    there, or any tmux pane sitting in it. Unknown counts as live."""
    from brindle import tmux, workspaces

    real = os.path.realpath(path)
    for ws in db.find_workspaces():
        if _inside(os.path.realpath(ws.path), real) and workspaces.live_workers(db, ws):
            return True
    try:
        for name, _attached in tmux.list_sessions():
            if any(p and _inside(os.path.realpath(p), real) for _pid, p in tmux.session_panes(name)):
                return True
    except tmux.TmuxTimeout:
        return True
    return False


def clean_worktree(db, path: str | Path, *, dry_run: bool = False) -> Cleaned:
    """Delete linked worktree ``path``'s regenerable build dirs (``regenerable``).
    Never the person's own checkout, one with uncommitted changes (untracked
    files count), or one with a live session (``live_in``)."""
    c = Cleaned(str(path))
    p = Path(path)
    if not p.is_dir():
        return c
    if not (p / ".git").is_file():
        c.kept = "not a linked worktree"
        return c
    try:
        if git.dirty_files(p):
            c.kept = "it has uncommitted changes"
            return c
    except git.GitError as e:
        c.kept = f"git can't read it ({e})"
        return c
    if live_in(db, p):
        c.kept = "a session is live in it"
        return c
    for d in regenerable(p):
        size = size_of(d)
        if not dry_run:
            shutil.rmtree(d, ignore_errors=True)
            if d.exists():
                continue
        c.removed.append(str(d.relative_to(p)))
        c.freed += size
    return c


def after_merge(db, ws) -> Cleaned | None:
    """A merged branch's worktree that stays on disk loses its build dirs.
    Never raises: cleaning up must not fail a merge or a removal."""
    try:
        if ws.path != ws.repo_root and os.path.isdir(ws.path):
            return clean_worktree(db, ws.path)
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't clean the build dirs of %s", ws.path)
    return None


def finished_worktrees(db, repo_root: str) -> list[tuple[str, str]]:
    """``(path, branch)`` of ``repo_root``'s linked worktrees whose branch is
    merged into its base (or the default branch)."""
    known = {os.path.realpath(ws.path): ws for ws in db.find_workspaces(repo_root)}
    default = git.default_branch(repo_root)
    out = []
    for wt in git.list_worktrees(repo_root)[1:]:   # the first is the main checkout
        path, ref = wt.get("worktree", ""), wt.get("branch", "")
        if not path or not ref.startswith("refs/heads/") or not os.path.isdir(path):
            continue
        branch = ref.removeprefix("refs/heads/")
        ws = known.get(os.path.realpath(path))
        base = (ws.base_branch if ws else None) or default
        if branch != base and (ws and db.merged_sha(ws.id) or merged(repo_root, branch, base)):
            out.append((path, branch))
    return out


# -- branches ---------------------------------------------------------------------------


def merged(repo_root: str, branch: str, base: str) -> bool:
    """Whether every change on ``branch`` is in ``base``: an ancestor, or
    (a squash merge) merging it into ``base`` would change nothing, or
    ``git cherry`` finds each of its commits there."""
    if not base or branch == base:
        return False
    b, h = f"refs/heads/{branch}", f"refs/heads/{base}"
    if git.ok(["merge-base", "--is-ancestor", b, h], repo_root):
        return True
    proc = git.run(["merge-tree", "--write-tree", h, b], repo_root, check=False)
    if proc.returncode == 0:
        tree = proc.stdout.split("\n", 1)[0].strip()
        if tree and tree == git.run(["rev-parse", f"{h}^{{tree}}"], repo_root, check=False).stdout.strip():
            return True
    cherry = git.run(["cherry", h, b], repo_root, check=False).stdout.split("\n")
    lines = [line for line in cherry if line.strip()]
    return bool(lines) and all(line.startswith("-") for line in lines)


def merged_pr(db, ws) -> bool:
    """Whether ``ws``'s branch landed through a session PR session_review saw
    merge, with nothing committed on it since the push."""
    row = db.conn.execute("SELECT pushed_sha FROM session_prs WHERE branch = ? AND merged_at IS NOT NULL"
                          " AND pushed_sha IS NOT NULL", (ws.branch,)).fetchone()
    if row is None:
        return False
    tip = git.run(["rev-parse", f"refs/heads/{ws.branch}"], ws.repo_root, check=False).stdout.strip()
    return tip == row["pushed_sha"]


def archive_tip(repo_root: str, branch: str) -> str | None:
    """Keep ``branch``'s tip under ``refs/brindle-archive/<branch>`` (restore it
    with ``git branch <branch> refs/brindle-archive/<branch>``)."""
    sha = git.run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo_root,
                  check=False).stdout.strip()
    if sha:
        git.run(["update-ref", f"{ARCHIVE_REF}{branch}", sha], repo_root)
    return sha or None


@dataclass
class Branch:
    name: str
    sha: str
    last_commit: float
    merged: bool


def _in_use(db, repo_root: str) -> set[str]:
    used = {ws.branch for ws in db.find_workspaces(repo_root)}
    used |= {wt["branch"].removeprefix("refs/heads/") for wt in git.list_worktrees(repo_root)
             if wt.get("branch")}
    return used | {git.default_branch(repo_root)}


def dead_branches(db, repo_root: str, now: float | None = None) -> list[Branch]:
    """Local branches no workspace or worktree uses: merged ones (whatever
    their age), and unmerged ones with no commit in ``stale_branch_days``.
    brindle never deletes these on its own (``brindle clean --branches``)."""
    now = time.time() if now is None else now
    days = retention().stale_branch_days
    base = git.default_branch(repo_root)
    used = _in_use(db, repo_root)
    rows = git.out(["for-each-ref", "--format=%(refname:short)\t%(objectname)\t%(committerdate:unix)",
                    "refs/heads/"], repo_root)
    out = []
    for line in rows.splitlines():
        name, sha, when = (line.split("\t") + ["", ""])[:3]
        if not name or name in used:
            continue
        last = float(when or 0)
        is_merged = merged(repo_root, name, base)
        if is_merged or now - last >= days * DAY:
            out.append(Branch(name, sha, last, is_merged))
    return out


def delete_branch(repo_root: str, b: Branch) -> None:
    """Archive ``b``'s tip, then delete it."""
    git.run(["update-ref", f"{ARCHIVE_REF}{b.name}", b.sha], repo_root)
    git.delete_branch(repo_root, b.name, force=True)


def delete_pushed_remote(ws, pushed_sha: str | None) -> str | None:
    """With the repo's ``delete_pushed_branches`` on, delete the branch brindle
    pushed for a PR that merged from origin, but only while origin's tip is
    still the commit brindle pushed (``pushed_sha``). What it did, or None."""
    try:
        if not pushed_sha or not config.load_repo_config(ws.repo_root).delete_pushed_branches:
            return None
    except ValueError:
        return None
    proc = git.run(["ls-remote", "--heads", "origin", f"refs/heads/{ws.branch}"], ws.repo_root, check=False)
    tip = proc.stdout.split("\t", 1)[0].strip() if proc.returncode == 0 else ""
    if tip != pushed_sha:
        return None
    if git.run(["push", "origin", "--delete", ws.branch], ws.repo_root, check=False).returncode != 0:
        return None
    return f"deleted origin/{ws.branch} (its PR merged)"


# -- retention --------------------------------------------------------------------------


@dataclass
class Item:
    path: Path
    size: int
    why: str


def _backups() -> list[Path]:
    found = [p for p in config.brindle_home().glob(BACKUP_GLOB) if p.is_file()]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _logs() -> list[Path]:
    return sorted(p for p in config.brindle_home().glob("*.log") if p.is_file())


def _transcripts() -> list[Path]:
    from brindle import history

    root = history.archive_dir() / "transcripts"
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink()),
                  key=lambda p: p.stat().st_mtime)


def aged(now: float | None = None) -> list[Item]:
    """brindle's own files past their retention: old DB backups, archived
    transcripts past ``archive_days`` or beyond ``archive_gb`` (oldest
    first), and logs past ``log_mb`` (rotated, not deleted)."""
    from brindle import history

    now = time.time() if now is None else now
    r = retention()
    out = []
    if r.db_backups:
        out += [Item(p, p.stat().st_size, "old database backup") for p in _backups()[r.db_backups:]]
    if r.archive_days or r.archive_gb:
        total = size_of(history.archive_dir())
        for d in _transcripts():
            old = bool(r.archive_days) and now - d.stat().st_mtime > r.archive_days * DAY
            big = bool(r.archive_gb) and total > r.archive_gb * GB
            if not (old or big):
                continue
            size = size_of(d)
            total -= size
            out.append(Item(d, size, "archived transcripts past retention"))
    if r.log_mb:
        out += [Item(p, p.stat().st_size, "log to rotate") for p in _logs()
                if p.stat().st_size > r.log_mb * MB]
    return out


def rotate(log_file: Path) -> None:
    """Copy ``log_file`` to ``<name>.1`` (replacing the last one) and empty it
    in place, so a process still appending to it carries on in the same file."""
    shutil.copyfile(log_file, log_file.with_name(log_file.name + ".1"))
    with open(log_file, "r+b") as f:
        f.truncate(0)


def retain(dry_run: bool = False) -> list[Item]:
    """Apply retention (``aged``); returns what was (or would be) done."""
    items = aged()
    if dry_run:
        return items
    done = []
    for it in items:
        try:
            if it.why == "log to rotate":
                rotate(it.path)
            elif it.path.is_dir():
                shutil.rmtree(it.path)
            else:
                it.path.unlink()
            done.append(it)
        except OSError as e:
            log.warning("brindle: couldn't apply retention to %s: %s", it.path, e)
    return done


def retain_now_and_then(now: float | None = None) -> None:
    """``retain`` from the automatic sweep, at most every ``RETAIN_EVERY``
    seconds. Never raises."""
    now = time.time() if now is None else now
    marker = config.brindle_home() / _RETAINED
    try:
        if marker.exists() and now - marker.stat().st_mtime < RETAIN_EVERY:
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        retain()
    except Exception:  # noqa: BLE001
        log.exception("brindle: retention failed")


# -- Spotlight --------------------------------------------------------------------------


def cache_dirs() -> list[Path]:
    """brindle's cache folders (``BRINDLE_CACHE_DIR`` overrides them)."""
    override = os.environ.get("BRINDLE_CACHE_DIR")
    if override:
        return [Path(override)]
    caches = Path.home() / "Library" / "Caches"
    return [caches / "com.pawdelta.brindle", caches / "brindle-desktop"]


def keep_out_of_spotlight(*dirs: Path) -> None:
    """On macOS, a ``.metadata_never_index`` in each existing folder of ``dirs``
    so Spotlight doesn't index build churn. Idempotent; never raises."""
    if sys.platform != "darwin":
        return
    for d in dirs:
        try:
            if d.is_dir() and not (d / NEVER_INDEX).exists():
                (d / NEVER_INDEX).touch()
        except OSError:
            pass


# -- reporting and `brindle clean` ------------------------------------------------------


def _repos(db, here: str | None) -> list[str]:
    """Every repo brindle knows: its workspaces', and any its history names
    (a repo whose workspace rows are all gone still has worktrees on disk)."""
    roots = {ws.repo_root for ws in db.find_workspaces()} | ({here} if here else set())
    roots |= {r[0] for r in db.conn.execute("SELECT DISTINCT repo_root FROM history")}
    return sorted(r for r in roots if r and os.path.isdir(os.path.join(r, ".git")))


UPGRADE_SWEPT = ".disk-swept"   # in brindle's home: the one-time sweep below has run


def upgrade_sweep(db) -> list[Cleaned] | None:
    """Once per machine (recorded in ``UPGRADE_SWEPT``), the first time the
    daemon starts after the upgrade that added this: the build dirs of every
    worktree whose branch merged before then (``finished_worktrees``: squash
    merges too), under ``clean_worktree``'s rules. None when it has run
    already. Never raises."""
    marker = config.brindle_home() / UPGRADE_SWEPT
    if marker.exists():
        return None
    done: list[Cleaned] = []
    try:
        for root in _repos(db, None):
            try:
                for path, _branch in finished_worktrees(db, root):
                    c = clean_worktree(db, path)
                    if c.removed:
                        done.append(c)
                        log.info("brindle: freed %s of build dirs in %s", human(c.freed), path)
            except git.GitError as e:
                log.warning("brindle: couldn't sweep %s: %s", root, e)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{time.time()}\n")
    except Exception:  # noqa: BLE001 - the daemon must start whatever happens here
        log.exception("brindle: the one-time disk sweep failed")
    return done


@dataclass
class Plan:
    worktrees: list[Cleaned] = field(default_factory=list)
    aged: list[Item] = field(default_factory=list)
    branches: dict[str, list[Branch]] = field(default_factory=dict)


def plan(db, here: str | None, *, dry_run: bool) -> Plan:
    """What ``brindle clean`` removes (``dry_run``: would remove): the build
    dirs of finished worktrees in every known repo and retention. Branches
    are only listed; ``delete_branches`` deletes them."""
    p = Plan()
    for root in _repos(db, here):
        try:
            for path, _branch in finished_worktrees(db, root):
                c = clean_worktree(db, path, dry_run=dry_run)
                if c.removed:
                    p.worktrees.append(c)
            if not dry_run:
                git.run(["worktree", "prune"], root, check=False)   # stale registrations
            dead = dead_branches(db, root)
        except git.GitError as e:
            log.warning("brindle: couldn't look at %s: %s", root, e)
            continue
        if dead:
            p.branches[root] = dead
    p.aged = retain(dry_run=dry_run)
    return p


def render(p: Plan, *, dry_run: bool) -> list[str]:
    verb = "would remove" if dry_run else "removed"
    lines = []
    for c in p.worktrees:
        lines.append(f"{verb} {human(c.freed)} of build dirs in {c.path}: {', '.join(c.removed)}")
    for it in p.aged:
        action = ("would rotate" if dry_run else "rotated") if it.why == "log to rotate" else verb
        lines.append(f"{action} {it.path} ({human(it.size)}, {it.why})")
    for root, dead in p.branches.items():
        for b in dead:
            when = time.strftime("%Y-%m-%d", time.localtime(b.last_commit))
            state = "merged" if b.merged else "unmerged, no workspace"
            lines.append(f"branch {b.name} in {root}: {state}, last commit {when}")
    if p.branches:
        lines.append("`brindle clean --branches` deletes those branches, keeping each tip under "
                     f"{ARCHIVE_REF}<branch>")
    return lines or ["nothing to clean"]


@dataclass
class Usage:
    name: str
    size: int
    detail: str


def usage(db, here: str | None) -> list[Usage]:
    """brindle's disk use by category, with what ``brindle clean`` would reclaim."""
    from brindle import history

    home = config.brindle_home()
    out = []
    build = 0
    roots = _repos(db, here)
    trees = 0
    for root in roots:
        try:
            for path, _branch in finished_worktrees(db, root):
                trees += 1
                build += sum(size_of(d) for d in regenerable(path))
        except git.GitError:
            continue
    out.append(Usage("build dirs", build, f"in {trees} merged worktree(s)"))
    worktrees = 0
    for root in roots:
        try:
            worktrees += size_of(config.worktree_root(root))
        except ValueError:
            continue
    out.append(Usage("worktrees", worktrees, f"under {len(roots)} repo(s)' worktree roots"))
    out.append(Usage("archive", size_of(history.archive_dir()), str(history.archive_dir())))
    out.append(Usage("database backups", sum(p.stat().st_size for p in _backups()),
                     f"{len(_backups())} {BACKUP_GLOB}"))
    out.append(Usage("logs", sum(p.stat().st_size for p in _logs()), f"{home}/*.log"))
    aged_items = aged()
    out.append(Usage("past retention", sum(i.size for i in aged_items), f"{len(aged_items)} item(s)"))
    return out
