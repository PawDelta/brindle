"""Codex's PermissionRequest and PreToolUse hooks for brindle's permission
policy, and the one-time trust they need.

PermissionRequest only runs when Codex would ask the person; Codex runs most
shell commands in its sandbox without asking (and the person's saved Codex
prefix rules run more), so brindle's denies (credentials, destructive git,
rm, the person's and org's own) are checked in PreToolUse, which runs before
every shell command, patch and MCP call (agents.codex_pre_tool_decision). The
two are passed and trusted together: one without the other is neither.

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
so there's no setup step; it says so once at ``brindle start``. Codex keeps one
trusted hash per key, so every brindle install on the machine shares that slot:
another install's trust (a terminal's ``brindle``, an app run from its disk
image) replaces this one's, as does an update that changes the hook, and a
config rewrite can lose it. None of that is for a person to deal with: the
daemon trusts the hook again by itself (``keep_trusted``), without waiting for
a Codex launch. Reading the config can't tell an entry a person deleted from
one a rewrite lost, so a missing entry is trusted again too. What a person can
do on purpose, and brindle can see, is turn the hook off in Codex
(``enabled = false``): that stays off (REMOVED) until they ask for it back.

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
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from brindle import spawn

EVENT = "codex-permission-request"
# Seconds Codex gives the hook: room for the default permission_hold_seconds
# (a day) plus agents.HOOK_SLACK. Codex caps only SessionEnd hooks' timeouts
# (codex-rs/hooks/src/engine/discovery.rs); others take any value. Fixed, not
# per repo: the trusted hash covers it, so a change is a new hook to Codex
# (status() reads it as MOVED, and ensure() trusts it again). A longer hold
# is cut to fit (agents).
TIMEOUT = 86400 + 30
# Codex runs most shell commands in its sandbox without asking, so the
# PermissionRequest hook never sees them; PreToolUse runs before every one
# (shell commands and exec_command match as Bash). It only denies, so it
# holds nothing for the person: a short timeout.
PRE_TOOL_EVENT = "codex-pre-tool"
# write_stdin too, should a Codex ever run PreToolUse for it: 0.162.1 doesn't,
# and asks for approval instead (its write_stdin_approval feature), where the
# PermissionRequest hook denies the input (permissions.codex_terminal_input).
# Users can't turn unified exec (resumable processes) off: Codex ignores it.
PRE_TOOL_MATCHER = "^(Bash|apply_patch|write_stdin|mcp__.*)$"
PRE_TOOL_SECONDS = 120
SOURCE = "sessionFlags"


class CodexHookError(RuntimeError):
    pass


class HookTurnedOff(CodexHookError):
    """A person turned the hook off in Codex, and nobody asked to turn it back on."""


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


def _launcher(argv: list[str] | None = None) -> list[str]:
    """A launcher at a fixed path (``<brindle home>/bin/codex-hook``) that runs
    the brindle that wrote it last. Codex keeps one trusted hash per hook and
    the hash covers the command, so every install on the machine (the app, one
    run from a disk image, a uv CLI) must name the same command: each one
    points the launcher at itself and none re-trusts over another. Without a
    writable launcher, the install's own invocation."""
    from brindle.config import brindle_home
    from brindle.providers import _config_lock

    argv = argv or _stable_invocation()
    launcher = brindle_home() / "bin" / "codex-hook"
    try:
        # Read, merge and replace as one step: two installs updating at once
        # would each keep only their own entry.
        with _config_lock("codex-hook-launcher.lock"):
            return _write_launcher(argv, launcher)
    except OSError:
        return argv


def _write_launcher(argv: list[str], launcher: Path) -> list[str]:
    # The installs it knows, newest first: at run time it execs the first that
    # still exists, so a removed one (an ejected disk image) never breaks the
    # others' running Codex sessions.
    known = [argv]
    old = ""
    try:
        old = launcher.read_text()
        for line in old.splitlines():
            if line.startswith("# install: "):
                other = shlex.split(line[len("# install: "):])
                if other and other != argv and os.access(other[0], os.X_OK):
                    known.append(other)
    except (OSError, ValueError):
        pass
    known = known[:4]
    lines = ["#!/bin/sh"]
    for a in known:
        q = " ".join(shlex.quote(x) for x in a)
        lines += [f"# install: {q}", f"[ -x {shlex.quote(a[0])} ] && exec {q} \"$@\""]
    body = "\n".join([*lines, 'echo "brindle is not installed any more" >&2', "exit 127"]) + "\n"
    tmp = None
    try:
        if old == body and os.access(launcher, os.X_OK):
            return [str(launcher)]
        launcher.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=launcher.parent, prefix=".codex-hook.")
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.chmod(tmp, 0o755)
        os.replace(tmp, launcher)
        tmp = None
        return [str(launcher)]
    except OSError:
        return argv
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def hook_command() -> str:
    """The hook's command: the same for every agent and every install, so one trust covers all."""
    assigns = f"BRINDLE_HOME={shlex.quote(os.environ['BRINDLE_HOME'])} " if "BRINDLE_HOME" in os.environ else ""
    return assigns + " ".join(shlex.quote(a) for a in [*_launcher(), "_hook", EVENT])


def pre_tool_command(command: str) -> str:
    """The PreToolUse hook's command beside the PermissionRequest ``command``."""
    return command.removesuffix(EVENT) + PRE_TOOL_EVENT


def config_flags(command: str | None = None) -> list[str]:
    """``-c`` arguments that give a Codex session brindle's hooks: PermissionRequest
    (what Codex would ask the person) and PreToolUse (every shell command, patch
    and MCP call, sandboxed or not: brindle's denies, before it runs)."""
    command = command or hook_command()
    hooks = f'[{{hooks=[{{type="command",command={json.dumps(command)},timeout={TIMEOUT}}}]}}]'
    pre = (f'[{{matcher={json.dumps(PRE_TOOL_MATCHER)},hooks=[{{type="command",'
           f'command={json.dumps(pre_tool_command(command))},timeout={PRE_TOOL_SECONDS}}}]}}]')
    return ["-c", f"hooks.PermissionRequest={hooks}", "-c", f"hooks.PreToolUse={pre}"]


def definition(command: str | None = None) -> str:
    """What Codex's trusted hashes cover, as brindle records it: both hooks' definitions."""
    return "\n".join(config_flags(command)[1::2])


TRUSTED, MOVED, NEW, REMOVED, UNREADABLE = "trusted", "moved", "new", "removed", "unreadable"


def _hook_entries(home: Path) -> dict:
    """``[hooks.state]`` of ``home``'s Codex config; {} without a config. A
    config that doesn't parse raises ValueError."""
    import tomllib

    try:
        with open(home / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
    except OSError:
        return {}
    hooks = cfg.get("hooks")
    state = hooks.get("state") if isinstance(hooks, dict) else None
    return state if isinstance(state, dict) else {}


def _turned_off(entry: object) -> bool:
    return isinstance(entry, dict) and entry.get("enabled") is False


# The keys Codex gives brindle's hooks (see above): a hook turned off there is
# off whatever brindle's own record says, or if it has none (a reset store,
# another BRINDLE_HOME sharing this Codex home).
SLOTS = ("/<session-flags>/config.toml:permission_request:0:0", "/<session-flags>/config.toml:pre_tool_use:0:0")


def status(command: str | None = None) -> str:
    """Where brindle's hook stands with Codex, in the Codex home brindle runs
    Codex with now: TRUSTED (this exact hook, and Codex's config still says so),
    NEW (never trusted), MOVED (trusted once, but not this hook any more: a
    reinstall, another build's definition, another install's trust in the slot,
    an entry a config rewrite lost), REMOVED (a person turned it off in Codex) or
    UNREADABLE (a config Codex can't read either, so nothing is written into it)."""
    from brindle import permissions

    command = command or hook_command()
    home = codex_home()
    rec = permissions.load_store().codex_hook.get(_home_key(home), {})
    try:
        state = _hook_entries(home)
    except ValueError:
        return UNREADABLE
    if any(_turned_off(state.get(key)) for key in (*SLOTS, rec.get("key"), rec.get("pre_key"))):
        return REMOVED
    if not rec.get("command") or not rec.get("key") or not rec.get("hash"):
        return NEW
    pairs = [(key, hash_) for key, hash_ in ((rec["key"], rec["hash"]), (rec.get("pre_key"), rec.get("pre_hash"))) if key]
    if not all(isinstance(state.get(key), dict) and state[key].get("trusted_hash") == hash_ for key, hash_ in pairs):
        return MOVED
    # Codex's hash covers the whole definition, not just the command: a trust
    # recorded for another timeout (another build of brindle) is for another
    # hook, and so is one recorded before brindle kept the definition (it can't
    # tell what that was for), or before it had a PreToolUse hook, so ensure()
    # trusts it again once.
    return TRUSTED if rec.get("definition") == definition(command) else MOVED


def trusted(command: str | None = None) -> bool:
    """The person trusted this exact hook, and Codex's config still says so."""
    return status(command) == TRUSTED


def ensure(binary: str | None = None, command: str | None = None) -> str:
    """``status``, after trusting brindle's hook when it's NEW or MOVED: brindle
    trusts its own hook the first time it's needed, so Codex workers follow
    the permission policy without a setup step. A hook the person turned off
    in Codex stays off (``brindle permissions install-codex-hook --yes`` turns
    it back on). Never raises."""
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
    except HookTurnedOff:
        return REMOVED
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


# How long a hook Codex wouldn't take is left before keep_trusted asks again:
# a Codex too old for hooks must not mean an app-server on every check.
AFTER_FAILURE = 60.0
_failed_at: dict[tuple[str, str, str], float] = {}


def keep_trusted(settle: float = 0.0) -> str:
    """Trust brindle's hook again, unasked, whenever it's MOVED, and when it's
    NEW with the policy on for this person (with it off, brindle has no use
    for the hook and adds nothing to Codex's config). Returns ``status``; never
    raises. The daemon calls this, so nobody waits for a Codex launch and the
    Inbox never asks. A trust another install just wrote (its config younger
    than ``settle`` seconds) is left a while: the Codex it was written for may
    still be starting, and replacing it now stops that Codex at "Hooks need review"."""
    from brindle.providers import codex_binary, which_cli

    now = NEW
    try:
        command = hook_command()
        now = status(command)
        # A throwaway BRINDLE_HOME that shares the person's Codex home (a
        # feature branch's CLI) has its own record: it must not fight over the slot.
        if "BRINDLE_HOME" in os.environ and "BRINDLE_CODEX_CONFIG_HOME" not in os.environ:
            return now
        if now not in (NEW, MOVED) or (now == NEW and not _policy_on_for_the_person()):
            return now
        binary = codex_binary()
        if not (which_cli(binary) or os.path.isfile(binary)):
            return now
        home = codex_home()
        if settle and time.time() - _config_changed_at(home) < settle:
            return now
        wanted = (_home_key(home), definition(command), binary)
        tried = _failed_at.get(wanted)
        if tried is not None and time.monotonic() - tried < AFTER_FAILURE:
            return now
        after = ensure(binary, command)
        if after == TRUSTED:
            _failed_at.pop(wanted, None)
        else:
            _failed_at[wanted] = time.monotonic()
        return after
    except Exception:  # noqa: BLE001 - upkeep; ensure() at the next launch still has its say
        return now


def _policy_on_for_the_person() -> bool:
    """The policy in the person's own settings, whatever a repo says (the
    daemon has no repo; a repo that alone turns it on gets it at its first launch)."""
    from brindle.config import brindle_home, load_repo_config

    return load_repo_config(brindle_home(), user_only=True).permission_policy == "on"


def _config_changed_at(home: Path) -> float:
    """When ``home``'s Codex config last changed (0 without one)."""
    try:
        return (home / "config.toml").stat().st_mtime
    except OSError:
        return 0.0


def policy_on(cwd: str | None) -> bool:
    from brindle.config import load_repo_config

    try:
        return bool(cwd) and load_repo_config(cwd).permission_policy == "on"
    except Exception:  # noqa: BLE001
        return False


def policy_chosen(cwd: str) -> bool:
    """Whether someone set ``cwd``'s policy, rather than it being on by default.
    If that can't be read, as if they did: it only ever keeps a refusal."""
    from brindle.config import load_repo_config

    try:
        return load_repo_config(cwd).permission_policy_source != "default"
    except Exception:  # noqa: BLE001
        return True


# The folders whose Codex agents this process launched without the policy,
# which was only on by default, and why (providers._codex_sandbox).
_skipped: dict[str, Exception] = {}
POLICY_SKIPPED = "brindle's permission rules aren't applied to Codex here, so Codex's own approvals and sandbox decide"


def skip_policy(cwd: str | None, why: Exception | None) -> None:
    """Record whether the Codex agent being launched in ``cwd`` skips the policy."""
    if why is None:
        _skipped.pop(cwd, None)
    else:
        _skipped[cwd] = why


def _tell_once(db, agent, marker: str, text: str) -> bool:
    """``text`` to whoever watches ``agent``, unless they already have one with
    ``marker``: its supervisor, or a supervisor itself, which the person is
    talking to (it's only starting: delivered once it's ready)."""
    told = agent.parent_id or agent.id
    if db.conn.execute("SELECT 1 FROM inbox WHERE agent_id=? AND instr(body, ?) > 0",
                       (told, marker)).fetchone():
        return False
    if not agent.parent_id:
        db.enqueue(told, text, None)
        return True
    from brindle import agents

    try:
        agents.send_message(db, told, text)
    except agents.AgentError:
        db.enqueue(told, text, None)
    return True


def tell_skipped(db, agent, cwd: str | None) -> bool:
    """Tell whoever watches ``agent`` why brindle's rules don't cover it, once."""
    why = _skipped.get(cwd) if agent.provider == "codex" else None
    if why is None:
        return False
    return _tell_once(db, agent, POLICY_SKIPPED, f"{POLICY_SKIPPED}: {why.problem}. {why.fix} to have them cover Codex.")


def launch_flags(cwd: str | None, review: bool = False, *, required: bool = False) -> list[str]:
    """Document supervisors cannot launch without verified permission hooks.

    With policy off, use only an already trusted hook, as for reviewers;
    never turn a hook back on against the person's choice.
    """
    flags = _optional_launch_flags(cwd, review=review or required)
    if required and not flags:
        raise CodexHookError("the read-only document supervisor requires a trusted Codex permission hook")
    return flags


def _optional_launch_flags(cwd: str | None, review: bool = False) -> list[str]:
    """The flags a Codex agent in ``cwd`` launches with: brindle's hook when the
    repo's policy is on and the person trusted the hook; else none. A reviewer
    gets it with the policy off too (nobody watches its prompts; the hook only
    allows what its allowed_tools cover, #241), but only a hook trusted
    already: with the policy off brindle never trusts a NEW one itself, and a
    hook the person turned off stays off. None at all for an agent skipping
    the policy: the hook would answer from rules its sandbox doesn't back."""
    try:
        if cwd in _skipped:
            return []
        if not policy_on(cwd):
            if not review:
                return []
            command = hook_command()
            if status(command) not in (TRUSTED, MOVED):
                return []
        else:
            command = hook_command()
        if ensure(command=command) != TRUSTED:
            return []
        return config_flags(command)
    except Exception:  # noqa: BLE001 - never stop a launch over this
        return []


INSTALL = "brindle permissions install-codex-hook --yes"


def launch_warning(provider: str, cwd: str | None) -> str | None:
    """Why a Codex worker in ``cwd`` runs without brindle's permission policy
    although the repo turned it on, or None. Whoever started it only sees the
    launch's reply, and the worker would otherwise just stop on Codex's prompts."""
    if provider != "codex" or cwd in _skipped or not policy_on(cwd):   # a skip has its own notice: tell_skipped
        return None
    state = status()
    if state == TRUSTED:
        return None   # a config Codex would ignore brindle's sandbox profile under is refused or skipped at launch
    why = {REMOVED: "it's turned off in Codex",
           UNREADABLE: "Codex's config.toml doesn't parse"}.get(
        state, "Codex didn't accept it (is Codex too old for hooks?)")
    return ("permission_policy is on, but this Codex worker runs without brindle's permission hook "
            f"({why}), so Codex asks for approvals itself. Ask the user to run `{INSTALL}`")


UNTRUSTED = ("brindle couldn't trust its own Codex permission hook (new, or changed by an upgrade), "
             "so Codex agents start without it and Codex asks for approvals itself. "
             f"Ask the user to run `{INSTALL}`.")


def tell_untrusted(db, agent, cwd: str | None) -> bool:
    """Tell whoever watches ``agent``, once, that brindle couldn't trust its
    hook, so this Codex agent started without it: nobody may watch a reviewer's
    pane to find out otherwise, and a supervisor gets no launch reply. A hook
    the person turned off stays theirs to turn back on (launch_warning,
    doctor), not news."""
    if agent.provider != "codex" or cwd in _skipped or not policy_on(cwd) or status() not in (NEW, MOVED):
        return False
    return _tell_once(db, agent, UNTRUSTED, UNTRUSTED)


# -- asking Codex itself ---------------------------------------------------------------


class _AppServer:
    """A short-lived ``codex app-server`` over stdio (JSON-RPC, one per line),
    on the Codex home brindle runs Codex with (``codex_home``): the trust it
    lists and writes is that home's."""

    def __init__(self, binary: str, flags: list[str], timeout: float = 30.0):
        self.timeout = timeout
        try:
            self.proc = spawn.start([binary, "app-server", *flags], stdin=subprocess.PIPE,
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
    key: str     # the PermissionRequest hook's trust key and hash
    hash: str
    status: str  # Codex's trustStatus (untrusted | trusted | modified | managed): "trusted" only if both are
    config: Path
    pre_key: str = ""    # the PreToolUse hook's
    pre_hash: str = ""

    def keys(self) -> dict[str, str]:
        return {self.key: self.hash, self.pre_key: self.pre_hash}

    def record(self) -> dict[str, str]:
        return {"command": self.command, "key": self.key, "hash": self.hash,
                "pre_key": self.pre_key, "pre_hash": self.pre_hash}


def inspect(binary: str, command: str | None = None) -> HookState:
    """What Codex makes of brindle's hooks: their trust keys, hashes and status."""
    command = command or hook_command()
    server = _AppServer(binary, config_flags(command))
    try:
        result = server.call("hooks/list", {"cwds": [str(Path.home())]})
    finally:
        server.close()
    found: dict[str, dict] = {}
    for entry in result.get("data") or []:
        for hook in entry.get("hooks") or []:
            if hook.get("source") == SOURCE and hook.get("command") in (command, pre_tool_command(command)):
                found.setdefault(hook["command"], hook)
    if len(found) < 2:
        raise CodexHookError("Codex didn't list brindle's hooks (is this Codex too old for PreToolUse hooks?)")
    asks, pre = found[command], found[pre_tool_command(command)]
    statuses = [str(h.get("trustStatus")) for h in (asks, pre)]
    return HookState(command, str(asks["key"]), str(asks["currentHash"]),
                     next((s for s in statuses if s != "trusted"), "trusted"), codex_home() / "config.toml",
                     str(pre["key"]), str(pre["currentHash"]))


def trust(binary: str, state: HookState, *, turn_on: bool = False) -> HookState:
    """Record the person's trust of the hooks in Codex's config (through Codex's
    own writer), check Codex now sees them as trusted, and remember it. A hook
    turned off in Codex stays off (HookTurnedOff) unless ``turn_on``: only the
    person asking for it back sets that, never brindle's own upkeep."""
    from brindle import permissions
    from brindle.providers import _config_lock

    server = _AppServer(binary, config_flags(state.command))
    try:
        # The lock trust_codex_project rewrites config.toml under: its text from
        # before this write would otherwise put the old trust back, or none.
        with _config_lock("codex-trust.lock"):
            try:
                entries = _hook_entries(codex_home())
            except ValueError:
                entries = {}
            off = {key for key in state.keys() if _turned_off(entries.get(key))}
            if off and not turn_on:
                raise HookTurnedOff("the hook is turned off in Codex")
            server.call("config/batchWrite", {"edits": [{
                "keyPath": "hooks.state", "mergeStrategy": "upsert",
                "value": {key: {"trusted_hash": hash_, **({"enabled": True} if key in off else {})}
                          for key, hash_ in state.keys().items()}}]})
    finally:
        server.close()
    # Written but not yet verified: on record without its definition, which
    # status() reads as MOVED (verify again, and tell), never as a removal.
    home = _home_key(codex_home())
    with permissions.editing() as store:
        store.codex_hook[home] = state.record()
    after = inspect(binary, state.command)
    if after.status != "trusted":
        raise CodexHookError(f"Codex still reports the hooks as {after.status}")
    with permissions.editing() as store:
        store.codex_hook[_home_key(after.config.parent)] = {**after.record(), "definition": definition(after.command)}
    return after
