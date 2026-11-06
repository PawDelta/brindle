"""The OS sandbox for Claude Code and Antigravity workers' shell commands.

brindle's permission rules read what a command says (brindle.permissions); they
aren't a sandbox: a program allowed alone can read a credential file however it
is spelled (an env var inlined into ``bash -c``, ``os.environ``, ``shopt
dotglob``, a case or array indirection). Codex workers already run their
commands under Codex's own sandbox with every credential store read-denied
(providers._codex_sandbox). This module gives Claude Code and Antigravity
workers the same boundary through each CLI's native command sandbox:

* Claude Code: its ``sandbox`` settings, passed with ``--settings`` (a layer
  only managed settings outrank): the sandbox required (``failIfUnavailable``),
  no unsandboxed retry (``allowUnsandboxedCommands: false``, which also makes
  repository settings unable to loosen it), brindle's permission decisions kept
  (``autoAllowBashIfSandboxed: false``), every credential store read- and
  write-denied, the CLI's own credentials scrubbed from command environments,
  and only brindle's daemon socket reachable among Unix sockets. Bash, its
  children and the Agent tool's subagents are inside; file tools, hooks and MCP
  servers stay under their existing rules.
* Antigravity (agy): ``--sandbox`` with session-only ``--settings`` denying the
  same stores to ``read_file``/``write_file`` (which the sandbox mounts honour).
  agy retries a blocked command with ``BypassSandbox: true`` on its own, so
  brindle's PreToolUse hook denies every such call (bypass_requested). agy's
  sandbox doesn't scrub the command environment, so the pane of a sandboxed agy
  worker starts without a Gemini key agy itself ignores (agy_env_denies).

Policy: on for workers (never an interactive supervisor's own chat) of both
providers whenever the repo's permission policy is on, unless the person turned
it off for their workers with ``"os_sandbox": "off"`` in ``~/.brindle/config.json``
(USER_KEY): an owner switch, not a repository setting a worker could edit.
Required means fail closed: an unsupported platform, a CLI too old to enforce
the settings, a missing dependency, a managed Claude setting that would weaken
the sandbox, or a boundary this machine can't apply (probe: brindle itself
sandboxed, so Seatbelt can't nest) refuses the launch with what to repair or
the off switch (OsSandboxRefused). Nothing falls back to unsandboxed commands.

Codex is unchanged. Nothing here starts a process but through brindle.spawn.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import threading
from dataclasses import dataclass

from brindle.config import brindle_home, user_settings

USER_KEY = "os_sandbox"             # ~/.brindle/config.json: "off" turns the OS sandbox off for workers
PROVIDERS = ("claude", "antigravity")
LABEL = {"claude": "Claude", "antigravity": "Antigravity"}
# The oldest CLI whose documented sandbox settings enforce what brindle relies
# on: Claude Code's ``allowUnsandboxedCommands: false`` from ``--settings`` holds
# against a project's ``true`` from 2.1.285; agy documents ``--sandbox`` and the
# unavailable-sandbox behaviour from 1.4.4 (verified on 1.5.2).
MIN_VERSION = {"claude": (2, 1, 285), "antigravity": (1, 4, 4)}
PROBE_SECONDS = 20
OFF_SWITCH = ('turn the OS sandbox off for your workers ("os_sandbox": "off" in ~/.brindle/config.json, '
              "`brindle permissions os-sandbox off`)")
# Claude Code's managed settings outrank --settings: a managed value for any of
# these would weaken the sandbox brindle asks for.
_CLAUDE_WEAKENING = {
    "enabled": lambda v: v is False,
    "failIfUnavailable": lambda v: v is False,
    "allowUnsandboxedCommands": lambda v: v is True,
    "autoAllowBashIfSandboxed": lambda v: v is True,
    "excludedCommands": lambda v: bool(v),
    "enableWeakerNestedSandbox": lambda v: v is True,
}
# What agy's PreToolUse payload says when the agent asks to run a command
# outside the sandbox (seen on agy 1.5.2: ``toolCall.args.BypassSandbox``).
AGY_BYPASS_ARG = "BypassSandbox"
# A sandboxed command's own stderr when the OS refuses it (Seatbelt, bubblewrap).
DENIED = re.compile(r"Operation not permitted|Permission denied|No such file or directory", re.I)
# sandbox-exec run inside a sandbox: Seatbelt profiles don't nest.
NESTED = "sandbox_apply"


class OsSandboxRefused(RuntimeError):
    """The OS sandbox is required for this worker but can't be applied:
    ``problem``, a clause saying why, and ``fix``, what would change that."""

    def __init__(self, provider: str, problem: str, fix: str) -> None:
        self.provider, self.problem, self.fix = provider, problem, fix
        super().__init__(
            f"brindle won't start this {LABEL.get(provider, provider)} worker: permission_policy is on, so its "
            f"shell commands must run in the OS sandbox, but {problem}. {fix}, or {OFF_SWITCH}")


# -- policy -----------------------------------------------------------------------------


def switch() -> str:
    """The person's own switch: "off" when ``~/.brindle/config.json`` says so, else "on"."""
    value = user_settings().get(USER_KEY)
    return "off" if isinstance(value, str) and value.strip().lower() == "off" else "on"


def policy_root(cwd: str) -> str:
    """Whose ``.brindle`` config decides for a launch in ``cwd``: the main
    checkout's, even for a linked worktree (a worker could write its own
    worktree's ``config.local.json``; the main checkout's it can't)."""
    from brindle import git

    try:
        return git.main_repo_root(cwd)
    except (git.GitError, OSError, ValueError):
        return cwd


def policy_on(cwd: str | None) -> bool:
    from brindle.config import load_repo_config

    try:
        return bool(cwd) and load_repo_config(policy_root(cwd)).permission_policy == "on"
    except Exception:  # noqa: BLE001 - as the rest of the policy reads it
        return False


def wanted(cwd: str | None, provider: str, mode: str | None) -> bool:
    """Whether a ``provider`` agent of ``mode`` launching in ``cwd`` runs its
    commands in the OS sandbox: a worker (never an interactive chat) of a
    provider this module covers, in a repo whose permission policy is on, with
    the person's switch not off."""
    return (provider in PROVIDERS and mode != "interactive" and bool(cwd)
            and switch() != "off" and policy_on(cwd))


# -- what is protected --------------------------------------------------------------------


def _aliases(path: str) -> list[str]:
    """``path`` expanded and normalized, and where it resolves to when that
    differs (a store that is itself a symlink: the sandbox matches the path a
    file is reached by, so both spellings are denied)."""
    full = os.path.normpath(os.path.expanduser(path))
    real = os.path.realpath(full)
    return [full] if real == full else [full, real]


def _env_named_stores() -> list[str]:
    """Credential stores a person moved with an environment variable."""
    out = []
    for name in ("GH_CONFIG_DIR", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "GOOGLE_APPLICATION_CREDENTIALS",
                 "CLOUDSDK_CONFIG", "GNUPGHOME", "DOCKER_CONFIG", "NPM_CONFIG_USERCONFIG", "KUBECONFIG"):
        if value := os.environ.get(name):
            out.append(value)
    if xdg := os.environ.get("XDG_CONFIG_HOME"):
        out += [os.path.join(xdg, "gh"), os.path.join(xdg, "gcloud")]
    return out


def protected_paths() -> list[str]:
    """Every path a sandboxed command may not read, absolute: the credential
    stores brindle's rules deny (permissions.credential_paths, the single
    source), the stores an environment variable relocated, and each one's
    resolved alias. Ordered, without duplicates."""
    from brindle import permissions

    seen: dict[str, None] = {}
    for path in [*permissions.credential_paths(), *_env_named_stores()]:
        for alias in _aliases(path):
            seen.setdefault(alias, None)
    return list(seen)


def policy_files(cwd: str | None) -> list[str]:
    """brindle's policy files a sandboxed command may not write either: the
    person's config (this switch, permission_policy), the rules store, and
    the repo's own ``.brindle`` config files in the main checkout."""
    from brindle import permissions
    from brindle.config import CONFIG_DIR, CONFIG_FILE, LOCAL_CONFIG_FILE, user_config_path

    paths = [str(user_config_path()), str(permissions.store_path())]
    if cwd:
        root = policy_root(cwd)
        paths += [str(permissions.repo_rules_path(root)), os.path.join(root, CONFIG_DIR, CONFIG_FILE),
                  os.path.join(root, CONFIG_DIR, LOCAL_CONFIG_FILE)]
    return paths


def write_protected_paths(cwd: str | None) -> list[str]:
    seen: dict[str, None] = {}
    for path in [*protected_paths(), *policy_files(cwd)]:
        for alias in _aliases(path):
            seen.setdefault(alias, None)
    return list(seen)


def writable_roots(cwd: str | None) -> list[str]:
    """What a sandboxed worker must write outside its worktree: the shared
    build caches and the git metadata a linked worktree keeps outside it
    (providers._worktree_git_dirs: never the common dir's hooks or config)."""
    if not cwd:
        return []
    from brindle import build_caches, git, providers
    from brindle.config import load_repo_config

    try:
        repo_root = git.main_repo_root(cwd)
        return build_caches.writable_dirs(repo_root, load_repo_config(repo_root)) + providers._worktree_git_dirs(cwd)
    except (git.GitError, ValueError, OSError):
        return []


def secret_env_names(provider: str, profile) -> list[str]:
    """The variables a ``provider`` worker's pane carries that hold a secret:
    the CLI's own credentials (an API key, a login token, a cloud session;
    secrets.provider_credentials, the names a pane keeps for it) that look
    like one. The CLI reads them outside the sandbox; its commands don't get
    them. A profile's own ``env`` lines are the person's choice and stay."""
    from brindle import secrets

    api_key_env = getattr(profile, "api_key_env", None)
    return sorted(n for n in secrets.provider_credentials(provider, api_key_env) if secrets.secret_looking(n))


def agy_env_denies() -> tuple[str, ...]:
    """Variables a sandboxed Antigravity worker's pane starts without: agy's
    sandbox leaves the command environment as it is, so the Gemini key agy
    itself ignores (providers.agy_uses_gemini_key) is kept from its commands.
    Under the Gemini-key route agy needs it, and its commands see it too."""
    from brindle import providers

    return () if providers.agy_uses_gemini_key() else providers._ENV_AUTH["antigravity"]


# -- the boundary itself --------------------------------------------------------------------


@dataclass
class Probe:
    """One run of the OS boundary: ``denied``, the protected read failed;
    ``control``, the allowed write and read worked; ``nested``, the sandbox
    couldn't be applied at all (brindle itself runs inside one)."""
    denied: bool
    control: bool
    nested: bool
    detail: str


def _seatbelt_profile(deny: list[str]) -> str:
    rules = "".join(f'(subpath "{p}")' for p in deny)
    return f"(version 1)(allow default)(deny file-read* file-write* {rules})"


def sandboxed_argv(deny: list[str], argv: list[str]) -> list[str]:
    """``argv`` under this platform's OS sandbox primitive, every path in
    ``deny`` unreadable and unwritable, everything else as outside: macOS
    Seatbelt (sandbox-exec), which Claude Code and agy also use; Linux
    bubblewrap, a tmpfs over each denied path. Raises OsSandboxRefused on a
    platform with neither."""
    if sys.platform == "darwin":
        return ["/usr/bin/sandbox-exec", "-p", _seatbelt_profile(deny), *argv]
    if sys.platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise OsSandboxRefused("claude", "bubblewrap (bwrap) isn't installed", "Install bubblewrap and socat")
        mounts = [a for p in deny for a in ("--tmpfs", p)]
        return [bwrap, "--unshare-user", "--die-with-parent", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                *mounts, "--", *argv]
    raise OsSandboxRefused("claude", f"{sys.platform} has no OS sandbox brindle can apply", "Run workers on macOS or Linux")


def run_sandboxed(deny: list[str], argv: list[str], *, cwd: str | None = None,
                  env: dict[str, str] | None = None):
    """Run ``argv`` under the OS boundary (sandboxed_argv) and return the
    CompletedProcess; the caller reads its exit code and stderr."""
    from brindle import spawn

    return spawn.read(sandboxed_argv(deny, argv), cwd=cwd, env=env, timeout=PROBE_SECONDS)


def probe(deny: list[str], target: str, control: str, argv: list[str] | None = None,
          env: dict[str, str] | None = None) -> Probe:
    """Execute the boundary: ``argv`` (default ``cat target``) must fail to
    read ``target`` (inside one of ``deny``), and a write then read of
    ``control`` (outside them) must work. The same OS primitive and the
    same deny list the providers get; a synthetic ``target`` only."""
    full_env = {**os.environ, **(env or {})}
    try:
        read = run_sandboxed(deny, argv or ["/bin/cat", target], env=full_env)
    except OSError as e:
        return Probe(False, False, False, f"couldn't run the sandbox: {e}")
    text = (read.stderr or "") + (read.stdout or "")
    if NESTED in text:
        return Probe(False, False, True, text.strip())
    marker = "brindle-os-sandbox-control"
    check = run_sandboxed(deny, ["/bin/sh", "-c", f"printf %s {marker} > {shlex.quote(control)} && cat {shlex.quote(control)}"],
                          env=full_env)
    denied = read.returncode != 0 and marker not in (read.stdout or "")
    return Probe(denied, check.returncode == 0 and marker in (check.stdout or ""), False,
                 f"read: rc={read.returncode} {text.strip()[:200]}; control: rc={check.returncode}")


# -- capability -------------------------------------------------------------------------------


_CAPABILITY: dict[tuple, tuple[str, str] | None] = {}
_LOCK = threading.Lock()


def _version_tuple(text: str | None) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(g) for g in m.groups()) if m else None


def _platform_problem() -> tuple[str, str] | None:
    if sys.platform == "darwin":
        if not os.path.isfile("/usr/bin/sandbox-exec"):
            return "macOS has no /usr/bin/sandbox-exec (Seatbelt)", "Repair the macOS install"
        return None
    if sys.platform.startswith("linux"):
        missing = [t for t in ("bwrap", "socat") if not shutil.which(t)]
        if missing:
            return (f"{' and '.join(missing)} missing: Claude Code's and agy's Linux sandboxes need bubblewrap "
                    "(and socat for the network proxy)", "Install them (apt/dnf install bubblewrap socat)")
        return None
    return f"{sys.platform} has no command sandbox either CLI can apply", "Run workers on macOS or Linux (WSL2)"


def _probe_problem() -> tuple[str, str] | None:
    """Whether this machine applies the boundary: a synthetic store under
    brindle's cache is denied and a write beside it works."""
    import tempfile

    base = brindle_home() / "cache"
    try:
        base.mkdir(parents=True, exist_ok=True)
        folder = tempfile.mkdtemp(prefix="os-sandbox-probe-", dir=str(base))
    except OSError:
        folder = tempfile.mkdtemp(prefix="brindle-os-sandbox-probe-")
    store = os.path.join(folder, ".store")
    os.mkdir(store)
    target = os.path.join(store, "credential")
    with open(target, "w", encoding="utf-8") as f:
        f.write("synthetic\n")
    try:
        try:
            result = probe([store], target, os.path.join(folder, "control"))
        except OsSandboxRefused as e:
            return e.problem, e.fix
        except Exception as e:  # noqa: BLE001 - a probe that can't run is a sandbox that can't be shown to work
            return f"the sandbox probe couldn't run ({e})", "Check that the sandbox tools run on this machine"
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if result.nested:
        return ("brindle itself runs inside an OS sandbox here, and Seatbelt doesn't nest, so a worker's commands "
                "couldn't be sandboxed", "Start brindle outside the sandbox")
    if not result.denied:
        return f"the OS sandbox didn't deny a protected read ({result.detail})", "Check the sandbox tools"
    if not result.control:
        return f"the OS sandbox refused an allowed write ({result.detail})", "Check the sandbox tools"
    return None


def capability_problem(provider: str, binary: str | None = None) -> tuple[str, str] | None:
    """Why ``provider``'s OS sandbox can't be required on this machine, as
    (problem, fix), or None. Cached per (provider, executable, its mtime,
    platform): no probe per launch."""
    from brindle import providers_install

    exe = binary or _binary(provider)
    try:
        key = (provider, exe, os.path.getmtime(exe), sys.platform)
    except OSError:
        key = (provider, exe, None, sys.platform)
    with _LOCK:
        if key in _CAPABILITY:
            return _CAPABILITY[key]
    problem = _platform_problem()
    if problem is None:
        version = _version_tuple(providers_install.probe_version(exe)) if exe and os.path.isfile(exe) else None
        if version is None:
            problem = (f"{LABEL[provider]}'s version couldn't be read from `{exe}`",
                       f"Install {LABEL[provider]}'s CLI {'.'.join(map(str, MIN_VERSION[provider]))} or newer")
        elif version < MIN_VERSION[provider]:
            problem = (f"{LABEL[provider]} {'.'.join(map(str, version))} is older than "
                       f"{'.'.join(map(str, MIN_VERSION[provider]))}, the first whose sandbox settings brindle can rely on",
                       f"Update {LABEL[provider]}")
    if problem is None:
        problem = _probe_problem()
    with _LOCK:
        _CAPABILITY[key] = problem
    return problem


def _binary(provider: str) -> str:
    from brindle import antigravity, providers

    exe = antigravity.binary() if provider == "antigravity" else providers.claude_binary()
    return providers.which_cli(exe) or exe


def forget_capability() -> None:
    with _LOCK:
        _CAPABILITY.clear()


def require(provider: str) -> None:
    """Raise OsSandboxRefused unless ``provider``'s sandbox can be required here."""
    problem = capability_problem(provider)
    if problem is not None:
        raise OsSandboxRefused(provider, *problem)
    if provider == "claude" and (conflict := claude_managed_conflict()) is not None:
        raise OsSandboxRefused(provider, *conflict)


# -- Claude Code ------------------------------------------------------------------------------


def claude_managed_conflict() -> tuple[str, str] | None:
    """A managed Claude Code setting that would weaken the sandbox brindle
    asks for (managed settings outrank ``--settings``), or None."""
    from brindle import providers

    for path in providers.managed_settings_paths():
        try:
            data = providers._read_json(path)
        except (OSError, ValueError):
            continue
        sandbox = data.get("sandbox") if isinstance(data, dict) else None
        if not isinstance(sandbox, dict):
            continue
        for key, weak in _CLAUDE_WEAKENING.items():
            if key in sandbox and weak(sandbox[key]):
                return (f"{path} sets sandbox.{key} = {json.dumps(sandbox[key])}, which managed settings make "
                        "override brindle's", f"Remove sandbox.{key} from {path}")
        fs = sandbox.get("filesystem")
        if isinstance(fs, dict) and fs.get("disabled") is True:
            return (f"{path} sets sandbox.filesystem.disabled = true, which managed settings make override brindle's",
                    f"Remove sandbox.filesystem.disabled from {path}")
    return None


def claude_settings(cwd: str | None, profile) -> dict:
    """Claude Code's ``sandbox`` settings for a worker in ``cwd`` (see the
    module docstring). Raises OsSandboxRefused when it can't be required."""
    from brindle.daemon import lifecycle

    require("claude")
    # A profile's add_dirs are the agent's to edit, by Bash too (providers.ClaudeCode.command).
    extra = [os.path.expanduser(d) for d in (getattr(profile, "add_dirs", None) or [])]
    return {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": False,
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        "enableWeakerNestedSandbox": False,
        "filesystem": {
            "disabled": False,
            "denyRead": protected_paths(),
            "denyWrite": write_protected_paths(cwd),
            "allowWrite": [*writable_roots(cwd), *extra],
        },
        "credentials": {"envVars": [{"name": n, "mode": "deny"} for n in secret_env_names("claude", profile)]},
        # Outbound network stays open (the boundary is credentials, not the
        # internet; Claude's proxy still refuses hosts resolving to link-local or
        # loopback), tests may bind and reach local ports, and of the Unix sockets
        # only brindle's daemon: git's credential helper asks it for the GitHub
        # sign-in. Its routes check the peer and a token the sandbox can't read.
        "network": {"allowedDomains": ["*"], "allowLocalBinding": True,
                    "allowUnixSockets": [str(lifecycle.socket_path())]},
    }


# -- Antigravity ----------------------------------------------------------------------------------


def agy_flags(cwd: str | None) -> list[str]:
    """agy's launch flags for a sandboxed worker in ``cwd``: ``--sandbox`` and
    session-only settings denying ``read_file``/``write_file`` on every
    protected path (the sandbox mounts honour them), allowing writes to
    brindle's writable roots, and no ``command`` allow entries of the
    person's (one lets a bypass run without a prompt; a relative path among
    them breaks the sandbox's mounts). Raises OsSandboxRefused."""
    require("antigravity")
    deny = [f"read_file({p})" for p in protected_paths()]
    deny += [f"write_file({p})" for p in write_protected_paths(cwd) if f"read_file({p})" not in deny]
    allow = [f"write_file({root})" for root in writable_roots(cwd)]
    settings = {"enableTerminalSandbox": True, "permissions": {"allow": allow, "ask": [], "deny": deny}}
    return ["--sandbox", "--settings", json.dumps(settings)]


def bypass_requested(payload: object) -> bool:
    """Whether an agy PreToolUse payload asks to run a command outside the
    sandbox (``run_command`` with ``BypassSandbox`` true)."""
    if not isinstance(payload, dict):
        return False
    call = payload.get("toolCall")
    if not isinstance(call, dict) or call.get("name") != "run_command":
        return False
    args = call.get("args")
    return isinstance(args, dict) and args.get(AGY_BYPASS_ARG) is True


BYPASS_DENIED = ("brindle: this worker's commands run in the OS sandbox and may not run outside it. Run it "
                 "inside the sandbox another way, or tell your supervisor what the sandbox stopped")


def bypass_denial() -> dict:
    return {"decision": "deny", "reason": BYPASS_DENIED}


# -- doctor -------------------------------------------------------------------------------------------


def status(repo_root: str | None) -> list[tuple[str, str]]:
    """(level, detail) lines for ``brindle doctor``: whether workers' commands
    run in the OS sandbox here, per provider, and if not why."""
    from brindle.config import load_repo_config

    lines: list[tuple[str, str]] = []
    if switch() == "off":
        return [("warn", f'off: "{USER_KEY}": "off" in {brindle_home() / "config.json"}; Claude and Antigravity '
                         "workers' commands can read credential files (`brindle permissions os-sandbox on`)")]
    try:
        on = bool(repo_root) and load_repo_config(repo_root).permission_policy == "on"
    except Exception:  # noqa: BLE001
        on = False
    if not on:
        return [("ok", "applies once permission_policy is on (then required for Claude and Antigravity workers)")]
    for provider in PROVIDERS:
        exe = _binary(provider)
        if not os.path.isfile(exe):
            continue
        problem = capability_problem(provider, exe)
        if problem is None and provider == "claude":
            problem = claude_managed_conflict()
        if problem is None:
            lines.append(("ok", f"{LABEL[provider]} workers: commands sandboxed, credential stores denied"))
        else:
            lines.append(("fail", f"{LABEL[provider]} workers won't start: {problem[0]}. {problem[1]}, or {OFF_SWITCH}"))
    return lines or [("ok", "no Claude or Antigravity CLI installed to sandbox")]
