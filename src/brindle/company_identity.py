"""Which company identity each agent signs in as, checked against the org
policy's ``agent_setup`` (see ``company_login``, which applies it).

``brindle doctor`` shows one line per agent and warns on a mismatch; with the
policy's ``enforce`` (Enterprise) a mismatch refuses to launch that provider's
agents. Only the CLIs' status output is read (``claude auth status``,
``aws sts get-caller-identity``, ``gcloud config get-value project``,
``az account show``, ``codex login status``), never a token. A CLI that is
missing, times out or answers nothing leaves the identity *undetermined*: that
is a warning, never a block.

Commands are argv lists with a timeout, through ``company_login.run_cmd`` and
``company_login.which`` (which tests replace); each answer is kept briefly so
one doctor run asks a CLI once.

A launch (``launch_problem``) also remembers a determined, matching cloud
identity (bedrock, vertex, foundry) for ``LAUNCH_CACHE_SECONDS`` per key (route
and profile or project, plus a fingerprint of the cloud CLI's sign-in files and
env vars: path, mtime and size, never contents), in
``$BRINDLE_HOME/identity-launch-cache.json`` (0600), so the launches that start
together don't each probe; any change to the fingerprint probes again. The
Claude login (subscription, console) is never cached: ``claude auth status``
runs on every launch. A mismatch or an undetermined answer is never
remembered, and doctor never reads the file.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from brindle import company_login, config

CACHE_SECONDS = 30.0
LAUNCH_CACHE_SECONDS = 300.0
LAUNCH_CACHE_FILE = "identity-launch-cache.json"
PROBE_TIMEOUT = 20.0
PERSONAL_PLANS = ("free", "pro", "max")

_cache: dict[tuple[str, ...], tuple[float, tuple[int, str]]] = {}


def reset() -> None:
    _cache.clear()


def _now() -> float:
    return time.time()


def _launch_cache_path() -> Path:
    return config.brindle_home() / LAUNCH_CACHE_FILE


def _stat_files(paths) -> list[str]:
    """path, mtime and size of each file (``missing`` when it isn't there);
    never the contents."""
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append(f"{p}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            out.append(f"{p}:missing")
    return out


def _dir_files(d: Path, prefix: str = "") -> list[Path]:
    try:
        return sorted(p for p in d.iterdir() if p.name.startswith(prefix))
    except OSError:
        return []


def _env(*names: str) -> list[str]:
    return [f"{n}={os.environ.get(n, '')}" for n in names]


def _present(*names: str) -> list[str]:
    """Whether each env var is set, never its value (these hold secrets)."""
    return [f"{n}={'set' if os.environ.get(n) else ''}" for n in names]


def _fingerprint(route: str) -> str:
    """A digest of the cloud CLI's sign-in state for ``route``: the path,
    mtime and size of its credential files (never their contents) and the env
    vars that pick the account. Any change means the cached probe is stale."""
    home = Path.home()
    parts: list[str] = [route]
    if route == "bedrock":
        aws = home / ".aws"
        parts += _stat_files([aws / "config", aws / "credentials"]
                             + _dir_files(aws / "sso" / "cache") + _dir_files(aws / "cli" / "cache"))
        parts += _env("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION")
        parts += _present("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                          "AWS_WEB_IDENTITY_TOKEN_FILE")
    elif route == "vertex":
        gc = Path(os.environ.get("CLOUDSDK_CONFIG") or home / ".config" / "gcloud")
        parts.append(f"CLOUDSDK_CONFIG={gc}")
        parts += _stat_files([gc / "active_config", gc / "credentials.db", gc / "access_tokens.db",
                              gc / "application_default_credentials.json"]
                             + _dir_files(gc / "configurations", "config_"))
        parts += _env("CLOUDSDK_CORE_PROJECT", "CLOUDSDK_ACTIVE_CONFIG_NAME")
        parts += _present("GOOGLE_APPLICATION_CREDENTIALS")
    elif route == "foundry":
        az = Path(os.environ.get("AZURE_CONFIG_DIR") or home / ".azure")
        parts.append(f"AZURE_CONFIG_DIR={az}")
        parts += _stat_files([az / "azureProfile.json", az / "msal_token_cache.json"])
        parts += _env("AZURE_SUBSCRIPTION_ID")
        parts += _present("AZURE_CLIENT_ID")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _launch_key(org_id: str, claude) -> str | None:
    """The launch cache key for ``claude``'s route: the route key plus the
    credential fingerprint. None when nothing is cached: a gateway has no
    probe, and the Claude login (subscription, console) is probed on every
    launch because a switched login must never be missed."""
    if claude.route == "bedrock":
        base = f"claude|bedrock|{company_login.profile_name(org_id)}"
    elif claude.route == "vertex":
        base = f"claude|vertex|{claude.gcp.project}"
    elif claude.route == "foundry":
        base = f"claude|foundry|{claude.azure.subscription_id.lower()}"
    else:
        return None
    return f"{base}|{_fingerprint(claude.route)}"


def _read_launch_cache() -> dict:
    try:
        got = json.loads(_launch_cache_path().read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _launch_cache_hit(key: str) -> bool:
    at = _read_launch_cache().get(key)
    return isinstance(at, (int, float)) and 0 <= _now() - at < LAUNCH_CACHE_SECONDS


def _remember_launch(key: str) -> None:
    now = _now()
    entries = {k: v for k, v in _read_launch_cache().items()
               if isinstance(v, (int, float)) and 0 <= now - v < LAUNCH_CACHE_SECONDS}
    entries[key] = now
    path = _launch_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(json.dumps(entries))
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that can't be written only means the next launch probes


def _probe(argv: list[str], cache: bool = True) -> tuple[int, str] | None:
    """(returncode, output) of a status command; None when its CLI isn't
    installed or the command couldn't run or timed out."""
    if not company_login.which(argv[0]):
        return None
    key = tuple(argv)
    now = time.monotonic()
    hit = _cache.get(key) if cache else None
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    code, out = company_login.run_cmd(argv, PROBE_TIMEOUT)
    if code in (124, 127):
        return None
    _cache[key] = (now, (code, out))
    return code, out


def _json(out: str) -> dict | None:
    try:
        got = json.loads(out)
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


@dataclass
class Identity:
    """One agent's identity. ``problem`` is a mismatch with the policy, with
    its fix; ``determined`` is False when the CLI couldn't say."""
    provider: str
    line: str
    problem: str | None = None
    determined: bool = True
    signed_out: bool = False
    ident: str = ""    # who it is signed in as (ids only, never a token): see signin_pause
    billed: str | None = None       # who pays for this agent's use, when known


def _unknown(provider: str, tool: str, why: str = "") -> Identity:
    install = company_login.INSTALL.get(tool)
    if why:
        return Identity(provider, f"{tool}: {why}", determined=False)
    return Identity(provider, f"{tool} isn't installed, so the identity can't be checked"
                    + (f" (install: {install})" if install else ""), determined=False)


FIX = "run `brindle doctor --fix`"


def claude_identity(org_id: str, claude, cache: bool = True) -> Identity:
    """Claude's identity against ``claude`` (a ``ClaudeSetup``, or None: no
    policy, so only shown)."""
    route = claude.route if claude is not None else None
    if route == "gateway":
        return Identity("claude", f"gateway {claude.gateway.base_url}",
                        ident=f"gateway:{claude.gateway.base_url}")
    if route == "bedrock":
        return _aws(org_id, claude, cache)
    if route == "vertex":
        return _gcloud(claude, cache)
    if route == "foundry":
        return _az(claude, cache)
    return _claude_login(claude, route, cache)


def _claude_login(claude, route, cache) -> Identity:
    got = _probe(["claude", "auth", "status"], cache)
    if got is None:
        return _unknown("claude", "claude")
    status = _json(got[1])
    if status is None:
        return Identity("claude", "claude: status unreadable", determined=False)
    if not status.get("loggedIn", got[0] == 0):
        fix = _claude_login_cmd(route)
        return Identity("claude", "Claude: signed out", signed_out=True,
                        problem=f"Claude is signed out; {FIX} or run `{fix}`" if claude is not None else None)
    org, name, plan = status.get("orgId"), status.get("orgName"), status.get("subscriptionType")
    who = f"org {name or org or 'none'}" + (f" ({org})" if org and name else "") + \
        (f", plan {plan}" if plan else "")
    line = f"Claude: {who}"
    problem = None
    if claude is not None and route in ("subscription", "console"):
        fix = f"{FIX} or run `{_claude_login_cmd(route)}`"
        if claude.org_id and org and claude.org_id.lower() != str(org).lower():
            problem = f"Claude is signed in to {name or org}, not the policy's org {claude.org_id}; {fix}"
        elif claude.org_id and not org:
            problem = f"Claude is signed in without an organization, but the policy says {claude.org_id}; {fix}"
        elif str(plan or "").lower() in PERSONAL_PLANS:
            problem = f"Claude is on a personal {plan} login, but the policy says the company plan; {fix}"
    billed = None
    if org or name:
        owner = f"{name}'s" if name else f"organization {org}'s"
        billed = f"Claude: {owner} {plan + ' ' if plan else ''}plan"
    return Identity("claude", line, problem, billed=billed,
                    ident=f"claude org {name or org or 'none'}" + (f" ({org})" if org and name else ""))


def _claude_login_cmd(route) -> str:
    return "claude auth login " + ("--console" if route == "console" else "--sso")


def _aws(org_id: str, claude, cache) -> Identity:
    profile = company_login.profile_name(org_id)
    got = _probe(["aws", "sts", "get-caller-identity", "--profile", profile, "--output", "json"], cache)
    if got is None:
        return _unknown("claude", "aws")
    fix = f"{FIX} or run `aws sso login --profile {profile}`"
    ident = _json(got[1]) if got[0] == 0 else None
    if ident is None:
        return Identity("claude", f"AWS profile {profile}: not signed in", signed_out=True,
                        problem=f"AWS profile {profile} isn't signed in; {fix}")
    account, arn = str(ident.get("Account", "")), str(ident.get("Arn", ""))
    parts = arn.split("/")
    role = parts[1] if len(parts) > 1 else "unknown"
    line = f"AWS account {account}, role {role}, region {claude.aws.region}"
    problem = None
    if account != claude.aws.account_id:
        problem = f"AWS account is {account}, not the policy's {claude.aws.account_id}; {fix}"
    return Identity("claude", line, problem, billed=f"Bedrock: AWS account {account[:4]}…",
                    ident=f"AWS account {account}, role {role}")


def _gcloud(claude, cache) -> Identity:
    got = _probe(["gcloud", "config", "get-value", "project"], cache)
    if got is None:
        return _unknown("claude", "gcloud")
    project = got[1].strip().splitlines()[-1].strip() if got[1].strip() else ""
    if got[0] != 0 or not project or project == "(unset)":
        project = ""
    want = claude.gcp.project
    fix = f"{FIX} or run `gcloud config set project {want}`"
    if not project:
        return Identity("claude", "gcloud project: none set", problem=f"no active gcloud project; {fix}")
    problem = None if project == want else f"gcloud project is {project}, not the policy's {want}; {fix}"
    return Identity("claude", f"gcloud project {project}, region {claude.gcp.region}", problem,
                    billed=f"Vertex: Google Cloud project {project}", ident=f"GCP project {project}")


def _az(claude, cache) -> Identity:
    got = _probe(["az", "account", "show", "--output", "json"], cache)
    if got is None:
        return _unknown("claude", "az")
    want = claude.azure.subscription_id
    fix = f"{FIX} or run `az login` and `az account set --subscription {want}`"
    acct = _json(got[1]) if got[0] == 0 else None
    if acct is None:
        return Identity("claude", "Azure: not signed in", signed_out=True, problem=f"Azure isn't signed in; {fix}")
    sub = str(acct.get("id", ""))
    line = f"Azure subscription {acct.get('name') or sub} ({sub})"
    problem = None if sub.lower() == want.lower() else \
        f"Azure subscription is {sub}, not the policy's {want}; {fix}"
    return Identity("claude", line, problem, billed=f"Foundry: Azure subscription {acct.get('name') or sub}",
                    ident=f"Azure subscription {sub}")


def codex_identity(cache: bool = True) -> Identity:
    """Codex can't be verified beyond signed in: its status line, as information."""
    got = _probe(["codex", "login", "status"], cache)
    if got is None:
        return _unknown("codex", "codex")
    text = next((ln.strip() for ln in got[1].splitlines() if ln.strip()), "")
    if got[0] != 0:
        return Identity("codex", f"Codex: {text or 'not signed in'}", signed_out=True)
    return Identity("codex", f"Codex: {text or 'signed in'} (the organization can't be checked)")


def _antigravity_identity() -> Identity:
    from brindle import providers

    why = providers.signed_out("antigravity")
    return Identity("antigravity", "Antigravity: " + ("signed out" if why else
                    "signed in (the organization can't be checked)"))


def _setup():
    got = company_login.org_setup()
    return None if got is None or got is company_login.UNAVAILABLE else got


def expected_ident(claude) -> str | None:
    """The identity the org's ``agent_setup`` expects Claude to run as, in the
    form ``claude_identity`` reports it (ids only). For a login route only the
    org id is known, so it's ``org:<id>`` (see ``matches``). None when the
    setup names nothing to check against."""
    if claude is None:
        return None
    if claude.route == "gateway":
        return f"gateway:{claude.gateway.base_url}" if claude.gateway else None
    if claude.route == "bedrock" and claude.aws:
        return f"AWS account {claude.aws.account_id}, role {claude.aws.role_name}"
    if claude.route == "vertex" and claude.gcp:
        return f"GCP project {claude.gcp.project}"
    if claude.route == "foundry" and claude.azure:
        return f"Azure subscription {claude.azure.subscription_id}"
    if claude.org_id:
        return f"org:{claude.org_id}"
    return None


def matches(expected: str | None, current: str | None) -> bool:
    """Whether ``current`` (a probed ``Identity.ident``) is the ``expected``
    one from ``expected_ident``. Never true when either is unknown."""
    if not expected or not current:
        return False
    if expected.startswith("org:"):
        org, cur = expected[4:].casefold(), current.casefold()
        return f"({org})" in cur or cur.endswith(f" {org}")   # "claude org Name (org)" or "claude org org"
    return expected.casefold() == current.casefold()


def _local_setup():
    """The person's own self-serve setup (never enforced), when saved."""
    from brindle import self_serve

    try:
        local = self_serve.local_setup()
    except Exception:  # noqa: BLE001 - never fail doctor over it
        return None
    return (self_serve.SELF_ORG, local) if local is not None else None


def identities(cache: bool = True) -> tuple[list[Identity], bool]:
    """(an identity per installed agent CLI, whether the policy enforces).
    With no org setup, a saved self-serve setup stands in (labelled as such,
    with who is billed; it never enforces)."""
    from brindle import doctor

    got = _setup()
    self_serve_setup = False
    if got is None:
        got = _local_setup()
        self_serve_setup = got is not None
    org_id, setup = got if got else (None, None)
    installed = doctor.signin_providers()
    out: list[Identity] = []
    if "claude" in installed or (setup and setup.claude):
        ident = claude_identity(org_id, setup.claude if setup else None, cache)
        if self_serve_setup and setup.claude is not None:
            ident.line += " (self-serve" + (f"; billed to {ident.billed}" if ident.billed else "") + ")"
        out.append(ident)
    if "codex" in installed:
        out.append(codex_identity(cache))
    if "antigravity" in installed:
        out.append(_antigravity_identity())
    return out, bool(setup and setup.enforce and not self_serve_setup)


def checks() -> list:
    """The doctor lines: an identity per agent; a mismatch or an undetermined
    identity is a warning (a refusal at launch is separate: see launch_problem)."""
    from brindle.doctor import OK, WARN, Check

    reset()
    try:
        found, enforce = identities()
    except Exception as e:  # noqa: BLE001 - never fail doctor over an identity probe
        return [Check(WARN, "identity", f"couldn't check: {e}")]
    out = []
    for i in found:
        if i.problem:
            out.append(Check(WARN, f"{i.provider} identity",
                             i.problem + ("; this org refuses to start the agent until then" if enforce else "")))
        elif not i.determined:
            out.append(Check(WARN, f"{i.provider} identity", i.line))
        else:
            out.append(Check(OK, f"{i.provider} identity", i.line))
    return out


def launch_problem(provider: str) -> str | None:
    """Why ``provider``'s agents must not start: the org policy enforces its
    company identity (Enterprise) and this machine's differs. None when it
    doesn't enforce, the identity matches, or it can't be determined."""
    if provider != "claude":
        return None
    try:
        got = _setup()
        if got is None or not got[1].enforce or got[1].claude is None:
            return None
        key = _launch_key(got[0], got[1].claude)
        if key and _launch_cache_hit(key):
            return None
        ident = claude_identity(got[0], got[1].claude, cache=False)
        if key and ident.problem is None and ident.determined:
            _remember_launch(key)
    except Exception:  # noqa: BLE001 - an unreadable identity never blocks a launch
        return None
    if ident.problem is None or not ident.determined:
        return None
    return f"the org policy requires the company sign-in: {ident.problem}"
