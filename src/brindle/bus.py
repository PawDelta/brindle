"""The local event bus: an append-only ``events`` table that publishers write
and readers tail by id. Rows are short-lived (see :func:`prune`, called by the
cull sweep). Publishing never raises: an event is a notification, and losing
one must not break the action that caused it."""

from __future__ import annotations

import json
import logging
import math
import statistics
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # db.py publishes too, so no runtime import back
    from brindle.db import DB, Workspace

log = logging.getLogger(__name__)
RETENTION_SECONDS = 24 * 3600
_COLUMNS = ("repo_root", "root_id", "agent_id", "workspace_id", "task_id")
# An agent's workspace, repo and session (its topmost ancestor), in one query.
_AGENT_CONTEXT = (
    "WITH RECURSIVE up(id, parent_id, n) AS ("
    " SELECT id, parent_id, 0 FROM agents WHERE id = ?"
    " UNION ALL SELECT a.id, a.parent_id, up.n + 1 FROM agents a JOIN up ON a.id = up.parent_id"
    " WHERE up.n < 64)"
    " SELECT (SELECT id FROM up ORDER BY n DESC LIMIT 1) AS root_id, a.workspace_id, w.repo_root"
    " FROM agents a LEFT JOIN workspaces w ON w.id = a.workspace_id WHERE a.id = ?")


def publish(db: DB, kind: str, **fields) -> int | None:
    """Append one event and return its id, or None if it couldn't be written.
    The known id columns are stored as columns, everything else as payload."""
    try:
        cols = {k: fields.pop(k, None) for k in _COLUMNS}
        cur = db.conn.execute(
            "INSERT INTO events (ts, kind, repo_root, root_id, agent_id, workspace_id, task_id,"
            " payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), kind, *cols.values(), json.dumps(fields, default=str)))
        return cur.lastrowid
    except Exception:
        return None


def publish_agent(db: DB, kind: str, agent_id: str, **fields) -> int | None:
    """:func:`publish` an event about agent ``agent_id``, with its workspace,
    repo and session filled in (``fields`` win over them)."""
    try:
        row = db.conn.execute(_AGENT_CONTEXT, (agent_id, agent_id)).fetchone()
        context = dict(row) if row else {}
    except Exception:
        context = {}
    return publish(db, kind, agent_id=agent_id, **{**context, **fields})


def publish_ws(db: DB, kind: str, ws: Workspace, **fields) -> int | None:
    """:func:`publish` an event about workspace ``ws``."""
    return publish(db, kind, **{"workspace_id": ws.id, "repo_root": ws.repo_root, **fields})


def tail(db: DB, since_id: int = 0, *, kinds: tuple[str, ...] | None = None,
         repo_root: str | None = None, limit: int = 500) -> list[dict]:
    """Events with an id above ``since_id``, oldest first. ``kinds`` and
    ``repo_root`` narrow them; None means any."""
    sql, args = "SELECT * FROM events WHERE id > ?", [since_id]
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    if repo_root is not None:
        sql += " AND repo_root = ?"
        args.append(repo_root)
    rows = db.conn.execute(sql + " ORDER BY id LIMIT ?", (*args, limit)).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def _percentile(ordered: list[float], q: float) -> float:
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _rollup(c, cutoff: float) -> None:
    """Fold the pipeline stage durations of events older than ``cutoff`` into
    ``stage_timings`` (median and p90 per UTC day and repo), so they outlive
    :func:`prune`. Stages: report to reviewer start, review duration (per
    reviewer), fix round (a rejection to the next report) and final report to
    merge; each counts on the day it ended. A stage still open when its start
    event is pruned waits in ``stage_open`` for its end. Runs inside the
    caller's transaction."""
    kinds = ("pipeline.reported", "review.started", "pipeline.review", "pipeline.merged",
             "pipeline.removed")
    rows = c.execute(
        f"SELECT ts, kind, repo_root, workspace_id, agent_id, payload FROM events WHERE ts < ? AND kind IN"
        f" ({','.join('?' * len(kinds))}) AND workspace_id IS NOT NULL ORDER BY id",
        (cutoff, *kinds)).fetchall()
    if not rows:
        return
    # (workspace, stage, reviewer or "") -> start ts
    open_at = {(r[0], r[1], r[2]): r[3] for r in c.execute(
        "SELECT workspace_id, stage, who, ts FROM stage_open")}
    samples: dict[tuple[str, str, str], list[float]] = {}

    def end(r, stage: str, who: str = "") -> None:
        began = open_at.pop((r["workspace_id"], stage, who), None)
        if began is not None:
            day = time.strftime("%Y-%m-%d", time.gmtime(r["ts"]))
            samples.setdefault((day, r["repo_root"] or "", stage), []).append(r["ts"] - began)

    for r in rows:
        ws, ts, kind = r["workspace_id"], r["ts"], r["kind"]
        if kind == "pipeline.reported":
            end(r, "fix_round")
            open_at[(ws, "report_to_review", "")] = open_at[(ws, "report_to_merge", "")] = ts
        elif kind == "review.started":
            end(r, "report_to_review")
            open_at[(ws, "review", r["agent_id"] or "")] = ts
        elif kind == "pipeline.review":
            payload = json.loads(r["payload"])
            end(r, "review", payload.get("actor") or "")
            if payload.get("approved") is False:
                open_at[(ws, "fix_round", "")] = ts
        elif kind == "pipeline.merged":
            end(r, "report_to_merge")
        if kind in ("pipeline.merged", "pipeline.removed"):
            open_at = {k: v for k, v in open_at.items() if k[0] != ws}
    for (day, repo, stage), new in samples.items():
        old = c.execute("SELECT samples FROM stage_timings WHERE day=? AND repo_root=? AND stage=?",
                        (day, repo, stage)).fetchone()
        values = (json.loads(old["samples"]) if old else []) + new
        c.execute("INSERT OR REPLACE INTO stage_timings VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (day, repo, stage, len(values), statistics.median(values),
                   _percentile(sorted(values), 0.9), json.dumps(values)))
    c.execute("DELETE FROM stage_open")
    c.executemany("INSERT INTO stage_open VALUES (?, ?, ?, ?)",
                  [(*k, v) for k, v in open_at.items()])


def prune(db: DB, older_than: float = RETENTION_SECONDS, now: float | None = None) -> int:
    """Delete events older than ``older_than`` seconds; returns how many.
    Their pipeline stage timings are rolled up first, in the same transaction
    (so concurrent sweeps count each event once); if that fails, nothing is
    deleted and the events wait for the next pass."""
    now = time.time() if now is None else now
    cutoff = now - older_than
    try:
        with db.tx() as c:
            _rollup(c, cutoff)
            return c.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
    except Exception:   # noqa: BLE001 - culling must never break its caller
        log.exception("brindle: couldn't roll up stage timings; keeping the events")
        return 0
