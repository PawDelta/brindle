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
- ``setup``: something to install or configure outside brindle (no route)."""

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
    kind: str           # signin | key | trust | consent | setup
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


def _consent(provider: str, item_id: str) -> Action:
    if item_id not in RECORDERS:
        return Action("open_provider_once", "POST", f"/v1/providers/{provider}/login")
    return Action("consent", "POST", f"/v1/providers/{provider}/consent", {"id": item_id})


def _has_key(name: str, env: dict[str, str] | None = None) -> bool:
    from brindle import keystore

    if os.environ.get(name) or (env or {}).get(name):
        return True
    try:
        return bool(keystore.get_key(name))
    except keystore.CredentialError:
        return False


def _binary(provider: str) -> str:
    from brindle import antigravity, opencode, providers

    return {"claude": providers.claude_binary, "codex": providers.codex_binary,
            "antigravity": antigravity.binary, "opencode": opencode.binary}[provider]()


def check(provider: str, repo_root: str | None = None) -> list[Item]:
    """What would stop a session on ``provider`` in ``repo_root`` before its
    first turn; empty when nothing would."""
    from brindle import doctor, providers

    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
    if provider == "native":
        return _native(repo_root)
    items = []
    if not shutil.which("tmux"):
        items.append(Item("setup", "tmux", provider, "Install tmux",
                          "brindle runs every agent in a tmux window. Install: brew install tmux "
                          "(Debian/Ubuntu: sudo apt install tmux)"))
    exe = _binary(provider)
    if not (providers.which_cli(exe) or os.path.isfile(exe)):
        name, how = doctor.CLI_INSTALL[provider]
        return items + [Item("setup", f"{provider}.install", provider, f"Install {name}",
                             f"{name} (`{exe}`) isn't on PATH. Install: {how}")]
    if provider == "antigravity" and providers.agy_uses_gemini_key() and not _has_key("GEMINI_API_KEY"):
        items.append(Item("key", "key.GEMINI_API_KEY", provider, "Add your Gemini API key",
                          "Antigravity is set to the Gemini API, which needs GEMINI_API_KEY",
                          _set_key("GEMINI_API_KEY")))
    else:
        why = providers.signed_out(provider)
        if why:
            items.append(Item("signin", f"{provider}.signin", provider,
                              f"Sign in to {doctor.CLI_INSTALL[provider][0]}", why, _login(provider)))
    if provider == "claude":
        items += _claude_consents()
    if provider == "codex" and repo_root:
        items += _codex_trust(repo_root)
    return items


def _claude_consents() -> list[Item]:
    from brindle import keystore, providers

    out = []
    if not providers.claude_onboarded():
        out.append(Item(
            "consent", "claude.onboarding", "claude", "Claude Code's security notes",
            "Before its first session Claude Code shows its security notes: Claude can make mistakes, "
            "so review what it does, and code or content it reads can try to steer it (prompt "
            "injection). Read them, then confirm to continue; brindle records it as Claude Code does.",
            _consent("claude", "claude.onboarding"), CLAUDE_SECURITY_URL))
    cred = providers.claude_credential()
    if cred.kind == "api key":
        try:
            value = os.environ.get("ANTHROPIC_API_KEY") or keystore.get_key("ANTHROPIC_API_KEY")
        except keystore.CredentialError:
            value = None
        if value and not providers.api_key_approved(value.strip()):
            out.append(Item(
                "consent", "claude.api_key", "claude", "Use ANTHROPIC_API_KEY in Claude Code?",
                f"Claude Code asks once whether to use the key ending {value.strip()[-4:]} "
                f"({cred.source}); with it, usage is billed to that key, not a subscription login.",
                _consent("claude", "claude.api_key")))
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


def _native(repo_root: str | None) -> list[Item]:
    from brindle.profiles import list_profiles

    out: dict[str, Item] = {}
    for p in list_profiles(repo_root):
        if p.provider != "native":
            continue
        if not p.base_url or not p.model:
            out[p.name] = Item("setup", f"native.{p.name}", "native", f"Finish profile {p.name}",
                               f"profile {p.name!r} uses the native provider but gives no "
                               f"{'base_url' if not p.base_url else 'model'}")
        elif p.api_key_env and not _has_key(p.api_key_env, p.env):
            out.setdefault(p.api_key_env, Item("key", f"key.{p.api_key_env}", "native",
                                               f"Add {p.api_key_env}",
                                               f"profile {p.name!r} needs {p.api_key_env}",
                                               _set_key(p.api_key_env)))
    return list(out.values())


def gate(provider_names, repo_root: str | None) -> Blocked | None:
    """The items blocking any of ``provider_names`` (each once), or None."""
    items: list[Item] = []
    for name in dict.fromkeys(provider_names):
        if name in PROVIDERS:
            items += check(name, repo_root)
    return Blocked(items) if items else None


# -- recording what the person accepted in the app --------------------------------


def _record_api_key() -> None:
    from brindle import keystore, providers

    value = os.environ.get("ANTHROPIC_API_KEY") or keystore.get_key("ANTHROPIC_API_KEY")
    if not value:
        raise ValueError("no ANTHROPIC_API_KEY to approve")
    providers.approve_api_key(value.strip())


def _record_onboarding() -> None:
    from brindle import providers

    providers.record_claude_onboarding()


# Consents a CLI keeps a record of, by item id. Each runs only on the
# person's confirmation in the app (POST /v1/providers/{p}/consent).
RECORDERS = {"claude.onboarding": _record_onboarding, "claude.api_key": _record_api_key}


def record_consent(provider: str, item_id: str) -> None:
    if not item_id.startswith(f"{provider}.") or item_id not in RECORDERS:
        raise ValueError(f"{item_id!r} isn't a consent brindle can record for {provider}")
    RECORDERS[item_id]()


def trust(provider: str, repo_root: str) -> bool:
    """The person chose in the app to trust ``repo_root`` in ``provider``."""
    from brindle import providers

    if provider == "codex":
        return providers.trust_codex_project(repo_root, override=True)
    if provider == "claude":
        return providers.trust_folder(repo_root)
    raise ValueError(f"{provider} has no recorded folder trust; brindle answers its screen at launch")
