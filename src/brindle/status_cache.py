"""A cheaper ``git.status`` for the dashboard and ``brindle ls``.

``git.status`` shells out several times per workspace (current branch, base
ref resolution, two rev-lists, dirty files). Redrawn every 2s across N
workspaces, that adds up. This module:

- caches ``base_ref`` per workspace behind a cheap fingerprint of the local
  ref, the origin ref and ``packed-refs`` (it rarely changes once a
  workspace exists, but a fetch or pull can flip which side it resolves to),
  and
- caches the rest of the status behind a cheap fingerprint (mtimes of the
  worktree's index, HEAD and logs/HEAD files, plus the base ref's
  mtime/sha and the worktree's top-level entries), so a workspace nothing
  has touched costs zero git subprocesses on a redraw. Only the root is
  listed: dependency directories are never walked by the fingerprint.

In-place edits below the root may leave all these timestamps untouched.
Every five minutes a safety look stats what ``git status`` would look at
there (each tracked file, from the index, and the directories holding them),
still without git, and git runs only when that moved: an idle sidebar starts
no git at all. ahead/behind is kept for as long as HEAD and the base ref are
the very commits it was counted for. Where the index can't be read that way
(see ``index_paths``) the safety look is a ``git status``, as before; and
every half hour (``BACKSTOP``) git looks regardless.

The cache is process-lifetime, module-level state: correct for one
long-lived ``brindle watch``/``brindle ls`` process, not meant to be shared
across processes.
"""

from __future__ import annotations

import os
import re
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from brindle import git, spawn

TTL = 300.0  # how often the safety look below the shallow worktree fingerprint runs
BACKSTOP = 1800.0  # how long a status the safety look keeps finding unchanged is believed
WARM_WORKERS = 8


class _Entry(NamedTuple):
    fp: tuple                 # _fingerprint after the status was computed
    refs: tuple               # _refs_key before it was
    checked_at: float         # the last look (git's or the safety look's)
    computed_at: float        # git's last look
    status: git.Status
    deep: int | None          # _deep_fingerprint taken before git's look, None if not taken
    hint: list[str]           # the dirty files that fingerprint watched the folders of


_base_ref_cache: dict[tuple[str, str], tuple[tuple, str]] = {}
_status_cache: dict[str, _Entry] = {}


def clear() -> None:
    """Drop all cached state. Mainly for tests."""
    _base_ref_cache.clear()
    _status_cache.clear()


def _worktree_gitdir(path: Path) -> Path:
    """The worktree's private gitdir: ``path/.git`` itself for a plain repo,
    or wherever its ``.git`` file points for a linked worktree. That pointer
    may be a relative path (git writes one, for example, when the worktree
    was created with a relative ``-C``), resolved against the worktree's own
    directory rather than the process's cwd."""
    dotgit = path / ".git"
    if dotgit.is_dir():
        return dotgit
    text = dotgit.read_text()
    raw = Path(text.split(":", 1)[1].strip())
    return raw if raw.is_absolute() else (path / raw).resolve()


def _common_gitdir(worktree_gitdir: Path) -> Path:
    """The shared gitdir (holds refs/objects) a linked worktree's private
    gitdir points back to, read from its ``commondir`` file. Avoids a
    ``git rev-parse --git-common-dir`` subprocess."""
    commondir_file = worktree_gitdir / "commondir"
    if not commondir_file.is_file():
        return worktree_gitdir
    rel = commondir_file.read_text().strip()
    return (worktree_gitdir / rel).resolve()


def read_head(path: str | Path) -> str | None:
    """Read the current commit for rendering without launching git.

    Follow loose or packed refs in the common gitdir of a linked worktree;
    detached HEADs live in its private gitdir. An unavailable or unsupported
    ref is unknown, never evidence of an old commit still being current.
    Merge gates continue to resolve HEAD through git itself.
    """
    try:
        private = _worktree_gitdir(Path(path))
        common = _common_gitdir(private)
        return _resolve(private, common, (private / "HEAD").read_text().strip())
    except (OSError, ValueError, IndexError):
        return None


def _resolve(private: Path, common: Path, value: str) -> str | None:
    """The commit ``value`` (a sha, or ``ref: refs/...``) names, read from the
    git files as ``read_head`` does; None when it can't be read that way."""
    try:
        seen = set()
        while value.startswith("ref: "):
            ref = value[5:]
            if ref in seen or not ref.startswith("refs/") or ".." in Path(ref).parts:
                return None
            seen.add(ref)
            directory = private if ref.startswith(("refs/bisect/", "refs/worktree/", "refs/rewritten/")) else common
            try:
                value = (directory / ref).read_text().strip()
            except FileNotFoundError:
                value = next((line.partition(" ")[0] for line in (common / "packed-refs").read_text().splitlines()
                              if line.partition(" ")[2] == ref), "")
        return value if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) else None
    except (OSError, ValueError, IndexError):
        return None


def _ref_file(common_gitdir: Path, ref: str) -> Path:
    if ref.startswith("origin/"):
        return common_gitdir / "refs" / "remotes" / ref
    return common_gitdir / "refs" / "heads" / ref


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _refs_fingerprint(common: Path, base: str) -> tuple[float, float, float]:
    """mtimes of the local ``base`` ref, its ``origin/<base>`` tracking ref,
    and ``packed-refs``: whichever of these changes, ``git.base_ref`` might
    now resolve differently (a fetch, a pull, or refs getting packed)."""
    local = _mtime(common / "refs" / "heads" / base)
    origin = _mtime(common / "refs" / "remotes" / "origin" / base)
    packed = _mtime(common / "packed-refs")
    return (local, origin, packed)


def _cached_base_ref(path: Path, base: str, common: Path) -> str:
    key = (str(path), base)
    fp = _refs_fingerprint(common, base)
    hit = _base_ref_cache.get(key)
    if hit is not None and hit[0] == fp:
        return hit[1]
    ref = git.base_ref(path, base)
    _base_ref_cache[key] = (fp, ref)
    return ref


def _worktree_fingerprint(path: Path) -> tuple:
    """Stat immediate children without descending into dependency trees.

    File metadata catches top-level edits; directory metadata catches new,
    removed or renamed children one level down. Sort by name so directory
    enumeration order cannot invalidate an otherwise unchanged workspace.
    Nanosecond timestamps and sizes also catch rapid successive edits.
    """
    entries = []
    with os.scandir(path) as children:
        for child in children:
            if child.name == ".git":
                continue
            stat = child.stat(follow_symlinks=False)
            entries.append((child.name, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
    return tuple(sorted(entries))


def _fingerprint(path: Path, wt_gitdir: Path, common: Path,
                 ref: str) -> tuple:
    index_mtime = _mtime(wt_gitdir / "index")
    head_mtime = _mtime(wt_gitdir / "HEAD")
    # Catches ref-moving operations (commit --amend, reset --soft, rebase)
    # that update the current branch's own ref file -- which this fingerprint
    # otherwise never looks at -- without touching the index, HEAD, or the
    # base ref: git appends to logs/HEAD any time HEAD's target commit moves.
    logs_head_mtime = _mtime(wt_gitdir / "logs" / "HEAD")
    ref_mtime = _mtime(_ref_file(common, ref))
    if ref_mtime == 0.0:
        ref_mtime = _mtime(common / "packed-refs")
    # The root catches top-level additions/removals, and the shallow entry
    # stats catch edits and immediate subdirectory changes. Deeper changes
    # still converge through the safety refresh, without recursive scanning.
    root_mtime = _mtime(path)
    entries = _worktree_fingerprint(path)
    return (index_mtime, head_mtime, logs_head_mtime, ref_mtime, root_mtime, entries)


def _refs_key(wt_gitdir: Path, common: Path, ref: str) -> tuple:
    """The commits HEAD, the base ``ref`` and ``origin/<branch>`` (the
    usual upstream) name, read from the git files, and the mtimes of the
    config files that say which upstream a branch has. ahead/behind is a
    function of the first two alone; the rest decides ``unpushed``."""
    try:
        head_value = (wt_gitdir / "HEAD").read_text().strip()
    except OSError:
        head_value = ""
    branch = head_value[len("ref: refs/heads/"):] if head_value.startswith("ref: refs/heads/") else None
    base = _resolve(wt_gitdir, common, "ref: " + str(_ref_file(Path(""), ref)))
    upstream = _resolve(wt_gitdir, common, f"ref: refs/remotes/origin/{branch}") if branch else None
    return (_resolve(wt_gitdir, common, head_value), base, upstream,
            _mtime(common / "config"), _mtime(wt_gitdir / "config.worktree"))


def _hash_len(common: Path) -> int:
    try:
        config = (common / "config").read_text()
    except OSError:
        return 20
    return 32 if re.search(r"(?im)^\s*objectformat\s*=\s*sha256\s*$", config) else 20


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    """git's offset varint (index v4's path prefix lengths)."""
    c = data[pos]
    pos += 1
    value = c & 127
    while c & 128:
        c = data[pos]
        pos += 1
        value = ((value + 1) << 7) | (c & 127)
    return value, pos


def index_paths(index: Path, hash_len: int = 20) -> list[bytes] | None:
    """The paths a git index (versions 2 to 4) tracks, read without git.
    None when this can't stand for what git status would look at: no index,
    one this doesn't read (another version, a split or sparse index), or one
    with a submodule (git looks inside it)."""
    try:
        data = index.read_bytes()
    except OSError:
        return None
    if len(data) < 12 + hash_len or data[:4] != b"DIRC":
        return None
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3, 4):
        return None
    pos, prev, paths = 12, b"", []
    try:
        for _ in range(count):
            start = pos
            mode = struct.unpack(">I", data[pos + 24:pos + 28])[0]
            pos += 40 + hash_len
            flags = struct.unpack(">H", data[pos:pos + 2])[0]
            pos += 2
            if version >= 3 and flags & 0x4000:
                pos += 2
            if version == 4:
                strip, pos = _varint(data, pos)
                end = data.index(b"\0", pos)
                path = prev[:len(prev) - strip] + data[pos:end]
                pos = end + 1
            else:
                end = data.index(b"\0", pos)
                path = data[pos:end]
                pos = start + ((end - start + 8) & ~7)
            if mode >> 12 not in (0o10, 0o12):      # a file or a symlink; not a gitlink or a sparse dir
                return None
            paths.append(path)
            prev = path
        while pos + 8 <= len(data) - hash_len:
            signature, size = data[pos:pos + 4], struct.unpack(">I", data[pos + 4:pos + 8])[0]
            if signature in (b"link", b"sdir"):
                return None
            pos += 8 + size
    except (struct.error, ValueError, IndexError):
        return None
    return paths


def _deep_fingerprint(path: Path, wt_gitdir: Path, common: Path, dirty: list[str]) -> int | None:
    """What ``git status`` would look at below the root, stat'ed without
    git: every tracked file, each directory holding one or a file git last
    reported, and the directories directly inside those (a file new in a
    directory git hasn't seen yet changes that directory). None when the
    index can't be read (see ``index_paths``)."""
    paths = index_paths(wt_gitdir / "index", _hash_len(common))
    if paths is None:
        return None
    root = os.fsencode(path)
    seen: list = []
    dirs = {b""}

    def holding(p: bytes) -> None:
        d = os.path.dirname(p)
        while d not in dirs:
            dirs.add(d)
            d = os.path.dirname(d)

    for p in paths:
        try:
            s = os.lstat(os.path.join(root, p))
            seen.append((s.st_mtime_ns, s.st_ctime_ns, s.st_size, s.st_ino))
        except OSError:
            seen.append(None)
        holding(p)
    for f in dirty:
        holding(os.fsencode(f.rpartition(" -> ")[2].strip('"')))
    for d in sorted(dirs):
        here = os.path.join(root, d) if d else root
        try:
            seen.append((d, os.stat(here).st_mtime_ns))
            with os.scandir(here) as children:
                seen.extend(sorted((c.name, c.stat(follow_symlinks=False).st_mtime_ns)
                                   for c in children if c.is_dir(follow_symlinks=False) and c.name != b".git"))
        except OSError:
            seen.append((d, None))
    return hash(tuple(seen))


def _parse_branch_header(line: str) -> tuple[str | None, bool, int]:
    """Parses ``git status --porcelain=v1 --branch``'s first line.
    Returns ``(branch, has_upstream, ahead_of_upstream)``."""
    body = line[3:]  # strip "## "
    if body.startswith("No commits yet on "):
        return body[len("No commits yet on "):], False, 0
    if body == "HEAD (no branch)":
        return None, False, 0
    branch, sep, rest = body.partition("...")
    if not sep:
        return branch, False, 0
    _upstream, _, bracket = rest.partition(" [")
    bracket = bracket.rstrip("]")
    if not bracket or bracket == "gone":
        return branch, bracket != "gone", 0
    ahead = 0
    for part in bracket.split(", "):
        if part.startswith("ahead"):
            ahead = int(part.split()[1])
    return branch, True, ahead


def _compute_status(path: Path, base: str, ref: str,
                    ahead_behind: tuple[int, int] | None = None) -> git.Status:
    """``ahead_behind``: (behind, ahead) already counted for these very
    commits, so no ``rev-list`` runs."""
    proc = git.run(["status", "--porcelain=v1", "--branch", "--untracked-files=all"], path)
    lines = proc.stdout.splitlines()
    branch = None
    unpushed: int | None = None
    file_lines = lines
    if lines and lines[0].startswith("## "):
        branch, has_upstream, ahead_of_upstream = _parse_branch_header(lines[0])
        unpushed = ahead_of_upstream if has_upstream else None
        file_lines = lines[1:]
    dirty = [line[3:] for line in file_lines if line]
    behind, ahead = git.ahead_behind(path, ref) if ahead_behind is None else ahead_behind
    return git.Status(branch, base, ahead, behind, dirty, unpushed)


def cached_status(path: str | Path, base: str | None, now: float | None = None) -> git.Status:
    """Like ``git.status``, but backed by the module-level cache above."""
    if not base:
        return git.status(path, base)
    path = Path(path)
    now = time.time() if now is None else now
    try:
        wt_gitdir = _worktree_gitdir(path)
        common = _common_gitdir(wt_gitdir)
    except OSError:
        return git.status(path, base)
    ref = _cached_base_ref(path, base, common)
    try:
        fp = _fingerprint(path, wt_gitdir, common, ref)
    except OSError:
        return _compute_status(path, base, ref)
    refs = _refs_key(wt_gitdir, common, ref)
    key = str(path)
    hit = _status_cache.get(key)
    hint: list[str] | None = None
    if hit is not None and hit.fp == fp and hit.refs == refs:
        if now - hit.checked_at < TTL:
            return hit.status
        # The safety look is due: below the root, by stat, before any git.
        if (hit.deep is not None and now - hit.computed_at < BACKSTOP
                and _deep_fingerprint(path, wt_gitdir, common, hit.hint) == hit.deep):
            _status_cache[key] = hit._replace(checked_at=now)
            return hit.status
        hint = hit.status.dirty_files
    elif hit is None:
        hint = []
    # A quiet worktree (a first look, or a safety look that found a change)
    # gets the deep baseline, taken before git looks so that a change made
    # while it looks shows next time; a busy one's waits until it is quiet.
    deep = None if hint is None else _deep_fingerprint(path, wt_gitdir, common, hint)
    if hit is not None and refs[0] and refs[1] and hit.refs[:2] == refs[:2]:
        # HEAD and the base are the very commits counted before: no rev-list.
        st = _compute_status(path, base, ref, ahead_behind=(hit.status.behind, hit.status.ahead))
    else:
        st = _compute_status(path, base, ref)
    # `git status` can rewrite the index (refreshing racily-clean stat data)
    # even when nothing changed, so re-fingerprint *after* computing: caching
    # the pre-call fingerprint would make every call look stale.
    try:
        fp = _fingerprint(path, wt_gitdir, common, ref)
    except OSError:
        return st
    _status_cache[key] = _Entry(fp, refs, now, now, st, deep, hint or [])
    return st


def warm(pairs: list[tuple[str, str | None]], now: float | None = None) -> None:
    """Bring the cached status of each (path, base) up to date at once, in
    parallel: a cold first look (the app opening) runs one `git status` per
    worktree, which done one after another took most of the time."""
    pairs = [(p, b) for p, b in pairs if b]
    if len(pairs) < 2:
        return

    def one(pair: tuple[str, str | None]) -> None:
        try:
            cached_status(pair[0], pair[1], now=now)
        except git.GitError:
            pass

    with ThreadPoolExecutor(max_workers=min(WARM_WORKERS, len(pairs))) as pool:
        list(pool.map(spawn.carry(one), pairs))   # under the request's deadline, if any
