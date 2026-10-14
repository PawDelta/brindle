"""Cloud login state, read without calling the model.

``status(provider)`` says whether the Bedrock, Vertex or Foundry login is still
good, when it lapses, and the one command that renews it. It reads the CLIs'
own credential files (AWS SSO cache, Azure CLI token cache) and, for Google,
whose application-default credentials hold only a refresh token, asks gcloud
for an access token. Token values are never logged or returned.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from brindle import model_access

GCLOUD_TIMEOUT = 15.0
GOOGLE_TOKEN_LIFETIME = 3600.0   # gcloud prints no expiry; Google access tokens last an hour

_FLAGS = {"bedrock": "CLAUDE_CODE_USE_BEDROCK", "vertex": "CLAUDE_CODE_USE_VERTEX",
          "foundry": "CLAUDE_CODE_USE_FOUNDRY"}


@dataclass(frozen=True)
class LoginStatus:
    ok: bool
    expires_at: float | None
    refresh_cmd: str
    note: str


def provider_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """The cloud provider whose ``CLAUDE_CODE_USE_*`` flag is on, or None."""
    env = os.environ if env is None else env
    for provider, flag in _FLAGS.items():
        if str(env.get(flag, "")).strip().lower() in ("1", "true", "yes", "on"):
            return provider
    return None


def _epoch(value) -> float | None:
    """A timestamp as epoch seconds: a number, a numeric string or ISO 8601."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _json_files(paths) -> list:
    out = []
    for p in paths:
        try:
            out.append(json.loads(Path(p).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _from_expiry(expires_at: float | None, refresh_cmd: str, now: float, missing: str) -> LoginStatus:
    if expires_at is None:
        return LoginStatus(False, None, refresh_cmd, missing)
    if expires_at <= now:
        return LoginStatus(False, expires_at, refresh_cmd, "login expired")
    return LoginStatus(True, expires_at, refresh_cmd, "logged in")


def _aws(env: Mapping[str, str], home: Path, now: float) -> LoginStatus:
    profile = env.get("AWS_PROFILE") or "default"
    refresh = f"aws sso login --profile {profile}"
    cache = home / ".aws" / "sso" / "cache"
    expiries = [_epoch(d.get("expiresAt")) for d in _json_files(sorted(cache.glob("*.json")))
                if isinstance(d, dict) and d.get("accessToken")]
    expiries = [e for e in expiries if e is not None]
    return _from_expiry(max(expiries, default=None), refresh, now, "no AWS SSO login found")


def _azure(home: Path, now: float) -> LoginStatus:
    path = home / ".azure" / "accessTokens.json"
    tokens = _json_files([path])
    expiries = [_epoch(t.get("expires_on", t.get("expiresOn")))
                for t in (tokens[0] if tokens and isinstance(tokens[0], list) else [])
                if isinstance(t, dict)]
    expiries = [e for e in expiries if e is not None]
    return _from_expiry(max(expiries, default=None), "az login", now, "no Azure CLI login found")


def _google(run, now: float) -> LoginStatus:
    refresh = "gcloud auth application-default login"
    try:
        proc = run(["gcloud", "auth", "application-default", "print-access-token"],
                   capture_output=True, text=True, timeout=GCLOUD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return LoginStatus(False, None, refresh, "gcloud timed out refreshing the access token")
    except OSError:
        return LoginStatus(False, None, refresh, "gcloud is not installed")
    if proc.returncode == 0 and (proc.stdout or "").strip():
        return LoginStatus(True, now + GOOGLE_TOKEN_LIFETIME, refresh, "logged in")
    if model_access.expired_login(f"{proc.stdout or ''}\n{proc.stderr or ''}") is not None:
        return LoginStatus(False, None, refresh, "login expired (invalid_grant)")
    return LoginStatus(False, None, refresh, "gcloud could not produce an access token")


def status(provider: str | None = None, *, env: Mapping[str, str] | None = None,
           home: str | Path | None = None, now: float | None = None,
           run=subprocess.run) -> LoginStatus:
    """The login state for ``provider`` (bedrock, vertex or foundry; by default
    the one the environment's ``CLAUDE_CODE_USE_*`` flag selects)."""
    env = os.environ if env is None else env
    provider = provider or provider_from_env(env)
    home = Path(home) if home is not None else Path.home()
    now = time.time() if now is None else now
    if provider == "bedrock":
        return _aws(env, home, now)
    if provider == "vertex":
        return _google(run, now)
    if provider == "foundry":
        return _azure(home, now)
    return LoginStatus(True, None, "", "no cloud provider selected")
