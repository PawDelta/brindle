"""What a provider's CLI would stop on before a session starts, so the
desktop app can ask the person for it instead of a session opening on the
CLI's own screen.

brindle answers what the person already chose by using brindle, at launch
(agents._launch): folder trust for the repo they started brindle in and the
worktrees it makes there (Claude Code's ``hasTrustDialogAccepted``, Codex's
``trust_level``). What is left is listed here as items, each with the daemon
route that resolves it:

- ``signin``: the CLI isn't signed in (POST /v1/providers/{p}/login).
- ``key``: a key the provider or a profile needs is missing (PUT /v1/keys/{name}).
- ``trust``: the person told the CLI not to trust this repo (POST /v1/providers/{p}/trust).
- ``consent``: a first-run notice or agreement only the person can accept,
  with the provider's own link. Never accepted for them: after they confirm
  in the app, POST /v1/providers/{p}/consent records it where the CLI keeps a
  record; with no record to keep, the action is ``open_provider_once``.
- ``setup``: something to install or configure outside brindle (no route).

Every launch is checked with the identity it runs as (agents.launch_blockers):
the daemon answers 409 with the items before starting a chat or worker, and
agents._launch refuses any worker launch they block (PreflightBlocked), so a
CLI never opens on a screen nobody will answer. A screen no check foresaw
that asks for terms or a licence is left to the person (prompt_answer.PERSONAL)."""

from __future__ import annotations

import dataclasses
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

PROVIDERS = ("claude", "codex", "antigravity", "opencode", "native")

CLAUDE_SECURITY_URL = "https://docs.claude.com/en/docs/claude-code/security"


@dataclass(frozen=True)
class Action:
    name: str           # login | set_key | trust | consent | open_provider_once
    method: str
    path: str
    body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Item:
    kind: str           # signin | key | trust | consent | setup | off (turned off in brindle)
    id: str             # stable, e.g. "claude.onboarding": what a consent names when recorded
    provider: str
    title: str
    detail: str
    action: Action | None = None
    link: str | None = None

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class Blocked:
    """A launch that would stop on ``items``: the daemon answers 409 with them."""
    items: list[Item]

    def to_json(self) -> dict[str, Any]:
        return {"preflight": [i.to_json() for i in self.items]}


def _login(provider: str) -> Action:
    return Action("login", "POST", f"/v1/providers/{provider}/login")


def _set_key(name: str) -> Action:
    return Action("set_key", "PUT", f"/v1/keys/{name}")


def _consent(provider: str, item_id: str, **body: str) -> Action:
    if item_id not in RECORDERS:
        return Action("open_provider_once", "POST", f"/v1/providers/{provider}/login")
    return Action("consent", "POST", f"/v1/providers/{provider}/consent", {"id": item_id, **body})


def key_fingerprint(value: str) -> str:
    """Which key, without the key: a short hash a consent is bound to, so a
    confirmation records the key the person was shown and no other."""
    import hashlib

    return hashlib.sha256(value.strip().encode()).hexdigest()[:16]


class StaleConsent(ValueError):
    """What the person confirmed isn't what would be recorded now."""


def _key_value(name: str, env: dict[str, str] | None = None) -> str | None:
    """The value of key ``name`` the CLI gets: from ``env`` when it names it
    (blank: denied to it), else brindle's environment or key store."""
    from brindle import keystore

    if env is not None and name in env:
        return env[name].strip() or None
    if os.environ.get(name):
        return os.environ[name].strip() or None
    try:
        return keystore.get_key(name)
    except keystore.CredentialError:
        return None


def _binary(provider: str) -> str:
    from brindle import antigravity, opencode, providers

    return {"claude": providers.claude_binary, "codex": providers.codex_binary,
            "antigravity": antigravity.binary, "opencode": opencode.binary}[provider]()


def turned_off(provider: str) -> Item | None:
    """The item that stops every launch on ``provider`` while the person has
    it turned off in brindle (DELETE /v1/providers/{provider}), or None."""
    from brindle import doctor, providers_install

    if provider not in providers_install.PROVIDERS or not providers_install.turned_off(provider):
        return None
    name = doctor.CLI_INSTALL[provider][0]
    return Item("off", f"{provider}.off", provider, f"Turn {name} back on",
                f"{name} is turned off in brindle, so nothing runs on it: pick an install of it "
                "(or install it) in brindle's provider settings to turn it back on")


def check(provider: str, repo_root: str | None = None, *, env: dict[str, str] | None = None,
          signed_in: bool = False, profile=None, launch: dict[str, str] | None = None,
          check_cloud: bool = True) -> list[Item]:
    """What would stop a session on ``provider`` in ``repo_root`` before its
    first turn; empty when nothing would. ``env``: the environment the CLI
    signs in with (agents.launch_identity; a blank value is a key it won't
    get), else brindle's own. ``signed_in``: the org's key signs it in.
    ``profile``: for native, the one profile to check (else all of them).
    ``launch``: the ``repo`` and ``profile`` that identity is for, which a
    consent's action carries so recording it resolves the same identity."""
    from brindle import cloud_login, doctor, providers

    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
    if provider == "native":
        return _native(repo_root, profile)
    off = turned_off(provider)
    if off:
        return [off]
    items = []
    if not shutil.which("tmux"):
        items.append(Item("setup", "tmux", provider, "Install tmux",
                          f"brindle runs every agent in a tmux window. Install: {doctor.tmux_install()}"))
    exe = _binary(provider)
    if not (providers.which_cli(exe) or os.path.isfile(exe)):
        name, how = doctor.CLI_INSTALL[provider]
        return items + [Item("setup", f"{provider}.install", provider, f"Install {name}",
                             f"{name} (`{exe}`) isn't on PATH. Install: {how}")]
    cloud = (cloud_login.configured_status(env or {})
             if check_cloud and ((provider == "claude" and not providers.claude_org_managed()) or
                                 (provider == "opencode" and (env or {}).get("BRINDLE_CLOUD_ROUTE"))) else None)
    if cloud is not None and not cloud.ok:
        command = cloud_login.login_command(env or {})
        action = (Action("login", "POST", f"/v1/providers/{provider}/login", launch or {}) if command else
                  Action("check", "POST", f"/v1/providers/{provider}/preflight", launch or {}))
        items.append(Item("signin", f"{provider}.cloud", provider, "Sign in to your cloud provider",
                          f"{cloud.note}; {cloud.refresh_cmd}" if command else
                          "Refresh the configured cloud credential file, then check again.", action))
    elif provider == "antigravity" and providers.agy_uses_gemini_key() and not _key_value("GEMINI_API_KEY", env):
        items.append(Item("key", "key.GEMINI_API_KEY", provider, "Add your Gemini API key",
                          "Antigravity is set to the Gemini API, which needs GEMINI_API_KEY",
                          _set_key("GEMINI_API_KEY")))
    elif not signed_in:
        why = providers.signed_out(provider, env)
        if why:
            items.append(Item("signin", f"{provider}.signin", provider,
                              f"Sign in to {doctor.CLI_INSTALL[provider][0]}", why, _login(provider)))
    if provider == "claude":
        items += _claude_consents(env, launch or {})
    if provider == "codex" and repo_root:
        items += _codex_trust(repo_root)
    return items


def _claude_consents(env: dict[str, str] | None, launch: dict[str, str]) -> list[Item]:
    from brindle import providers

    out = []
    if not providers.claude_onboarded():
        out.append(Item(
            "consent", "claude.onboarding", "claude", "Claude Code's security notes",
            "Before its first session Claude Code shows its security notes: Claude can make mistakes, "
            "so review what it does, and code or content it reads can try to steer it (prompt "
            "injection). Read them, then confirm to continue (or run `claude` once in a terminal); "
            "brindle records it as Claude Code does.",
            _consent("claude", "claude.onboarding"), CLAUDE_SECURITY_URL))
    # Claude Code asks about a key only when it would use one: not under its
    # managed settings, a cloud backend or a token, which come first.
    first = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
             "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")
    value = _key_value("ANTHROPIC_API_KEY", env)
    if value and not providers.claude_org_managed() and not any(_key_value(k, env) for k in first) \
            and not providers.api_key_approved(value):
        out.append(Item(
            "consent", "claude.api_key", "claude", "Use ANTHROPIC_API_KEY in Claude Code?",
            f"Claude Code asks once whether to use the key ending {value[-4:]}; with it, usage "
            "is billed to that key, not a subscription login.",
            _consent("claude", "claude.api_key", **launch, key=key_fingerprint(value))))
    return out


def _codex_trust(repo_root: str) -> list[Item]:
    from brindle import providers

    try:
        level = providers.codex_trust(repo_root)
    except ValueError as e:
        return [Item("setup", "codex.config", "codex", "Fix Codex's config.toml",
                     f"{providers.codex_config_path()} doesn't parse, so Codex won't start: {e}")]
    if level in (None, "trusted"):
        return []           # None: recorded as trusted at launch
    return [Item("trust", "codex.trust", "codex", f"Trust {os.path.basename(repo_root.rstrip('/'))} in Codex?",
                 f"Codex is set to {level!r} for {repo_root}, so its agents can't edit or run anything "
                 "there without asking each time. Trust it to let brindle's agents work.",
                 Action("trust", "POST", "/v1/providers/codex/trust", {"repo": repo_root}))]


def _native(repo_root: str | None, profile=None) -> list[Item]:
    from brindle.profiles import list_profiles

    out: dict[str, Item] = {}
    for p in [profile] if profile is not None else list_profiles(repo_root):
        if p.provider != "native":
            continue
        if not p.base_url or not p.model:
            out[p.name] = Item("setup", f"native.{p.name}", "native", f"Finish profile {p.name}",
                               f"profile {p.name!r} uses the native provider but gives no "
                               f"{'base_url' if not p.base_url else 'model'}")
        elif p.api_key_env and not _key_value(p.api_key_env, p.env if p.api_key_env in p.env else None):
            out.setdefault(p.api_key_env, Item("key", f"key.{p.api_key_env}", "native",
                                               f"Add {p.api_key_env}",
                                               f"profile {p.name!r} needs {p.api_key_env}",
                                               _set_key(p.api_key_env)))
    return list(out.values())


# -- recording what the person accepted in the app --------------------------------


def _record_api_key(env: dict[str, str] | None, key: str | None) -> None:
    """Approve the key the launch's identity (``env``) gets, only if it's the
    one the person was shown (``key``, its fingerprint)."""
    from brindle import providers

    value = _key_value("ANTHROPIC_API_KEY", env)
    if not value:
        raise StaleConsent("no ANTHROPIC_API_KEY to approve any more; check again")
    if key is None or key_fingerprint(value) != key:
        raise StaleConsent("ANTHROPIC_API_KEY isn't the key you were asked about any more; check again")
    providers.approve_api_key(value)


def _record_onboarding(env: dict[str, str] | None, key: str | None) -> None:
    from brindle import providers

    providers.record_claude_onboarding()


# Consents a CLI keeps a record of, by item id. Each runs only on the
# person's confirmation in the app (POST /v1/providers/{p}/consent).
RECORDERS = {"claude.onboarding": _record_onboarding, "claude.api_key": _record_api_key}


def record_consent(provider: str, item_id: str, env: dict[str, str] | None = None, key: str | None = None) -> None:
    """Record consent ``item_id``. ``env``: the identity of the launch it was
    asked for (as the item's action names it); ``key``: what it was bound to."""
    if not item_id.startswith(f"{provider}.") or item_id not in RECORDERS:
        raise ValueError(f"{item_id!r} isn't a consent brindle can record for {provider}")
    RECORDERS[item_id](env, key)


def trust(provider: str, repo_root: str) -> bool:
    """The person chose in the app to trust ``repo_root`` in ``provider``."""
    from brindle import providers

    if provider == "codex":
        return providers.trust_codex_project(repo_root, override=True)
    if provider == "claude":
        return providers.trust_folder(repo_root)
    raise ValueError(f"{provider} has no recorded folder trust; brindle answers its screen at launch")
