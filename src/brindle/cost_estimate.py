"""What a task, or a whole goal, is likely to cost before it runs (brindle
Pro, feature ``cost``).

Built from this repo's history rows (see :mod:`brindle.history`): each row
holds only the tokens its agent used since its previous row, so summing an
agent's rows gives what that one run used. Runs are grouped by role (a
worker run or a review run), profile and weight, and each group's median and
80th percentile token counts are priced through ``price`` (the
:mod:`brindle.pricing` signature, injectable for tests).

An estimate is always a range: median to p80. With too few runs on record it
says "low confidence" and falls back to a coarse prior, widened. The cheaper
alternative swaps the reviewer for the cheapest one available here (a local
model when one is served).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable

from brindle.config import WEIGHTS
from brindle.db import DB
from brindle.usage import format_tokens

FEATURE = "cost"
MIN_RUNS = 5          # runs in a group before its percentiles are trusted
HISTORY_ROWS = 5000   # history.CAP_PER_REPO

# price(model, input, output, cache_write, cache_read) -> dollars, or None if unknown.
PriceFn = Callable[[str | None, int, int, int, int], "float | None"]


@dataclass(frozen=True)
class Tokens:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_write + self.cache_read

    def __add__(self, other: Tokens) -> Tokens:
        return Tokens(self.input + other.input, self.output + other.output,
                      self.cache_write + other.cache_write, self.cache_read + other.cache_read)

    def scaled(self, k: float) -> Tokens:
        return Tokens(int(self.input * k), int(self.output * k), int(self.cache_write * k),
                      int(self.cache_read * k))


# A coarse prior for when history is thin: (median, p80) per role and weight.
# Deliberately rough; it's shown widened and labelled low confidence.
PRIOR: dict[tuple[str, str], tuple[Tokens, Tokens]] = {
    ("worker", "light"): (Tokens(30_000, 15_000, 150_000, 1_500_000),
                          Tokens(60_000, 30_000, 300_000, 3_000_000)),
    ("worker", "medium"): (Tokens(60_000, 40_000, 400_000, 5_000_000),
                           Tokens(120_000, 80_000, 800_000, 10_000_000)),
    ("worker", "heavy"): (Tokens(120_000, 90_000, 900_000, 12_000_000),
                          Tokens(250_000, 180_000, 1_800_000, 25_000_000)),
    ("review", "light"): (Tokens(15_000, 6_000, 80_000, 600_000),
                          Tokens(30_000, 12_000, 160_000, 1_200_000)),
    ("review", "medium"): (Tokens(20_000, 10_000, 100_000, 1_000_000),
                           Tokens(40_000, 20_000, 200_000, 2_000_000)),
    ("review", "heavy"): (Tokens(40_000, 20_000, 200_000, 2_500_000),
                          Tokens(80_000, 40_000, 400_000, 5_000_000)),
}
PRIOR_LOW, PRIOR_HIGH = 0.5, 1.5   # how far a prior's range is widened


@dataclass
class Run:
    role: str              # "worker" or "review"
    profile: str | None
    weight: str | None
    tokens: Tokens
    model: str | None = None


@dataclass
class Dist:
    """A group's token distribution: median and p80, per component."""
    n: int
    p50: Tokens
    p80: Tokens
    model: str | None = None


@dataclass
class Part:
    """The estimate for one run (a worker's or a reviewer's)."""
    role: str
    profile: str
    weight: str
    low: float | None      # dollars at the median, None when it can't be priced
    high: float | None     # dollars at p80
    low_tokens: int
    high_tokens: int
    runs: int              # runs of history behind it
    confident: bool


@dataclass
class Estimate:
    parts: list[Part] = field(default_factory=list)
    tasks: int = 0

    @property
    def confident(self) -> bool:
        return all(p.confident for p in self.parts)

    @property
    def priced(self) -> bool:
        return all(p.low is not None and p.high is not None for p in self.parts)

    @property
    def low(self) -> float:
        return sum(p.low or 0.0 for p in self.parts)

    @property
    def high(self) -> float:
        return sum(p.high or 0.0 for p in self.parts)

    @property
    def low_tokens(self) -> int:
        return sum(p.low_tokens for p in self.parts)

    @property
    def high_tokens(self) -> int:
        return sum(p.high_tokens for p in self.parts)

    @property
    def runs(self) -> int:
        return min((p.runs for p in self.parts), default=0)

    def range_text(self) -> str:
        """Always a range, never a single number."""
        if self.priced:
            low, high = self.low, self.high
            if high <= low:
                high = low * 1.25 if low else 0.01
            return f"{_dollars(low)}–{_dollars(high)}"
        low, high = self.low_tokens, max(self.high_tokens, self.low_tokens + 1)
        return f"{format_tokens(low)}–{format_tokens(high)} tokens (no price for some models)"


@dataclass(frozen=True)
class TaskSpec:
    profile: str
    weight: str | None = None


# -- history --------------------------------------------------------------------------------------


def _tokens(raw: str | None) -> tuple[Tokens, str | None] | None:
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    t = Tokens(int(d.get("input") or 0), int(d.get("output") or 0),
               int(d.get("cache_creation") or 0), int(d.get("cache_read") or 0))
    return t, (d.get("model") or None)


def runs(db: DB, repo_root: str) -> list[Run]:
    """One Run per worker or reviewer on record: the sum of its history rows'
    token deltas. Its weight comes from the task it ran (a reviewer's from
    the task of the branch it reviewed), when that was recorded."""
    rows = db.list_history(repo_root, None, HISTORY_ROWS)
    weight_by_agent: dict[str, str] = {}
    weight_by_branch: dict[str, str] = {}
    for t in db.list_tasks(repo_root):
        if t.weight in WEIGHTS:
            if t.agent_id:
                weight_by_agent[t.agent_id] = t.weight
            if t.branch:
                weight_by_branch[t.branch] = t.weight
    for d in db.list_routing_decisions(repo_root):
        if d.agent_id and d.weight in WEIGHTS:
            weight_by_agent.setdefault(d.agent_id, d.weight)

    totals: dict[str, Tokens] = defaultdict(Tokens)
    kinds: dict[str, set[str]] = defaultdict(set)
    profiles: dict[str, str | None] = {}
    branches: dict[str, str | None] = {}
    models: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if not r.agent_id or r.kind not in ("worker_result", "review"):
            continue
        kinds[r.agent_id].add(r.kind)
        profiles.setdefault(r.agent_id, r.profile)
        branches.setdefault(r.agent_id, r.branch)
        parsed = _tokens(r.tokens)
        if parsed:
            totals[r.agent_id] = totals[r.agent_id] + parsed[0]
            if parsed[1]:
                models[r.agent_id][parsed[1]] += 1
    out = []
    for agent_id, t in totals.items():
        if t.total <= 0:
            continue
        role = "review" if "review" in kinds[agent_id] else "worker"
        weight = weight_by_agent.get(agent_id)
        if weight is None and role == "review" and branches.get(agent_id):
            weight = weight_by_branch.get(branches[agent_id])
        model = models[agent_id].most_common(1)[0][0] if models[agent_id] else None
        out.append(Run(role, profiles.get(agent_id), weight, t, model))
    return out


def _percentile(values: list[int], q: float) -> int:
    s = sorted(values)
    if not s:
        return 0
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return int(round(s[lo] + (s[hi] - s[lo]) * (pos - lo)))


def distribution(group: list[Run]) -> Dist:
    def at(q: float) -> Tokens:
        return Tokens(_percentile([r.tokens.input for r in group], q),
                      _percentile([r.tokens.output for r in group], q),
                      _percentile([r.tokens.cache_write for r in group], q),
                      _percentile([r.tokens.cache_read for r in group], q))

    models = Counter(r.model for r in group if r.model)
    return Dist(len(group), at(0.5), at(0.8), models.most_common(1)[0][0] if models else None)


def distributions(all_runs: list[Run]) -> dict[tuple[str, str | None, str | None], Dist]:
    """Per (role, profile, weight) distributions, plus the pooled
    (role, profile, None) for each profile across weights."""
    groups: dict[tuple, list[Run]] = defaultdict(list)
    for r in all_runs:
        groups[(r.role, r.profile, r.weight)].append(r)
        if r.weight is not None:
            groups[(r.role, r.profile, None)].append(r)
    return {k: distribution(v) for k, v in groups.items()}


# -- pricing --------------------------------------------------------------------------------------


def default_price() -> PriceFn:
    """brindle.pricing's ``price``, or one that knows no prices."""
    try:
        from brindle.pricing import price
    except Exception:  # noqa: BLE001 - not there yet: tokens only
        return lambda model, i, o, cw, cr: None
    return price


def _profile(name: str, repo_root: str | None):
    from brindle.profiles import load_profile

    try:
        return load_profile(name, repo_root)
    except Exception:  # noqa: BLE001 - a profile since removed
        return None


def is_local(name: str, repo_root: str | None) -> bool:
    p = _profile(name, repo_root)
    if p is None:
        return False
    return bool(p.local or (p.base_url and any(h in p.base_url for h in ("localhost", "127.0.0.1"))))


def _cost(price: PriceFn, model: str | None, t: Tokens, local: bool) -> float | None:
    if local:
        return 0.0
    try:
        value = price(model, t.input, t.output, t.cache_write, t.cache_read)
    except Exception:  # noqa: BLE001 - an unpriceable model
        return None
    return None if value is None else float(value)


def part(dists: dict, role: str, profile: str, weight: str | None, price: PriceFn,
         repo_root: str | None) -> Part:
    """The estimate for one run of ``profile`` in ``role`` at ``weight``."""
    exact = dists.get((role, profile, weight))
    pooled = dists.get((role, profile, None))
    d = exact if exact and exact.n >= MIN_RUNS else (
        pooled if pooled and pooled.n >= MIN_RUNS else None)
    tier = weight if weight in WEIGHTS else "medium"
    p = _profile(profile, repo_root)
    local = is_local(profile, repo_root)
    if d is not None:
        model = d.model or (p.model if p else None)
        low_t, high_t, n, confident = d.p50, d.p80, d.n, True
    else:
        seen = max((x.n for x in (exact, pooled) if x), default=0)
        p50, p80 = PRIOR[(role, tier)]
        model = (p.model if p else None) or ((exact or pooled).model if (exact or pooled) else None)
        low_t, high_t, n, confident = p50.scaled(PRIOR_LOW), p80.scaled(PRIOR_HIGH), seen, False
    return Part(role, profile, tier, _cost(price, model, low_t, local),
                _cost(price, model, high_t, local), low_t.total, high_t.total, n, confident)


# -- estimates ------------------------------------------------------------------------------------


def estimate(tasks: list[TaskSpec], reviewer: str, *, dists: dict, price: PriceFn,
             repo_root: str | None = None) -> Estimate:
    """Each task is one worker run plus one review run; the goal is their sum."""
    est = Estimate(tasks=len(tasks))
    for t in tasks:
        est.parts.append(part(dists, "worker", t.profile, t.weight, price, repo_root))
        est.parts.append(part(dists, "review", reviewer, t.weight, price, repo_root))
    return est


def available_reviewers(repo_root: str | None) -> list[str]:
    """Reviewer profiles that could run here now."""
    from brindle import agents
    from brindle.profiles import list_profiles
    from brindle.providers import unusable

    out = []
    for p in list_profiles(repo_root):
        if not p.name.startswith("reviewer"):
            continue
        if p.provider == "native":
            if p.name == "reviewer-local" and not agents._local_reviewer_available():
                continue
        elif p.provider in ("claude", "codex", "antigravity"):
            try:
                if unusable(p.provider, p.env):
                    continue
            except Exception:  # noqa: BLE001
                continue
        out.append(p.name)
    return out


def cheaper(tasks: list[TaskSpec], reviewer: str, candidates: list[str], *, dists: dict,
            price: PriceFn, repo_root: str | None = None,
            base: Estimate | None = None) -> tuple[str, Estimate] | None:
    """The same goal with the reviewer swapped for the cheapest available
    one (a local model first), or None if none is cheaper."""
    base = base or estimate(tasks, reviewer, dists=dists, price=price, repo_root=repo_root)
    others = [c for c in dict.fromkeys(candidates) if c != reviewer]
    if not others:
        return None
    local = [c for c in others if is_local(c, repo_root)]
    options = [(c, estimate(tasks, c, dists=dists, price=price, repo_root=repo_root))
               for c in (local[:1] or others)]
    priced = [(c, e) for c, e in options if e.priced]
    if base.priced:
        priced = [(c, e) for c, e in priced if e.high < base.high]
        if not priced:
            return None
        return min(priced, key=lambda ce: (ce[1].high, ce[1].low))
    return options[0] if local else None


def describe(tasks: list[TaskSpec], reviewer: str, *, repo_root: str | None, db: DB | None = None,
             price: PriceFn | None = None, candidates: list[str] | None = None,
             all_runs: list[Run] | None = None) -> str:
    """The estimate as a few lines for a reply or the CLI."""
    if not tasks:
        return "Cost estimate: nothing to estimate."
    price = price or default_price()
    if all_runs is None:
        all_runs = runs(db, repo_root) if db is not None and repo_root else []
    dists = distributions(all_runs)
    est = estimate(tasks, reviewer, dists=dists, price=price, repo_root=repo_root)
    what = f"{len(tasks)} task{'s' if len(tasks) != 1 else ''}, each a worker run and a review"
    if est.confident:
        lines = [f"Cost estimate: {est.range_text()} ({what}; median to p80 of at least "
                 f"{est.runs} past runs per profile in this repo)."]
    else:
        lines = [f"Cost estimate (low confidence: too little history here, so a coarse prior): "
                 f"{est.range_text()} ({what})."]
    if candidates is None:
        try:
            candidates = available_reviewers(repo_root)
        except Exception:  # noqa: BLE001 - no alternative then
            candidates = []
    alt = cheaper(tasks, reviewer, candidates, dists=dists, price=price, repo_root=repo_root,
                  base=est)
    if alt:
        name, e = alt
        lines.append(f"Cheaper: review with {name} instead of {reviewer}: {e.range_text()}.")
    return "\n".join(lines)


def _dollars(x: float) -> str:
    return f"${x:,.2f}" if x < 100 else f"${x:,.0f}"


# -- gate -----------------------------------------------------------------------------------------


def entitled() -> bool:
    from brindle.pro import license

    try:
        return license.has(FEATURE)
    except Exception:  # noqa: BLE001 - an unreadable license is "not entitled"
        return False


def reply_note(db: DB, repo_root: str, tasks: list[TaskSpec], reviewer: str | None = None) -> str | None:
    """The estimate to add to an MCP reply, or None (not entitled, or any
    failure: an estimate must never fail the call it rides along with)."""
    if not entitled():
        return None
    try:
        from brindle.config import load_repo_config

        reviewer = reviewer or load_repo_config(repo_root).reviewer
        return describe(tasks, reviewer, repo_root=repo_root, db=db)
    except Exception:  # noqa: BLE001
        return None
