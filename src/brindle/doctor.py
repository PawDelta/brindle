"""``brindle doctor``: what brindle needs, and whether it's there.

Plain Claude Code needs nothing set up; brindle needs tmux, the agent CLIs,
a writable home, and (per repo) a few optional pieces. This checks each one
and says what to do about anything missing, so a first run doesn't fail
halfway through a launch.
"""

from __future__ import annotations

import contextvars
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import closing, nullcontext
from dataclasses import dataclass
from pathlib import Path

from brindle import spawn
from brindle.db import DB

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    level: str
    name: str
    detail: str


VERSION_SECONDS = 15
TMUX_SECONDS = 10


def _version(cmd: list[str]) -> str | None:
    try:
        out = spawn.read(cmd, timeout=VERSION_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[-1].strip() if text else ""


def _tool(name: str, required: bool, why: str, version_args: list[str] | None = None,
          install: str = "") -> Check:
    from brindle.providers import which_cli

    path = which_cli(name)
    if not path:
        windows = shutil.which(name)
        if windows:
            return Check(FAIL if required else WARN, name,
                         f"only a Windows install was found ({windows}): WSL needs a "
                         f"Linux install. Install: {install or 'the Linux version of ' + name}")
        level = FAIL if required else WARN
        return Check(level, name, f"not found: {why}." + (f" Install: {install}" if install else ""))
    version = _version([path, *(version_args or ["--version"])]) or ""
    return Check(OK, name, f"{path}" + (f" ({version[:40]})" if version else ""))


# Linux package managers and their tmux install, in the order looked for.
TMUX_LINUX = (("apt-get", "sudo apt install tmux"), ("dnf", "sudo dnf install tmux"),
              ("pacman", "sudo pacman -S tmux"), ("zypper", "sudo zypper install tmux"),
              ("apk", "sudo apk add tmux"))


def tmux_install() -> str:
    """How to install tmux here: Homebrew on macOS, else this distro's package manager."""
    if sys.platform == "darwin":
        return "brew install tmux"
    for manager, cmd in TMUX_LINUX:
        if shutil.which(manager):
            return cmd
    return "sudo apt install tmux (or your distro's package manager)"


CLI_INSTALL = {
    "claude": ("Claude Code", "curl -fsSL https://claude.ai/install.sh | bash (or: npm install -g "
                              "@anthropic-ai/claude-code; on Linux, EACCES means set a user prefix first: "
                              "`npm config set prefix ~/.local`)"),
    "codex": ("Codex", "npm install -g @openai/codex (on Linux, EACCES means set a user prefix first: "
                       "`npm config set prefix ~/.local`)"),
    "antigravity": ("Google Antigravity", "see https://antigravity.google"),
    "opencode": ("OpenCode", "npm install -g opencode-ai (or see https://opencode.ai)"),
}


def preflight(provider: str) -> list[str]:
    """What stops a chat on ``provider`` from starting at all, as lines to
    print, so a first run fails before anything launches instead of leaving
    an empty tmux window. Empty when it can start."""
    from brindle import antigravity, opencode, providers

    problems = []
    if not shutil.which("tmux"):
        problems.append("tmux isn't installed: brindle runs every agent in a tmux window. "
                        f"Install: {tmux_install()}")
    binary = {"claude": providers.claude_binary, "codex": providers.codex_binary,
              "antigravity": antigravity.binary, "opencode": opencode.binary}.get(provider)
    if binary:
        exe = binary()
        if not (providers.which_cli(exe) or os.path.isfile(exe)):
            name, how = CLI_INSTALL[provider]
            problems.append(f"{name} (`{exe}`) isn't on PATH: the chat runs on it. Install: {how}")
        else:
            why = providers.signed_out(provider)
            if why:
                problems.append(why)
    return problems


def signin_providers() -> list[str]:
    """The provider CLIs installed here, in CLI_INSTALL order."""
    from brindle import antigravity, opencode, providers

    binaries = {"claude": providers.claude_binary, "codex": providers.codex_binary,
                "antigravity": antigravity.binary, "opencode": opencode.binary}
    out = []
    for provider in CLI_INSTALL:
        exe = binaries[provider]()
        if providers.which_cli(exe) or os.path.isfile(exe):
            out.append(provider)
    return out


def signed_in_providers() -> list[str]:
    """Names of the installed provider CLIs this machine is signed in to, by
    the same test as signin_checks (an environment key, or a status check that
    answered "signed in"). Names only: never a key or token."""
    from brindle import providers

    out = []
    for provider in signin_providers():
        if any(os.environ.get(k) for k in providers._ENV_AUTH.get(provider, ())):
            out.append(provider)
        elif providers.signed_out(provider) is None and providers.seen_signed_in(provider):
            out.append(provider)
    return out


def _profile_keys(provider: str, repo_root: str | None) -> dict[str, list[str]]:
    """The environment keys (from providers._ENV_AUTH) that profiles on
    ``provider`` set in their ``env.NAME: value`` lines, with the profiles
    setting each. An empty value clears a key, so it doesn't count."""
    from brindle import providers
    from brindle.profiles import list_profiles

    keys = providers._ENV_AUTH.get(provider, ())
    out: dict[str, list[str]] = {}
    try:
        profiles = list_profiles(repo_root)
    except Exception:  # noqa: BLE001 - a bad profile is reported elsewhere
        return out
    for p in profiles:
        if p.provider != provider:
            continue
        key = next((k for k in keys if p.env.get(k)), None)
        if key:
            out.setdefault(key, []).append(p.name)
    return out


def signin_checks(repo_root: str | None = None) -> list[Check]:
    """For each installed CLI: how it's signed in (its own login, a key from
    the environment, or signed out) and whether a quota limit is in effect.
    The environment keys come from providers._ENV_AUTH, so a provider listed
    there shows its key here, whether it's set in brindle's environment or in
    a profile's env lines; only the variable's name is shown."""
    from brindle import providers, quota

    out = []
    for provider in signin_providers():
        level = OK
        env = next((k for k in providers._ENV_AUTH.get(provider, ()) if os.environ.get(k)), None)
        why = None if env else providers.signed_out(provider)
        if env:
            signin = f"environment key {env}"
        elif why:
            level = FAIL if provider == "claude" else WARN
            signin = f"signed out: {why}"
        elif providers.seen_signed_in(provider):
            # Only a status check that answered "signed in" records this.
            signin = "its own login"
        else:
            signin = "sign-in unknown (the status check gave no answer)"
        if not env:
            for key, names in _profile_keys(provider, repo_root).items():
                signin += f"; profile{'s' if len(names) > 1 else ''} {', '.join(names)}: environment key {key}"
        limit = quota.note(provider)
        if limit and quota.headroom(provider) <= 10 and level == OK:
            level = WARN
        # A signed-out CLI has no quota to speak of, unless a limit was recorded before.
        if limit:
            quota_text = f"; quota: {limit}"
        else:
            quota_text = "" if why else "; quota: no limit in effect"
        out.append(Check(level, f"{CLI_INSTALL[provider][0]} sign-in", f"{signin}{quota_text}"))
    return out


def credential_checks() -> list[Check]:
    """One line per installed provider naming the credential its panes will
    use (names only, never a value). For Claude, a key and a subscription login
    together is a warning: Claude Code bills the key once it's approved."""
    from brindle import keystore, providers

    out = []
    for provider in signin_providers():
        if provider == "claude":
            cred = providers.claude_credential()
            if cred.kind == "org-managed":
                out.append(Check(OK, "claude", f"org-managed ({cred.name})"))
            elif cred.keyed or cred.kind == "cloud":
                what = "API key" if cred.kind == "api key" else "auth token" if cred.kind == "token" else "cloud provider"
                detail = f"{what} ({cred.name}, from {cred.source})"
                level = OK
                if cred.keyed and providers.subscription_login("claude"):
                    level = WARN
                    detail += ("; a subscription login exists too, but Claude Code bills the key once "
                               "it is approved (`brindle keys set ANTHROPIC_API_KEY` or `brindle doctor "
                               "--fix`), not the subscription")
                out.append(Check(level, "claude", detail))
            elif providers.signed_out("claude"):
                # The sign-in line above is the failure; this one only says there's no credential yet.
                out.append(Check(WARN, "claude", "no credential yet: Claude Code isn't signed in "
                                 "(see Claude Code sign-in above)"))
            else:
                out.append(Check(OK, "claude", "subscription login"))
            continue
        found = None
        for name in providers._ENV_AUTH.get(provider, ()):
            if os.environ.get(name):
                found = (name, "environment")
            else:
                try:
                    if keystore.get_key(name):
                        found = (name, "key store")
                except keystore.CredentialError:
                    pass
            if found:
                break
        if found and provider == "antigravity" and not providers.agy_uses_gemini_key():
            out.append(Check(WARN, provider, providers.agy_ignores_key_message()))
            continue
        if provider == "codex" and providers.codex_chatgpt_login():
            codex_key = _codex_key_source("CODEX_API_KEY")
            if codex_key:
                out.append(Check(WARN, provider, f"API key (CODEX_API_KEY, from {codex_key}); "
                                 f"{providers.CODEX_INTERACTIVE_USES_PLAN}; "
                                 + providers.CODEX_EXEC_BILLS_KEY))
                continue
            if found and found[0] == "OPENAI_API_KEY":
                out.append(Check(WARN, provider, f"OPENAI_API_KEY is set, but "
                                 + providers.CODEX_INTERACTIVE_USES_PLAN))
                continue
        out.append(Check(OK, provider, f"API key ({found[0]}, from {found[1]})" if found else "its own login"))
    return out


def cloud_checks(repo_root: str | None = None) -> list[Check]:
    """Cloud login diagnostics for the identities the configured profiles launch with."""
    from brindle import agents, cloud_login, company_login, providers
    from brindle.db import Agent, Workspace
    from brindle.profiles import ProfileError, list_profiles

    claude_managed = providers.claude_org_managed()
    grouped: dict[tuple[str, bool], tuple[dict, list[str]]] = {}

    def identity_key(env):
        # A bearer-backed profile and an SSO profile can name the same AWS
        # config. Grouping them together would hide the SSO profile's lapse.
        bearer = bool({**os.environ, **env}.get("AWS_BEARER_TOKEN_BEDROCK"))
        return cloud_login.action_key(env), bearer

    ambient = {**os.environ, **company_login.stored_env()}
    if not claude_managed and cloud_login.provider_from_env(ambient):
        grouped[identity_key(ambient)] = (ambient, ["default"])
    root = repo_root or os.getcwd()
    ws = Workspace("doctor", root, "doctor", "main", "HEAD", None, root, None, "", 0.0)
    managed = agents.managed(root)
    out = []
    for profile in list_profiles(repo_root):
        if profile.provider not in ("claude", "opencode") or (profile.provider == "claude" and claude_managed):
            continue
        agent = Agent("", ws.id, profile.name, profile.provider, None, "assign", "starting", "", None, 0.0)
        try:
            resolved = agents.launch_identity(agent, ws, profile, managed)[0]
        except ProfileError as e:
            out.append(Check(FAIL, f"cloud login ({profile.name})", str(e)))
            continue
        # Diagnostics use cloud selectors, never a repo's PATH or interpreter options.
        env = {k: v for k, v in resolved.items() if k in cloud_login.IDENTITY_ENV
               or k in ("AWS_ACCESS_KEY_ID", "AWS_BEARER_TOKEN_BEDROCK")}
        if not cloud_login.provider_from_env({**os.environ, **env}):
            continue
        key = identity_key(env)
        grouped.setdefault(key, (env, []))[1].append(profile.name)
    for env, names in grouped.values():
        status = cloud_login.configured_status(env)
        if status is None:
            continue
        detail = status.note
        if status.expires_at is not None:
            from datetime import datetime, timezone

            detail += f"; expires {datetime.fromtimestamp(status.expires_at, timezone.utc).isoformat()}"
        command = cloud_login.login_command(env)
        if command:
            import shlex

            detail += f"; refresh with `{shlex.join(command)}`"
        elif not status.ok:
            detail += "; refresh the configured credential file"
        out.append(Check(OK if status.ok else WARN, f"cloud login ({', '.join(names)})", detail))
    return out


def _codex_key_source(name: str) -> str | None:
    from brindle import keystore

    if os.environ.get(name):
        return "environment"
    try:
        return "key store" if keystore.get_key(name) else None
    except keystore.CredentialError:
        return None


def _gemini_key_present() -> bool:
    from brindle import keystore

    if os.environ.get("GEMINI_API_KEY"):
        return True
    try:
        return bool(keystore.get_key("GEMINI_API_KEY"))
    except keystore.CredentialError:
        return False


def fix(confirm) -> list[str]:
    """`brindle doctor --fix`: offer to let Claude Code use an exported
    ANTHROPIC_API_KEY without its approval question. ``confirm(prompt)``
    answers; declining (or nothing to do) leaves Claude Code's config alone.
    Also offers to set agy's ``"modelProvider": "gemini"`` when a Gemini key
    exists that agy would ignore. Returns the lines to print."""
    from brindle import company_login, providers

    lines: list[str] = []
    # Self-serve setup asks only on a terminal. What it found and its menu print
    # at once (before the question, not at the end); only an applied setup adds
    # result lines, so keeping the personal setup leaves "nothing to fix".
    company_login.apply_current(lines.append, info=print)
    if "antigravity" in signin_providers() and _gemini_key_present():
        providers.offer_agy_gemini(confirm, lines.append)
    return lines + _fix_claude(confirm, lines)


def _fix_claude(confirm, done: list[str]) -> list[str]:
    from brindle import providers

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return [] if done else ["nothing to fix"]
    if providers.claude_org_managed():
        return ["claude: org-managed; brindle does not approve keys there"]
    if providers.api_key_approved(key):
        return ["ANTHROPIC_API_KEY: already approved in Claude Code"]
    if not confirm("Let Claude Code use the exported ANTHROPIC_API_KEY in brindle panes without asking?"):
        return ["ANTHROPIC_API_KEY: left as is"]
    try:
        providers.approve_api_key(key)
    except (OSError, ValueError) as e:
        return [f"ANTHROPIC_API_KEY: couldn't update Claude Code's config: {e}"]
    return ["ANTHROPIC_API_KEY: approved for Claude Code"]


CHECK_SECONDS = 20     # what one check may take before doctor reports it and moves on
DISK_SECONDS = 90      # the disk report measures every worktree: legitimately slower
GRACE_SECONDS = 2      # past a check's deadline, for its own reads to stop and report


def _python_check() -> list[Check]:
    v = sys.version_info
    return [Check(OK if v >= (3, 11) else FAIL, "python",
                  f"{v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 11) else " (brindle needs 3.11 or newer)"))]


def _clipboard_check() -> list[Check]:
    from brindle import tmux

    clip = tmux.clipboard_command()
    return [Check(OK if clip else WARN, "clipboard",
                  f"{clip.split()[0]}: dragging in the chat copies to the clipboard" if clip else
                  "no pbcopy, wl-copy or xclip: mouse selection copies only within tmux")]


def _sidebar_check() -> list[Check]:
    return [Check(OK, "sidebar",
                  "`brindle sidebar` (or prefix S in tmux) brings a lost sidebar back to the session you're in")]


def _update_check() -> list[Check]:
    from brindle import updates

    update = updates.notice(refresh=True)
    return [Check(OK, "brindle update", update)] if update else []


def _company_identity_checks() -> list[Check]:
    from brindle import company_identity

    return company_identity.checks()


def _self_serve_checks() -> list[Check]:
    from brindle import self_serve

    return self_serve.checks()


def _signin_pause_checks() -> list[Check]:
    from brindle import signin_pause

    return signin_pause.checks()


def _legacy_checks(repo_root: str | None) -> list[Check]:
    from brindle import legacy

    return [Check(WARN, "previous name", line) for line in legacy.findings(repo_root)]


def _steps(repo_root: str | None, db: DB | None) -> list[tuple[str, object, float]]:
    """Every check, in the order doctor shows them: (name, function, seconds)."""
    tool = lambda *a, **k: (lambda: [_tool(*a, **k)])   # noqa: E731
    steps: list[tuple[str, object, float]] = [
        ("python", _python_check, CHECK_SECONDS),
        ("tmux", tool("tmux", True, "brindle runs every agent in a tmux window", ["-V"], tmux_install()),
         CHECK_SECONDS),
        ("claude", tool("claude", True, "the supervisor and the built-in profiles use Claude Code",
                        install=CLI_INSTALL["claude"][1]), CHECK_SECONDS),
        ("codex", tool("codex", False, "only needed for Codex agents (reviewer-codex)",
                       install=CLI_INSTALL["codex"][1]), CHECK_SECONDS),
        ("agy", tool("agy", False, "only needed for Google Antigravity agents"), CHECK_SECONDS),
        ("opencode", tool("opencode", False,
                          "only needed for OpenCode agents (developer-opencode, reviewer-opencode)",
                          install=CLI_INSTALL["opencode"][1]), CHECK_SECONDS),
        ("sign-in", lambda: signin_checks(repo_root), CHECK_SECONDS),
        ("credentials", credential_checks, CHECK_SECONDS),
        ("cloud", lambda: cloud_checks(repo_root), CHECK_SECONDS),
        ("company identity", _company_identity_checks, CHECK_SECONDS),
        ("self-serve", _self_serve_checks, CHECK_SECONDS),
        ("sign-in pause", _signin_pause_checks, CHECK_SECONDS),
        ("gh", tool("gh", False, "only needed for `brindle pr` and `brindle new --pr`"), CHECK_SECONDS),
        ("pre-commit", tool("pre-commit", False, "only needed if the repo uses pre-commit hooks"),
         CHECK_SECONDS),
        ("graphify", tool("graphify", False, "only needed for the code map agents can query"),
         CHECK_SECONDS),
        ("clipboard", _clipboard_check, CHECK_SECONDS),
        ("sidebar", _sidebar_check, CHECK_SECONDS),
        ("daemon", daemon_checks, CHECK_SECONDS),
        ("native models", lambda: native_checks(repo_root), CHECK_SECONDS),
        ("overrides", override_checks, CHECK_SECONDS),
        ("quota", lambda: quota_checks(repo_root), CHECK_SECONDS),
        ("airgap", lambda: airgap_checks(repo_root), CHECK_SECONDS),
        ("agent environment", lambda: agent_env_checks(repo_root), CHECK_SECONDS),
        ("tmux server directory", tmux_cwd_checks, CHECK_SECONDS),
        ("brindle update", _update_check, CHECK_SECONDS),
        ("brindle Pro", pro_checks, CHECK_SECONDS),
        ("managed models", lambda: managed_checks(repo_root), CHECK_SECONDS),
        ("previous name", lambda: _legacy_checks(repo_root), CHECK_SECONDS),
        ("brindle home", lambda: _home_check(db), CHECK_SECONDS),
        ("tmux focus-events", _tmux_server_check, CHECK_SECONDS),
        ("leftover processes", lambda: _process_check(db), CHECK_SECONDS),
        ("reaped", lambda: reap_checks(db), CHECK_SECONDS),
    ]
    if repo_root:
        steps.append(("repo", lambda: _repo_checks(repo_root), CHECK_SECONDS))
        steps.append(("working tree", lambda: _working_tree_check(repo_root), CHECK_SECONDS))
    return steps


def _home_check(db: DB | None) -> list[Check]:
    from brindle import config

    home = config.brindle_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".doctor-probe"
        probe.write_text("ok")
        probe.unlink()
        probe_db = db if db is not None else DB()
        with closing(probe_db.conn) if db is None else nullcontext():
            probe_db.conn.execute("SELECT 1")
        return [Check(OK, "brindle home", f"{home} (writable, database opens)")]
    except Exception as e:  # noqa: BLE001
        return [Check(FAIL, "brindle home", f"{home}: {e}")]


def _tmux_server_check() -> list[Check]:
    from brindle import tmux

    out: list[Check] = []
    if shutil.which("tmux"):
        try:
            sock = os.environ.get("BRINDLE_TMUX_SOCKET")
            cmd = ["tmux", *(["-L", sock] if sock else []), "-V"]
            spawn.read(cmd, timeout=TMUX_SECONDS)
            focus = tmux._tmux("show-options", "-gs", "focus-events", check=False).stdout.strip()
            out.append(Check(OK, "tmux focus-events", focus or "not set yet (brindle sets it at launch)"))
        except tmux.TmuxTimeout as e:
            # The server accepts connections but answers nothing: every
            # brindle command would hang on it until it is stopped.
            out.append(Check(FAIL, "tmux server", str(e)))
        except Exception as e:  # noqa: BLE001
            out.append(Check(WARN, "tmux focus-events", str(e)))
    return out


def _process_check(db: DB | None) -> list[Check]:
    from brindle import procs

    out: list[Check] = []
    try:
        process_db = db if db is not None else DB()
        with closing(process_db.conn) if db is None else nullcontext():
            table = procs.table()
            stray = []
            for aid in procs.all_agent_ids(table):
                a = process_db.get_agent(aid)
                if a is None or a.status in ("paused", "done") or a.dismissed_at is not None:
                    stray.append(aid)
        if stray:
            out.append(Check(WARN, "leftover processes",
                             f"{len(stray)} agent(s) have processes running but aren't active: "
                             f"{', '.join(stray[:5])}. `brindle prune` stops them."))
        else:
            out.append(Check(OK, "leftover processes", "none"))
    except Exception as e:  # noqa: BLE001
        out.append(Check(WARN, "leftover processes", f"couldn't check: {e}"))
    return out


def _repo_checks(repo_root: str) -> list[Check]:
    from brindle import config, detect

    out: list[Check] = []
    if repo_root:
        try:
            cfg = config.load_repo_config(repo_root)
            out.extend(routing_checks(cfg, repo_root))
            path = Path(repo_root) / config.CONFIG_DIR / config.CONFIG_FILE
            if path.exists():
                out.append(Check(OK, "repo config", f"{path}"))
            else:
                out.append(Check(OK, "repo config", "none (defaults apply; `brindle init` writes a starter)"))
            if cfg.checks:
                out.append(Check(OK, "checks", "; ".join(cfg.checks)))
            else:
                out.append(Check(WARN, "checks",
                                 "none configured: nothing verifies a branch before it merges. "
                                 'Add e.g. {"checks": ["uv run pytest -q"]} to .brindle/config.json'))
            out.extend(codex_hook_checks(repo_root))
            if cfg.add_dirs:
                from brindle.profiles import load_profile, missing_add_dirs
                # The repo's entries as any profile gets them: resolved, ~ expanded.
                profile = load_profile("developer", repo_root)
                missing = missing_add_dirs(profile)
                if missing:
                    out.append(Check(WARN, "add_dirs",
                                     f"{', '.join(missing)} not found: Claude Code ignores a "
                                     "missing --add-dir, so agents won't reach it"))
                else:
                    out.append(Check(OK, "add_dirs", ", ".join(profile.add_dirs or [])))
            if cfg.services:
                docker = shutil.which("docker")
                names = ", ".join(str(s.get("name")) for s in cfg.services)
                out.append(Check(OK if docker else WARN, "docker",
                                 f"{docker} (services: {names})" if docker else
                                 f"not found: per-worktree services ({names}) won't start"))
            elif any((Path(repo_root) / name).is_file() for name in detect.COMPOSE_FILES):
                out.append(Check(OK, "services", "docker compose found: parallel workers share its "
                                 "containers; per-worktree databases are `services` (brindle Pro)"))
            if (Path(repo_root) / "graphify-out" / "graph.json").is_file():
                out.append(Check(OK, "code map", "graphify-out/graph.json"))
            else:
                out.append(Check(WARN, "code map",
                                 "no graphify graph: agents find code by grepping. Run /graphify once."))
        except ValueError as e:
            out.append(Check(FAIL, "repo config", str(e)))
    return out


def _working_tree_check(repo_root: str) -> list[Check]:
    dirty = spawn.read(["git", "status", "--porcelain"], cwd=repo_root).stdout.strip()
    if dirty:
        return [Check(WARN, "working tree", "uncommitted changes: workers branch from committed work only")]
    return [Check(OK, "working tree", "clean")]


class _Stuck(Exception):
    """A check that ran past its bound."""


def _run_step(name: str, fn, seconds: float, bounded: bool) -> list[Check]:
    """``fn``'s checks, or one warning saying why it couldn't run. Bounded: it
    runs on a thread under a spawn deadline (stopping its git, tmux and other
    reads) and is abandoned if still stuck a moment after (a directory walk,
    the daemon, the network), so one check never holds doctor up."""
    def call() -> list[Check]:
        with spawn.deadline(seconds):
            return list(fn())

    def failed(e: BaseException) -> list[Check]:
        if isinstance(e, (spawn.Busy, subprocess.TimeoutExpired, _Stuck)):
            return [Check(WARN, name, f"couldn't check: took too long (over {seconds:g}s)")]
        return [Check(WARN, name, f"couldn't check: {e}")]

    if not bounded:
        try:
            return call()
        except Exception as e:  # noqa: BLE001 - a report, never a reason doctor fails
            return failed(e)
    box: dict[str, object] = {}
    ctx = contextvars.copy_context()

    def target() -> None:
        try:
            box["ok"] = ctx.run(call)
        except BaseException as e:  # noqa: BLE001 - handed back to the caller's thread
            box["err"] = e

    thread = threading.Thread(target=target, name=f"doctor-{name}", daemon=True)
    thread.start()
    thread.join(seconds + GRACE_SECONDS)
    if thread.is_alive():
        return failed(_Stuck())
    if "err" in box:
        return failed(box["err"])  # type: ignore[arg-type]
    return box["ok"]  # type: ignore[return-value]


def stream(repo_root: str | None, db: DB | None = None, disk: bool = False, cwd: str | None = None):
    """Run every check in turn, yielding each one's Checks as it finishes.
    ``disk``: also the disk report (only `brindle doctor` asks, as it is slow).
    ``cwd``: find the repo root from there, after the first check and as
    bounded as any (no repo is no repo checks, not an error).
    Without ``db`` each check is bounded (see _run_step); a borrowed ``db``
    belongs to the caller's thread, so those run on it, unbounded."""
    bounded = db is None
    skip = 0
    if cwd is not None:
        yield _run_step("python", _python_check, CHECK_SECONDS, bounded)
        skip = 1
        found: list[str] = []

        def discover() -> list[Check]:
            from brindle import git

            try:
                found.append(git.main_repo_root(cwd))
            except git.GitError:
                pass
            return []

        yield _run_step("repository", discover, CHECK_SECONDS, bounded)
        repo_root = found[0] if found else None
    steps = _steps(repo_root, db)[skip:]
    if disk:
        steps.append(("disk", lambda: disk_checks(repo_root, db), DISK_SECONDS))
    for name, fn, seconds in steps:
        yield _run_step(name, fn, seconds, bounded)


def checks(repo_root: str | None, db: DB | None = None) -> list[Check]:
    """Run diagnostics, borrowing ``db`` or closing each connection we open."""
    return [c for group in stream(repo_root, db) for c in group]


def disk_checks(repo_root: str | None, db: DB | None = None) -> list[Check]:
    """brindle's disk use by category, what `brindle clean` reclaims, free
    space against ``min_free_disk_gb``, and dead branches. Also keeps
    Spotlight out of brindle's folders on macOS (``disk.keep_out_of_spotlight``).
    Only `brindle doctor` runs it: measuring walks every worktree, too slow
    for the daemon's doctor endpoint."""
    from brindle import config, disk

    try:
        dirs = [config.brindle_home(), *disk.cache_dirs()]
        if repo_root:
            dirs.append(config.worktree_root(repo_root))
        disk.keep_out_of_spotlight(*dirs)
        use_db = db if db is not None else DB()
        with closing(use_db.conn) if db is None else nullcontext():
            report = disk.storage_report(use_db, repo_root)
    except Exception as e:  # noqa: BLE001 - a report, never a reason doctor fails
        return [Check(WARN, "disk", f"couldn't measure brindle's disk use: {e}")]
    parts = ", ".join(f"{c['name']} {disk.human(c['size'])}" for c in report["categories"])
    reclaim = report["reclaimable"]
    out = [Check(WARN if reclaim > disk.GB else OK, "disk use",
                 f"{parts}. `brindle clean --dry-run` lists what `brindle clean` would reclaim "
                 f"(~{disk.human(reclaim)})")]
    if report["low_disk"]:
        out.append(Check(WARN, "free disk", f"{report['low_disk']}: new workers and check runs wait"))
    for state, how in ((disk.MERGED, "`brindle clean` removes them"),
                       (disk.ABANDONED, "`brindle clean --abandoned` (or --worktree PATH) removes them")):
        found = [t for t in report["worktrees"] if t["state"] == state]
        if found:
            shown = "; ".join(f"{t['path']} {disk.human(t['size'])}, last activity "
                              f"{time.strftime('%Y-%m-%d', time.localtime(t['last_activity']))}"
                              + (f" (kept: {t['reason']})" if t["reason"] else "") for t in found[:10])
            more = f" and {len(found) - 10} more" if len(found) > 10 else ""
            out.append(Check(WARN, f"{state} worktrees", f"{shown}{more}: {how}, archiving each branch tip "
                             f"under {disk.ARCHIVE_REF}<branch>"))
    dead = report["dead_branches"]
    if dead:
        names = ", ".join(f"{b['name']} ({'merged' if b['merged'] else 'unmerged'}, last commit "
                          f"{time.strftime('%Y-%m-%d', time.localtime(b['last_commit']))})" for b in dead[:10])
        more = f" and {len(dead) - 10} more" if len(dead) > 10 else ""
        out.append(Check(WARN, "dead branches",
                         f"{names}{more}: `brindle clean --branches` deletes them, keeping each tip "
                         f"under {disk.ARCHIVE_REF}<branch>"))
    return out


def reap_checks(db: DB | None = None) -> list[Check]:
    """What the sweep reaped lately (brindle.reap), and what it would reap now."""
    from brindle import reap

    try:
        reap_db = db if db is not None else DB()
        with closing(reap_db.conn) if db is None else nullcontext():
            reaped = reap.recent(reap_db)
            would = reap.run(reap_db, dry_run=True)
    except Exception as e:  # noqa: BLE001
        return [Check(WARN, "reaped", f"couldn't check: {e}")]
    return [Check(OK, "reaped", "\n".join(reaped[:20]) if reaped else "nothing in the last 7 days"),
            Check(WARN if would else OK, "to reap",
                  "\n".join(would) + "\n(the next sweep does this)" if would else "nothing")]


def routing_checks(cfg, repo_root: str) -> list[Check]:
    """Effective routing per tier, with its config source and missing names."""
    from brindle.config import WEIGHTS
    from brindle.profiles import load_profile

    out = []
    for tier in WEIGHTS:
        names, missing = [], []
        for name in cfg.routing[tier]:
            try:
                load_profile(name, repo_root)
            except KeyError:
                missing.append(name)
            else:
                names.append(name)
        source = cfg.routing_sources[tier]
        detail = f"{' -> '.join(names) or 'none configured'} (from {source})"
        if missing:
            detail += f"; no profile {', '.join(missing)} in {source}, skipped"
        out.append(Check(WARN if missing else OK, f"routing {tier}", detail))
    return out


def native_checks(repo_root: str | None) -> list[Check]:
    """One check per endpoint the native profiles use: reachable, and does
    it have the model. A missing endpoint is a warning (those profiles are
    optional); a profile with no endpoint at all is a failure, since it can
    never run."""
    from brindle.native import runner
    from brindle.profiles import list_profiles

    out: list[Check] = []
    seen: dict[tuple[str, str], list[str]] = {}
    profiles: list = []
    for p in list_profiles(repo_root):
        if p.provider != "native":
            continue
        try:
            ep = runner.endpoint_for(p)
        except ValueError as e:
            out.append(Check(FAIL, f"profile {p.name}", str(e)))
            continue
        seen.setdefault((ep.base_url, ep.model), []).append(p.name)
        profiles.append((p, ep))
    reachable: set[tuple[str, str]] = set()
    for (base_url, model), names in seen.items():
        ep = runner.Endpoint(base_url, model)
        ok, detail = runner.probe(ep)
        who = ", ".join(names)
        if ok:
            reachable.add((base_url, model))
            out.append(Check(OK if "is available" in detail else WARN, f"model {model}", f"{base_url}: {detail} (for {who})"))
        else:
            out.append(Check(WARN, f"model {model}",
                             f"{base_url} {detail}: only needed for {who}. For Ollama: "
                             f"`ollama serve`, then `ollama pull {model}`"))
    # Ollama silently drops the start of a conversation that outgrows its context.
    # Group profiles by endpoint to avoid duplicate warnings.
    endpoint_profiles: dict[tuple[str, str], list[tuple[object, runner.Endpoint]]] = {}
    for p, ep in profiles:
        if (ep.base_url, ep.model) not in reachable or not p.context_tokens or not runner.is_ollama(ep):
            continue
        endpoint_profiles.setdefault((ep.base_url, ep.model), []).append((p, ep))
    
    for (base_url, model), profile_eps in endpoint_profiles.items():
        # Find the maximum context_tokens among all profiles using this endpoint
        max_context_tokens = max(p.context_tokens for p, _ in profile_eps if p.context_tokens)
        fix = f"OLLAMA_CONTEXT_LENGTH={max_context_tokens + 8192} ollama serve"
        ep = profile_eps[0][1]  # Use the first profile's endpoint for context check
        ctx = runner.server_context(ep)
        profile_names = ", ".join(p.name for p, _ in profile_eps)
        if ctx is None:
            out.append(Check(WARN, f"context {profile_names}",
                             f"{ep.base_url} doesn't report its context length; Ollama truncates "
                             f"silently past it, so it must be at least {max_context_tokens} "
                             f"(context_tokens): {fix}"))
        elif ctx < max_context_tokens:
            out.append(Check(WARN, f"context {profile_names}",
                             f"{ep.base_url} runs {ep.model} with a {ctx}-token context, less than "
                             f"the profile's context_tokens ({max_context_tokens}); Ollama truncates "
                             f"silently. Restart with: {fix}"))
    return out


def override_checks() -> list[Check]:
    """A profile in ~/.brindle/agents named like a built-in hides it. If the
    built-in has an ``allowed_tools`` list and the copy has none, its agents
    lose that allowlist and stop on prompts (or refusals) the built-in avoids."""
    from importlib import resources

    from brindle import profiles
    from brindle.config import user_profiles_dir

    out = []
    d = user_profiles_dir()
    for f in sorted(d.glob("*.md")) if d.is_dir() else []:
        builtin = resources.files("brindle.builtin_agents").joinpath(f.name)
        if not builtin.is_file():
            continue
        try:
            mine = profiles._load(f.stem, None)
            theirs = profiles._build(*profiles._frontmatter(builtin.read_text(encoding="utf-8")), f.stem)
        except (KeyError, profiles.ProfileError):
            continue
        if theirs.allowed_tools and not mine.allowed_tools:
            out.append(Check(WARN, f"profile {f.stem}",
                             f"{f} hides the built-in {f.stem}, and has no allowed_tools while the "
                             "built-in does: its agents get no allowlist. Copy the built-in's "
                             "allowed_tools into it, or delete the file to use the built-in"))
    return out


def airgap_checks(repo_root: str | None) -> list[Check]:
    """Air-gap mode (brindle Enterprise): whether it's on and licensed, the
    configured profiles it refuses (hosted providers), and whether the
    offline policy file and license are there. One ``ok`` line when off."""
    from brindle import airgap, config
    from brindle.pro import license

    cfg = None
    if repo_root:
        try:
            cfg = config.load_repo_config(repo_root)
        except ValueError:
            cfg = None                 # the repo config check reports the bad file
    if not airgap.enabled(cfg):
        return [Check(OK, "air-gap", "off")]
    out: list[Check] = []
    warning = airgap.warning()
    if warning:
        out.append(Check(WARN, "air-gap", f"on via {airgap.source(cfg)}; {warning}"))
    else:
        out.append(Check(OK, "air-gap", f"on via {airgap.source(cfg)}: no outbound traffic, "
                                        "local models only"))
    hosted = airgap.hosted_profiles(repo_root)
    if hosted:
        out.append(Check(WARN, "hosted profiles",
                         f"{', '.join(hosted)}: refused in air-gap mode. Only a native profile "
                         "on a loopback or private-network base_url (or one marked `local: "
                         "true`) can run; point default_agent, routing and reviewer at one"))
    else:
        out.append(Check(OK, "hosted profiles", "none (every profile is local)"))
    # The chat itself is an agent: `brindle` won't start on a hosted default_agent.
    default = (cfg.default_agent if cfg else None) or config.RepoConfig().default_agent
    ok, why = airgap.check_profile(default, repo_root)
    if ok:
        out.append(Check(OK, "default agent", f"{default} (local: `brindle` can start)"))
    else:
        out.append(Check(FAIL, "default agent",
                         f"{default}: `brindle` won't start in air-gap mode, since the chat is a "
                         f"hosted agent ({why.split(': ', 2)[-1]}). Set default_agent in "
                         ".brindle/config.json to a local native profile"))
    try:
        ent = license.installed()
    except license.LicenseError as e:
        out.append(Check(FAIL, "offline license", f"{e}; reinstall with "
                                                  "`brindle account license install <file>`"))
    else:
        if ent is None:
            out.append(Check(WARN, "offline license",
                             "none installed (`brindle account license install <file>`); "
                             "nothing is entitled while the network is off"))
        else:
            out.append(Check(OK, "offline license",
                             f"org {ent.org_id}, plan {ent.plan}, features "
                             f"{', '.join(sorted(ent.features)) or '-'}"
                             + (" (expired; in grace)" if ent.in_grace else "")))
    if repo_root:
        path = airgap.policy_path(repo_root)
        if path.is_file():
            out.append(Check(OK, "offline policy", str(path)))
        else:
            out.append(Check(WARN, "offline policy",
                             f"none at {path}: with a Team license, delegations and merges are "
                             "refused until the org's policy is put there"))
    return out


def tmux_cwd_checks() -> list[Check]:
    """Whether the tmux server brindle uses sits in a deleted directory (see
    tmux.cwd_gone). brindle's own panes change directory themselves, but any
    other new window on that server starts in the dead copy."""
    from brindle import tmux

    try:
        gone = tmux.cwd_gone(tmux.current_server())
    except tmux.TmuxError:
        return []
    if not gone:
        return []
    return [Check(WARN, "tmux server directory",
                  "the tmux server's working directory was deleted, so new windows outside brindle start in "
                  "a directory that no longer exists. Restart the server (`tmux kill-server`) when no agents run.")]


def agent_env_checks(repo_root: str | None) -> list[Check]:
    """What the agent environment allowlist took out of this repo's last
    agent launch (agent_env_mode "enforce") or would have ("report"):
    names only (see secrets.record_env)."""
    from brindle import secrets

    found = secrets.env_report()["repos"].get(str(repo_root)) if repo_root else None
    if not found:
        return []
    names = found.get("names") or []
    if found.get("mode") == "enforce":
        return [Check(OK, "agent environment",
                      f"enforced: the last launch dropped {', '.join(names)}; add any an agent needs to "
                      "agent_env in .brindle/config.json" if names else "enforced: nothing dropped")]
    if not names:
        return [Check(OK, "agent environment", "report mode: the allowlist would drop nothing")]
    return [Check(WARN, "agent environment",
                  f"report mode: enforcing the allowlist would drop {', '.join(names)} from the last "
                  "agent launch. Add any an agent needs to agent_env in .brindle/config.json, then set "
                  "agent_env_mode to \"enforce\"")]


def codex_hook_checks(repo_root: str) -> list[Check]:
    """With the permission policy on and Codex installed: whether Codex will
    run brindle's hook (see brindle.codex_hook). Only reads; changes nothing."""
    from brindle import codex_hook, providers

    if not codex_hook.policy_on(repo_root):
        return []
    exe = providers.codex_binary()
    from brindle.providers import which_cli

    if not (which_cli(exe) or os.path.isfile(exe)):
        return []
    state = codex_hook.status()
    if state == codex_hook.TRUSTED:
        return [Check(OK, "codex hook", "trusted: Codex workers follow the permission policy")]
    if state == codex_hook.REMOVED:
        return [Check(WARN, "codex hook", "its trust was removed from Codex's config, so Codex "
                      f"workers ask for approvals themselves. `{codex_hook.INSTALL}` restores it")]
    return [Check(OK, "codex hook", "brindle trusts it on the next Codex launch"
                  + (" (brindle moved since the last trust)" if state == codex_hook.MOVED else ""))]


def pro_checks() -> list[Check]:
    """One line on the brindle Pro plan, from the stored entitlement (no network).
    Never a warning: brindle is complete without a plan."""
    from brindle import airgap

    if airgap.enabled():
        return []      # airgap_checks reports the offline license
    try:
        from brindle.pro import license

        ent = license.current(refresh=False)
    except Exception:  # noqa: BLE001 - no plan, for whatever reason
        return [Check(OK, "brindle Pro", "not logged in; `brindle account` shows what the paid plans add")]
    return [Check(OK, "brindle Pro", f"plan {ent.plan}, features {', '.join(sorted(ent.features)) or '-'}"
                  + (" (offline grace)" if ent.in_grace else "") + "; details: `brindle account`")]


def managed_checks(repo_root: str | None) -> list[Check]:
    """One line on the org's managed models (Enterprise), when there are any:
    the provider workers run on, or why the policy can't be read (every
    launch is refused until it can). Nothing without the feature."""
    try:
        from brindle.pro import managed_models

        line = managed_models.doctor_line(repo_root)
    except Exception:  # noqa: BLE001 - no plan, for whatever reason
        return []
    if line is None:
        return []
    ok, detail = line
    return [Check(OK if ok else FAIL, "managed models", detail)]


def quota_checks(repo_root: str | None) -> list[Check]:
    """One line per provider that has quota data (a warning past 90% used or
    while limited). The local model server has its own checks above, and an
    installed CLI's quota is on its sign-in line."""
    from brindle import quota

    out = []
    shown = set(signin_providers())
    for p in quota.PROVIDERS:
        if p == "native" or p in shown:
            continue
        n = quota.note(p)
        if n:
            out.append(Check(WARN if quota.headroom(p) <= 10 else OK, f"{p} quota", n))
    return out


def daemon_checks(home: Path | None = None) -> list[Check]:
    """Whether the daemon is running, from the pid in its ``daemon.json``, and
    its TCP address when it also listens there (``--tcp``)."""
    import json

    from brindle import config, procs

    path = (home or config.brindle_home()) / "daemon.json"
    try:
        info = json.loads(path.read_text())
        pid = info.get("pid")
    except (OSError, ValueError, AttributeError):
        return [Check(OK, "daemon", "not running")]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return [Check(WARN, "daemon", f"{path} has no pid; restart the daemon to rewrite it")]
    if procs.alive(pid):
        tcp = f", tcp 127.0.0.1:{info['port']}" if info.get("port") else ""
        return [Check(OK, "daemon", f"running (pid {pid}{tcp})")]
    return [Check(WARN, "daemon", f"not running: {path} is left from pid {pid}, which is gone")]


MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


def is_optional(c: Check) -> bool:
    """A warning about something only some setups use (Codex, a local
    model, gh, ...): listed apart, so a fresh install doesn't look broken."""
    return c.level == WARN and "only needed" in c.detail


def render(results: list[Check]) -> str:
    width = max(len(c.name) for c in results) if results else 0
    main = [c for c in results if not is_optional(c)]
    optional = [c for c in results if is_optional(c)]
    lines = [f"{MARK[c.level]} {c.name.ljust(width)}  {c.detail}" for c in main]
    if optional:
        lines.append("\nOptional, not set up (brindle works without them):")
        lines.extend(f"- {c.name.ljust(width)}  {c.detail}" for c in optional)
    lines.append(summary(results))
    return "\n".join(lines)


def summary(results: list[Check]) -> str:
    """The closing line: how many problems and warnings (optional tools aside)."""
    main = [c for c in results if not is_optional(c)]
    fails = sum(c.level == FAIL for c in main)
    warns = sum(c.level == WARN for c in main)
    if fails:
        return f"\n{fails} problem(s) will stop brindle from working; {warns} warning(s)."
    if warns:
        return f"\nbrindle can run; {warns} warning(s) worth a look."
    return "\nAll good."


def render_line(c: Check, width: int = 22) -> str:
    """One check as `brindle doctor` prints it the moment it finishes."""
    if is_optional(c):
        return f"- {c.name.ljust(width)}  {c.detail}  (optional)"
    return f"{MARK[c.level]} {c.name.ljust(width)}  {c.detail}"
