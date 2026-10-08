"""Enterprise ``managed_models``: run the org's workers on the provider the
org chose, and keep personal API keys out of their panes.

The org policy (``brindle.pro.team_policy``) carries two fields, enforced only
with the ``managed_models`` feature (``license.has``, fail closed: no verified
entitlement, nothing is managed):

* ``provider_config``: Bedrock or Vertex (with a region and model ids), or an
  Azure / OpenAI-compatible endpoint. Every launched agent gets that provider:

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

  A profile that points elsewhere (another endpoint, another cloud, a
  provider the managed one can't serve) is refused with a message saying why.
* ``deny_personal_keys``: the person's own model keys (``secrets.
  PERSONAL_KEYS``) are taken out of every agent pane, and a profile that
  brings its own is refused. This can't stop a Claude subscription/keychain
  login unless ``provider_config`` routes Claude Code to Bedrock, Vertex or
  Foundry, so pair the two.

Without the feature both fields are ignored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from brindle import secrets
from brindle.pro.team_policy import OrgPolicy, ProviderConfig

log = logging.getLogger(__name__)

# What picks a Claude Code (or Codex) backend: a profile setting any of these
# points the agent somewhere the org didn't choose.
ROUTING_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
               "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE", "CLAUDE_CODE_USE_BEDROCK",
               "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "OPENAI_BASE_URL",
               "AWS_REGION", "AWS_DEFAULT_REGION", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID")

NAMES = {"bedrock": "AWS Bedrock", "vertex": "Google Vertex AI", "azure": "Azure",
         "openai-compatible": "an OpenAI-compatible endpoint", "anthropic": "the Anthropic API"}

# The variable the org's key (``key_env`` / ``key_helper``) is set as in a pane,
# by the agent's provider, for the managed provider that carries a key.
ORG_KEY_VARS = {("claude", "anthropic"): "ANTHROPIC_API_KEY",
                ("codex", "openai-compatible"): "OPENAI_API_KEY"}
KEY_HELPER_TIMEOUT = 15.0


class ManagedUnavailable(Exception):
    """The org's managed-models policy applies but can't be read: fail closed."""


@dataclass(frozen=True)
class Managed:
    org_id: str
    config: ProviderConfig | None
    deny_personal_keys: bool

    @property
    def denied_keys(self) -> tuple[str, ...]:
        return secrets.PERSONAL_KEYS if self.deny_personal_keys else ()

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
        if self.deny_personal_keys:
            parts.append("personal API keys denied")
        return f"org {self.org_id}: " + "; ".join(parts)


def current(repo_root: str | None) -> Managed | None:
    """The managed-models settings that apply, None when there are none (the
    feature isn't entitled, no team org, or the org set neither field).
    Raises :class:`ManagedUnavailable` when they apply but can't be read."""
    from brindle.pro import team_policy

    try:
        p = team_policy.managed_models(repo_root or "")
    except Exception as e:  # noqa: BLE001 - fail closed on anything
        raise ManagedUnavailable(f"couldn't read the org's managed-models policy ({e})") from e
    if p is None:
        return None
    if not isinstance(p, OrgPolicy):
        raise ManagedUnavailable(p.reason)
    if p.provider_config is None and not p.deny_personal_keys:
        return None
    return Managed(p.org_id, p.provider_config, p.deny_personal_keys)


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
                env["ANTHROPIC_SMALL_FAST_MODEL"] = cfg.model_ids[-1]
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


def apply_profile(m: Managed | None, profile):
    """``profile`` as launched under ``m``: a native profile gets the managed
    endpoint, and a Claude profile a model the managed provider serves."""
    if m is None or m.config is None:
        return profile
    c = m.config
    if profile.provider == "native" and c.endpoint:
        profile = replace(profile, base_url=c.endpoint)
        if c.model_ids and profile.model not in c.model_ids:
            profile = replace(profile, model=c.model_ids[0])
    elif profile.provider == "claude" and c.model_ids and profile.model not in c.model_ids:
        profile = replace(profile, model=c.model_ids[0])
    return profile


def agent_env(m: Managed | None, provider: str) -> dict[str, str]:
    """What ``agents.agent_env`` adds for ``provider`` under ``m``: the
    managed provider's variables (the personal keys are taken out of the pane
    by ``tmux.new_window``'s ``deny``, not set here)."""
    if m is None or m.config is None:
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
