"""What routing aims for: a mix of cost, speed and first try (design
routing-goals §1, §3, §6a).

A goal is three weights, ``cost``, ``speed`` and ``first_try``, each 0–1 with
at least one above 0, kept summing to 1. It is set at four levels and the most
specific wins::

    built-in (none) < personal < repo < session < task   -> clamped by org policy

- personal: ``routing_goal`` in ~/.brindle/config.json
- repo: ``routing_goal`` in the repo's shared .brindle/config.json
- session: the ``session_routing_goals`` row of the session's root
- task: ``goal=`` on assign/handoff, a preset name or a weight object

With nothing set anywhere (and no org default) there is no goal: routing is
exactly what it was, and the learner is sent no weights. The org's policy
(``routing_goal`` in the team policy: ``default``, per-weight ``min`` and
``max``, ``locked``) can floor, cap or lock it; the server applies it again on
every suggest and never trusts this clamp.

Before any hosted estimate exists (Free, offline, a new install: §4.1), the
goal orders each tier's eligible candidates by §1's score on the shipped
priors (``brindle/data/routing_priors.json``): that order is routing's pick
without hosted learning and the default the learner must beat. A tier the
owner locked keeps its order.

The triangle (§6a) places a *setup* (an outcome: $ a task, minutes, first try)
against a yardstick, Fable alone, at the centre. The preview's own point is
this repo's routing under the goal, on its recent mix of tiers, against Fable
on the same mix; the reference setups ship in
``brindle/data/reference_setups.json``.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from importlib import resources

log = logging.getLogger(__name__)

KEYS = ("cost", "speed", "first_try")
SETTING = "routing_goal"
SCOPES = ("personal", "repo", "session")
# Retunable as evidence arrives (§3): a preset is a lean, not an absolute.
PRESETS: dict[str, dict[str, float]] = {
    "balanced": {"cost": 1 / 3, "speed": 1 / 3, "first_try": 1 / 3},
    "cheap": {"cost": 0.7, "speed": 0.1, "first_try": 0.2},
    "fast": {"cost": 0.1, "speed": 0.7, "first_try": 0.2},
    "first_try": {"cost": 0.2, "speed": 0.1, "first_try": 0.7},
}
ALIASES = {"cost": "cheap", "cheapest": "cheap", "speed": "fast", "fastest": "fast", "first-try": "first_try"}
ODDS_CLIP = (0.02, 0.98)


class GoalError(ValueError):
    """A goal that isn't three weights 0–1 with one above 0, or an unknown preset."""


def normalize(raw) -> dict[str, float]:
    """``raw`` (a preset name or ``{cost, speed, first_try}``) as weights
    summing to 1. A missing weight is 0; anything else is refused."""
    if isinstance(raw, str):
        name = ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if name not in PRESETS:
            raise GoalError(f"goal must be one of {', '.join(PRESETS)} or weights {{cost, speed, first_try}}, "
                            f"not {raw!r}")
        return dict(PRESETS[name])
    if not isinstance(raw, dict):
        raise GoalError("weights must be an object {cost, speed, first_try}")
    if extra := sorted(set(raw) - set(KEYS)):
        raise GoalError(f"unknown weight(s): {', '.join(map(str, extra))}")
    w = {}
    for k in KEYS:
        v = raw.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise GoalError(f"{k} must be a number from 0 to 1")
        w[k] = float(v)
    total = sum(w.values())
    if total <= 0:
        raise GoalError("at least one weight must be above 0")
    return {k: round(v / total, 4) for k, v in w.items()}


# -- org policy ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class OrgGoal:
    """The org's word on the goal: a ``default`` for members, per-weight
    ``low`` floors and ``high`` ceilings, and ``locked`` (no personal, repo,
    session or task goal applies)."""
    default: dict | None = None
    low: dict | None = None
    high: dict | None = None
    locked: bool = False

    def to_json(self) -> dict:
        return {"default": self.default, "min": self.low, "max": self.high, "locked": self.locked}


def parse_org_goal(raw) -> OrgGoal | None:
    """The team policy's ``routing_goal`` block, or None when absent.
    Malformed raises GoalError (the caller ignores it, as for agent_setup)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise GoalError("routing_goal must be an object")
    default = normalize(raw["default"]) if raw.get("default") is not None else None

    def bounds(key: str) -> dict | None:
        b = raw.get(key)
        if b is None:
            return None
        if not isinstance(b, dict) or set(b) - set(KEYS):
            raise GoalError(f"routing_goal.{key} must be an object of weights")
        for k, v in b.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
                raise GoalError(f"routing_goal.{key}.{k} must be a number from 0 to 1")
        return {k: float(v) for k, v in b.items()}

    low, high = bounds("min"), bounds("max")
    lo, hi = _bounds(low, high)
    if sum(lo.values()) > 1 + 1e-9 or sum(hi.values()) < 1 - 1e-9 or any(lo[k] > hi[k] for k in KEYS):
        raise GoalError("routing_goal's min and max leave no allowed mix")
    locked = raw.get("locked", False)
    if not isinstance(locked, bool):
        raise GoalError("routing_goal.locked must be true or false")
    return OrgGoal(default=default, low=low, high=high, locked=locked)


def _bounds(low: dict | None, high: dict | None) -> tuple[dict, dict]:
    return ({k: (low or {}).get(k, 0.0) for k in KEYS}, {k: (high or {}).get(k, 1.0) for k in KEYS})


def _solve(f, a: float, b: float) -> float:
    """The x in [a, b] where the non-decreasing ``f`` reaches 1 (f(a) <= 1 <= f(b))."""
    for _ in range(80):
        mid = (a + b) / 2
        a, b = (mid, b) if f(mid) < 1 else (a, mid)
    return (a + b) / 2


def clamp(weights: dict, org: OrgGoal | None) -> dict:
    """``weights`` projected into the org's allowed region (each weight within
    its floor and ceiling, summing to 1). Proportional where it can be: every
    weight scaled by one factor, each held at the bound it would cross, so a
    weight under its floor is raised to it and the others keep their ratios.
    When scaling can't reach a sum of 1 (the weights that can grow are all at
    their ceilings), every weight is shifted by one amount instead. Either way
    a bound fixed early is released when the sum needs it, so the result is
    always inside the region whenever the region isn't empty."""
    if org is None or (org.low is None and org.high is None):
        return dict(weights)
    lo, hi = _bounds(org.low, org.high)

    def held(x: float, k: str) -> float:
        return min(max(x, lo[k]), hi[k])

    def scaled(t: float) -> float:
        return sum(held(t * weights[k], k) for k in KEYS)

    def shifted(d: float) -> float:
        return sum(held(weights[k] + d, k) for k in KEYS)

    top = max((hi[k] / weights[k] for k in KEYS if weights[k] > 0), default=0.0)
    if scaled(top) >= 1:
        t = _solve(scaled, 0.0, top)
        w = {k: held(t * weights[k], k) for k in KEYS}
    else:
        d = _solve(shifted, -1.0, 1.0)
        w = {k: held(weights[k] + d, k) for k in KEYS}
    return {k: round(w[k], 4) for k in KEYS}


def org_goal(repo_root: str) -> tuple[OrgGoal | None, str | None]:
    """(the org's goal policy, why it can't be read): (None, None) with no org."""
    from brindle.service.routing import RoutingPolicy

    policy = RoutingPolicy.load(repo_root)
    if not policy.applies:
        return None, None
    if policy.access.error:
        return None, policy.access.error
    return policy.access.routing_goal, None


# -- levels ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    """The goal in force: ``weights`` (None: no goal, routing as before),
    the level they came from ("default", "org", "personal", "repo",
    "session" or "task") and whether the org locks it."""
    weights: dict | None
    scope: str
    locked: bool = False


def _stored(raw, where: str) -> dict | None:
    if raw is None:
        return None
    try:
        return normalize(raw)
    except GoalError as e:
        log.warning("brindle: ignoring the routing goal in %s (%s)", where, e)
        return None


def levels(repo_root: str, db=None, root_id: str | None = None) -> dict[str, dict | None]:
    """Each settable level's own goal (None: not set there)."""
    from brindle import config

    out = {"personal": _stored(config.user_settings().get(SETTING), "your settings"),
           "repo": _stored(config._read_json(config.scope_paths(repo_root)["repo"]).get(SETTING),
                           "the repo's .brindle/config.json"),
           "session": None}
    if db is not None and root_id:
        out["session"] = _stored(db.session_routing_goal(root_id), "this session")
    return out


def resolve(repo_root: str, *, db=None, root_id: str | None = None, task=None,
            org: OrgGoal | None = None, org_known: bool = False) -> Resolved:
    """The goal in force for a task in ``repo_root`` (in session ``root_id``,
    with ``task``'s own ``goal=``): the most specific level that sets one,
    then the org's clamp. ``org``/``org_known``: the org's goal policy when
    the caller has it already. A malformed ``task`` raises GoalError."""
    task_w = normalize(task) if task is not None else None
    if not org_known:
        org, _ = org_goal(repo_root)
    balanced = dict(PRESETS["balanced"])
    if org is not None and org.locked:
        return Resolved(clamp(org.default or balanced, org), "org", locked=True)
    have = levels(repo_root, db, root_id)
    have["task"] = task_w
    scope, weights = next(((s, have[s]) for s in ("task", "session", "repo", "personal") if have[s]), ("", None))
    if weights is None:
        if org is not None and (org.default is not None or org.low is not None or org.high is not None):
            # The org asks for a mix (its default, or Balanced within its bounds): that is a goal.
            return Resolved(clamp(org.default or balanced, org), "org")
        return Resolved(None, "default")
    return Resolved(clamp(weights, org), scope)


def for_learner(repo_root: str, *, db=None, root_id: str | None = None, task=None) -> dict | None:
    """The weights to send the learner, or None to send none. Never raises:
    a goal that can't be worked out is no goal (the server applies the org's)."""
    try:
        return resolve(repo_root, db=db, root_id=root_id, task=task).weights
    except Exception:  # noqa: BLE001 - routing must never fail on the goal
        log.info("brindle: routing goal unavailable", exc_info=True)
        return None


def as_tuple(weights: dict | None) -> tuple[float, float, float] | None:
    return None if weights is None else tuple(float(weights[k]) for k in KEYS)


# -- the triangle ------------------------------------------------------------------------------------


def references() -> dict:
    """The shipped reference setups (``brindle/data/reference_setups.json``)."""
    raw = resources.files("brindle").joinpath("data", "reference_setups.json").read_text(encoding="utf-8")
    return json.loads(raw)


def _odds(f: float) -> float:
    f = min(max(f, ODDS_CLIP[0]), ODDS_CLIP[1])
    return f / (1 - f)


def place(setup: dict, yardstick: dict, stretch: float = 2.0) -> dict[str, float]:
    """Where ``setup`` (usd, minutes, first_try) lands on the triangle
    (§6a): its cheaper / sooner / likelier-first-time ratios against the
    ``yardstick``, each raised to ``stretch`` (display only), as barycentric
    shares of the Cheaper, Faster and First try corners."""
    c = (yardstick["usd"] / setup["usd"]) ** stretch
    v = (yardstick["minutes"] / setup["minutes"]) ** stretch
    f = (_odds(setup["first_try"]) / _odds(yardstick["first_try"])) ** stretch
    total = c + v + f
    return {"cost": round(c / total, 4), "speed": round(v / total, 4), "first_try": round(f / total, 4)}


SETUP_FIELDS = ("name", "usd", "minutes", "first_try", "measured", "runs", "note")


# -- cold-start ranking (§1, §4.1) -------------------------------------------------------------------

P_FLOOR = 0.5           # a candidate likelier to fail than merge is never put first by the goal
MIN_USD = 0.01          # a free (local) model still costs something to run and review
GAP_CAP = 5.0           # doublings: no goal swamps the others by more than 32x
YARDSTICK_PROFILE = "developer-fable"     # "one model doing everything", as the references' yardstick
HISTORY = 30            # the person's last tasks whose tier mix the preview uses


def priors() -> dict:
    """The shipped cold-start priors (``brindle/data/routing_priors.json``)."""
    raw = resources.files("brindle").joinpath("data", "routing_priors.json").read_text(encoding="utf-8")
    return json.loads(raw)


def _price(profile: str, repo_root: str):
    from brindle import pricing

    try:
        return pricing.profile_price(profile, repo_root, pricing.repo_overrides(repo_root))
    except Exception:  # noqa: BLE001 - an unknown price is no evidence, not an error
        return None


def estimates(candidates: list[str], tier: str, repo_root: str, pri: dict | None = None) -> dict[str, dict]:
    """Each candidate's prior on ``tier``: ``usd`` and ``minutes`` a task,
    ``first_try`` and ``p`` (it merges at all). A profile's own entry first,
    else its list price times the tier's tokens and the tier's defaults; a $
    brindle can't price is the median of those it can (no evidence either way)."""
    pri = pri or priors()
    t = pri["tiers"][tier]
    mix = pri["token_mix"]
    out = {}
    for c in candidates:
        mine = pri["profiles"].get(c, {})
        own = {**mine.get("*", {}), **mine.get(tier, {})}
        usd = own.get("usd")
        if usd is None and (price := _price(c, repo_root)) is not None:
            usd = t["mtok"] * (mix["input"] * price.input + mix["output"] * price.output)
        out[c] = {"usd": usd, "minutes": own.get("minutes", t["minutes"] * own.get("minutes_factor", 1)),
                  "first_try": own.get("first_try", t["first_try"]), "p": own.get("p", t["p"])}
    known = sorted(e["usd"] for e in out.values() if e["usd"] is not None)
    middle = known[len(known) // 2] if known else 1.0
    for e in out.values():
        e["usd"] = max(middle if e["usd"] is None else e["usd"], MIN_USD)
    return out


def _gap(ratio: float) -> float:
    return min(max(math.log2(ratio), 0.0), GAP_CAP)


def scores(est: dict[str, dict], weights: dict) -> dict[str, float]:
    """§1's score of each candidate (lower is better): the weighted doublings
    of its $ and minutes per success over the best candidate's, and of the
    best first-try odds over its own."""
    usd = {c: e["usd"] / e["p"] for c, e in est.items()}
    mins = {c: e["minutes"] / e["p"] for c, e in est.items()}
    odds = {c: _odds(e["first_try"]) for c, e in est.items()}
    cheapest, soonest, surest = min(usd.values()), min(mins.values()), max(odds.values())
    return {c: (weights["cost"] * _gap(usd[c] / cheapest) + weights["speed"] * _gap(mins[c] / soonest)
                + weights["first_try"] * _gap(surest / odds[c])) for c in est}


def rank(candidates: list[str], tier: str, weights: dict | None, repo_root: str) -> list[str]:
    """``candidates`` (already eligible, in routing's order) reordered for the
    goal by the shipped priors: lowest §1 score first, ties keeping routing's
    order, any candidate under the success floor last. No goal, or nothing to
    choose between: the order as given."""
    from brindle.config import normalize_weight

    tier = normalize_weight(tier)
    pri = priors()
    if not weights or len(candidates) < 2 or tier not in pri["tiers"]:
        return list(candidates)
    est = estimates(candidates, tier, repo_root, pri)
    s = scores(est, weights)
    sure = [c for c in candidates if est[c]["p"] >= P_FLOOR]
    return sorted(sure, key=lambda c: s[c]) + [c for c in candidates if c not in sure]


def tier_mix(db, repo_root: str) -> tuple[dict[str, float], int]:
    """({tier: share}, tasks): the tiers of this repo's last tasks, else the priors' default mix."""
    from brindle.config import WEIGHTS, normalize_weight

    recent = [normalize_weight(d.weight) for d in db.list_routing_decisions(repo_root)[-HISTORY:]
              if d.weight] if db is not None else []
    recent = [t for t in recent if t in WEIGHTS]
    if not recent:
        return dict(priors()["default_mix"]), 0
    return {t: recent.count(t) / len(recent) for t in WEIGHTS if t in recent}, len(recent)


def preview(weights: dict | None, repo_root: str, db=None) -> dict:
    """The triangle's numbers for ``repo_root``. ``aim``: the weights asked
    for (Balanced without a goal). ``roles``: each worker tier's first pick
    under the goal (``profile``) and without it (``previous``); the reviewer
    and supervisor stay as they are until they have evidence of their own
    (§4). ``you``: what those picks cost, take and merge first time on this
    repo's recent mix of tiers, from the shipped priors (``measured`` false:
    not yet measured on your tasks; ``runs``: the tasks the mix is from).
    ``landed``: where ``you`` sits against Fable alone on the same mix."""
    from brindle import autopilot, config

    cfg = config.load_repo_config(repo_root)
    pri = priors()
    mix, runs = tier_mix(db, repo_root)
    roles, you, yard, covered = [], dict.fromkeys(("usd", "minutes", "first_try"), 0.0), \
        dict.fromkeys(("usd", "minutes", "first_try"), 0.0), 0.0
    for t in config.WEIGHTS:
        order = autopilot.tier_order(cfg, t)
        previous = order[0] if order else None
        locked = bool(cfg.routing_locked.get(t))
        pick = previous if (weights is None or locked) else (rank(order, t, weights, repo_root) or [None])[0]
        roles.append({"kind": t, "profile": pick, "previous": previous})
        if pick is None or t not in mix:
            continue
        mine = estimates(order, t, repo_root, pri)[pick]
        fable = estimates(list(dict.fromkeys([YARDSTICK_PROFILE, *order])), t, repo_root, pri)[YARDSTICK_PROFILE]
        for k in you:
            you[k] += mix[t] * mine[k]
            yard[k] += mix[t] * fable[k]
        covered += mix[t]
    reviewer = cfg.review_profile or cfg.reviewer
    supervisor = cfg.supervisor_profile or "supervisor"
    roles += [{"kind": "reviewer", "profile": reviewer, "previous": reviewer},
              {"kind": "supervisor", "profile": supervisor, "previous": supervisor}]
    data = references()
    if covered:
        you = {k: v / covered for k, v in you.items()}
        yard = {k: v / covered for k, v in yard.items()}
        landed = place(you, yard, data.get("stretch", 2))
    else:
        landed = {k: round(1 / 3, 4) for k in KEYS}
    return {"aim": dict(weights or PRESETS["balanced"]),
            "landed": landed,
            "you": {"usd": round(you["usd"], 2), "minutes": round(you["minutes"], 1),
                    "first_try": round(you["first_try"], 3), "measured": False, "runs": runs},
            "references": [{k: s[k] for k in SETUP_FIELDS} for s in data["setups"]],
            "roles": roles}
