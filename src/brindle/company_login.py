"""Company login: apply the org policy's ``agent_setup`` on this machine.

After ``brindle account login`` (and on ``brindle doctor --fix``), when the
org's policy has an ``agent_setup`` block, this signs the person in to the
route the org chose for Claude and Codex, using each CLI's own login command
(brindle never reads or stores a Claude.ai or ChatGPT token), stores the
Claude Code environment for brindle's panes in ``~/.brindle/config.json``
(never the shell), and checks that the pinned models can be called.

Every external command is an argv list (no shell) with a timeout. They all go
through :func:`run_cmd` and :func:`which`, which tests replace.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from brindle import config

log = logging.getLogger(__name__)

ENV_KEY = "company_claude_env"          # ~/.brindle/config.json: {"org": id, "env": {NAME: value}}
CHECK_TIMEOUT = 30.0
LOGIN_TIMEOUT = 600.0

INSTALL = {
    "aws": "brew install awscli  (https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)",
    "gcloud": "brew install --cask google-cloud-sdk  (https://cloud.google.com/sdk/docs/install)",
    "az": "brew install azure-cli  (https://learn.microsoft.com/cli/azure/install-azure-cli)",
    "claude": "npm install -g @anthropic-ai/claude-code",
    "codex": "npm install -g @openai/codex",
}


def which(name: str) -> str | None:
    return shutil.which(name)


def run_cmd(argv: list[str], timeout: float = CHECK_TIMEOUT, interactive: bool = False,
            cwd: str | None = None, env=None, stdin: str | None = None):
    """Run ``argv`` (no shell) with a timeout; (returncode, stdout+stderr). A
    login command is ``interactive``: it keeps the terminal. A command that
    can't run or times out is returncode 127 or 124."""
    try:
        if interactive:
            proc = subprocess.run(argv, timeout=timeout, check=False)
            return proc.returncode, ""
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                              cwd=cwd, env=env, input=stdin)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)


# -- the AWS SSO profile -------------------------------------------------------------------

def aws_config_path() -> Path:
    return Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config")


def profile_name(org_id: str) -> str:
    return "brindle-" + re.sub(r"[^A-Za-z0-9_-]", "-", org_id)


def _section_header(line: str) -> str | None:
    m = re.match(r"\s*\[(.*?)\]", line)
    return " ".join(m.group(1).split()) if m else None


def write_aws_profile(path: Path, name: str, aws) -> None:
    """Write (or refresh) ``[profile name]`` in the AWS config at ``path``,
    leaving every other line as it was; atomically."""
    body = [f"[profile {name}]",
            f"sso_start_url = {aws.sso_start_url}",
            f"sso_region = {aws.sso_region}",
            f"sso_account_id = {aws.account_id}",
            f"sso_role_name = {aws.role_name}",
            f"region = {aws.region}"]
    try:
        text = path.read_text(encoding="utf-8")
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        text, mode = "", 0o600
    lines = text.splitlines()
    out: list[str] = []
    replaced = False
    i = 0
    while i < len(lines):
        header = _section_header(lines[i])
        if header is not None and header == f"profile {name}":
            j = i + 1
            while j < len(lines) and _section_header(lines[j]) is None:
                j += 1
            if not replaced:
                out.extend(body)
                if j < len(lines):
                    out.append("")
                replaced = True
            i = j
            continue
        out.append(lines[i])
        i += 1
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.extend(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- the Claude Code environment for panes -------------------------------------------------

def claude_env(org_id: str, claude) -> dict[str, str]:
    """The variables a Claude Code pane needs for the org's route."""
    env: dict[str, str] = {}
    if claude.route == "bedrock":
        env.update(CLAUDE_CODE_USE_BEDROCK="1", AWS_PROFILE=profile_name(org_id),
                   AWS_REGION=claude.aws.region)
    elif claude.route == "vertex":
        env.update(CLAUDE_CODE_USE_VERTEX="1", ANTHROPIC_VERTEX_PROJECT_ID=claude.gcp.project,
                   CLOUD_ML_REGION=claude.gcp.region)
    elif claude.route == "foundry":
        env.update(CLAUDE_CODE_USE_FOUNDRY="1", ANTHROPIC_FOUNDRY_RESOURCE=claude.azure.resource)
    elif claude.route == "gateway":
        env["ANTHROPIC_BASE_URL"] = claude.gateway.base_url
    m = claude.models
    if m is not None:
        for tier, model in (("OPUS", m.opus), ("SONNET", m.sonnet), ("HAIKU", m.haiku)):
            if model:
                env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = model
    return env


# The only names stored or handed to panes: the config file is editable (and
# syncable), so it must not become a way to set arbitrary variables (PATH, ...).
ENV_NAMES = frozenset({
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_PROFILE", "AWS_REGION", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
    "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_BASE_URL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"})


def store_env(org_id: str, env: dict[str, str], self_serve: bool = False) -> None:
    env = {k: v for k, v in env.items() if k in ENV_NAMES}
    got = {"org": org_id, "env": env}
    if self_serve:
        got["self_serve"] = True
    config.set_user(ENV_KEY, got if env else None)


def clear_env(force: bool = False) -> None:
    """Forget the stored env (logout, or an org with no agent_setup), so a
    stale route can't keep sending panes to the previous org's backend. A
    self-serve env (the person's own choice, not an org's) stays unless ``force``."""
    try:
        got = config.user_settings().get(ENV_KEY)
        if got is not None and (force or not (isinstance(got, dict) and got.get("self_serve"))):
            config.set_user(ENV_KEY, None)
    except (OSError, ValueError):
        pass


def _current_org() -> str | None:
    """The org of the locally cached entitlement (no network), or None."""
    from brindle.pro import license

    try:
        return license.current(refresh=False).org_id
    except Exception:  # noqa: BLE001 - no usable entitlement: no org
        return None


def stored_env() -> dict[str, str]:
    """The company's Claude Code environment stored by company login, for
    ``agents.agent_env``. Empty when none, or when Claude Code's own managed
    settings pick the backend and credential (they take precedence)."""
    from brindle import providers

    try:
        got = config.user_settings().get(ENV_KEY)
    except (OSError, ValueError):
        return {}
    env = got.get("env") if isinstance(got, dict) else None
    if not isinstance(env, dict) or providers.claude_org_managed():
        return {}
    if not got.get("self_serve") and got.get("org") != _current_org():
        log.warning("the stored company agent setup is for another org; run `brindle login`")
        return {}
    return {k: v for k, v in env.items() if k in ENV_NAMES and isinstance(v, str)}


# -- sign-in per route ---------------------------------------------------------------------

class Login:
    """One company login run: prints through ``say``."""

    def __init__(self, org_id: str, setup, say: Callable[[str], None], repo_root: str | None = None,
                 aws_config: Path | None = None, self_serve: bool = False) -> None:
        self.org_id, self.setup, self.say = org_id, setup, say
        self.self_serve = self_serve
        self.repo_root = repo_root or os.getcwd()
        self.aws_config = aws_config

    def _need(self, tool: str) -> bool:
        if which(tool):
            return True
        self.say(f"{tool} isn't installed; install it with: {INSTALL[tool]}")
        return False

    def _ensure(self, tool: str, check: list[str], login: list[str], what: str) -> bool:
        """Sign in with ``login`` unless ``check`` says already signed in."""
        if run_cmd(check)[0] == 0:
            self.say(f"{what}: already signed in")
            return True
        self.say(f"{what}: signing in ({' '.join(login)})")
        if run_cmd(login, LOGIN_TIMEOUT, interactive=True)[0] != 0:
            self.say(f"{what}: sign-in didn't finish; run `{' '.join(login)}` and try again")
            return False
        return True

    def run(self) -> bool:
        """Sign in, store the env, check model access. True when everything is ready."""
        ok = True
        if self.setup.claude is not None:
            ok = self._claude(self.setup.claude) and ok
        if self.setup.codex is not None:
            ok = self._codex(self.setup.codex) and ok
        return ok

    def _claude(self, c) -> bool:
        if not self._claude_signin(c):
            # Not signed in to the new route: keep no env, rather than panes
            # on the previous org's route (or on this one, unsigned).
            clear_env(force=True)
            return False
        store_env(self.org_id, claude_env(self.org_id, c), self.self_serve)
        return self._model_check(c)

    def _claude_signin(self, c) -> bool:
        r = c.route
        if r == "bedrock":
            if not self._need("aws"):
                return False
            name = profile_name(self.org_id)
            try:
                write_aws_profile(self.aws_config or aws_config_path(), name, c.aws)
            except OSError as e:
                self.say(f"couldn't write the AWS profile {name}: {e}")
                return False
            return self._ensure("aws", ["aws", "sts", "get-caller-identity", "--profile", name],
                                ["aws", "sso", "login", "--profile", name], f"AWS profile {name}")
        if r == "vertex":
            if not self._need("gcloud"):
                return False
            ok = self._ensure("gcloud", ["gcloud", "auth", "application-default", "print-access-token"],
                              ["gcloud", "auth", "application-default", "login"], "Google Cloud")
            run_cmd(["gcloud", "config", "set", "project", c.gcp.project])
            return ok
        if r == "foundry":
            if not self._need("az"):
                return False
            ok = self._ensure("az", ["az", "account", "show"], ["az", "login"], "Azure")
            if ok:
                run_cmd(["az", "account", "set", "--subscription", c.azure.subscription_id])
            return ok
        if r in ("subscription", "console"):
            if not self._need("claude"):
                return False
            code, out = run_cmd(["claude", "auth", "status"])
            if code == 0 and c.org_id and c.org_id.lower() in out.lower():
                self.say("Claude: already signed in to your organization")
                return True
            login = ["claude", "auth", "login", "--sso" if r == "subscription" else "--console"]
            self.say(f"Claude: signing in ({' '.join(login)})")
            if run_cmd(login, LOGIN_TIMEOUT, interactive=True)[0] != 0:
                self.say(f"Claude: sign-in didn't finish; run `{' '.join(login)}` and try again")
                return False
            return True
        return True     # gateway: nothing to sign in

    def _codex(self, c) -> bool:
        if c.route == "chatgpt":
            if not self._need("codex"):
                return False
            return self._ensure("codex", ["codex", "login", "status"], ["codex", "login"], "Codex")
        if c.route == "api_key":
            self.say("Codex: sign in with your key: run `codex login --with-api-key`")
        return True     # azure: the existing openai-compatible provider handling

    def _where(self, c) -> str:
        region = {"bedrock": c.aws.region if c.aws else None,
                  "vertex": c.gcp.region if c.gcp else None,
                  "foundry": c.azure.resource if c.azure else None}.get(c.route)
        return f"{c.route} ({region})" if region else c.route

    def _model_check(self, c) -> bool:
        models = [m for m in ((c.models.opus, c.models.sonnet, c.models.haiku) if c.models else ()) if m]
        binary = which("claude")
        env = {**os.environ, **claude_env(self.org_id, c)}
        if models and not binary:
            self.say(f"claude isn't installed; couldn't check model access (install: {INSTALL['claude']})")
            return False
        # Probe from an empty directory: a repo's own .claude settings and hooks
        # must not run while we sign in.
        with tempfile.TemporaryDirectory(prefix="brindle-probe-") as scratch:
            failed = self._probe_models(binary, models, scratch, env)
        if not failed:
            owner = "your" if self.self_serve else f"{self.org_id}'s"
            self.say(f"Ready: Claude on {owner} {self._where(c)}")
        return not failed

    def _probe_models(self, binary, models, scratch: str, env) -> bool:
        """Probe each model; True when any failed (the reasons are printed)."""
        from brindle import model_access

        failed = False
        if models:
            def run(argv, **kw):
                code, out = run_cmd(argv, kw.get("timeout", CHECK_TIMEOUT), cwd=kw.get("cwd"),
                                    env=kw.get("env"), stdin=kw.get("input"))
                if code == 124:
                    raise subprocess.TimeoutExpired(argv, kw.get("timeout", CHECK_TIMEOUT))
                if code == 127:
                    raise OSError(out)
                return subprocess.CompletedProcess(argv, code, out, "")
            for model in models:
                ok, cause, text = model_access.probe(binary, model, scratch, env, run=run)
                if ok:
                    continue
                failed = True
                self.say(f"Claude model {model}: " + (cause.line() if cause else
                                                      (text or "couldn't be called")))
        return failed


def apply(org_id: str, setup, say: Callable[[str], None], repo_root: str | None = None,
          self_serve: bool = False) -> bool:
    """Run company login for ``setup`` (an ``AgentSetup``); never raises."""
    try:
        return Login(org_id, setup, say, repo_root, self_serve=self_serve).run()
    except Exception as e:  # noqa: BLE001 - company login must not fail the login it follows
        log.debug("company login failed", exc_info=True)
        say(f"company login: {e}")
        return False


UNAVAILABLE = object()      # org_setup: the entitlement or policy couldn't be had (offline, ...)


def org_setup(repo_root: str | None = None, store=None, client=None):
    """(org id, AgentSetup) for the signed-in team org; None when the policy
    was had and there is no ``agent_setup`` (or no team entitlement);
    ``UNAVAILABLE`` when it couldn't be had, which says nothing about the setup."""
    from brindle.pro import license, team_policy

    try:
        ent = license.current(store=store, client=client)
        if team_policy.FEATURE not in ent.features:
            return None
        p = team_policy.current_policy(ent, client, store, repo_root)
    except Exception:  # noqa: BLE001 - offline, logged out, unparsable: leave things as they are
        log.debug("no org policy for company login", exc_info=True)
        return UNAVAILABLE
    return (ent.org_id, p.agent_setup) if p.agent_setup is not None else None


def _logged_in(store=None, client=None) -> bool:
    from brindle.pro import license

    try:
        license.current(refresh=False, store=store, client=client)
        return True
    except Exception:  # noqa: BLE001 - no usable entitlement
        return False


def apply_current(say: Callable[[str], None], repo_root: str | None = None, store=None,
                  client=None, ask=None, info=None) -> bool | None:
    """Apply the signed-in org's agent setup, if it has one (None: it has none,
    or it couldn't be read; the stored env is cleared only in the first case).
    With no org setup, the person's own self-serve setup applies, or is offered
    (``ask``; see ``self_serve``). An org's setup always wins over it."""
    from brindle import self_serve

    got = org_setup(repo_root, store, client)
    if got is UNAVAILABLE:
        if _logged_in(store, client):
            return None     # an org whose policy we couldn't read: leave everything as it is
        return self_serve.run(say, repo_root, ask, info)
    if got is None:
        clear_env()
        return self_serve.run(say, repo_root, ask, info)
    return apply(got[0], got[1], say, repo_root)


__all__ = ["apply", "apply_current", "claude_env", "stored_env", "write_aws_profile"]
