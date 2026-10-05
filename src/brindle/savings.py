"""What hosted learning's picks gained, from this machine's records only.

Every delegation leaves a row in ``routing_decisions`` (``record``): the
profile brindle would have used without learning (the baseline), the profile it
used, and whether learning made the pick. The pipeline adds how the task went
(``note_outcome``): review rounds, escalations, merged or removed unmerged.

``report`` turns those rows into what ``brindle account savings`` prints, per
calendar month. The cost figure is an estimate and is always labelled as one:
for each finished task learning sent to a different profile, the tokens it
used (from ``brindle.history``) weighted by its profile's relative cost, against
what the baseline profile would likely have cost -- this repo's own average
for that profile and weight when there are at least ``MIN_BASELINE`` such
tasks, else the same tokens at the baseline's relative cost. Relative cost,
not money. With fewer than ``MIN_TASKS`` such tasks it says there isn't
enough data yet instead.

Nothing here is sent anywhere, and nothing here may fail a delegation,
review or merge: ``record`` and ``note_outcome`` swallow their errors.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from brindle.db import DB, RoutingDecision
from brindle.history import tokens_total

log = logging.getLogger(__name__)

MIN_TASKS = 5       # finished tasks learning rerouted, before an estimate is shown
MIN_BASELINE = 3    # tasks on the baseline profile at a weight, before their average is used
FAILED = "removed_unmerged"
ESTIMATE_NOTE = ("Estimates: each task learning sent to a different profile, against what the "
                 "baseline profile would likely have cost (this repo's average for it at that "
                 "weight when there is one, else the profiles' relative cost). "
                 "Relative cost, not money.")


# -- recording ------------------------------------------------------------------------------------


def record(db: DB, repo_root: str, decision: dict | None, *, task_id: str | None = None,
           agent_id: str | None = None) -> None:
    """Keep ``decision`` (as ``autopilot.choose_profile`` filled it) for a
    task that was just started or queued. Never raises."""
    try:
        if not decision or not decision.get("profile"):
            return
        db.add_routing_decision(
            repo_root, task_id=task_id, agent_id=agent_id, weight=decision.get("weight"),
            baseline_profile=decision.get("baseline") or decision["profile"],
            profile=decision["profile"], learned=bool(decision.get("learned")),
            prior=decision.get("prior") is True,
        )
    except Exception:
        log.exception("brindle: couldn't record the routing decision")


def attach_agent(db: DB, task_id: str, agent_id: str) -> None:
    """A queued task started: its decision now has a worker. Never raises."""
    try:
        db.set_routing_agent(task_id, agent_id)
    except Exception:
        log.exception("brindle: couldn't update the routing decision")


def note_outcome(db: DB, worker, *, approved: bool | None = None, escalated: bool = False,
                 merged: bool | None = None, **_ignored) -> None:
    """One event in ``worker``'s task (the same events ``learning.note``
    hears), counted on its decision. Never raises."""
    try:
        if worker is None:
            return
        outcome = None if merged is None else ("merged" if merged else FAILED)
        db.note_routing_outcome(worker.id, review=approved is not None, escalated=bool(escalated),
                                outcome=outcome)
    except Exception:
        log.exception("brindle: couldn't record the routing outcome")


# -- the estimate ----------------------------------------------------------------------------------


@dataclass
class Picks:
    """The tasks of one period that learning picked, or that it didn't."""
    tasks: int = 0
    review_rounds: int = 0
    troubled: int = 0      # removed unmerged, or escalated to the supervisor
    prior: int = 0         # picks whose evidence included other orgs' shared results


@dataclass
class Period:
    label: str
    learned: Picks = field(default_factory=Picks)
    baseline: Picks = field(default_factory=Picks)
    compared: int = 0              # finished tasks learning rerouted, with usage on record
    actual_cost: float = 0.0       # tokens x relative cost of the profile used
    baseline_cost: float = 0.0     # the same for the baseline profile, estimated
    from_average: int = 0          # of ``compared``, how many used the repo's own average

    @property
    def enough(self) -> bool:
        return self.compared >= MIN_TASKS and self.baseline_cost > 0

    @property
    def saved_fraction(self) -> float | None:
        """The estimated share of cost saved (negative: it cost more), or
        None when there's too little to say."""
        if not self.enough:
            return None
        return (self.baseline_cost - self.actual_cost) / self.baseline_cost


def _default_cost(repo_root: str) -> Callable[[str], int]:
    """Relative cost per token: cost_rank + 1, so a local model (rank 0)
    counts as the cheapest rather than free, and never reads as 100% saved."""
    from brindle.pro.learning import cost_rank

    return lambda name: cost_rank(name, repo_root) + 1


def _month_start(now: float, back: int = 0) -> float:
    t = time.localtime(now)
    month = t.tm_year * 12 + t.tm_mon - 1 - back
    return time.mktime((month // 12, month % 12 + 1, 1, 0, 0, 0, 0, 0, -1))


def _tokens(db: DB, repo_root: str, rows: list[RoutingDecision]) -> dict[str, int]:
    """Tokens each worker used: the sum of its history rows (each holds only
    what's new since that agent's previous row)."""
    ids = sorted({r.agent_id for r in rows if r.agent_id})
    return {agent: sum(tokens_total(t) for t in tokens)
            for agent, tokens in db.history_tokens(repo_root, ids).items()}


def _period(label: str, rows: list[RoutingDecision], tokens: dict[str, int],
            averages: dict[tuple[str, str | None], float], cost: Callable[[str], int]) -> Period:
    p = Period(label)
    for r in rows:
        rerouted = bool(r.learned) and r.profile != r.baseline_profile
        picks = p.learned if rerouted else p.baseline
        picks.tasks += 1
        picks.prior += int(rerouted and bool(r.prior))
        picks.review_rounds += r.review_rounds
        picks.troubled += int(r.outcome == FAILED or r.escalations > 0)
        used = tokens.get(r.agent_id or "", 0)
        if not rerouted or not r.outcome or used <= 0:
            continue
        average = averages.get((r.baseline_profile, r.weight))
        p.compared += 1
        p.from_average += int(average is not None)
        p.actual_cost += used * cost(r.profile)
        p.baseline_cost += (average if average is not None else used) * cost(r.baseline_profile)
    return p


def report(db: DB, repo_root: str, now: float | None = None,
           cost: Callable[[str], int] | None = None) -> list[Period]:
    """This month, last month and all time for ``repo_root``."""
    now = time.time() if now is None else now
    cost = cost or _default_cost(repo_root)
    rows = db.list_routing_decisions(repo_root)
    tokens = _tokens(db, repo_root, rows)
    # what a task on a profile at a weight used here, where enough of them finished
    samples: dict[tuple[str, str | None], list[int]] = {}
    for r in rows:
        used = tokens.get(r.agent_id or "", 0)
        if r.outcome and used > 0:
            samples.setdefault((r.profile, r.weight), []).append(used)
    averages = {k: sum(v) / len(v) for k, v in samples.items() if len(v) >= MIN_BASELINE}
    this, last = _month_start(now), _month_start(now, 1)
    month = lambda ts: time.strftime("%Y-%m", time.localtime(ts))  # noqa: E731
    return [
        _period(f"This month ({month(this)})", [r for r in rows if r.ts >= this],
                tokens, averages, cost),
        _period(f"Last month ({month(last)})", [r for r in rows if last <= r.ts < this],
                tokens, averages, cost),
        _period("All time", rows, tokens, averages, cost),
    ]


# -- showing it ------------------------------------------------------------------------------------


def _percent(fraction: float) -> str:
    pct = round(abs(fraction) * 100)
    if pct == 0:
        return "about the same cost"
    return f"~{pct}% {'lower' if fraction > 0 else 'higher'} cost"


def _estimate(p: Period) -> str:
    if p.saved_fraction is None:
        return (f"not enough data yet ({p.compared} of the {MIN_TASKS} finished tasks "
                "picked by learning that an estimate needs)")
    return (f"estimated {_percent(p.saved_fraction)} than the baseline profiles, over "
            f"{p.compared} tasks picked by learning")


def _per_task(picks: Picks) -> str:
    return f"{picks.review_rounds / picks.tasks:.1f}" if picks.tasks else "-"


def describe(periods: list[Period], repo_root: str) -> str:
    """What ``brindle account savings`` prints."""
    name = os.path.basename(repo_root.rstrip(os.sep)) or repo_root
    lines = [f"Hosted learning's picks in {name}, from this machine's records "
             "(nothing here is sent anywhere)."]
    if not periods or not periods[-1].learned.tasks + periods[-1].baseline.tasks:
        lines.append("Not enough data yet: no delegated tasks are on record for this repo.")
        return "\n".join(lines)
    for p in periods:
        lines.append("")
        lines.append(p.label)
        if not p.learned.tasks + p.baseline.tasks:
            lines.append("  no delegated tasks")
            continue
        shared = f" ({p.learned.prior} helped by your company's other orgs)" if p.learned.prior else ""
        lines.append(f"  picks                {p.learned.tasks} by learning{shared} · "
                     f"{p.baseline.tasks} baseline")
        lines.append(f"  review rounds/task   {_per_task(p.learned)} learning · "
                     f"{_per_task(p.baseline)} baseline")
        lines.append(f"  failed or escalated  {p.learned.troubled} of {p.learned.tasks} learning · "
                     f"{p.baseline.troubled} of {p.baseline.tasks} baseline")
        lines.append(f"  cost                 {_estimate(p)}")
    lines.append("")
    lines.append(ESTIMATE_NOTE)
    return "\n".join(lines)


def summary_line(periods: list[Period]) -> str:
    """The one line bare ``brindle account`` shows while learning is active."""
    for p, when in zip(periods, ("this month", "last month", "so far")):
        if p.saved_fraction is not None:
            return (f"Learning {when}: {p.learned.tasks} picks, estimated "
                    f"{_percent(p.saved_fraction)} on them (an estimate; `brindle account savings`).")
    picks = periods[-1].learned.tasks if periods else 0
    return (f"Learning: {picks} picks so far, not enough data yet for a savings estimate "
            "(`brindle account savings`).")


__all__ = ["ESTIMATE_NOTE", "MIN_BASELINE", "MIN_TASKS", "Period", "Picks", "attach_agent",
           "describe", "note_outcome", "record", "report", "summary_line"]
