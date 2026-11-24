"""Giving a person a plan without waiting for the Library (the share panel's
Add): the invite is written to ``plan_invites`` and answered at once (202), and
the daemon lands it in the background with share_access.plan_write.

- Safe to send again: the plan keeps its one Library doc (shared_docs.ensure_doc
  finds the one an earlier create left), and a repeated invite for the same
  person updates their row, never a second one.
- While the Library can't be reached, or is busy, it is tried again later with
  a growing wait (``due_at``), up to MOST_TRIES; a refusal is kept on the row
  (``failed``) until the person asks again (Try again).
- A change to the same person while their invite is out (access, sign-off,
  Stop sharing) goes into the same row, with a new ``seq``: one plan's rows are
  sent one at a time under its lock, and an answer to an older ``seq`` never
  drops the newer change, so the changes land in the order they were made.
- The rows survive a daemon restart: its watch (daemon.lifecycle) runs the due
  ones on every tick.

The Library's own access list stays the truth once an invite lands: the row
is gone then, and ``merge`` only adds what is still on its way.
"""

from __future__ import annotations

import logging
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


@contextmanager
def held(plan_id: str) -> Iterator[None]:
    """One plan's invites (and Stop sharing with everyone) one at a time."""
    with _locks_lock:
        lock = _locks.setdefault(plan_id, threading.Lock())
    with lock:
        yield


def backoff(tries: int) -> float:
    return min(BACKOFF_MOST, BACKOFF_FIRST * 2 ** max(0, tries - 1))


def _row(db: DB, plan_id: str, sub: str) -> dict | None:
    row = db.conn.execute("SELECT * FROM plan_invites WHERE plan_id=? AND sub=?", (plan_id, sub)).fetchone()
    return None if row is None else dict(row)


def dto(row: dict) -> dict:
    """The row as the access list shows a person: ``state`` sharing (still
    being sent), removing, or failed (refused; ``error`` says why)."""
    state = "failed" if row["failed"] else "removing" if row["op"] == "remove" else "sharing"
    return {"sub": row["sub"], "name": None, "access": row["access"],
            "sign_off": "required" if row["sign_off"] else None, "state": state,
            "error": row["failed"] or row["error"]}


def pending(db: DB, plan_id: str) -> list[dict]:
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM plan_invites WHERE plan_id=? ORDER BY created_at, sub", (plan_id,))]


def merge(access: dict, rows: list[dict]) -> dict:
    """The Library's access list with what is still on its way: an invite's
    person shows (at the access asked for) as sharing or failed; one being
    removed is left out, unless that removal was refused."""
    people = {p["sub"]: p for p in access.get("people") or []}
    for row in rows:
        shown = dto(row)
        if row["op"] == "remove":
            if row["failed"]:
                people[row["sub"]] = {**people.get(row["sub"], shown), "state": "failed", "error": row["failed"]}
            else:
                people.pop(row["sub"], None)
            continue
        if row["sub"] in people:
            shown["name"] = people[row["sub"]].get("name")
        people[row["sub"]] = shown
    return {**access, "people": list(people.values())}


# -- the routes' side: written at once ----------------------------------------------------


def record(db: DB, plan_id: str, sub: Any, access: Any, sign_off: Any) -> dict:
    """Write one person's invite to send; a second for the same person
    replaces the first and asks again (Try again). Refused here, before
    anything is written, when the request itself is wrong."""
    from brindle import plan_docs
    from brindle.pro import share_access as sa
    from brindle.pro import shared_docs as sd

    sub, access = sa._sub(sub), sa._access(access)
    if not isinstance(sign_off, bool):
        raise ServiceError("invalid", "sign_off is true or false", 400)
    sd._review_open(plan_docs._plan(db, plan_id))
    now = clock()
    with db.tx() as c:
        c.execute("INSERT INTO plan_invites (plan_id, sub, access, sign_off, due_at, created_at)"
                  " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(plan_id, sub) DO UPDATE SET op='invite',"
                  " access=excluded.access, sign_off=excluded.sign_off, seq=seq+1, tries=0, due_at=excluded.due_at,"
                  " error=NULL, failed=NULL", (plan_id, sub, access, int(sign_off), now, now))
    return dto(_row(db, plan_id, sub))


def change(db: DB, plan_id: str, sub: str, *, access: Any = None, sign_off: Any = None) -> dict | None:
    """A change to a person whose invite is still out goes into that invite,
    sent after it; None when nothing is out for them (the route writes it now)."""
    from brindle.pro import share_access as sa

    if access is not None:
        access = sa._access(access)
    if sign_off is not None and not isinstance(sign_off, bool):
        raise ServiceError("invalid", "sign_off is true or false", 400)
    with db.tx() as c:
        row = c.execute("SELECT op FROM plan_invites WHERE plan_id=? AND sub=?", (plan_id, sub)).fetchone()
        if row is None:
            return None
        if row["op"] == "remove":
            raise ServiceError("conflict", "they're being removed from this plan", 409)
        c.execute("UPDATE plan_invites SET access=COALESCE(?, access), sign_off=COALESCE(?, sign_off), seq=seq+1,"
                  " tries=0, due_at=?, error=NULL, failed=NULL WHERE plan_id=? AND sub=?",
                  (access, None if sign_off is None else int(sign_off), clock(), plan_id, sub))
    return dto(_row(db, plan_id, sub))


def withdraw(db: DB, plan_id: str, sub: str) -> dict | None:
    """Stop sharing with a person whose invite is still out. One never sent
    (or a removal the Library refused) is dropped here; one that may have
    landed becomes a removal, sent after it. None when nothing is out for
    them; ``{}`` when it was dropped; else the row now."""
    with db.tx() as c:
        row = c.execute("SELECT * FROM plan_invites WHERE plan_id=? AND sub=?", (plan_id, sub)).fetchone()
        if row is None:
            return None
        if not row["sent"] or (row["op"] == "remove" and row["failed"]):
            c.execute("DELETE FROM plan_invites WHERE plan_id=? AND sub=?", (plan_id, sub))
            return {}
        c.execute("UPDATE plan_invites SET op='remove', seq=seq+1, tries=0, due_at=?, error=NULL, failed=NULL"
                  " WHERE plan_id=? AND sub=?", (clock(), plan_id, sub))
    return dto(_row(db, plan_id, sub))


def drop_all(db: DB, plan_id: str) -> None:
    """Stop sharing with everyone: nothing still out is sent after it (hold
    ``held(plan_id)``, so an invite out now lands first and is revoked with the rest)."""
    db.conn.execute("DELETE FROM plan_invites WHERE plan_id=?", (plan_id,))


# -- the daemon's side: sent in the background --------------------------------------------


def kick(plan_id: str) -> None:
    """After the route's answer is written (a BackgroundTask): send what is
    due on the plan now. Its own connection and no request deadline; a
    failure is only logged, the row keeps it."""
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


def run(db: DB, plan_id: str | None = None, *, now: float | None = None, client=None, store=None) -> int:
    """Send the due invites, of ``plan_id`` or every plan; how many landed."""
    now = clock() if now is None else now
    ids = [plan_id] if plan_id else [r[0] for r in db.conn.execute(
        "SELECT DISTINCT plan_id FROM plan_invites WHERE failed IS NULL AND due_at<=?", (now,))]
    landed = 0
    for pid in ids:
        with held(pid):
            landed += _run_plan(db, pid, now, client, store)
    return landed


def _run_plan(db: DB, plan_id: str, now: float, client, store) -> int:
    landed = 0
    while True:
        row = db.conn.execute("SELECT * FROM plan_invites WHERE plan_id=? AND failed IS NULL AND due_at<=?"
                              " ORDER BY created_at, sub LIMIT 1", (plan_id, now)).fetchone()
        if row is None:
            return landed
        row = dict(row)
        key = (plan_id, row["sub"], row["seq"])
        with db.tx() as c:          # a change since the read is the next one to send
            if not c.execute("UPDATE plan_invites SET tries=tries+1, sent=1 WHERE plan_id=? AND sub=? AND seq=?",
                             key).rowcount:
                continue
        try:
            _send(db, row, client, store)
        except Exception as e:  # noqa: BLE001 - every outcome is the row's
            again, why = _outcome(e)
            tries = row["tries"] + 1
            if again and tries < MOST_TRIES:
                db.conn.execute("UPDATE plan_invites SET error=?, due_at=? WHERE plan_id=? AND sub=? AND seq=?",
                                (why, now + backoff(tries), *key))
                _changed(db, plan_id)
                return landed       # the Library isn't answering: the plan's other rows wait too
            db.conn.execute("UPDATE plan_invites SET failed=?, error=NULL WHERE plan_id=? AND sub=? AND seq=?",
                            (why, *key))
        else:
            landed += 1
            db.conn.execute("DELETE FROM plan_invites WHERE plan_id=? AND sub=? AND seq=?", key)
        _changed(db, plan_id)


def _send(db: DB, row: dict, client, store) -> None:
    from brindle.pro import analysis_docs as ad
    from brindle.pro import share_access as sa
    from brindle.pro import shared_docs as sd

    plan_id, sub = row["plan_id"], row["sub"]
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
