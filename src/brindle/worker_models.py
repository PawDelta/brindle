"""The models a task size (light, medium, heavy) can run on, as the workers
screen of the picker offers them.

Each model runs one of three ways: through a signed-in CLI (an existing
developer profile), locally on Ollama (``developer-local``), or hosted with an
API key (a native profile on OpenRouter, written when it is picked). A pick is
saved as ``routing_<size>`` in the user config; :func:`apply` turns the saved
picks into ``RepoConfig.routing`` and falls back to the built-in routing for a
size whose pick isn't ready.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from dataclasses import dataclass

from brindle import model_catalog, model_names

TIERS = ("light", "medium", "heavy")
CLI, LOCAL, KEY = "cli", "local", "key"
HOW = {CLI: "a signed-in CLI", LOCAL: "local (Ollama)", KEY: "hosted, with an API key (OpenRouter)"}
OPENROUTER_KEY = "OPENROUTER_API_KEY"
OPENROUTER_URL = "https://openrouter.ai/api/v1"

# Ranked profile routing policy, not model facts. Availability checks fall
# through this order; model metadata and suitability live in model_catalog.
TIER_PREFERENCES: dict[str, tuple[str, ...]] = {
    "light": ("dev.gpt", "dev.claude-haiku", "dev.claude-sonnet", "developer", "dev.qwen3-coder",
              "dev.gpt-luna", "dev.gpt-terra"),
    "medium": ("dev.gpt", "dev.claude-sonnet", "developer", "dev.claude-opus",
               "dev.gpt-terra", "dev.gpt-sol"),
    "heavy": ("dev.claude-opus", "dev.gemini", "developer", "dev.gpt",
              "dev.gpt-sol", "dev.gpt-astra"),
    "advanced": ("dev.claude-fable", "dev.claude-opus", "dev.gemini"),
}


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    how: str
    provider: str            # CLI name, or "native" for local and hosted
    profile: str             # the profile a pick runs on
    tiers: tuple[str, ...] = TIERS
    model: str | None = None  # what Ollama pulls, or what a hosted pick's profile asks OpenRouter for
    key_env: str | None = None


def model_tiers(model: str) -> tuple[str, ...]:
    found = model_catalog.lookup(model)
    return found.tiers if found else ()


def _pick(pick_id: str, model: str, how: str, provider: str, profile: str,
          tiers: tuple[str, ...] = ()) -> Model:
    return Model(pick_id, model_names.model_name(model, provider).display, how, provider, profile,
                 model_tiers(model) or tiers, model, OPENROUTER_KEY if how == KEY else None)


# These are existing profile bindings. Adding model facts never requires
# another name, price or tier table here; CLI-default choices have no model.
CATALOG: tuple[Model, ...] = (
    _pick("claude-haiku", "haiku", CLI, "claude", "dev.claude-haiku"),
    _pick("claude-sonnet", "sonnet", CLI, "claude", "dev.claude-sonnet"),
    _pick("claude-opus", "opus", CLI, "claude", "dev.claude-opus"),
    _pick("claude-fable", "fable", CLI, "claude", "dev.claude-fable"),
    Model("codex", "Codex", CLI, "codex", "dev.gpt"),
    _pick("gpt-6-luna", "gpt-6-luna", CLI, "codex", "dev.gpt-luna"),
    _pick("gpt-6.1-terra", "gpt-6.1-terra", CLI, "codex", "dev.gpt-terra"),
    _pick("gpt-6.1-sol", "gpt-6.1-sol", CLI, "codex", "dev.gpt-sol"),
    _pick("gpt-6-astra", "gpt-6-astra", CLI, "codex", "dev.gpt-astra"),
    _pick("antigravity", "gemini-4-argon", CLI, "antigravity", "dev.gemini"),
    _pick("local-qwen3-coder", "qwen3-coder:30b", LOCAL, "native", "dev.qwen3-coder"),
    _pick("or-qwen3-coder", "qwen/qwen3-coder", KEY, "native", "developer-or-qwen3-coder", ("light", "medium")),
    _pick("or-glm-4.6", "z-ai/glm-4.6", KEY, "native", "developer-or-glm-4.6", ("medium", "heavy")),
    _pick("or-kimi-k2", "moonshotai/kimi-k2", KEY, "native", "developer-or-kimi-k2", ("medium", "heavy")),
)


def find(model_id: object) -> Model | None:
    return next((m for m in CATALOG if m.id == model_id), None)


def runs(m: Model, repo_root: str | None = None, *, user_library: bool = False) -> tuple[str | None, str | None]:
    """(provider, model) of the profile ``m`` runs on, resolved as check_assign
    resolves them; a hosted pick whose profile isn't written yet runs what
    :func:`profile_text` will write. ``user_library``: the person's profile
    of that name, never the repo's own."""
    from brindle.policy import _profile_fields

    provider, model = _profile_fields(m.profile, repo_root or "", user_library=user_library)
    if provider is None and m.how == KEY:
        return "native", m.model
    return provider, model


def allowed(acc, m: Model, repo_root: str | None = None, *, user_library: bool = False) -> bool:
    """Whether ``acc`` (``team_policy.access``) would let a worker start on
    ``m``'s profile; never a hosted-key model when personal keys are denied."""
    if m.how == KEY and acc.deny_personal_keys:
        return False
    return acc.allows_profile(m.profile, *runs(m, repo_root, user_library=user_library))


def offered(acc, tier: str, repo_root: str | None = None) -> list[Model]:
    """The models for ``tier`` that ``acc`` allows, and nothing else."""
    return [m for m in CATALOG if tier in m.tiers and allowed(acc, m, repo_root)]


def ready(m: Model, signed_in: list[str]) -> bool:
    """Whether ``m`` can run now: its CLI is signed in, Ollama has it, or its key is stored."""
    if m.how == CLI:
        return m.provider in signed_in
    if m.how == LOCAL:
        from brindle import local_setup
        from brindle.native import serve

        return serve.ollama_available() and local_setup.verify_tags(local_setup.DEFAULT_HOST, m.model or "")
    import os

    from brindle import keystore

    return bool(os.environ.get(m.key_env or "") or keystore.get_key(m.key_env or ""))


def profile_text(m: Model) -> str:
    return (f"---\nname: {m.profile}\n"
            f"description: Implements a task on {m.label} through OpenRouter (written by the picker)\n"
            f"extends: developer-local\napi: openai\nbase_url: {OPENROUTER_URL}\nmodel: {m.model}\n"
            f"api_key_env: {m.key_env}\ncontext_tokens: 128k\n---\n")


def ensure_profile(m: Model) -> None:
    """Write the native profile a hosted model runs on, if the person has none by that name."""
    from brindle.config import user_profiles_dir

    if m.how != KEY or not re.fullmatch(r"[\w.-]+", m.profile):
        return
    path = user_profiles_dir() / f"{m.profile}.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(profile_text(m), encoding="utf-8")


SIGNED_IN_TTL = 60.0

_warned: set[tuple[str, str]] = set()
_local_ready: dict[str, bool] = {}    # model -> True once Ollama has it
_signed_in: tuple[float, list[str]] | None = None
_applying = threading.local()


def apply(cfg, user: dict, repo_root: str | None = None, *, user_library: bool = False) -> None:
    """Route each size the person picked a model for to that model's profile
    (``cfg.routing[size] = [profile]``). A pick that isn't ready, isn't allowed
    by the org policy now, or is gone from the catalog, leaves the built-in routing for that size and says so once.
    ``user_library``: a pick's profile is the person's, never the repo's own
    of that name (the org policy is still the repo's)."""
    # Judging a pick loads its profile, which loads the repo config again: that inner load keeps its routing.
    if getattr(_applying, "on", False):
        return
    _applying.on = True
    try:
        _apply(cfg, user, repo_root, user_library)
    finally:
        _applying.on = False


def _apply(cfg, user: dict, repo_root: str | None, user_library: bool = False) -> None:
    global _signed_in
    from brindle import doctor
    from brindle.config import user_config_path

    # A size the repo's own config routes stays as the repo set it.
    picks = {t: find(user.get(f"routing_{t}")) for t in TIERS
             if user.get(f"routing_{t}") and cfg.routing_sources[t] in ("DEFAULT_ROUTING", str(user_config_path()))}
    if not picks:
        return
    from brindle.pro import team_policy

    acc = team_policy.access(repo_root)      # the policy may have narrowed since the pick; unreadable: nothing
    from brindle import providers

    now = time.monotonic()
    fresh = _signed_in is not None and now < _signed_in[0]
    if fresh or providers.probing():
        if not fresh:
            _signed_in = (now + SIGNED_IN_TTL, doctor.signed_in_providers())
        signed_in = _signed_in[1]
    else:
        # No probe allowed here (providers.no_sign_in_probes) and none kept: an
        # installed CLI counts, as a sign-in that can't be told does, and nothing is kept.
        signed_in = doctor.signin_providers()

    def is_ready(m: Model) -> bool:
        if m.how == KEY and not providers.probing():
            # Can't be told without reading the credential store (a `security`
            # run on macOS): ready, as a can't-be-told sign-in is; a later read checks.
            return True
        if m.how != LOCAL:
            return ready(m, signed_in)
        key = m.model or ""
        if not providers.probing():
            return True     # can't be told without the HTTP probe of Ollama: a later read makes it
        if not _local_ready.get(key) and ready(m, signed_in):   # an HTTP probe of Ollama: cached once ready
            _local_ready[key] = True
        return _local_ready.get(key, False)

    for tier, m in picks.items():
        if m is not None and tier in m.tiers and allowed(acc, m, repo_root, user_library=user_library) and is_ready(m):
            cfg.routing[tier] = [m.profile]
            cfg.routing_sources[tier] = str(user_config_path())
        elif (tier, str(user.get(f"routing_{tier}"))) not in _warned:
            _warned.add((tier, str(user.get(f"routing_{tier}"))))
            name = m.label if m else user.get(f"routing_{tier}")
            print(f"brindle: {name} isn't ready for {tier} tasks; using the built-in {tier} profile "
                  "(`brindle -s`, then w, to fix it)", file=sys.stderr)
