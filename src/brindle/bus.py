"""The local event bus: an append-only ``events`` table that publishers write
and readers tail by id. Rows are short-lived (see :func:`prune`, called by the
cull sweep). Publishing never raises: an event is a notification, and losing
one must not break the action that caused it."""

from __future__ import annotations

import json
import time

from brindle.db import DB

RETENTION_SECONDS = 24 * 3600
_COLUMNS = ("repo_root", "root_id", "agent_id", "workspace_id", "task_id")


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


def prune(db: DB, older_than: float = RETENTION_SECONDS, now: float | None = None) -> int:
    """Delete events older than ``older_than`` seconds; returns how many."""
    now = time.time() if now is None else now
    return db.conn.execute("DELETE FROM events WHERE ts < ?", (now - older_than,)).rowcount
