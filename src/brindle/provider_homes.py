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

import contextlib
import contextvars
import json
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


# Isolation never strands a working setup: until the person has signed in to
# a CLI inside brindle (its login run with brindle's home, then a status check
# with that home saying signed in), brindle runs the CLI with the person's own
# home, as before. OpenCode keeps its sign-in outside its config folder
# (~/.local/share/opencode), so its config is isolated from the start.
SIGNED_IN = ".brindle-signed-in"            # a status check with brindle's home said signed in
LOGIN_STARTED = ".brindle-login-started"    # brindle ran the CLI's login with brindle's home
SIGNED_OUT = ".brindle-signed-out"          # the person signed brindle's copy out; kept until the next login
CONFIRM_EVERY = 10.0
_confirmed_at: dict[str, float] = {}


def _confirm(provider: str, where: Path, *, force: bool = False) -> bool:
    """Ask the CLI, with brindle's home and no key from the environment,
    whether it is signed in there. Background home lookups are spaced by
    CONFIRM_EVERY; an explicit completion check can force a fresh answer."""
    from brindle import providers

    now = time.monotonic()
    if not force and now - _confirmed_at.get(provider, -CONFIRM_EVERY) < CONFIRM_EVERY:
        return False
    _confirmed_at[provider] = now
    env = {**{k: v for k, v in os.environ.items() if k not in providers._ENV_AUTH.get(provider, ())},
           VARIABLE[provider]: str(where)}
    if provider == "claude":
        res = providers._auth_probe([providers.claude_binary(), "auth", "status"], env)
        try:
            ok = bool(res) and json.loads(res[1]).get("loggedIn") is True
        except (ValueError, AttributeError):
            ok = False
    else:
        res = providers._auth_probe([providers.codex_binary(), "login", "status"], env)
        ok = bool(res) and res[0] == 0
    if ok:
        (where / SIGNED_IN).write_text(f"{time.time()}\n")
        (where / LOGIN_STARTED).unlink(missing_ok=True)
        (where / SIGNED_OUT).unlink(missing_ok=True)
    return ok


def confirm_login(provider: str) -> bool | None:
    """Check an unfinished login in the home it was opened in, now.

    A desktop completion check must not reuse the negative result from while
    the browser was open, or confirm a login in the person's other home.
    None means there is no pending home switch to confirm.
    """
    where = home(provider)
    if where is None or not (where / LOGIN_STARTED).exists():
        return None
    return _confirm(provider, where, force=True)


def isolated(provider: str) -> bool:
    """Whether brindle runs ``provider``'s CLI with brindle's own config home now."""
    where = home(provider)
    if where is None:
        return False
    if provider == "opencode" or (where / SIGNED_IN).exists():
        return True
    if (where / SIGNED_OUT).exists():        # signed out: stays brindle's home, a login in progress included
        signed_out(provider)
        return True
    return (where / LOGIN_STARTED).exists() and _confirm(provider, where)


def signed_out(provider: str) -> bool:
    """Whether brindle's copy of ``provider`` was signed out and not signed in since."""
    where = home(provider)
    if where is None or not (where / SIGNED_OUT).exists():
        return False
    if (where / LOGIN_STARTED).exists():     # a login ran since: signed in once the CLI says so
        _confirm(provider, where)
    return (where / SIGNED_OUT).exists()


def logout_env(provider: str) -> dict[str, str]:
    """The environment for the CLI's logout: always brindle's home, with no
    login marker written (unlike login_env). Empty for Antigravity."""
    where = home(provider)
    return {} if where is None else _prepared(provider, where)


def mark_signed_out(provider: str) -> None:
    """Record that brindle's copy is signed out: its home stays the one in use
    (never the person's own login), and reads say not signed in until a login."""
    where = home(provider)
    if where is None:
        return
    where.mkdir(parents=True, exist_ok=True, mode=0o700)
    (where / SIGNED_IN).unlink(missing_ok=True)
    (where / LOGIN_STARTED).unlink(missing_ok=True)
    (where / SIGNED_OUT).write_text(f"{time.time()}\n")
    _confirmed_at.pop(provider, None)


def effective_home(provider: str) -> Path | None:
    """The config folder ``provider``'s CLI runs with under brindle: brindle's
    own once it is in use, else the person's own (None for Antigravity)."""
    if home(provider) is None:
        return None
    return home(provider) if launch_env(provider) else source(provider)


def _prepared(provider: str, where: Path) -> dict[str, str]:
    try:
        ensure(provider)
    except OSError:      # never stop a launch over the copy; it is tried again next time
        log.warning("brindle: couldn't copy the setup for %s into %s", provider, where, exc_info=True)
    return {VARIABLE[provider]: str(where)}


# An agent's own home while brindle works on it (using()): its stored decision.
_agent_home: contextvars.ContextVar[dict[str, dict[str, str]]] = contextvars.ContextVar("provider_homes", default={})


def launch_env(provider: str) -> dict[str, str]:
    """What points ``provider``'s CLI at brindle's config home once it is in
    use (after the one-time copy of the person's files); empty while the CLI
    still runs with the person's own home, and for Antigravity. Inside
    using(), the agent's stored home instead: nothing is resolved again."""
    stored = _agent_home.get()
    if provider in stored:
        return dict(stored[provider])
    where = home(provider)
    return _prepared(provider, where) if where is not None and isolated(provider) else {}


def decide(provider: str) -> str | None:
    """The config home a new agent on ``provider`` runs with, decided once
    (at spawn) and stored on it: brindle's home's path, or "" for the
    person's own; None for a CLI with no config home of brindle's (Antigravity)."""
    if home(provider) is None:
        return None
    return launch_env(provider).get(VARIABLE[provider], "")


def env_for(provider: str, stored: str | None) -> dict[str, str]:
    """The environment an agent's stored decision (decide()) gives its CLI."""
    return {VARIABLE[provider]: stored} if stored and provider in VARIABLE else {}


@contextlib.contextmanager
def using(provider: str, stored: str | None):
    """Work on an agent whose home was decided as ``stored``: every home
    lookup inside (preflight, consents, trust, sign-in and identity checks)
    answers with it rather than resolving the home again."""
    token = _agent_home.set({**_agent_home.get(), provider: env_for(provider, stored)})
    try:
        yield
    finally:
        _agent_home.reset(token)


def login_env(provider: str) -> dict[str, str]:
    """The environment for the CLI's own login inside brindle: always
    brindle's home, which brindle starts using once a status check there says
    signed in. Empty for Antigravity."""
    where = home(provider)
    if where is None:
        return {}
    env = _prepared(provider, where)
    if provider != "opencode" and not (where / SIGNED_IN).exists():
        where.mkdir(parents=True, exist_ok=True, mode=0o700)
        (where / LOGIN_STARTED).write_text(f"{time.time()}\n")
        _confirmed_at.pop(provider, None)
    return env


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
