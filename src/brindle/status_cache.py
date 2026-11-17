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
there (each tracked file, from the index, every directory but those git
reported ignored, and every file that decides what is ignored), still
without git, and git runs only when that moved: an idle sidebar starts no
git at all. Its baseline is stat'ed just before each git look and then made
to leave out what that look reported ignored, so it always stands for what
git saw. ahead/behind is kept for as long as HEAD and the base ref are the
very commits it was counted for, and the upstream git reported is watched
as a ref. Where the index can't be read that way (see ``index_paths``) the
safety look is a ``git status``, as before.

The cache is process-lifetime, module-level state: correct for one
long-lived ``brindle watch``/``brindle ls`` process, not meant to be shared
across processes.
"""

from __future__ import annotations

import codecs
import os
import re
import shutil
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from stat import S_ISBLK, S_ISCHR
from typing import NamedTuple

from brindle import git, spawn

TTL = 300.0  # how often the safety look below the shallow worktree fingerprint runs
WARM_WORKERS = 8


class _Entry(NamedTuple):
    fp: tuple                 # _fingerprint after the status was computed
    refs: tuple               # _refs_key before it was (or, the upstream new, just after)
    checked_at: float         # the last look (git's or the safety look's)
    status: git.Status
    deep: int | None          # what git looked at (_deep_hash, stat'ed just before), None if unknown
    upstream: str | None      # the upstream git reported
    ignored: list[str]        # the ignored paths git reported: what ``deep`` leaves out


_base_ref_cache: dict[tuple[str, str], tuple[tuple, str]] = {}
_status_cache: dict[str, _Entry] = {}
# What _compute_status's git status said besides the Status: (upstream, ignored paths).
_reported: dict[str, tuple[str | None, list[str]]] = {}
# A worktree whose base ref names no commit (its base branch was deleted, say):
# the fingerprint and refs git failed under, and git's message. The failure is
# a function of the refs alone, so it stands until one of them moves.
_failed: dict[str, tuple[tuple, tuple, str]] = {}


def clear() -> None:
    """Drop all cached state. Mainly for tests."""
    _base_ref_cache.clear()
    _status_cache.clear()
    _reported.clear()
    _failed.clear()
    _sources.clear()
    _tracked_names.clear()
    _tracked_dirty.clear()
    _remotes.clear()


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


def _short_ref(wt_gitdir: Path, common: Path, short: str) -> tuple[str, str] | None:
    """(full ref, commit) of a ref name git printed in its short form (the
    upstream in ``git status``'s branch line: already mapped through the
    remote's refspec), tried in git's own order; None when none exists."""
    for full in (f"refs/{short}", f"refs/tags/{short}", f"refs/heads/{short}",
                 f"refs/remotes/{short}", f"refs/remotes/{short}/HEAD"):
        sha = _resolve(wt_gitdir, common, f"ref: {full}")
        if sha:
            return full, sha
    return None


def _refs_key(wt_gitdir: Path, common: Path, ref: str, upstream: str | None) -> tuple:
    """The commits HEAD, the base ``ref`` and the branch's ``upstream`` (as
    git last reported it) name, read from the git files, and the mtimes of
    the config files that say which upstream a branch has. ahead/behind is a
    function of the first two alone; the rest decides ``unpushed``."""
    try:
        head_value = (wt_gitdir / "HEAD").read_text().strip()
    except OSError:
        head_value = ""
    base = _resolve(wt_gitdir, common, "ref: " + str(_ref_file(Path(""), ref)))
    tracked = (upstream, _short_ref(wt_gitdir, common, upstream)) if upstream else None
    return (_resolve(wt_gitdir, common, head_value), base, tracked,
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


def _unquote(name: str) -> bytes:
    """A path as git status prints it (C-quoted when it has to be) as bytes."""
    if len(name) >= 2 and name[0] == name[-1] == '"':
        try:
            return codecs.escape_decode(name[1:-1].encode())[0]
        except ValueError:
            pass
    return os.fsencode(name)


def _stat(path: str | bytes | Path) -> tuple | None:
    try:
        s = os.lstat(path)
    except OSError:
        return None
    return (s.st_mtime_ns, s.st_ctime_ns, s.st_size, s.st_ino)


# What ``git status`` of tracked files alone reads, and what it said: per worktree.
# The index's paths and the folders they are in, per version of the index.
_tracked_names: dict[str, tuple[tuple, tuple[list[bytes], list[bytes]] | None]] = {}
_tracked_dirty: dict[str, tuple[tuple, list[str]]] = {}
_TRACKED_MAX = 256


def _tracked_folders(names: list[bytes]) -> list[bytes]:
    """Every folder a tracked path is in or under, the worktree's root (``b""``) first."""
    folders = {b""}
    for name in names:
        while (name := os.path.dirname(name)) and name not in folders:
            folders.add(name)
    return sorted(folders)


def _tracked_stamps(path: Path) -> tuple | None:
    """Everything the tracked files' status depends on: HEAD's commit, the
    index, every tracked file, each one stat'ed wherever it is (exact, unlike
    the shallow fingerprint: git itself judges a file modified by its stat),
    and what decides how git reads those files: a ``.gitattributes`` in any
    folder that holds one, there or not yet, tracked or not, and every config
    and attributes file outside the worktree (``_outside_sources``: includes,
    the person's and the system's among them). None when that can't be known
    without git (no commit yet, an index ``index_paths`` doesn't read, a
    config git couldn't list): then git is asked every time."""
    try:
        wt_gitdir = _worktree_gitdir(path)
        common = _common_gitdir(wt_gitdir)
        head = _resolve(wt_gitdir, common, (wt_gitdir / "HEAD").read_text().strip())
    except (OSError, ValueError, IndexError):
        return None
    index = _stat(wt_gitdir / "index")
    if head is None or index is None:
        return None
    key = str(wt_gitdir)
    names = _tracked_names.get(key)
    if names is None or names[0] != index:
        if len(_tracked_names) >= _TRACKED_MAX:
            _tracked_names.clear()
        listed = index_paths(wt_gitdir / "index", _hash_len(common))
        names = _tracked_names[key] = (index, None if listed is None else (listed, _tracked_folders(listed)))
    sources = None if names[1] is None else _outside_sources(path, wt_gitdir, common)
    if names[1] is None or sources is None:
        return None
    root = os.fsencode(path)
    return (head, index, tuple(_watched(p) for p in sources),
            tuple(_stat(os.path.join(root, folder, b".gitattributes")) for folder in names[1][1]),
            tuple(_stat(os.path.join(root, name)) for name in names[1][0]))


def tracked_dirty(path: str | Path) -> list[str]:
    """``git.dirty_files(path, tracked_only=True)``, asked of git only when
    something it reads has moved since it last looked (``_tracked_stamps``,
    taken before git looks, so a change made meanwhile asks again). For the
    daemon's sweeps, which judge a session's readiness again whenever one of
    its workers' rows changes: its own checkout is usually as it was."""
    path = Path(path)
    stamps = _tracked_stamps(path)
    hit = _tracked_dirty.get(str(path))
    if stamps is not None and hit is not None and hit[0] == stamps:
        return list(hit[1])
    dirty = git.dirty_files(path, tracked_only=True)
    if stamps is not None:
        if len(_tracked_dirty) >= _TRACKED_MAX:
            _tracked_dirty.clear()
        _tracked_dirty[str(path)] = (stamps, dirty)
    return list(dirty)


_SYSTEM_CONFIGS: list[Path] | None = None


def _system_configs() -> list[Path]:
    """Where git's system config may be: /etc, and beside the git on PATH
    (its prefix's etc/ or share/git-core/, Apple's command line tools'
    included). More than git reads, never fewer: a file that isn't there
    costs a stat."""
    global _SYSTEM_CONFIGS
    if _SYSTEM_CONFIGS is None:
        found = [Path("/etc/gitconfig"),
                 Path("/Library/Developer/CommandLineTools/usr/share/git-core/gitconfig"),
                 Path("/Applications/Xcode.app/Contents/Developer/usr/share/git-core/gitconfig")]
        binary = shutil.which("git")
        if binary:
            prefix = Path(os.path.realpath(binary)).parent.parent
            found += [prefix / "etc" / "gitconfig", prefix / "share" / "git-core" / "gitconfig"]
        _SYSTEM_CONFIGS = found
    return _SYSTEM_CONFIGS


def _as_watched(s: os.stat_result) -> tuple:
    """A device (``/dev/null`` named as the global config or an excludes
    file, say) reads the same whatever its times, which every process
    writing to it moves: only which device it is counts."""
    if S_ISCHR(s.st_mode) or S_ISBLK(s.st_mode):
        return (0, 0, 0, s.st_rdev)
    return (s.st_mtime_ns, s.st_ctime_ns, s.st_size, s.st_ino)


def _watched(path: Path) -> tuple:
    """A file outside the worktree as git reads it: the link itself and,
    through it, what it points at."""
    try:
        link = _as_watched(os.lstat(path))
    except OSError:
        return (None, None)
    try:
        return (link, _as_watched(os.stat(path)))
    except OSError:
        return (link, None)


def _settled_since(stats: tuple, before_ns: int) -> bool:
    return all(s is None or max(s[0], s[1]) < before_ns for pair in stats for s in pair)


# Per worktree: (what the list was read against, the list, git's config listing) for _outside_sources.
_sources: dict[str, tuple[tuple, list[Path], str]] = {}
_UNSETTLED = object()       # a list read while one of its files changed: never matches


def _outside_sources(path: Path, wt_gitdir: Path, common: Path) -> list[Path] | None:
    """Every file outside the worktree that can change how git reads it:
    what it ignores (info/exclude, the default global ignore file), how it
    reads a tracked file (info/attributes, the global and system attributes
    files), the config files git may read even when they don't exist yet
    (system, global, the repo's, the worktree's), and, as git itself reads
    the config (``git config --list --show-origin --includes``: its own
    parsing, escapes and includes included), every file it read, every
    include.path or includeIf path whatever its condition, and every
    core.excludesFile and core.attributesFile. git is asked again only when
    one of those files (or the branch, for onbranch: includes) changed: an
    idle sidebar asks nothing. None when git couldn't say (busy, or the
    command failed)."""
    key = str(path)
    try:
        head = (wt_gitdir / "HEAD").read_text()
    except OSError:
        head = ""
    hit = _sources.get(key)
    if hit is not None and hit[0] == (head, tuple(_watched(p) for p in hit[1])):
        return hit[1]
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git"
    found = [common / "info" / "exclude", xdg / "ignore", common / "info" / "attributes", xdg / "attributes",
             common / "config", wt_gitdir / "config.worktree"]
    if not os.environ.get("GIT_ATTR_NOSYSTEM"):
        found += [p.with_name("gitattributes") for p in _system_configs()]
    if not os.environ.get("GIT_CONFIG_NOSYSTEM"):
        system = os.environ.get("GIT_CONFIG_SYSTEM")
        found += [Path(system)] if system else _system_configs()
    glob = os.environ.get("GIT_CONFIG_GLOBAL")
    found += [Path(glob)] if glob else [Path.home() / ".gitconfig", xdg / "config"]
    before_ns = time.time_ns()
    try:
        proc = git.run(["config", "--list", "--show-origin", "--includes", "-z"], path, check=False)
    except git.GitError:       # busy, or a deadline: no answer
        proc = None
    if proc is None or proc.returncode != 0:
        # An incomplete list would be believed for good: there is none, and
        # the next look asks again (git status meanwhile, see _deep_entries).
        _sources.pop(key, None)
        return None
    fields = proc.stdout.split("\0")
    for origin, entry in zip(fields[0::2], fields[1::2]):
        source = None
        if origin.startswith("file:"):
            source = Path(origin[len("file:"):])
            source = source if source.is_absolute() else path / source
            found.append(source)
        name, _, value = entry.partition("\n")
        name = name.lower()
        if not value:
            continue
        if name in ("core.excludesfile", "core.attributesfile"):
            found.append(path / os.path.expanduser(value))       # an absolute value stays as it is
        elif name.endswith(".path") and name.startswith(("include.", "includeif.")):
            found.append((source.parent if source else path) / os.path.expanduser(value))
    found = list(dict.fromkeys(found))
    stats = tuple(_watched(p) for p in found)
    # The settings alone, without where each was read from: the same in every checkout that reads the same config.
    _sources[key] = ((head if _settled_since(stats, before_ns) else _UNSETTLED, stats), found,
                     "\0".join(fields[1::2]))
    return found


_remotes: dict[tuple, frozenset[str]] = {}


def remotes(path: str | Path) -> frozenset[str] | None:
    """The remotes ``git remote`` lists in the checkout at ``path``, asked of
    git once per repo and per config as git reads it there, includes and
    conditions resolved (``_outside_sources``: checkouts whose listing is the
    same share the answer), and again when any file that config depends on,
    or the ``remotes`` and ``branches`` folders of long ago, changed. None
    when that can't be known without asking: the caller asks git."""
    path = Path(path)
    try:
        wt_gitdir = _worktree_gitdir(path)
        common = _common_gitdir(wt_gitdir)
    except (OSError, ValueError, IndexError):
        return None

    def look() -> tuple | None:
        if _outside_sources(path, wt_gitdir, common) is None:
            return None
        return (str(common), _sources[str(path)][2], _stat(common / "remotes"), _stat(common / "branches"))

    key = look()
    if key is None:
        return None
    if key not in _remotes:
        names = frozenset(git.out(["remote"], path).split())
        if look() != key:
            return names        # the config moved while git read it: this answer is for neither
        if len(_remotes) >= _TRACKED_MAX:
            _remotes.clear()
        _remotes[key] = names
    return _remotes[key]


def _deep_entries(path: Path, wt_gitdir: Path, common: Path, ignored: list[str] | None) -> list | None:
    """What ``git status`` would look at, stat'ed without git: the
    ``_outside_sources``, every tracked file (from the index), and every
    directory (each ``.gitignore`` in it too) but nested repositories, which
    git doesn't enter, and the directories in ``ignored`` (git's last report;
    None: leave none out). A file new anywhere git looks changes its
    directory. None when the index can't be read (see ``index_paths``) or
    git couldn't say which files decide what is ignored: the safety look is
    a git look then, until it can."""
    paths = index_paths(wt_gitdir / "index", _hash_len(common))
    sources = None if paths is None else _outside_sources(path, wt_gitdir, common)
    if paths is None or sources is None:
        return None
    root = os.fsencode(path)
    out: list = [(0, str(p), _watched(p)) for p in sources]
    out += [(1, p, _stat(os.path.join(root, p))) for p in paths]
    skip = _ignored_dirs(root, ignored or [])
    for here, dirs, files in os.walk(root):
        out.append((2, here, _stat(here)))
        if here != root and (b".git" in dirs or b".git" in files):
            dirs[:] = []            # a repository of its own: git shows it whole, never what's inside
            continue
        dirs[:] = sorted(d for d in dirs if d != b".git" and os.path.join(here, d) not in skip)
        if b".gitignore" in files:
            out.append((3, here, _stat(os.path.join(here, b".gitignore"))))
    return out


def _ignored_dirs(root: bytes, ignored: list[str]) -> set[bytes]:
    return {os.path.join(root, _unquote(name).rstrip(b"/")) for name in ignored if name.endswith("/")}


def _deep_hash(path: Path, entries: list, ignored: list[str]) -> int:
    """``entries`` as a fingerprint, without what lies in the ``ignored``
    directories: equal to the fingerprint of a walk that left them out."""
    root = os.fsencode(path)
    skip = _ignored_dirs(root, ignored)

    def inside(p: bytes) -> bool:
        while len(p) > len(root):
            if p in skip:
                return True
            p = os.path.dirname(p)
        return False

    return hash(tuple(e for e in entries if e[0] < 2 or not (skip and inside(e[1]))))


def _deep_fingerprint(path: Path, wt_gitdir: Path, common: Path, ignored: list[str]) -> int | None:
    entries = _deep_entries(path, wt_gitdir, common, ignored)
    return None if entries is None else _deep_hash(path, entries, ignored)


def _settled(common: Path, upstream: str, before_ns: int) -> bool:
    """Whether ``upstream`` (a short name, see ``_short_ref``) has stayed as
    it was since ``before_ns``: neither its loose ref file (or, where there is
    none, the folder it would be in) nor packed-refs changed since."""
    candidates = [common / "packed-refs"]
    for full in (f"refs/{upstream}", f"refs/tags/{upstream}", f"refs/heads/{upstream}",
                 f"refs/remotes/{upstream}", f"refs/remotes/{upstream}/HEAD"):
        loose = common / full
        candidates.append(loose if loose.exists() else loose.parent)
    return all((s := _stat(c)) is None or max(s[0], s[1]) < before_ns for c in candidates)


def _parse_branch_header(line: str) -> tuple[str | None, bool, int, str | None]:
    """Parses ``git status --porcelain=v1 --branch``'s first line.
    Returns ``(branch, has_upstream, ahead_of_upstream, upstream)``."""
    body = line[3:]  # strip "## "
    if body.startswith("No commits yet on "):
        return body[len("No commits yet on "):], False, 0, None
    if body == "HEAD (no branch)":
        return None, False, 0, None
    branch, sep, rest = body.partition("...")
    if not sep:
        return branch, False, 0, None
    upstream, _, bracket = rest.partition(" [")
    bracket = bracket.rstrip("]")
    if not bracket or bracket == "gone":
        return branch, bracket != "gone", 0, upstream
    ahead = 0
    for part in bracket.split(", "):
        if part.startswith("ahead"):
            ahead = int(part.split()[1])
    return branch, True, ahead, upstream


def _compute_status(path: Path, base: str, ref: str,
                    ahead_behind: tuple[int, int] | None = None) -> git.Status:
    """``ahead_behind``: (behind, ahead) already counted for these very
    commits, so no ``rev-list`` runs. The upstream and the ignored paths git
    reported (an ignored directory as ``dir/``, never what's in it) go to
    ``_reported``, for the safety look."""
    proc = git.run(["status", "--porcelain=v1", "--branch", "--untracked-files=all",
                    "--ignored=matching"], path)
    lines = proc.stdout.splitlines()
    branch = upstream = None
    unpushed: int | None = None
    file_lines = lines
    if lines and lines[0].startswith("## "):
        branch, has_upstream, ahead_of_upstream, upstream = _parse_branch_header(lines[0])
        unpushed = ahead_of_upstream if has_upstream else None
        file_lines = lines[1:]
    dirty = [line[3:] for line in file_lines if line and not line.startswith("!! ")]
    ignored = [line[3:] for line in file_lines if line.startswith("!! ")]
    behind, ahead = git.ahead_behind(path, ref) if ahead_behind is None else ahead_behind
    _reported[str(path)] = (upstream, ignored)
    return git.Status(branch, base, ahead, behind, dirty, unpushed)


def _cached_status(path: str | Path, base: str | None, now: float | None = None) -> git.Status:
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
    key = str(path)
    hit = _status_cache.get(key)
    used = hit.upstream if hit is not None else None
    refs = _refs_key(wt_gitdir, common, ref, used)
    if hit is not None and hit.fp == fp and hit.refs == refs:
        if now - hit.checked_at < TTL:
            return hit.status
        # The safety look is due: below the root, by stat, before any git.
        if hit.deep is not None and _deep_fingerprint(path, wt_gitdir, common, hit.ignored) == hit.deep:
            _status_cache[key] = hit._replace(checked_at=now)
            return hit.status
    # What git is about to look at, stat'ed just before it does, so that a
    # change made while it looks shows next time. Only what the last look
    # found ignored is left out (a first look leaves out nothing); once git
    # has said what is ignored now, that is left out of the fingerprint too,
    # so it is what a safety look leaving out the same will find.
    entries = _deep_entries(path, wt_gitdir, common, hit.ignored if hit is not None else None)
    before_ns = time.time_ns()
    if hit is not None and refs[0] and refs[1] and hit.refs[:2] == refs[:2]:
        # HEAD and the base are the very commits counted before: no rev-list.
        st = _compute_status(path, base, ref, ahead_behind=(hit.status.behind, hit.status.ahead))
    else:
        st = _compute_status(path, base, ref)
    upstream, ignored = _reported.pop(key, (None, []))
    deep = None if entries is None else _deep_hash(path, entries, ignored)
    if upstream != used:
        # A new upstream (or the first look's): watched from now on, read just
        # after git's look. If it changed since git began, what git counted is
        # unknown: the next safety look asks git again.
        refs = _refs_key(wt_gitdir, common, ref, upstream)
        if upstream and not _settled(common, upstream, before_ns):
            deep = None
    # `git status` can rewrite the index (refreshing racily-clean stat data)
    # even when nothing changed, so re-fingerprint *after* computing: caching
    # the pre-call fingerprint would make every call look stale.
    try:
        fp = _fingerprint(path, wt_gitdir, common, ref)
    except OSError:
        return st
    _status_cache[key] = _Entry(fp, refs, now, st, deep, upstream, ignored)
    return st


def _failure_key(path: Path, base: str) -> tuple[str, tuple, tuple] | None:
    """(key, fingerprint, refs) of ``path`` as ``_cached_status`` sees them,
    or None when its git files can't be read. Starts no git while the base
    ref is cached."""
    try:
        wt_gitdir = _worktree_gitdir(path)
        common = _common_gitdir(wt_gitdir)
        ref = _cached_base_ref(path, base, common)
        fp = _fingerprint(path, wt_gitdir, common, ref)
    except OSError:
        return None
    hit = _status_cache.get(str(path))
    return str(path), fp, _refs_key(wt_gitdir, common, ref, hit.upstream if hit is not None else None)


def cached_status(path: str | Path, base: str | None, now: float | None = None) -> git.Status:
    """``_cached_status``, and a worktree whose base ref names no commit (its
    base branch deleted, say) fails again without git: git can't count
    against that base until a ref moves, and asking it on every refresh ran
    git status and a failing rev-list each time."""
    if not base:
        return _cached_status(path, base, now)
    path = Path(path)
    failed = _failed.get(str(path))
    if failed is not None and (seen := _failure_key(path, base)) is not None and failed[:2] == seen[1:]:
        raise git.GitError(failed[2])
    try:
        st = _cached_status(path, base, now)
    except git.GitError as e:
        _failed.pop(str(path), None)
        after = _failure_key(path, base)       # git status may have rewritten the index
        if after is not None and after[2][0] and not after[2][1]:
            # HEAD reads from the files and the base names nothing there.
            _failed[after[0]] = (after[1], after[2], str(e))
        raise
    _failed.pop(str(path), None)
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


def cached_any_status(path: str | Path, base: str | None, now: float | None = None) -> git.Status:
    """``cached_status`` for a checkout with no ``base`` too (the repo's own,
    where its coordinators run), which ``git.status`` would ask git about on
    every look: the daemon's events feed looks every 5 s. It is cached against
    its own branch (read from HEAD, no git) and reported, as ``git.status``
    does, with no base and nothing counted ahead or behind. A detached HEAD
    is asked of git."""
    if base:
        return cached_status(path, base, now=now)
    try:
        head = (_worktree_gitdir(Path(path)) / "HEAD").read_text().strip()
    except (OSError, ValueError, IndexError):
        head = ""
    if not head.startswith("ref: refs/heads/"):
        return git.status(path, base)
    st = cached_status(path, head[len("ref: refs/heads/"):], now=now)
    return git.Status(st.branch, None, 0, 0, st.dirty_files, st.unpushed)
