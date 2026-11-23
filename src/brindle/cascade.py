"""One cascade for every role: which profiles can take this work now, best first.

A role is a worker tier ("light", "medium", "heavy", "advanced"), "reviewer"
or "supervisor". For a role and a task, :func:`run` returns the runnable
candidates in the order to try them:

1. Pool: every profile the role may use under the ceiling. For a worker tier
   that is its own tier first, then the heavier tiers up to the ceiling, then
   the lighter ones, then any other built-in profile for the role; a tier the
   owner pinned (``routing_locked``) is that tier only.
2. Drop what can't run now (the role's own check: provider_health, policy,
   budget and exclusion gates, the same checks as a fresh assign) and any
   provider this task already ran on (``after``).
3. Order by the measured table (``brindle/data/routing_priors.json``,
   version 2): measured candidates trade places among themselves by the
   log-gap score (``routing_goal.scores``: cost per success, minutes per
   success, first-try odds) on the Balanced weights 0.45/0.25/0.30 unless a
   goal says otherwise; a measured handover pair's success rate (from the
   profile this task last ran on) replaces the solo one; a measured candidate
   under 50% success goes last. A candidate the table hasn't measured (its
   cell under ``min_runs``) keeps its tier-order place.

With brindle Pro's learner the caller then asks the learner, with this
order's first as the default it must beat.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from brindle.model_names import canonical

log = logging.getLogger(__name__)

TIERS = ("light", "medium", "heavy", "advanced")
ROLES = (*TIERS, "reviewer", "supervisor")
VERSION = 2
CELL_FIELDS = ("p", "first_try", "usd", "minutes")
TIER_FIELDS = ("p", "first_try", "minutes", "mtok")
P_FLOOR = 0.5
MIN_MINUTES = 0.1      # a run measured at ~0 minutes still takes some time: scores divide by it
MIN_P = 0.01

# Built-in profiles a role falls back to once its configured candidates are out.
DEVELOPERS = ("dev.gpt", "dev.claude-sonnet", "developer", "dev.claude-haiku", "dev.claude-opus",
              "dev.gemini", "dev.opencode", "dev.claude-fable", "dev.qwen3-coder")
REVIEWERS = ("review.claude-sonnet.high", "reviewer", "review.gpt", "review.gemini", "review.opencode",
             "review.qwen3-coder")
# The supervisor takes its provider, model and effort from one of these.
SUPERVISORS = ("supervisor", "dev.gpt", "dev.gemini", "dev.opencode")


def balanced() -> dict[str, float]:
    """The weights routing aims for when no goal is set (as the learner's)."""
    from brindle.pro.learning import BALANCED_WEIGHTS

    return dict(zip(("cost", "speed", "first_try"), BALANCED_WEIGHTS))


# -- the table ---------------------------------------------------------------------------------------


class TableError(ValueError):
    """routing_priors.json isn't a version 2 table."""


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# What each number may be: rates are shares, everything else can't be negative.
RANGES = {"p": (0.0, 1.0), "first_try": (0.0, 1.0), "usd": (0.0, math.inf), "minutes": (0.0, math.inf),
          "mtok": (0.0, math.inf), "runs": (0, math.inf)}


def _valid(v, key: str) -> bool:
    lo, hi = RANGES.get(key, (-math.inf, math.inf))
    return _num(v) and lo <= v <= hi


@dataclass(frozen=True)
class Table:
    min_runs: int
    tiers: dict
    profiles: dict
    handover: dict

    def cell(self, profile: str, role: str, kind: str | None = None) -> dict | None:
        """``profile``'s measured cell for ``role`` (the ``role/kind`` cell
        before the ``role`` one), or None when it has none with ``min_runs``."""
        mine = self.profiles.get(canonical(profile)) or {}
        for key in ([f"{role}/{kind}"] if kind else []) + [role]:
            c = mine.get(key)
            if isinstance(c, dict) and _measured(c, self.min_runs):
                return c
        return None

    def handover_p(self, src: str, dst: str) -> float | None:
        """The success rate of ``dst`` taking over from ``src``, when measured."""
        c = self.handover.get(f"{canonical(src)}>{canonical(dst)}")
        return float(c["p"]) if isinstance(c, dict) and c.get("runs", 0) >= self.min_runs else None


def _measured(cell: dict, min_runs: int) -> bool:
    return isinstance(cell.get("runs"), int) and cell["runs"] >= min_runs \
        and all(_num(cell.get(k)) for k in CELL_FIELDS)


def parse(raw) -> Table:
    """A version 2 table from ``raw``; raises TableError for anything else."""
    if not isinstance(raw, dict):
        raise TableError("not an object")
    if raw.get("version") != VERSION:
        raise TableError(f"version {raw.get('version')!r}, not {VERSION}")
    min_runs = raw.get("min_runs")
    if not isinstance(min_runs, int) or isinstance(min_runs, bool) or min_runs < 1:
        raise TableError("min_runs must be a positive whole number")
    if not isinstance(raw.get("measured"), str) or not isinstance(raw.get("source"), str):
        raise TableError("measured and source must be text")
    tiers, profiles, handover = raw.get("tiers"), raw.get("profiles"), raw.get("handover", {})
    if not isinstance(tiers, dict) or any(not isinstance(tiers.get(t), dict)
                                          or not all(_valid(tiers[t].get(k), k) for k in TIER_FIELDS)
                                          for t in TIERS):
        raise TableError(f"tiers must give {', '.join(TIER_FIELDS)} for {', '.join(TIERS)}")
    if not isinstance(profiles, dict) or not all(
            isinstance(cells, dict) and all(isinstance(c, dict) for c in cells.values())
            for cells in profiles.values()):
        raise TableError("profiles must map each profile to its cells")
    for name, cells in profiles.items():
        for key, c in cells.items():
            bad = [k for k in (*CELL_FIELDS, "runs") if k in c and not _valid(c[k], k)]
            if bad:
                raise TableError(f"{name} {key}: {', '.join(bad)} out of range")
    if not isinstance(handover, dict) or not all(
            isinstance(k, str) and ">" in k and isinstance(c, dict) and _valid(c.get("p"), "p")
            and isinstance(c.get("runs"), int) for k, c in handover.items()):
        raise TableError("handover must map from>to pairs to {p, runs}")
    # The table's rows predate the renames: an old id's cells count for its renamed profile.
    return Table(min_runs, tiers, _by_identity(profiles), _handover_by_identity(handover))


def _by_identity(profiles: dict) -> dict:
    """``profiles`` keyed by current ids. An identity named twice (an old and a
    new id) keeps the evidence of both, whichever comes first."""
    out: dict = {}
    for name, cells in profiles.items():
        mine = out.setdefault(canonical(name), {})
        for role, cell in cells.items():
            mine[role] = _merge_cells(mine[role], cell) if role in mine else cell
    return out


def _handover_by_identity(handover: dict) -> dict:
    out: dict = {}
    for pair, cell in handover.items():
        key = ">".join(canonical(p) for p in pair.split(">", 1))
        out[key] = _merge_cells(out[key], cell) if key in out else cell
    return out


def _merge_cells(a: dict, b: dict) -> dict:
    """One cell from two cells of the same identity, the same whichever is
    first. Runs add up. ``p`` and ``first_try`` are weighted by runs. ``usd``
    and ``minutes`` are medians, and a median of two medians is not the median
    of the pooled runs, so they take the cell with more runs (the larger sample
    speaks for both) rather than an average. The pooled median would need the
    runs themselves, which a table keeps only as summaries."""
    wa, wb = a.get("runs") or 0, b.get("runs") or 0
    if wa + wb == 0:
        return {**b, **a}
    out = {**b, **a}
    for k in set(a) & set(b):
        if k == "runs":
            out[k] = wa + wb
        elif k in ("usd", "minutes"):
            out[k] = a[k] if (wa, repr(a[k])) >= (wb, repr(b[k])) else b[k]
        elif _num(a[k]) and _num(b[k]):
            out[k] = (a[k] * wa + b[k] * wb) / (wa + wb)
        else:
            out[k] = max(a[k], b[k], key=repr)
    return out


_warned = False


def table() -> Table | None:
    """The shipped table, or None (routing then keeps tier order) when it is
    missing, older or malformed: said once per process."""
    global _warned
    from brindle import routing_goal

    try:
        return parse(routing_goal.priors())
    except Exception as e:  # noqa: BLE001 - a bad table is no evidence, never a failed route
        if not _warned:
            _warned = True
            log.warning("brindle: routing_priors.json can't be used (%s); routing keeps tier order", e)
        return None


# -- ordering ----------------------------------------------------------------------------------------


def order(candidates: list[str], role: str, *, kind: str | None = None, after: Iterable[str] = (),
          weights: dict | None = None, tbl: Table | None = None, lead: str | None = None) -> list[str]:
    """``candidates`` (runnable, in tier order) ordered by the table (see the
    module's docstring). ``lead`` stays first when it is among them (an
    owner's or learner's own pick: the table orders what replaces it)."""
    from brindle import routing_goal

    tbl = tbl if tbl is not None else table()
    rest = [c for c in candidates if c != lead]
    head = [lead] if lead in candidates else []
    if tbl is None:
        return head + rest
    last = list(after)[-1:] if after else []
    est = {}
    for c in rest:
        cell = tbl.cell(c, role, kind)
        if cell is None:
            continue
        p = tbl.handover_p(last[0], c) if last else None
        est[c] = {"usd": max(float(cell["usd"]), routing_goal.MIN_USD),
                  "minutes": max(float(cell["minutes"]), MIN_MINUTES),
                  "first_try": float(cell["first_try"]), "p": max(p if p is not None else float(cell["p"]), MIN_P)}
    if len(est) >= 2:
        s = routing_goal.scores(est, weights or balanced())
        ranked = iter(sorted(est, key=lambda c: (s[c], rest.index(c))))
        rest = [next(ranked) if c in est else c for c in rest]
    weak = [c for c in rest if c in est and est[c]["p"] < P_FLOOR]
    return head + [c for c in rest if c not in weak] + weak


# -- the cascade -------------------------------------------------------------------------------------


@dataclass
class Result:
    pool: list[str]                                     # every candidate considered, in tier order
    order: list[str]                                    # those that can run now, best first
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (candidate, why it can't)
    # Candidates that could run but the assignment policy refused (they are in ``skipped`` too).
    policy_refused: list[str] = field(default_factory=list)

    @property
    def first(self) -> str | None:
        return self.order[0] if self.order else None


def run(role: str, pool: Iterable[str], why_not: Callable[[str], str | None], *, repo_root: str | None,
        kind: str | None = None, after: Iterable[str] = (), weights: dict | None = None,
        lead: str | None = None, pinned: bool = False, beyond: int | None = None) -> Result:
    """The cascade over ``pool`` (in tier order) for ``role``: ``why_not``
    says why a candidate can't run now (None: it can); a candidate on a
    provider one of ``after`` (profiles this task already ran on) used is
    dropped too. ``pinned``: the owner pinned this order, the table never
    reorders it. ``beyond``: where the pool's replacements start (the rest of
    the pool past the role's own list); while something in the role's own
    list can run, a replacement the table hasn't measured could never come
    ahead of it, so it isn't checked (each check can be slow) and isn't in
    ``order``. Once the own list is out, every replacement is checked; a
    caller that will weigh every replacement itself (a learner) passes
    ``beyond=None`` to have them all checked."""
    after = [a for a in after if a]
    used = {p for p in (_provider(a, repo_root) for a in after) if p}
    names = list(dict.fromkeys(n for n in pool if n))
    tbl = None if pinned else table()
    runnable, skipped = [], []
    for i, name in enumerate(names):
        if beyond is not None and i >= beyond and any(names.index(r) < beyond for r in runnable) \
                and (pinned or tbl is None or tbl.cell(name, role, kind) is None):
            continue
        provider = _provider(name, repo_root)
        why = f"this task already ran on {provider}, skipped {name}" if provider in used else why_not(name)
        if why:
            skipped.append((name, why))
        else:
            runnable.append(name)
    if pinned:
        return Result(names, runnable, skipped)
    return Result(names, order(runnable, role, kind=kind, after=after, weights=weights, lead=lead, tbl=tbl),
                  skipped)


def _provider(name: str, repo_root: str | None) -> str | None:
    from brindle.profiles import ProfileError, load_profile

    try:
        return load_profile(name, repo_root).provider
    except (KeyError, ProfileError):
        return None


# -- pools -------------------------------------------------------------------------------------------


def worker_pool(cfg, repo_root: str, tier: str, ceiling: str | None = None) -> list[str]:
    """Every profile a ``tier`` task may use under ``ceiling`` (else the
    repo's ``max_weight``), in tier order: the tier, heavier tiers to the
    ceiling, lighter tiers, then the other built-in developers. A pinned
    tier is its own list only."""
    from brindle import autopilot

    rank = {t: i for i, t in enumerate(TIERS)}
    ceiling = ceiling or (cfg.max_weight if cfg.max_weight in rank else None)
    top = rank[ceiling] if ceiling in rank else len(TIERS) - 1
    own = autopilot.tier_order(cfg, tier)
    if cfg.routing_locked.get(tier) or tier not in rank:
        names = own
    else:
        i = rank[tier]
        heavier = [t for t in TIERS[i + 1:top + 1]]
        lighter = list(reversed(TIERS[:i]))
        names = own + [n for t in heavier + lighter for n in autopilot.tier_order(cfg, t)] + list(DEVELOPERS)
    from brindle.profiles import current_name, existing_profiles

    names = existing_profiles(dict.fromkeys(current_name(n, repo_root) for n in names), repo_root)
    if ceiling in rank:
        names = [n for n in names if rank.get(_weight(autopilot.profile_weight(n, cfg, repo_root)), 3)
                 <= rank[ceiling]]
    return names


def _weight(w: str) -> str:
    return {"reasoning": "advanced", "complex": "advanced"}.get(w, w)


def agent_pool(cfg, repo_root: str, agent: str | None, tier: str, ceiling: str | None = None) -> list[str] | None:
    """``agent``'s own model order for a ``tier`` task (``agent_models``)
    under ``ceiling`` (else the repo's ``max_weight``), or None when it has
    none and the shared table applies. Its own order is locked: it is the
    whole pool, never reordered and never overridden by the learner."""
    names = (cfg.agent_models.get(agent) or {}).get(tier) if agent else None
    if not names:
        return None
    from brindle import autopilot
    from brindle.profiles import current_name, existing_profiles

    rank = {t: i for i, t in enumerate(TIERS)}
    ceiling = ceiling or (cfg.max_weight if cfg.max_weight in rank else None)
    names = existing_profiles(dict.fromkeys(current_name(n, repo_root) for n in names), repo_root)
    if ceiling in rank:
        names = [n for n in names if rank.get(_weight(autopilot.profile_weight(n, cfg, repo_root)), 3)
                 <= rank[ceiling]]
    return names


def workers(cfg, repo_root: str, tier: str, *, gate=None, ceiling: str | None = None,
            design_heavy: bool = False, after: Iterable[str] = (), kind: str | None = None,
            weights: dict | None = None, policy: Callable[[str], str | None] | None = None,
            full: bool = False, agent: str | None = None,
            availability: Callable[[str], str | None] | None = None) -> Result:
    """The cascade for a worker of ``tier``: what ``autopilot.eligible``
    checks for its own tier, over the whole pool, plus the assignment
    policy (``policy``: why it refuses a profile for this task, as a fresh
    assign decides). A candidate the policy refuses is never in ``order``;
    ``policy_refused`` names those that could otherwise have run, so a
    caller can refuse the delegation as policy does. ``full``: check every
    replacement even while the tier has a candidate (for a learner that
    weighs them all). ``agent``: who does the task; its own ``agent_models``
    order for ``tier`` (``agent_pool``), when set, is the pool instead.
    ``availability`` checks a candidate in a known launch context."""
    from brindle import autopilot

    refused: list[str] = []

    def why_not(name: str) -> str | None:
        if not cfg.routing_allows(name, design_heavy):
            return f"Fable is reserved for design-heavy tasks, skipped {name}"
        why = availability(name) if availability is not None else autopilot._unavailable(name, cfg, repo_root, gate, tier)
        if why is None and policy is not None and (no := policy(name)):
            refused.append(name)
            return f"refused by policy ({no}), skipped {name}"
        return why

    own_order = agent_pool(cfg, repo_root, agent, tier, ceiling)
    if own_order is not None:
        found = run(tier, own_order, why_not, repo_root=repo_root, kind=kind, after=after, pinned=True)
        found.policy_refused = refused
        return found
    pool = worker_pool(cfg, repo_root, tier, ceiling)
    own = set(autopilot.tier_order(cfg, tier))
    found = run(tier, pool, why_not, repo_root=repo_root, kind=kind, after=after, weights=weights,
                pinned=bool(cfg.routing_locked.get(tier)),
                beyond=None if full else sum(1 for n in pool if n in own))
    found.policy_refused = refused
    return found
