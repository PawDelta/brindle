"""brindle's own config home per provider CLI, so brindle's settings, hooks
and sign-in are kept apart from the person's own use of the same CLI.

Only each maker's documented variable is used: ``CLAUDE_CONFIG_DIR`` (Claude
Code), ``CODEX_HOME`` (Codex), ``OPENCODE_CONFIG_DIR`` (OpenCode). Each
points at ``~/Library/Application Support/brindle/config/<id>`` on macOS,
``$XDG_CONFIG_HOME/brindle/config/<id>`` (``~/.config``) elsewhere.
Antigravity documents no such variable, so it shares the person's
``~/.gemini``; brindle doesn't move it by any other means.

On first use brindle copies in, once, only the person's plain instruction
and Markdown files: CLAUDE.md or AGENTS.md, and the ``*.md`` files under
their agents and commands folders (Codex: prompts). Nothing else: no
settings.json, config.toml or MCP config, nothing with hooks, commands or
env, no credential or token file, no Keychain entry. brindle's homes start
from each CLI's defaults plus brindle's own flags, and the person signs in
through the CLI's own login inside brindle. The person's config folder is
resolved once (a dotfile manager may link ~/.claude itself); below it, a path
with a link anywhere (a folder, the file) is never copied, nor is a file or
folder named like a credential, and only regular files are read, without
following links. The Markdown is copied as it is, unscanned. After that one
copy, brindle's changes stay in brindle's copy."""

from __future__ import annotations

import logging
import os
import re
import stat
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

VARIABLE = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "opencode": "OPENCODE_CONFIG_DIR"}
MARKER = ".brindle-setup-copied"
# What is copied per CLI: its instruction file, and the folders whose *.md files come over.
FILES = {"claude": ("CLAUDE.md",), "codex": ("AGENTS.md",), "opencode": ("AGENTS.md",)}
FOLDERS = {"claude": ("agents", "commands"), "codex": ("prompts",),
           "opencode": ("agent", "agents", "command", "commands")}

_lock = threading.Lock()


def root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "brindle" / "config"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "brindle" / "config"


def home(provider: str) -> Path | None:
    """brindle's config home for ``provider``'s CLI; None for a CLI with no
    documented variable for one (Antigravity). ``BRINDLE_<ID>_CONFIG_HOME``
    overrides it (tests)."""
    if provider not in VARIABLE:
        return None
    override = os.environ.get(f"BRINDLE_{provider.upper()}_CONFIG_HOME")
    return Path(override) if override else root() / provider


def source(provider: str) -> Path:
    """Where the person's own setup for ``provider`` is."""
    if provider == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    if provider == "codex":
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"


def launch_env(provider: str) -> dict[str, str]:
    """The variable that points ``provider``'s CLI at brindle's config home,
    after the one-time copy of the person's files. Empty for Antigravity."""
    where = home(provider)
    if where is None:
        return {}
    try:
        ensure(provider)
    except OSError:      # never stop a launch over the copy; it is tried again next time
        log.warning("brindle: couldn't copy the setup for %s into %s", provider, where, exc_info=True)
    return {VARIABLE[provider]: str(where)}


# -- the copy --------------------------------------------------------------------


def _no_links(base: Path, rel: str) -> bool:
    """Whether ``base`` and every component of ``rel`` under it exist and none is a link."""
    path = base
    for part in ("", *Path(rel).parts):
        path = path / part if part else path
        try:
            if stat.S_ISLNK(os.lstat(path).st_mode):
                return False
        except OSError:
            return False
    return True


def _read(base: Path, rel: str) -> bytes | None:
    """``base/rel`` if no component of it is a link and it is a regular file."""
    if not _no_links(base, rel):
        return None
    try:
        fd = os.open(base / rel, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _copy(src: Path, dest: Path, rel: str) -> None:
    raw = _read(src, rel)
    target = dest / rel
    if raw is not None and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


# A file or folder named like a credential is never copied, whatever it holds.
SECRET_NAME = re.compile(r"(?i)(token|key|secret|passw|auth|credential|bearer|cookie|^\.env)")


def _markdown(src: Path, folder: str) -> list[str]:
    """The ``*.md`` files under ``src/folder``, relative to ``src``; links and
    credential-named files and folders are left out."""
    if not _no_links(src, folder) or not (src / folder).is_dir():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(src / folder, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if not os.path.islink(os.path.join(dirpath, d)) and not SECRET_NAME.search(d)]
        out += [os.path.relpath(os.path.join(dirpath, n), src) for n in filenames
                if n.endswith(".md") and not SECRET_NAME.search(n)]
    return sorted(out)


def ensure(provider: str) -> bool:
    """Create brindle's config home for ``provider`` and copy the person's
    instruction and Markdown files into it the first time. Whether it copied now."""
    where = home(provider)
    if where is None:
        return False
    # Resolved once: a dotfile manager may link ~/.claude itself. Below it, no link is followed.
    src = Path(os.path.realpath(source(provider)))
    with _lock:
        if (where / MARKER).exists():
            return False
        if os.path.realpath(src) == os.path.realpath(where):
            return False        # one and the same folder: nothing to copy
        where.mkdir(parents=True, exist_ok=True, mode=0o700)
        for rel in [*FILES[provider], *(f for d in FOLDERS[provider] for f in _markdown(src, d))]:
            _copy(src, where, rel)
        (where / MARKER).write_text(f"{time.time()}\n")
        return True
