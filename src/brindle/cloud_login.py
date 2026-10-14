"""Cloud login state, read without calling the model.

``status(provider)`` says whether the Bedrock, Vertex or Foundry login is still
good, when it lapses, and the one command that renews it. It reads the CLIs'
own credential files (AWS SSO cache, Azure CLI token cache) and, for Google,
whose application-default credentials hold only a refresh token, asks gcloud
for an access token. Token values are never logged or returned.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from brindle import model_access

CLI_TIMEOUT = 15.0

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
        dt = datetime.fromisoformat(value.strip().replace("UTC", "Z").replace("Z", "+00:00"))
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


def _aws_token_key(profile: str, config_path: Path) -> str | None:
    """What the SSO cache file name is the SHA-1 of for ``profile``: its
    sso_session name, or (legacy config) its sso_start_url; None when the
    profile doesn't use SSO."""
    cp = configparser.RawConfigParser()
    try:
        cp.read(config_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    section = "default" if profile == "default" else f"profile {profile}"
    if not cp.has_section(section):
        return None
    return cp.get(section, "sso_session", fallback=None) or cp.get(
        section, "sso_start_url", fallback=None)


def _aws(env: Mapping[str, str], home: Path, now: float) -> LoginStatus:
    profile = env.get("AWS_PROFILE") or "default"
    refresh = f"aws sso login --profile {profile}"
    config = Path(env.get("AWS_CONFIG_FILE") or home / ".aws" / "config")
    key = _aws_token_key(profile, config)
    if key is None:
        return LoginStatus(False, None, refresh, f"AWS profile {profile} is not an SSO profile")
    token = home / ".aws" / "sso" / "cache" / (hashlib.sha1(key.strip().encode()).hexdigest() + ".json")
    data = (_json_files([token]) or [None])[0]
    expires = _epoch(data.get("expiresAt")) if isinstance(data, dict) and data.get("accessToken") else None
    return _from_expiry(expires, refresh, now, "no AWS SSO login found for this profile")


def _full_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    """The environment for a CLI call: the supplied one over the ambient one,
    or None (inherit) when the caller supplied none."""
    return None if env is None else {**os.environ, **env}


def _azure_expiry(token: dict) -> float | None:
    exp = _epoch(token.get("expires_on"))
    if exp is not None:
        return exp
    try:   # "expiresOn" is a naive local time
        return datetime.fromisoformat(str(token.get("expiresOn")).strip()).timestamp()
    except ValueError:
        return None


def _azure(run, env, now: float) -> LoginStatus:
    refresh = "az login"
    try:
        proc = run(["az", "account", "get-access-token", "--output", "json"],
                   capture_output=True, text=True, timeout=CLI_TIMEOUT, env=_full_env(env))
    except subprocess.TimeoutExpired:
        return LoginStatus(False, None, refresh, "az timed out fetching an access token")
    except OSError:
        return LoginStatus(False, None, refresh, "az is not installed")
    if proc.returncode == 0:
        try:
            token = json.loads(proc.stdout or "")
        except ValueError:
            token = None
        exp = _azure_expiry(token) if isinstance(token, dict) else None
        if exp is not None:
            return _from_expiry(exp, refresh, now, "")
    if model_access.expired_login(f"{proc.stdout or ''}\n{proc.stderr or ''}") is not None:
        return LoginStatus(False, None, refresh, "login expired")
    return LoginStatus(False, None, refresh, "no Azure CLI login found")


def tokeninfo_expiry(token: str, timeout: float = CLI_TIMEOUT) -> float | None:
    """Seconds the Google access ``token`` has left, from Google's tokeninfo
    endpoint (the token goes in the POST body, never a URL), or None."""
    req = urllib.request.Request("https://oauth2.googleapis.com/tokeninfo",
                                 data=urllib.parse.urlencode({"access_token": token}).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - fixed https URL
            return float(json.load(r)["expires_in"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _google(run, env, now: float, expiry) -> LoginStatus:
    refresh = "gcloud auth application-default login"
    try:
        proc = run(["gcloud", "auth", "application-default", "print-access-token"],
                   capture_output=True, text=True, timeout=CLI_TIMEOUT, env=_full_env(env))
    except subprocess.TimeoutExpired:
        return LoginStatus(False, None, refresh, "gcloud timed out refreshing the access token")
    except OSError:
        return LoginStatus(False, None, refresh, "gcloud is not installed")
    token = (proc.stdout or "").strip()
    if proc.returncode == 0 and token:
        left = expiry(token)
        if left is None:
            return LoginStatus(True, None, refresh, "logged in; token expiry unknown")
        return LoginStatus(True, now + left, refresh, "logged in")
    if model_access.expired_login(f"{proc.stdout or ''}\n{proc.stderr or ''}") is not None:
        return LoginStatus(False, None, refresh, "login expired (invalid_grant)")
    return LoginStatus(False, None, refresh, "gcloud could not produce an access token")


def status(provider: str | None = None, *, env: Mapping[str, str] | None = None,
           home: str | Path | None = None, now: float | None = None,
           run=subprocess.run, expiry=tokeninfo_expiry) -> LoginStatus:
    """The login state for ``provider`` (bedrock, vertex or foundry; by default
    the one the environment's ``CLAUDE_CODE_USE_*`` flag selects)."""
    supplied = env
    env = os.environ if env is None else {**os.environ, **env}
    provider = provider or provider_from_env(env)
    home = Path(home) if home is not None else Path.home()
    now = time.time() if now is None else now
    if provider == "bedrock":
        return _aws(env, home, now)
    if provider == "vertex":
        return _google(run, supplied, now, expiry)
    if provider == "foundry":
        return _azure(run, supplied, now)
    return LoginStatus(True, None, "", "no cloud provider selected")
