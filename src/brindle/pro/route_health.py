"""Route health: how each agent's model route is doing, reported to the org as
``cli.route_health`` events on the Team event feed (:mod:`team_events`, the
same ``POST /orgs/{org_id}/events`` path as every CLI event), so the Routing
page can say "1,284 calls · 0 refused" per route and per model.

An event is agent, route, model, outcome and count: codes and bounded names,
never a provider's error text, a key, an account or a URL. The outcomes:

* ``identity_match`` / ``identity_mismatch`` / ``identity_undetermined``: the
  launch identity check (``company_identity.launch_check``), for an org whose
  policy enforces its company identity.
* ``model_unavailable``: the one ``model_availability`` result that is safe to
  act on (``unavailable``); ``unverified`` is never reported.
* ``policy_refused``: the org's managed-models policy refused the profile at
  launch.
* ``provider_401`` / ``provider_403`` / ``provider_404``: the provider refused a
  run's calls with that status (``provider_outcome`` reads only the code).

Only a signed-in Pro, Team or Enterprise user reports (an entitlement on a paid
plan with an org; a free account, no login or air-gap mode reports nothing).

Batching: ``record`` counts in memory per (repo, agent, route, model, outcome)
and one event per key goes out when the window closes: ``FLUSH_SECONDS`` after
the first record (a timer) or at exit. A session that starts fifty workers
against a dead route sends one event with count 50, not fifty events. At most
``MAX_KEYS`` distinct keys per window; later ones are dropped. Never raises.
"""

from __future__ import annotations

import atexit
import logging
import re
import threading
import time

from brindle import events
from brindle.events import Event

log = logging.getLogger(__name__)

KIND = events.ROUTE_HEALTH
AGENTS = ("claude", "codex", "ollama")             # what the backend's RouteHealthAgent accepts
OUTCOMES = ("identity_match", "identity_mismatch", "identity_undetermined", "model_unavailable",
            "policy_refused", "provider_401", "provider_403", "provider_404")
PAID_PLANS = ("pro", "team", "enterprise")
DEFAULT_MODEL = "default"                          # a profile that leaves the model to the CLI
FLUSH_SECONDS = 60.0
MAX_KEYS = 64
MAX_COUNT = 10_000                                 # the backend refuses more
ROUTE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_STATUS = re.compile(r"\b(401|403|404)\b")

_lock = threading.Lock()
_counts: dict[tuple[str, str, str, str, str], int] = {}
_timer: threading.Timer | None = None
_at_exit_registered = False


# -- what to report ------------------------------------------------------------------------------


def provider_outcome(text: str | None) -> str | None:
    """``provider_401``/``403``/``404`` when a provider's error ``text`` carries
    that status, else None. Only the code leaves this function; the text never
    reaches an event."""
    m = _STATUS.search(text or "")
    return f"provider_{m.group(1)}" if m else None


def route_of(provider: str, env, managed=None) -> str:
    """The route ``provider``'s calls take with ``env`` (the pane's effective
    variables), in the org policy's route names: ``bedrock``, ``vertex``,
    ``foundry``, ``gateway``, ``console`` or ``subscription`` for Claude;
    ``azure``, ``gateway``, ``api_key`` or ``chatgpt`` for Codex. ``managed``
    (a ``managed_models.Managed``) names the org's route when it has one."""
    env = env or {}
    if provider == "claude":
        if env.get("CLAUDE_CODE_USE_BEDROCK"):
            return "bedrock"
        if env.get("CLAUDE_CODE_USE_VERTEX"):
            return "vertex"
        if env.get("CLAUDE_CODE_USE_FOUNDRY"):
            return "foundry"
        if env.get("ANTHROPIC_BASE_URL"):
            return "gateway"
        if env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
            return "console"
        return "subscription"
    if provider == "codex":
        cfg = getattr(managed, "config", None)
        if cfg is not None and cfg.provider == "azure":
            return "azure"
        if env.get("OPENAI_BASE_URL"):
            return "gateway"
        if env.get("OPENAI_API_KEY"):
            return "api_key"
        return "chatgpt"
    return "default"


def org_for(ent) -> str | None:
    """The org ``ent`` (a ``license.Entitlement``) reports route health to, or
    None: a free plan or an org id the backend wouldn't take."""
    from brindle.pro.team_events import ORG_RE

    try:
        org = ent.org_id
        return org if ent.plan in PAID_PLANS and ORG_RE.match(org) else None
    except Exception:  # noqa: BLE001 - not an entitlement
        return None


def reporting() -> bool:
    """Whether this brindle reports route health at all: signed in on a paid
    plan, not in air-gap mode. Fails closed."""
    try:
        from brindle import airgap
        from brindle.pro import license

        if airgap.enabled():
            return False
        return org_for(license.current(refresh=False)) is not None
    except Exception:  # noqa: BLE001 - no entitlement, unreadable: nothing to report
        return False


def payload(ev: Event) -> dict | None:
    """The wire form of a ``route_health`` event (exactly the backend's
    fields), or None when the event isn't one the backend would take (an
    unknown agent or outcome, a model name that isn't a plain identifier)."""
    if ev.kind != KIND or ev.provider not in AGENTS or ev.outcome not in OUTCOMES:
        return None
    route = ev.route if isinstance(ev.route, str) and ROUTE_RE.match(ev.route) else None
    model = ev.model if isinstance(ev.model, str) and MODEL_RE.match(ev.model) else None
    if route is None or model is None:
        return None
    try:
        at = float(ev.at)
    except (TypeError, ValueError):
        at = time.time()
    if not 1_000_000_000 <= at <= 10_000_000_000:
        at = time.time()
    count = ev.count if isinstance(ev.count, int) and not isinstance(ev.count, bool) else 1
    return {"kind": KIND, "agent": ev.provider, "route": route, "model": model,
            "outcome": ev.outcome, "count": min(max(count, 1), MAX_COUNT), "at": at}


# -- batching ------------------------------------------------------------------------------------


def record(repo_root: str, *, agent: str | None, route: str | None, model: str | None,
           outcome: str) -> None:
    """Count one ``outcome`` for ``agent``'s calls over ``route`` to ``model``
    (``DEFAULT_MODEL`` when the profile names none), to go out with the
    window's batch. Nothing when this brindle doesn't report (see
    ``reporting``) or the event couldn't be sent anyway."""
    global _timer, _at_exit_registered

    try:
        if outcome not in OUTCOMES or agent not in AGENTS or not route or not reporting():
            return
        key = (repo_root, agent, route, model or DEFAULT_MODEL, outcome)
        with _lock:
            if key not in _counts and len(_counts) >= MAX_KEYS:
                log.debug("brindle: route health window full; %s dropped", key[1:])
                return
            _counts[key] = _counts.get(key, 0) + 1
            if not _at_exit_registered:
                atexit.register(_flush_at_exit)
                _at_exit_registered = True
            if _timer is None:
                _timer = threading.Timer(FLUSH_SECONDS, flush)
                _timer.daemon = True
                _timer.start()
    except Exception:  # noqa: BLE001 - never fail the launch or run being reported
        log.debug("brindle: couldn't record route health", exc_info=True)


def pending() -> dict[tuple[str, str, str, str, str], int]:
    """What the window holds so far (for tests and doctor)."""
    with _lock:
        return dict(_counts)


def _take() -> dict[tuple[str, str, str, str, str], int]:
    global _timer

    with _lock:
        taken = dict(_counts)
        _counts.clear()
        if _timer is not None:
            _timer.cancel()
            _timer = None
    return taken


def _config(repo_root: str):
    from brindle.config import RepoConfig, load_repo_config

    try:
        return load_repo_config(repo_root)
    except Exception:  # noqa: BLE001 - an unreadable repo config still has the installed plugins
        return RepoConfig()


def flush(at_exit: bool = False) -> int:
    """Close the window: one event per key to the repo's events plugins.
    Returns how many events went out. ``at_exit`` also asks the plugins that
    send in the background to spool and send what they can now."""
    sent = 0
    taken = _take()
    try:
        cfgs = {}
        for (repo_root, agent, route, model, outcome), n in taken.items():
            cfg = cfgs.setdefault(repo_root, _config(repo_root))
            events.emit_route_health(cfg, repo_root, agent=agent, route=route, model=model,
                                     outcome=outcome, count=n)
            sent += 1
        if at_exit:
            from brindle.pro.team_events import FLUSH_AT_EXIT

            for repo_root, cfg in cfgs.items():
                for p in events.plugins_for(cfg, repo_root):
                    flush_now = getattr(p, "flush", None)
                    if callable(flush_now):
                        flush_now(FLUSH_AT_EXIT)
    except Exception:  # noqa: BLE001
        log.debug("brindle: couldn't send route health", exc_info=True)
    return sent


def _flush_at_exit() -> None:
    flush(at_exit=True)


def reset() -> None:
    """Forget the window (tests)."""
    _take()


__all__ = ["AGENTS", "FLUSH_SECONDS", "KIND", "MAX_KEYS", "OUTCOMES", "PAID_PLANS", "flush", "org_for",
           "payload", "pending", "provider_outcome", "record", "reporting", "reset", "route_of"]
