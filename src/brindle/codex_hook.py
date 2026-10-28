"""Codex's PermissionRequest hook for brindle's permission policy, and the
one-time trust it needs.

Codex runs a hook it didn't get from managed config only once the person has
trusted it. Codex records that trust in its user ``config.toml`` as
``[hooks.state."<key>"] trusted_hash = "sha256:..."``, where the key is the
hook's source file, event and position (``<file>:permission_request:0:0``)
and the hash covers the hook's definition (its command, timeout...), not the
directory Codex runs in.

brindle hands Codex its hook on the command line (``-c hooks.PermissionRequest
=...``, Codex's "session flags" layer), so nothing is added to a hooks file
and only brindle's own Codex agents get it. The key is then always
``/<session-flags>/config.toml:permission_request:0:0`` and the command is the
same for every agent (the hook finds its agent from ``BRINDLE_AGENT_ID``, which
Codex passes through), so one trust covers every worktree brindle ever creates.
A changed command (brindle installed somewhere else, a different BRINDLE_HOME)
or timeout (another build of brindle) is a new hook to Codex, so brindle
records the whole definition it trusted, not just the command.

brindle trusts its own hook the first time a Codex agent needs it (``ensure``),
and again after it moves, so there's no setup step; it says so once at
``brindle start``. A trust the person removes from Codex's config stays removed.

Trusting (``ensure``, or ``brindle permissions install-codex-hook --yes``)
asks Codex itself (its app-server's ``hooks/list``) for the key and hash, writes the trust through
Codex's own config writer (``config/batchWrite``) and records what it trusted
in ``~/.brindle/permissions.json``. All of that is per Codex config home (the
one brindle runs Codex with, ``codex_home``; brindle's own once it is in use,
see provider_homes): the app-server runs with that ``CODEX_HOME``, and the
record is kept under the home's resolved path, so a home brindle never trusted
its hook in is NEW there, whatever another home says. Without it brindle doesn't pass the hook at
all: an untrusted hook is skipped by ``codex exec`` and makes the interactive
CLI ask for a review at startup.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

EVENT = "codex-permission-request"
# Seconds Codex gives the hook: room for the default permission_hold_seconds
# (a day) plus agents.HOOK_SLACK. Codex caps only SessionEnd hooks' timeouts
# (codex-rs/hooks/src/engine/discovery.rs); others take any value. Fixed, not
# per repo: the trusted hash covers it, so a change is a new hook to Codex
# (status() reads it as MOVED, and ensure() trusts it again). A longer hold
# is cut to fit (agents).
TIMEOUT = 86400 + 30
SOURCE = "sessionFlags"


class CodexHookError(RuntimeError):
    pass


def codex_home() -> Path:
    from brindle import providers

    return Path(providers.codex_home())


def _home_key(home: Path) -> str:
    """The key a Codex home's trust record is kept under (permissions.Store.codex_hook)."""
    return os.path.realpath(home)


def _runnable_script(script: str) -> bool:
    if not os.access(script, os.X_OK):
        return False
    try:
        with open(script, "rb") as f:
            first = f.readline(4096)
    except OSError:
        return False
    if first.startswith(b"#!"):
        interpreter = first[2:].split(maxsplit=1)
        return bool(interpreter) and os.access(os.fsdecode(interpreter[0]), os.X_OK)
    return True


def _stable_invocation() -> list[str]:
    """brindle_invocation, with the interpreter named the same however brindle
    was started (a venv's ``python`` and ``python3`` are the same program,
    but a different name would be a different hook to Codex)."""
    from brindle.providers import brindle_invocation

    argv = brindle_invocation()
    exe = argv[0]
    # Codex keeps one trusted hash for the hook's slot, so the processes brindle
    # runs as (the installed ``brindle`` shim on PATH, the app's ``python -m
    # brindle``) must name the same install the same way, or each one's trust
    # replaces the other's and the next launch of either stops at "Hooks need review".
    script = os.path.join(os.path.dirname(exe), "brindle")
    if len(argv) == 1:
        if _runnable_script(exe):
            return [os.path.realpath(exe)]
        # Older bundled engines carry a console script with a CI-only shebang.
        exe = os.path.join(os.path.dirname(os.path.realpath(exe)), "python3")
        argv = [exe, "-P", "-m", "brindle"]
    if argv[1:] == ["-P", "-m", "brindle"] and _runnable_script(script):
        return [os.path.realpath(script)]
    plain = os.path.join(os.path.dirname(exe), "python")
    try:
        if plain != exe and os.path.realpath(plain) == os.path.realpath(exe):
            argv = [plain, *argv[1:]]
    except OSError:
        pass
    return argv


def hook_command() -> str:
    """The hook's command: the same for every agent, so one trust covers all."""
    assigns = f"BRINDLE_HOME={shlex.quote(os.environ['BRINDLE_HOME'])} " if "BRINDLE_HOME" in os.environ else ""
    return assigns + " ".join(shlex.quote(a) for a in [*_stable_invocation(), "_hook", EVENT])


def config_flags(command: str | None = None) -> list[str]:
    """``-c`` arguments that give a Codex session brindle's hook."""
    command = command or hook_command()
    hooks = f'[{{hooks=[{{type="command",command={json.dumps(command)},timeout={TIMEOUT}}}]}}]'
    return ["-c", f"hooks.PermissionRequest={hooks}"]


TRUSTED, MOVED, NEW, REMOVED = "trusted", "moved", "new", "removed"


def status(command: str | None = None) -> str:
    """Where brindle's hook stands with Codex: TRUSTED (this exact hook, and
    Codex's config still says so), MOVED (trusted, but brindle's hook has
    changed since: a reinstall, another BRINDLE_HOME, another timeout), NEW
    (never trusted) or REMOVED (trusted once, then the trust was taken out of
    Codex's config). All of it in the Codex home brindle runs Codex with now."""
    import tomllib

    from brindle import permissions

    command = command or hook_command()
    home = codex_home()
    rec = permissions.load_store().codex_hook.get(_home_key(home), {})
    if not rec.get("command") or not rec.get("key") or not rec.get("hash"):
        return NEW
    try:
        with open(home / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
    except OSError:
        cfg = {}
    except ValueError:
        return REMOVED  # can't tell; don't write into a config Codex can't read either
    state = cfg.get("hooks", {}).get("state", {}) if isinstance(cfg.get("hooks"), dict) else {}
    entry = state.get(rec["key"]) if isinstance(state, dict) else None
    if not (isinstance(entry, dict) and entry.get("trusted_hash") == rec["hash"]):
        return REMOVED
    # Codex's hash covers the whole definition, not just the command: a trust
    # recorded for another timeout (another build of brindle) is for another
    # hook, and so is one recorded before brindle kept the definition (it can't
    # tell what that was for), so ensure() trusts it again once.
    return TRUSTED if rec.get("definition") == config_flags(command)[1] else MOVED


def trusted(command: str | None = None) -> bool:
    """The person trusted this exact hook, and Codex's config still says so."""
    return status(command) == TRUSTED


def ensure(binary: str | None = None, command: str | None = None) -> str:
    """``status``, after trusting brindle's hook when it's NEW or MOVED: brindle
    trusts its own hook the first time it's needed, so Codex workers follow
    the permission policy without a setup step. A trust the person removed
    from Codex's config stays removed (``brindle permissions install-codex-hook
    --yes`` gives it back). Never raises."""
    from brindle.providers import codex_binary

    command = command or hook_command()
    now = status(command)
    if now not in (NEW, MOVED, TRUSTED):
        return now
    binary = binary or codex_binary()
    was_trusted = now == TRUSTED
    try:
        state = inspect(binary, command)
        if was_trusted and state.status == "trusted":
            return TRUSTED
        # brindle's record is only what it last saw. The Codex that runs now
        # (an upgrade, or a wrapper's own executable) may hash or key the
        # hook differently, which is "Hooks need review" at its startup: ask it.
        trust(binary, state)
    except Exception:  # noqa: BLE001 - Codex missing, too old, or not answering
        if not was_trusted:
            return now
        # Can't verify a trust brindle has on record: don't pass the hook on it,
        # and don't keep calling it trusted (the warning, the supervisor's
        # notice and the inbox read the record); the next launch asks again.
        _forget_definition()
        return MOVED
    return TRUSTED


def _forget_definition() -> None:
    from brindle import permissions

    with permissions.editing() as store:
        rec = store.codex_hook.get(_home_key(codex_home()))
        if rec:
            rec.pop("definition", None)


def policy_on(cwd: str | None) -> bool:
    from brindle.config import load_repo_config

    try:
        return bool(cwd) and load_repo_config(cwd).permission_policy == "on"
    except Exception:  # noqa: BLE001
        return False


def launch_flags(cwd: str | None, review: bool = False) -> list[str]:
    """The flags a Codex agent in ``cwd`` launches with: brindle's hook when the
    repo's policy is on and the person trusted the hook; else none. A reviewer
    gets it with the policy off too (nobody watches its prompts; the hook only
    allows what its allowed_tools cover, #241), but only a hook trusted
    already: with the policy off brindle never trusts a NEW one itself, and a
    REMOVED trust stays removed."""
    try:
        if not policy_on(cwd):
            if not review:
                return []
            command = hook_command()
            if status(command) not in (TRUSTED, MOVED):
                return []
        else:
            command = hook_command()
        return config_flags(command) if ensure(command=command) == TRUSTED else []
    except Exception:  # noqa: BLE001 - never stop a launch over this
        return []


INSTALL = "brindle permissions install-codex-hook --yes"


def launch_warning(provider: str, cwd: str | None) -> str | None:
    """Why a Codex worker in ``cwd`` runs without brindle's permission policy
    although the repo turned it on, or None. Whoever started it only sees the
    launch's reply, and the worker would otherwise just stop on Codex's prompts."""
    if provider != "codex" or not policy_on(cwd):
        return None
    state = status()
    if state == TRUSTED:
        return None
    why = ("its trust was removed from Codex's config" if state == REMOVED
           else "Codex didn't accept it (is Codex too old for hooks?)")
    return ("permission_policy is on, but this Codex worker runs without brindle's permission hook "
            f"({why}), so Codex asks for approvals itself. Ask the user to run `{INSTALL}`")


UNTRUSTED = ("brindle couldn't trust its own Codex permission hook (new, or changed by an upgrade), "
             "so Codex agents start without it and Codex asks for approvals itself. "
             f"Ask the user to run `{INSTALL}`.")


def tell_untrusted(db, agent, cwd: str | None) -> bool:
    """Tell ``agent``'s supervisor, once, that brindle couldn't trust its hook,
    so this Codex agent started without it: nobody may watch a reviewer's pane
    to find out otherwise. A trust the person removed stays theirs to give
    back (launch_warning, doctor), not news."""
    if agent.provider != "codex" or not agent.parent_id or not policy_on(cwd) \
            or status() not in (NEW, MOVED):
        return False
    if db.conn.execute("SELECT 1 FROM inbox WHERE agent_id=? AND instr(body, ?) > 0",
                       (agent.parent_id, UNTRUSTED)).fetchone():
        return False
    from brindle import agents

    try:
        agents.send_message(db, agent.parent_id, UNTRUSTED)
    except agents.AgentError:
        db.enqueue(agent.parent_id, UNTRUSTED, None)
    return True


# -- asking Codex itself ---------------------------------------------------------------


class _AppServer:
    """A short-lived ``codex app-server`` over stdio (JSON-RPC, one per line),
    on the Codex home brindle runs Codex with (``codex_home``): the trust it
    lists and writes is that home's."""

    def __init__(self, binary: str, flags: list[str], timeout: float = 30.0):
        self.timeout = timeout
        try:
            self.proc = subprocess.Popen([binary, "app-server", *flags], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                         start_new_session=True,
                                         env={**os.environ, "CODEX_HOME": str(codex_home())})
        except OSError as e:
            raise CodexHookError(f"couldn't run {binary}: {e}") from e
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.next_id = 0
        try:
            self.call("initialize", {"clientInfo": {"name": "brindle", "version": "0"}})
            self._send({"method": "initialized"})
        except Exception:
            self.close()
            raise

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
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                line = self.lines.get(timeout=remaining)
            except queue.Empty as e:
                raise CodexHookError(f"codex app-server didn't answer {method}") from e
            if line is None:
                raise CodexHookError(f"codex app-server exited during {method}")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == self.next_id:
                if "error" in msg:
                    raise CodexHookError(f"codex {method}: {msg['error']}")
                result = msg.get("result")
                if not isinstance(result, dict):
                    raise CodexHookError(f"codex {method}: invalid result")
                return result

    def close(self) -> None:
        # The app-server leads its own process group (see __init__): kill the
        # group so nothing it spawned outlives it.
        pid = getattr(self.proc, "pid", None)
        if pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


@dataclass
class HookState:
    command: str
    key: str
    hash: str
    status: str  # Codex's trustStatus: untrusted | trusted | modified | managed
    config: Path


def inspect(binary: str, command: str | None = None) -> HookState:
    """What Codex makes of brindle's hook: its trust key, hash and status."""
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
    raise CodexHookError("Codex didn't list brindle's hook (is this Codex too old for hooks?)")


def trust(binary: str, state: HookState) -> HookState:
    """Record the person's trust of the hook in Codex's config (through Codex's
    own writer), check Codex now sees it as trusted, and remember it."""
    from brindle import permissions

    server = _AppServer(binary, config_flags(state.command))
    try:
        server.call("config/batchWrite", {"edits": [{
            "keyPath": "hooks.state", "mergeStrategy": "upsert",
            "value": {state.key: {"trusted_hash": state.hash}}}]})
    finally:
        server.close()
    # Written but not yet verified: on record without its definition, which
    # status() reads as MOVED (verify again, and tell), never as a removal.
    home = _home_key(codex_home())
    with permissions.editing() as store:
        store.codex_hook[home] = {"command": state.command, "key": state.key, "hash": state.hash}
    after = inspect(binary, state.command)
    if after.status != "trusted":
        raise CodexHookError(f"Codex still reports the hook as {after.status}")
    with permissions.editing() as store:
        store.codex_hook[_home_key(after.config.parent)] = {
            "command": after.command, "key": after.key, "hash": after.hash,
            "definition": config_flags(after.command)[1]}
    return after
