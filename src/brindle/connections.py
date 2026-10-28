"""How each provider connects: its own sign-in, an API key, or the person's cloud.

The person's choice (PUT /v1/providers/{p}/connection) is kept in
``~/.brindle/config.json`` under ``provider_connections``:
``{provider: {"method", "cloud", "env"}}``. A profile whose file doesn't say
``auth:`` runs with it (:func:`apply`): the method as its ``auth`` and the
route's non-secret variables (cloud_login.IDENTITY_ENV) under its own
``env.`` lines, which still win. No secret is kept here: a key goes to
`brindle keys`, a cloud login stays the cloud CLI's. An org's managed
identity still wins at launch (agents.agent_env, pane_auth), and a choice it
would override is refused with the reason (:func:`refusal`).
"""

from __future__ import annotations

import configparser
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from brindle import cloud_login, config

KEY = "provider_connections"        # ~/.brindle/config.json
PROVIDERS = ("claude", "codex", "antigravity", "opencode")
# The API's method -> the profile ``auth`` it stands for (profiles.AUTH_CHOICES).
METHODS = {"auto": "auto", "sign_in": "subscription", "api_key": "api_key", "cloud": "cloud"}

# Each provider's cloud routes: the switch the route sets, the variables the
# person may set for it (all in cloud_login.IDENTITY_ENV) and those it needs.
# An OpenCode route serves the profiles whose model is that model provider's.
ROUTES: dict[str, dict[str, dict]] = {
    "claude": {
        "bedrock": {"flag": {"CLAUDE_CODE_USE_BEDROCK": "1"}, "required": ("AWS_REGION",),
                    "optional": ("AWS_PROFILE",)},
        "vertex": {"flag": {"CLAUDE_CODE_USE_VERTEX": "1"},
                   "required": ("ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION"), "optional": ("CLOUDSDK_CONFIG",)},
        "foundry": {"flag": {"CLAUDE_CODE_USE_FOUNDRY": "1"}, "required": ("ANTHROPIC_FOUNDRY_RESOURCE",),
                    "optional": ("AZURE_CONFIG_DIR", "AZURE_TENANT_ID")},
    },
    "opencode": {
        "vertex": {"flag": {}, "model_provider": "google-vertex", "required": ("GOOGLE_CLOUD_PROJECT",),
                   "optional": ("VERTEX_LOCATION", "CLOUDSDK_CONFIG")},
        "azure": {"flag": {}, "model_provider": "azure", "required": ("AZURE_RESOURCE_NAME",),
                  "optional": ("AZURE_CONFIG_DIR", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID")},
    },
}

# The usual regions for each route, for the app's picker; any region is accepted.
REGIONS = {
    "bedrock": ["us-east-1", "us-east-2", "us-west-2", "eu-central-1", "eu-west-1", "eu-west-3",
                "ap-northeast-1", "ap-south-1", "ap-southeast-1", "ap-southeast-2"],
    "vertex": ["global", "us-east5", "us-central1", "europe-west1", "europe-west4", "asia-southeast1"],
}


class Invalid(ValueError):
    """The request names no such method, route or setting."""


class Refused(Exception):
    """The org decides how this provider connects: the message says how."""


def saved(provider: str) -> dict:
    """The person's saved choice for ``provider``, or {}."""
    got = config.user_settings().get(KEY)
    entry = got.get(provider) if isinstance(got, dict) else None
    return entry if isinstance(entry, dict) and entry.get("method") in METHODS else {}


def route_env(provider: str, route: str | None, env: Mapping[str, str]) -> dict[str, str]:
    """The variables that select ``route`` for ``provider``: its switch and settings."""
    if not route:
        return {}
    spec = ROUTES[provider][route]
    return {**({"BRINDLE_CLOUD_ROUTE": route} if provider == "opencode" else {}), **spec["flag"], **env}


def apply(profile):
    """``profile`` with the person's saved connection for its provider: the
    method as ``auth``, the route's variables under the profile's own env
    lines. An OpenCode cloud route only for a model it serves."""
    c = saved(profile.provider)
    if not c:
        return profile
    env = dict(profile.env)
    if c["method"] == "cloud":
        spec = ROUTES.get(profile.provider, {}).get(c.get("cloud") or "")
        if spec is None:
            return profile
        if "model_provider" in spec and (profile.model or "").split("/")[0] != spec["model_provider"]:
            return profile
        env = {**spec["flag"], **(c.get("env") or {}), **env}
    return replace(profile, auth=METHODS[c["method"]], env=env)


def org_managed(provider: str, repo: str | None = None) -> str | None:
    """Why the org, not the person, decides how ``provider`` connects, or None."""
    from brindle import company_identity, providers
    from brindle.pro import managed_models

    if provider == "claude":
        if what := providers.claude_org_managed():
            return f"Claude Code's managed settings on this machine ({what}) decide how Claude connects"
        if company_identity.enforced_route() is not None:
            return "your org enforces its company identity for Claude Code"
    try:
        m = managed_models.current(repo)
    except managed_models.ManagedUnavailable as e:
        return str(e)
    if m is None:
        return None
    route = {"claude": m.claude, "codex": m.codex, "antigravity": m.antigravity}.get(provider)
    if route is not None:
        return f"{m.describe()}: the org decides how {provider} connects"
    return None


def refusal(provider: str, method: str, env: Mapping[str, str], repo: str | None = None) -> str | None:
    """Why the org doesn't allow ``provider`` to connect by ``method`` with
    ``env``, or None."""
    from brindle import pane_auth
    from brindle.pro import managed_models
    from brindle.profiles import Profile

    if why := org_managed(provider, repo):
        return why
    try:
        m = managed_models.current(repo)
    except managed_models.ManagedUnavailable as e:
        return str(e)
    if m is not None:
        keys = pane_auth.API_KEYS.get(provider, ())
        if method == "api_key" and keys and all(k in m.denied_keys for k in keys):
            return f"org {m.org_id} doesn't allow personal API keys ({', '.join(keys)})"
        probe = Profile(name=f"{provider} connection", description="", provider=provider, prompt="",
                        env=dict(env), auth=METHODS[method])
        if why := managed_models.refusal(m, probe):
            return why
    if repo:
        from brindle.service.routing import RoutingPolicy

        access = RoutingPolicy.load(repo).access
        if access.error or (access.org_id is not None and access.refusal(provider)):
            return access.error or access.refusal(provider)
    return None


def choose(provider: str, method: str, cloud: str | None = None, settings: Mapping | None = None,
           repo: str | None = None) -> dict:
    """Save how ``provider`` connects and return the saved entry. Raises
    :class:`Invalid` for an unknown method, route or setting, and
    :class:`Refused` when the org's policy doesn't allow it."""
    if provider not in PROVIDERS:
        raise Invalid(f"provider must be one of {', '.join(PROVIDERS)}")
    if method not in METHODS:
        raise Invalid(f"method must be one of {', '.join(METHODS)}")
    settings = dict(settings or {})
    env: dict[str, str] = {}
    if method == "cloud":
        routes = ROUTES.get(provider, {})
        if not routes:
            raise Invalid(f"{provider} can't connect through a cloud in brindle yet; "
                          "use OpenCode with a google-vertex/ or azure/ model")
        if cloud not in routes:
            raise Invalid(f"{provider} clouds: {', '.join(routes)}")
        spec = routes[cloud]
        allowed = (*spec["required"], *spec["optional"])
        for name, value in settings.items():
            if name not in allowed:
                raise Invalid(f"{cloud} settings: {', '.join(allowed)}")
            if not isinstance(value, str) or not value.isprintable() or len(value) > 512:
                raise Invalid(f"{name} must be one line of text")
        env = {k: v.strip() for k, v in settings.items() if v.strip()}
        missing = [n for n in spec["required"] if n not in env]
        if missing:
            raise Invalid(f"{cloud} needs {', '.join(missing)}")
        if provider == "opencode" and cloud == "azure" and "AZURE_CONFIG_DIR" not in env:
            # OpenCode's Azure route reads the Azure CLI's login from this directory.
            env["AZURE_CONFIG_DIR"] = os.environ.get("AZURE_CONFIG_DIR") or str(Path.home() / ".azure")
    elif cloud or settings:
        raise Invalid("cloud and settings go with method cloud")
    if why := refusal(provider, method, route_env(provider, cloud, env), repo):
        raise Refused(why)
    entry = {"method": method, **({"cloud": cloud, "env": env} if method == "cloud" else {})}
    current = config.user_settings().get(KEY)
    all_ = dict(current) if isinstance(current, dict) else {}
    if method == "auto":
        all_.pop(provider, None)
    else:
        all_[provider] = entry
    config.set_user(KEY, all_)
    return entry


def view(provider: str, repo: str | None = None) -> dict:
    """How ``provider`` connects now: the saved choice, else what the
    environment selects; for a cloud, its login's state."""
    if provider not in PROVIDERS:
        raise Invalid(f"provider must be one of {', '.join(PROVIDERS)}")
    c = saved(provider)
    route, env = c.get("cloud"), dict(c.get("env") or {})
    if not c and provider == "claude":
        route = cloud_login.provider_from_env({k: v for k, v in os.environ.items() if k != "BRINDLE_CLOUD_ROUTE"})
        spec = ROUTES["claude"].get(route or "")
        env = {k: os.environ[k] for k in (*spec["required"], *spec["optional"])
               if k in os.environ} if spec else {}
    login = None
    if route:
        s = cloud_login.status(route, env=route_env(provider, route, env))
        login = {"valid": s.ok, "expired": s.expired, "expires_at": s.expires_at,
                 "renew_command": s.refresh_cmd, "note": s.note}
    return {"provider": provider, "method": c.get("method") or ("cloud" if route else "auto"),
            "saved": bool(c), "cloud": route, "settings": env, "login": login,
            "managed": org_managed(provider, repo), "clouds": list(ROUTES.get(provider, {}))}


def sign_in(provider: str, *, now: float) -> dict:
    """Open the browser sign-in the saved cloud route's login needs
    (cloud_login.sign_in): the person pressed for it, so they are present."""
    c = saved(provider)
    if c.get("method") != "cloud":
        raise Invalid(f"{provider} doesn't connect through a cloud: choose one first")
    route = c["cloud"]
    result = cloud_login.sign_in(route, env=route_env(provider, route, c.get("env") or {}), last_input=now)
    return {"provider": provider, "cloud": route, "result": result}


# -- what this machine is set up with (GET /v1/cloud/options) ------------------------------------


def _aws_profiles(path: Path) -> list[dict]:
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        cp.read(path, encoding="utf-8")
    except (OSError, configparser.Error, UnicodeDecodeError):
        return []
    out = []
    for section in cp.sections():
        parts = section.split(None, 1)
        if section == "default":
            name = "default"
        elif parts[0] == "profile" and len(parts) == 2:
            name = " ".join(parts[1].split())
        else:
            continue
        p = dict(cp.items(section))
        out.append({"name": name, "sso": bool(p.get("sso_session") or p.get("sso_start_url")),
                    "region": p.get("region") or None})
    return out


def _gcloud(env: Mapping[str, str], home: Path) -> dict:
    root = Path(env.get("CLOUDSDK_CONFIG") or home / ".config" / "gcloud").expanduser()
    try:
        active = (root / "active_config").read_text(encoding="utf-8").strip() or "default"
    except OSError:
        active = "default"
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        cp.read(root / "configurations" / f"config_{active}", encoding="utf-8")
    except (OSError, configparser.Error, UnicodeDecodeError):
        pass
    return {"adc": cloud_login._gcloud_adc_file(env, home).is_file(),
            "project": cp.get("core", "project", fallback=None) or None,
            "region": cp.get("compute", "region", fallback=None) or None}


def _azure(env: Mapping[str, str], home: Path) -> dict | None:
    root = Path(env.get("AZURE_CONFIG_DIR") or home / ".azure").expanduser()
    try:
        subs = json.loads((root / "azureProfile.json").read_text(encoding="utf-8-sig")).get("subscriptions")
    except (OSError, ValueError, AttributeError):
        return None
    current = next((s for s in subs or [] if isinstance(s, dict) and s.get("isDefault")), None)
    if current is None or not current.get("tenantId"):
        return None
    return {"tenant_id": str(current["tenantId"]), "subscription_id": current.get("id"),
            "subscription_name": current.get("name")}


def options(env: Mapping[str, str] | None = None, home: Path | None = None) -> dict:
    """The cloud choices this machine has, read from the CLIs' own files
    (no CLI runs, no secret is read)."""
    env = os.environ if env is None else env
    home = home or Path.home()
    aws = Path(env.get("AWS_CONFIG_FILE") or home / ".aws" / "config").expanduser()
    return {"aws_profiles": _aws_profiles(aws), "gcloud": _gcloud(env, home), "azure": _azure(env, home),
            "regions": REGIONS}
