"""Dollar budgets (brindle Pro, the ``cost`` feature; fails closed).

``budget: {task_usd, goal_usd, month_usd, stop}`` in the config (see
``config.load_repo_config``). Without the entitlement, or with no budget set,
nothing here does anything.

A Team org's budget (``org_budgets``: ``seat_month_usd`` becomes ``month_usd``,
``goal_usd``) is merged in for its members, whatever the repo says: a repo may
tighten it, never loosen it (``limits`` takes the smaller of the two).

Routing (``autopilot.choose_profile``): a candidate whose estimated cost
(``estimate``) would put the task, its goal or the month over a limit is
skipped, so the task goes to the next cheaper candidate for its weight; when
none fits the assign is refused and autopilot asks the user. Running workers
(``sweep``, from the cull pass): the supervisor is warned at ``WARN_AT`` of
``task_usd`` and, only with ``stop: true``, the worker is closed at 100%.

Estimates are rough: this repo's median spend for a finished task on the
profile (``MIN_SAMPLES`` or more of them), else a typical token mix at the
profile's list price. A profile with no known price is never skipped.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass

from brindle import pricing
from brindle.db import DB

log = logging.getLogger(__name__)

WARN_AT = 0.8
MIN_SAMPLES = 3
KEYS = ("task_usd", "goal_usd", "month_usd")
# input, output, cache read tokens of a typical task, when there's no history to go on
TYPICAL = {"input_tokens": 200_000, "output_tokens": 40_000, "cache_read_tokens": 600_000}


@dataclass
class Limits:
    task_usd: float | None = None
    goal_usd: float | None = None
    month_usd: float | None = None
    stop: bool = False      # close a worker that reaches task_usd (default: only warn)
    seat_spent_usd: float | None = None   # this month's spend as the org counts it (org budgets)
    org: bool = False       # an org budget is part of these limits: it can't be skipped for want of a price
    hide_dollars: bool = False   # the org hides this person's org spend and limits: words, not dollars

    def any(self) -> bool:
        return any(getattr(self, k) is not None for k in KEYS)


AT_LIMIT, CLOSE, WITHIN = "at limit", "getting close", "within limit"


def status_word(spent: float, limit: float | None) -> str:
    """"within limit", "getting close" (at ``WARN_AT`` of ``limit`` or more) or
    "at limit" (what is shown instead of org dollars when the org hides them)."""
    if limit is None:
        return WITHIN
    if limit <= 0 or spent >= limit:
        return AT_LIMIT
    return CLOSE if spent >= limit * WARN_AT else WITHIN


def parse(raw: object) -> Limits:
    """``Limits`` from a config value; anything malformed or non-positive is ignored."""
    out = Limits()
    if not isinstance(raw, dict):
        return out
    for key in KEYS:
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            setattr(out, key, float(v))
    out.stop = raw.get("stop") is True
    return out


def entitled() -> bool:
    try:
        from brindle import cost

        cost.require_entitled()
        return True
    except Exception:  # noqa: BLE001 - fail closed
        return False


def _tighter(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else min(a, b)


def org_limits(repo_root: str | None) -> Limits | None:
    """The org's budget for this member (Team ``org_budgets``): ``month_usd``
    is their per-seat monthly limit, ``goal_usd`` the per-goal one, and
    ``seat_spent_usd`` what the org counts they have spent this month. None
    without the feature or an org. A policy that can't be had raises
    ``OrgBudgetUnavailable``: an org budget can't be skipped for being unreadable."""
    if not repo_root:
        return None
    from brindle.pro import team_policy

    try:
        p = team_policy.org_budgets(repo_root)
    except Exception as e:  # noqa: BLE001 - fail closed
        raise OrgBudgetUnavailable(str(e)) from e
    if p is None:
        return None
    if not isinstance(p, team_policy.OrgPolicy):
        raise OrgBudgetUnavailable(p.reason)
    p = p.enforced
    spent = p.spend_seat_usd
    if spent is not None and p.spend_month != time.strftime("%Y-%m", time.gmtime()):
        spent = None                      # last month's number
    lim = Limits(task_usd=p.budget_task_usd, goal_usd=p.budget_goal_usd,
                 month_usd=p.budget_seat_month_usd, seat_spent_usd=spent,
                 hide_dollars=p.visibility.get("own_dollars") is False)
    return lim if lim.any() else None


class OrgBudgetUnavailable(Exception):
    pass


def limits(cfg, repo_root: str | None = None) -> Limits | None:
    """The budget in force for ``cfg``: the repo's (needs the ``cost`` feature)
    and, for an org member with ``org_budgets``, the org's. A repo may tighten
    the org's limits but never loosen them: each limit is the smaller of the
    two. None when there is none. An org budget that can't be read is
    ``month_usd = 0``: nothing fits until it can."""
    lim = parse(getattr(cfg, "budget", None))
    if not entitled():
        lim = Limits()
    try:
        org = org_limits(repo_root)
    except OrgBudgetUnavailable as e:
        log.warning("brindle: the org budget can't be read (%s); treating it as exhausted", e)
        org = Limits(month_usd=0.0, goal_usd=0.0)
    if org is not None:
        lim.org = True
        lim.task_usd = _tighter(lim.task_usd, org.task_usd)
        lim.goal_usd = _tighter(lim.goal_usd, org.goal_usd)
        lim.month_usd = _tighter(lim.month_usd, org.month_usd)
        lim.seat_spent_usd = org.seat_spent_usd
        lim.hide_dollars = org.hide_dollars
    if lim.month_usd is not None:
        lim.month_usd += _approved_month()      # an admin's cost-center approval (Enterprise)
    return lim if lim.any() else None


def _approved_month() -> float:
    try:
        from brindle.pro import cost_centers

        return cost_centers.month_raise()
    except Exception:  # noqa: BLE001 - fail closed: no raise
        return 0.0


def _month_start(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def typical(price: pricing.Price) -> float:
    return price.cost(TYPICAL["input_tokens"], TYPICAL["output_tokens"], 0, TYPICAL["cache_read_tokens"])


def price_tokens(price: pricing.Price, tokens: int) -> float:
    """What ``tokens`` cost at ``price`` in a typical mix (``TYPICAL``)."""
    return typical(price) * tokens / sum(TYPICAL.values())


def history_spend(db: DB, repo_root: str, agent_id: str, extra=None, fallback=None) -> float | None:
    """What ``agent_id`` has spent per its history rows, or None if nothing is priced."""
    from brindle import savings

    rows = db.history_tokens(repo_root, [agent_id]).get(agent_id, [])
    if not rows:
        return None
    return savings._spend(rows, None, fallback, extra or {})


def estimate(db: DB, repo_root: str, profile: str, weight: str | None, extra=None) -> float | None:
    """Likely dollars for one task on ``profile``, or None when its price is unknown."""
    from brindle import savings

    price = pricing.profile_price(profile, repo_root, extra)
    if price is None:
        return None
    spent = []
    for r in db.list_routing_decisions(repo_root):
        if r.profile == profile and r.outcome and r.agent_id and (weight is None or r.weight == weight):
            s = history_spend(db, repo_root, r.agent_id, extra, price)
            if s is not None:
                spent.append(s)
    if len(spent) >= MIN_SAMPLES:
        return statistics.median(spent)
    return typical(price)


class Gate:
    """Says whether a profile fits the budget for one delegation."""

    def __init__(self, db: DB, cfg, repo_root: str, caller_id: str | None, now: float | None = None):
        self.db, self.repo_root, self.lim = db, repo_root, limits(cfg, repo_root)
        self.caller_id = caller_id
        self.now = time.time() if now is None else now
        self.extra = pricing.repo_overrides(repo_root)
        self._month: float | None = None
        self._goal: float | None = None
        self._goal_failed = False

    @property
    def active(self) -> bool:
        return self.lim is not None

    def month_spend(self) -> float:
        if self._month is None:
            from brindle import cost

            self._month = sum(p.dollars or 0.0 for p in
                              cost.priced_rows(self.db, self.repo_root, _month_start(self.now)))
            if self.lim is not None and self.lim.seat_spent_usd is not None:
                # the org also counts what this seat spent in other repos and on other machines
                self._month = max(self._month, self.lim.seat_spent_usd)
        return self._month

    def goal_text(self) -> str | None:
        """The first line of the caller's autopilot goal, or None."""
        from brindle import autopilot

        try:
            root = autopilot.root_of(self.db, self.caller_id) if self.caller_id else None
            ap = self.db.get_autopilot(root) if root else None
            return ap.goal.strip().splitlines()[0] if ap and ap.goal and ap.goal.strip() else None
        except Exception:  # noqa: BLE001
            return None

    def goal_limit(self) -> float | None:
        """The goal budget, with any approved raise for this goal."""
        if self.lim is None or self.lim.goal_usd is None:
            return None
        try:
            from brindle.pro import cost_centers

            return self.lim.goal_usd + cost_centers.goal_raise(self.goal_text())
        except Exception:  # noqa: BLE001
            return self.lim.goal_usd

    def goal_spend(self) -> float | None:
        """The goal's spend so far, or None when it can't be totalled (a goal
        budget can't be met by spend nobody could count)."""
        if self._goal is None:
            from brindle import cost

            total = 0.0
            try:
                goal = self.goal_text()
                if goal:
                    cache: dict = {}
                    for p in cost.priced_rows(self.db, self.repo_root, 0):
                        if p.dollars and cost._goal_of(self.db, p.row.agent_id, cache) == goal:
                            total += p.dollars
                self._goal = total
            except Exception:  # noqa: BLE001
                log.exception("brindle: couldn't total the goal's spend")
                self._goal_failed = True
        return None if self._goal_failed else self._goal

    def why_not(self, profile: str, weight: str | None) -> str | None:
        """Why ``profile`` doesn't fit the budget, or None when it does."""
        if self.lim is None:
            return None
        est = estimate(self.db, self.repo_root, profile, weight, self.extra)
        lim = self.lim
        if est is None:
            if lim.org:      # an org's budget isn't skipped because a profile has no known price
                return f"{profile} has no known price, so it can't be shown to fit the org's budget"
            return None
        m = pricing.money
        hide = lim.hide_dollars      # the org hides its dollars from this person: say it in words
        if lim.task_usd is not None and est > lim.task_usd:
            if hide:
                return f"a {profile} task would go over the task budget (at limit)"
            return f"a {profile} task costs about {m(est)}, over the {m(lim.task_usd)} task budget"
        if lim.goal_usd is not None:
            spent, cap = self.goal_spend(), self.goal_limit()
            if spent is None:
                return f"the goal's spend can't be totalled, so {profile} can't be shown to fit its budget"
            if spent + est > cap:
                if hide:
                    return f"{profile} would take the goal over its budget (at limit)"
                return (f"{profile} at about {m(est)} would take the goal to {m(spent + est)}, "
                        f"over its {m(cap)} budget")
        if lim.month_usd is not None:
            spent = self.month_spend()
            if spent + est > lim.month_usd:
                if hide:
                    return f"{profile} would take this month over the budget (at limit)"
                return (f"{profile} at about {m(est)} would take this month to {m(spent + est)}, "
                        f"over the {m(lim.month_usd)} budget")
        return None


# -- running workers ----------------------------------------------------------------------------


def worker_spend(db: DB, agent, repo_root: str) -> float | None:
    """What a running worker has spent so far: the larger of its recorded history
    and its live usage, or None if neither can be priced."""
    from brindle import usage

    extra = pricing.repo_overrides(repo_root)
    found = [history_spend(db, repo_root, agent.id, extra, pricing.profile_price(agent.profile, repo_root, extra))]
    try:
        found.append(usage.usage_cost(usage.agent_usage(db, agent), extra))
    except Exception:  # noqa: BLE001
        pass
    found = [f for f in found if f is not None]
    return max(found) if found else None


def sweep(db: DB, now: float | None = None) -> list[str]:
    """Warn at 80% of ``task_usd`` and (with ``stop``) close a worker at 100%. One line per action."""
    from brindle import agents
    from brindle.config import load_repo_config

    done: list[str] = []
    # Only process routing decisions for agents that are currently running
    for r in db.list_routing_decisions_for_live_agents():
        if r.budget_state == "stopped" or not r.agent_id:
            continue
        agent = db.get_agent(r.agent_id)
        if agent is None or agent.dismissed_at is not None or agent.status == "done":
            continue
        try:
            lim = limits(load_repo_config(r.repo_root), r.repo_root)
        except Exception:  # noqa: BLE001
            continue
        if lim is None or lim.task_usd is None:
            continue
        spent = worker_spend(db, agent, r.repo_root)
        if spent is None:
            continue
        m = pricing.money
        try:
            if spent >= lim.task_usd:
                note = (f"[brindle budget] worker {agent.id} is at limit on its task budget."
                        if lim.hide_dollars else
                        f"[brindle budget] worker {agent.id} has spent about {m(spent)}, "
                        f"its {m(lim.task_usd)} task budget.")
                if lim.stop:
                    try:
                        agents.close(db, agent.id)
                    except Exception:  # noqa: BLE001 - leave the state alone so the next sweep retries
                        log.exception("brindle: budget stop failed to close %s", agent.id)
                        continue
                    db.set_budget_state(agent.id, "stopped")
                    note += " It was stopped (budget.stop is on)."
                elif r.budget_state != "over":
                    db.set_budget_state(agent.id, "over")
                else:
                    continue
                _tell(db, agent, note)
                done.append(note)
            elif spent >= lim.task_usd * WARN_AT and not r.budget_state:
                db.set_budget_state(agent.id, "warned")
                note = (f"[brindle budget] worker {agent.id} is getting close to its task budget."
                        if lim.hide_dollars else
                        f"[brindle budget] worker {agent.id} has spent about {m(spent)}, over "
                        f"{round(WARN_AT * 100)}% of its {m(lim.task_usd)} task budget.")
                _tell(db, agent, note)
                done.append(note)
        except Exception:  # noqa: BLE001 - a budget note never breaks the sweep
            log.exception("brindle: budget sweep failed for %s", agent.id)
    return done


def _tell(db: DB, agent, note: str) -> None:
    from brindle import agents, autopilot

    try:
        agents.send_message(db, autopilot.root_of(db, agent.id), note)
    except Exception:  # noqa: BLE001 - the supervisor may be gone
        log.info("brindle: couldn't deliver the budget note for %s", agent.id)


def refusal(task_reasons: list[str]) -> str:
    try:
        from brindle.pro import cost_centers

        hint = cost_centers.refusal_hint()
    except Exception:  # noqa: BLE001
        hint = ""
    return ("Not started: every candidate is over budget (" + "; ".join(task_reasons) + "). "
            "Raise `budget` in .brindle/config.json, pick a cheaper profile by name, or split the work."
            + hint)
