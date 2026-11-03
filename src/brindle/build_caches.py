"""Build caches every worktree of a repo shares instead of building its own.

Each brindle worktree used to build its own copies of the same artifacts: a
Rust ``target/`` (3-4.5 GB), ``node_modules`` (220 MB), a ``.uv-cache`` and a
graphify graph. Workers, check runs and setup commands now get:

- ``CARGO_TARGET_DIR``: one target dir per repo, ``~/.brindle/cache/<repo>/
  cargo-target``. Cargo's own lock on the target dir serializes concurrent
  builds. ``"shared_cargo_target": false`` in a repo's config opts out.
- ``UV_CACHE_DIR``: uv's global cache (uv's cache is safe to share and
  locks itself).
- ``node_modules``: a setup command that installs a package's dependencies
  (``npm ci``, ``cd app && npm ci``, ...) is skipped when the base checkout's
  lockfile and ``package.json`` match the new worktree's: its
  ``node_modules`` is cloned copy-on-write (APFS ``cp -c``, macOS) instead.
  Any mismatch, another OS, or a failed clone runs the command as configured.

A value already in brindle's own environment (the person set
``CARGO_TARGET_DIR`` or ``UV_CACHE_DIR`` themselves) is kept.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from brindle import spawn

CACHE_DIR = "cache"
# The variables ``env`` sets. A worktree gets them and the repo's own checkout
# doesn't, so they are launch bookkeeping, not part of a worker's identity.
ENV_KEYS = frozenset({"CARGO_TARGET_DIR", "UV_CACHE_DIR"})

# Package manager -> its lockfiles. A worktree's node_modules is cloned only
# for these, and only when every one present matches the base checkout's.
_LOCKFILES = {
    "npm": ("package-lock.json", "npm-shrinkwrap.json"),
    "pnpm": ("pnpm-lock.yaml",),
    "yarn": ("yarn.lock",),
    "bun": ("bun.lockb", "bun.lock"),
}
# `[cd <dir> &&] <install> [--flags]`: a plain install of what the lockfile
# says. Anything else (a package name, a pipe, a second command) runs as is.
# Only flags in _SAFE_FLAGS: any other (--prefix, --omit, --ignore-scripts,
# ...) can change what is installed or where, so the command runs as is.
_INSTALL = re.compile(
    r"^\s*(?:cd\s+(?P<dir>[^\s;&|<>`$()]+)\s*&&\s*)?"
    r"(?P<pm>npm|pnpm|yarn|bun)(?:\s+(?P<sub>ci|install|i))?"
    r"(?P<flags>(?:\s+-[^\s;&|<>`$()]+)*)\s*$"
)

_SAFE_FLAGS = frozenset({
    "--no-audit", "--no-fund", "--no-progress", "--progress=false", "--no-update-notifier",
    "--silent", "--quiet", "-s", "-q", "--prefer-offline", "--offline",
    "--frozen-lockfile", "--pure-lockfile", "--immutable", "--non-interactive",
})


def repo_cache_dir(repo_root: str | Path) -> Path:
    """``~/.brindle/cache/<repo name>-<hash>``: the repo's shared caches. The
    hash of the repo's path keeps two repos with the same name apart."""
    from brindle.config import brindle_home

    root = os.path.realpath(repo_root)
    digest = hashlib.sha256(root.encode()).hexdigest()[:12]
    return brindle_home() / CACHE_DIR / f"{Path(root).name}-{digest}"


def cargo_target_dir(repo_root: str | Path) -> Path:
    return repo_cache_dir(repo_root) / "cargo-target"


def uv_cache_dir() -> str:
    """uv's global cache directory, as uv itself resolves it."""
    if os.environ.get("UV_CACHE_DIR"):
        return os.environ["UV_CACHE_DIR"]
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), "AppData", "Local")
        return os.path.join(base, "uv", "cache")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return os.path.join(base, "uv")


def env(repo_root: str | Path, cfg) -> dict[str, str]:
    """The shared-cache variables for a worktree of ``repo_root``."""
    out = {"UV_CACHE_DIR": uv_cache_dir()}
    if getattr(cfg, "shared_cargo_target", True):
        out["CARGO_TARGET_DIR"] = os.environ.get("CARGO_TARGET_DIR") or str(cargo_target_dir(repo_root))
    return out


def writable_dirs(repo_root: str | Path, cfg) -> list[str]:
    """Shared cache directories outside the worktree a sandboxed worker must
    be able to write."""
    return list(env(repo_root, cfg).values())


# -- node_modules ----------------------------------------------------------


def can_clone() -> bool:
    """Copy-on-write directory clones: APFS ``cp -c`` on macOS."""
    return sys.platform == "darwin"


def clone_tree(src: Path, dst: Path) -> bool:
    """Clone ``src`` to ``dst`` (which must not exist) copy-on-write."""
    proc = spawn.action(["cp", "-c", "-R", str(src), str(dst)])
    return proc.returncode == 0


def _install_dir(cmd: str, worktree: Path) -> tuple[Path, str] | None:
    """The package directory a plain install command installs into, and its
    package manager, or None if ``cmd`` is anything else."""
    m = _INSTALL.match(cmd)
    if not m:
        return None
    pm, sub = m.group("pm"), m.group("sub")
    if sub is None and pm != "yarn":   # bare `npm` / `pnpm` / `bun` isn't an install
        return None
    if sub == "ci" and pm != "npm":
        return None
    if not all(flag in _SAFE_FLAGS or flag.startswith("--loglevel=") for flag in m.group("flags").split()):
        return None
    rel = shlex.split(m.group("dir"))[0] if m.group("dir") else "."
    target = (worktree / rel).resolve()
    if not target.is_relative_to(worktree.resolve()):
        return None
    return target, pm


def _same(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _matches(src: Path, dst: Path, pm: str) -> bool:
    """``src``'s installed node_modules is what installing in ``dst`` would
    produce: package.json and every lockfile identical, at least one lockfile,
    and no workspaces (whose packages have node_modules of their own)."""
    if not (src / "node_modules").is_dir() or (dst / "node_modules").exists():
        return False
    if not _same(src / "package.json", dst / "package.json"):
        return False
    try:
        manifest = json.loads((dst / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict) or "workspaces" in manifest or (dst / "pnpm-workspace.yaml").exists():
        return False
    present = [name for name in _LOCKFILES[pm] if (dst / name).exists() or (src / name).exists()]
    return bool(present) and all(_same(src / name, dst / name) for name in present)


def clone_node_modules(cmd: str, worktree: str | Path, bases: list[str | Path]) -> str | None:
    """If ``cmd`` is a plain install whose package matches one of the ``bases``
    checkouts now, clone that checkout's node_modules into ``worktree`` in its
    place and return a log line; None means run ``cmd`` as configured. Called
    when setup reaches ``cmd``, so earlier commands' effects count."""
    if not can_clone():
        return None
    worktree = Path(worktree)
    found = _install_dir(cmd, worktree)
    if found is None:
        return None
    target, pm = found
    rel = target.relative_to(worktree.resolve())
    for base in bases:
        src = Path(base) / rel
        if src.resolve() == target or not _matches(src, target, pm):
            continue
        dst = target / "node_modules"
        if clone_tree(src / "node_modules", dst):
            return f"$ cp -c -R {src / 'node_modules'} {dst}  (lockfile unchanged; skipped: {cmd})"
        shutil.rmtree(dst, ignore_errors=True)
    return None
