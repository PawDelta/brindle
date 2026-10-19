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
import sys
import tempfile
from pathlib import Path
from typing import Callable

from brindle import cloud_credential, config

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

# What to do instead on Linux (and WSL), where there is no brew and a global
# npm install into a root-owned prefix fails with EACCES. INSTALL stays the
# command brindle itself runs.
NPM_PREFIX_NOTE = "on Linux, if that fails with EACCES: `npm config set prefix ~/.local`, then retry (no sudo)"
INSTALL_LINUX = {
    "aws": "Linux: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html "
           "(or `sudo snap install aws-cli --classic`)",
    "gcloud": "Linux: `sudo snap install google-cloud-cli --classic` (or https://cloud.google.com/sdk/docs/install)",
    "az": "Linux: `curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash` "
          "(https://learn.microsoft.com/cli/azure/install-azure-cli-linux)",
    "claude": NPM_PREFIX_NOTE,
    "codex": NPM_PREFIX_NOTE,
}


def install_hint(tool: str) -> str:
    """How to install ``tool``, with the Linux alternative appended on Linux."""
    hint = INSTALL[tool]
    if sys.platform.startswith("linux"):
        hint += f"  [{INSTALL_LINUX[tool]}]"
    return hint


def which(name: str) -> str | None:
    return shutil.which(name)


def run_cmd(argv: list[str], timeout: float = CHECK_TIMEOUT, interactive: bool = False,
            cwd: str | None = None, env=None, stdin: str | None = None):
    """Run ``argv`` (no shell) with a timeout; (returncode, stdout+stderr). A
    login command is ``interactive``: it keeps the terminal. A command that
    can't run or times out is returncode 127 or 124."""
    try:
        if interactive:
            proc = subprocess.run(argv, timeout=timeout, check=False, env=env)
            return proc.returncode, ""
        # Never hand a probe the terminal: `claude` turns newline translation
        # back on when it exits, which shifts the curses sidebar's drawing.
        feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                              cwd=cwd, env=env, **feed)
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


def write_aws_profile(path: Path, name: str, aws, credential_process: str | None = None) -> None:
    """Write (or refresh) ``[profile name]`` in the AWS config at ``path``,
    leaving every other line as it was; atomically. With ``credential_process``
    (a federated org) the profile runs that command instead of using SSO."""
    if credential_process is not None:
        body = [f"[profile {name}]", f"credential_process = {credential_process}", f"region = {aws.region}"]
    else:
        body = [f"[profile {name}]",
                f"sso_start_url = {aws.sso_start_url}",
                f"sso_region = {aws.sso_region}",
                f"sso_account_id = {aws.account_id}",
                f"sso_role_name = {aws.role_name}",
                f"region = {aws.region}"]
    _edit_profile(path, name, body)


def remove_aws_profile(path: Path, name: str) -> None:
    """Remove ``[profile name]`` from the AWS config at ``path``, if it is there."""
    if path.exists():
        _edit_profile(path, name, None)


def _edit_profile(path: Path, name: str, body: list[str] | None) -> None:
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
            if not replaced and body is not None:
                out.extend(body)
                if j < len(lines):
                    out.append("")
            replaced = True
            i = j
            continue
        out.append(lines[i])
        i += 1
    if body is None:
        while out and not out[-1].strip():
            out.pop()
    elif not replaced:
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
    """The variables a Claude Code pane needs for the org's route (a
    ``ClaudeSetup``): the one env builder, for ``brindle login``'s stored env
    and for an enforced route (brindle.pro.managed_models). Bedrock without
    the SSO group sets no ``AWS_PROFILE``: the machine's own AWS sign-in
    applies. ``default_model`` is ``ANTHROPIC_MODEL``; with no ``models``
    aliases and several ``model_ids``, the last id stands in for Haiku, as
    the ``provider_config`` it replaces had it."""
    env: dict[str, str] = {}
    if claude.route == "bedrock":
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        if claude.aws.sso or claude.federated:
            env["AWS_PROFILE"] = profile_name(org_id)
        env["AWS_REGION"] = claude.aws.region
    elif claude.route == "vertex":
        env.update(CLAUDE_CODE_USE_VERTEX="1", ANTHROPIC_VERTEX_PROJECT_ID=claude.gcp.project,
                   CLOUD_ML_REGION=claude.gcp.region)
        if claude.federated:
            env.update(GOOGLE_APPLICATION_CREDENTIALS=str(cloud_credential.gcp_config_path(org_id)),
                       GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES="1")
    elif claude.route == "foundry":
        env["CLAUDE_CODE_USE_FOUNDRY"] = "1"
        if claude.azure.resource:
            env["ANTHROPIC_FOUNDRY_RESOURCE"] = claude.azure.resource
        if claude.azure.base_url:
            env["ANTHROPIC_FOUNDRY_BASE_URL"] = claude.azure.base_url
        if claude.federated:
            fed = claude.azure.federation
            env.update(AZURE_CLIENT_ID=fed.client_id, AZURE_TENANT_ID=fed.tenant_id,
                       AZURE_FEDERATED_TOKEN_FILE=str(cloud_credential.azure_token_path(org_id)))
    elif claude.route == "gateway":
        env["ANTHROPIC_BASE_URL"] = claude.gateway.base_url
    if claude.default_model:
        env["ANTHROPIC_MODEL"] = claude.default_model
    m = claude.models
    if m is not None:
        for tier, model in (("OPUS", m.opus), ("SONNET", m.sonnet), ("HAIKU", m.haiku)):
            if model:
                env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = model
    elif len(claude.model_ids) > 1:
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = claude.model_ids[-1]
    return env


# The only names stored or handed to panes: the config file is editable (and
# syncable), so it must not become a way to set arbitrary variables (PATH, ...).
ENV_NAMES = frozenset({
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_PROFILE", "AWS_REGION", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
    "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    # a federated org's cloud hooks (cloud_credential)
    "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES",
    "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE",
    "GOOGLE_GEMINI_BASE_URL"})


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
                 aws_config: Path | None = None, self_serve: bool = False,
                 store=None, client=None) -> None:
        self.org_id, self.setup, self.say = org_id, setup, say
        self.self_serve = self_serve
        self.store, self.client = store, client
        self.repo_root = repo_root or os.getcwd()
        self.aws_config = aws_config

    def _need(self, tool: str) -> bool:
        if which(tool):
            return True
        self.say(f"{tool} isn't installed; install it with: {install_hint(tool)}")
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
        self._adopt()
        return self._model_check(c)

    def _adopt(self) -> None:
        """Agents launched while no one was signed in to brindle become the
        account's that signed in now, under the Claude identity its route
        gives (``signin_pause.adopt``), instead of locking. Never raises."""
        try:
            from brindle import signin_pause

            got = signin_pause.check("claude", cache=False)
            if got is None or got[0].kind != "ok" or not got[0].ident or got[0].problem:
                return
            signin_pause.adopt(got[0].ident, signed_in=_logged_in(self.store, self.client))
        except Exception:  # noqa: BLE001 - a sign-in never fails over this
            log.debug("couldn't adopt the signed-out agents", exc_info=True)

    def _federated_signin(self, c) -> bool:
        """Write the cloud's hook (no cloud CLI, no cloud login) and prove it
        with one real exchange, which also warms the credential cache."""
        r, cloud = c.route, {"bedrock": "aws", "vertex": "gcp", "foundry": "azure"}[c.route]
        try:
            if r == "bedrock":
                name = profile_name(self.org_id)
                write_aws_profile(self.aws_config or aws_config_path(), name, c.aws,
                                  cloud_credential.hook_command("aws", self.org_id))
            elif r == "vertex":
                cloud_credential.write_gcp_config(self.org_id, c.gcp.federation,
                                                  cloud_credential.hook_command("gcp", self.org_id))
        except (OSError, cloud_credential.CredentialError) as e:
            self.say(f"couldn't set up federated {cloud} credentials: {e}")
            return False
        why = cloud_credential.check(cloud, self.org_id, store=self.store, client=self.client)
        if why:
            self.say(f"federated {cloud} credentials: {why}")
            return False
        self.say(f"federated {cloud} credentials: ready (short-lived, through PawDelta)")
        return True

    def _claude_signin(self, c) -> bool:
        r = c.route
        if c.federated and r in ("bedrock", "vertex", "foundry"):
            return self._federated_signin(c)
        if r == "bedrock":
            if not self._need("aws"):
                return False
            if not c.aws.sso:
                # No SSO group: the org leaves the AWS sign-in to the person, so
                # there is no profile to write or log in to; only check it answers.
                if run_cmd(["aws", "sts", "get-caller-identity", "--region", c.aws.region])[0] == 0:
                    self.say("AWS: already signed in (your own AWS sign-in applies)")
                    return True
                self.say("AWS: not signed in; sign in to AWS the way you usually do (for example "
                         "`aws sso login` or `aws configure`), then run `brindle login` again")
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
            if ok and c.azure.subscription_id:
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
        return True     # azure, gateway: panes get the endpoint (OPENAI_BASE_URL); no sign-in here

    def _where(self, c) -> str:
        region = {"bedrock": c.aws.region if c.aws else None,
                  "vertex": c.gcp.region if c.gcp else None,
                  "foundry": (c.azure.resource or c.azure.base_url) if c.azure else None}.get(c.route)
        return f"{c.route} ({region})" if region else c.route

    def _model_check(self, c) -> bool:
        pinned = (c.models.opus, c.models.sonnet, c.models.haiku) if c.models else ()
        models = list(dict.fromkeys(m for m in (*pinned, c.default_model) if m))
        binary = which("claude")
        env = {**os.environ, **claude_env(self.org_id, c)}
        if models and not binary:
            self.say(f"claude isn't installed; couldn't check model access (install: {install_hint('claude')})")
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


def clear_federation(aws_config: Path | None = None, keep: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Forget federated cloud credentials (logout, a signed-out ``doctor --fix``):
    every ``federation/<org>/`` directory (but those of ``keep``) and the AWS
    profile that ran its hook. Returns the org ids cleared."""
    path = aws_config or aws_config_path()
    orgs = cloud_credential.clear_all(keep)
    for org in orgs:
        try:
            remove_aws_profile(path, profile_name(org))
        except OSError:
            log.debug("couldn't remove the AWS profile for %s", org, exc_info=True)
    return orgs


def apply(org_id: str, setup, say: Callable[[str], None], repo_root: str | None = None,
          self_serve: bool = False, store=None, client=None) -> bool:
    """Run company login for ``setup`` (an ``AgentSetup``); never raises."""
    try:
        return Login(org_id, setup, say, repo_root, self_serve=self_serve, store=store, client=client).run()
    except Exception as e:  # noqa: BLE001 - company login must not fail the login it follows
        log.debug("company login failed", exc_info=True)
        say(f"company login: {e}")
        return False


UNAVAILABLE = object()      # org_setup: the entitlement or policy couldn't be had (offline, ...)


def claude_model(model: str | None, repo_root: str | None = None) -> str | None:
    """The model a Claude worker can use on the company's route.

    A served profile model wins; otherwise the route's default wins over
    the profile's CLI flag, just as it does in the company login env.
    Launch and policy checks share this resolution without changing the
    saved profile. Claude Code's own managed settings pick their own route.
    An unavailable setup leaves the profile alone, as company login does,
    and so does a launch the route's env doesn't reach (``route_in_use``):
    a route model id on the person's own Anthropic sign-in doesn't exist.
    """
    from brindle import providers

    if providers.claude_org_managed():
        return model
    got = org_setup(repo_root)
    if got is None or got is UNAVAILABLE:
        return model
    c = got[1].claude
    if c is None or not route_in_use(got[0], got[1]):
        return model
    return route_model(c, model)


def route_model(c, model: str | None) -> str | None:
    """The model a Claude pane runs on route ``c`` (a ``ClaudeSetup``) for a
    profile's ``model``: the profile's own when the route serves it, or when
    it is an alias (opus, sonnet, haiku) the route pins in ``models`` (the
    pane's ANTHROPIC_DEFAULT_*_MODEL maps it); else the route's default."""
    if not c.model_ids or model in c.model_ids:
        return model
    if model in ("opus", "sonnet", "haiku") and c.models is not None and getattr(c.models, model):
        return model
    return c.default_model or c.model_ids[0]


def route_in_use(org_id: str, setup) -> bool:
    """Whether a Claude pane gets this org's route env: an enforced setup's
    (``company_identity.enforced_route_env``), or the env company login
    stored for this org (``stored_env``, what ``agents.agent_env`` applies)."""
    if setup.enforce:
        return True
    if not stored_env():
        return False
    try:
        got = config.user_settings().get(ENV_KEY)
    except (OSError, ValueError):
        return False
    return isinstance(got, dict) and got.get("org") == org_id


def org_setup(repo_root: str | None = None, store=None, client=None):
    """(org id, AgentSetup) for the signed-in org; personal policies supply
    only federated Claude setup. None when there is no applicable setup;
    ``UNAVAILABLE`` when it couldn't be had, which says nothing about the setup."""
    from brindle.pro import credentials, license, team_policy

    try:
        store = store or credentials.default_store()
        ent = license.current(store=store, client=client)
        personal = team_policy.FEATURE not in ent.features
        if personal and (ent.policy_version <= 0 or (store.load() or {}).get("org_id")
                         or ent.plan in ("team", "enterprise")):
            return None
        p = team_policy.current_policy(ent, client, store, repo_root)
        setup = p.agent_setup
        if personal:
            if setup is None or setup.claude is None or not setup.claude.federated:
                return None
            # Personal policies configure federation, never other agents or enforcement.
            setup = team_policy.AgentSetup(claude=setup.claude)
    except Exception:  # noqa: BLE001 - offline, logged out, unparsable: leave things as they are
        log.debug("no org policy for company login", exc_info=True)
        return UNAVAILABLE
    return (ent.org_id, setup) if setup is not None else None


def _logged_in(store=None, client=None) -> bool:
    from brindle.pro import license

    try:
        license.current(refresh=False, store=store, client=client)
        return True
    except Exception:  # noqa: BLE001 - no usable entitlement
        return False


def _other_orgs(store=None, client=None) -> set[str]:
    """Orgs besides the signed-in one whose membership still exists; when that
    can't be read, every org with federation files (nothing else is cleared)."""
    from brindle.pro import auth, credentials, license

    store = store or credentials.default_store()
    try:
        current = license.current(refresh=False, store=store, client=client).org_id
        orgs = auth.member_orgs(client or auth.Client(None), store)
    except Exception:  # noqa: BLE001 - offline or logged out: clear nothing but the current org
        log.debug("couldn't list the member orgs", exc_info=True)
        return set(cloud_credential.org_ids()) - {_current_org() or ""}
    return orgs - {current}


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
        clear_federation()  # signed out: no federated credential outlives the sign-in
        return self_serve.run(say, repo_root, ask, info)
    if got is None:
        clear_env()
        # the signed-in org has no agent_setup: its federation files go, other member orgs' stay
        clear_federation(keep=_other_orgs(store, client))
        return self_serve.run(say, repo_root, ask, info)
    return apply(got[0], got[1], say, repo_root, store=store, client=client)


__all__ = ["apply", "apply_current", "claude_env", "stored_env", "write_aws_profile"]
