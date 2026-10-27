"""History of permission asks, so how often brindle interrupts the person (and
why) can be measured.

``permission_requests`` rows are deleted once a request is answered; this
module keeps one ``permission_asks`` row per hook decision instead. A request
the policy decided itself (allow or deny) is a *decision*; one it left to the
person, or that a saved action settled, is an *ask*. The ask rate is asks over
both. Nothing here raises into the hook: a failure to record is logged only.

An ask starts pending (``resolved_by`` NULL) and is finished by how the hold
for the app's answer ended (``finish_ask``). Its ``wait_seconds`` runs from
the ask to the end of the person's wait, and is NULL where that end can't be
seen: an approval at the CLI's own prompt is only reported by the tool's
PostToolUse, once the tool has run.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

from brindle.db import DB

log = logging.getLogger("brindle.permission_asks")

APP_ANSWERS = ("allow_once", "allow_always", "deny")
# How a hold ended (agents._await_app_answer), besides one of APP_ANSWERS:
#   prompt       the CLI's own prompt took it while held (the person answered there)
#   terminal     the person sent it from the app to the CLI's prompt
#   timeout      the hold ran out; the CLI's prompt asks from then on
#   agent_exit   the agent was closed or stopped
#   daemon_lost  the app's daemon went away; the CLI's prompt asks from then on
# and None when nothing was held (no daemon: straight to the CLI's prompt, "unheld").
UNHELD = "unheld"
ASK_SUFFIX = "; asking the user"
DEFAULT_DAYS = 7
TOP_CAUSES = 10


def _insert(db: DB, *, agent_id: str, repo_root: str, request_id: str | None, kind: str, summary: str,
            reason: str, asked: bool, resolved_by: str | None, outcome: str, wait: float | None) -> int | None:
    try:
        with db.tx() as c:
            return c.execute(
                "INSERT INTO permission_asks (ts, repo_root, agent_id, request_id, kind, summary, "
                "reason, asked, resolved_by, outcome, wait_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), repo_root, agent_id, request_id, kind, summary[:300], reason,
                 int(asked), resolved_by, outcome, wait)).lastrowid
    except Exception:  # noqa: BLE001 - measuring must never break the hook
        log.exception("brindle: couldn't record a permission ask")
        return None


def record_decision(db: DB, agent_id: str, repo_root: str, kind: str, summary: str,
                    decision: str, reason: str, saved: bool = False) -> None:
    """A request the policy answered without the person (``decision`` allow or
    deny). One a saved action settled (``saved``: the Decision's) counts as an
    ask it answered."""
    _insert(db, agent_id=agent_id, repo_root=repo_root, request_id=None, kind=kind, summary=summary,
            reason=reason, asked=saved, resolved_by="saved_action" if saved else "policy",
            outcome=decision, wait=0.0)


def start_ask(db: DB, agent_id: str, repo_root: str, request_id: str, kind: str, summary: str,
              reason: str) -> int | None:
    """A request left to the person, recorded before the wait so an approval at
    the CLI's own prompt during the hold can find it (``resolve_at_prompt``).
    ``ts`` is when the ask began; it is pending until ``finish_ask``."""
    return _insert(db, agent_id=agent_id, repo_root=repo_root, request_id=request_id, kind=kind,
                   summary=summary, reason=reason, asked=True, resolved_by=None, outcome="pending",
                   wait=None)


def finish_ask(db: DB, ask_id: int | None, ended: str | None) -> None:
    """The hold for the app's answer ended, now, as ``ended`` says (see
    APP_ANSWERS and the exits under it). The wait is known only when the
    person's answer ended the hold, in the app or at the CLI's prompt. An ask
    the terminal already resolved is left as it is."""
    if ask_id is None:
        return
    if ended in APP_ANSWERS:
        resolved_by, outcome = "person", "deny" if ended == "deny" else "allow"
    elif ended == "prompt":
        resolved_by, outcome = "person", "prompt"     # allowed or refused there: a PostToolUse tells which
    else:
        resolved_by, outcome = ended or UNHELD, "none" if ended == "agent_exit" else "prompt"
    try:
        with db.tx() as c:
            c.execute("UPDATE permission_asks SET resolved_by=?, outcome=?, "
                      "wait_seconds=CASE WHEN ? THEN ?-ts END WHERE id=? AND outcome='pending'",
                      (resolved_by, outcome, resolved_by == "person", time.time(), ask_id))
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't finish a permission ask")


def record_unheld_ask(db: DB, agent_id: str, repo_root: str, kind: str, summary: str, reason: str) -> None:
    """An ask that was not held for the app (left straight to the CLI's prompt)."""
    finish_ask(db, start_ask(db, agent_id, repo_root, "", kind, summary, reason), None)


def resolve_at_prompt(db: DB, agent_id: str, request_id: str) -> None:
    """The person approved the request at the CLI's own prompt (its PostToolUse
    came): allow, answered by the person. The wait is left as it is: the
    PostToolUse comes once the tool has run, so its time says nothing about
    when the person answered (the hold's end, if it saw the answer, does)."""
    try:
        with db.tx() as c:
            c.execute("UPDATE permission_asks SET resolved_by='person', outcome='allow' "
                      "WHERE agent_id=? AND request_id=? AND outcome IN ('pending', 'prompt')",
                      (agent_id, request_id))
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't resolve a permission ask")


def _cause(reason: str) -> str:
    return reason.removesuffix(ASK_SUFFIX) or "(no policy: left to the CLI)"


def report(db: DB, days: int = DEFAULT_DAYS, repo_root: str | None = None, now: float | None = None) -> dict:
    """The ask rate and top causes over the last ``days`` (all repos, or
    ``repo_root``'s). A pending ask counts as an ask, but not toward who
    answered or the waits; an average wait is over the waits known (None
    when none is)."""
    since = (now if now is not None else time.time()) - days * 86400
    where, args = "ts >= ?", [since]
    if repo_root:
        where, args = where + " AND repo_root=?", [*args, repo_root]
    rows = db.conn.execute(f"SELECT * FROM permission_asks WHERE {where}", args).fetchall()
    asks = [r for r in rows if r["asked"]]
    by_cause: dict[str, list] = defaultdict(list)
    for r in asks:
        by_cause[_cause(r["reason"])].append(r)
    causes = sorted(by_cause.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:TOP_CAUSES]
    by = lambda key: dict(sorted(_count(r[key] for r in asks).items()))  # noqa: E731
    return {
        "days": days,
        "decisions": len(rows),
        "asks": len(asks),
        "ask_rate": len(asks) / len(rows) if rows else 0.0,
        "resolved_by": by("resolved_by"),
        "outcomes": by("outcome"),
        "avg_wait_seconds": _avg_wait(asks),
        "causes": [{"reason": cause, "count": len(rs), "kinds": sorted({r["kind"] for r in rs}),
                    "avg_wait_seconds": _avg_wait(rs),
                    "example": rs[-1]["summary"]} for cause, rs in causes],
    }


def _count(values) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for v in values:
        if v is not None:   # a pending ask: nobody answered it yet
            out[v] += 1
    return out


def _avg_wait(rows) -> float | None:
    waits = [r["wait_seconds"] for r in rows if r["wait_seconds"] is not None]
    return sum(waits) / len(waits) if waits else None


def _seconds(wait: float | None) -> str:
    return "unknown" if wait is None else f"{wait:.1f}s"


def format_report(data: dict) -> str:
    lines = [f"Permission asks, last {data['days']} day(s)",
             f"  hook decisions: {data['decisions']}",
             f"  asks:           {data['asks']}  ({data['ask_rate']:.0%} of decisions)"]
    if data["asks"]:
        lines.append("  answered by:    " + ", ".join(f"{k} {v}" for k, v in data["resolved_by"].items()))
        lines.append("  outcomes:       " + ", ".join(f"{k} {v}" for k, v in data["outcomes"].items()))
        lines.append(f"  average wait:   {_seconds(data['avg_wait_seconds'])}")
        lines.append("\nTop causes:")
        for c in data["causes"]:
            lines.append(f"  {c['count']:>4}  {c['reason']}  [{', '.join(c['kinds'])}; "
                         f"avg wait {_seconds(c['avg_wait_seconds'])}; e.g. {c['example']}]")
    return "\n".join(lines)
