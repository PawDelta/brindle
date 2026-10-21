"""brindle's own config home per provider CLI, so brindle's settings, hooks
and sign-in are kept apart from the person's own use of the same CLI.

Only each maker's documented variable is used: ``CLAUDE_CONFIG_DIR`` (Claude
Code), ``CODEX_HOME`` (Codex), ``OPENCODE_CONFIG_DIR`` (OpenCode). Each
points at ``~/Library/Application Support/brindle/config/<id>`` on macOS,
``$XDG_CONFIG_HOME/brindle/config/<id>`` (``~/.config``) elsewhere.
Antigravity documents no such variable, so it shares the person's
``~/.gemini``; brindle doesn't move it by any other means.

On first use brindle copies the person's NON-secret setup in once: settings,
CLAUDE.md or AGENTS.md, MCP config, agents and commands. Never a credential
or token file, never a Keychain entry: the person signs in through the CLI's
own login inside brindle. What is copied is vetted, failing closed:

- folders: only Markdown files, never through a link at any depth;
- JSON and TOML settings: parsed (unparsable files aren't copied), every key
  whose name says credential dropped, every value that looks like one
  (a token, a URL with a password or a key in its query, a ``--token`` flag
  and what follows it) dropped with its key; an MCP server entry holding one
  anywhere is dropped whole, since its credential can't be separated safely.

After that one copy, brindle's changes stay in brindle's copy."""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import stat
import sys
import threading
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VARIABLE = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "opencode": "OPENCODE_CONFIG_DIR"}
MARKER = ".brindle-setup-copied"

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
    after the one-time copy of the person's setup. Empty for Antigravity."""
    where = home(provider)
    if where is None:
        return {}
    try:
        ensure(provider)
    except OSError:      # never stop a launch over the copy; it is tried again next time
        log.warning("brindle: couldn't copy the setup for %s into %s", provider, where, exc_info=True)
    return {VARIABLE[provider]: str(where)}


# -- what counts as a credential -------------------------------------------------

SECRET_NAME = re.compile(r"(?i)(api[_-]?key|token|secret|passw(or)?d|passwd|credential|bearer|authorization|"
                         r"private[_-]?key|cookie|session[_-]?key|access[_-]?key|^auth$|^\.env)")
SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[abposr]-[\w-]{10,}"
    r"|glpat-[\w-]{16,}|AKIA[0-9A-Z]{16}|AIza[\w-]{30,}|eyJ[\w-]{8,}\.[\w-]{8,}\.|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}|^\s*Token\s+\S|\b(authorization|x-api-key|api-key)\s*:\s*\S)",
    re.IGNORECASE)
_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s/]*", re.IGNORECASE)
MCP_KEYS = {"mcpServers", "mcp_servers", "mcp"}


def _secret_string(s: str) -> bool:
    if SECRET_VALUE.search(s):
        return True
    if any("@" in m.group(0) and ":" in m.group(0).split("://", 1)[1] for m in _URL.finditer(s)):
        return True     # user:password@host
    if "?" in s and any(SECRET_NAME.search(k) for k in re.findall(r"[?&]([^=&#\s]+)=", s)):
        return True
    flag = re.match(r"^-{1,2}([A-Za-z0-9_.-]+)", s)
    if flag and SECRET_NAME.search(flag.group(1)):
        return True     # --token or --token=value: the flag, or the value after it
    assign = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", s)
    return bool(assign and SECRET_NAME.search(assign.group(1)))


def has_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(SECRET_NAME.search(str(k)) or has_secret(v) for k, v in value.items())
    if isinstance(value, list):
        return any(has_secret(v) for v in value)
    return isinstance(value, str) and _secret_string(value)


def scrub(value: dict) -> dict:
    """``value`` without anything that holds a credential (see the module doc)."""
    out = {}
    for k, v in value.items():
        if SECRET_NAME.search(str(k)):
            continue
        if k in MCP_KEYS and isinstance(v, dict):
            out[k] = {name: entry for name, entry in v.items() if not has_secret(entry)}
        elif isinstance(v, dict):
            out[k] = scrub(v)
        elif not has_secret(v):
            out[k] = v
    return out


def _secret_file(name: str) -> bool:
    return bool(SECRET_NAME.search(name)) or name in ("auth.json", ".credentials.json", "credentials.json")


# -- reading without following links ---------------------------------------------


def _read(path: Path) -> bytes | None:
    """The file's bytes, or None if it is missing, a link, or not a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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


# -- TOML out (Codex's config.toml) ------------------------------------------------


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"not a TOML value: {type(v).__name__}")


def toml_dumps(data: dict, prefix: tuple[str, ...] = ()) -> str:
    lines, tables, arrays = [], [], []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            arrays.append((k, v))
        else:
            lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
    for k, v in tables:
        path = (*prefix, k)
        lines += ["", f"[{'.'.join(map(_toml_key, path))}]", toml_dumps(v, path).rstrip("\n")]
    for k, items in arrays:
        path = (*prefix, k)
        for item in items:
            lines += ["", f"[[{'.'.join(map(_toml_key, path))}]]", toml_dumps(item, path).rstrip("\n")]
    return "\n".join(line for line in lines if line is not None).lstrip("\n") + "\n"


# -- the copy --------------------------------------------------------------------


def _merge(base: dict, ours: dict) -> dict:
    """``base`` (the person's) with ``ours`` (what brindle wrote already) winning."""
    out = dict(base)
    for k, v in ours.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _copy_settings(src: Path, dest: Path, load: Callable[[bytes], Any], dump: Callable[[dict], str],
                   pick: Callable[[dict], dict] = lambda d: d) -> None:
    raw = _read(src)
    if raw is None:
        return
    try:
        data = load(raw)
    except (ValueError, UnicodeDecodeError):
        return      # not vetted, not copied (JSON with comments, say)
    if not isinstance(data, dict):
        return
    mine = scrub(pick(data))
    try:
        ours = load(dest.read_bytes()) if dest.exists() else {}
    except (ValueError, UnicodeDecodeError, OSError):
        return      # brindle's own file there that won't parse: left as it is
    dest.write_text(dump(_merge(mine, ours if isinstance(ours, dict) else {})))


def _json_load(raw: bytes) -> Any:
    return json.loads(raw)


def _json_dump(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


def _toml_load(raw: bytes) -> Any:
    return tomllib.loads(raw.decode())


def _copy_file(src: Path, dest: Path) -> None:
    raw = _read(src)
    if raw is not None and not dest.exists():
        dest.write_bytes(raw)


def _copy_markdown(src: Path, dest: Path) -> None:
    """The Markdown files under ``src`` (agents, commands), never through a link."""
    if src.is_symlink() or not src.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)) and not _secret_file(d)]
        for name in filenames:
            if not name.endswith(".md") or _secret_file(name):
                continue
            raw = _read(Path(dirpath, name))
            target = dest / os.path.relpath(os.path.join(dirpath, name), src)
            if raw is not None and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)


def _items(provider: str) -> list[tuple[str, Callable[[Path, Path], None]]]:
    json_settings = lambda s, d: _copy_settings(s, d, _json_load, _json_dump)    # noqa: E731
    if provider == "claude":
        return [("settings.json", json_settings), ("CLAUDE.md", _copy_file),
                ("agents", _copy_markdown), ("commands", _copy_markdown)]
    if provider == "codex":
        return [("config.toml", lambda s, d: _copy_settings(s, d, _toml_load, toml_dumps)),
                ("AGENTS.md", _copy_file), ("prompts", _copy_markdown)]
    return [("opencode.json", json_settings), ("AGENTS.md", _copy_file),
            *((d, _copy_markdown) for d in ("agent", "agents", "command", "commands"))]


def ensure(provider: str) -> bool:
    """Create brindle's config home for ``provider`` and copy the person's
    setup into it the first time. Whether it copied now."""
    where = home(provider)
    if where is None:
        return False
    src = source(provider)
    with _lock:
        if (where / MARKER).exists():
            return False
        if os.path.realpath(src) == os.path.realpath(where):
            return False        # one and the same folder: nothing to copy
        where.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, copy in _items(provider):
            copy(src / name, where / name)
        if provider == "claude":
            # Claude Code keeps its MCP servers in its global state file, beside
            # the sign-in: only ``mcpServers`` comes over from there.
            state = (src / ".claude.json") if os.environ.get("CLAUDE_CONFIG_DIR") else Path.home() / ".claude.json"
            _copy_settings(state, where / ".claude.json", _json_load, _json_dump,
                           lambda d: {"mcpServers": d["mcpServers"]} if isinstance(d.get("mcpServers"), dict) else {})
        (where / MARKER).write_text(f"{time.time()}\n")
        return True
