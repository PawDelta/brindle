"""Giving a person a plan without waiting for the Library (the share panel's
Add): the change is written to ``plan_invites`` and answered at once (202), and
the daemon lands it in the background with share_access.plan_write.

- Sent only once the app has the answer: each change has a ``receipt`` the
  202 carries, and it is sent only after the app sends that receipt back
  (``ack``). HTTP can't say whether an answer reached the app (a server
  writes to a closed connection without an error), so the app's own receipt
  is the confirmation. One it never got (the app went away, the request was
  cancelled), or whose receipt couldn't be sent back, is never sent by
  itself: it stays unconfirmed, a restart included, and the panel shows it
  as not sent, for the person to send (its receipt, explicitly) or discard
  (``dismiss``).
- One row per change, ``id`` never reused: a person's newest row is what the
  panel shows, and their newest acknowledged row is what is sent, so an
  acknowledged change is never held up by a newer one that isn't, and an
  answer to an older send never touches a newer change.
- Sent as the account that asked: each row keeps it (``owner``, ``org_id``),
  and the send runs with a credential store bound to it (``_Bound``): if a
  different account is signed in at any read of the credentials, a refresh
  included, nothing more is sent. A change to a pending person is made only
  by the account their change belongs to.
- Safe to send again: the plan keeps its one Library doc (shared_docs.ensure_doc
  finds the one an earlier create left), and the Library's share is per person.
- While the Library can't be reached, or is busy, it is tried again later with
  a growing wait (``due_at``), up to MOST_TRIES; a refusal is kept on the row
  (``failed``) until the person asks again (Try again).
- The rows survive a daemon restart: its watch (daemon.lifecycle) runs the due
  ones on every tick.

The Library's own access list stays the truth once a change lands: its rows
are gone then, and ``merge`` only adds what is still on its way.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from brindle.db import DB
from brindle.service.errors import ServiceError, from_exception

log = logging.getLogger(__name__)

BACKOFF_FIRST = 5.0         # seconds before the second attempt; doubled after each
BACKOFF_MOST = 300.0        # never longer between attempts
MOST_TRIES = 20             # about an hour and a quarter of no answer: then it is shown as failed

clock = time.time            # tests drive it

_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


class AccountChanged(ServiceError):
    def __init__(self) -> None:
        super().__init__("account_changed", "brindle is signed in to another account than the one that asked: "
                         "add them again to share it from this one", 409)


@contextmanager
def held(plan_id: str) -> Iterator[None]:
    """One plan's sends (and Stop sharing with everyone) one at a time."""
    with _locks_lock:
        lock = _locks.setdefault(plan_id, threading.Lock())
    with lock:
        yield


def backoff(tries: int) -> float:
    return min(BACKOFF_MOST, BACKOFF_FIRST * 2 ** max(0, tries - 1))


def _newest(c: sqlite3.Connection, plan_id: str, sub: str) -> dict | None:
    row = c.execute("SELECT * FROM plan_invites WHERE plan_id=? AND sub=? ORDER BY id DESC LIMIT 1",
                    (plan_id, sub)).fetchone()
    return None if row is None else dict(row)


def dto(row: dict) -> dict:
    """A person's newest change as the panel shows it: ``op`` invite or
    remove; ``state`` sharing or removing (on its way), or failed (refused:
    ``error`` says why); ``receipt`` while it isn't confirmed. The app sends
    the receipt back for an answer it got; one it reads here instead (its
    answer may never have arrived: the request was cancelled) is the
    person's to send or discard, never sent by itself."""
    state = "failed" if row["failed"] else "removing" if row["op"] == "remove" else "sharing"
    return {"sub": row["sub"], "name": None, "access": row["access"],
            "sign_off": "required" if row["sign_off"] else None, "op": row["op"], "state": state,
            "error": row["failed"] or row["error"], "receipt": None if row["acked"] else row["receipt"],
            "rev": row["id"]}


def pending(db: DB, plan_id: str) -> list[dict]:
    """Each person's newest change on the plan, in the order they were first made."""
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM plan_invites p WHERE plan_id=? AND id=(SELECT MAX(id) FROM plan_invites q"
        " WHERE q.plan_id=p.plan_id AND q.sub=p.sub) ORDER BY (SELECT MIN(id) FROM plan_invites q"
        " WHERE q.plan_id=p.plan_id AND q.sub=p.sub)", (plan_id,))]


def merge(access: dict, rows: list[dict]) -> dict:
    """The Library's access list with what is still on its way: an invite's
    person shows (at the access asked for) as sharing or failed; one being
    removed is left out, unless that removal was refused."""
    people = {p["sub"]: p for p in access.get("people") or []}
    for row in rows:
        shown = {k: v for k, v in dto(row).items() if k not in ("receipt", "rev")}
        if row["op"] == "remove" and not row["failed"]:
            if row["acked"]:            # an unconfirmed removal hasn't been asked for yet: they keep it
                people.pop(row["sub"], None)
            continue
        if row["sub"] in people:
            shown["name"] = people[row["sub"]].get("name")
            if row["op"] == "remove":           # what they have now, and the removal that was refused
                shown = {**people[row["sub"]], **{k: shown[k] for k in ("op", "state", "error")}}
        people[row["sub"]] = shown
    return {**access, "people": list(people.values())}


# -- the routes' side: written at once ----------------------------------------------------


def _claims(creds: dict | None) -> tuple[str, str | None] | None:
    from brindle.pro import license

    try:
        claims = license.verify_signed((creds or {}).get("entitlement") or "", typ=license.TYP,
                                       token_use=license.TOKEN_USE)
    except license.LicenseError:
        return None
    return claims.get("sub"), claims.get("org_id")


def _identity(store=None) -> tuple[str, str | None]:
    """The signed-in account and org, from the entitlement verified on this Mac (nothing is sent)."""
    from brindle.pro import analysis_docs as ad
    from brindle.pro import shared_docs as sd

    _, store = ad._session(None, store)
    ent = sd._require_team(store)
    return ent.sub, ent.org_id


def _insert(c: sqlite3.Connection, plan_id: str, sub: str, op: str, access: str, sign_off: int,
            owner: str, org_id: str | None) -> dict:
    """A new change for the person, carrying whether the Library may already hold a grant for them."""
    now = clock()
    sent = c.execute("SELECT MAX(sent) FROM plan_invites WHERE plan_id=? AND sub=?", (plan_id, sub)).fetchone()[0]
    c.execute("INSERT INTO plan_invites (plan_id, sub, receipt, op, access, sign_off, owner, org_id, sent, due_at,"
              " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (plan_id, sub, secrets.token_urlsafe(16), op, access, sign_off, owner, org_id, sent or 0, now, now))
    return _newest(c, plan_id, sub)


def record(db: DB, plan_id: str, sub: Any, access: Any, sign_off: Any, *, store=None) -> dict:
    """One person's invite to send, as the signed-in account; a second for
    the same person supersedes the first once acknowledged (Try again).
    Refused here, before anything is written, when the request itself is wrong."""
    from brindle import plan_docs
    from brindle.pro import share_access as sa
    from brindle.pro import shared_docs as sd

    sub, access = sa._sub(sub), sa._access(access)
    if not isinstance(sign_off, bool):
        raise ServiceError("invalid", "sign_off is true or false", 400)
    sd._review_open(plan_docs._plan(db, plan_id))
    owner, org_id = _identity(store)
    with db.tx() as c:
        return _insert(c, plan_id, sub, "invite", access, int(sign_off), owner, org_id)


def _asker(db: DB, plan_id: str, sub: str, store) -> tuple[str, str | None] | None:
    """The signed-in account, read before the write's transaction, which
    compares it with the row it then reads; None when nothing is out for
    ``sub`` (no sign-in needed then)."""
    if _newest(db.conn, plan_id, sub) is None:
        return None
    return _identity(store)


def _theirs(old: dict, who: tuple[str, str | None]) -> None:
    """Only the account a pending change belongs to changes it, compared on the row read in the write itself."""
    if (old["owner"], old["org_id"]) != who:
        raise AccountChanged()


def change(db: DB, plan_id: str, sub: str, field: str, value: Any, *, store=None) -> dict | None:
    """A change to a person whose invite is still out (``field`` access or
    sign_off), sent after it; None when nothing is out for them (the route
    writes it now)."""
    from brindle.pro import share_access as sa

    if field == "access":
        value = sa._access(value)
    elif not isinstance(value, bool):
        raise ServiceError("invalid", "must is true or false", 400)
    who = _asker(db, plan_id, sub, store)
    if who is None:
        return None
    with db.tx() as c:
        old = _newest(c, plan_id, sub)
        if old is None:
            return None             # it landed meanwhile: the route writes it now
        _theirs(old, who)
        if old["op"] == "remove":
            raise ServiceError("conflict", "they're being removed from this plan", 409)
        new = {**old, field: int(value) if field == "sign_off" else value}
        return _insert(c, plan_id, sub, "invite", new["access"], new["sign_off"], *who)


def withdraw(db: DB, plan_id: str, sub: str, *, store=None) -> dict | None:
    """Stop sharing with a person whose invite is still out: one the Library
    can't hold a grant from (never sent, nothing landed) is dropped here;
    otherwise it is removed, sent after what is out (a refused removal is
    asked again). None when nothing is out for them; ``{}`` when it was
    dropped; else the removal's row."""
    who = _asker(db, plan_id, sub, store)
    if who is None:
        return None
    with db.tx() as c:
        old = _newest(c, plan_id, sub)
        if old is None:
            return None
        _theirs(old, who)
        # Any of their changes may hold a grant: an older one being sent right now is marked on itself only.
        sent = c.execute("SELECT MAX(sent) FROM plan_invites WHERE plan_id=? AND sub=?", (plan_id, sub)).fetchone()[0]
        if old["op"] == "invite" and not sent:
            c.execute("DELETE FROM plan_invites WHERE plan_id=? AND sub=?", (plan_id, sub))
            return {}
        return _insert(c, plan_id, sub, "remove", old["access"], old["sign_off"], *who)


def dismiss(db: DB, plan_id: str, sub: str, rev: Any, *, store=None) -> None:
    """Let a refused change go (the Library keeps what it has), or discard
    an unconfirmed one (its answer may never have reached the app) with any
    older unconfirmed ones. ``rev`` is the change the panel showed
    (``dto``'s rev): refused when it isn't the person's newest change any
    more, so a newer one (another panel's, say, sent or refused since) is
    never let go by a request about an older one."""
    try:
        rev = int(rev)
    except (TypeError, ValueError):
        raise ServiceError("invalid", "rev is the change as the panel showed it", 400) from None
    who = _asker(db, plan_id, sub, store)
    with db.tx() as c:
        old = _newest(c, plan_id, sub)
        if old is None or not (old["failed"] or not old["acked"]):
            raise ServiceError("not_found", "nothing refused or unsent to let go for them", 404)
        _theirs(old, who)
        if rev != old["id"]:
            raise ServiceError("conflict", "they were changed again since: read the list again", 409)
        if old["failed"]:
            c.execute("DELETE FROM plan_invites WHERE plan_id=? AND sub=?", (plan_id, sub))
        else:
            c.execute("DELETE FROM plan_invites WHERE plan_id=? AND sub=? AND acked=0 AND id<=?",
                      (plan_id, sub, old["id"]))


def ack(db: DB, plan_id: str, receipts: Any) -> int:
    """The app has the answers that named ``receipts``: those changes may be
    sent now, each superseding the person's older ones. A receipt for a
    change already superseded, landed or dropped is let go. How many were new."""
    if not isinstance(receipts, list) or not all(isinstance(r, str) for r in receipts):
        raise ServiceError("invalid", "receipts is a list of the receipts the app was given", 400)
    done = 0
    with db.tx() as c:
        for receipt in receipts:
            row = c.execute("SELECT * FROM plan_invites WHERE plan_id=? AND receipt=? AND acked=0",
                            (plan_id, receipt)).fetchone()
            if row is None:
                continue
            older = c.execute("SELECT MAX(sent) FROM plan_invites WHERE plan_id=? AND sub=? AND id<?",
                              (plan_id, row["sub"], row["id"])).fetchone()[0]
            c.execute("UPDATE plan_invites SET acked=1, sent=MAX(sent, ?) WHERE id=?", (older or 0, row["id"]))
            c.execute("DELETE FROM plan_invites WHERE plan_id=? AND sub=? AND id<?", (plan_id, row["sub"], row["id"]))
            done += 1
    return done


def drop_all(db: DB, plan_id: str) -> None:
    """Stop sharing with everyone: nothing still out is sent after it (hold
    ``held(plan_id)``, so a change out now lands first and is undone with the rest)."""
    db.conn.execute("DELETE FROM plan_invites WHERE plan_id=?", (plan_id,))


# -- the daemon's side: sent in the background --------------------------------------------


def kick(plan_id: str) -> None:
    """After an acknowledgement's answer: send what is due on the plan now,
    with no request deadline. A failure is only logged, the row keeps it."""
    from brindle import spawn

    try:
        with spawn.after_response():
            db = DB()
            try:
                run(db, plan_id)
            finally:
                db.conn.close()
    except Exception:  # noqa: BLE001 - the row keeps what happened
        log.debug("brindle: couldn't send a plan's invites", exc_info=True)


_DUE = ("SELECT * FROM plan_invites p WHERE plan_id=? AND acked=1 AND failed IS NULL AND due_at<=?"
        " AND id=(SELECT MAX(id) FROM plan_invites q WHERE q.plan_id=p.plan_id AND q.sub=p.sub AND q.acked=1)"
        " ORDER BY id LIMIT 1")


def run(db: DB, plan_id: str | None = None, *, now: float | None = None, client=None, store=None) -> int:
    """Send the due changes, of ``plan_id`` or (the watch) every plan; how many landed."""
    now = clock() if now is None else now
    ids =[plan_id] if plan_id else [r[0] for r in db.conn.execute(
        "SELECT DISTINCT plan_id FROM plan_invites WHERE acked=1 AND failed IS NULL AND due_at<=?", (now,))]
    landed = 0
    for pid in ids:
        with held(pid):
            landed += _run_plan(db, pid, now, client, store)
    return landed


def _run_plan(db: DB, plan_id: str, now: float, client, store) -> int:
    landed = 0
    while True:
        row = db.conn.execute(_DUE, (plan_id, now)).fetchone()
        if row is None:
            return landed
        row = dict(row)
        with db.tx() as c:          # superseded since the read: the next one to send
            if not c.execute("UPDATE plan_invites SET tries=tries+1, sent=1 WHERE id=?", (row["id"],)).rowcount:
                continue
        try:
            _send(db, row, client, store)
        except Exception as e:  # noqa: BLE001 - every outcome is the row's
            again, why = _outcome(e)
            tries = row["tries"] + 1
            if again and tries < MOST_TRIES:
                db.conn.execute("UPDATE plan_invites SET error=?, due_at=? WHERE id=?",
                                (why, now + backoff(tries), row["id"]))
                _changed(db, plan_id)
                return landed       # the Library isn't answering: the plan's other changes wait too
            db.conn.execute("UPDATE plan_invites SET failed=?, error=NULL WHERE id=?", (why, row["id"]))
        else:
            landed += 1
            with db.tx() as c:
                # What the Library now holds for them, for any newer change still out: a grant after
                # an invite (so Stop sharing then is sent, never dropped), none after a removal.
                c.execute("UPDATE plan_invites SET sent=? WHERE plan_id=? AND sub=? AND id>?",
                          (int(row["op"] == "invite"), plan_id, row["sub"], row["id"]))
                c.execute("DELETE FROM plan_invites WHERE plan_id=? AND sub=? AND id<=?",
                          (plan_id, row["sub"], row["id"]))
        _changed(db, plan_id)


class _Bound:
    """The credential store, as the account a change belongs to: every read,
    and every write a refresh makes, first checks that it is still that
    account (and org) signed in, and stops with AccountChanged when not."""

    def __init__(self, store, owner: str, org_id: str | None) -> None:
        self._store, self._who = store, (owner, org_id)

    def _check(self, creds: dict | None) -> dict | None:
        if _claims(creds) != self._who:
            raise AccountChanged()
        return creds

    def load(self) -> dict | None:
        return self._check(self._store.load())

    def save(self, creds: dict) -> None:
        self._check(self._store.load())             # nobody signed in as someone else meanwhile
        if creds.get("entitlement"):
            self._check(creds)                      # and the refresh is for this account
        self._store.save(creds)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def _send(db: DB, row: dict, client, store) -> None:
    from brindle.pro import analysis_docs as ad
    from brindle.pro import share_access as sa
    from brindle.pro import shared_docs as sd

    plan_id, sub = row["plan_id"], row["sub"]
    from brindle.pro import auth

    client, store = ad._session(client, store)
    if auth.warm_for(store) is None:
        store = _Bound(store, row["owner"], row["org_id"])
    # else the daemon's warm sign-in pinned this view to the sign-in it holds now: every read, token
    # and refresh through it fails closed once that sign-in is replaced, from memory (no keychain, no
    # refresh lock), so the check below binds the whole send to the account that asked.
    if _identity(store) != (row["owner"], row["org_id"]):
        raise AccountChanged()
    if row["op"] == "remove":
        share = sd.link(db, plan_id)
        if share is None or share["doc_id"] is None:
            return                  # never shared: nobody to remove
        try:
            sa.plan_write(db, plan_id, lambda call: sa.revoke(call, sub), client=client, store=store)
        except ad.Refused as e:
            if e.status != 404:     # never had it: removed already
                raise
        return
    sa.plan_write(db, plan_id, lambda call: sa.invite(call, sub, row["access"], bool(row["sign_off"])),
                  first=True, client=client, store=store)


def _outcome(e: BaseException) -> tuple[bool, str]:
    """Whether ``e`` is worth sending again later (no answer, the Library or
    the sign-in busy) rather than a refusal, and why, as the person reads it."""
    from brindle import keystore, redact
    from brindle.daemon import routes_actions as actions
    from brindle.pro import analysis_docs as ad
    from brindle.pro import auth

    cause: BaseException | None = e
    while cause is not None:
        if isinstance(cause, AccountChanged):     # however deep a credential read raised it
            return False, cause.message
        cause = cause.__cause__ or cause.__context__
    if isinstance(e, ServiceError):
        err, again = e, e.http_status in (429, 502, 503, 504)
    elif isinstance(e, (RuntimeError, auth.AuthError, keystore.KeychainReadBlocked)):
        err = actions.upstream_error(e)
        if err.code == "busy" or isinstance(e, auth.TransportError):
            again = True
        elif isinstance(e, ad.Refused):
            again = e.status >= 500 or e.status in (401, 408, 429) or (e.status == 403 and not e.code)
        else:
            # No answer to a create, or a claim still out: unknown, so asked again (MOST_TRIES bounds it).
            again = not isinstance(e, auth.AuthError)
    else:
        err, again = from_exception(e), False
    return again, redact.mask(err.message)


def _changed(db: DB, plan_id: str) -> None:
    """Tell the app the plan's people changed, so an open share panel reads them again."""
    from brindle import plan_docs, plans

    try:
        p = plans.get(db, plan_id)
    except ServiceError:
        return
    plan_docs._changed(db, p, p["rev"])
