"""The Models.dev catalog (https://models.dev/api.json), for native profiles
that name their model as ``model: <provider>/<model>``.

The catalog is cached in ``~/.brindle/models-catalog.json`` and refetched
once it is older than ``TTL``, in the background: loading a profile never
touches the network. Air-gap mode never fetches. The cache is used however
old, and without one the error says so. :func:`resolve` fills the endpoint fields a profile didn't
set itself from the entry.
"""

from __future__ import annotations

import json
import threading
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


def _fresh() -> bool:
    try:
        return time.time() - cache_path().stat().st_mtime < TTL
    except OSError:
        return False


def load() -> dict:
    """The cached catalog, however old. Never touches the network (profiles
    load often); :func:`refresh_async` and :func:`refresh` keep the cache up."""
    cached = _read_cache()
    if cached is None:
        if airgap.enabled():
            raise CatalogError(f"air-gap mode ({airgap.source()}): the Models.dev catalog isn't fetched and "
                               f"there is no cached copy; copy models.dev/api.json to {cache_path()}")
        raise CatalogError(f"the Models.dev catalog isn't cached yet ({cache_path()}); it is being "
                           "fetched in the background, so try again in a moment")
    return cached


def refresh() -> bool:
    """Download the catalog into the cache unless it is fresh or air-gap mode
    is on. Whether the cache was updated; a failure leaves the old cache."""
    if airgap.enabled() or _fresh():
        return False
    try:
        data = _download()
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except (OSError, ValueError):   # URLError and timeouts are OSErrors
        return False
    return True


def refresh_async() -> None:
    """:func:`refresh` on a background thread, when the cache is missing or stale."""
    if airgap.enabled() or _fresh():
        return
    threading.Thread(target=refresh, name="models-catalog", daemon=True).start()


def resolve(spec: str, catalog: dict | None = None) -> Resolved:
    """The endpoint for ``spec`` (``<provider>/<model>``, split at the first
    slash). Raises CatalogError when the catalog can't be had or doesn't list it."""
    provider_id, _, model_id = spec.partition("/")
    if not provider_id or not model_id:
        raise CatalogError(f"model {spec!r} isn't of the form <provider>/<model>")
    catalog = load() if catalog is None else catalog
    try:
        return _resolve(spec, provider_id, model_id, catalog)
    except (AttributeError, TypeError, KeyError, IndexError) as e:
        raise CatalogError(f"Models.dev entry for {spec!r} is malformed ({e!r})") from e


def _resolve(spec: str, provider_id: str, model_id: str, catalog: dict) -> Resolved:
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
    if not isinstance(env, list):
        raise TypeError("env is not a list")
    limit = entry.get("limit") or {}
    context = limit.get("context")
    tokens = None
    if isinstance(context, int) and context > 0:
        output = limit.get("output") if isinstance(limit.get("output"), int) else 0
        tokens = max(context - output, context // 2)      # leave room for the reply
    return Resolved(model_id, base_url, api, env[0] if env else None, tokens)
