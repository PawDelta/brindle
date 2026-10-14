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

TIERS = ("light", "medium", "heavy")
CLI, LOCAL, KEY = "cli", "local", "key"
HOW = {CLI: "a signed-in CLI", LOCAL: "local (Ollama)", KEY: "hosted, with an API key (OpenRouter)"}
OPENROUTER_KEY = "OPENROUTER_API_KEY"
OPENROUTER_URL = "https://openrouter.ai/api/v1"


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


CATALOG: tuple[Model, ...] = (
    Model("claude-haiku", "Claude Haiku", CLI, "claude", "developer-light", ("light",)),
    Model("claude-sonnet", "Claude Sonnet", CLI, "claude", "developer", ("light", "medium")),
    Model("claude-opus", "Claude Opus", CLI, "claude", "developer-heavy", ("medium", "heavy")),
    Model("codex", "Codex", CLI, "codex", "developer-codex"),
    Model("antigravity", "Gemini (Antigravity)", CLI, "antigravity", "developer-gemini", ("medium", "heavy")),
    Model("local-qwen3-coder", "Qwen3 Coder 30B", LOCAL, "native", "developer-local", ("light", "medium"),
          "qwen3-coder:30b"),
    Model("or-qwen3-coder", "Qwen3 Coder", KEY, "native", "developer-or-qwen3-coder", ("light", "medium"),
          "qwen/qwen3-coder", OPENROUTER_KEY),
    Model("or-glm-4.6", "GLM 4.6", KEY, "native", "developer-or-glm-4.6", ("medium", "heavy"),
          "z-ai/glm-4.6", OPENROUTER_KEY),
    Model("or-kimi-k2", "Kimi K2", KEY, "native", "developer-or-kimi-k2", ("medium", "heavy"),
          "moonshotai/kimi-k2", OPENROUTER_KEY),
)


def find(model_id: object) -> Model | None:
    return next((m for m in CATALOG if m.id == model_id), None)


def runs(m: Model, repo_root: str | None = None) -> tuple[str | None, str | None]:
    """(provider, model) of the profile ``m`` runs on, resolved as check_assign
    resolves them; a hosted pick whose profile isn't written yet runs what
    :func:`profile_text` will write."""
    from brindle.policy import _profile_fields

    provider, model = _profile_fields(m.profile, repo_root or "")
    if provider is None and m.how == KEY:
        return "native", m.model
    return provider, model


def allowed(acc, m: Model, repo_root: str | None = None) -> bool:
    """Whether ``acc`` (``team_policy.access``) would let a worker start on
    ``m``'s profile; never a hosted-key model when personal keys are denied."""
    if m.how == KEY and acc.deny_personal_keys:
        return False
    return acc.allows_profile(m.profile, *runs(m, repo_root))


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


def apply(cfg, user: dict, repo_root: str | None = None) -> None:
    """Route each size the person picked a model for to that model's profile
    (``cfg.routing[size] = [profile]``). A pick that isn't ready, isn't allowed
    by the org policy now, or is gone from the catalog, leaves the built-in routing for that size and says so once."""
    # Judging a pick loads its profile, which loads the repo config again: that inner load keeps its routing.
    if getattr(_applying, "on", False):
        return
    _applying.on = True
    try:
        _apply(cfg, user, repo_root)
    finally:
        _applying.on = False


def _apply(cfg, user: dict, repo_root: str | None) -> None:
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
    now = time.monotonic()
    if _signed_in is None or now >= _signed_in[0]:
        _signed_in = (now + SIGNED_IN_TTL, doctor.signed_in_providers())
    signed_in = _signed_in[1]

    def is_ready(m: Model) -> bool:
        if m.how != LOCAL:
            return ready(m, signed_in)
        key = m.model or ""
        if not _local_ready.get(key) and ready(m, signed_in):   # an HTTP probe of Ollama: cached once ready
            _local_ready[key] = True
        return _local_ready.get(key, False)

    for tier, m in picks.items():
        if m is not None and tier in m.tiers and allowed(acc, m, repo_root) and is_ready(m):
            cfg.routing[tier] = [m.profile]
            cfg.routing_sources[tier] = str(user_config_path())
        elif (tier, str(user.get(f"routing_{tier}"))) not in _warned:
            _warned.add((tier, str(user.get(f"routing_{tier}"))))
            name = m.label if m else user.get(f"routing_{tier}")
            print(f"brindle: {name} isn't ready for {tier} tasks; using the built-in {tier} profile "
                  "(`brindle -s`, then w, to fix it)", file=sys.stderr)
