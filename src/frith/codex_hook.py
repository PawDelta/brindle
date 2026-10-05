"""Codex's PermissionRequest hook for frith's permission policy, and the
one-time trust it needs.

Codex runs a hook it didn't get from managed config only once the person has
trusted it. Codex records that trust in its user ``config.toml`` as
``[hooks.state."<key>"] trusted_hash = "sha256:..."``, where the key is the
hook's source file, event and position (``<file>:permission_request:0:0``)
and the hash covers the hook's definition (its command, timeout...), not the
directory Codex runs in.

frith hands Codex its hook on the command line (``-c hooks.PermissionRequest
=...``, Codex's "session flags" layer), so nothing is added to a hooks file
and only frith's own Codex agents get it. The key is then always
``/<session-flags>/config.toml:permission_request:0:0`` and the command is the
same for every agent (the hook finds its agent from ``FRITH_AGENT_ID``, which
Codex passes through), so one trust covers every worktree frith ever creates.
A changed command (frith installed somewhere else, a different FRITH_HOME)
is a new hook to Codex.

frith trusts its own hook the first time a Codex agent needs it (``ensure``),
and again after it moves, so there's no setup step; it says so once at
``frith start``. A trust the person removes from Codex's config stays removed.

Trusting (``ensure``, or ``frith permissions install-codex-hook --yes``)
asks Codex itself (its app-server's ``hooks/list``) for the key and hash, writes the trust through
Codex's own config writer (``config/batchWrite``) and records what it trusted
in ``~/.frith/permissions.json``. Without it frith doesn't pass the hook at
all: an untrusted hook is skipped by ``codex exec`` and makes the interactive
CLI ask for a review at startup.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

EVENT = "codex-permission-request"
TIMEOUT = 30
SOURCE = "sessionFlags"


class CodexHookError(RuntimeError):
    pass


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _stable_invocation() -> list[str]:
    """frith_invocation, with the interpreter named the same however frith
    was started (a venv's ``python`` and ``python3`` are the same program,
    but a different name would be a different hook to Codex)."""
    from frith.providers import frith_invocation

    argv = frith_invocation()
    exe = argv[0]
    plain = os.path.join(os.path.dirname(exe), "python")
    try:
        if plain != exe and os.path.realpath(plain) == os.path.realpath(exe):
            argv = [plain, *argv[1:]]
    except OSError:
        pass
    return argv


def hook_command() -> str:
    """The hook's command: the same for every agent, so one trust covers all."""
    assigns = f"FRITH_HOME={shlex.quote(os.environ['FRITH_HOME'])} " if "FRITH_HOME" in os.environ else ""
    return assigns + " ".join(shlex.quote(a) for a in [*_stable_invocation(), "_hook", EVENT])


def config_flags(command: str | None = None) -> list[str]:
    """``-c`` arguments that give a Codex session frith's hook."""
    command = command or hook_command()
    hooks = f'[{{hooks=[{{type="command",command={json.dumps(command)},timeout={TIMEOUT}}}]}}]'
    return ["-c", f"hooks.PermissionRequest={hooks}"]


TRUSTED, MOVED, NEW, REMOVED = "trusted", "moved", "new", "removed"


def status(command: str | None = None) -> str:
    """Where frith's hook stands with Codex: TRUSTED (this exact command, and
    Codex's config still says so), MOVED (trusted, but frith's command has
    changed since: a reinstall, another FRITH_HOME), NEW (never trusted) or
    REMOVED (trusted once, then the trust was taken out of Codex's config)."""
    import tomllib

    from frith import permissions

    command = command or hook_command()
    rec = permissions.load_store().codex_hook
    if not rec.get("command") or not rec.get("key") or not rec.get("hash"):
        return NEW
    try:
        with open(codex_home() / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
    except OSError:
        cfg = {}
    except ValueError:
        return REMOVED  # can't tell; don't write into a config Codex can't read either
    state = cfg.get("hooks", {}).get("state", {}) if isinstance(cfg.get("hooks"), dict) else {}
    entry = state.get(rec["key"]) if isinstance(state, dict) else None
    if not (isinstance(entry, dict) and entry.get("trusted_hash") == rec["hash"]):
        return REMOVED
    return TRUSTED if rec["command"] == command else MOVED


def trusted(command: str | None = None) -> bool:
    """The person trusted this exact hook, and Codex's config still says so."""
    return status(command) == TRUSTED


def ensure(binary: str | None = None, command: str | None = None) -> str:
    """``status``, after trusting frith's hook when it's NEW or MOVED: frith
    trusts its own hook the first time it's needed, so Codex workers follow
    the permission policy without a setup step. A trust the person removed
    from Codex's config stays removed (``frith permissions install-codex-hook
    --yes`` gives it back). Never raises."""
    command = command or hook_command()
    now = status(command)
    if now not in (NEW, MOVED):
        return now
    from frith.providers import codex_binary

    binary = binary or codex_binary()
    try:
        trust(binary, inspect(binary, command))
    except Exception:  # noqa: BLE001 - Codex missing, too old, or not answering
        return now
    return TRUSTED


def policy_on(cwd: str | None) -> bool:
    from frith.config import load_repo_config

    try:
        return bool(cwd) and load_repo_config(cwd).permission_policy == "on"
    except Exception:  # noqa: BLE001
        return False


def launch_flags(cwd: str | None) -> list[str]:
    """The flags a Codex agent in ``cwd`` launches with: frith's hook when the
    repo's policy is on and the person trusted the hook; else none."""
    try:
        if not policy_on(cwd):
            return []
        command = hook_command()
        return config_flags(command) if ensure(command=command) == TRUSTED else []
    except Exception:  # noqa: BLE001 - never stop a launch over this
        return []


INSTALL = "frith permissions install-codex-hook --yes"


def launch_warning(provider: str, cwd: str | None) -> str | None:
    """Why a Codex worker in ``cwd`` runs without frith's permission policy
    although the repo turned it on, or None. Whoever started it only sees the
    launch's reply, and the worker would otherwise just stop on Codex's prompts."""
    if provider != "codex" or not policy_on(cwd):
        return None
    state = status()
    if state == TRUSTED:
        return None
    why = ("its trust was removed from Codex's config" if state == REMOVED
           else "Codex didn't accept it (is Codex too old for hooks?)")
    return ("permission_policy is on, but this Codex worker runs without frith's permission hook "
            f"({why}), so Codex asks for approvals itself. Ask the user to run `{INSTALL}`")


# -- asking Codex itself ---------------------------------------------------------------


class _AppServer:
    """A short-lived ``codex app-server`` over stdio (JSON-RPC, one per line)."""

    def __init__(self, binary: str, flags: list[str], timeout: float = 30.0):
        self.timeout = timeout
        try:
            self.proc = subprocess.Popen([binary, "app-server", *flags], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            raise CodexHookError(f"couldn't run {binary}: {e}") from e
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.next_id = 0
        self.call("initialize", {"clientInfo": {"name": "frith", "version": "0"}})
        self._send({"method": "initialized"})

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: dict) -> dict:
        self.next_id += 1
        self._send({"id": self.next_id, "method": method, "params": params})
        while True:
            try:
                line = self.lines.get(timeout=self.timeout)
            except queue.Empty as e:
                raise CodexHookError(f"codex app-server didn't answer {method}") from e
            if line is None:
                raise CodexHookError(f"codex app-server exited during {method}")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == self.next_id:
                if "error" in msg:
                    raise CodexHookError(f"codex {method}: {msg['error']}")
                return msg.get("result") or {}

    def close(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass


@dataclass
class HookState:
    command: str
    key: str
    hash: str
    status: str  # Codex's trustStatus: untrusted | trusted | modified | managed
    config: Path


def inspect(binary: str, command: str | None = None) -> HookState:
    """What Codex makes of frith's hook: its trust key, hash and status."""
    command = command or hook_command()
    server = _AppServer(binary, config_flags(command))
    try:
        result = server.call("hooks/list", {"cwds": [str(Path.home())]})
    finally:
        server.close()
    for entry in result.get("data") or []:
        for hook in entry.get("hooks") or []:
            if hook.get("source") == SOURCE and hook.get("command") == command:
                return HookState(command, str(hook["key"]), str(hook["currentHash"]),
                                 str(hook.get("trustStatus")), codex_home() / "config.toml")
    raise CodexHookError("Codex didn't list frith's hook (is this Codex too old for hooks?)")


def trust(binary: str, state: HookState) -> HookState:
    """Record the person's trust of the hook in Codex's config (through Codex's
    own writer), check Codex now sees it as trusted, and remember it."""
    from frith import permissions

    server = _AppServer(binary, config_flags(state.command))
    try:
        server.call("config/batchWrite", {"edits": [{
            "keyPath": "hooks.state", "mergeStrategy": "upsert",
            "value": {state.key: {"trusted_hash": state.hash}}}]})
    finally:
        server.close()
    after = inspect(binary, state.command)
    if after.status != "trusted":
        raise CodexHookError(f"Codex still reports the hook as {after.status}")
    with permissions.editing() as store:
        store.codex_hook = {"command": after.command, "key": after.key, "hash": after.hash}
    return after
