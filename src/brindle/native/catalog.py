"""The Models.dev catalog (https://models.dev/api.json), for native profiles
that name their model as ``model: <provider>/<model>``.

The catalog is cached in ``~/.brindle/models-catalog.json`` and refetched
once it is older than ``TTL``. It is never fetched in air-gap mode or when
the network is down: the cache is used (however old), and without one the
error says so. :func:`resolve` fills the endpoint fields a profile didn't
set itself from the entry.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from brindle import airgap
from brindle.config import brindle_home

URL = "https://models.dev/api.json"
TTL = 24 * 3600
CACHE_NAME = "models-catalog.json"
# Providers the catalog lists without an ``api`` URL: their own SDK knows it.
DEFAULT_BASE_URLS = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}


class CatalogError(Exception):
    """The catalog is unavailable, or has no such provider or model."""


@dataclass(frozen=True)
class Resolved:
    model: str                      # the provider's own id for it
    base_url: str
    api: str                        # openai | anthropic
    api_key_env: str | None
    context_tokens: int | None


def cache_path() -> Path:
    return brindle_home() / CACHE_NAME


def _read_cache() -> dict | None:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _download(timeout: float = 15.0) -> dict:
    req = urllib.request.Request(URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not a provider table")
    return data


def load() -> dict:
    """The catalog: the fresh cache, else a download (written to the cache),
    else the stale cache. Never downloads in air-gap mode."""
    path = cache_path()
    cached = _read_cache()
    if cached is not None:
        try:
            fresh = time.time() - path.stat().st_mtime < TTL
        except OSError:
            fresh = False
        if fresh or airgap.enabled():
            return cached
    if airgap.enabled():
        raise CatalogError(f"air-gap mode ({airgap.source()}): the Models.dev catalog isn't fetched and "
                           f"there is no cached copy at {path}; copy models.dev/api.json there")
    try:
        data = _download()
    except (OSError, ValueError) as e:   # URLError and timeouts are OSErrors
        if cached is not None:
            return cached
        raise CatalogError(f"couldn't fetch the Models.dev catalog ({getattr(e, 'reason', e)}) and "
                           f"there is no cached copy at {path}") from e
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return data


def resolve(spec: str, catalog: dict | None = None) -> Resolved:
    """The endpoint for ``spec`` (``<provider>/<model>``, split at the first
    slash). Raises CatalogError when the catalog can't be had or doesn't list it."""
    provider_id, _, model_id = spec.partition("/")
    if not provider_id or not model_id:
        raise CatalogError(f"model {spec!r} isn't of the form <provider>/<model>")
    catalog = load() if catalog is None else catalog
    provider = catalog.get(provider_id)
    if not isinstance(provider, dict):
        raise CatalogError(f"Models.dev lists no provider {provider_id!r} (model {spec!r})")
    models = provider.get("models") or {}
    entry = models.get(model_id)
    if not isinstance(entry, dict):
        raise CatalogError(f"Models.dev lists no model {model_id!r} for provider {provider_id!r}")
    base_url = provider.get("api") or DEFAULT_BASE_URLS.get(provider_id)
    if not base_url:
        raise CatalogError(f"Models.dev gives no API URL for provider {provider_id!r}; set base_url")
    api = "anthropic" if "anthropic" in str(provider.get("npm") or "") else "openai"
    env = provider.get("env") or []
    limit = entry.get("limit") or {}
    context = limit.get("context")
    tokens = None
    if isinstance(context, int) and context > 0:
        output = limit.get("output") if isinstance(limit.get("output"), int) else 0
        tokens = max(context - output, context // 2)      # leave room for the reply
    return Resolved(model_id, base_url, api, env[0] if env else None, tokens)
