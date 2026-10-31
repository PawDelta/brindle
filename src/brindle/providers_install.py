"""Provider CLIs as the person has them: brindle finds each one the way a
terminal would (PATH, then each maker's standard install locations), runs it
as it is, and leaves its updates to the CLI itself. On request it runs the
maker's official install command, exactly as the maker documents it, into
its default place, and looks again. brindle never uninstalls anything:
turning a provider off only stops brindle using it. (brindle's own config
home for each CLI is brindle.provider_homes'.)

What brindle keeps (``brindle_home()/providers.json``): which install to
run when several are found, and which providers are turned off."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from brindle import provider_homes, spawn

PROVIDERS = ("claude", "codex", "opencode", "antigravity")
NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "antigravity": "Antigravity"}
CLI = {"claude": "claude", "codex": "codex", "opencode": "opencode", "antigravity": "agy"}

# The makers' install commands, as documented (desktop/docs/provider-install.md).
OFFICIAL = {
    "claude": "curl -fsSL https://claude.ai/install.sh | bash",
    "codex": "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
    "opencode": "curl -fsSL https://opencode.ai/install | bash",
    "antigravity": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
}
# Where each installer puts the CLI by default, and other standard places
# (Homebrew; Claude Code's older local install; the ChatGPT app's Codex).
STANDARD = {
    "claude": ["~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude",
               "/usr/local/bin/claude"],
    "codex": ["~/.local/bin/codex", "/opt/homebrew/bin/codex", "/usr/local/bin/codex",
              "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"],
    "opencode": ["~/.opencode/bin/opencode", "/opt/homebrew/bin/opencode", "/usr/local/bin/opencode"],
    "antigravity": ["~/.local/bin/agy", "/opt/homebrew/bin/agy", "/usr/local/bin/agy"],
}

INSTALL_TIMEOUT = 900
VERSION_PROBE_TIMEOUT = 10


class InstallError(Exception):
    pass


def _check(provider: str) -> None:
    if provider not in PROVIDERS:
        raise InstallError(f"unknown provider {provider!r}: one of {', '.join(PROVIDERS)}")


# -- detect ----------------------------------------------------------------------


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def detect(provider: str) -> list[str]:
    """Every install of ``provider``'s CLI found here: PATH order first, then
    the makers' standard places; one entry per real file. Under WSL a Windows
    copy (under /mnt/) doesn't count."""
    from brindle import providers

    _check(provider)
    name = CLI[provider]
    first = providers.which_cli(name)       # what a terminal runs; any later PATH copies after it
    later = [os.path.join(d, name) for d in os.environ.get("PATH", "").split(os.pathsep) if d] if first else []
    candidates = [*([first] if first else []), *(p for p in later if _executable(p))]
    candidates += [p for p in (os.path.expanduser(s) for s in STANDARD[provider]) if _executable(p)]
    found, seen = [], set()
    wsl = providers.is_wsl()
    for path in candidates:
        if wsl and path.startswith("/mnt/"):
            continue
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            found.append(path)
    return found


_VERSIONS: dict[tuple[str, float], str | None] = {}


def probe_version(path: str) -> str | None:
    """``<cli> --version``'s version number, cached by the file's mtime."""
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key not in _VERSIONS:
        try:
            out = spawn.read([path, "--version"], timeout=VERSION_PROBE_TIMEOUT)
            m = re.search(r"\d+\.\d+\.\d+[\w.+-]*", out.stdout + out.stderr)
            _VERSIONS[key] = m.group(0) if m else None
        except (OSError, subprocess.TimeoutExpired):
            _VERSIONS[key] = None
    return _VERSIONS[key]


def signed_in(provider: str) -> bool | None:
    """Confirm the CLI's login, including one just finished in the desktop.

    Pending logins are checked in their destination home before accepting
    cached evidence from elsewhere. None means the CLI gave no answer.
    """
    from brindle import providers

    pending = provider_homes.confirm_login(provider)
    if pending is not None:
        return pending
    if provider_homes.signed_out(provider):      # the person signed brindle's copy out; a key still works, the row says not signed in
        return False
    if any(os.environ.get(k) for k in providers._ENV_AUTH.get(provider, ())):
        return True
    if providers.signed_out(provider) is not None:
        return False
    if providers.seen_signed_in(provider):
        return True
    if provider == "codex" and os.path.exists(providers.codex_auth_path()):
        return True
    return None


# -- what brindle keeps ----------------------------------------------------------

_lock = threading.RLock()


def _path() -> Path:
    from brindle.config import brindle_home

    return brindle_home() / "providers.json"


def _load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _set(provider: str, entry: dict[str, Any]) -> None:
    with _lock:
        data = _load()
        if entry:
            data[provider] = entry
        else:
            data.pop(provider, None)
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".providers-{os.getpid()}-{threading.get_ident()}.json")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)


def turned_off(provider: str) -> bool:
    return bool(_load().get(provider, {}).get("off"))


def selected(provider: str) -> str | None:
    """The install the person picked, while it is still there."""
    path = _load().get(provider, {}).get("path")
    return path if isinstance(path, str) and _executable(path) else None


def found(provider: str) -> str | None:
    """The install brindle runs: the one picked, else the first found."""
    return selected(provider) or next(iter(detect(provider)), None)


def use(provider: str, path: str) -> dict[str, Any]:
    """Run ``path``, one of the installs found, when there are several. Turns
    the provider back on."""
    _check(provider)
    chosen = next((p for p in detect(provider) if os.path.realpath(p) == os.path.realpath(path)), None)
    if chosen is None:
        raise InstallError(f"{path} isn't an install of {NAMES[provider]} brindle finds")
    _set(provider, {"path": chosen})
    return status(provider)


def turn_off(provider: str) -> dict[str, Any]:
    """Stop using ``provider`` in brindle. Nothing is uninstalled."""
    _check(provider)
    _set(provider, {"off": True})
    return status(provider)


SIGN_OUT = {"claude": ("auth", "logout"), "codex": ("logout",)}


def sign_out(provider: str) -> dict[str, Any]:
    """Sign brindle's own copy of ``provider`` out (``claude auth logout``,
    ``codex logout``), run with brindle's config home and never the person's
    own CLI login. The provider's row afterwards."""
    sign_out_only(provider)
    return status(provider)


def sign_out_only(provider: str) -> None:
    """What sign_out does, without reading the provider's row back."""
    from brindle import providers
    from brindle.company_login import run_cmd

    _check(provider)
    args = SIGN_OUT.get(provider)
    path = found(provider)
    if args is None or path is None:
        raise InstallError(f"can't sign {NAMES[provider]} out from brindle")
    where = provider_homes.home(provider)
    if where is None:
        raise InstallError(f"{NAMES[provider]} has no sign-in of brindle's own")
    code, out = run_cmd([path, *args], env={**os.environ, **provider_homes.logout_env(provider)})
    if code != 0:
        raise InstallError(f"{NAMES[provider]} sign-out failed: {out.strip()[-200:]}")
    provider_homes.mark_signed_out(provider)
    providers.invalidate_signed_in(provider)


def status(provider: str) -> dict[str, Any]:
    """One provider for GET /v1/providers/installed."""
    _check(provider)
    installs = detect(provider)
    active = found(provider)
    state = "off" if turned_off(provider) else "installed" if active else "missing"
    return {"provider": provider, "name": NAMES[provider], "state": state, "path": active,
            "version": probe_version(active) if active else None,
            "signed_in": signed_in(provider) if active else None,
            "selected": selected(provider) is not None,
            "installs": [{"path": p, "version": probe_version(p)} for p in installs],
            "config_home": str(provider_homes.home(provider)) if provider_homes.home(provider) else None}


def statuses() -> list[dict[str, Any]]:
    return [status(p) for p in PROVIDERS]


# -- install ---------------------------------------------------------------------


def install(provider: str, progress: Callable[[str], None]) -> dict[str, Any]:
    """Run the maker's install command, each line of its output to
    ``progress``, then find the CLI again. Turns the provider back on."""
    _check(provider)
    command = OFFICIAL[provider]
    progress(f"$ {command}")
    proc = spawn.start(["bash", "-c", command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    timer = threading.Timer(INSTALL_TIMEOUT, proc.kill)
    timer.start()
    tail: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                progress(line)
                tail = [*tail[-19:], line]
        code = proc.wait()
    finally:
        timer.cancel()
    if code != 0:
        raise InstallError(f"{NAMES[provider]}'s installer exited {code}: " + " / ".join(tail[-3:]))
    if not detect(provider):
        raise InstallError(f"{NAMES[provider]}'s installer finished, but brindle doesn't find {CLI[provider]}")
    if turned_off(provider):
        _set(provider, {})
    return status(provider)


_running: dict[str, Any] = {}
_running_lock = threading.Lock()


def claim(provider: str, job: Any) -> Any:
    """Register ``job`` as ``provider``'s install, or return the one already
    running (a second press gets that job back, never a second installer)."""
    with _running_lock:
        return _running.setdefault(provider, job)


def release(provider: str) -> None:
    with _running_lock:
        _running.pop(provider, None)


def running(provider: str) -> Any:
    with _running_lock:
        return _running.get(provider)
