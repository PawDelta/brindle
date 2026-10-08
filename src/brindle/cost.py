"""Dollar spend from brindle's history: `brindle cost` and `brindle cost report`.

Every history row (``brindle.history``) holds the tokens its agent used since
its previous row, and the model that used them. ``spend`` prices each row at
that model's price (``brindle.pricing``), else at its profile's (a local
model is $0), and keeps the tokens it can't price as "unknown" rather than
guessing. Codex agents' rows have tokens when Codex recorded them in its
rollout; Antigravity's never do, so their spend isn't counted at all.

`brindle cost` (free) is the last 30 days' total and its split by model.
`brindle cost report` (brindle Pro, the ``cost`` feature; fails closed) adds
spend by day, profile and goal, the cost per merged branch and each worker
profile's review pass rate.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from brindle import pricing
from brindle.db import DB, HistoryEntry
from brindle.history import tokens_total
from brindle.pricing import Price
from brindle.usage import format_tokens

FEATURE = "cost"
DAYS = 30
ALL_ROWS = 10_000_000
NO_GOAL = "(no goal)"
LIST_NOTE = ("At list prices: a subscription (Claude Max, ChatGPT), batch or negotiated "
             "discount makes the real bill lower.")
# Claude on an API key is billed at list price: no subscription to caveat.
KEY_LIST_NOTE = "At list prices: a batch or negotiated discount makes the real bill lower."


def _list_note() -> str:
    from brindle import providers

    try:
        return KEY_LIST_NOTE if providers.claude_credential().keyed else LIST_NOTE
    except Exception:  # noqa: BLE001 - the footer must not fail on a broken key store
        return LIST_NOTE


class NotEntitled(Exception):
    pass


@dataclass
class Bucket:
    dollars: float = 0.0
    tokens: int = 0            # tokens priced into ``dollars`` (not the free ones)
    unknown: int = 0           # tokens with no known price
    free: int = 0              # tokens on models known to be free (priced at exactly zero)

    def add(self, dollars: float | None, tokens: int, free: bool = False) -> None:
        if dollars is None:
            self.unknown += tokens
        elif free:
            self.free += tokens
        else:
            self.dollars += dollars
            self.tokens += tokens

    @property
    def all_tokens(self) -> int:
        return self.tokens + self.free + self.unknown

    def show(self) -> str:
        if self.tokens:
            text = pricing.money(self.dollars)
            if self.free:
                text += f" (+{format_tokens(self.free)} tokens free)"
            if self.unknown:
                text += f" (+{format_tokens(self.unknown)} tokens unpriced)"
            return text
        if self.free:
            return "Free" + (f" (+{format_tokens(self.unknown)} tokens unpriced)" if self.unknown else "")
        if self.unknown:
            return f"unknown ({format_tokens(self.unknown)} tokens unpriced)"
        return pricing.money(self.dollars)


@dataclass
class Priced:
    """One history row in dollars."""
    row: HistoryEntry
    model: str | None
    tokens: int
    dollars: float | None      # None: no price known
    free: bool = False         # priced at exactly zero (a local model)


class Pricer:
    """Prices history rows for one repo (its ``pricing`` overrides apply)."""

    def __init__(self, repo_root: str | None):
        self.repo_root = repo_root
        self.extra = pricing.repo_overrides(repo_root)
        self._profiles: dict[str, Price | None] = {}

    def profile(self, name: str | None) -> Price | None:
        if not name:
            return None
        if name not in self._profiles:
            self._profiles[name] = pricing.profile_price(name, self.repo_root, self.extra)
        return self._profiles[name]

    def row(self, row: HistoryEntry) -> Priced | None:
        """``row`` in dollars, or None when it carries no tokens."""
        if not row.tokens:
            return None
        try:
            d = json.loads(row.tokens)
        except ValueError:
            return None
        total = tokens_total(row.tokens)
        if not isinstance(d, dict) or total <= 0:
            return None
        model = d.get("model") if isinstance(d.get("model"), str) else None
        price = pricing.price_for(model, self.extra) or self.profile(row.profile)
        dollars = None
        if price is not None:
            dollars = price.cost(int(d.get("input") or 0), int(d.get("output") or 0),
                                 int(d.get("cache_creation") or 0), int(d.get("cache_read") or 0))
        return Priced(row, model, total, dollars, price is not None and price == pricing.FREE)


def rows_since(db: DB, repo_root: str | None, since: float) -> list[HistoryEntry]:
    """History rows at or after ``since`` (one repo, or all), oldest first."""
    return [r for r in reversed(db.list_history(repo_root, None, ALL_ROWS)) if r.ts >= since]


def priced_rows(db: DB, repo_root: str | None, since: float) -> list[Priced]:
    pricers: dict[str, Pricer] = {}
    out = []
    for r in rows_since(db, repo_root, since):
        pricer = pricers.get(r.repo_root) or pricers.setdefault(r.repo_root, Pricer(r.repo_root))
        p = pricer.row(r)
        if p is not None:
            out.append(p)
    return out


# -- `brindle cost` ---------------------------------------------------------------------------------


@dataclass
class Summary:
    days: int
    total: Bucket = field(default_factory=Bucket)
    by_model: dict[str, Bucket] = field(default_factory=dict)


def model_label(p: Priced) -> str:
    return p.model or (f"profile {p.row.profile}" if p.row.profile else "unknown model")


def summary(db: DB, repo_root: str | None, now: float | None = None, days: int = DAYS) -> Summary:
    now = time.time() if now is None else now
    s = Summary(days)
    for p in priced_rows(db, repo_root, now - days * 86400):
        s.total.add(p.dollars, p.tokens, p.free)
        s.by_model.setdefault(model_label(p), Bucket()).add(p.dollars, p.tokens, p.free)
    return s


def _where(repo_root: str | None) -> str:
    return os.path.basename(repo_root.rstrip(os.sep)) if repo_root else "all repos"


def _footer() -> list[str]:
    lines = ["", f"Prices as of {pricing.AS_OF.isoformat()} (set your own under \"pricing\" in "
             ".brindle/config.json). " + _list_note()]
    warning = pricing.stale_warning()
    if warning:
        lines.append(warning)
    return lines


def describe_summary(s: Summary, repo_root: str | None) -> str:
    lines = [f"Spend in {_where(repo_root)}, last {s.days} days: {s.total.show()}"]
    if not s.by_model:
        lines.append("  no agent usage on record")
    for label, b in sorted(s.by_model.items(), key=lambda kv: (-kv[1].dollars, kv[0])):
        lines.append(f"  {label:<28} {b.show():<24} {format_tokens(b.all_tokens)} tokens")
    lines += _footer()
    lines.append("By day, profile and goal, cost per merged branch and review pass rates: "
                 "`brindle cost report` (brindle Pro).")
    return "\n".join(lines)


# -- `brindle cost report` --------------------------------------------------------------------------


def require_entitled() -> None:
    """Raise ``NotEntitled`` unless the verified entitlement has ``cost``.
    Fails closed: no license, an unreadable one, or any error means no."""
    try:
        from brindle.pro import license

        ok = license.has(FEATURE)
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        raise NotEntitled("`brindle cost report` is part of brindle Pro (the \"cost\" feature): "
                          "`brindle account` shows your plan and how to upgrade.")


@dataclass
class Report:
    days: int
    total: Bucket = field(default_factory=Bucket)
    by_day: dict[str, Bucket] = field(default_factory=dict)
    by_profile: dict[str, Bucket] = field(default_factory=dict)
    by_goal: dict[str, Bucket] = field(default_factory=dict)
    merged: int = 0
    merged_spend: Bucket = field(default_factory=Bucket)
    reviews: dict[str, list[int]] = field(default_factory=dict)   # worker profile -> [approved, total]
    savings: "Savings | None" = None
    stats: dict[tuple[str, str], "ProfileStats"] = field(default_factory=dict)   # (profile, weight)
    depth: list[str] = field(default_factory=list)   # review-depth suggestions (see escapes)

    @property
    def per_merged_branch(self) -> float | None:
        if not self.merged or not self.merged_spend.tokens:
            return None
        return self.merged_spend.dollars / self.merged


@dataclass
class ProfileStats:
    """How one worker profile did at one weight: tasks that finished in the window."""
    finished: int = 0
    merged: int = 0
    unmerged: int = 0                                          # removed without merging
    rounds: int = 0                                            # review rounds over the finished tasks
    merged_spend: Bucket = field(default_factory=Bucket)       # the merged workers' own rows
    merged_counted: int = 0                                    # merged branches with spend rows

    @property
    def merge_rate(self) -> float | None:
        done = self.merged + self.unmerged
        return self.merged / done if done else None

    @property
    def avg_rounds(self) -> float | None:
        return self.rounds / self.finished if self.finished else None

    @property
    def tokens_per_merged(self) -> int | None:
        # Over the merged branches that have spend rows: one with none would
        # pull the average down without costing nothing.
        return self.merged_spend.all_tokens // self.merged_counted if self.merged_counted else None

    @property
    def dollars_per_merged(self) -> float | None:
        """None when nothing was priced; 0.0 when every token was free."""
        b = self.merged_spend
        if not self.merged_counted or not (b.tokens or (b.free and not b.unknown)):
            return None
        return b.dollars / self.merged_counted


def profile_stats(db: DB, repo_root: str | None, since: float) -> dict[tuple[str, str], ProfileStats]:
    """Per (worker profile, weight) results from ``routing_decisions`` made since ``since``."""
    decisions = [d for d in db.list_routing_decisions(repo_root)
                 if d.ts >= since and d.outcome in ("merged", "removed_unmerged")]
    agents = {(d.repo_root, d.agent_id) for d in decisions if d.outcome == "merged" and d.agent_id}
    spend: dict[tuple[str, str | None], Bucket] = {}
    if agents:
        for p in priced_rows(db, repo_root, 0):
            key = (p.row.repo_root, p.row.agent_id)
            if key in agents:
                spend.setdefault(key, Bucket()).add(p.dollars, p.tokens, p.free)
    out: dict[tuple[str, str], ProfileStats] = {}
    for d in decisions:
        s = out.setdefault((d.profile, d.weight or "-"), ProfileStats())
        s.finished += 1
        s.rounds += d.review_rounds
        if d.outcome == "merged":
            s.merged += 1
            b = spend.get((d.repo_root, d.agent_id))
            if b is not None:
                s.merged_counted += 1
                s.merged_spend.dollars += b.dollars
                s.merged_spend.tokens += b.tokens
                s.merged_spend.free += b.free
                s.merged_spend.unknown += b.unknown
        else:
            s.unmerged += 1
    return out


def _goal_of(db: DB, agent_id: str | None, cache: dict) -> str:
    """The goal of the session ``agent_id`` worked in, or ``NO_GOAL``. Found
    through the agent itself, else the task that started it (agents are
    pruned with their sessions; tasks outlive them less often)."""
    from brindle import autopilot

    if not agent_id:
        return NO_GOAL
    if agent_id in cache:
        return cache[agent_id]
    if None not in cache:   # once: each started task's worker -> its caller
        cache[None] = {t.agent_id: t.caller_id for t in db.list_tasks() if t.agent_id and t.caller_id}
    goal = None
    starts = [agent_id] if db.get_agent(agent_id) else []
    if agent_id in cache[None]:
        starts.append(cache[None][agent_id])
    for start in starts:
        ap = db.get_autopilot(autopilot.root_of(db, start))
        if ap and ap.goal:
            goal = ap.goal.strip().splitlines()[0]
            break
    cache[agent_id] = goal if goal else NO_GOAL
    return cache[agent_id]


def _verdict(row: HistoryEntry) -> bool | None:
    """A branch review row's verdict (not a goal audit's), or None."""
    first = (row.result or "").strip().splitlines()[0] if (row.result or "").strip() else ""
    if not first.startswith("Review of "):
        return None
    if first.endswith("CHANGES REQUESTED"):
        return False
    if first.endswith("APPROVED"):
        return True
    return None


def report(db: DB, repo_root: str | None, now: float | None = None, days: int = DAYS) -> Report:
    now = time.time() if now is None else now
    since = now - days * 86400
    rep = Report(days)
    goals: dict = {}
    for p in priced_rows(db, repo_root, since):
        rep.total.add(p.dollars, p.tokens, p.free)
        day = time.strftime("%Y-%m-%d", time.localtime(p.row.ts))
        rep.by_day.setdefault(day, Bucket()).add(p.dollars, p.tokens, p.free)
        rep.by_profile.setdefault(p.row.profile or "?", Bucket()).add(p.dollars, p.tokens, p.free)
        rep.by_goal.setdefault(_goal_of(db, p.row.agent_id, goals), Bucket()).add(p.dollars, p.tokens, p.free)
    rows = rows_since(db, repo_root, since)
    merged = {(r.repo_root, r.branch) for r in rows if r.kind == "merge" and r.branch}
    rep.merged = len(merged)
    # every row of a merged branch counts toward it, back to before the window began
    for p in priced_rows(db, repo_root, 0):
        if (p.row.repo_root, p.row.branch) in merged:
            rep.merged_spend.add(p.dollars, p.tokens, p.free)
    worker_profile = {(r.repo_root, r.branch): r.profile for r in db.list_history(repo_root, "worker_result", ALL_ROWS)
                      if r.branch and r.profile}   # newest first: keep the oldest (the worker's own)
    for r in rows:
        if r.kind != "review":
            continue
        verdict = _verdict(r)
        if verdict is None:
            continue
        counts = rep.reviews.setdefault(worker_profile.get((r.repo_root, r.branch), "?"), [0, 0])
        counts[0] += int(verdict)
        counts[1] += 1
    rep.stats = profile_stats(db, repo_root, since)
    rep.savings = savings(db, repo_root, now, days)
    rep.depth = _depth(db, repo_root, now)
    return rep


def _depth(db: DB, repo_root: str | None, now: float) -> list[str]:
    """Review-depth suggestions for one repo (none for ``--all``); never raises."""
    if not repo_root:
        return []
    try:
        from brindle import escapes
        from brindle.config import load_repo_config

        return escapes.review_depth_suggestions(db, repo_root, load_repo_config(repo_root).second_review, now)
    except Exception:  # noqa: BLE001 -- a side section never breaks the report
        return []


def describe_depth(lines: list[str]) -> list[str]:
    if not lines:
        return []
    return (["", f"Review depth suggestions, last {escapes_days()} days (copy into .brindle/config.json; "
                 "brindle never changes it)"] + [f"  {line}" for line in lines])


def escapes_days() -> int:
    from brindle import escapes

    return escapes.DEPTH_DAYS


@dataclass
class Savings:
    """What caching, cheaper routing and budget demotions saved (list prices)."""
    days: int
    cache_read: int = 0            # input tokens served from the cache
    cache_input: int = 0           # all input tokens (fresh, cache writes and cache reads)
    top_profile: str | None = None  # the most expensive profile the routing can pick
    routed: int = 0                # tasks priced both ways
    routing_saved: float = 0.0     # the same tokens at ``top_profile``'s price, less the actual spend
    demotions: int = 0             # tasks a budget moved to a cheaper profile (priced ones)
    demotion_saved: float = 0.0

    @property
    def cache_hit_rate(self) -> float | None:
        return self.cache_read / self.cache_input if self.cache_input else None


def _top_profile(cfg, repo_root: str, extra) -> str | None:
    best: tuple[float, str] | None = None
    for names in cfg.routing.values():
        for name in names:
            price = pricing.profile_price(name, repo_root, extra)
            if price is not None and (best is None or price.output > best[0]):
                best = (price.output, name)
    return best[1] if best else None


def savings(db: DB, repo_root: str | None, now: float | None = None, days: int = DAYS) -> Savings:
    from brindle import savings as sv
    from brindle.config import load_repo_config

    now = time.time() if now is None else now
    since = now - days * 86400
    out = Savings(days)
    for p in priced_rows(db, repo_root, since):
        d = json.loads(p.row.tokens)
        read = int(d.get("cache_read") or 0)
        out.cache_read += read
        out.cache_input += int(d.get("input") or 0) + int(d.get("cache_creation") or 0) + read
    decisions = [r for r in db.list_routing_decisions(repo_root) if r.ts >= since]
    for root in sorted({r.repo_root for r in decisions}):
        rows = [r for r in decisions if r.repo_root == root]
        prices = sv._default_prices(db, root, rows)
        try:
            top = _top_profile(load_repo_config(root), root, prices.extra)
        except Exception:  # noqa: BLE001
            top = None
        out.top_profile = out.top_profile or top
        for r in rows:
            actual = prices.actual(r)
            if actual is None:
                continue
            if top and r.profile != top:
                at_top = prices.at(r, top)
                if at_top is not None:
                    out.routed += 1
                    out.routing_saved += at_top - actual
            if r.demoted_from:
                before = prices.at(r, r.demoted_from)
                if before is not None:
                    out.demotions += 1
                    out.demotion_saved += before - actual
    return out


def describe_savings(s: Savings) -> list[str]:
    lines = ["", f"Savings, last {s.days} days (estimates at list prices)"]
    rate = s.cache_hit_rate
    lines.append("  cache-hit rate: " + (f"{rate * 100:.0f}% of input tokens ({format_tokens(s.cache_read)} "
                                         f"of {format_tokens(s.cache_input)}) came from the cache"
                                         if rate is not None else "no token usage on record"))
    if s.top_profile and s.routed:
        lines.append(f"  cheaper routing: {pricing.money(s.routing_saved)} saved over {s.routed} task(s) "
                     f"against running them all on {s.top_profile}")
    else:
        lines.append("  cheaper routing: nothing to compare yet")
    if s.demotions:
        lines.append(f"  budget demotions: {pricing.money(s.demotion_saved)} avoided over {s.demotions} "
                     "task(s) moved to a cheaper profile")
    else:
        lines.append("  budget demotions: none")
    return lines


def _table(title: str, buckets: dict[str, Bucket], width: int = 28, limit: int | None = None) -> list[str]:
    lines = ["", title]
    items = sorted(buckets.items(), key=lambda kv: (-kv[1].dollars, -kv[1].unknown, kv[0]))
    if not items:
        lines.append("  -")
    for label, b in items[:limit] if limit else items:
        label = label if len(label) <= width else label[: width - 1] + "…"
        lines.append(f"  {label:<{width}} {b.show()}")
    if limit and len(items) > limit:
        lines.append(f"  … and {len(items) - limit} more")
    return lines


def describe_stats(stats: dict[tuple[str, str], ProfileStats]) -> list[str]:
    lines = ["", "Worker profiles by weight (finished tasks)"]
    if not stats:
        lines.append("  no finished tasks in this period")
    for (profile, weight), s in sorted(stats.items(), key=lambda kv: (-kv[1].finished, kv[0])):
        rate = s.merge_rate
        rounds = s.avg_rounds
        per = s.dollars_per_merged
        tok = s.tokens_per_merged
        cost_text = "-"
        if tok is not None:
            free = per == 0 and not s.merged_spend.tokens
            cost_text = f"{format_tokens(tok)} tokens, " + (
                "free" if free else pricing.money(per) if per is not None else "unpriced")
            cost_text += " per merged branch"
        lines.append(f"  {profile + ' / ' + weight:<28} {s.finished} finished, "
                     + (f"{round(100 * rate)}% merged" if rate is not None else "no merge rate")
                     + f", {rounds:.1f} review rounds avg, {cost_text}")
    return lines


def org_budget_line(repo_root: str | None) -> str | None:
    """This month's org seat budget: dollars, or only a status word when the
    org hides this person's dollars. None without an org budget."""
    from brindle import budget

    try:
        lim = budget.org_limits(repo_root)
    except Exception:  # noqa: BLE001 - the report outlives an unreadable policy
        return None
    if lim is None or lim.month_usd is None:
        return None
    spent = lim.seat_spent_usd or 0.0
    if lim.hide_dollars:
        return f"Org budget this month: {budget.status_word(spent, lim.month_usd)}"
    return f"Org budget this month: {pricing.money(spent)} of {pricing.money(lim.month_usd)}"


def describe_report(rep: Report, repo_root: str | None) -> str:
    lines = [f"Cost report for {_where(repo_root)}, last {rep.days} days: {rep.total.show()}"]
    org_line = org_budget_line(repo_root)
    if org_line:
        lines.append(org_line)
    lines += _table("By day", dict(sorted(rep.by_day.items())), width=12)
    lines += _table("By profile", rep.by_profile)
    lines += _table("By goal", rep.by_goal, width=48, limit=10)
    lines.append("")
    per = rep.per_merged_branch
    if per is None:
        if rep.merged and rep.merged_spend.free and not rep.merged_spend.tokens:
            lines.append(f"Merged branches: {rep.merged}, Free"
                         + (f" (+{format_tokens(rep.merged_spend.unknown)} tokens unpriced)"
                            if rep.merged_spend.unknown else ""))
        else:
            lines.append(f"Merged branches: {rep.merged}, no priced spend on them" if rep.merged
                         else "Merged branches: none in this period")
    else:
        unpriced = ((f" (+{format_tokens(rep.merged_spend.free)} tokens free)" if rep.merged_spend.free else "")
                    + (f" (+{format_tokens(rep.merged_spend.unknown)} tokens unpriced)"
                       if rep.merged_spend.unknown else ""))
        lines.append(f"Merged branches: {rep.merged}, {pricing.money(per)} each on average "
                     f"(every row on the branch: worker, reviews, merge){unpriced}")
    lines.append("")
    lines.append("Review pass rate (first-time and re-reviews), by worker profile")
    if not rep.reviews:
        lines.append("  no reviews in this period")
    for profile, (ok, total) in sorted(rep.reviews.items(), key=lambda kv: (-kv[1][1], kv[0])):
        lines.append(f"  {profile:<28} {ok}/{total} approved ({round(100 * ok / total)}%)")
    lines += describe_stats(rep.stats)
    lines += describe_depth(rep.depth)
    if rep.savings is not None:
        lines += describe_savings(rep.savings)
    lines += _footer()
    return "\n".join(lines)


__all__ = ["DAYS", "FEATURE", "NO_GOAL", "Bucket", "NotEntitled", "Pricer", "Report", "Summary",
           "describe_report", "describe_summary", "report", "require_entitled", "summary"]
