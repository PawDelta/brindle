"""Self-serve company setup: use your company's access without an org ``agent_setup``.

When the signed-in org has no ``agent_setup`` (Free, Pro, or a Team org that
hasn't configured one), ``brindle account login`` / ``brindle login`` /
``brindle doctor --fix`` look at what this machine is already signed in to,
propose a setup, and, only on a yes, save it to ``~/.brindle/config.json`` as
``"agent_setup"`` (the org policy's format, validated by the same parser, with
``enforce`` always false) and apply it through ``company_login``. An org's own
``agent_setup`` always wins over it.

Detection is read-only and reads nothing but these status outputs and
``~/.aws/config``: ``claude auth status``, ``aws sts get-caller-identity``,
``gcloud config get-value project|compute/region``, ``az account show`` and
``codex login status`` (the Google Cloud sign-in itself is checked, and made,
when the setup is applied, by ``company_login``); plus Claude Code's managed settings
(``providers.claude_org_managed``). Every command is an argv list with a
timeout through ``company_login.run_cmd``. You must be authorized to use any
company credentials you connect this way.
"""

from __future__ import annotations

import configparser
import dataclasses
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Callable

from brindle import company_login, config

log = logging.getLogger(__name__)

LOCAL_KEY = "agent_setup"       # ~/.brindle/config.json
SELF_ORG = "personal"           # stands in for an org id (AWS profile name brindle-personal)
PROBE_TIMEOUT = 20.0
PERSONAL_PLANS = ("free", "pro", "max")
MAX_AWS_PROFILES = 5
DEFAULT_GCP_REGION = "us-east5"
NUDGE = ("Setting this up for your team? Your admin can do it once for everyone with "
         "brindle Team: pawdelta.com/brindle#pricing")


# -- the saved setup -----------------------------------------------------------------------

def _prune(v):
    if isinstance(v, dict):
        return {k: _prune(x) for k, x in v.items() if x is not None}
    return v


def to_json(setup) -> dict:
    """``setup`` (an ``AgentSetup``) in the org policy's ``agent_setup`` format."""
    d = _prune(dataclasses.asdict(setup))
    d["enforce"] = False
    return d


def parse(raw):
    """``raw`` through the org policy's own parser; ``enforce`` is never kept."""
    from brindle.pro import team_policy

    return dataclasses.replace(team_policy._parse_agent_setup(raw), enforce=False)


def local_setup():
    """The self-serve ``AgentSetup`` saved in ``~/.brindle/config.json``, or
    None (absent, or one that doesn't parse)."""
    try:
        raw = config.user_settings().get(LOCAL_KEY)
    except (OSError, ValueError):
        return None
    if raw is None:
        return None
    try:
        return parse(raw)
    except Exception as e:  # noqa: BLE001 - an edited config must not break login
        log.warning("ignoring the agent_setup in ~/.brindle/config.json (%s)", e)
        return None


def save_local(setup) -> None:
    config.set_user(LOCAL_KEY, to_json(setup))


# -- detection -----------------------------------------------------------------------------

@dataclass
class Choice:
    label: str              # what the proposal says
    claude: object          # a ClaudeSetup
    billed: str             # who pays


@dataclass
class Detection:
    managed: str | None = None
    found: list[str] = field(default_factory=list)      # what was seen, for the person
    choices: list[Choice] = field(default_factory=list)
    codex: object = None    # a CodexSetup, when Codex is signed in with ChatGPT


def _run(argv: list[str]) -> tuple[int, str] | None:
    """(returncode, output); None when the CLI isn't installed or the command
    couldn't run or timed out."""
    if not company_login.which(argv[0]):
        return None
    code, out = company_login.run_cmd(argv, PROBE_TIMEOUT)
    return None if code in (124, 127) else (code, out)


def _json(out: str) -> dict | None:
    try:
        got = json.loads(out)
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def masked(account: str) -> str:
    return account[:4] + "…"


def aws_billed(account: str) -> str:
    return f"AWS account {masked(account)}"


def _claude_choice(d: Detection) -> None:
    got = _run(["claude", "auth", "status"])
    status = _json(got[1]) if got else None
    if not status or not status.get("loggedIn", got[0] == 0):
        return
    org, name, plan = status.get("orgId"), status.get("orgName"), status.get("subscriptionType")
    plan_s = str(plan or "")
    who = f"{name or org or 'no organization'}" + (f" ({plan_s} plan)" if plan_s else "")
    d.found.append(f"Claude: signed in to {who}")
    if not org or plan_s.lower() in PERSONAL_PLANS:
        return      # a personal login: nothing company to propose
    owner = f"{name}'s" if name else "your organization's"
    billed = f"{owner} {plan_s} plan" if plan_s else f"{owner} plan"
    from brindle.pro import team_policy

    d.choices.append(Choice(f"Claude on {billed}", team_policy.ClaudeSetup(
        route="subscription", org_id=str(org)), f"Claude: {billed}"))


def _aws_profiles(path) -> list[dict]:
    """SSO profiles with everything a Bedrock setup needs, from ``~/.aws/config``."""
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        cp.read(path, encoding="utf-8")
    except (OSError, configparser.Error, UnicodeDecodeError):
        return []
    sessions = {" ".join(s.split(None, 1)[1].split()): dict(cp.items(s))
                for s in cp.sections() if s.split(None, 1)[0] == "sso-session" and len(s.split(None, 1)) > 1}
    out = []
    for section in cp.sections():
        parts = section.split(None, 1)
        if parts[0] != "profile" or len(parts) < 2:
            continue
        name = " ".join(parts[1].split())
        if name.startswith("brindle-"):
            continue        # the ones brindle wrote for an org
        p = dict(cp.items(section))
        sess = sessions.get(p.get("sso_session", ""), {})
        url = p.get("sso_start_url") or sess.get("sso_start_url")
        sso_region = p.get("sso_region") or sess.get("sso_region")
        region = p.get("region") or sso_region
        account, role = p.get("sso_account_id"), p.get("sso_role_name")
        if url and sso_region and account and role and region:
            out.append({"name": name, "sso_start_url": url, "sso_region": sso_region,
                        "account_id": account, "role_name": role, "region": region})
    return out[:MAX_AWS_PROFILES]


def _aws_choices(d: Detection) -> None:
    from brindle.pro import team_policy

    for p in _aws_profiles(company_login.aws_config_path()):
        got = _run(["aws", "sts", "get-caller-identity", "--profile", p["name"], "--output", "json"])
        state = "not signed in" if got is None or got[0] != 0 else "signed in"
        d.found.append(f"AWS profile {p['name']}: account {masked(p['account_id'])}, role "
                       f"{p['role_name']}, {p['region']} ({state})")
        aws = team_policy.AwsSetup(sso_start_url=p["sso_start_url"], sso_region=p["sso_region"],
                                   account_id=p["account_id"], role_name=p["role_name"], region=p["region"])
        d.choices.append(Choice(
            f"Bedrock ({aws_billed(aws.account_id)}, role {aws.role_name}, {aws.region})",
            team_policy.ClaudeSetup(route="bedrock", aws=aws), f"Bedrock: {aws_billed(aws.account_id)}"))


def _value(argv: list[str]) -> str:
    got = _run(argv)
    if got is None or got[0] != 0:
        return ""
    lines = got[1].strip().splitlines()
    v = lines[-1].strip() if lines else ""
    return "" if v == "(unset)" else v


def _gcloud_choice(d: Detection) -> None:
    from brindle.pro import team_policy

    project = _value(["gcloud", "config", "get-value", "project"])
    if not project:
        return
    region = _value(["gcloud", "config", "get-value", "compute/region"]) or DEFAULT_GCP_REGION
    d.found.append(f"Google Cloud: project {project}, region {region}")
    d.choices.append(Choice(
        f"Vertex AI (Google Cloud project {project}, {region})",
        team_policy.ClaudeSetup(route="vertex", gcp=team_policy.GcpSetup(project=project, region=region)),
        f"Vertex: Google Cloud project {project}"))


def _az_found(d: Detection) -> None:
    got = _run(["az", "account", "show", "--output", "json"])
    acct = _json(got[1]) if got and got[0] == 0 else None
    if acct and acct.get("id"):
        d.found.append(f"Azure: subscription {acct.get('name') or acct['id']} ({acct['id']})")
        resource = os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE", "")
        if resource:
            from brindle.pro import team_policy

            d.choices.append(Choice(
                f"Azure AI Foundry (subscription {acct.get('name') or acct['id']}, resource {resource})",
                team_policy.ClaudeSetup(route="foundry", azure=team_policy.AzureSetup(
                    subscription_id=str(acct["id"]), resource=resource)),
                f"Foundry: Azure subscription {acct.get('name') or acct['id']}"))
        else:
            d.found.append("  (to use it for Claude, set ANTHROPIC_FOUNDRY_RESOURCE to your Foundry resource name)")


def _codex_found(d: Detection) -> None:
    got = _run(["codex", "login", "status"])
    if got is None or got[0] != 0:
        return
    text = next((ln.strip() for ln in got[1].splitlines() if ln.strip()), "signed in")
    d.found.append(f"Codex: {text}")
    if "chatgpt" in text.lower():
        from brindle.pro import team_policy

        d.codex = team_policy.CodexSetup(route="chatgpt")


def detect() -> Detection:
    """What this machine is already signed in to (read-only; see the module doc)."""
    from brindle import providers

    d = Detection()
    d.managed = providers.claude_org_managed()
    if d.managed:
        return d
    for step in (_claude_choice, _aws_choices, _gcloud_choice, _az_found, _codex_found):
        try:
            step(d)
        except Exception:  # noqa: BLE001 - one unreadable source never blocks the others
            log.debug("self-serve detection step failed", exc_info=True)
    return d


# -- the proposal --------------------------------------------------------------------------

def tty_ask(prompt: str) -> str:
    """Ask on the terminal; nothing (= no) without one or at EOF."""
    if not sys.stdin.isatty():
        return ""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


def _pick(choices: list[Choice], ask, say) -> Choice | None:
    """Ask which choice; anything but a clear yes/number is no."""
    if len(choices) == 1:
        ans = ask(f"Use {choices[0].label}? [y/N] ").strip().lower()
        return choices[0] if ans in ("y", "yes") else None
    for i, c in enumerate(choices, 1):
        say(f"  {i}. Use {c.label}")
    say("  0. Keep my personal setup")
    ans = ask(f"Which one? [0-{len(choices)}, default 0] ").strip()
    if ans.isdigit() and 1 <= int(ans) <= len(choices):
        return choices[int(ans) - 1]
    return None


def offer(say: Callable[[str], None], repo_root: str | None, ask,
          info: Callable[[str], None] | None = None) -> bool | None:
    """Show what was found and propose a setup; a confirmed one is saved and
    applied. None when nothing was chosen (the default), else whether it is ready.
    What was found and the menu go to ``info`` (default ``say``); only the
    outcome of an applied setup goes to ``say``."""
    from brindle.pro import team_policy

    info = info or say
    d = detect()
    if d.managed:
        info("Claude Code: your IT already configures Claude Code; brindle will use it")
        return None
    if d.found:
        info("Found on this machine:")
        for line in d.found:
            info(f"  {line}")
    if not d.choices:
        return None
    choice = _pick(d.choices, ask, info)
    if choice is None:
        info("Keeping your personal setup; nothing changed.")
        return None
    setup = team_policy.AgentSetup(claude=choice.claude, codex=d.codex, enforce=False)
    try:
        setup = parse(to_json(setup))
        save_local(setup)
    except Exception as e:  # noqa: BLE001 - e.g. a value the shared parser refuses
        say(f"Couldn't save that setup ({e}); nothing changed.")
        return None
    ok = company_login.apply(SELF_ORG, setup, say, repo_root, self_serve=True)
    if ok:
        say(NUDGE)
    return ok


def run(say: Callable[[str], None], repo_root: str | None = None, ask=None,
        info: Callable[[str], None] | None = None) -> bool | None:
    """For an org with no ``agent_setup`` (or no org): re-apply the saved
    self-serve setup, else offer one. Offers only with ``ask`` or a terminal."""
    setup = local_setup()
    if setup is not None:
        return company_login.apply(SELF_ORG, setup, say, repo_root, self_serve=True)
    try:
        stored = config.user_settings().get(company_login.ENV_KEY)
    except (OSError, ValueError):
        stored = None
    if isinstance(stored, dict) and stored.get("self_serve"):
        company_login.clear_env(force=True)     # its setup was removed: don't keep its route
    if ask is None:
        if not sys.stdin.isatty():
            return None
        ask = tty_ask
    return offer(say, repo_root, ask, info)


__all__ = ["detect", "local_setup", "offer", "run", "save_local"]
