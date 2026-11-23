"""Shared model facts: names, prices, aliases and supported worker sizes.

Add a priced model here once. Pricing, display and worker metadata are views
of these rows. Discovery is advisory: an unlisted ID is still described, but
never inherits another version's price or an assumed capability tier.

Rates are USD/MTok, standard short-context text rates. Sources checked
2026-11-23: platform.claude.com/docs/en/about-claude/pricing,
developers.openai.com/api/docs/pricing, ai.google.dev/gemini-api/docs/pricing.
OpenCode's free routes come from the installed CLI's ``models --verbose``.
Legacy rates are retained for historical usage. Storage/tool fees are excluded.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

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


@dataclass(frozen=True)
class ModelName:
    family: str
    display: str
    maker: str


@dataclass(frozen=True)
class Model:
    name: ModelName
    price: Price
    tiers: tuple[str, ...]
    provider: str
    aliases: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()
    promo: tuple[dt.date, Price] | None = None

    @property
    def cost_tier(self) -> str:
        return cost_tier(self.price)


def cost_tier(price: Price | None) -> str:
    """A relative token-price band, independent of a task's difficulty."""
    if price is None:
        return "unknown"
    return "low" if price.output <= 5 else "medium" if price.output <= 15 else "high"


_PREFIXES = re.compile(r"^(?:(?:us|eu|apac|global)\.)?(?:anthropic|openai|google|antigravity)[./]")
_DATE = re.compile(r"(?:-|@)\d{8}$")
_SUFFIX = re.compile(r"\[[^\]]*\]$")
_EFFORT = re.compile(r"^((?:claude-|gemini-|gpt-oss-).+)-(low|medium|high|xhigh)$")


def split_effort(model: str) -> tuple[str, str | None]:
    key = _DATE.sub("", _PREFIXES.sub("", _SUFFIX.sub("", model.strip().lower())))
    match = _EFFORT.fullmatch(key)
    return (match[1], match[2]) if match else (key, None)


def normalize(model: str) -> str:
    key, _ = split_effort(model)
    return ALIASES.get(key, key)


def inferred_name(model: str, fallback: ModelName | None = None) -> ModelName:
    """Readable identity for a newly discovered model; no price inference."""
    key, _ = split_effort(model)
    key = key.rsplit("/", 1)[-1]
    fallback = fallback or ModelName("Unknown", "Unknown model", "Unknown")
    # Internal Codex routes are not public product names.
    if key in ("codex-auto-review", "gpt-reserve"):
        return ModelName("GPT", key, "OpenAI")
    if key.startswith("claude-"):
        tail = key.removeprefix("claude-")
        tail = re.sub(r"(?<=\d)-(?=\d)", ".", tail)
        return ModelName("Claude", "Claude " + tail.replace("-", " ").title(), "Anthropic")
    if key.startswith("gpt-"):
        tail = key.removeprefix("gpt-").replace("-", " ").title()
        return ModelName("GPT", "GPT‑" + tail, "OpenAI")
    if re.match(r"o\d(?:-|$)", key):
        return ModelName("GPT", "OpenAI " + key.replace("-", " "), "OpenAI")
    if key.startswith("gemini-"):
        return ModelName("Gemini", "Gemini " + key[7:].replace("-", " ").title(), "Google")
    families = {"qwen": ("Qwen", "Alibaba"), "glm": ("GLM", "Z.ai"),
                "kimi": ("Kimi", "Moonshot AI"), "deepseek": ("DeepSeek", "DeepSeek"),
                "minimax": ("MiniMax", "MiniMax"), "nemotron": ("Nemotron", "NVIDIA"),
                "mimo": ("MiMo", "Xiaomi"), "longcat": ("LongCat", "Meituan")}
    for prefix, (family, maker) in families.items():
        if key.startswith(prefix):
            tail = key[len(prefix):].replace("-", " ").replace(":", " ").title()
            return ModelName(family, family + tail, maker)
    return ModelName(fallback.family, key.replace("-", " ").replace("_", " ").title(), fallback.maker)


def _anthropic(inp: float, out: float, read: float | None = None) -> Price:
    return Price(inp, out, inp * 1.25, inp * 0.1 if read is None else read)


def _openai(inp: float, cached: float, out: float, write: float | None = None) -> Price:
    return Price(inp, out, inp if write is None else write, cached)


LIGHT = ("light",)
MEDIUM = ("light", "medium")
HEAVY = ("medium", "heavy")
ADVANCED = ("heavy", "advanced")
AGY_EFFORTS = ("low", "medium", "high")


def _model(model: str, price: Price, tiers: tuple[str, ...], *, aliases: tuple[str, ...] = (),
           efforts: tuple[str, ...] = (), promo: tuple[dt.date, Price] | None = None,
           name: ModelName | None = None) -> tuple[str, Model]:
    name = name or inferred_name(model)
    provider = {"Claude": "claude", "GPT": "codex", "Gemini": "antigravity"}.get(name.family, "native")
    if model.startswith("opencode/"):
        provider = "opencode"
    return model, Model(name, price, tiers, provider, aliases, efforts, promo)


# Task-size suitability is separate from cost: a free model is not necessarily
# suitable for a heavy task. These are metadata, not automatic routing choices.
CATALOG: dict[str, Model] = dict([
    _model("claude-fable-5-2", _anthropic(8, 40, 0.20), ADVANCED, aliases=("fable",)),
    _model("claude-mythos-5-2", _anthropic(8, 40, 0.20), ADVANCED),
    _model("claude-fable-5-1", _anthropic(10, 50, 0.25), ADVANCED),
    _model("claude-mythos-5-1", _anthropic(10, 50, 0.25), ADVANCED),
    _model("claude-fable-5", _anthropic(10, 50), ADVANCED),
    _model("claude-mythos-5", _anthropic(10, 50), ADVANCED),
    _model("claude-opus-5-5", _anthropic(4, 20, 0.20), HEAVY, aliases=("opus",), efforts=AGY_EFFORTS),
    _model("claude-opus-5", _anthropic(5, 25), HEAVY),
    _model("claude-opus-4-8", _anthropic(5, 25), HEAVY),
    _model("claude-opus-4-7", _anthropic(5, 25), HEAVY),
    _model("claude-opus-4-6", _anthropic(5, 25), HEAVY),
    _model("claude-opus-4-5", _anthropic(5, 25), HEAVY),
    _model("claude-opus-4-1", _anthropic(15, 75), HEAVY),
    _model("claude-opus-4", _anthropic(15, 75), HEAVY, aliases=("claude-opus-4-0",)),
    _model("claude-sonnet-5-5", _anthropic(2, 10, 0.10), MEDIUM, aliases=("sonnet",), efforts=AGY_EFFORTS),
    _model("claude-sonnet-5", _anthropic(2, 10), MEDIUM),
    _model("claude-sonnet-4-6", _anthropic(3, 15), MEDIUM),
    _model("claude-sonnet-4-5", _anthropic(3, 15), MEDIUM),
    _model("claude-sonnet-4", _anthropic(3, 15), MEDIUM, aliases=("claude-sonnet-4-0",)),
    _model("claude-haiku-5-5", _anthropic(0.10, 0.50), LIGHT, aliases=("haiku",)),  # <=100K input
    _model("claude-haiku-4-5", _anthropic(1, 5), LIGHT),
    _model("claude-3-5-haiku", _anthropic(0.80, 4), LIGHT),
    # IDs: https://developers.openai.com/api/docs/models/gpt-6.2-astra and /gpt-6.2-sol
    # Standard short-context rates: https://developers.openai.com/api/docs/pricing
    _model("gpt-6.2-astra", _openai(8, 0.40, 40, 10), ("heavy",)),
    _model("gpt-6.2-sol", _openai(2, 0.10, 10, 2.50), HEAVY),
    _model("gpt-6-astra", _openai(10, 1, 50, 12.50), ("heavy",)),
    _model("gpt-6.1-sol", _openai(2, 0.10, 10, 2.50), HEAVY),
    _model("gpt-6.1-terra", _openai(1, 0.05, 5, 1.25), MEDIUM),
    _model("gpt-6-sol", _openai(2, 0.20, 10, 2.50), HEAVY),
    _model("gpt-6-luna", _openai(0.10, 0.01, 0.50, 0.125), LIGHT),
    _model("gpt-5.6-sol", _openai(4, 0.40, 20, 5.00), HEAVY),
    _model("gpt-5.6-terra", _openai(2, 0.20, 12, 2.50), MEDIUM),
    _model("gpt-5.6-luna", _openai(0.20, 0.02, 1.20, 0.25), LIGHT),
    _model("gpt-5.5", _openai(5, 0.50, 30), HEAVY),
    _model("gpt-5.4", _openai(2.50, 0.25, 15), HEAVY),
    _model("gpt-5.4-mini", _openai(0.75, 0.075, 4.50), LIGHT),
    _model("gpt-5.4-nano", _openai(0.20, 0.02, 1.25), LIGHT),
    _model("gpt-5.3-codex", _openai(1.75, 0.175, 14), HEAVY),
    _model("gpt-5.2", _openai(1.75, 0.175, 14), HEAVY),
    _model("gpt-5.1", _openai(1.25, 0.125, 10), HEAVY),
    _model("gpt-5", _openai(1.25, 0.125, 10), HEAVY),
    _model("gpt-5-mini", _openai(0.25, 0.025, 2), LIGHT),
    _model("gpt-5-nano", _openai(0.05, 0.005, 0.40), LIGHT),
    _model("o3", _openai(2, 0.50, 8), HEAVY),
    _model("o4-mini", _openai(1.10, 0.275, 4.40), LIGHT),
    _model("gemini-4-argon", Price(4, 20, 4, 0.2), HEAVY, efforts=AGY_EFFORTS,
           promo=(dt.date(2026, 12, 1), Price(2, 10, 2, 0.1))),
    _model("gemini-4-carbon", Price(4, 20, 4, 0.2), ADVANCED, efforts=AGY_EFFORTS,
           promo=(dt.date(2026, 12, 1), Price(2, 10, 2, 0.1))),
    _model("gemini-4-neon", Price(3, 15, 3, 0.15), HEAVY, efforts=(*AGY_EFFORTS, "xhigh")),
    _model("gemini-4-hydrogen", Price(1.5, 7.5, 1.5, 0.075), MEDIUM, efforts=(*AGY_EFFORTS, "xhigh")),
    _model("gemini-3.8-flash", Price(1.5, 7.5, 1.5, 0.15), MEDIUM, efforts=AGY_EFFORTS,
           promo=(dt.date(2027, 1, 1), Price(0.75, 3.75, 0.75, 0.075))),
    _model("qwen3-coder:30b", FREE, MEDIUM, name=ModelName("Qwen", "Qwen3 Coder 30B", "Local")),
    *[_model("opencode/" + model, FREE, MEDIUM, name=ModelName(family, display, maker), efforts=efforts)
      for model, family, display, maker, efforts in (
          ("beam-free", "Beam", "Beam Free", "Unknown", (*AGY_EFFORTS, "xhigh", "max")),
          ("big-pickle", "Big Pickle", "Big Pickle", "Unknown", ()),
          ("deepseek-v4.1-flash-free", "DeepSeek", "DeepSeek V4.1 Flash Free", "DeepSeek", ("low", "high", "max")),
          ("flint-40b-free", "Flint", "Flint 40B Free", "Unknown", (*AGY_EFFORTS, "xhigh", "max")),
          ("glm-5.3-flash-free", "GLM", "GLM 5.3 Flash Free", "Z.ai", ("low", "high", "max")),
          ("hy4-flash-free", "Hy", "Hy4 Flash Free", "Tencent", ("none", "high")),
          ("iceman-free", "Iceman", "Iceman Free", "Unknown", ()),
          ("inkling-2-small-free", "Inkling", "Inkling 2 Small Free", "Unknown", ("none", "minimal", *AGY_EFFORTS, "max")),
          ("longcat-2.5-free", "LongCat", "LongCat 2.5 Free", "Meituan", AGY_EFFORTS),
          ("mimo-v2.6-flash-free", "MiMo", "MiMo V2.6 Flash Free", "Xiaomi", ()),
          ("minimax-m3.1-flash-free", "MiniMax", "MiniMax M3.1 Flash Free", "MiniMax", (*AGY_EFFORTS, "xhigh", "max")),
          ("nemotron-3.5-lightning-free", "Nemotron", "Nemotron 3.5 Lightning Free", "NVIDIA", ()),
          ("nemotron-3.5-super-vl-free", "Nemotron", "Nemotron 3.5 Super VL Free", "NVIDIA", ("none", "minimal", *AGY_EFFORTS, "xhigh")),
      )],
])

ALIASES = {alias: key for key, model in CATALOG.items() for alias in model.aliases}
_PROVIDER_DEFAULTS = {"claude": "sonnet", "codex": "gpt-6.2-sol", "opencode": "opencode/big-pickle"}


def default_model(provider: str) -> str | None:
    """Catalog default to pass explicitly when a profile leaves its model unset."""
    key = _PROVIDER_DEFAULTS.get(provider)
    model = ALIASES.get(key, key) if key else None
    return model if model in CATALOG else None


def lookup(model: str | None) -> Model | None:
    return CATALOG.get(normalize(model)) if model else None


_VERSION = re.compile(r"(?<=-)v?(\d{1,4}(?:[.-]\d{1,4})*)(?=-|$)")


def _versioned_line(model: str) -> tuple[tuple[str, str, str], tuple[int, ...]] | None:
    match = _VERSION.search(model)
    if match is None:
        return None
    entry = CATALOG.get(model)
    identity = entry.name if entry else inferred_name(model)
    line = model[:match.start()].rstrip("-") + model[match.end():]
    parts = [int(part) for part in re.split(r"[.-]", match[1])]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return (identity.maker, identity.family, line), tuple(parts)


def newer_versions(model: str) -> list[str]:
    """Known higher versions of an explicit pin in descending version order."""
    key, effort = split_effort(model)
    current = _versioned_line(ALIASES.get(key, key))
    if current is None or not _VERSION.search(key):
        return []
    line, version = current
    newer = []
    for name in CATALOG:
        candidate = _versioned_line(name)
        if candidate is not None and candidate[0] == line and candidate[1] > version:
            newer.append((candidate[1], name))
    prefix = _PREFIXES.match(model.strip().lower())
    return [
        (prefix[0] if prefix else "") + name + (f"-{effort}" if effort else "")
        for _, name in sorted(newer, reverse=True)
    ]
