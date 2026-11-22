"""How a model is named to a person: one place for every display name.

A profile's ``model`` is an id its CLI takes (``opus``, ``gpt-6.1-sol``,
``qwen3-coder:30b``); people see a family and model name ("Claude Opus",
"GPT‑6.1 Sol"), who made it and which tool runs it. ``describe`` resolves a
profile to all of that. A profile that pins no model is shown by its family
("Gemini"): the CLI picks the model, and the family is what's known.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brindle.profiles import Profile


@dataclass(frozen=True)
class ModelName:
    family: str            # "Claude", "GPT", "Gemini", "Qwen"
    display: str           # family and model: "Claude Opus", "GPT‑6.1 Sol"
    maker: str             # "Anthropic", "OpenAI", "Google", "Local"


# U+2011 (non-breaking hyphen) keeps "GPT‑6.1" on one line wherever it wraps.
MODELS: dict[str, ModelName] = {
    "haiku": ModelName("Claude", "Claude Haiku", "Anthropic"),
    "sonnet": ModelName("Claude", "Claude Sonnet", "Anthropic"),
    "opus": ModelName("Claude", "Claude Opus", "Anthropic"),
    "fable": ModelName("Claude", "Claude Fable", "Anthropic"),
    "gpt-6-luna": ModelName("GPT", "GPT‑6 Luna", "OpenAI"),
    "gpt-6-astra": ModelName("GPT", "GPT‑6 Astra", "OpenAI"),
    "gpt-6.1-sol": ModelName("GPT", "GPT‑6.1 Sol", "OpenAI"),
    "gpt-6.1-terra": ModelName("GPT", "GPT‑6.1 Terra", "OpenAI"),
    "gemini-4-argon": ModelName("Gemini", "Gemini 4 Argon", "Google"),
    "qwen3-coder:30b": ModelName("Qwen", "Qwen3 Coder", "Local"),
}

# Per provider: the family a profile with no model pinned is shown as, its
# maker, and the tool that runs it.
PROVIDERS: dict[str, tuple[ModelName, str]] = {
    "claude": (ModelName("Claude", "Claude", "Anthropic"), "Claude Code"),
    "subagent": (ModelName("Claude", "Claude", "Anthropic"), "Claude Code"),
    "codex": (ModelName("GPT", "GPT", "OpenAI"), "Codex"),
    "antigravity": (ModelName("Gemini", "Gemini", "Google"), "Antigravity"),
    "opencode": (ModelName("OpenCode", "OpenCode", "OpenCode"), "OpenCode"),
    "native": (ModelName("Local", "Local model", "Local"), "brindle"),
}

# A full Claude id (``claude-opus-5-5``, ``opus[1m]``) names its model by one of these.
_CLAUDE_MODELS = ("haiku", "sonnet", "opus", "fable")


def model_name(model: str | None, provider: str | None = None) -> ModelName:
    """The display name of ``model`` run by ``provider``; with no model, the
    provider's family. An id this table doesn't know is shown as written."""
    fallback, _ = PROVIDERS.get(provider or "claude", PROVIDERS["claude"])
    if not model:
        return fallback
    key = model.strip().lower()
    if key in MODELS:
        return MODELS[key]
    if provider in (None, "claude", "subagent") or key.startswith("claude"):
        for name in _CLAUDE_MODELS:
            if name in key:
                return MODELS[name]
    return ModelName(fallback.family, model, fallback.maker)


def tool_name(profile: Profile) -> str:
    """The tool that runs ``profile``: its provider's CLI, or Ollama for a
    native profile on a local Ollama endpoint."""
    if profile.provider == "native" and ":11434" in (profile.base_url or ""):
        return "Ollama"
    return PROVIDERS.get(profile.provider, (None, profile.provider))[1]


def describe(profile: Profile, model: str | None = None) -> dict[str, str | None]:
    """``{display, maker, tool, effort}`` for ``profile``; ``model`` is its
    effective model when known (an env override), else the profile's own."""
    name = model_name(model or profile.model, profile.provider)
    return {"display": name.display, "maker": name.maker,
            "tool": tool_name(profile), "effort": profile.effort or None}
