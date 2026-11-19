"""The team half of the Inbox (brindle Pro, Team plan): sign-off progress and
shared-doc comments the Library reports for the signed-in person's own docs,
with read state kept per person.

The Library's per-person feed (``GET /me/inbox``) is the source. Its rows are
read-only until the person marks one read (``POST /me/inbox/read``), and the
Library keeps that state per account, so a row read on one machine is read on
every other. Nothing here is stored on this machine, apart from the last feed
read for each account (the same pattern as :mod:`brindle.pro.review_queue`):
a failed poll keeps what was last read.

Comments on one thread are one row: the key is the thread, so a second comment
on it updates the row rather than adding one. Every change publishes
``inbox.changed`` on the bus, so the app re-reads its Inbox at once."""

from __future__ import annotations

import logging
import threading

from brindle import bus
from brindle.db import DB
from brindle.service.dto import InboxItemDTO, PersonDTO

log = logging.getLogger(__name__)

FEED = "/me/inbox"
READ = "/me/inbox/read"
CHANGED = "inbox.changed"
NEEDS_YOU, FROM_TEAM = "needs_you", "from_team"
SIGNOFF, COMMENT = "signoff_progress", "shared_doc_comment"
KINDS = (SIGNOFF, COMMENT)     # review requests come from the review queue (pro/review_queue), with their sender

_lock = threading.Lock()
_seen: dict[str, list[dict]] = {}      # account sub -> the feed rows as last read


def _person(raw: object) -> PersonDTO | None:
    if isinstance(raw, dict) and isinstance(raw.get("id"), str) and raw["id"]:
        name = raw.get("name")
        return PersonDTO(id=raw["id"], name=name if isinstance(name, str) and name else raw["id"])
    return None


def _rows(raw: object) -> list[dict]:
    """The usable feed rows: a known kind, a key, and the doc they are about."""
    rows = raw.get("items") if isinstance(raw, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("kind") not in KINDS:
            continue
        doc, key = r.get("doc_id"), r.get("key")
        if not isinstance(doc, str) or not doc or not isinstance(key, str) or not key:
            continue
        title = r.get("title")
        out.append({"kind": r["kind"], "key": key, "doc_id": doc, "thread_id": r.get("thread_id"),
                    "title": title if isinstance(title, str) and title else "Design doc",
                    "from": _person(r.get("from")), "signed": r.get("signed"), "required": r.get("required"),
                    "comments": r.get("comments") if isinstance(r.get("comments"), int) else 1,
                    "read": bool(r.get("read")), "updated_at": float(r.get("updated_at") or 0)})
    return out


def fetch(call, token: str | None) -> list[dict]:
    """The person's feed, as ``call("GET", FEED, None, token=...)`` returns it.
    Raises when the Library can't be read."""
    status, body = call("GET", FEED, None, token=token)
    if status != 200:
        raise RuntimeError(f"the Library's inbox feed answered {status}")
    return _rows(body)


def poll(db: DB, sub: str, call, token: str | None) -> bool:
    """Read the account's feed and remember it. True when it changed; a failed
    read keeps the last one and says nothing."""
    try:
        rows = fetch(call, token)
    except Exception:  # noqa: BLE001 - offline or refused: keep what we have; the next pass tries again
        log.debug("brindle: couldn't read the team inbox", exc_info=True)
        return False
    with _lock:
        changed = _seen.get(sub) != rows
        _seen[sub] = rows
    if changed:
        bus.publish(db, CHANGED, sub=sub)
    return changed


def mark_read(db: DB, sub: str, key: str, call, token: str | None) -> None:
    """Mark one feed row read for ``sub`` in the Library, then here, and tell
    the apps. Raises when the Library refuses it, so nothing is marked locally."""
    status, _ = call("POST", READ, {"key": key}, token=token)
    if status != 200:
        raise RuntimeError(f"the Library didn't take the read mark ({status})")
    with _lock:
        for r in _seen.get(sub, []):
            if r["key"] == key:
                r["read"] = True
    bus.publish(db, CHANGED, sub=sub)


def _grouped(rows: list[dict]) -> list[dict]:
    """One row per comment thread: the newest comment's row, carrying the count
    of comments on it. Other kinds pass through unchanged."""
    threads: dict[str, dict] = {}
    out = []
    for r in rows:
        if r["kind"] != COMMENT:
            out.append(r)
            continue
        key = f"comment:{r['doc_id']}:{r['thread_id'] or r['key']}"
        if key not in threads:
            threads[key] = {**r, "key": key}
            out.append(threads[key])
            continue
        kept = threads[key]
        kept["comments"] += r["comments"]
        if r["updated_at"] >= kept["updated_at"]:   # the newest comment decides who sent it, and read state
            kept.update({"from": r["from"], "updated_at": r["updated_at"], "read": r["read"], "title": r["title"]})
    return out


def items(sub: str | None) -> list[InboxItemDTO]:
    """The team rows for ``sub``'s Inbox, from the last feed read. Nothing for
    no account, or one never read."""
    if not sub:
        return []
    with _lock:
        rows = [dict(r) for r in _seen.get(sub, [])]
    out = []
    for r in _grouped(rows):
        base = {"doc": r["doc_id"], "detail": r["title"], "from_person": r["from"], "read": r["read"],
                "item_key": r["key"], "section": FROM_TEAM}
        if r["kind"] == SIGNOFF:
            out.append(InboxItemDTO(kind=SIGNOFF, reason=f"{r['signed']} of {r['required']} required", **base))
        else:
            reason = "Commented" if r["comments"] == 1 else f"{r['comments']} comments"
            out.append(InboxItemDTO(kind=COMMENT, reason=reason, **base))
    return out


def clear() -> None:
    with _lock:
        _seen.clear()
