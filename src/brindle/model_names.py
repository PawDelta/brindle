"""How a model is named to a person: one place for every display name.

A profile's ``model`` is an id its CLI takes (``opus``, ``gpt-6.1-sol``,
``qwen3-coder:30b``); people see a family and model name ("Claude Opus",
"GPT‑6.1 Sol"), who made it and which tool runs it. ``describe`` resolves a
profile to all of that. A profile that pins no model is shown by its family
("Gemini"): the CLI picks the model, and the family is what's known.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brindle import model_catalog
from brindle.model_catalog import ModelName

if TYPE_CHECKING:
    from brindle.profiles import Profile


MODELS: dict[str, ModelName] = {key: model.name for key, model in model_catalog.CATALOG.items()}
MODELS.update({alias: MODELS[key] for alias, key in model_catalog.ALIASES.items()})

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


def model_name(model: str | None, provider: str | None = None) -> ModelName:
    """The display name of ``model`` run by ``provider``; with no model, the
    provider's family. New IDs retain their family and version."""
    fallback, _ = PROVIDERS.get(provider or "claude", PROVIDERS["claude"])
    if not model:
        return fallback
    found = model_catalog.lookup(model)
    if found:
        return found.name
    return model_catalog.inferred_name(model, fallback)


def tool_name(profile: Profile) -> str:
    """The tool that runs ``profile``: its provider's CLI, or Ollama for a
    native profile on a local Ollama endpoint."""
    if profile.provider == "native" and ":11434" in (profile.base_url or ""):
        return "Ollama"
    return PROVIDERS.get(profile.provider, (None, profile.provider))[1]


# Built-in profile ids renamed to ``role.family-model[.effort]``: old id -> new id.
# Every place a profile name is read resolves an old id through this table, so
# existing config, milestones, ``extends:`` and learner history keep working.
ALIASES: dict[str, str] = {
    "developer-heavy": "dev.claude-opus",
    "developer-light": "dev.claude-haiku",
    "developer-sonnet": "dev.claude-sonnet",
    "developer-fable": "dev.claude-fable",
    "developer-codex": "dev.gpt",
    "developer-codex-sol": "dev.gpt-sol",
    "developer-codex-luna": "dev.gpt-luna",
    "developer-codex-terra": "dev.gpt-terra",
    "developer-codex-astra": "dev.gpt-astra",
    "developer-gemini": "dev.gemini",
    "developer-local": "dev.qwen3-coder",
    "developer-opencode": "dev.opencode",
    "reviewer-light": "review.claude-sonnet.low",
    "reviewer-sonnet-high": "review.claude-sonnet.high",
    "reviewer-codex": "review.gpt",
    "reviewer-codex-light": "review.gpt.low",
    "reviewer-gemini": "review.gemini",
    "reviewer-local": "review.qwen3-coder",
    "reviewer-opencode": "review.opencode",
}


def canonical(name: str) -> str:
    """The current id of a profile named ``name``: an old built-in id maps to
    its renamed profile, any other name is returned as it is."""
    return ALIASES.get(name, name)


def is_reviewer_id(name: str) -> bool:
    """Whether a profile id is a reviewer's: ``reviewer...`` (old) or ``review.`` (current)."""
    return canonical(name).startswith(("reviewer", "review."))


# The config keys that name profiles: one id each, or a list of ids.
_CONFIG_PROFILE_KEYS = ("default_agent", "reviewer", "review_profile", "supervisor_profile", "learned_rules_profile")
_CONFIG_PROFILE_LISTS = ("learning_candidates", "reviewer_fallbacks", "supervisor_fallbacks")


def rename_in_config(data: dict, keep: frozenset = frozenset()) -> tuple[dict, dict[str, str]]:
    """``data`` (a parsed config file) with its old built-in profile ids renamed,
    and the renames made, ``{old: new}``. Ids in ``keep`` (a custom profile
    under that old name) are left alone. Nothing is renamed unless a caller writes it."""
    done: dict[str, str] = {}

    def one(v):
        if isinstance(v, str) and v in ALIASES and v not in keep:
            done[v] = ALIASES[v]
            return ALIASES[v]
        return v

    out = dict(data)
    for key in _CONFIG_PROFILE_KEYS:
        if key in out:
            out[key] = one(out[key])
    for key in _CONFIG_PROFILE_LISTS:
        if isinstance(out.get(key), list):
            out[key] = [one(v) for v in out[key]]
    if isinstance(out.get("routing"), dict):
        out["routing"] = {t: [one(v) for v in names] if isinstance(names, list) else names
                          for t, names in out["routing"].items()}
    return out, done


def describe(profile: Profile, model: str | None = None) -> dict[str, str | None]:
    """``{display, maker, tool, effort}`` for ``profile``; ``model`` is its
    effective model when known (an env override), else the profile's own."""
    selected = model or profile.model
    name = model_name(selected, profile.provider)
    _, effort = model_catalog.split_effort(selected) if selected else (None, None)
    return {"display": name.display, "maker": name.maker,
            "tool": tool_name(profile), "effort": effort or profile.effort or None}
