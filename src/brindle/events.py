"""Events: tell an installed plugin what happens to worker tasks.

brindle emits an ``Event`` when a task is delegated (``assign`` or
``handoff``), when a reviewer gives a verdict (``review``), when the
supervisor has to step in (``escalated``), when a branch is merged
(``merge``), when a worktree is removed (``remove``), and when the repo's
policy refuses a delegation or a merge (``deny_assign``, ``deny_merge``,
with the reason). An event names the repo, the worker (id, branch, profile,
provider and model), the workspace and the actor that caused it (an agent
id, or ``"user"`` for a command run by hand), with a timestamp. It carries
no diff and no prompt or task text.

A plugin is a package registering an entry point in the ``brindle.events``
group whose object is a factory ``make(repo_root) -> EventsPlugin | None``;
see ``brindle.plugins`` for how plugins are selected. Every selected plugin
hears every event (brindle's own ``pro`` team feed and ``audit`` chain run
side by side). Emitting is guarded: a plugin that's missing or raises never
fails the operation being reported, and never keeps another plugin from
hearing the event.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from brindle import plugins
from brindle.config import RepoConfig
from brindle.db import Agent, Workspace

log = logging.getLogger(__name__)

GROUP = plugins.EVENTS
KINDS = ("assign", "handoff", "review", "escalated", "merge", "remove", "deny_assign",
         "deny_merge", "cost")
# Not an audit event: a count of how a model route did (see brindle.pro.route_health).
# Only the Team feed reports it; the audit chain and export leave it out.
ROUTE_HEALTH = "route_health"


@dataclass(frozen=True)
class Event:
    """One thing that happened. ``approved`` is set for a ``review``;
    ``merged`` for a ``remove`` (whether the branch had been merged);
    ``reason`` for a ``deny_*`` (what the policy said); ``route``,
    ``outcome`` and ``count`` for a ``route_health`` (``provider`` is the agent)."""
    kind: str
    repo_root: str
    agent_id: str | None = None
    branch: str | None = None
    profile: str | None = None
    provider: str | None = None
    model: str | None = None
    actor: str | None = None
    at: float = field(default_factory=time.time)
    approved: bool | None = None
    merged: bool | None = None
    workspace_id: str | None = None
    reason: str | None = None
    cost_usd: float | None = None   # what the worker's task cost (a ``remove`` of a Team org member)
    by_model: dict | None = None    # that cost split by model: {model: {"tokens", "usd"}}
    route: str | None = None        # route_health: the route the agent's calls took
    outcome: str | None = None      # route_health: one of brindle.pro.route_health.OUTCOMES
    count: int | None = None        # route_health: how many times, in the batch window


class EventsPlugin(ABC):
    """What an events plugin implements."""

    @abstractmethod
    def emit(self, event: Event) -> None:
        """Take note of ``event``."""


def plugins_for(cfg: RepoConfig, repo_root: str) -> list[EventsPlugin]:
    """Every events plugin the repo uses (see ``brindle.plugins.select_all``)."""
    return plugins.select_all(GROUP, cfg, repo_root)  # type: ignore[return-value]


def plugin(cfg: RepoConfig, repo_root: str) -> EventsPlugin | None:
    """The first of the repo's events plugins, or None."""
    found = plugins_for(cfg, repo_root)
    return found[0] if found else None


def _dispatch(cfg: RepoConfig, event: Event) -> None:
    """Hand ``event`` to every plugin; one that raises doesn't stop the rest."""
    for p in plugins_for(cfg, event.repo_root):
        try:
            p.emit(event)
        except Exception:
            log.exception("brindle: the events plugin %s failed on a %s event",
                          type(p).__name__, event.kind)


def _model(profile: str | None, repo_root: str, worker: Agent | None = None, db=None) -> str | None:
    if worker is not None:
        try:
            from brindle import usage

            m = usage._agent_model(db, worker)
            if m:
                return m
        except Exception:
            pass
    if not profile:
        return None
    try:
        from brindle.profiles import load_profile

        return load_profile(profile, repo_root).model
    except Exception:
        return None


def _cost(kind: str, worker: Agent | None, repo_root: str) -> float | None:
    """What ``worker`` spent, for the ``remove`` or ``cost`` event that ends it or tracks spend, when the
    org counts spend (Team ``org_budgets``); None otherwise or when it can't be priced."""
    if kind not in ("remove", "cost") or worker is None:
        return None
    db = None
    try:
        from brindle import budget
        from brindle.db import DB
        from brindle.pro import license

        if not license.has("org_budgets"):
            return None
        db = DB()
        spent = budget.worker_spend(db, worker, repo_root)
        if spent is None:
            return None
        get_rep = getattr(db, "get_reported_spend", None)
        reported = get_rep(worker.id) if callable(get_rep) else 0.0
        delta = round(spent - (reported or 0.0), 4)
        return delta if delta > 0 else None
    except Exception:
        log.debug("brindle: couldn't price %s for the event", worker.id, exc_info=True)
        return None
    finally:
        if db is not None:
            db.conn.close()


def _by_model(kind: str, worker: Agent | None, repo_root: str,
              cost_usd: float | None = None) -> dict | None:
    """``{model: {"tokens", "usd"}}`` for the spend ``_cost`` covers, under the
    same conditions. Unpriced tokens count with ``usd`` 0. When ``cost_usd``
    (which can include live usage) exceeds what the history rows add up to,
    the remainder is attributed to the worker's live/launch model (or
    "unknown model" when none is known), so the split sums to ``cost_usd``."""
    if kind not in ("remove", "cost") or worker is None:
        return None
    db = None
    try:
        from brindle import cost, usage
        from brindle.db import DB
        from brindle.pro import license

        if not license.has("org_budgets"):
            return None
        db = DB()
        pricer = cost.Pricer(repo_root)
        out: dict[str, dict] = {}
        for r in cost.rows_since(db, repo_root, 0.0):
            if r.agent_id != worker.id:
                continue
            p = pricer.row(r)
            if p is None:
                continue
            slot = out.setdefault(cost.model_label(p), {"tokens": 0, "usd": 0.0})
            slot["tokens"] += p.tokens
            slot["usd"] += p.dollars or 0.0
        live = None
        if getattr(worker, "provider", None) or getattr(worker, "transcript_path", None) or getattr(worker, "session_ref", None):
            try:
                live = usage.agent_usage(db, worker)
            except Exception:
                live = None
        live_model = (live.model if live and live.model and live.model != "unknown" else None) or _model(
            getattr(worker, "profile", None), repo_root, worker, db
        )
        if kind == "remove" and live and live.total > 0 and live_model:
            unrecorded = live.total - sum(b["tokens"] for b in out.values())
            if unrecorded > 0:
                slot = out.setdefault(live_model, {"tokens": 0, "usd": 0.0})
                slot["tokens"] += unrecorded
        total_usd = sum(b["usd"] for b in out.values())
        if cost_usd is not None and total_usd > cost_usd > 0:
            factor = cost_usd / total_usd
            for b in out.values():
                b["usd"] = round(b["usd"] * factor, 4)
            diff = round(cost_usd - sum(b["usd"] for b in out.values()), 4)
            if diff:
                first = next(iter(out.values()))
                first["usd"] = round(first["usd"] + diff, 4)
        else:
            rest = round((cost_usd or 0.0) - total_usd, 4)
            if rest > 0:
                slot = out.setdefault(live_model or "unknown model", {"tokens": 0, "usd": 0.0})
                slot["usd"] += rest
        return out or None
    except Exception:
        log.debug("brindle: couldn't split %s by model for the event", worker.id, exc_info=True)
        return None
    finally:
        if db is not None:
            db.conn.close()


def emit(cfg: RepoConfig, kind: str, ws: Workspace, worker: Agent | None = None, *,
         actor: Agent | str | None = None, approved: bool | None = None,
         merged: bool | None = None) -> None:
    """Tell the repo's events plugins that ``kind`` happened to ``worker`` in
    ``ws``. Does nothing without a plugin, and never raises."""
    try:
        who = actor.id if isinstance(actor, Agent) else (actor or "user")
        cost_usd = _cost(kind, worker, ws.repo_root)
        _dispatch(cfg, Event(
            kind=kind, repo_root=ws.repo_root, agent_id=worker.id if worker else None,
            branch=ws.branch, profile=worker.profile if worker else None,
            provider=worker.provider if worker else None,
            model=_model(worker.profile if worker else None, ws.repo_root, worker),
            actor=who, approved=approved, merged=merged, workspace_id=ws.id,
            cost_usd=cost_usd,
            by_model=_by_model(kind, worker, ws.repo_root, cost_usd),
        ))
        if cost_usd and worker and kind in ("remove", "cost"):
            db = None
            try:
                from brindle.db import DB
                db = DB()
                set_rep = getattr(db, "set_reported_spend", None)
                get_rep = getattr(db, "get_reported_spend", None)
                if callable(set_rep) and callable(get_rep):
                    old_reported = get_rep(worker.id) or 0.0
                    set_rep(worker.id, round(old_reported + cost_usd, 4))
            except Exception:
                pass
            finally:
                if db is not None:
                    db.conn.close()
    except Exception:
        log.exception("brindle: the events plugin failed on a %s event", kind)


def emit_periodic_cost(cfg: RepoConfig, repo_root: str) -> list[str]:
    """Check active agents (workers and supervisor sessions) in ``repo_root``,
    compute their delta spend since last report, and emit a 'cost' event
    if delta >= 0.01."""
    from brindle import budget
    from brindle.db import DB
    from brindle.pro import license

    done: list[str] = []
    try:
        if not license.has("org_budgets"):
            return done
    except Exception:
        return done

    db = None
    try:
        db = DB()
        for a in db.list_agents():
            if a.dismissed_at is not None:
                continue
            ws = db.get_workspace(a.workspace_id)
            if ws is None or ws.repo_root != repo_root:
                continue
            spent = budget.worker_spend(db, a, repo_root)
            if spent is None:
                continue
            get_rep = getattr(db, "get_reported_spend", None)
            reported = get_rep(a.id) if callable(get_rep) else 0.0
            delta = round(spent - (reported or 0.0), 4)
            if delta < 0.01:
                continue
            emit(cfg, "cost", ws, worker=a)
            done.append(f"emitted periodic cost ${delta:.2f} for agent {a.id}")
    except Exception:
        log.exception("brindle: periodic cost check failed for %s", repo_root)
    finally:
        if db is not None:
            db.conn.close()
    return done


def emit_denial(cfg: RepoConfig, repo_root: str, what: str, reason: str, *,
                branch: str | None = None, profile: str | None = None,
                provider: str | None = None, model: str | None = None,
                actor: str | None = None, agent_id: str | None = None,
                workspace_id: str | None = None) -> None:
    """Tell the repo's events plugins that the policy refused a ``what``
    (``"assign"`` or ``"merge"``) with ``reason``. Never raises."""
    try:
        _dispatch(cfg, Event(
            kind=f"deny_{what}", repo_root=repo_root, agent_id=agent_id, branch=branch,
            profile=profile, provider=provider, model=model, actor=actor or "user",
            workspace_id=workspace_id, reason=reason,
        ))
    except Exception:
        log.exception("brindle: the events plugin failed on a deny_%s event", what)


def emit_route_health(cfg: RepoConfig, repo_root: str, *, agent: str | None, route: str | None,
                      model: str | None, outcome: str, count: int) -> None:
    """Tell the repo's events plugins that ``agent``'s calls over ``route`` to
    ``model`` ended in ``outcome`` ``count`` times (see
    ``brindle.pro.route_health``). Never raises."""
    try:
        _dispatch(cfg, Event(kind=ROUTE_HEALTH, repo_root=repo_root, provider=agent, model=model,
                             route=route, outcome=outcome, count=count))
    except Exception:
        log.exception("brindle: the events plugin failed on a route_health event")


__all__ = ["GROUP", "KINDS", "ROUTE_HEALTH", "Event", "EventsPlugin", "emit", "emit_denial",
           "emit_periodic_cost", "emit_route_health", "plugin", "plugins_for"]
