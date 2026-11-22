"""The team half of the Inbox (brindle Pro): what the Library's per-person
feed (``GET /me/inbox``, docs/shared-designs-api.md in the web repo) reports
for the signed-in person, with read state kept per person on the server.

The daemon's watch polls the feed next to the review queue (``poll``); the
Inbox shows sign-off progress and comment threads from it (``items``) and
takes each review request's sender and read state from it (``review_row``).
A row is marked read with ``POST /me/inbox/read`` (``mark_read``): the server
keeps that per account, so a row read on one machine is read on every other.
Nothing is stored here but the last feed read for each account (the pattern
of :mod:`brindle.pro.review_queue`): a failed poll keeps what was last read.

The server already makes one item per comment thread. Sign-offs come one per
signer; the Inbox shows one progress row per design, under a key of its own
(``signoffs:<doc_id>``) that ``mark_read`` expands to the sign-offs it covers.
Every change publishes ``inbox.changed``, so the app re-reads its Inbox."""

from __future__ import annotations

import logging
import threading

from brindle import bus
from brindle.db import DB
from brindle.service.dto import InboxItemDTO, PersonDTO, SignoffDTO

log = logging.getLogger(__name__)

FEED = "/me/inbox"
READ = "/me/inbox/read"
CHANGED = "inbox.changed"
NEEDS_YOU, FROM_TEAM = "needs_you", "from_team"
REVIEW, SIGNED, COMMENT = "review_requested", "signed_off", "shared_doc_comment"   # the feed's kinds
SHARED, ACCESS = "shared_with_you", "access_requested"
PROGRESS = "signoff_progress"       # the Inbox's own kind for a design's sign-offs ("signed_off" is the
                                    # author's local card once every sign-off is in)
NEW_KINDS = (PROGRESS, COMMENT, SHARED, ACCESS)     # Inbox kinds that only exist with the team Inbox
# Rows that ask nothing of the person: a teammate's action, or an admin's notice
# (the owner-approved Inbox lists notices under From the team). Every other kind
# waits on them, a review request among them: the plan can't go on until they review it.
TEAM_KINDS = (PROGRESS, COMMENT, SIGNED, SHARED, "changes_requested", "org_notice")
MAX_KEYS = 200                      # per POST /me/inbox/read

_lock = threading.Lock()
_seen: dict[str, list[dict]] = {}      # account sub -> the feed rows as last read
_read_versions: dict[str, int] = {}   # acknowledgements since a feed request began


def group(kind: str) -> str:
    """The Inbox group a row of ``kind`` belongs in."""
    return FROM_TEAM if kind in TEAM_KINDS else NEEDS_YOU


def person(raw: object) -> PersonDTO | None:
    """A feed person (``{sub, name}``), or a bare account id."""
    if isinstance(raw, str) and raw:
        return PersonDTO(sub=raw)
    if isinstance(raw, dict) and isinstance(raw.get("sub"), str) and raw["sub"]:
        name = raw.get("name")
        return PersonDTO(sub=raw["sub"], name=name if isinstance(name, str) and name else None)
    return None


def _int(raw: object, default: int = 0) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else default


def _rows(raw: object) -> list[dict]:
    """The usable feed rows: a known kind, a key and the doc they are about."""
    rows = raw.get("items") if isinstance(raw, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("kind") not in (REVIEW, SIGNED, COMMENT, SHARED, ACCESS):
            continue
        doc, key = r.get("doc_id"), r.get("item_key")
        if not isinstance(doc, str) or not doc or not isinstance(key, str) or not key:
            continue
        title = r.get("title")
        row = {"kind": r["kind"], "key": key, "doc_id": doc, "read": r.get("read") is True,
               "title": title if isinstance(title, str) and title else "Plan",
               "created_at": _int(r.get("created_at"))}
        if r["kind"] == ACCESS:
            if r.get("role") not in ("comment", "edit") or person(r.get("requester")) is None:
                continue
            row.update(requester=person(r["requester"]), role=r["role"])
        elif r["kind"] in (REVIEW, SHARED):
            row["sender"] = person(r.get("sender"))
            count = r.get("decision_count")
            row["decision_count"] = count if type(count) is int and count >= 0 else None
        elif r["kind"] == SIGNED:
            waiting = r.get("waiting")
            row.update(rev=_int(r.get("rev")), signer=person(r.get("signer")), signed=_int(r.get("signed")),
                       required=_int(r.get("required")),
                       waiting=[p for p in map(person, waiting if isinstance(waiting, list) else []) if p])
        else:
            if not isinstance(r.get("thread_id"), int):
                continue
            excerpt = r.get("excerpt")
            row.update(thread_id=r["thread_id"], suggestion=r.get("suggestion") is True,
                       author=person(r.get("author")), excerpt=excerpt if isinstance(excerpt, str) else "",
                       count=max(_int(r.get("count"), 1), 1))
        out.append(row)
    return out


def poll(db: DB, *, client=None, store=None) -> bool:
    """Read the signed-in account's feed (``review_queue.account``) and
    remember it under that account. True, with ``inbox.changed`` published,
    when it differs from the last read; a failed read keeps the last one."""
    from brindle.pro import review_queue

    try:
        signed = review_queue.account(client, store)
        if signed is None:
            return False
        client, sub, token = signed
        with _lock:
            version = _read_versions.get(sub, 0)
        status, body = client.call("GET", FEED, None, token=token)
    except Exception:  # noqa: BLE001 - offline or refused: keep what we have; the next pass tries again
        log.debug("brindle: couldn't read the team inbox", exc_info=True)
        return False
    if status != 200:
        return False
    rows = _rows(body)
    with _lock:
        # A feed captured before a successful read must not undo its acknowledgement.
        if version != _read_versions.get(sub, 0):
            return False
        changed = _seen.get(sub) != rows
        _seen[sub] = rows
    if changed:
        bus.publish(db, CHANGED)
    return changed


def _signoffs(rows: list[dict]) -> dict[str, list[dict]]:
    """Each design's sign-off rows, newest first. Times are whole seconds and the
    Library orders a tie by key, not by when each was signed: within one
    revision and second, the one with more signatures is the later."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        if r["kind"] == SIGNED:
            out.setdefault(r["doc_id"], []).append(r)
    for found in out.values():
        found.sort(key=lambda r: (-r["rev"], -r["created_at"], -r["signed"]))
    return out


def _progress_key(doc_id: str) -> str:
    return f"signoffs:{doc_id}"


def mark_read(db: DB, keys: list[str], *, client=None, store=None) -> list[str]:
    """Mark the Inbox rows ``keys`` (as ``items`` and ``review_row`` give them)
    read for the signed-in account, in the Library first: a progress row's key
    stands for every sign-off it covers. Returns the keys now read; the next
    read of the feed, on any machine, shows them read."""
    from brindle.pro import auth, review_queue
    from brindle.service.errors import ServiceError

    try:
        signed = review_queue.account(client, store)
    except Exception as e:  # noqa: BLE001 - a credential that can't be used is the person's to fix
        raise ServiceError("signed_out", "sign in to brindle Pro to mark Inbox rows read", 401) from e
    if signed is None:
        raise ServiceError("signed_out", "sign in to brindle Pro to mark Inbox rows read", 401)
    client, sub, token = signed
    with _lock:
        groups = {_progress_key(doc): [r["key"] for r in found]
                  for doc, found in _signoffs(_seen.get(sub, [])).items()}
    wanted = {k: groups.get(k, [k]) for k in dict.fromkeys(keys)}
    server = list(dict.fromkeys(s for found in wanted.values() for s in found))
    marked: set[str] = set()
    for i in range(0, len(server), MAX_KEYS):
        try:
            status, body = client.call("POST", READ, auth.JSONBody({"item_keys": server[i:i + MAX_KEYS]}), token=token)
        except Exception as e:  # noqa: BLE001 - offline: nothing is marked here either
            raise ServiceError("unavailable", "couldn't reach the Library to mark Inbox rows read", 503) from e
        if status != 200:
            raise ServiceError("unavailable", f"the Library didn't take the read marks ({status})", 502)
        read = body.get("read") if isinstance(body, dict) else None
        marked.update(k for k in (read if isinstance(read, list) else []) if isinstance(k, str))
    with _lock:
        _read_versions[sub] = _read_versions.get(sub, 0) + 1
        for r in _seen.get(sub, []):
            if r["key"] in marked:
                r["read"] = True
    bus.publish(db, CHANGED)
    return [k for k, found in wanted.items() if found and all(s in marked for s in found)]


def review_row(sub: str | None, doc_id: str) -> dict | None:
    """The feed's review request for ``doc_id``: its key, sender and read state."""
    with _lock:
        return next((dict(r) for r in _seen.get(sub or "", [])
                     if r["kind"] == REVIEW and r["doc_id"] == doc_id), None)


def items(sub: str | None) -> list[InboxItemDTO]:
    """Sign-off progress and comment threads for ``sub``'s Inbox, from the
    last feed read, newest first. Nothing for no account, or one never read."""
    if not sub:
        return []
    with _lock:
        rows = [dict(r) for r in _seen.get(sub, [])]
    out: list[tuple[int, InboxItemDTO]] = []
    for doc, found in _signoffs(rows).items():
        newest = found[0]
        out.append((max(r["created_at"] for r in found), InboxItemDTO(
            kind=PROGRESS, reason=f"{newest['signed']} of {newest['required']} signed off", inbox_group=FROM_TEAM,
            doc=doc, detail=newest["title"], from_person=newest["signer"], item_key=_progress_key(doc),
            read=all(r["read"] for r in found),
            signoff=SignoffDTO(rev=newest["rev"], signed=newest["signed"], required=newest["required"],
                               waiting=newest["waiting"]))))
    for r in rows:
        if r["kind"] in (SHARED, ACCESS):
            access = r["kind"] == ACCESS
            out.append((r["created_at"], InboxItemDTO(
                kind=r["kind"], reason=f"Asked to {r['role']}" if access else "Shared with you",
                inbox_group=NEEDS_YOU if access else FROM_TEAM, doc=r["doc_id"], detail=r["title"],
                from_person=r["requester"] if access else r["sender"], item_key=r["key"], read=r["read"],
                access_role=r["role"] if access else None)))
            continue
        if r["kind"] != COMMENT:
            continue
        what = "Suggested" if r["suggestion"] else "Commented"
        out.append((r["created_at"], InboxItemDTO(
            kind=COMMENT, reason=what if r["count"] == 1 else f"{r['count']} comments", inbox_group=FROM_TEAM,
            doc=r["doc_id"], detail=r["title"], from_person=r["author"], item_key=r["key"], read=r["read"],
            thread_id=r["thread_id"], excerpt=r["excerpt"])))
    out.sort(key=lambda t: -t[0])
    return [i for _, i in out]


def clear() -> None:
    with _lock:
        _seen.clear()
        _read_versions.clear()
