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

The triangle (§6a) places a *setup* (an outcome: $ a task, minutes, first try)
against a yardstick, Fable 5.2 alone, at the centre. The reference setups
ship in ``brindle/data/reference_setups.json``.
"""

from __future__ import annotations

import json
import logging
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


def clamp(weights: dict, org: OrgGoal | None) -> dict:
    """``weights`` projected into the org's allowed region: a weight under its
    floor is raised to it (over its ceiling, lowered to it) and the others
    rescaled to keep the sum at 1."""
    if org is None or (org.low is None and org.high is None):
        return dict(weights)
    lo, hi = _bounds(org.low, org.high)
    w = dict(weights)
    fixed: dict[str, float] = {}
    for _ in range(len(KEYS)):
        for k in KEYS:
            if k not in fixed and (w[k] < lo[k] - 1e-9 or w[k] > hi[k] + 1e-9):
                fixed[k] = min(max(w[k], lo[k]), hi[k])
        free = [k for k in KEYS if k not in fixed]
        rest = 1.0 - sum(fixed.values())
        share = sum(w[k] for k in free)
        for k in free:
            w[k] = rest * w[k] / share if share > 0 else rest / len(free)
        w.update(fixed)
        if all(lo[k] - 1e-9 <= w[k] <= hi[k] + 1e-9 for k in KEYS):
            break
    return {k: round(max(0.0, w[k]), 4) for k in KEYS}


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
    if org is not None and org.locked:
        return Resolved(org.default or dict(PRESETS["balanced"]), "org", locked=True)
    have = levels(repo_root, db, root_id)
    have["task"] = task_w
    scope, weights = next(((s, have[s]) for s in ("task", "session", "repo", "personal") if have[s]), ("", None))
    if weights is None:
        if org is not None and org.default is not None:
            return Resolved(clamp(org.default, org), "org")
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


def preview(weights: dict | None, roles: list[dict]) -> dict:
    """The triangle's numbers: ``aim`` (the weights asked for, Balanced
    without a goal), ``landed`` (where what routing delivered lands),
    ``you`` (what it delivered), the ``references`` and ``roles``. Until
    hosted preview estimates exist, ``you`` is the benchmark of brindle's
    default mix (``measured`` false, ``runs`` 0: not your own tasks)."""
    data = references()
    setups = data["setups"]
    yard = next(s for s in setups if s["name"] == data["yardstick"])
    mix = next(s for s in setups if s.get("weights"))
    you = {"usd": mix["usd"], "minutes": mix["minutes"], "first_try": mix["first_try"],
           "measured": False, "runs": 0}
    return {"aim": dict(weights or PRESETS["balanced"]),
            "landed": place(you, yard, data.get("stretch", 2)),
            "you": you,
            "references": [{k: s[k] for k in SETUP_FIELDS} for s in setups],
            "roles": roles}
