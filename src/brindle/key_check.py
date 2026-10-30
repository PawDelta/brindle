"""Check a pasted API key with its provider before `brindle keys set` stores it.
Every check is a read-only GET that generates nothing and costs nothing."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

# name -> (where to create a key, how the provider is called)
KEY_PAGES = {
    "ANTHROPIC_API_KEY": "https://console.anthropic.com/settings/keys",
    "OPENAI_API_KEY": "https://platform.openai.com/api-keys",
    "OPENROUTER_API_KEY": "https://openrouter.ai/keys",
    "GEMINI_API_KEY": "https://aistudio.google.com/apikey",
    "GOOGLE_API_KEY": "https://aistudio.google.com/apikey",
}


def key_page(name: str) -> str | None:
    return KEY_PAGES.get(name)


def personal_keys_denied(repo_root: str | None) -> bool:
    """True when the org's policy sets deny_personal_keys (read as agents.py does)."""
    from brindle import agents

    m = agents.managed(repo_root or ".")
    return bool(m is not None and m.deny_personal_keys)


def _get(url: str, headers: dict[str, str]) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https URLs
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


VERIFIED, REJECTED, UNCHECKED = "verified", "rejected", "unchecked"


def evaluate(name: str, value: str) -> tuple[str, str | None]:
    """(status, problem): VERIFIED when the provider accepts the key, REJECTED
    when it refuses it (or it has no credit), else UNCHECKED, with a message
    when the provider couldn't be asked. Names without a check are UNCHECKED."""
    gemini = name in ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    if name == "OPENROUTER_API_KEY":
        url, headers = "https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {value}"}
    elif name == "ANTHROPIC_API_KEY":
        url = "https://api.anthropic.com/v1/models"
        headers = {"x-api-key": value, "anthropic-version": "2023-06-01"}
    elif name == "OPENAI_API_KEY":
        url, headers = "https://api.openai.com/v1/models", {"Authorization": f"Bearer {value}"}
    elif gemini:
        url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"
        headers = {"x-goog-api-key": value}
    else:
        return UNCHECKED, None
    try:
        status, body = _get(url, headers)
    except (OSError, ValueError) as e:
        # Never the exception's text: http.client quotes a header it refuses, the key included.
        return UNCHECKED, f"couldn't reach the provider to check the key ({type(e).__name__})"
    # Google answers a malformed or unknown key with 400 ("API key not valid"), not 401.
    if status == 401 or (gemini and status in (400, 403)):
        return REJECTED, f"the provider rejected this key ({status} unauthorized); check that you copied all of it"
    if status != 200:
        return UNCHECKED, f"the provider answered {status} when checking the key"
    if name == "OPENROUTER_API_KEY":
        data = (body.get("data") if isinstance(body, dict) else None) or {}
        if not isinstance(data, dict):
            data = {}
        left = data.get("limit_remaining")
        if left is not None and left <= 0:
            return REJECTED, "this OpenRouter key has no credit left"
    return VERIFIED, None


def verify(name: str, value: str) -> str | None:
    """None when the provider accepts the key, else a message saying why not.
    Names without a check are accepted unchecked."""
    return evaluate(name, value)[1]
