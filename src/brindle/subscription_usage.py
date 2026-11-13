"""Claude subscription identity from its public config, never its token store."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

STATUS_IDENTITY = "BRINDLE_CLAUDE_USAGE_IDENTITY"


def identity() -> str | None:
    from brindle import providers

    # An explicit token can belong to someone other than the config's account.
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return None
    try:
        data = json.loads(Path(providers.claude_global_config()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(account, dict):
        return None
    parts = [account.get("accountUuid"), account.get("organizationUuid")]
    if not all(isinstance(part, str) and part.strip() for part in parts):
        return None
    return json.dumps(parts)


def cache_key(provider: str, *, who: str | None = None) -> str | None:
    if provider != "claude":
        return provider
    who = who or identity()
    return "claude:" + hashlib.sha256(who.encode()).hexdigest() if who else None
