"""Enterprise ``managed_models``: run the org's workers on the route the org
chose, and keep personal API keys out of their panes.

Enforced only with the ``managed_models`` feature (``license.has``, fail
closed: no verified entitlement, nothing is managed). The route comes from
the org policy (``brindle.pro.team_policy``) in one of two forms:

* ``agent_setup`` with ``enforce`` (the one source of truth): per agent, the
  Claude route (bedrock, vertex, foundry, gateway) with its ``model_ids`` and
  ``default_model``, and the Codex route (azure, gateway) with its
  ``base_url``. An agent whose route points at a cloud or an endpoint of the
  org's is managed exactly as ``provider_config`` always managed every agent:
  its env is the top layer of the pane's (``agents.agent_env``), a profile
  pointing elsewhere is refused, and the identity is checked at launch
  (``company_identity``). An agent with no such route (a Claude org login,
  Codex on ChatGPT, nothing set) isn't managed here; ``allowed_providers``
  is the way to keep it out. Without ``enforce`` the same block is login
  help only (``company_login``) and the lowest env layer.
* ``provider_config`` (servers older than ``agent_setup`` routing, which keep
  sending it as a mirror): one route for the whole org. Bedrock or Vertex
  (with a region and model ids), or an Azure / OpenAI-compatible endpoint.

  =================  =======================================================
  Claude Code        bedrock: ``CLAUDE_CODE_USE_BEDROCK`` + ``AWS_REGION``;
                     vertex: ``CLAUDE_CODE_USE_VERTEX`` + ``CLOUD_ML_REGION``
                     + ``ANTHROPIC_VERTEX_PROJECT_ID`` (the ``project``,
                     else the ``endpoint``, as older policies had it);
                     azure: ``CLAUDE_CODE_USE_FOUNDRY`` +
                     ``ANTHROPIC_FOUNDRY_BASE_URL``. The first model id is
                     ``ANTHROPIC_MODEL``.
  Codex              azure / openai-compatible: ``OPENAI_BASE_URL``
  native             azure / openai-compatible: the profile's ``base_url``
  =================  =======================================================

The Claude env of an ``agent_setup`` route is ``company_login.claude_env``,
the same builder ``brindle login`` stores, so an old ``provider_config`` and
its ``agent_setup`` form give a pane the same variables.

``deny_personal_keys`` (in ``agent_setup``, else the policy's top-level
field): the person's own model keys (``secrets.PERSONAL_KEYS``) are taken
out of every agent pane, and a profile that brings its own is refused. This
can't stop a Claude subscription/keychain login unless the route takes
Claude Code to Bedrock, Vertex or Foundry, so pair the two.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from brindle import secrets
from brindle.pro.team_policy import (ANTIGRAVITY_ENDPOINT_ROUTES, CLAUDE_CLOUD_ROUTES,
                                     CODEX_ENDPOINT_ROUTES, AgentSetup, OrgPolicy, ProviderConfig)

log = logging.getLogger(__name__)

# What picks a Claude Code (or Codex) backend: a profile setting any of these
# points the agent somewhere the org didn't choose.
ROUTING_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
               "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE", "CLAUDE_CODE_USE_BEDROCK",
               "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "OPENAI_BASE_URL",
               "GOOGLE_GEMINI_BASE_URL",
               "AWS_REGION", "AWS_DEFAULT_REGION", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID")

NAMES = {"bedrock": "AWS Bedrock", "vertex": "Google Vertex AI", "azure": "Azure",
         "openai-compatible": "an OpenAI-compatible endpoint", "anthropic": "the Anthropic API"}
# The same, by agent_setup route: what a refusal names.
CLAUDE_ROUTE_NAMES = {"bedrock": NAMES["bedrock"], "vertex": NAMES["vertex"], "foundry": NAMES["azure"],
                      "gateway": "an Anthropic-compatible gateway"}
CODEX_ROUTE_NAMES = {"azure": NAMES["azure"], "gateway": NAMES["openai-compatible"]}
ANTIGRAVITY_ROUTE_NAMES = {"gateway": "a Gemini-compatible gateway"}

# The variable the org's key (``key_env`` / ``key_helper``) is set as in a pane,
# by the agent's provider, for the managed provider that carries a key.
ORG_KEY_VARS = {("claude", "anthropic"): "ANTHROPIC_API_KEY",
                ("codex", "openai-compatible"): "OPENAI_API_KEY"}
KEY_HELPER_TIMEOUT = 15.0


class ManagedUnavailable(Exception):
    """The org's managed-models policy applies but can't be read: fail closed."""


@dataclass(frozen=True)
class Managed:
    """What applies: the routes of an enforced ``agent_setup`` (``setup``), or
    an older server's ``provider_config`` (``config``); never both."""
    org_id: str
    config: ProviderConfig | None
    deny_personal_keys: bool
    setup: AgentSetup | None = None

    @property
    def denied_keys(self) -> tuple[str, ...]:
        return secrets.PERSONAL_KEYS if self.deny_personal_keys else ()

    @property
    def claude(self):
        """The managed Claude route (a ``ClaudeSetup``), or None."""
        s = self.setup
        return s.claude if s is not None and s.claude is not None and s.claude.route in CLAUDE_CLOUD_ROUTES \
            else None

    @property
    def codex(self):
        """The managed Codex route (a ``CodexSetup`` with a ``base_url``), or None."""
        s = self.setup
        return s.codex if s is not None and s.codex is not None and s.codex.route in CODEX_ENDPOINT_ROUTES \
            and s.codex.base_url else None

    @property
    def antigravity(self):
        """The managed Antigravity route (an ``AntigravitySetup`` with a ``base_url``), or None."""
        s = self.setup
        return s.antigravity if s is not None and s.antigravity is not None and s.antigravity.route in ANTIGRAVITY_ENDPOINT_ROUTES \
            and s.antigravity.base_url else None

    def describe(self) -> str:
        parts = []
        c = self.config
        if c is not None:
            bits = [NAMES[c.provider]]
            if c.region:
                bits.append(f"region {c.region}")
            if c.endpoint:
                bits.append(c.endpoint)
            if c.model_ids:
                bits.append("models " + ", ".join(c.model_ids))
            if c.key_env or c.key_helper:
                bits.append("org-owned key")
            parts.append(bits[0] + (f" ({', '.join(bits[1:])})" if bits[1:] else ""))
        if self.claude is not None:
            parts.append("Claude on " + _describe_claude(self.claude))
        if self.codex is not None:
            parts.append(f"Codex on {CODEX_ROUTE_NAMES[self.codex.route]} ({self.codex.base_url})")
        if self.antigravity is not None:
            parts.append(f"Antigravity on {ANTIGRAVITY_ROUTE_NAMES[self.antigravity.route]} ({self.antigravity.base_url})")
        if self.exclusive:
            parts.append("agents without a route refused")
        if self.deny_personal_keys:
            parts.append("personal API keys denied")
        return f"org {self.org_id}: " + "; ".join(parts)

    @property
    def exclusive(self) -> bool:
        """Whether an agent the org's routing gives no route is refused."""
        return bool(self.setup is not None and self.setup.exclusive)


def _describe_claude(c) -> str:
    bits = [CLAUDE_ROUTE_NAMES[c.route]]
    if c.route == "bedrock":
        bits.append(f"region {c.aws.region}" + ("" if c.aws.sso or c.federated else ", your own AWS sign-in"))
    elif c.route == "vertex":
        bits.append(f"project {c.gcp.project}, region {c.gcp.region}")
    elif c.route == "foundry":
        bits.append(c.azure.base_url or f"resource {c.azure.resource}")
    elif c.route == "gateway":
        bits.append(c.gateway.base_url)
    if c.model_ids:
        bits.append("models " + ", ".join(c.model_ids))
    if c.default_model:
        bits.append(f"default {c.default_model}")
    return bits[0] + (f" ({', '.join(bits[1:])})" if bits[1:] else "")


def current(repo_root: str | None) -> Managed | None:
    """The managed-models settings that apply, None when there are none (the
    feature isn't entitled, no team org, or the org set nothing managed).
    From ``agent_setup`` when it is the org's routing (``AgentSetup.routing``:
    the block carries ``exclusive``, as a phase-1 server always sends it) and
    is enforced and either routes an agent somewhere (``routed``) or is
    exclusive; else from ``provider_config``, exactly as before: a server
    older than that can hold both, and its ``provider_config`` wins while
    its ``agent_setup`` is login help and the identity check only.
    ``deny_personal_keys`` from ``agent_setup`` when set there, else the
    policy's top-level field. Raises :class:`ManagedUnavailable` when they
    apply but can't be read."""
    from brindle.pro import team_policy

    try:
        p = team_policy.managed_models(repo_root or "")
    except Exception as e:  # noqa: BLE001 - fail closed on anything
        raise ManagedUnavailable(f"couldn't read the org's managed-models policy ({e})") from e
    if p is None:
        return None
    if not isinstance(p, OrgPolicy):
        raise ManagedUnavailable(p.reason)
    s = p.agent_setup
    deny = p.deny_personal_keys if s is None or s.deny_personal_keys is None else s.deny_personal_keys
    if s is not None and s.routing and s.enforce and (s.routed or s.exclusive):
        return Managed(p.org_id, None, deny, setup=s)
    if p.provider_config is None and not deny:
        return None
    return Managed(p.org_id, p.provider_config, deny)


def provider_env(cfg: ProviderConfig, provider: str) -> dict[str, str]:
    """The environment that points ``provider``'s CLI at ``cfg`` (empty for
    one that has none: native reads the profile's base_url, see
    :func:`apply_profile`)."""
    env: dict[str, str] = {}
    if provider == "claude":
        if cfg.provider == "bedrock":
            env["CLAUDE_CODE_USE_BEDROCK"] = "1"
            if cfg.region:
                env["AWS_REGION"] = cfg.region
            if cfg.endpoint:
                env["ANTHROPIC_BEDROCK_BASE_URL"] = cfg.endpoint
        elif cfg.provider == "vertex":
            env["CLAUDE_CODE_USE_VERTEX"] = "1"
            if cfg.region:
                env["CLOUD_ML_REGION"] = cfg.region
            # ``endpoint`` as the project id is the older shape of the policy.
            project = cfg.project or cfg.endpoint
            if project:
                env["ANTHROPIC_VERTEX_PROJECT_ID"] = project
        elif cfg.provider == "azure":
            env["CLAUDE_CODE_USE_FOUNDRY"] = "1"
            if cfg.endpoint:
                env["ANTHROPIC_FOUNDRY_BASE_URL"] = cfg.endpoint
        if cfg.model_ids and cfg.provider in ("bedrock", "vertex", "azure", "anthropic"):
            env["ANTHROPIC_MODEL"] = cfg.model_ids[0]
            if len(cfg.model_ids) > 1:
                env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = cfg.model_ids[-1]
    elif provider == "codex" and cfg.provider in ("azure", "openai-compatible") and cfg.endpoint:
        env["OPENAI_BASE_URL"] = cfg.endpoint
    return env


def refusal(m: Managed, profile) -> str | None:
    """Why the org's managed models don't allow ``profile`` to run, or None."""
    provider = profile.provider
    if provider in ("shell", "subagent"):
        return None      # no model of its own to point anywhere (a subagent runs in the caller's)
    who = f"profile {profile.name!r} ({provider})"
    if m.deny_personal_keys:
        mine = [k for k in profile.env if k in secrets.PERSONAL_KEYS]
        if profile.api_key_env in secrets.PERSONAL_KEYS:
            mine.append(profile.api_key_env)
        if mine:
            return (f"{who} uses a personal API key ({', '.join(sorted(set(mine)))}), which org "
                    f"{m.org_id} doesn't allow; remove it from the profile (brindle strips "
                    "OPENAI_API_KEY and the other personal keys from agent panes, so a profile "
                    "that needs a key should name a non-personal one in api_key_env)")
    if m.setup is not None:
        return _route_refusal(m, profile, who)
    c = m.config
    if c is None:
        return None
    where = NAMES[c.provider]
    if provider == "claude":
        if c.provider == "openai-compatible":
            return (f"{who} runs Claude Code, which can't use {where}, the provider org {m.org_id} "
                    "manages; use a native or Codex profile")
        elsewhere = sorted(k for k in profile.env if k in ROUTING_ENV)
        if elsewhere:
            return (f"{who} sets {', '.join(elsewhere)}, pointing it away from {where}, the "
                    f"provider org {m.org_id} manages; remove it from the profile")
    elif provider == "codex":
        if c.provider not in ("azure", "openai-compatible"):
            return (f"{who} runs Codex, which can't use {where}, the provider org {m.org_id} "
                    "manages; use a Claude Code profile")
        elsewhere = sorted(k for k in profile.env if k in ROUTING_ENV)
        if elsewhere:
            return (f"{who} sets {', '.join(elsewhere)}, pointing it away from {where}, the "
                    f"provider org {m.org_id} manages; remove it from the profile")
    elif provider == "native":
        if c.provider not in ("azure", "openai-compatible"):
            return (f"{who} uses its own endpoint, which isn't {where}, the provider org "
                    f"{m.org_id} manages; use a Claude Code profile")
        if not c.endpoint:
            return f"org {m.org_id}'s managed provider has no endpoint for a native profile"
        if profile.base_url and profile.base_url.rstrip("/") != c.endpoint.rstrip("/"):
            return (f"{who} points at {profile.base_url}, not {c.endpoint}, the endpoint org "
                    f"{m.org_id} manages; remove its base_url")
    else:
        return (f"{who} uses {provider}, which can't run on {where}, the provider org "
                f"{m.org_id} manages")
    return None


def _route_refusal(m: Managed, profile, who: str) -> str | None:
    """:func:`refusal` under an enforced ``agent_setup``: an agent whose route
    the org set may not be pointed elsewhere by its profile. One the block
    gives no route is refused when the routing is ``exclusive`` (as
    ``provider_config`` refused every agent it couldn't serve), else it isn't
    the org's to manage here."""
    provider = profile.provider
    if provider == "claude" and m.claude is not None:
        where = CLAUDE_ROUTE_NAMES[m.claude.route]
    elif provider in ("codex", "native") and m.codex is not None:
        where = CODEX_ROUTE_NAMES[m.codex.route]
    elif provider == "antigravity" and m.antigravity is not None:
        where = ANTIGRAVITY_ROUTE_NAMES[m.antigravity.route]
    else:
        return _exclusive_refusal(m, profile, who) if m.exclusive else None
    if provider == "native":
        # brindle's own OpenAI-compatible loop follows the Codex endpoint, as
        # it followed an azure / openai-compatible provider_config.
        want = m.codex.base_url
        if profile.base_url and profile.base_url.rstrip("/") != want.rstrip("/"):
            return (f"{who} points at {profile.base_url}, not {want}, the endpoint org "
                    f"{m.org_id} manages; remove its base_url")
        return None
    elsewhere = sorted(k for k in profile.env if k in ROUTING_ENV)
    if elsewhere:
        return (f"{who} sets {', '.join(elsewhere)}, pointing it away from {where}, the "
                f"provider org {m.org_id} manages; remove it from the profile")
    return None


def _exclusive_refusal(m: Managed, profile, who: str) -> str | None:
    """Under exclusive routing, why an agent the org gave no route may not
    run: the messages ``provider_config`` gives for an agent it can't serve.
    An agent with a login route (a Claude org, Codex on ChatGPT or a key)
    has its route and runs on it."""
    s = m.setup
    provider = profile.provider
    if provider == "claude" and s.claude is not None:
        return None
    if provider == "codex" and s.codex is not None:
        return None
    if provider == "antigravity" and s.antigravity is not None:
        return None
    where = (CLAUDE_ROUTE_NAMES[m.claude.route] if m.claude is not None
             else CODEX_ROUTE_NAMES[m.codex.route] if m.codex is not None
             else ANTIGRAVITY_ROUTE_NAMES[m.antigravity.route] if m.antigravity is not None
             else "the routes")
    if provider == "claude":
        return (f"{who} runs Claude Code, which can't use {where}, the provider org {m.org_id} "
                "manages; use a native or Codex profile")
    if provider == "codex":
        return (f"{who} runs Codex, which can't use {where}, the provider org {m.org_id} "
                "manages; use a Claude Code profile")
    if provider == "native":
        return (f"{who} uses its own endpoint, which isn't {where}, the provider org "
                f"{m.org_id} manages; use a Claude Code profile")
    return (f"{who} uses {provider}, which can't run on {where}, the provider org "
            f"{m.org_id} manages")


def apply_profile(m: Managed | None, profile):
    """``profile`` as launched under ``m``: a native profile gets the managed
    endpoint, and a Claude profile a model the managed route serves."""
    if m is None:
        return profile
    if m.setup is not None:
        c = m.claude
        if profile.provider == "native" and m.codex is not None:
            return replace(profile, base_url=m.codex.base_url)
        if profile.provider == "claude" and c is not None and c.model_ids and profile.model not in c.model_ids:
            return replace(profile, model=c.default_model or c.model_ids[0])
        return profile
    c = m.config
    if c is None:
        return profile
    if profile.provider == "native" and c.endpoint:
        profile = replace(profile, base_url=c.endpoint)
        if c.model_ids and profile.model not in c.model_ids:
            profile = replace(profile, model=c.model_ids[0])
    elif profile.provider == "claude" and c.model_ids and profile.model not in c.model_ids:
        profile = replace(profile, model=c.model_ids[0])
    return profile


def route_env(m: Managed, provider: str) -> dict[str, str]:
    """The environment of ``provider``'s managed ``agent_setup`` route: for
    Claude, ``company_login.claude_env`` (the one builder); for Codex, the
    endpoint; for Antigravity, GOOGLE_GEMINI_BASE_URL. Empty for an agent with no
    managed route (native reads the profile's base_url, see :func:`apply_profile`)."""
    from brindle import company_login

    if provider == "claude" and m.claude is not None:
        return company_login.claude_env(m.org_id, m.claude)
    if provider == "codex" and m.codex is not None:
        return {"OPENAI_BASE_URL": m.codex.base_url}
    if provider == "antigravity" and m.antigravity is not None:
        return {"GOOGLE_GEMINI_BASE_URL": m.antigravity.base_url}
    return {}


def agent_env(m: Managed | None, provider: str) -> dict[str, str]:
    """What ``agents.agent_env`` adds for ``provider`` under ``m``: the
    managed route's variables (the personal keys are taken out of the pane
    by ``tmux.new_window``'s ``deny``, not set here)."""
    if m is None:
        return {}
    if getattr(m, "setup", None) is not None:
        return route_env(m, provider)
    if m.config is None:
        return {}
    return provider_env(m.config, provider)


def has_org_key(m: Managed | None, provider: str) -> bool:
    """Whether the org supplies a key (``key_env`` / ``key_helper``) for
    ``provider``'s agents. Reads nothing and runs nothing."""
    c = getattr(m, "config", None)
    return bool(c and (c.key_env or c.key_helper) and (provider, c.provider) in ORG_KEY_VARS)


def org_key_env(m: Managed | None, provider: str) -> dict[str, str]:
    """The org-owned key for a pane of ``provider``: ``{variable: key}``, or {}
    when the org sets none for it. ``key_env`` is read from this process's
    environment, ``key_helper`` run now (no shell, with a timeout). The key goes
    only into that pane's environment: never logged, cached or sent anywhere,
    and no failure message contains the helper's output. Raises
    :class:`ManagedUnavailable` when the key can't be had (fail closed)."""
    import os
    import subprocess

    if not has_org_key(m, provider):
        return {}
    c = m.config
    var = ORG_KEY_VARS[(provider, c.provider)]
    if c.key_env:
        value = os.environ.get(c.key_env, "").strip()
        if not value:
            raise ManagedUnavailable(f"org {m.org_id}'s key variable {c.key_env} isn't set")
        return {var: value}
    name = c.key_helper[0]
    try:
        out = subprocess.run(list(c.key_helper), shell=False, capture_output=True, text=True,
                             timeout=KEY_HELPER_TIMEOUT, stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        raise ManagedUnavailable(f"org {m.org_id}'s key helper {name!r} wasn't found") from None
    except (OSError, subprocess.TimeoutExpired) as e:
        why = "timed out" if isinstance(e, subprocess.TimeoutExpired) else "couldn't be run"
        raise ManagedUnavailable(f"org {m.org_id}'s key helper {name!r} {why}") from None
    value = out.stdout.strip()
    if out.returncode != 0 or not value:
        raise ManagedUnavailable(f"org {m.org_id}'s key helper {name!r} "
                                 + (f"failed (exit {out.returncode})" if out.returncode else "printed no key"))
    return {var: value}


def doctor_line(repo_root: str | None) -> tuple[bool, str] | None:
    """(ok, detail) for ``brindle doctor``: None when nothing is managed."""
    try:
        m = current(repo_root)
    except ManagedUnavailable as e:
        return False, str(e)
    return None if m is None else (True, m.describe())
