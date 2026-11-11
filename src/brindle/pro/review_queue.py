"""The Library's per-person review queue (``GET /me/analysis-docs/review-requests``):
the designs waiting on this account, whether or not this machine has a copy of
the plan. The daemon's watch loop polls it (``poll``); the Inbox shows each
item as a "Review requested" card (``waiting``). The queue is the server's, so
a card clears when its item leaves the queue. A failed poll (offline, logged
out, refused) keeps what was last read and says nothing: this runs in the
background and never prompts for credentials."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

PATH = "/me/analysis-docs/review-requests"

_lock = threading.Lock()
_seen: dict[str, list[dict]] = {}      # account sub -> the queue as last read


def _items(raw: object) -> list[dict]:
    """The usable rows of the answer: a doc id and a title, nothing else trusted."""
    rows = raw.get("requests") if isinstance(raw, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and isinstance(r.get("doc_id"), str) and r["doc_id"]:
            title = r.get("title")
            out.append({"doc_id": r["doc_id"], "title": title if isinstance(title, str) and title else "Design doc"})
    return out


def poll(who: Callable[[], str | None], *, client=None, store=None) -> bool:
    """Read the queue for the signed-in account (``who`` names it) and remember
    it. The request is bound to one captured credential record: the record is
    read, the account named, and the record read again (it must be unchanged,
    so ``sub`` is that record's owner). The request then carries that record's
    own token, and an expired token is refreshed only if the store still holds
    exactly that record (``auth.refresh_captured``), so a switch of account
    while it runs can neither change who asks nor file the answer under
    someone else. False (nothing changed) when logged out, offline, refused or
    switched."""
    import time

    from brindle.pro import analysis_docs as ad
    from brindle.pro import auth

    try:
        client, store = ad._session(client, store)
        ad.require_entitlement(store)
        rec = store.load() or {}
        sub = who()
        if not sub or not rec.get("refresh_token") or (store.load() or {}) != rec:
            return False
        if float(rec.get("access_expires_at") or 0) <= time.time() + auth.ACCESS_SLACK:
            rec = auth.refresh_captured(store, rec, client.transport)
        status, body = client.call("GET", PATH, None, token=rec.get("access_token"))
    except Exception:  # noqa: BLE001 - keep the cards we have; the next pass tries again
        log.debug("brindle: couldn't read the review queue", exc_info=True)
        return False
    if status != 200:
        return False
    with _lock:
        _seen[sub] = _items(body)
    return True


def waiting(sub: str | None) -> list[dict]:
    """What ``poll`` last read for this account (another account's queue is never shown)."""
    with _lock:
        return list(_seen.get(sub or "", []))


def clear() -> None:
    with _lock:
        _seen.clear()
