"""Giving disk space back.

brindle's worktrees, archive, backups and logs only ever grew. This module
takes back what is finished or has aged out, and nothing else:

- worktrees (``trees``, ``storage_report``). Each linked worktree of a repo
  brindle knows is *merged* (its branch is merged or squash-merged into its
  base or the default branch, see ``merged``), *abandoned* (unmerged, no
  commit or checkout in ``stale_branch_days``), or *active*. A merged one
  that brindle made, with no uncommitted changes and no live or paused
  session, is removed whole with its local branch, whose tip is kept under
  ``refs/brindle-archive/<branch>`` first (``remove_tree``). This happens
  hourly from the sweep, once on the first daemon start after the upgrade
  (``upgrade_sweep``) and on ``brindle clean``. An abandoned one is only
  listed, with its size, until someone cleans it (``clean``: one, or every
  abandoned one). A worktree with uncommitted changes is never removed by
  any path, only flagged;
- ``retain`` keeps the newest ``db_backups`` ``brindle.db.before-*`` copies,
  ages and caps ``archive/transcripts``, and rotates brindle's own top-level
  ``*.log`` files past ``log_mb`` (settings: ``retention`` in
  ~/.brindle/config.json, see ``Retention``). The database itself is never
  touched;
- ``low_disk`` holds a new worker or check run while free space is under the
  repo's ``min_free_disk_gb`` (see ``spawn.no_room``);
- local branches with no worktree: merged ones go with their workspace
  (``workspaces.remove``, tip archived); others nothing uses are only listed
  (``dead_branches``) until ``brindle clean --branches``;
- remote branches are left alone unless ``delete_pushed_branches`` is on
  (``delete_pushed_remote``).

``brindle doctor`` and ``brindle clean --dry-run`` show the report; the
daemon serves it at ``GET /v1/storage`` and cleans at ``POST /v1/storage/clean``."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from brindle import config, git

log = logging.getLogger(__name__)

GB = 1024 ** 3
MB = 1024 ** 2
DAY = 86400.0
# Folders of a worktree that rebuild themselves: reported apart in its size.
BUILD_DIRS = frozenset({"target", "node_modules", ".venv", "graphify-out", ".uv-cache"})
BACKUP_GLOB = "brindle.db.before-*"
ARCHIVE_REF = "refs/brindle-archive/"
POOL_PREFIX = "brindle-pool/"           # warm pool worktrees (brindle.pool): never classified
NEVER_INDEX = ".metadata_never_index"   # Spotlight skips a folder holding this file
RETAIN_EVERY = 3600.0                    # the automatic sweep runs at most this often
FRESH = 3600.0                           # a worktree touched this recently is never removed on its own
_RETAINED = ".retained"                  # the sweep's marker in brindle's home
UPGRADE_SWEPT = ".disk-swept"            # the one-time upgrade sweep has run

MERGED, ABANDONED, ACTIVE = "merged", "abandoned", "active"


# -- settings ---------------------------------------------------------------------------


@dataclass
class Retention:
    """``retention`` in ~/.brindle/config.json; 0 turns a cap off."""
    db_backups: int = 2             # brindle.db.before-* copies kept, newest first
    archive_days: float = 30.0      # archived transcripts older than this go
    archive_gb: float = 1.0         # and the oldest go while the archive is bigger
    log_mb: float = 10.0            # a log past this is rotated to <name>.1
    stale_branch_days: float = 14.0  # unmerged with no commit this long: abandoned


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


# -- worktrees --------------------------------------------------------------------------


def regenerable(path: str | Path) -> list[Path]:
    """The build dirs in worktree ``path``: folders named in ``BUILD_DIRS``
    that git ignores and that hold no tracked file, never a symlink."""
    proc = git.run(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
                   path, check=False)
    if proc.returncode != 0:
        return []
    found = []
    for entry in proc.stdout.split("\0"):
        p = Path(path) / entry.rstrip("/")
        if entry.endswith("/") and p.name in BUILD_DIRS and not p.is_symlink() and p.is_dir():
            found.append(p)
    return found


def _inside(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent + os.sep)


def live_in(db, path: str | Path) -> bool:
    """Whether a session is live in ``path``: a running agent of a workspace
    there, any tmux pane sitting in it, or this very process. Unknown counts
    as live."""
    from brindle import tmux, workspaces

    real = os.path.realpath(path)
    try:
        if _inside(os.path.realpath(os.getcwd()), real):
            return True
    except OSError:
        pass
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


@dataclass
class Tree:
    """One linked worktree in the storage report."""
    path: str
    repo_root: str
    branch: str
    state: str                  # MERGED, ABANDONED or ACTIVE
    last_activity: float        # its last commit, checkout or creation
    dirty: bool                 # uncommitted changes (untracked files count)
    live: bool                  # a live session or pane, or a paused session that can resume there
    owned: bool                 # brindle made it (a workspace row, or under brindle's worktree roots)
    size: int = 0               # everything under it, build dirs included
    build_size: int = 0         # of which build dirs (BUILD_DIRS)
    workspace_id: str | None = None

    @property
    def reason(self) -> str | None:
        """Why it can't be cleaned, or None."""
        if self.dirty:
            return "it has uncommitted changes"
        if self.live:
            return "a session is live or paused in it"
        if not self.owned:
            return "brindle didn't make it"
        if self.state == ACTIVE:
            return "it is still in use (unmerged, with recent activity)"
        return None

    def to_json(self) -> dict:
        return {**asdict(self), "removable": self.reason is None, "reason": self.reason}


def _gitdir(path: str) -> Path | None:
    try:
        text = (Path(path) / ".git").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text.removeprefix("gitdir:").strip()) if text.startswith("gitdir:") else None


def _last_activity(repo_root: str, path: str, branch: str, rows: list) -> float:
    """The newest of the branch's last commit, the worktree's last checkout
    or commit (its HEAD reflog) and its workspace rows' creation."""
    tip = git.run(["log", "-1", "--format=%ct", f"refs/heads/{branch}"], repo_root, check=False).stdout.strip()
    last = float(tip) if tip.isdigit() else 0.0
    last = max([last, *(w.created_at for w in rows)])
    gitdir = _gitdir(path)
    reflog = gitdir / "logs" / "HEAD" if gitdir else None
    if reflog is not None and reflog.is_file():
        last = max(last, reflog.stat().st_mtime)
    return last


def _owned(repo_root: str, path: str) -> bool:
    try:
        roots = [config.worktree_root(repo_root), config.worktrees_dir()]
    except ValueError:
        roots = [config.worktrees_dir()]
    real = os.path.realpath(path)
    return any(_inside(real, os.path.realpath(r)) for r in roots)


def trees(db, repo_root: str, now: float | None = None, *, sizes: bool = True,
          only: str | None = None) -> list[Tree]:
    """``repo_root``'s linked worktrees on a branch (not the pool's), classified;
    ``only``: just the one at that path. ``sizes``: measure them (slow)."""
    now = time.time() if now is None else now
    days = retention().stale_branch_days
    default = git.default_branch(repo_root)
    rows: dict[str, list] = {}
    for ws in db.find_workspaces(repo_root):
        rows.setdefault(os.path.realpath(ws.path), []).append(ws)
    out = []
    for wt in git.list_worktrees(repo_root)[1:]:   # the first is the main checkout
        path, ref = wt.get("worktree", ""), wt.get("branch", "")
        if not path or not ref.startswith("refs/heads/") or not os.path.isdir(path):
            continue
        if only is not None and os.path.realpath(path) != os.path.realpath(only):
            continue
        branch = ref.removeprefix("refs/heads/")
        here = rows.get(os.path.realpath(path), [])
        base = next((w.base_branch for w in here if w.base_branch), None) or default
        if branch.startswith(POOL_PREFIX) or branch in (base, default):
            continue
        last = _last_activity(repo_root, path, branch, here)
        recorded = any(db.merged_sha(w.id) for w in here)
        is_merged = (recorded or merged(repo_root, branch, base)
                     or (base != default and merged(repo_root, branch, default))
                     or any(merged_pr(db, w) for w in here))
        if is_merged and (recorded or now - last >= FRESH):
            state = MERGED   # (a branch made a moment ago looks merged too: it has nothing of its own yet)
        elif not is_merged and now - last >= days * DAY:
            state = ABANDONED
        else:
            state = ACTIVE
        try:
            dirty = bool(git.dirty_files(path))
        except git.GitError:
            dirty = True
        paused = any(a.status == "paused" for w in here for a in db.list_agents(w.id))
        t = Tree(path, repo_root, branch, state, last, dirty, paused or live_in(db, path),
                 bool(here) or _owned(repo_root, path), workspace_id=here[0].id if here else None)
        if sizes:
            t.size = size_of(path)
            t.build_size = sum(size_of(d) for d in regenerable(path))
        out.append(t)
    return out


def _repos(db, here: str | None) -> list[str]:
    """Every repo brindle knows: its workspaces', and any its history names
    (a repo whose workspace rows are all gone still has worktrees on disk)."""
    roots = {ws.repo_root for ws in db.find_workspaces()} | ({here} if here else set())
    roots |= {r[0] for r in db.conn.execute("SELECT DISTINCT repo_root FROM history")}
    return sorted(r for r in roots if r and os.path.isdir(os.path.join(r, ".git")))


def all_trees(db, here: str | None = None, now: float | None = None, *, sizes: bool = True) -> list[Tree]:
    """Every known repo's worktrees, biggest first."""
    out = []
    for root in _repos(db, here):
        try:
            out += trees(db, root, now, sizes=sizes)
        except git.GitError as e:
            log.warning("brindle: couldn't look at %s: %s", root, e)
    return sorted(out, key=lambda t: t.size, reverse=True)


class CantClean(ValueError):
    """A worktree that must stay (the message says why)."""


def remove_tree(db, path: str, *, states: tuple[str, ...] = (MERGED, ABANDONED),
                now: float | None = None) -> dict:
    """Remove the worktree at ``path`` whole, and its local branch, keeping the
    branch's tip under ``refs/brindle-archive/<branch>`` first. Its state is
    looked at again here, so a worktree that changed since a report was
    taken is kept: only one in ``states`` that ``Tree.reason`` lets go.
    Raises ``CantClean`` otherwise. What was removed, as for the route."""
    from brindle import workspaces

    try:
        root = git.main_repo_root(path)
        found = trees(db, root, now, only=path)
    except git.GitError as e:
        raise CantClean(f"{path} isn't a worktree: {e}") from e
    if not found:
        raise CantClean(f"{path} isn't a worktree brindle can clean (none on a branch there)")
    t = found[0]
    if t.reason:
        raise CantClean(f"kept {path}: {t.reason}")
    if t.state not in states:
        raise CantClean(f"kept {path}: it is {t.state}")
    sha = archive_tip(root, t.branch)
    for ws in db.find_workspaces(root):
        if os.path.realpath(ws.path) == os.path.realpath(path):
            try:
                workspaces.remove(db, ws, delete_branch=False)
            except workspaces.WorkspaceError as e:
                raise CantClean(f"kept {path}: {e}") from e
    if os.path.isdir(path):
        git.remove_worktree(root, path)   # never forced: git refuses a dirty one too
    git.run(["worktree", "prune"], root, check=False)
    if sha and git.branch_exists(root, t.branch):
        git.delete_branch(root, t.branch, force=True)
    return {"path": t.path, "repo_root": root, "branch": t.branch, "state": t.state, "size": t.size,
            "archived": f"{ARCHIVE_REF}{t.branch}" if sha else None}


def clean(db, *, path: str | None = None, abandoned: bool = False, here: str | None = None) -> dict:
    """The one-step cleanups the app and ``brindle clean`` offer: the
    worktree at ``path`` (merged or abandoned), or every abandoned one.
    ``{"removed": [...], "kept": [{"path", "reason"}]}``."""
    if path:
        targets = [path]
    elif abandoned:
        targets = [t.path for t in all_trees(db, here, sizes=False) if t.state == ABANDONED]
    else:
        raise CantClean("say which worktree (path) or all abandoned ones (abandoned)")
    removed, kept = [], []
    for p in targets:
        try:
            removed.append(remove_tree(db, p))
        except (CantClean, git.GitError) as e:
            kept.append({"path": p, "reason": str(e)})
    return {"removed": removed, "kept": kept}


def sweep_merged(db, now: float | None = None) -> list[dict]:
    """Remove every merged worktree that can go (``remove_tree``), quietly:
    what the hourly sweep and the upgrade sweep do."""
    now = time.time() if now is None else now
    done = []
    for t in all_trees(db, None, now, sizes=False):
        if t.state != MERGED or t.reason or now - t.last_activity < FRESH:
            continue   # (the pipeline removes what it merges itself: this is for what it left)
        try:
            done.append(remove_tree(db, t.path, states=(MERGED,), now=now))
            log.info("brindle: removed merged worktree %s (%s)", t.path, t.branch)
        except (CantClean, git.GitError) as e:
            log.info("brindle: %s", e)
    return done


def upgrade_sweep(db, now: float | None = None) -> list[dict] | None:
    """Once per machine (recorded in ``UPGRADE_SWEPT``), the first time the
    daemon starts after the upgrade that added this: every worktree whose
    branch merged before then goes (``sweep_merged``: squash merges too).
    None when it has run already. Never raises."""
    marker = config.brindle_home() / UPGRADE_SWEPT
    if marker.exists():
        return None
    done: list[dict] = []
    try:
        done = sweep_merged(db, now)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{time.time()}\n")
    except Exception:  # noqa: BLE001 - the daemon must start whatever happens here
        log.exception("brindle: the one-time disk sweep failed")
    return done


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


def now_and_then(db, now: float | None = None) -> None:
    """From the automatic sweep, at most every ``RETAIN_EVERY`` seconds:
    ``retain`` and ``sweep_merged``. Never raises."""
    now = time.time() if now is None else now
    marker = config.brindle_home() / _RETAINED
    try:
        if marker.exists() and now - marker.stat().st_mtime < RETAIN_EVERY:
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        retain()
        sweep_merged(db, now)
    except Exception:  # noqa: BLE001
        log.exception("brindle: the hourly disk cleanup failed")


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


# -- the storage report and `brindle clean` ---------------------------------------------


def storage_report(db, here: str | None = None, now: float | None = None) -> dict:
    """What the daemon's ``GET /v1/storage``, ``brindle doctor`` and ``brindle
    clean --dry-run`` show: every worktree (``Tree.to_json``) biggest first,
    brindle's disk use by category, free space, and dead branches."""
    from brindle import history

    items = all_trees(db, here, now)
    backups, logs, past = _backups(), _logs(), aged(now)
    categories = [
        {"name": "worktrees", "size": sum(t.size for t in items), "detail": f"{len(items)} worktree(s)"},
        {"name": "build dirs", "size": sum(t.build_size for t in items),
         "detail": "of the worktrees: " + ", ".join(sorted(BUILD_DIRS))},
        {"name": "archive", "size": size_of(history.archive_dir()), "detail": str(history.archive_dir())},
        {"name": "database backups", "size": sum(p.stat().st_size for p in backups),
         "detail": f"{len(backups)} {BACKUP_GLOB}"},
        {"name": "logs", "size": sum(p.stat().st_size for p in logs),
         "detail": f"{config.brindle_home()}/*.log"},
        {"name": "past retention", "size": sum(i.size for i in past), "detail": f"{len(past)} item(s)"},
    ]
    branches = []
    for root in _repos(db, here):
        try:
            branches += [{"repo_root": root, "name": b.name, "last_commit": b.last_commit, "merged": b.merged}
                         for b in dead_branches(db, root, now)]
        except git.GitError:
            continue
    try:
        free = free_bytes(here or config.brindle_home())
    except OSError:
        free = None
    return {"worktrees": [t.to_json() for t in items], "categories": categories,
            "reclaimable": sum(t.size for t in items if t.reason is None) + sum(i.size for i in past),
            "free_bytes": free, "low_disk": low_disk(here), "dead_branches": branches}


def report_lines(report: dict) -> list[str]:
    """``storage_report`` as lines for the terminal."""
    lines = [", ".join(f"{c['name']} {human(c['size'])}" for c in report["categories"])]
    for t in report["worktrees"]:
        when = time.strftime("%Y-%m-%d", time.localtime(t["last_activity"]))
        flag = f"; kept: {t['reason']}" if t["reason"] else ""
        lines.append(f"{t['state']:9} {human(t['size']):>8} (build {human(t['build_size'])}) {t['path']} "
                     f"[{t['branch']}, last activity {when}{flag}]")
    for b in report["dead_branches"]:
        when = time.strftime("%Y-%m-%d", time.localtime(b["last_commit"]))
        lines.append(f"branch {b['name']} in {b['repo_root']}: "
                     f"{'merged' if b['merged'] else 'unmerged, no worktree'}, last commit {when}")
    if report["low_disk"]:
        lines.append(report["low_disk"])
    return lines
