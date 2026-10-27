"""History of permission asks, so how often brindle interrupts the person (and
why) can be measured.

``permission_requests`` rows are deleted once a request is answered; this
module keeps one ``permission_asks`` row per hook decision instead. A request
the policy decided itself (allow or deny) is a *decision*; one it left to the
person, or that a saved action settled, is an *ask*. The ask rate is asks over
both. Nothing here raises into the hook: a failure to record is logged only.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

from brindle.db import DB

log = logging.getLogger("brindle.permission_asks")

# How a Decision's reason reads when a saved action settled it (permission_grants.settle).
SAVED_PREFIXES = ("covered by your saved action", "stopped by your saved deny")
ASK_SUFFIX = "; asking the user"
DEFAULT_DAYS = 7
TOP_CAUSES = 10


def _insert(db: DB, *, agent_id: str, repo_root: str, request_id: str | None, kind: str, summary: str,
            reason: str, asked: bool, resolved_by: str, outcome: str, wait: float) -> None:
    try:
        with db.tx() as c:
            c.execute("INSERT INTO permission_asks (ts, repo_root, agent_id, request_id, kind, summary, "
                      "reason, asked, resolved_by, outcome, wait_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (time.time(), repo_root, agent_id, request_id, kind, summary[:300], reason,
                       int(asked), resolved_by, outcome, wait))
    except Exception:  # noqa: BLE001 - measuring must never break the hook
        log.exception("brindle: couldn't record a permission ask")


def record_decision(db: DB, agent_id: str, repo_root: str, kind: str, summary: str,
                    decision: str, reason: str) -> None:
    """A request the policy answered without the person (``decision`` allow or
    deny). One a saved action settled counts as an ask it answered."""
    saved = reason.startswith(SAVED_PREFIXES)
    _insert(db, agent_id=agent_id, repo_root=repo_root, request_id=None, kind=kind, summary=summary,
            reason=reason, asked=saved, resolved_by="saved_action" if saved else "policy",
            outcome=decision, wait=0.0)


def record_ask(db: DB, agent_id: str, repo_root: str, request_id: str, kind: str, summary: str,
               reason: str, answer: str | None, wait: float) -> None:
    """A request left to the person, once the hold for the app's answer ended.
    ``answer`` is the app's (allow_once, allow_always, deny), or None when it
    timed out or went to the CLI's own prompt (a terminal approval is recorded
    later by ``resolve_at_prompt``)."""
    outcome = "prompt" if answer is None else "deny" if answer == "deny" else "allow"
    _insert(db, agent_id=agent_id, repo_root=repo_root, request_id=request_id, kind=kind, summary=summary,
            reason=reason, asked=True, resolved_by="timeout" if answer is None else "person",
            outcome=outcome, wait=wait)


def resolve_at_prompt(db: DB, agent_id: str, request_id: str) -> None:
    """The person approved at the CLI's own prompt a request whose hold ended
    unanswered: its outcome is allow, answered by the person, after the whole wait."""
    try:
        with db.tx() as c:
            c.execute("UPDATE permission_asks SET resolved_by='person', outcome='allow', "
                      "wait_seconds=? - ts WHERE agent_id=? AND request_id=? AND outcome='prompt'",
                      (time.time(), agent_id, request_id))
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't resolve a permission ask")


def _cause(reason: str) -> str:
    return reason.removesuffix(ASK_SUFFIX) or "(no policy: left to the CLI)"


def report(db: DB, days: int = DEFAULT_DAYS, repo_root: str | None = None, now: float | None = None) -> dict:
    """The ask rate and top causes over the last ``days`` (all repos, or ``repo_root``'s)."""
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
        "avg_wait_seconds": sum(r["wait_seconds"] for r in asks) / len(asks) if asks else 0.0,
        "causes": [{"reason": cause, "count": len(rs), "kinds": sorted({r["kind"] for r in rs}),
                    "avg_wait_seconds": sum(r["wait_seconds"] for r in rs) / len(rs),
                    "example": rs[-1]["summary"]} for cause, rs in causes],
    }


def _count(values) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for v in values:
        out[v] += 1
    return out


def format_report(data: dict) -> str:
    lines = [f"Permission asks, last {data['days']} day(s)",
             f"  hook decisions: {data['decisions']}",
             f"  asks:           {data['asks']}  ({data['ask_rate']:.0%} of decisions)"]
    if data["asks"]:
        lines.append("  answered by:    " + ", ".join(f"{k} {v}" for k, v in data["resolved_by"].items()))
        lines.append("  outcomes:       " + ", ".join(f"{k} {v}" for k, v in data["outcomes"].items()))
        lines.append(f"  average wait:   {data['avg_wait_seconds']:.1f}s")
        lines.append("\nTop causes:")
        for c in data["causes"]:
            lines.append(f"  {c['count']:>4}  {c['reason']}  [{', '.join(c['kinds'])}; "
                         f"avg wait {c['avg_wait_seconds']:.1f}s; e.g. {c['example']}]")
    return "\n".join(lines)
