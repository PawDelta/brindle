"""How each provider connects: its own sign-in, an API key, or the person's cloud.

The person's choice (PUT /v1/providers/{p}/connection) is kept in
``~/.brindle/config.json`` under ``provider_connections``:
``{provider: {"method", "cloud", "env"}}``. A profile whose file doesn't say
``auth:`` runs with it (:func:`apply`): the method as its ``auth`` and the
route's non-secret variables (cloud_login.IDENTITY_ENV) under its own
``env.`` lines, which still win. No secret is kept here: a key goes to
`brindle keys`, a cloud login stays the cloud CLI's. Antigravity's API key
also sets agy's own ``modelProvider`` (:func:`_agy_setup`), put back when the
key goes. An org's managed
identity still wins at launch (agents.agent_env, pane_auth), and a choice it
would override is refused with the reason (:func:`refusal`).
"""

from __future__ import annotations

import configparser
import json
import os
import sys
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


# Claude Code's endpoint overrides: an inherited one would send a chosen
# route's calls elsewhere (Foundry takes a base URL or a resource, never both).
CLAUDE_ENDPOINTS = ("ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_FOUNDRY_BASE_URL")


def _selectors(provider: str, method: str, route: str | None) -> dict[str, str]:
    """Blanks for the inherited variables that would pick another route or
    endpoint than the chosen one (a Claude cloud switch, a cloud endpoint, a
    gateway under sign-in or a cloud): the choice, not whatever the shell or
    a saved company env exported, decides. An API key keeps its gateway."""
    if provider != "claude":
        return {}
    keep = ROUTES["claude"][route]["flag"] if route else {}
    blanks = {flag: "" for flag in (*cloud_login._FLAGS.values(), *CLAUDE_ENDPOINTS) if flag not in keep}
    if method != "api_key":
        blanks["ANTHROPIC_BASE_URL"] = ""
    return blanks


def route_env(provider: str, route: str | None, env: Mapping[str, str]) -> dict[str, str]:
    """The variables that select ``route`` for ``provider``: its switch and
    settings, over blanks for any other route's switch."""
    if not route:
        return {}
    spec = ROUTES[provider][route]
    return {**_selectors(provider, "cloud", route),
            "BRINDLE_CLOUD_ROUTE": route if provider == "opencode" else "claude", **spec["flag"], **env}


def identity_env(provider: str, route: str, env: Mapping[str, str]) -> dict[str, str]:
    """What a pane on this route signs in with, as the launch builds it: for
    OpenCode, also the isolated store (cloud_login.profile_env) its login goes in."""
    from brindle.profiles import Profile

    got = route_env(provider, route, env)
    if provider != "opencode":
        return got
    probe = Profile(name="connection", description="", provider=provider, prompt="", auth="cloud",
                    model=f"{ROUTES[provider][route]['model_provider']}/connection", env=dict(env))
    return {**got, **cloud_login.profile_env(probe, env)}


def apply(profile, repo_root: str | None = None):
    """``profile`` with the person's saved connection for its provider: the
    method as ``auth``, the route's variables under the profile's own env
    lines. An OpenCode cloud route only for a model it serves; nothing while
    the org decides how the provider connects."""
    c = saved(profile.provider)
    if not c or _org(profile.provider, repo_root)[0]:
        return profile
    route = c.get("cloud") if c["method"] == "cloud" else None
    extra = _selectors(profile.provider, c["method"], route)
    if c["method"] == "cloud":
        spec = ROUTES.get(profile.provider, {}).get(route or "")
        if spec is None:
            return profile
        if "model_provider" in spec and (profile.model or "").split("/")[0] != spec["model_provider"]:
            return profile
        extra = {**extra, **spec["flag"], **(c.get("env") or {})}
    return replace(profile, auth=METHODS[c["method"]], env={**extra, **profile.env})


def _org(provider: str, repo: str | None) -> tuple[str | None, dict[str, str]]:
    """Why the org, not the person, decides how ``provider`` connects (None
    when it doesn't), and the variables of the route it sets, when it sets one."""
    from brindle import company_identity, providers
    from brindle.pro import managed_models

    if provider == "claude":
        if what := providers.claude_org_managed():
            return f"Claude Code's managed settings on this machine ({what}) decide how Claude connects", {}
        if (enforced := company_identity.enforced_route_env()) is not None:
            return "your org enforces its company identity for Claude Code", dict(enforced)
    try:
        m = managed_models.current(repo)
    except managed_models.ManagedUnavailable as e:
        return str(e), {}
    # An enforced agent_setup route or an older provider_config, as the launch applies it.
    env = managed_models.agent_env(m, provider)
    if env:
        return f"{m.describe()}: the org decides how {provider} connects", env
    return None, {}


def org_managed(provider: str, repo: str | None = None) -> str | None:
    """Why the org, not the person, decides how ``provider`` connects, or None."""
    return _org(provider, repo)[0]


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
                        env={k: v for k, v in env.items() if v}, auth=METHODS[method])
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

    def change(current):
        # Under the config writer's lock: another provider's choice saved meanwhile is kept.
        all_ = dict(current) if isinstance(current, dict) else {}
        if provider == "antigravity":
            entry.update(_agy_setup(all_.get(provider), method))
        if method == "auto":
            all_.pop(provider, None)
        else:
            all_[provider] = entry
        return all_

    config.update_user(KEY, change)
    return entry


# The modelProvider agy's settings had before brindle set "gemini" for an API
# key, kept in the saved choice so that leaving the key puts it back.
AGY_WAS = "agy_model_provider_was"


def _agy_setup(old, method: str) -> dict:
    """agy reads GEMINI_API_KEY only with ``"modelProvider": "gemini"`` in its
    one settings.json, which the person's own agy shares (it has no other
    config): for an API key set it, recording what it replaced; leaving the
    key, put that back. What to keep in the saved entry."""
    from brindle import providers

    old = old if isinstance(old, dict) else {}
    try:
        if method == "api_key":
            kept = {AGY_WAS: old[AGY_WAS] if AGY_WAS in old else providers.agy_model_provider()}
            providers.set_agy_gemini()
            return kept
        if AGY_WAS in old:
            _agy_restore(old[AGY_WAS])
    except (OSError, ValueError) as e:
        raise Invalid(f"couldn't update Antigravity's settings ({providers.agy_settings_path()}): {e}") from e
    return {}


def _agy_restore(was: str | None) -> None:
    """agy's modelProvider back to ``was``, unless it was changed by hand since brindle set it."""
    from brindle import providers

    if providers.agy_model_provider() == "gemini" and was != "gemini":
        providers.set_agy_model_provider(was)


def key_removed(name: str) -> None:
    """A stored key was removed: GEMINI_API_KEY gives agy its modelProvider
    back (see _agy_setup); the choice stays, and the connection's ``ready``
    asks for a key again."""
    from brindle import pane_auth

    if name not in pane_auth.API_KEYS["antigravity"]:
        return

    def change(current):
        all_ = dict(current) if isinstance(current, dict) else {}
        entry = all_.get("antigravity")
        if isinstance(entry, dict) and AGY_WAS in entry:
            try:
                _agy_restore(entry[AGY_WAS])
            except (OSError, ValueError):
                return all_      # the key is gone either way; tried again on the next removal or choice
            all_["antigravity"] = {k: v for k, v in entry.items() if k != AGY_WAS}
        return all_

    config.update_user(KEY, change)


def _login(route: str, env: Mapping[str, str]) -> dict:
    s = cloud_login.status(route, env=env)
    return {"valid": s.ok, "expired": s.expired, "expires_at": s.expires_at,
            "renew_command": s.refresh_cmd, "note": s.note}


def view(provider: str, repo: str | None = None, db=None) -> dict:
    """How ``provider`` connects now: the org's route when it decides, else
    the saved choice, else what the environment selects; for a cloud, the
    state of the login that route uses. ``choice`` is the saved choice,
    which an org's route overrides; method "org" when the org decides with
    no cloud route here. ``ready``: whether an agent would launch with it now;
    ``blocked_by``: a login in the way; ``stopped_agents`` (with ``db``): the
    agents its credentials stopped."""
    if provider not in PROVIDERS:
        raise Invalid(f"provider must be one of {', '.join(PROVIDERS)}")
    c = saved(provider)
    choice = {"method": c["method"], "cloud": c.get("cloud"), "settings": dict(c.get("env") or {})} if c else None
    managed, org_env = _org(provider, repo)
    login = None
    if managed:
        route = cloud_login.provider_from_env({**org_env, "BRINDLE_CLOUD_ROUTE": "claude"}) \
            if provider == "claude" and org_env else None
        method = "cloud" if route else "org"
        env = {k: v for k, v in org_env.items() if k in cloud_login.IDENTITY_ENV and k not in cloud_login._FLAGS.values()}
        if route:
            login = _login(route, org_env)
    else:
        method, route, env = (c["method"], c.get("cloud"), dict(c.get("env") or {})) if c else ("auto", None, {})
        if not c and provider == "claude":
            route = cloud_login.provider_from_env({**os.environ, "BRINDLE_CLOUD_ROUTE": "claude"})
            spec = ROUTES["claude"].get(route or "")
            env = {k: os.environ[k] for k in (*spec["required"], *spec["optional"])
                   if k in os.environ} if spec else {}
            method = "cloud" if route else "auto"
        if route:
            login = _login(route, identity_env(provider, route, env))
    blocked_by = _blocked_by(provider, method)
    return {"provider": provider, "method": method, "choice": choice, "cloud": route, "settings": env,
            "login": login, "managed": managed, "clouds": list(ROUTES.get(provider, {})),
            "ready": ready(provider, method, login, blocked_by), "blocked_by": blocked_by,
            "stopped_agents": stopped_agents(db, provider) if db is not None else []}


def _blocked_by(provider: str, method: str) -> str | None:
    """The login that keeps ``method`` from being used: Codex signed in with
    ChatGPT runs interactive agents on the plan whatever key is set
    (providers.CODEX_INTERACTIVE_IGNORES_KEYS), until brindle's copy signs out."""
    from brindle import providers

    if provider == "codex" and method == "api_key" and providers.codex_chatgpt_login():
        return "codex_login"
    return None


def _key(provider: str) -> tuple[str | None, str | None]:
    """The API key an agent of ``provider`` would get (the environment's,
    else a stored one) and how its last check came out (verified, rejected,
    unchecked; None for one from the environment): (None, None) without one."""
    from brindle import keystore, pane_auth

    names = pane_auth.API_KEYS.get(provider, ())
    for name in names:
        if os.environ.get(name):
            return name, None
    for name in names:
        try:
            rec = keystore.get_record(name)
        except keystore.CredentialError:
            continue
        if rec.get("value"):
            return name, rec.get("status")
    return None, None


def ready(provider: str, method: str, login: dict | None, blocked_by: str | None) -> dict:
    """Whether an agent of ``provider`` would launch with ``method`` right
    now, and if not why and what the app can offer (``action``): install the
    CLI, sign in, sign brindle's copy out, renew a cloud login, or set a key.
    A key's own check (verified, rejected) is the key's; a rejected one only
    makes a method that needs it not ready."""
    from brindle import pane_auth, providers, providers_install

    name = providers_install.NAMES[provider]

    def no(reason: str, action: str | None) -> dict:
        return {"ok": False, "reason": reason, "action": action}

    if providers_install.found(provider) is None:
        return no(f"{name} isn't installed", "install")
    if providers_install.turned_off(provider):
        return no(f"{name} is turned off in brindle", None)
    if login is not None and not login["valid"]:
        if login["expired"]:
            return no("the cloud login has expired", "renew")
        return no("not signed in to the cloud", "sign_in")
    if method in ("cloud", "org"):
        return {"ok": True, "reason": None, "action": None}
    key, status = _key(provider)
    if method == "api_key" and pane_auth.API_KEYS.get(provider):
        if key is None:
            return no(f"no API key: store {pane_auth.API_KEYS[provider][0]}", "set_key")
        if status == "rejected":
            return no(f"{key} was rejected by {name}", "set_key")
        if blocked_by == "codex_login":
            return no(providers.CODEX_INTERACTIVE_IGNORES_KEYS, "sign_out")
        if provider == "antigravity":
            if not providers.agy_uses_gemini_key():
                return no(providers.agy_ignores_key_message(), "set_key")
            where = "this Mac" if sys.platform == "darwin" else "this computer"
            return {"ok": True, "reason": f"Antigravity on {where} now uses your Gemini key", "action": None}
        return {"ok": True, "reason": None, "action": None}
    if method == "auto" and key is not None:
        # auto runs with the key ahead of any login (pane_auth), a rejected one too.
        if status == "rejected":
            return no(f"{key} was rejected by {name}", "set_key")
        return {"ok": True, "reason": None, "action": None}
    if provider != "opencode" and _signed_out(provider, method):
        return no(f"{name} isn't signed in", "sign_in")
    return {"ok": True, "reason": None, "action": None}


def _signed_out(provider: str, method: str) -> bool:
    """Whether the CLI's own login is definitely missing, probed as the pane
    would run: without the provider's API keys and, under sign_in, without
    another route's selectors (_selectors), so an exported key can't stand in
    for the login; a subscription token stays."""
    from brindle import pane_auth, provider_homes, providers

    pending = provider_homes.confirm_login(provider)      # a login just finished in the app
    if pending is not None:
        return not pending
    if provider_homes.signed_out(provider):
        return True
    env = {**(_selectors(provider, method, None) if method == "sign_in" else {}),
           **{k: "" for k in pane_auth.API_KEYS.get(provider, ())}}
    return providers.signed_out(provider, env) is not None


def stopped_agents(db, provider: str) -> list[dict]:
    """The agents of ``provider`` stopped because its credentials were
    refused (failed_why auth, service.inbox's kind "credentials")."""
    rows = db.conn.execute(
        "SELECT id, name, profile FROM agents WHERE provider = ? AND status = 'failed' AND failed_why = 'auth'"
        " AND dismissed_at IS NULL ORDER BY created_at", (provider,)).fetchall()
    return [{"id": r["id"], "name": r["name"] or r["profile"], "profile": r["profile"]} for r in rows]


def sign_in_target(provider: str) -> tuple[str, dict[str, str]]:
    """The saved cloud route and the identity its sign-in is for. Raises
    :class:`Invalid` with no cloud chosen and :class:`Refused` while the org
    decides how ``provider`` connects."""
    c = saved(provider)
    if c.get("method") != "cloud" or c.get("cloud") not in ROUTES.get(provider, {}):
        raise Invalid(f"{provider} doesn't connect through a cloud: choose one first")
    if why := org_managed(provider):
        raise Refused(why)
    return c["cloud"], identity_env(provider, c["cloud"], c.get("env") or {})


def sign_in(provider: str, *, run, probe=None, say=print, extra_env: Mapping[str, str] | None = None) -> dict:
    """The cloud sign-in the person asked for, for the saved route, a first
    one or a renewal (cloud_login.requested_sign_in: one at a time per login).
    For OpenCode's Azure route it also connects the pane's own OpenCode store."""
    route, env = sign_in_target(provider)
    outcome, code = cloud_login.requested_sign_in(env={**env, **(extra_env or {})}, run=run, probe=probe, say=say)
    return {"provider": provider, "cloud": route, "result": outcome, "exit_code": code}


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
