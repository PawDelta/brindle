"""Ordered delivery of one account's changes to a shared Library plan."""

import json
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from brindle import airgap, config
from brindle.db import DB
from brindle.pro.analysis_docs import Refused
from brindle.redact import mask, mask_data
from brindle.service.errors import ServiceError, network_error

current = ContextVar("plan_outbox", default=None)
_locks = [threading.Lock() for _ in range(64)]


class AccessLost(Refused):
    def __init__(self, refusal, receipts, unconfirmed):
        super().__init__(str(refusal), refusal.status, refusal.code)
        self.receipts = receipts
        self.unconfirmed = unconfirmed


@contextmanager
def writing(db, client_id, repo):
    legacy = client_id is None
    if client_id is None:
        client_id = uuid4().hex
    if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
        raise ServiceError("invalid", "The change needs a valid client id.")
    token = current.set((db, client_id, repo, legacy))
    try:
        yield
    finally:
        current.reset(token)


@contextmanager
def destination():
    if active := current.get():
        yield active
        return
    db = DB()
    try:
        yield db, uuid4().hex, None, True
    finally:
        db.conn.close()


def _matching_id(view, owner, tail, body):
    """Newest matching thread or reply. Older Libraries have no replay key."""
    matches = []
    for thread in view.get("threads") or []:
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), int):
            continue
        if "parent" in body:
            if thread.get("id") == body["parent"]:
                matches.extend(r["id"] for r in thread.get("replies") or []
                               if isinstance(r, dict) and isinstance(r.get("id"), int)
                               and r.get("author") == owner and r.get("text") == body["text"])
            continue
        if thread.get("author") != owner or thread.get("rev") != body["rev"]:
            continue
        anchor = thread.get("anchor") or {}
        if not isinstance(anchor, dict):
            continue
        if tail == "/suggestions":
            if (thread.get("kind") == "suggestion" and thread.get("after") == body["after"]
                    and all(anchor.get(k) == body[v] for k, v in
                            (("section", "section"), ("start", "start"), ("quote", "before")))):
                matches.append(thread["id"])
        elif (thread.get("kind") == "comment" and thread.get("text") == body["text"]
              and all(anchor.get(k) == v for k, v in body["anchor"].items())):
            matches.append(thread["id"])
    return max(matches, default=-1)


def _read_for_delivery(db, owner, doc, load):
    try:
        return load()
    except Refused as exc:
        if exc.code == "key_unavailable" or not (exc.status == 404 or (exc.status == 403 and exc.code)):
            raise
        with db.tx() as tx:
            tx.execute("UPDATE plan_outbox SET state='refused', code='forbidden' "
                       "WHERE owner=? AND doc=? AND state='waiting' AND attempted=0", (owner, doc))
            refused, unconfirmed = [], []
            for row in tx.execute(
                "SELECT state,client_id,tail,json_extract(payload,'$.rev') AS rev FROM plan_outbox "
                "WHERE owner=? AND doc=? AND state IN ('refused','waiting','unconfirmed') ORDER BY seq", (owner, doc)
            ):
                receipts = refused if row["state"] == "refused" else unconfirmed
                receipts.append({k: row[k] for k in ("client_id", "tail", "rev")})
        if refused or unconfirmed:
            raise AccessLost(exc, refused, unconfirmed) from exc
        raise


def deliver(db, owner, doc, send, load, *, item=None, read=False, legacy=False):
    """Persist before sending. A reconnect uses the same account, revision and key."""
    conn = db.conn
    if item:
        client_id, tail, body, repo = item
        conn.execute(
            "INSERT INTO plan_outbox(owner,doc,client_id,tail,payload,repo) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(owner,doc,client_id) DO NOTHING",
            (owner, doc, client_id, tail, json.dumps(mask_data(body)), repo),
        )
    with _locks[hash((db.path, owner, doc)) % len(_locks)]:
        view = _read_for_delivery(db, owner, doc, load) if read else None
        features = view.get("features", []) if view is not None else None
        for row in conn.execute("SELECT * FROM plan_outbox WHERE owner=? AND doc=? AND state IN ('waiting','unconfirmed') ORDER BY seq",
                                (owner, doc)).fetchall():
            body = mask_data(json.loads(row["payload"]))
            if row["repo"]:
                config.load_repo_config(row["repo"])
            if airgap.enabled():
                break
            tail = row["tail"]
            if features is None and (row["attempted"] or tail in ("/comments", "/suggestions")):
                try:
                    view = _read_for_delivery(db, owner, doc, load)
                except Exception as exc:
                    if network_error(exc):
                        break
                    raise
                features = view.get("features") or []
            try:
                already_saved = False
                prior_match = row["prior_match"]
                keyed = False
                if tail in ("/comments", "/suggestions"):
                    feature = "comment_idempotency" if tail == "/comments" else "suggestion_idempotency"
                    keyed = feature in features
                    match = _matching_id(view, owner, tail, body)
                    if not row["attempted"]:
                        prior_match = match
                    else:
                        already_saved = match > prior_match
                    if keyed:
                        body["idempotency_key"] = row["client_id"]
                elif row["attempted"] and tail == "/sign-offs":
                    already_saved = any(isinstance(s, dict) and s.get("sub") == owner and s.get("rev") == body["rev"]
                                        for s in view.get("sign_offs") or [])
                elif row["attempted"] and tail == "/votes" and view.get("rev") == body["rev"]:
                    already_saved = any(d.get("id") == body["decision"] and
                                        owner in (d.get("votes") or {}).get(body["option"], [])
                                        for d in view.get("decisions") or [] if isinstance(d, dict))
                if not already_saved and row["state"] == "unconfirmed":
                    # The replay was refused. Later reads may still confirm the original POST.
                    continue
                if not already_saved:
                    if not row["attempted"]:
                        # Record the replay evidence before the POST can reach the Library.
                        conn.execute("UPDATE plan_outbox SET attempted=1, prior_match=?, keyed=? WHERE seq=?",
                                     (prior_match, keyed, row["seq"]))
                    view = send(tail, body)
            except Exception as exc:
                if network_error(exc):
                    break
                if not isinstance(exc, Refused) or exc.status not in (400, 403, 404, 409, 422):
                    raise
                reason = {403: "forbidden", 404: "forbidden", 409: "stale"}.get(exc.status, "invalid")
                state = "unconfirmed" if row["attempted"] else "refused"
                conn.execute("UPDATE plan_outbox SET state=?, code=? WHERE seq=?", (state, reason, row["seq"]))
                if legacy and item and item[0] == row["client_id"]:
                    raise
            else:
                # Keep the receipt and its target. The delivered text belongs in the Library.
                target = {k: body[k] for k in ("decision", "rev", "option", "parent") if k in body}
                if tail in ("/comments", "/suggestions"):
                    target["thread_item"] = _matching_id(view, owner, tail, body)
                if tail in ("/votes", "/sign-offs"):
                    conn.execute("UPDATE plan_outbox SET state='sent',sent_at=0,payload='{}' "
                                 "WHERE owner=? AND doc=? AND tail=? AND seq<? AND state='refused' "
                                 "AND json_extract(payload,'$.decision') IS ? AND json_extract(payload,'$.rev') IS ?",
                                 (owner, doc, tail, row["seq"], body.get("decision"), body.get("rev")))
                conn.execute("UPDATE plan_outbox SET state='sent', sent_at=?, payload=? WHERE seq=?",
                             (time.time(), json.dumps(target), row["seq"]))
        rows = conn.execute("SELECT * FROM plan_outbox WHERE owner=? AND doc=? AND "
                            "(state!='sent' OR sent_at>?) ORDER BY seq", (owner, doc, time.time() - 3))
        items = []
        for row in rows:
            body = json.loads(row["payload"])
            for field in ("text", "before", "after"):
                if isinstance(body.get(field), str):
                    body[field] = mask(body[field])
            if isinstance(body.get("anchor"), dict):
                body["anchor"] = {k: body["anchor"][k] for k in ("section", "decision") if k in body["anchor"]}
            items.append({"client_id": row["client_id"], "tail": row["tail"], "state": row["state"],
                          "code": row["code"], "body": body})
        return view, items
