"""What a model's tokens cost, in US dollars per million tokens (MTok).

``PRICES`` holds the providers' published list prices as of ``AS_OF``, copied
from their official pricing pages:

- Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
- OpenAI: https://developers.openai.com/api/docs/pricing (Standard tier)
- Gemini: https://ai.google.dev/gemini-api/docs/pricing (Standard tier)

Each price has four rates: uncached input, output, cache write and cache
read. Anthropic's cache write is the 5-minute rate (1.25x input; brindle's
transcripts don't say which TTL a write used). OpenAI lists a cache-write
price (1.25x input) for its gpt-6 and gpt-5.6 models; for the older ones it
lists none, so there the cache write rate is the input rate. OpenAI's prices
are its short-context (<= 272K input tokens) ones: gpt-6's long-context
rates are higher, and brindle doesn't see each request's length.

A model brindle has no price for costs "unknown", never a guess. A profile
that runs on this machine (a native profile whose ``base_url`` is localhost)
costs $0. The ``pricing`` key in ``~/.brindle/config.json`` or a repo's
``.brindle/config.json`` / ``config.local.json`` adds or overrides prices::

    "pricing": {"my-model": {"input": 1, "output": 5, "cache_write": 1.25, "cache_read": 0.1}}

``input`` and ``output`` are required; a missing ``cache_write`` or
``cache_read`` is charged at the input rate. Prices are list prices: a
subscription, batch or negotiated discount makes the real bill lower, and
data residency or fast mode makes it higher.
"""

from __future__ import annotations

import datetime as _dt
from brindle.model_catalog import ALIASES, CATALOG, FREE, MTOK, Price, normalize

AS_OF = _dt.date(2026, 10, 6)  # oldest retained rates; new entries carry sources in model_catalog
STALE_DAYS = 90
FREE_LABEL = "Free"

# Compatibility views for callers that inspect the tables. Model facts live in
# model_catalog; adding a row there supplies both the regular and launch rates.
PRICES: dict[str, Price] = {key: model.price for key, model in CATALOG.items()}
PRICES.update({alias: PRICES[key] for alias, key in ALIASES.items()})
PROMOS: dict[str, tuple[_dt.date, Price]] = {
    key: model.promo for key, model in CATALOG.items() if model.promo is not None
}


def _parse_override(value: object) -> Price | None:
    if not isinstance(value, dict):
        return None
    try:
        inp, out = float(value["input"]), float(value["output"])
        write = float(value.get("cache_write", inp))
        read = float(value.get("cache_read", inp))
    except (KeyError, TypeError, ValueError):
        return None
    if min(inp, out, write, read) < 0:
        return None
    return Price(inp, out, write, read)


def overrides(raw: object) -> dict[str, Price]:
    """The valid entries of a ``pricing`` config value, keyed like ``PRICES``.
    An entry without numeric ``input`` and ``output`` is ignored."""
    if not isinstance(raw, dict):
        return {}
    found = {}
    for model, value in raw.items():
        price = _parse_override(value)
        if isinstance(model, str) and price is not None:
            found[normalize(model)] = price
    return found


def repo_overrides(repo_root: str | None) -> dict[str, Price]:
    """The ``pricing`` overrides in force for ``repo_root`` (or the user's
    own, without a repo). An unreadable config means none."""
    from brindle import config

    try:
        if repo_root:
            return overrides(config.load_repo_config(repo_root).pricing)
        return overrides(config.user_settings().get("pricing"))
    except Exception:  # noqa: BLE001 - a broken config: list prices only
        return {}


def price_for(model: str | None, extra: dict[str, Price] | None = None) -> Price | None:
    """``model``'s price (``extra`` first, then a launch rate still running
    today, then ``PRICES``), or None when brindle doesn't know it."""
    if not model:
        return None
    key = normalize(model)
    if extra and key in extra:
        return extra[key]
    promo = PROMOS.get(key)
    if promo and _dt.date.today() < promo[0]:   # checked per lookup: a long-lived daemon crosses the end
        return promo[1]
    return PRICES.get(key)


def is_local(base_url: str | None) -> bool:
    return bool(base_url) and any(h in base_url for h in ("localhost", "127.0.0.1", "[::1]"))


def profile_price(name: str | None, repo_root: str | None,
                  extra: dict[str, Price] | None = None) -> Price | None:
    """The price of the model profile ``name`` runs: $0 for a local model,
    None when the profile is unknown or names no model brindle has a price
    for (e.g. a Claude profile without ``model``, which runs whatever the
    account's default is)."""
    from brindle.profiles import load_profile

    if not name:
        return None
    try:
        p = load_profile(name, repo_root)
    except Exception:  # noqa: BLE001 - a profile since removed
        return None
    if is_local(p.base_url):
        return FREE
    return price_for(p.model, extra if extra is not None else repo_overrides(repo_root))


def stale_warning(today: _dt.date | None = None) -> str | None:
    """A warning when the built-in prices are more than ``STALE_DAYS`` old."""
    today = today or _dt.date.today()
    age = (today - AS_OF).days
    if age <= STALE_DAYS:
        return None
    return (f"brindle's built-in prices are from {AS_OF.isoformat()} ({age} days ago) and may be "
            "out of date: update brindle, or set current ones under \"pricing\" in "
            "~/.brindle/config.json.")


def money(dollars: float) -> str:
    if dollars and abs(dollars) < 0.01:
        return "<$0.01"
    return f"${dollars:,.2f}"


__all__ = ["ALIASES", "AS_OF", "FREE", "FREE_LABEL", "PRICES", "Price", "STALE_DAYS", "is_local", "money",
           "normalize", "overrides", "price_for", "profile_price", "repo_overrides",
           "stale_warning"]
