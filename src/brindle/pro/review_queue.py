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

log = logging.getLogger(__name__)

PATH = "/me/analysis-docs/review-requests"

_lock = threading.Lock()
_seen: dict[str, list[dict]] = {}      # account sub -> the queue as last read


def _items(raw: object) -> list[dict]:
    """The usable rows of the answer: a doc id, a title and the design's
    author (an account id), nothing else trusted."""
    rows = raw.get("requests") if isinstance(raw, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and isinstance(r.get("doc_id"), str) and r["doc_id"]:
            title, author = r.get("title"), r.get("author")
            count = r.get("decision_count")
            out.append({"doc_id": r["doc_id"], "title": title if isinstance(title, str) and title else "Design doc",
                        "decision_count": count if type(count) is int and count >= 0 else None,
                        "author": author if isinstance(author, str) and author else None})
    return out


def account(client=None, store=None) -> tuple[object, str, str | None] | None:
    """``(client, sub, token)`` for the signed-in account, all from one
    captured credential record, or None when air-gapped or logged out.
    Raises when the record can't be used (not entitled, refresh refused).

    The account is the ``sub`` of the entitlement signed into that record
    (verified, from its issuer), the plan check is on that entitlement, and
    the token is that record's own. An expired token is refreshed only if the
    store still holds exactly that record (``auth.refresh_captured``), and the
    account is read again from the refreshed record. No separate read of the
    store or of "who is signed in" can pair one account's answer with
    another's name."""
    import time

    from brindle import airgap
    from brindle.pro import analysis_docs as ad
    from brindle.pro import auth, license

    def owner(record: dict) -> str:
        ent = license.verify(record.get("entitlement") or "", issuer=record.get("base_url"), grace=0)
        if ad.FEATURE not in ent.features:
            raise license.NotEntitled("no analysis doc library")
        return ent.sub

    if airgap.enabled():
        return None
    client, store = ad._session(client, store)
    rec = store.load() or {}
    if not rec.get("refresh_token"):
        return None
    # A stored entitlement that has expired (the machine slept, the app was closed
    # for a while) is renewed from this record like an expired access token:
    # checked first, it failed every pass and the queue was never asked.
    try:
        sub = owner(rec)
        stale = float(rec.get("access_expires_at") or 0) <= time.time() + auth.ACCESS_SLACK
    except license.Expired:
        stale = True
    if stale:
        rec = auth.refresh_captured(store, rec, client.transport)
        sub = owner(rec)
    return client, sub, rec.get("access_token")


def poll(*, client=None, store=None) -> bool:
    """Read the signed-in account's queue (see ``account``) and remember it
    under that account. True when the queue read differs from the last one
    (the caller tells the app, which only re-reads its Inbox on an event);
    False when it is unchanged, or when logged out, offline, refused, not
    entitled or switched."""
    try:
        signed = account(client, store)
        if signed is None:
            return False
        client, sub, token = signed
        status, body = client.call("GET", PATH, None, token=token)
    except Exception:  # noqa: BLE001 - keep the cards we have; the next pass tries again
        log.debug("brindle: couldn't read the review queue", exc_info=True)
        return False
    if status != 200:
        return False
    items = _items(body)
    with _lock:
        changed = _seen.get(sub) != items
        _seen[sub] = items
    return changed


def waiting(sub: str | None) -> list[dict]:
    """What ``poll`` last read for this account (another account's queue is never shown)."""
    with _lock:
        return list(_seen.get(sub or "", []))


def clear() -> None:
    with _lock:
        _seen.clear()
