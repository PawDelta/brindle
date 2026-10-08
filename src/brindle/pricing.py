"""What a model's tokens cost, in US dollars per million tokens (MTok).

``PRICES`` holds the providers' published list prices as of ``AS_OF``, copied
from their official pricing pages:

- Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
- OpenAI: https://developers.openai.com/api/docs/pricing (Standard tier)

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
import re
from dataclasses import dataclass

AS_OF = _dt.date(2026, 10, 6)
STALE_DAYS = 90
MTOK = 1_000_000


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""
    input: float
    output: float
    cache_write: float
    cache_read: float

    def cost(self, input_tokens: int = 0, output_tokens: int = 0, cache_write_tokens: int = 0,
             cache_read_tokens: int = 0) -> float:
        return (input_tokens * self.input + output_tokens * self.output
                + cache_write_tokens * self.cache_write + cache_read_tokens * self.cache_read) / MTOK


FREE = Price(0.0, 0.0, 0.0, 0.0)
FREE_LABEL = "Free"   # what a known-free model's spend reads as, instead of "$0.00"


def _anthropic(inp: float, out: float, read: float | None = None) -> Price:
    """Anthropic: a 5-minute cache write is 1.25x input; a read 0.1x unless the page says otherwise."""
    return Price(inp, out, inp * 1.25, inp * 0.1 if read is None else read)


def _openai(inp: float, cached: float, out: float, write: float | None = None) -> Price:
    """OpenAI: ``write`` where the page lists a cache-write price, else the input rate."""
    return Price(inp, out, inp if write is None else write, cached)


PRICES: dict[str, Price] = {
    # Anthropic
    "claude-fable-5-1": _anthropic(10, 50, read=0.25),
    "claude-mythos-5-1": _anthropic(10, 50, read=0.25),
    "claude-fable-5": _anthropic(10, 50),
    "claude-mythos-5": _anthropic(10, 50),
    "claude-opus-5-5": _anthropic(4, 20, read=0.20),
    "claude-opus-5": _anthropic(5, 25),
    "claude-opus-4-8": _anthropic(5, 25),
    "claude-opus-4-7": _anthropic(5, 25),
    "claude-opus-4-6": _anthropic(5, 25),
    "claude-opus-4-5": _anthropic(5, 25),
    "claude-opus-4-1": _anthropic(15, 75),
    "claude-opus-4": _anthropic(15, 75),
    "claude-opus-4-0": _anthropic(15, 75),
    "claude-sonnet-5-5": _anthropic(2, 10),
    "claude-sonnet-5": _anthropic(2, 10),
    "claude-sonnet-4-6": _anthropic(3, 15),
    "claude-sonnet-4-5": _anthropic(3, 15),
    "claude-sonnet-4": _anthropic(3, 15),
    "claude-sonnet-4-0": _anthropic(3, 15),
    "claude-haiku-5-5": _anthropic(0.10, 0.50),   # prompts up to 100K tokens; $0.50 / $2.50 beyond
    "claude-haiku-4-5": _anthropic(1, 5),
    "claude-3-5-haiku": _anthropic(0.80, 4),
    # OpenAI (Standard tier)
    "gpt-6-astra": _openai(10, 1, 50, write=12.50),
    "gpt-6.1-sol": _openai(2, 0.10, 10, write=2.50),
    "gpt-6-sol": _openai(2, 0.20, 10, write=2.50),
    "gpt-6-luna": _openai(0.10, 0.01, 0.50, write=0.125),
    "gpt-5.6-sol": _openai(4, 0.40, 20, write=5.00),
    "gpt-5.6-terra": _openai(2, 0.20, 12, write=2.50),
    "gpt-5.6-luna": _openai(0.20, 0.02, 1.20, write=0.25),
    "gpt-5.5": _openai(5, 0.50, 30),
    "gpt-5.4": _openai(2.50, 0.25, 15),
    "gpt-5.4-mini": _openai(0.75, 0.075, 4.50),
    "gpt-5.4-nano": _openai(0.20, 0.02, 1.25),
    "gpt-5.3-codex": _openai(1.75, 0.175, 14),
    "gpt-5.2": _openai(1.75, 0.175, 14),
    "gpt-5.1": _openai(1.25, 0.125, 10),
    "gpt-5": _openai(1.25, 0.125, 10),
    "gpt-5-mini": _openai(0.25, 0.025, 2),
    "gpt-5-nano": _openai(0.05, 0.005, 0.40),
    "o3": _openai(2, 0.50, 8),
    "o4-mini": _openai(1.10, 0.275, 4.40),
}

# Claude Code's model aliases, as profiles write them: each names the latest of its family.
ALIASES = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5",
}

_PREFIXES = re.compile(r"^(?:(?:us|eu|apac|global)\.)?(?:anthropic|openai)[./]")
_DATE = re.compile(r"(?:-|@)\d{8}$")
_SUFFIX = re.compile(r"\[[^\]]*\]$")


def normalize(model: str) -> str:
    """``model`` as a key of ``PRICES``: lower case, without a provider prefix
    (``anthropic/``, ``us.anthropic.``), a context tag (``[1m]``) or a date
    suffix (``-20251001``)."""
    m = _SUFFIX.sub("", model.strip().lower())
    m = _PREFIXES.sub("", m)
    m = _DATE.sub("", m)
    return ALIASES.get(m, m)


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
    """``model``'s price (``extra`` first, then ``PRICES``), or None when
    brindle doesn't know it."""
    if not model:
        return None
    key = normalize(model)
    if extra and key in extra:
        return extra[key]
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
