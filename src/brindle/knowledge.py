"""The shared knowledge layer's store: events, intents and context receipts.

This module alone writes ``kb_events``, ``intents`` and ``context_receipts``.
``kb_events`` is append-only and the source of truth; ``intents`` is a
projection of it, kept current by :func:`append` and recreated from the log
by :func:`rebuild`. Every row is keyed by :func:`repo_key`, so two clones of
one repo share their knowledge and Team sync can replicate events by id.
"""

from __future__ import annotations

import json
import platform
import subprocess
import time
import uuid
from pathlib import Path

from brindle.db import DB
from brindle.pro.orgkey import GIT_TIMEOUT, normalize_remote

# An intent event announces (or replaces) what a session will change and makes
# it live; a pause event marks that session's intent paused.
INTENT = "intent"
PAUSE = "pause"
# A resume event makes a paused intent live again; an end event drops it.
RESUME = "resume"
END = "end"


def repo_key(repo_root: str | Path) -> str:
    """``host/owner/repo`` of the origin remote, else the repo's absolute path."""
    root = str(Path(repo_root).resolve())
    try:
        r = subprocess.run(["git", "-C", root, "config", "--get", "remote.origin.url"],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
        url = r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        url = ""
    return normalize_remote(url) or root


def append(db: DB, repo_key: str, kind: str, payload: dict, *, actor: str | None = None,
           session: str | None = None, machine: str | None = None,
           ts: float | None = None) -> str:
    """Record one event and apply it to the projections; returns its id."""
    event = {"id": uuid.uuid4().hex, "repo_key": repo_key, "kind": kind,
             "payload": json.dumps(payload), "actor": actor, "session": session,
             "machine": machine if machine is not None else platform.node(),
             "ts": ts if ts is not None else time.time()}
    with db.tx() as c:
        c.execute("INSERT INTO kb_events (id, repo_key, kind, payload, actor, session, machine, ts)"
                  " VALUES (:id, :repo_key, :kind, :payload, :actor, :session, :machine, :ts)",
                  event)
        _apply(c, event)
    return event["id"]


def rebuild(db: DB, repo_key: str) -> None:
    """Recreate ``repo_key``'s projection rows by replaying its events."""
    with db.tx() as c:
        c.execute("DELETE FROM intents WHERE repo_key = ?", (repo_key,))
        for event in c.execute("SELECT * FROM kb_events WHERE repo_key = ? ORDER BY ts, rowid",
                               (repo_key,)).fetchall():
            _apply(c, event)


def _apply(c, event) -> None:
    if not event["session"]:
        return
    payload = json.loads(event["payload"])
    if event["kind"] == INTENT:
        c.execute("INSERT OR REPLACE INTO intents (session_root, repo_key, summary, paths, status,"
                  " updated_at) VALUES (?, ?, ?, ?, 'live', ?)",
                  (event["session"], event["repo_key"], payload.get("summary", ""),
                   json.dumps(list(payload.get("paths", []))), event["ts"]))
    elif event["kind"] == PAUSE:
        c.execute("UPDATE intents SET status = 'paused', updated_at = ?"
                  " WHERE session_root = ? AND repo_key = ?",
                  (event["ts"], event["session"], event["repo_key"]))
    elif event["kind"] == RESUME:
        c.execute("UPDATE intents SET status = 'live', updated_at = ?"
                  " WHERE session_root = ? AND repo_key = ?",
                  (event["ts"], event["session"], event["repo_key"]))
    elif event["kind"] == END:
        c.execute("DELETE FROM intents WHERE session_root = ? AND repo_key = ?",
                  (event["session"], event["repo_key"]))


def note(db: DB, root_id: str, kind: str, payload: dict | None = None) -> None:
    """Record an event for session root ``root_id`` in its workspace's repo.
    Does nothing for a session whose root or workspace is gone."""
    root = db.get_agent(root_id)
    ws = db.get_workspace(root.workspace_id) if root else None
    if ws is not None:
        append(db, repo_key(ws.repo_root), kind, payload or {}, session=root_id)


def intents(db: DB, repo_key: str) -> list[dict]:
    """The sessions' announced intents for ``repo_key``, oldest first."""
    rows = db.conn.execute("SELECT * FROM intents WHERE repo_key = ? ORDER BY updated_at, session_root",
                           (repo_key,)).fetchall()
    return [{**dict(r), "paths": json.loads(r["paths"])} for r in rows]


def record_receipt(db: DB, task_id: str, items) -> None:
    """Record what a task's brief contained: ``(item_kind, item_id, content_hash)``
    triples."""
    with db.tx() as c:
        c.executemany("INSERT OR REPLACE INTO context_receipts (task_id, item_kind, item_id,"
                      " content_hash) VALUES (?, ?, ?, ?)",
                      [(task_id, kind, item_id, digest) for kind, item_id, digest in items])


def receipt(db: DB, task_id: str) -> list[tuple[str, str, str]]:
    """The ``(item_kind, item_id, content_hash)`` triples recorded for a task,
    in the order they were recorded."""
    rows = db.conn.execute("SELECT item_kind, item_id, content_hash FROM context_receipts"
                           " WHERE task_id = ? ORDER BY rowid", (task_id,)).fetchall()
    return [tuple(r) for r in rows]
