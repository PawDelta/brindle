"""A profile's ``auth`` choice, and the variables a pane must start without.

``auto`` (the default): a key from the environment or `brindle keys` wins, else
the CLI's own login. ``subscription``: no stored key goes into the pane and the
provider's exported keys are taken out, so the CLI's login is used. ``api_key``:
the agent doesn't start without a key. An org's ``deny_personal_keys`` wins
over all three (see :mod:`brindle.pro.managed_models`).
"""

from __future__ import annotations

import os
from typing import Iterable

from brindle import keystore

# The API keys each provider's CLI reads from the environment. A subscription
# token (CLAUDE_CODE_OAUTH_TOKEN) is a login, not a key: ``subscription`` keeps it.
API_KEYS = {"claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
            "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
            "antigravity": ("GEMINI_API_KEY",)}
# What Claude Code's managed settings (apiKeyHelper, a gateway) replace: a
# personally exported credential must not bypass them.
CLAUDE_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


def deny_names(provider: str, auth: str, m, org_managed: bool = False) -> tuple[str, ...]:
    """The variables a ``provider`` pane starts without whatever else says:
    the org's denied personal keys, Claude's credentials under Claude Code
    managed settings, the provider's API keys for ``auth: subscription``, and
    for Claude under an enforced company identity whatever else picks an account."""
    deny = list(m.denied_keys) if m is not None else []
    extra = (CLAUDE_CREDENTIALS if provider == "claude" and org_managed else ()) + (
        API_KEYS.get(provider, ()) if auth == "subscription" else ())
    if provider == "claude" and not org_managed:
        # An enforced company identity: nothing but the checked route picks the account.
        from brindle import company_identity

        route = company_identity.enforced_route_env()
        if route is not None:
            extra += tuple(sorted(company_identity.ROUTE_VARS - set(route)))
    return tuple(deny + [n for n in dict.fromkeys(extra) if n not in deny])


def key_problem(profile, provider: str, deny: Iterable[str], org_key: bool,
                exec_mode: bool = False) -> str | None:
    """Why ``profile`` (``auth: api_key``) can't start for lack of a key, or
    None. A key counts when the org supplies one, or one of the provider's key
    variables isn't denied and is set (the profile's env, the environment) or
    stored with `brindle keys`. ``exec_mode``: the profile runs headless
    `codex exec` rather than the interactive Codex brindle uses in panes."""
    if profile.auth != "api_key" or org_key:
        return None
    names = ((profile.api_key_env,) if profile.api_key_env else ()) if provider == "native" \
        else API_KEYS.get(provider, ())
    if not names:
        return None
    denied = set(deny)
    usable = [n for n in names if n not in denied]
    if provider == "codex" and usable:
        from brindle import providers

        if providers.codex_chatgpt_login():
            if not exec_mode:
                # Interactive Codex ignores both env keys under a ChatGPT login.
                return f"profile {profile.name!r} has auth: api_key, but " + providers.CODEX_INTERACTIVE_IGNORES_KEYS
            # `codex exec` takes CODEX_API_KEY and ignores OPENAI_API_KEY.
            had_openai = bool(profile.env.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
                              or keystore.pane_keys(["OPENAI_API_KEY"]))
            usable = [n for n in usable if n != "OPENAI_API_KEY"]
            have_codex = "CODEX_API_KEY" in usable and (
                profile.env.get("CODEX_API_KEY") or os.environ.get("CODEX_API_KEY")
                or keystore.pane_keys(["CODEX_API_KEY"]))
            if had_openai and not have_codex:
                return f"profile {profile.name!r} has auth: api_key, but " + providers.CODEX_EXEC_IGNORES_OPENAI_KEY
    if any(profile.env.get(n) or os.environ.get(n) for n in usable) or keystore.pane_keys(usable):
        if provider == "antigravity":
            from brindle import providers

            if not providers.agy_uses_gemini_key():
                return f"profile {profile.name!r} has auth: api_key, but " + providers.agy_ignores_key_message()
        return None
    if not usable:
        return (f"profile {profile.name!r} has auth: api_key, but your org doesn't allow personal API "
                f"keys ({', '.join(names)}); use a profile with auth: auto or subscription")
    return (f"profile {profile.name!r} has auth: api_key, but no API key is available: export "
            f"{usable[0]} or run `brindle keys set {usable[0]}`")
