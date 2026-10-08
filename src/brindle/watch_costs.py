"""The "Costs" section of the sidebar dashboard (``brindle watch``).

Two kinds of figures, kept apart so the screen never waits on the network:

* local ones (``local``): this month's token share by model from the history
  (what the section shows), the month's dollars (for the budget line), and a
  dollar figure per running worker. Cheap; recomputed on every redraw.
* remote ones (``Feed``): the budget in force (``budget.limits``, which can
  read the org policy over the network) and, for a Team or Enterprise org
  with ``org_budgets``, the org's month (``GET /orgs/{id}/spend``). The
  org's totals and cost centers are not drawn here (the website has them).
  Collected on a thread, kept for ``CACHE_SECONDS``.

``render`` knows nothing about curses; it returns ``watch.Line`` rows.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

from brindle import budget, pricing
from brindle.db import DB

log = logging.getLogger(__name__)

CACHE_SECONDS = 30.0
GROUP = "@costs"                  # the section's id, for folding (a workspace id always has a "/")
ADMIN_ROLES = ("owner", "admin")
BAR_WIDTH = 10
TOP_MODELS = 3
RUNNING = ("processing", "starting", "waiting", "idle")


@dataclass
class Local:
    month: float = 0.0            # dollars this month, for the budget line
    models: list[tuple[str, int]] = field(default_factory=list)   # (model label, tokens) this month
    workers: dict[str, float | str] = field(default_factory=dict)   # agent id -> dollars ("Free" if free)


@dataclass
class Org:
    """What the org line shows: an admin's view of the org, or a member's own seat."""
    admin: bool
    spent: float
    limit: float | None = None
    centers: list[tuple[str, float, float | None]] = field(default_factory=list)   # name, usd, budget
    seats_over: int = 0           # seats past the per-seat monthly budget
    unattributed: float = 0.0     # spend no cost center claims


@dataclass
class Remote:
    limits: budget.Limits | None = None
    org: Org | None = None
    at: float = 0.0               # when it was collected


def local(db: DB, repo_root: str | None, snap: list[dict], now: float | None = None) -> Local:
    """This month's tokens per model and dollars, and dollars per running
    worker. Never raises: a figure that can't be had is left out."""
    from brindle import cost

    now = time.time() if now is None else now
    out = Local()
    try:
        tokens: dict[str, int] = {}
        for p in cost.priced_rows(db, repo_root, budget._month_start(now)):
            out.month += p.dollars or 0.0
            label = cost.model_label(p)
            tokens[label] = tokens.get(label, 0) + p.tokens
        out.models = sorted(tokens.items(), key=lambda m: (-m[1], m[0]))
    except Exception:  # noqa: BLE001 - the dashboard outlives a bad history row
        log.exception("brindle watch: couldn't total the spend")
    if repo_root:
        for ws in snap:
            for a in ws["agents"]:
                if a.get("mode") not in ("assign", "handoff") or a["status"] not in RUNNING:
                    continue
                try:
                    rec = db.get_agent(a["id"])
                    dollars = budget.worker_spend(db, rec, repo_root) if rec else None
                except Exception:  # noqa: BLE001
                    dollars = None
                if dollars is not None:
                    free = False
                    if not dollars:   # $0 on a known-free profile is "Free", not "$0.00"
                        try:
                            free = pricing.profile_price(rec.profile, repo_root) == pricing.FREE
                        except Exception:  # noqa: BLE001
                            free = False
                    out.workers[a["id"]] = pricing.FREE_LABEL if free else dollars
    return out


# -- the org -----------------------------------------------------------------------------------


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def parse_spend(body) -> Org | None:
    """An admin's :class:`Org` from the ``/orgs/{id}/spend`` answer::

        {org_id, month, seats: [{sub, role, usd}], total_usd,
         budget: {seat_month_usd, goal_usd},
         cost_centers: {centers: [{name, usd, budget_usd}], unattributed_usd}}

    (``cost_centers`` only on Enterprise.) The org's cap is the per-seat
    budget times the seats. Anything unreadable is None."""
    if not isinstance(body, dict):
        return None
    total = _num(body.get("total_usd"))
    if total is None:
        return None
    seats = [s for s in body.get("seats") or [] if isinstance(s, dict)]
    budget_ = body.get("budget") if isinstance(body.get("budget"), dict) else {}
    per_seat = _num(budget_.get("seat_month_usd"))
    over = sum(1 for s in seats if per_seat is not None and (_num(s.get("usd")) or 0.0) > per_seat)
    cc = body.get("cost_centers") if isinstance(body.get("cost_centers"), dict) else {}
    centers = [(c["name"], n, _num(c.get("budget_usd")))
               for c in cc.get("centers") or []
               if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"]
               and (n := _num(c.get("usd"))) is not None]
    return Org(True, total, per_seat * len(seats) if per_seat and seats else None, centers, over,
               _num(cc.get("unattributed_usd")) or 0.0)


def fetch_spend(org_id: str, client=None, store=None) -> Org | None:
    """``GET /orgs/{org_id}/spend``, as ``team_policy.fetch_policy`` calls the
    policy endpoint. None on any failure, a 403 included."""
    from brindle.pro import auth, credentials
    from brindle.pro.team_policy import FETCH_TIMEOUT, ORG_RE

    if not ORG_RE.match(org_id):
        return None
    store = store or credentials.default_store()
    if client is None:
        creds = store.load() or {}
        client = auth.Client(creds.get("base_url"), auth.UrllibTransport(timeout=FETCH_TIMEOUT))
    try:
        status, body = auth.authed(client, store, "GET", f"/orgs/{org_id}/spend")
    except Exception:  # noqa: BLE001 - offline, signed out...: no org line
        return None
    return parse_spend(body) if status == 200 else None


def _org(repo_root: str | None, lim: budget.Limits | None) -> Org | None:
    from brindle.pro import license, team_policy

    ent = license.current(refresh=False)
    if team_policy.BUDGETS_FEATURE not in ent.features:
        return None
    if ent.role in ADMIN_ROLES:
        return fetch_spend(ent.org_id)
    # A plain member sees only their own seat, from the policy fetch ``budget.limits``
    # already made (``lim.org``: an org budget is part of it).
    # Not at all when the org hides this person's dollars: the Org line carries only dollars.
    if lim is None or not lim.org or lim.seat_spent_usd is None or lim.hide_dollars:
        return None
    return Org(False, lim.seat_spent_usd, lim.month_usd)


def collect(repo_root: str | None) -> Remote:
    """Everything that may touch the network. Each part fails on its own."""
    out = Remote(at=time.time())
    if repo_root:
        try:
            from brindle.config import load_repo_config

            out.limits = budget.limits(load_repo_config(repo_root), repo_root)
        except Exception:  # noqa: BLE001
            log.debug("brindle watch: no budget", exc_info=True)
    try:
        out.org = _org(repo_root, out.limits)
    except Exception:  # noqa: BLE001 - offline, signed out or no Pro: no org line
        out.org = None
    return out


class Feed:
    """The remote figures, collected on a thread so a slow network never holds
    up a redraw. ``get`` returns the last answer at once and starts a new
    collection when it is older than ``CACHE_SECONDS``."""

    def __init__(self, repo_root: str | None, collector=collect):
        self.repo_root, self._collector = repo_root, collector
        self._remote: Remote | None = None
        self._running = False
        self._lock = threading.Lock()

    def get(self, now: float | None = None) -> Remote | None:
        now = time.time() if now is None else now
        with self._lock:
            if not self._running and (self._remote is None or now - self._remote.at >= CACHE_SECONDS):
                self._running = True
                threading.Thread(target=self._work, daemon=True).start()
            return self._remote

    def _work(self) -> None:
        try:
            remote = self._collector(self.repo_root)
        except Exception:  # noqa: BLE001
            remote = Remote(at=time.time())
        with self._lock:
            self._remote, self._running = remote, False

    def wait(self, timeout: float = 5.0) -> None:
        """Block until the running collection ends (``--once`` and tests)."""
        end = time.time() + timeout
        while self._running and time.time() < end:
            time.sleep(0.01)


# -- drawing -----------------------------------------------------------------------------------


def bar(spent: float, limit: float, width: int = BAR_WIDTH) -> str:
    filled = min(width, round(width * spent / limit)) if limit > 0 else width
    return "█" * filled + "░" * (width - filled)


def _vs(spent: float, limit: float, width: int, label: str = "") -> tuple[str, str]:
    """``(text, style)`` for "$X of $Y" with a bar; alert at ``budget.WARN_AT``."""
    m = pricing.money
    ratio = spent / limit if limit > 0 else 1.0
    style = "alert" if ratio >= budget.WARN_AT else "ok"
    text = f"{label}{m(spent)} of {m(limit)}"
    room = width - 2 - len(text) - 1
    return (f"{bar(spent, limit, min(BAR_WIDTH, room))} {text}" if room >= 3 else text), style


def shares(models: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """``(label, percent)`` for the top ``TOP_MODELS`` models by tokens, the
    rest folded into "other". Percents are whole numbers summing to 100."""
    total = sum(t for _, t in models)
    if total <= 0:
        return []
    ranked = sorted(models, key=lambda m: (-m[1], m[0]))
    top, rest = ranked[:TOP_MODELS], sum(t for _, t in ranked[TOP_MODELS:])
    rows = top + ([("other", rest)] if rest else [])
    pct = [int(t * 100 / total) for _, t in rows]
    # largest remainders get the leftover points, so the column adds up
    order = sorted(range(len(rows)), key=lambda i: -(rows[i][1] * 100 % total))
    for i in order[:100 - sum(pct)]:
        pct[i] += 1
    return [(name, p) for (name, _), p in zip(rows, pct)]


def short(label: str) -> str:
    """A model label as the narrow sidebar shows it: no ``claude-`` prefix and no
    trailing date stamp (``claude-opus-4-1-20250805`` -> ``opus-4-1``)."""
    name = label.removeprefix("claude-")
    return re.sub(r"-\d{8}$", "", name) or label


def share_rows(models: list[tuple[str, int]], width: int) -> list[str]:
    rows = [(short(n), p) for n, p in shares(models)]
    name_w = min(max((len(n) for n, _ in rows), default=0), max(width - BAR_WIDTH - 6, 4))
    out = []
    for name, p in rows:
        room = width - name_w - 6
        b = bar(p, 100, min(BAR_WIDTH, room)) + " " if room >= 3 else ""
        out.append(f"{name[:name_w]:<{name_w}} {b}{p}%")
    return out


def title(collapsed: bool, width: int, local_: Local | None) -> str:
    arrow = "▸ " if collapsed else "▾ "
    top = shares(local_.models)[:1] if collapsed and local_ else []
    tail = f" ({short(top[0][0])} {top[0][1]}%)" if top else ""
    if len(arrow) + len("Costs") + len(tail) > width:
        tail = ""    # the title stays whole; the top model shows when unfolded
    return arrow + "Costs"[:max(width - len(arrow) - len(tail), 1)] + tail


def render(local_: Local, remote: Remote | None, now: float, width: int,
           collapsed: bool) -> list:
    """The section: a header and, unless folded, a row per model and the budget line."""
    from brindle.watch import Line, fit

    lines = [Line(fit(title(collapsed, width, local_), width), "bold", group=GROUP)]
    if collapsed:
        return lines
    body = [(t, "normal") for t in share_rows(local_.models, width - 2)]
    if not body:
        body.append(("no usage this month", "dim"))
    lim = remote.limits if remote else None
    if lim is not None and lim.month_usd:
        # What the org counts for this seat can be more than what this repo shows.
        spent = max(local_.month, lim.seat_spent_usd or 0.0)
        if lim.hide_dollars:      # the org hides this person's dollars: only a status
            word = budget.status_word(spent, lim.month_usd)
            body.append((f"budget: {word}", "ok" if word == budget.WITHIN else "alert"))
        else:
            body.append(_vs(spent, lim.month_usd, width - 2))
    lines += [Line(fit("  " + t, width), s) for t, s in body]
    return lines
