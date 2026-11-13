"""Shared design docs (brindle Pro, Team plan and up): a plan doc
(:mod:`brindle.plan_docs`) in the Library, where teammates on other machines
read it, comment, suggest, vote and sign off. The contract is
docs/shared-designs-api.md in pawdelta-web.

:func:`share` saves the plan to the person's Library (the default ``Designs``
folder, unless a folder is named) and shares it with a team or the org.
:func:`set_reviewers` edits the required and optional reviewers afterwards.
:func:`sync` brings comments, suggestions, votes and the review state back,
then sends the plan's newest revision and what the person did on the doc.

The Library numbers its own revisions, so ``plan_shares`` keeps which local
revision its head holds. Everything pulled is stored under ``teammate:<sub>``,
whoever wrote it: a pulled row is never the local person's, so a vote from the
Library can't become their confirmed pick. A teammate's open comments and
suggestions are how they ask for changes (the API has no verdict of its own):
Request changes carries them to the supervisor (:func:`plan_docs.pending`).

When a teammate with edit rights changes the text, an open plan that hasn't
changed here takes the Library's head as a new revision. If the plan changed
here too nothing is merged or sent: the build gate's state is ``conflict``
(:func:`plans._gate`) until the Library's head is a revision this plan has.

Once the Library's document has a live draft that can't happen to the
person's writing. This machine keeps a replica of the draft
(:mod:`brindle.plan_drafts`), made once from the Library's snapshot, and the
person's direct edits and accepted suggestions go into it, not into
revisions (:func:`plan_docs.edit`, :func:`plan_docs.accept`). A sync trades
Loro updates with the relay (a :class:`DraftLink` in ``relays``): first what teammates
typed, then whatever the relay's version lacks from here, worked out afresh
each time, so nothing unsent is lost while there is no link. The text is
never sent as a revision then: the Library refuses one once a draft exists,
and only a freeze there makes a version, which comes in by :func:`_import`
like a teammate's. Revisions made here that the Library never took when its
draft arrives become suggestions on its head (:func:`_carry_over`).
Comments and suggestions stay threads, placed in the draft's text with the
Loro cursor they were made with, which goes up with them; the draft's
updates go first, so what they name is there. A suggestion accepted in the
draft goes up as ``DRAFT_ACCEPT``, which marks it and applies no text.

What the person writes after sharing goes up as theirs: comments, replies,
suggestions, thread actions and votes. What they wrote before stays here. A
suggestion they accepted goes up as the Library's own ``accept``, which makes
the revision and closes the thread together.

When the Team plan the doc was shared under ends, it becomes a personal doc: no
build gate and no sync, what teammates wrote stays readable, and resubscribing
picks the same Library document back up. Only an entitlement the server issues
now, for the account and org that shared it, can say so (:func:`_note_ended`),
and only while that answer is recent and they are still signed in
(:func:`lapse_stands`).

When the Library can't confirm a review, approving is refused with a typed
reason (:func:`why_unconfirmed`) and the Approve is held until a background
pass hears from it (:func:`plans.settle_held_approval`). The account that
shared a plan that never had required reviewers or org rules may stop sharing
it instead (:func:`stop_sharing`); the Library hears at its next pass.

Calls raise ``RuntimeError`` with a readable reason, as ``brindle docs`` does.
"""

from __future__ import annotations

import base64
import binascii
import difflib
import json
import logging
import re
import time
from typing import Any, Protocol
from urllib.parse import urlencode

from brindle import plan_docs, plans, visuals
from brindle.crdt import CrdtError
from brindle.db import DB
from brindle.plan_drafts import DraftReplica
from brindle.pro import analysis_docs as ad
from brindle.pro import review_policies
from brindle.redact import mask, mask_data
from brindle.service.errors import ServiceError

log = logging.getLogger(__name__)

TEAM_FEATURE = "analysis_docs_team"
NO_FEATURE = "entitlement_required"     # the Library's refusal when the plan lacks a feature
LAPSE_GOOD_FOR = 300.0       # seconds the server's word that a Team plan ended lets work go with no gate
LAPSE_CHECK_AFTER = 120.0    # the background pass asks again once that word is this old
SCOPE_KINDS = ("group", "org")
CLAIM_FOR = 60.0             # seconds a create or push in flight holds the plan; then it is presumed dead
FIRST_REVISION = 1           # what the Library numbers a new document's head
REVIEW = ("rev", "reviewers", "sign_offs", "pending_reviewers", "ready", "policy_reviewers", "pending_policies")
STATUSES = ("open", "resolved", "accepted", "rejected")
REVIEWER_GRANT = {"can_suggest": True, "can_comment": True}   # a reviewer must be able to answer
SENDING = "sending"          # plan_share_threads.status of a write sent, its answer not yet back (remote_id: -local)
# What the person's change to a thread is called in the Library. Not an accept:
# that is sent only on the text it was made on (_send_revisions), or, on a
# live draft, as DRAFT_ACCEPT, whose text went with the draft's updates.
THREAD_ACTIONS = {("comment", "resolved"): "resolve", ("comment", "open"): "reopen",
                  ("suggestion", "rejected"): "reject"}
DRAFT_ACCEPT = {"action": "accept", "in_draft": True}   # mark it accepted; the Library applies no text
MAX_CURSOR = 512             # a pulled anchor's Loro cursor, base64
_TOKENS = re.compile(r"\w+|\W")   # words, and each other character: what changes carried over are made of


def link(db: DB, plan_id: str) -> dict | None:
    """The plan's row in ``plan_shares`` (``review`` decoded), or None."""
    row = db.conn.execute("SELECT * FROM plan_shares WHERE plan_id=?", (plan_id,)).fetchone()
    return None if row is None else {**dict(row), "review": json.loads(row["review"])}


def _require_team(store):
    ent = ad.require_entitlement(store)
    if TEAM_FEATURE not in ent.features:
        raise RuntimeError("sharing a design doc needs a Team plan (the `analysis_docs_team` entitlement)")
    return ent


def _path(doc_id: str, tail: str = "") -> str:
    return ad._doc_path(doc_id, f"/design{tail}")


def _shared(db: DB, plan_id: str, store) -> dict:
    """The plan's link, for the account that shared it and nobody else."""
    share = link(db, plan_id)
    if share is None or share["doc_id"] is None:
        raise RuntimeError("this plan hasn't been shared")
    _own(share, _require_team(store))
    return share


def _own(share: dict, ent) -> None:
    if share["owner"] != ent.sub:
        raise RuntimeError("this plan was shared from another brindle account: log in to that one")


def _issued(client, store):
    """The entitlement the server issues now, verified with no grace, or None
    however that fails. What is stored can't prove a plan ended: it may be
    old, and going offline must not turn it into a way past a review."""
    from brindle import airgap
    from brindle.pro import auth

    if airgap.enabled():
        return None
    try:
        return auth.refresh(client, store)
    except Exception:  # noqa: BLE001 - offline, signed out, unverifiable: it proves nothing, so the gate stays
        log.debug("brindle: no fresh entitlement to check a shared plan against", exc_info=True)
        return None


def _ended_for(share_, ent) -> bool:
    """Whether verified entitlement ``ent`` is the account and org that shared
    the plan, without Team. Another account, or the same person in another
    org, says nothing about this one's plan; a share with no org on record
    can't match."""
    return TEAM_FEATURE not in ent.features and (ent.sub, ent.org_id) == (share_["owner"], share_["org_id"])


def _confirmed(share_) -> bool:
    """Whether the server said recently that the plan's Team plan has ended."""
    return share_["lapsed_at"] is not None and time.time() - share_["lapsed_at"] < LAPSE_GOOD_FOR


def lapse_stands(share_, p: dict) -> bool:
    """Whether a lapse on record lets plan ``p`` go without its gate now, which
    every look at the gate asks (plans._gate), so no path releases work on
    an old answer. It takes a recent confirmation from the server (a failed
    check withdraws it, and one nobody renews runs out) and that account and
    org still signed in, per the signed entitlement held here. A plan already
    building got there on a confirmation of its own, and has nothing to release."""
    if share_["lapsed_at"] is None:
        return False
    if p["state"] == "approved":
        return True
    if not _confirmed(share_):
        return False
    from brindle.pro import license

    try:
        ent = license.current(refresh=False)
    except Exception:  # noqa: BLE001 - nothing verified to go on: the gate stays
        return False
    return not ent.in_grace and _ended_for(share_, ent)


def _note_ended(db: DB, p: dict, ent) -> bool:
    """Record the server's latest word: whether ``ent`` (from :func:`_issued`,
    or None when it couldn't be had) says the Team plan behind a share has
    ended. It is an answer about an account and org, not one document, so it
    goes for ``p`` and every plan in review: anything short of ended withdraws
    an earlier confirmation, and those gates are back until the server says
    so again. Returns whether ``p``'s Team plan has ended."""
    rows = db.conn.execute("SELECT s.* FROM plan_shares s JOIN plans p ON p.id=s.plan_id"
                           " WHERE s.plan_id=? OR p.state IN ('open','redirected')", (p["id"],)).fetchall()
    now, verdicts = time.time(), [(r, ent is not None and _ended_for(r, ent)) for r in rows]
    with db.tx() as c:
        c.executemany("UPDATE plan_shares SET lapsed_at=? WHERE plan_id=?",
                      [(now if ended else None, r["plan_id"]) for r, ended in verdicts])
    for r, ended in verdicts:
        if ended != _confirmed(r):
            changed = plans.get(db, r["plan_id"])       # to say it changed: no acting in that session, so no lock
            plan_docs._changed(db, changed, changed["rev"])
    return any(ended for r, ended in verdicts if r["plan_id"] == p["id"])


def _team_active(db: DB, p: dict, store) -> None:
    """The Library answered this account about the plan: a lapse is over, and
    the org is on record for a share made before it was kept."""
    org_id = _require_team(store).org_id
    with db.tx() as c:
        c.execute("UPDATE plan_shares SET org_id=? WHERE plan_id=? AND org_id IS NULL", (org_id, p["id"]))
        resumed = c.execute("UPDATE plan_shares SET lapsed_at=NULL WHERE plan_id=? AND lapsed_at IS NOT NULL",
                            (p["id"],)).rowcount
    if resumed:
        plan_docs._changed(db, p, p["rev"])


def _review_open(p: dict) -> None:
    """Review closes once building starts: reviewers and sign-offs are settled."""
    if p["state"] == "approved":
        raise ServiceError("review_closed", "this plan is building, so its review is closed", 409)


def _content(rev: dict) -> dict:
    """A revision (or the Library's view of one) as the API's structured
    content: exactly its fields, because it refuses any others."""
    if not rev["sections"]:
        raise RuntimeError("a plan needs at least one ## section to be shared")
    return {
        "lede": rev["lede"],
        "sections": [{k: s.get(k) for k in ("id", "title", "kind", "markdown", "caption")} for s in rev["sections"]],
        "decisions": [{**{k: d.get(k) for k in ("id", "title", "mine", "challenge")},
                       "options": [{"id": o["id"], "label": o["label"], "detail": o.get("detail"),
                                    "pros": o.get("pros") or [], "cons": o.get("cons") or [],
                                    "touches": o.get("touches") or []} for o in d["options"]]}
                      for d in rev["decisions"]],
        "split": [{"id": w["id"], "title": w["title"], "after": w.get("after") or [], "files": w.get("files") or [],
                   "test": w.get("test")} for w in rev["split"]],
        "ask": rev.get("ask"),
    }


def _scope(raw: Any) -> dict:
    if not isinstance(raw, dict) or raw.get("kind") not in SCOPE_KINDS or not isinstance(raw.get("id"), str):
        raise RuntimeError("share with a team or the org: {kind: group|org, id}")
    return {"kind": raw["kind"], "id": raw["id"]}


def _reviewers(raw: Any) -> list[dict]:
    """``[{sub, required?}]``: required unless it says otherwise."""
    if not isinstance(raw, list) or not all(isinstance(r, dict) and r.get("sub") and isinstance(r["sub"], str)
                                            for r in raw):
        raise RuntimeError("reviewers is a list of {sub, required?}")
    return [{"sub": r["sub"], "required": bool(r.get("required", True))} for r in raw]


def _remember(db: DB, plan_id: str, view: dict) -> bool:
    """Keep the review state the Library answered with. True if it changed."""
    kept = {k: view.get(k) for k in REVIEW}
    review = json.dumps(kept, sort_keys=True)
    whole = plans._review_rows(kept) is not None        # only a whole answer takes back an access refusal
    with db.tx() as c:
        c.execute("UPDATE plan_shares SET unconfirmed=NULL, access_refused=CASE WHEN ? THEN NULL ELSE access_refused"
                  " END WHERE plan_id=?", (int(whole), plan_id))
        return c.execute("UPDATE plan_shares SET review=? WHERE plan_id=? AND review<>?",
                         (review, plan_id, review)).rowcount == 1


def _grant(db: DB, p: dict, doc_id: str, reviewers: list[dict], permissions: dict | None, client, store) -> None:
    """Scope members start read-only, and ``PUT /permissions`` replaces the
    list: add to what is there, giving each reviewer the right to comment and
    suggest. A reviewer taken off the list keeps the grant."""
    held = review_policies.ask(db, p, "GET", _path(doc_id), client=client, store=store).get("permissions") or {}
    grants = {**held, **(permissions or {})}
    for r in reviewers:
        grants[r["sub"]] = {"can_edit": False, **grants.get(r["sub"], {}), **REVIEWER_GRANT}
    if grants != held:
        review_policies.ask(db, p, "PUT", _path(doc_id, "/permissions"), {"permissions": grants},
                            client=client, store=store)


# -- sharing -----------------------------------------------------------------------------


def _claim_new(db: DB, plan_id: str, owner: str, org_id: str, now: float) -> tuple[dict, bool]:
    """The plan's link, claimed if the Library has no document for it yet: a
    second share while the first is creating it is refused, so one plan never
    becomes two documents. A claim older than CLAIM_FOR is taken over, and
    the second value says so: that create may have reached the Library."""
    with db.tx() as c:
        resumed = c.execute("UPDATE plan_shares SET claimed_at=? WHERE plan_id=? AND doc_id IS NULL AND claimed_at<?",
                            (now, plan_id, now - CLAIM_FOR)).rowcount == 1
        won = c.execute("INSERT OR IGNORE INTO plan_shares (plan_id, owner, org_id, claimed_at) VALUES (?,?,?,?)",
                        (plan_id, owner, org_id, now)).rowcount == 1
    share = link(db, plan_id)
    if share["doc_id"] is None and not (won or resumed):
        raise RuntimeError("this plan is already being shared")
    return share, resumed


def _find(owner: str, p: dict, client, store) -> str | None:
    """The document an earlier create of this plan left in ``owner``'s
    Library, or None. Raises when the Library can't be asked: not knowing
    must never lead to a second document."""
    query = urlencode({"scope_kind": "user", "scope_id": owner, "session": p["root_id"], "design": "true"})
    docs = ad.api("GET", f"/analysis-docs?{query}", client=client, store=store).get("docs") or []
    hits = [d for d in docs if (d.get("source") or {}).get("ref") == p["id"]]
    return min(hits, key=lambda d: d["created_at"])["doc_id"] if hits else None


def _linked(db: DB, plan_id: str, doc_id: str, remote_rev: int, local_rev: int) -> str:
    with db.tx() as c:
        c.execute("UPDATE plan_shares SET doc_id=?, remote_rev=?, local_rev=?, claimed_at=NULL WHERE plan_id=?",
                  (doc_id, remote_rev, local_rev, plan_id))
        # Written before there was anyone to read it: linked to no Library thread, so it is never sent.
        c.execute("INSERT OR IGNORE INTO plan_share_threads (plan_id, remote_id, local_id, status)"
                  " SELECT plan_id, -id, id, status FROM plan_threads WHERE plan_id=?", (plan_id,))
    return doc_id


def _create(db: DB, p: dict, owner: str, cur: dict, content: dict, resumed: bool, client, store) -> str:
    """The plan's document in ``owner``'s Library: the one an interrupted
    create already made (at the revision it sent, the Library's first), else
    a new one. Only a refusal gives the claim up. When the outcome is unknown
    (no answer, or a server error) the row stays, for a later share to look."""
    found = _find(owner, p, client, store) if resumed else None
    if found is not None:
        return _linked(db, p["id"], found, FIRST_REVISION, link(db, p["id"])["local_rev"])
    body = {"scope": {"kind": "user", "id": owner}, "files": [{"name": f"plan-{p['id']}.md", "md": cur["markdown"]}],
            "source": {"kind": "goal", "ref": p["id"], "session": p["root_id"]}, "design": content}
    with db.tx() as c:
        c.execute("UPDATE plan_shares SET local_rev=? WHERE plan_id=?", (cur["rev"], p["id"]))
    status, made = ad._call("POST", "/analysis-docs", body, client=client, store=store)
    if status == 200:
        return _linked(db, p["id"], made["id"], made["version"], cur["rev"])
    if status < 500:
        with db.tx() as c:
            c.execute("DELETE FROM plan_shares WHERE plan_id=? AND doc_id IS NULL", (p["id"],))
    raise RuntimeError(made.get("error") or f"HTTP {status}")


def share(db: DB, plan_id: str, scope: Any, *, reviewers: Any = None, permissions: dict | None = None,
          folder: dict | None = None, client=None, store=None, now: float | None = None) -> dict:
    """Save the plan to the Library and share it with ``scope`` (a team or
    the org). Safe to run again: a plan keeps its one document, so this also
    adds a scope or finishes a share that failed partway. ``reviewers``:
    ``[{sub, required?}]``; ``permissions``: ``{sub: {can_edit, can_suggest,
    can_comment}}`` for other teammates; ``folder``: ``{scope, id}`` instead
    of the default ``Designs`` folder. Returns the link."""
    p = plan_docs._plan(db, plan_id)
    if reviewers is not None:
        _review_open(p)
    client, store = ad._session(client, store)
    ent = _require_team(store)
    scope = _scope(scope)
    people = None if reviewers is None else _reviewers(reviewers)
    cur = plan_docs.revision(db, p["id"])
    content = _content(cur)          # a plan the API would refuse takes no claim
    row, resumed = _claim_new(db, p["id"], ent.sub, ent.org_id, time.time() if now is None else now)
    _own(row, ent)
    doc_id = row["doc_id"] or _create(db, p, ent.sub, cur, content, resumed, client, store)
    review_policies.ask(db, p, "POST", ad._doc_path(doc_id, "/share"), {"scope": scope}, client=client, store=store)
    if people or permissions:
        _grant(db, p, doc_id, people or [], permissions, client, store)
    if people is not None:
        _remember(db, p["id"], review_policies.ask(db, p, "PUT", _path(doc_id, "/reviewers"), {"reviewers": people},
                                                   client=client, store=store))
    if folder is not None:
        _remember(db, p["id"], review_policies.ask(db, p, "PUT", _path(doc_id, "/folder"), {"folder": folder},
                                                   client=client, store=store))
    return link(db, p["id"])


def set_reviewers(db: DB, plan_id: str, reviewers: Any, *, client=None, store=None) -> dict:
    """Replace the required and optional reviewers of a shared plan. Returns the link."""
    p = plan_docs._plan(db, plan_id)
    _review_open(p)
    client, store = ad._session(client, store)
    share_ = _shared(db, p["id"], store)
    people = _reviewers(reviewers)
    _grant(db, p, share_["doc_id"], people, None, client, store)
    if _remember(db, p["id"], review_policies.ask(db, p, "PUT", _path(share_["doc_id"], "/reviewers"),
                                                  {"reviewers": people}, client=client, store=store)):
        plan_docs._changed(db, p, p["rev"])
    return link(db, p["id"])


# -- pulling what teammates wrote --------------------------------------------------------


def _author(sub: Any) -> str | None:
    return plan_docs.TEAMMATE + sub if isinstance(sub, str) and sub else None


def _when(at: Any, now: float) -> float:
    return float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else now


def _anchor(raw: Any, cur: dict | None) -> dict | None:
    """A pulled anchor in the local shape, or None when it isn't one (a bad
    row would otherwise break reading the whole plan). The Library doesn't
    return an insertion point's context, so it is taken from ``cur``, the
    revision the Library's head holds (None when that isn't known). A point
    the Library couldn't place, or one with no ``cur``, gets no context, which
    leaves it detached here too rather than placed by an old offset."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind in ("diagram", "section") and isinstance(raw.get("section"), str):
        return {"kind": kind, "section": raw["section"]}
    if kind == "decision" and isinstance(raw.get("decision"), str):
        return {"kind": kind, "decision": raw["decision"], "option": raw.get("option")}
    section, start, quote = raw.get("section"), raw.get("start"), raw.get("quote")
    if kind != "text" or not isinstance(section, str) or not isinstance(start, int) or not isinstance(quote, str):
        return None
    out = {"kind": "text", "section": section, "start": start, "end": start + len(quote), "quote": quote}
    if isinstance(raw.get("cursor"), str) and len(raw["cursor"]) <= MAX_CURSOR:
        out["cursor"] = raw["cursor"]       # made on the live draft: it follows the words there
    body = plan_docs._body(cur, section) if cur is not None and raw.get("state") != "detached" else None
    if not quote and body is not None:
        out.update(plan_docs._context(body, start))
    return out


def _insert(c, plan_id: str, remote_id: int, known: dict, *, parent: int | None, rev: int, kind: str,
            anchor: dict | None, author: str, text: str, after: str | None, status: str, at: float) -> int:
    local = int(c.execute(
        "INSERT INTO plan_threads (plan_id, parent_id, rev, kind, anchor, author, body, after, attachments, status,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,'[]',?,?,?)",
        (plan_id, parent, rev, kind, json.dumps(anchor) if anchor else None, author, text, after, status, at,
         at)).lastrowid)
    c.execute("INSERT INTO plan_share_threads (plan_id, remote_id, local_id, status) VALUES (?,?,?,?)",
              (plan_id, remote_id, local, status))
    known[remote_id] = {"local_id": local, "status": status}
    return local


def _pull_thread(c, plan_id: str, rev: int, cur: dict | None, known: dict, t: Any, now: float) -> int:
    """Add a Library thread and its replies, once, or carry a change of its
    status. A change made here first (the person accepted the suggestion)
    stands until the Library's own status changes. Returns how many changed."""
    if not isinstance(t, dict) or not isinstance(t.get("id"), int):
        return 0
    rid, kind, status = t["id"], t.get("kind"), t.get("status")
    changes, row = 0, known.get(rid)
    if row is None:
        anchor, author = _anchor(t.get("anchor"), cur), _author(t.get("author"))
        text, after = mask(t.get("text") or ""), mask(t.get("after"))
        if (kind not in ("comment", "suggestion") or status not in STATUSES or anchor is None or author is None
                or not isinstance(text, str) or (kind == "suggestion" and not isinstance(after, str))):
            log.info("shared doc thread %s skipped: not a thread this brindle reads", rid)
            return 0
        _insert(c, plan_id, rid, known, parent=None, rev=rev, kind=kind, anchor=anchor, author=author, text=text,
                after=after if kind == "suggestion" else None, status=status, at=_when(t.get("at"), now))
        changes, row = 1, known[rid]
    elif row["status"] != status and status in STATUSES:
        c.execute("UPDATE plan_threads SET status=?, updated_at=? WHERE id=?", (status, now, row["local_id"]))
        c.execute("UPDATE plan_share_threads SET status=? WHERE plan_id=? AND remote_id=?", (status, plan_id, rid))
        row["status"] = status
        changes = 1
    for r in t.get("replies") or []:
        author = _author(r.get("author")) if isinstance(r, dict) else None
        text = mask(r.get("text")) if author else None
        if author and isinstance(r.get("id"), int) and r["id"] not in known and isinstance(text, str) and text:
            _insert(c, plan_id, r["id"], known, parent=row["local_id"], rev=rev, kind="reply", anchor=None,
                    author=author, text=text, after=None, status="open", at=_when(r.get("at"), now))
            changes += 1
    from brindle.pro import doc_questions

    doc_questions.pull_flags(c, t, known)
    return changes


def _pull_votes(c, plan_id: str, owner: str, rev: int, decisions: list, now: float) -> int:
    """Make the teammates' votes the Library's current ones (it answers with
    the head revision's only, so a vote from an older revision goes). The
    owner's vote on a decision the person picked here is that pick coming back."""
    mine = {r["decision_id"] for r in c.execute("SELECT decision_id FROM plan_votes WHERE plan_id=? AND voter=?",
                                                (plan_id, plan_docs.PERSON))}
    wanted = {(d["id"], plan_docs.TEAMMATE + sub): option for d in decisions
              for option, subs in (d.get("votes") or {}).items() for sub in subs
              if sub != owner or d["id"] not in mine}
    pattern = plan_docs.TEAMMATE + "%"
    held = {(r["decision_id"], r["voter"]): r["option_id"] for r in c.execute(
        "SELECT decision_id, voter, option_id FROM plan_votes WHERE plan_id=? AND voter LIKE ?", (plan_id, pattern))}
    if held == wanted:
        return 0
    c.execute("DELETE FROM plan_votes WHERE plan_id=? AND voter LIKE ?", (plan_id, pattern))
    c.executemany("INSERT INTO plan_votes (plan_id, decision_id, voter, option_id, rev, at) VALUES (?,?,?,?,?,?)",
                  [(plan_id, d, voter, option, rev, now) for (d, voter), option in wanted.items()])
    return 1


def _known(c, plan_id: str) -> dict:
    return {r["remote_id"]: {"local_id": r["local_id"], "status": r["status"]} for r in c.execute(
        "SELECT remote_id, local_id, status FROM plan_share_threads WHERE plan_id=?", (plan_id,))}


def _place(anchor: Any) -> tuple | None:
    """Where a thread is, in the terms both sides keep: anchored text can move
    between sections, so it is known by its quote."""
    if not isinstance(anchor, dict):
        return None
    where = anchor.get("quote") if anchor.get("kind") == "text" else anchor.get("section")
    return anchor.get("kind"), where, anchor.get("decision"), anchor.get("option")


def _link_own(c, plan_id: str, owner: str, known: dict, threads: Any) -> None:
    """Pair what the Library holds from this account with what was sent from
    here and not linked yet, oldest first, so it comes back as itself instead
    of as a teammate's, and an answer that was lost doesn't send it twice.
    Only a write actually sent can pair: the same words from this account
    elsewhere are another thread. Text is compared as sent, secrets masked.
    Linked local threads take the Library's creation time, so review gates
    compare their objections and sign-offs on the same clock."""
    waiting: dict[tuple, list[int]] = {}
    for r in c.execute("SELECT t.id, t.parent_id, t.kind, t.body, t.after, t.anchor, t.question FROM plan_threads t"
                       " JOIN plan_share_threads s ON s.plan_id=t.plan_id AND s.local_id=t.id"
                       " WHERE t.plan_id=? AND s.remote_id<0 AND s.status=? ORDER BY t.id", (plan_id, SENDING)):
        place = _place(json.loads(r["anchor"]) if r["anchor"] else None)
        waiting.setdefault((r["parent_id"], r["kind"], mask(r["body"]), mask(r["after"]), place, bool(r["question"])), []).append(r["id"])

    def pair(remote_id: int, key: tuple, status: Any) -> None:
        if waiting.get(key) and remote_id not in known:
            local = waiting[key].pop(0)
            c.execute("DELETE FROM plan_share_threads WHERE plan_id=? AND remote_id=?", (plan_id, -local))
            c.execute("INSERT INTO plan_share_threads (plan_id, remote_id, local_id, status) VALUES (?,?,?,?)",
                      (plan_id, remote_id, local, status))
            known[remote_id] = {"local_id": local, "status": status}

    for t in threads or []:
        if not isinstance(t, dict) or not isinstance(t.get("id"), int):
            continue
        if t.get("author") == owner:
            pair(t["id"], (None, t.get("kind"), t.get("text") or "", t.get("after"), _place(t.get("anchor")), bool(t.get("question"))),
                 t.get("status"))
        parent = known.get(t["id"])
        at = t.get("at")
        if parent and t.get("author") == owner and isinstance(at, (int, float)) and not isinstance(at, bool):
            c.execute("UPDATE plan_threads SET created_at=? WHERE id=? AND author=? AND created_at<>?",
                      (at, parent["local_id"], plan_docs.PERSON, at))
        for r in (t.get("replies") or []) if parent else []:
            if isinstance(r, dict) and isinstance(r.get("id"), int) and r.get("author") == owner:
                pair(r["id"], (parent["local_id"], "reply", r.get("text") or "", None, None, False), "open")


def _pull(db: DB, p: dict, share_: dict, view: dict) -> int:
    rev, now = share_["local_rev"], time.time()
    cur = plan_docs.revision(db, p["id"], rev) if view.get("rev") == share_["remote_rev"] else None
    changes = 0
    with db.tx() as c:
        known = _known(c, p["id"])
        _link_own(c, p["id"], share_["owner"], known, view.get("threads"))
        for t in view.get("threads") or []:
            changes += _pull_thread(c, p["id"], rev, cur, known, t, now)
        changes += _pull_votes(c, p["id"], share_["owner"], rev, view.get("decisions") or [], now)
    changes += _remember(db, p["id"], view)
    if changes:
        plan_docs._changed(db, p, p["rev"])
    return changes


def _readable(view: dict) -> dict:
    """The Library's head as a revision of this plan: held to what a
    supervisor's proposal must be, because it is drawn and shown here, and
    with secrets masked. Raises ``RuntimeError`` if it can't be one."""
    try:
        content = mask_data(_content(view))
        sections = plan_docs.parse_sections(content["sections"])
        lede = plan_docs._body_text(content["lede"], "the overview", plans.MAX_MARKDOWN).strip()
        out = {"lede": lede, "sections": sections, "decisions": plans._decisions(content["decisions"]),
               "split": plans._split(content["split"]), "ask": visuals._text(content["ask"], "ask", visuals.MAX_NOTE),
               "markdown": plan_docs.render(lede, sections)}
    except (ServiceError, KeyError, TypeError, AttributeError) as e:
        raise RuntimeError(f"the Library's r{view.get('rev')} can't be read here: {mask(str(e))}") from e
    if len(out["markdown"].encode("utf-8")) > plans.MAX_MARKDOWN:
        raise RuntimeError(f"the Library's r{view.get('rev')} is too big to read here")
    return out


def _import(db: DB, p: dict, share_: dict, view: dict) -> int:
    """Take the Library's head as a new revision, when a teammate moved it and
    the plan hasn't changed here. Only an open plan changes: a redirected one
    is the supervisor's to revise and an approved one is building. Returns
    how many revisions it added: claimed from the revision read here, so a
    concurrent edit or import leaves this one out."""
    if view.get("rev") == share_["remote_rev"] or share_["local_rev"] != p["rev"] or p["state"] != "open":
        return 0
    cur = plan_docs.revision(db, p["id"])
    if _content(view) == mask_data(_content(cur)):       # moved, but the same text: nothing to read
        with db.tx() as c:
            c.execute("UPDATE plan_shares SET remote_rev=? WHERE plan_id=?", (view["rev"], p["id"]))
        return 0
    new = _readable(view)
    before = {s["id"]: s for s in cur["sections"]}
    after = {s["id"]: s for s in new["sections"]}
    changed = [sid for sid in dict.fromkeys([*before, *after]) if before.get(sid) != after.get(sid)]
    changed += [field for field in ("lede", "decisions", "split", "ask") if cur[field] != new[field]]
    author, rev, at = _author(view.get("author")) or plan_docs.TEAMMATE + "unknown", p["rev"] + 1, time.time()
    with db.tx() as c:
        claimed = c.execute("UPDATE plans SET rev=?, markdown=?, decisions=?, split=?, ask=?, updated_at=?"
                            " WHERE id=? AND rev=? AND state='open'",
                            (rev, new["markdown"], json.dumps(new["decisions"]), json.dumps(new["split"]), new["ask"],
                             at, p["id"], p["rev"])).rowcount == 1
        if claimed:
            plan_docs.keep(c, cur)
            plan_docs.insert_revision(c, p["id"], rev, author, "edit", new["lede"], new["sections"], new["markdown"],
                                      new["decisions"], new["split"], new["ask"], changed, at)
            c.execute("UPDATE plan_shares SET remote_rev=?, local_rev=? WHERE plan_id=?", (view["rev"], rev, p["id"]))
    return int(claimed)


# -- sending what was done here -----------------------------------------------------------


class _Head:
    """The Library's head as this send knows it: every write is made on it,
    and answers with the whole view after it. A refusal by an org review
    rule is kept on the plan, whichever write it was."""

    def __init__(self, db: DB, p: dict, doc_id: str, view: dict, client, store):
        self.db, self.p, self.doc_id, self.view, self.client, self.store = db, p, doc_id, view, client, store
        self.refused = False            # the Library's latest verdict on a write here was an org rule's refusal

    @property
    def rev(self) -> Any:
        return self.view.get("rev")

    def write(self, tail: str, body: dict) -> tuple[int, dict]:
        status, resp = ad._call("POST", _path(self.doc_id, tail), {"rev": self.rev, **body},
                                client=self.client, store=self.store)
        if status == 200:
            self.view, self.refused = resp, False
            review_policies.seen(self.db, self.p, resp)
        else:
            review_policies.refused(self.db, self.p, resp, status)
            self.refused = (self.refused or resp.get("error") in review_policies.REFUSALS
                            or status in review_policies.ACCESS_REFUSED)
        return status, resp

    def send(self, tail: str, body: dict) -> bool:
        """A write that may be refused on its own (the text it names isn't on
        the Library's head, say): it stays unsent and the rest still go."""
        status, resp = self.write(tail, body)
        if status != 200:
            log.info("shared doc %s: %s refused (%s), kept for the next sync", self.doc_id, tail, resp.get("error"))
        return status == 200


def _landed(db: DB, plan_id: str, since: int, newest: int, view: dict) -> int | None:
    """Which local revision after ``since`` the Library's head holds, or None."""
    theirs = _content(view)
    return next((n for n in range(newest, since, -1)
                 if mask_data(_content(plan_docs.revision(db, plan_id, n))) == theirs), None)


def _set_head(c, plan_id: str, remote_rev: int, local_rev: int) -> None:
    c.execute("UPDATE plan_shares SET remote_rev=?, local_rev=? WHERE plan_id=?", (remote_rev, local_rev, plan_id))


def _unsent(db: DB, plan_id: str) -> list:
    """The person's threads and replies with no Library thread yet, oldest
    first: never sent, or sent and the answer never came back."""
    return db.conn.execute(
        "SELECT * FROM plan_threads WHERE plan_id=? AND author=? AND id NOT IN (SELECT local_id FROM plan_share_threads"
        " WHERE plan_id=? AND NOT (remote_id<0 AND status=?)) ORDER BY id",
        (plan_id, plan_docs.PERSON, plan_id, SENDING)).fetchall()


def _send_thread(db: DB, plan_id: str, owner: str, head: _Head, t: Any, on: dict, replica=None) -> None:
    """Make thread or reply ``t`` in the Library, placed by revision ``on``,
    or, on a live draft (``replica``), by the draft's text and the anchor's
    cursor, which goes with it. A suggestion already accepted in the draft
    goes as it was made: its words are gone, its cursor says where they were.
    Marked first, so an answer that never comes can be recognised later."""
    if t["parent_id"] is not None:
        parent = db.conn.execute("SELECT remote_id FROM plan_share_threads WHERE plan_id=? AND local_id=?"
                                 " AND remote_id>0", (plan_id, t["parent_id"])).fetchone()
        if parent is None:
            return
        tail, body = "/comments", {"parent": parent["remote_id"], "text": t["body"]}
    else:
        anchor = plan_docs._followed(replica, json.loads(t["anchor"]))
        gone = replica is not None and t["status"] == "accepted"
        where = dict(anchor, state="current") if gone else plan_docs._where(on, anchor)
        if where["state"] == "detached":
            return
        cursor = {"cursor": anchor["cursor"]} if replica is not None and anchor.get("cursor") else {}
        if t["kind"] == "comment":
            anchor = {k: v for k, v in where.items() if k not in ("state", "prefix", "suffix", "cursor")}
            tail, body = "/comments", {"anchor": {**anchor, **cursor}, "text": t["body"]}
        else:
            tail, body = "/suggestions", {"section": where["section"], "start": where["start"],
                                          "before": where["quote"], "after": t["after"], "text": t["body"], **cursor}
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO plan_share_threads (plan_id, remote_id, local_id, status) VALUES (?,?,?,?)",
                  (plan_id, -t["id"], t["id"], SENDING))
    if t["question"]:
        body["question"] = True
    status, resp = head.write(tail, body)
    with db.tx() as c:
        if status == 200:
            _link_own(c, plan_id, owner, _known(c, plan_id), head.view.get("threads"))
        elif status < 500:      # refused outright. After a server error it may have landed: the next pull will say
            c.execute("DELETE FROM plan_share_threads WHERE plan_id=? AND remote_id=?", (plan_id, -t["id"]))
    if status != 200:
        log.info("shared doc %s: %s refused (%s), kept for the next sync", head.doc_id, tail, resp.get("error"))


def _send_revision(db: DB, plan_id: str, head: _Head, rev: int) -> bool:
    """Put local revision ``rev`` on the Library's head. False if the head
    moved since it was read: a conflict for now, the next sync looks again."""
    status, resp = head.write("/revisions", _content(plan_docs.revision(db, plan_id, rev)))
    if status == 409 and resp.get("error") == "stale":
        head.view = {**head.view, "rev": resp.get("rev")}
        return False
    if status != 200:
        raise RuntimeError(resp.get("error") or f"HTTP {status}")
    with db.tx() as c:
        _set_head(c, plan_id, head.rev, rev)
    return True


def _pending_accepts(db: DB, plan_id: str, local_rev: int) -> list:
    """The suggestions accepted here in revisions after ``local_rev`` that the
    Library hasn't closed, oldest first, each with the revision it made."""
    return db.conn.execute(
        "SELECT r.rev AS made_rev, s.remote_id, t.* FROM plan_revisions r JOIN plan_threads t ON t.plan_id=r.plan_id"
        " AND t.updated_at=r.created_at AND t.kind='suggestion' AND t.status='accepted'"
        " LEFT JOIN plan_share_threads s ON s.plan_id=t.plan_id AND s.local_id=t.id AND s.remote_id>0"
        " WHERE r.plan_id=? AND r.source='suggestion' AND r.rev>? AND (s.remote_id IS NULL OR s.status='open')"
        " AND t.id NOT IN (SELECT local_id FROM plan_share_threads WHERE plan_id=? AND remote_id<0 AND status<>?)"
        " ORDER BY r.rev", (plan_id, local_rev, plan_id, SENDING)).fetchall()


def _send_revisions(db: DB, plan_id: str, owner: str, head: _Head, newest: int) -> bool:
    """Bring the Library's head up to revision ``newest``, in as few revisions
    as the suggestions accepted here allow. Each acceptance is replayed on
    the exact text it was made on: the revision before it goes up first if
    the head doesn't hold it, the suggestion is made there if it never went
    up, and the Library's own accept then makes the revision and closes the
    thread. It can't be sent later (on text already holding the change it
    would apply it again, and ``resolve`` is refused on a suggestion), so a
    step that fails stops the rest (raised) and the text waits for it.
    Returns whether the head holds a new revision from here."""
    local, moved = link(db, plan_id)["local_rev"], False
    for t in _pending_accepts(db, plan_id, local):
        before = t["made_rev"] - 1
        if local != before:
            if not _send_revision(db, plan_id, head, before):
                return moved
            local, moved = before, True
        remote = t["remote_id"]
        if remote is None:
            _send_thread(db, plan_id, owner, head, t, plan_docs.revision(db, plan_id, before))
            found = db.conn.execute("SELECT remote_id FROM plan_share_threads WHERE plan_id=? AND local_id=?"
                                    " AND remote_id>0", (plan_id, t["id"])).fetchone()
            if found is None:
                raise RuntimeError(f"the Library didn't take a suggestion accepted here, so r{newest} wasn't sent")
            remote = found["remote_id"]
        status, resp = head.write(f"/threads/{remote}", {"action": "accept"})
        if status != 200:
            raise RuntimeError(f"the Library refused an accepted suggestion ({resp.get('error') or status}), "
                               f"so r{newest} wasn't sent")
        moved = True
        local = _landed(db, plan_id, before, t["made_rev"], head.view) or local
        with db.tx() as c:
            c.execute("UPDATE plan_share_threads SET status='accepted' WHERE plan_id=? AND remote_id=?",
                      (plan_id, remote))
            _set_head(c, plan_id, head.rev, local)
    if local == newest:
        return moved
    return _send_revision(db, plan_id, head, newest) or moved


def _send_text(db: DB, plan_id: str, head: _Head) -> bool:
    """Bring the Library's head up to the plan's newest revision, on the head
    this plan last saw. If the head moved without this plan it is left alone:
    that is the gate's ``conflict`` (or a redirected plan's text, which isn't
    imported). Returns whether the head holds a new revision from here."""
    share_, cur = link(db, plan_id), plan_docs.revision(db, plan_id)
    moved = False
    if head.rev != share_["remote_rev"]:
        # Equal text can't prove an acceptance happened (a later revision may repeat an earlier one's
        # words), but the Library's thread status can: never read the head as past one it hasn't closed.
        pending = _pending_accepts(db, plan_id, share_["local_rev"])
        ceiling = pending[0]["made_rev"] - 1 if pending else cur["rev"]
        held = _landed(db, plan_id, share_["local_rev"], ceiling, head.view)
        if held is None:
            return False
        with db.tx() as c:        # an earlier send landed and only its answer was lost
            _set_head(c, plan_id, head.rev, held)
        share_, moved = link(db, plan_id), True
    if share_["local_rev"] == cur["rev"]:
        return moved
    return _send_revisions(db, plan_id, share_["owner"], head, cur["rev"]) or moved


def _send_threads(db: DB, plan_id: str, owner: str, head: _Head, replica=None) -> None:
    """Send the person's new comments, suggestions and replies, whatever has
    happened to them since: a thread closed before its first send goes up,
    then _send_actions closes it. A reply waits for its thread to be in the
    Library (a supervisor's never will be). An accepted suggestion goes up
    with its revision (_send_revisions), never here on text that holds it;
    on a live draft (``replica``) it goes up here too, and is then marked
    accepted (_send_actions), its text having gone with the draft."""
    if replica is None:
        cur = plan_docs.revision(db, plan_id)
    else:
        cur = plan_docs.current(db, plans._row(db, plan_id), replica)
    for t in _unsent(db, plan_id):
        if t["status"] != "accepted" or replica is not None:
            _send_thread(db, plan_id, owner, head, t, cur, replica)


def _send_actions(db: DB, plan_id: str, head: _Head, drafting: bool) -> None:
    """Send what the person did to threads the Library has: resolved,
    reopened, rejected, and on a live draft (``drafting``) accepted."""
    rows = db.conn.execute(
        "SELECT t.kind, t.status, s.remote_id FROM plan_threads t JOIN plan_share_threads s"
        " ON s.plan_id=t.plan_id AND s.local_id=t.id"
        " WHERE t.plan_id=? AND t.parent_id IS NULL AND s.remote_id>0 AND t.status<>s.status", (plan_id,)).fetchall()
    for r in rows:
        action = THREAD_ACTIONS.get((r["kind"], r["status"]))
        body = {"action": action} if action else None
        if drafting and (r["kind"], r["status"]) == ("suggestion", "accepted"):
            body = DRAFT_ACCEPT
        if body and head.send(f"/threads/{r['remote_id']}", dict(body)):
            with db.tx() as c:
                c.execute("UPDATE plan_share_threads SET status=? WHERE plan_id=? AND remote_id=?",
                          (r["status"], plan_id, r["remote_id"]))


def _send_votes(db: DB, plan_id: str, head: _Head) -> None:
    """Send the person's picks the head doesn't have: a new head starts with no votes."""
    theirs = {d["id"]: d.get("confirmed") for d in head.view.get("decisions") or []}
    for decision, option in plan_docs.confirmed(db, plans._row(db, plan_id)).items():
        if decision in theirs and theirs[decision] != option:
            head.send("/votes", {"decision": decision, "option": option})


def _send(db: DB, p: dict, view: dict, client, store, now: float) -> bool:
    """Send what was done here: the text first, so what the person wrote on it
    can be placed. One send at a time: while another holds the plan this one
    leaves it alone. Returns whether the Library took a new revision."""
    with db.tx() as c:
        held = c.execute("UPDATE plan_shares SET claimed_at=? WHERE plan_id=? AND (claimed_at IS NULL OR claimed_at<?)",
                         (now, p["id"], now - CLAIM_FOR)).rowcount
    if not held:
        return False
    try:
        share_ = link(db, p["id"])
        head = _Head(db, p, share_["doc_id"], view, client, store)
        # With a live draft the text changes only there: the Library refuses revisions.
        drafting = bool(view.get("draft"))
        pushed = not drafting and _send_text(db, p["id"], head)
        _send_threads(db, p["id"], share_["owner"], head, plan_docs.draft(db, p) if drafting else None)
        _send_actions(db, p["id"], head, drafting)
        _send_votes(db, p["id"], head)
        if head.rev != view.get("rev"):        # a new head: votes on the old one no longer count
            local_rev = link(db, p["id"])["local_rev"]
            with db.tx() as c:
                _pull_votes(c, p["id"], share_["owner"], local_rev, head.view.get("decisions") or [], now)
        if head.view is not view and not head.refused:      # an answer older than a refusal can't clear it
            _remember(db, p["id"], head.view)
        return pushed
    finally:
        with db.tx() as c:
            c.execute("UPDATE plan_shares SET claimed_at=NULL WHERE plan_id=?", (p["id"],))


# -- the live draft -------------------------------------------------------------------------


class DraftLink(Protocol):
    """The relay connection a live draft's Loro updates travel on."""

    def pull(self, doc_id: str, since: bytes) -> tuple[bytes, bytes]:
        """The updates a replica at version ``since`` lacks, and the relay's version."""

    def push(self, doc_id: str, updates: bytes) -> None:
        """Hand the relay updates made here."""


# Each document's relay connection, by doc id, while the live client holds one.
# With none, sync trades nothing and the replica keeps what was typed here.
relays: dict[str, DraftLink] = {}


def _hunks(a: str, b: str) -> list[tuple[int, str, str]]:
    """Where ``b`` differs from ``a``, by whole words: ``(offset in a, a's text
    there, b's)``. An insertion takes the word after it (or before it, at the
    end) on both sides, so it has words to be found by."""
    ta, tb = _TOKENS.findall(a), _TOKENS.findall(b)
    at = [0]
    for t in ta:
        at.append(at[-1] + len(t))
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if i1 == i2 and i2 < len(ta):
            i2, j2 = i2 + 1, j2 + 1
        elif i1 == i2 and i1 > 0:
            i1, j1 = i1 - 1, j1 - 1
        out.append((at[i1], "".join(ta[i1:i2]), "".join(tb[j1:j2])))
    return out


def _carried(new: dict, ours: dict, base: dict) -> tuple[list[tuple[dict, str]], list[str]]:
    """What revision ``ours`` changed since ``base`` (the Library's revision
    it was made on), placed in ``new`` (the Library's head now): suggestion
    anchors in ``new`` with their replacements. And the names of what it
    changed that can't be one: text the head no longer has, sections added,
    removed or retitled, decisions, the split, the ask."""
    was, mine = {s["id"]: s for s in base["sections"]}, {s["id"]: s for s in ours["sections"]}
    out, missed = [], []
    for sid in [plan_docs.LEDE, *was]:
        lede = sid == plan_docs.LEDE
        name = "the overview" if lede else was[sid]["title"]
        a = base["lede"] if lede else was[sid]["markdown"]
        b = ours["lede"] if lede else (mine[sid]["markdown"] if sid in mine else None)
        if b is None:
            missed.append(f"removed {name}")
            continue
        if not lede and any(was[sid].get(k) != mine[sid].get(k) for k in ("title", "kind", "caption")):
            missed.append(name)
        for start, before, after in _hunks(a, b):
            anchor = {"kind": "text", "section": sid, "start": start, "end": start + len(before), "quote": before}
            if not before:
                anchor.update(plan_docs._context(a, start))
            where = plan_docs._where(new, anchor)
            if where["state"] == "detached" or len(before) > plan_docs.MAX_QUOTE:
                missed.append(name)
                continue
            out.append(({k: v for k, v in where.items() if k != "state"}, after))
    missed += [f"added {s['title']}" for sid, s in mine.items() if sid not in was]
    missed += [name for name in ("decisions", "split", "ask") if ours[name] != base[name]]
    return out, list(dict.fromkeys(missed))


def _carry_over(db: DB, p: dict, share_: dict, view: dict) -> int:
    """When the Library's draft reaches an open plan holding revisions the
    Library never took (made here before then, or by the supervisor since):
    the Library's head becomes the plan's revision, as a teammate's would,
    and what those revisions changed in its text is filed as suggestions on
    it, so nothing written here is lost, overwritten or left in conflict.
    What a draft can't hold is named in a comment. Suggestions accepted in
    those revisions never reached the Library: they are open again. Claimed
    from the revision read here, in one transaction. Returns 1 if it ran."""
    if not view.get("draft") or p["state"] != "open" or share_["local_rev"] == p["rev"]:
        return 0
    ours, new = plan_docs.revision(db, p["id"]), _readable(view)
    authors = {r["author"] for r in db.conn.execute(
        "SELECT author FROM plan_revisions WHERE plan_id=? AND rev>?", (p["id"], share_["local_rev"]))}
    by = plan_docs.SUPERVISOR if authors == {plan_docs.SUPERVISOR} else plan_docs.PERSON
    suggestions, missed = _carried(new, ours, plan_docs.revision(db, p["id"], share_["local_rev"]))
    reopened = [t["id"] for t in _pending_accepts(db, p["id"], share_["local_rev"])]
    before = {s["id"]: s for s in ours["sections"]}
    after = {s["id"]: s for s in new["sections"]}
    changed = [sid for sid in dict.fromkeys([*before, *after]) if before.get(sid) != after.get(sid)]
    changed += [field for field in ("lede", "decisions", "split", "ask") if ours[field] != new[field]]
    author, rev, at = _author(view.get("author")) or plan_docs.TEAMMATE + "unknown", p["rev"] + 1, time.time()
    with db.tx() as c:
        claimed = c.execute("UPDATE plans SET rev=?, markdown=?, decisions=?, split=?, ask=?, updated_at=?"
                            " WHERE id=? AND rev=? AND state='open'",
                            (rev, new["markdown"], json.dumps(new["decisions"]), json.dumps(new["split"]), new["ask"],
                             at, p["id"], p["rev"])).rowcount == 1
        if claimed:
            plan_docs.keep(c, ours)
            plan_docs.insert_revision(c, p["id"], rev, author, "edit", new["lede"], new["sections"], new["markdown"],
                                      new["decisions"], new["split"], new["ask"], changed, at)
            _set_head(c, p["id"], view["rev"], rev)
            for anchor, text in suggestions:
                plan_docs.file_thread(c, p["id"], rev, "suggestion", anchor, by, "", after=text)
            if missed:
                note = (f"r{ours['rev']} changed this here before the live draft reached this machine, and a draft "
                        f"holds only text, so these changes weren't carried into it: {', '.join(missed)}.")
                plan_docs.file_thread(c, p["id"], rev, "comment", {"kind": "section", "section": new["sections"][0]["id"]},
                                      by, note[:visuals.MAX_NOTE])
            if reopened:
                c.execute(f"UPDATE plan_threads SET status='open', updated_at=? WHERE id IN ({','.join('?' * len(reopened))})",
                          (at, *reopened))
    return int(claimed)


def _draft(db: DB, p: dict, share_: dict, view: dict, client, store) -> DraftReplica | None:
    """This machine's replica of the document's live draft, or None while the
    Library has none. The first time, it starts from the Library's snapshot:
    a draft's first state is the server's, so its text is never seeded twice."""
    doc_id = share_["doc_id"]
    if not view.get("draft"):
        return None
    if db.conn.execute("SELECT 1 FROM plan_drafts WHERE doc_id=?", (doc_id,)).fetchone():
        return DraftReplica.open(db, doc_id)
    status, got = ad._call("GET", _path(doc_id, "/draft"), client=client, store=store)
    if status != 200:
        raise RuntimeError(f"the Library's live draft can't be read ({got.get('error') or status})")
    try:
        replica = DraftReplica.open(db, doc_id, base64.b64decode(got["snapshot"], validate=True))
    except (KeyError, TypeError, binascii.Error, CrdtError) as e:
        raise RuntimeError("the Library's live draft can't be read here") from e
    plan_docs._changed(db, p, p["rev"])
    return replica


def _trade(replica: DraftReplica, live: DraftLink) -> bool:
    """Take what teammates typed, then send the relay what it lacks from
    here. Returns whether the text here changed."""
    before = replica.sections()
    theirs, version = live.pull(replica.doc_id, replica.version())
    if theirs:
        replica.apply_remote(theirs)
    if replica.lacks(version):
        live.push(replica.doc_id, replica.export_updates(version))
    return replica.sections() != before


def sync(db: DB, plan_id: str, *, client=None, store=None, now: float | None = None) -> dict:
    """Take a teammate's new text if the plan hasn't changed here, bring back
    what teammates wrote, then send what was done here; with a live draft,
    trade its updates on the document's relay (``relays``) last. Pulled first, so a refused send still
    leaves their comments here. Returns the link, plus ``pulled`` (how many
    things changed here) and ``pushed``. Refused for want of a Team plan the
    server confirms has ended, it syncs nothing and the plan becomes a
    personal doc; any other failure is raised."""
    p = plan_docs._plan(db, plan_id)
    client, store = ad._session(client, store)
    try:
        share_ = _shared(db, p["id"], store)
        view = review_policies.ask(db, p, "GET", _path(share_["doc_id"]), client=client, store=store)
    except RuntimeError as e:
        no_team = not isinstance(e, ad.Refused) or e.code == NO_FEATURE     # an outage or org rule says nothing of it
        if no_team and _note_ended(db, p, _issued(client, store)):
            return {**link(db, p["id"]), "pulled": 0, "pushed": False}
        raise
    _team_active(db, p, store)
    imported = _import(db, p, share_, view)
    share_ = link(db, p["id"])
    imported += _carry_over(db, p, share_, view)
    if imported:
        p, share_ = plan_docs._plan(db, p["id"]), link(db, p["id"])
    pulled = imported + _pull(db, p, share_, view)
    # The draft's updates go before the threads, so what those are anchored on is there.
    replica = _draft(db, p, share_, view, client, store)
    live = relays.get(share_["doc_id"])
    typed = replica is not None and live is not None and _trade(replica, live)
    pulled += typed
    pushed = _send(db, p, view, client, store, time.time() if now is None else now)
    if pushed or imported or typed:
        plan_docs._changed(db, p, p["rev"])
    return {**link(db, p["id"]), "pulled": pulled, "pushed": pushed}


def sign_off(db: DB, plan_id: str, rev: Any, *, client=None, store=None) -> dict:
    """The person's own sign-off on the revision on screen (``rev``), for when
    they are one of its reviewers. The revision is sent first if the Library
    doesn't have it, so what they read is what they sign. Returns the link."""
    p = plan_docs._plan(db, plan_id)
    _review_open(p)
    plan_docs._on_screen(p, rev)
    client, store = ad._session(client, store)
    share_ = sync(db, p["id"], client=client, store=store)
    if share_["local_rev"] != p["rev"]:
        raise RuntimeError(f"r{p['rev']} isn't the Library's current revision, so it can't be signed off")
    if _remember(db, p["id"], review_policies.ask(db, p, "POST", _path(share_["doc_id"], "/sign-offs"),
                                                  {"rev": share_["remote_rev"]}, client=client, store=store)):
        plan_docs._changed(db, p, p["rev"])
    return link(db, p["id"])


# -- when the Library can't confirm a review ------------------------------------------------


def _unreachable(e: Exception) -> bool:
    from brindle.pro import auth

    return isinstance(e, auth.TransportError) or (isinstance(e, auth.AuthError) and e.code in ("network", "lock_timeout"))


def _no_entitlement_why(store) -> str:
    """No verified entitlement here: renewing it says whether that is the network or the sign-in."""
    from brindle.pro import auth

    client, store = ad._session(None, store)
    try:
        auth.refresh(client, store)
    except Exception as e:  # noqa: BLE001 - anything but the network means they have to sign in again
        return plans.OFFLINE if _unreachable(e) else plans.SIGN_IN
    return plans.LIBRARY_ERROR


def why_unconfirmed(db: DB, plan_id: str, e: Exception, store=None) -> str:
    """What stopped the Library confirming this plan's review, as a key of
    plans.UNCONFIRMED: the failure's own words never reach the person."""
    from brindle import airgap
    from brindle.pro import auth, license

    if isinstance(e, ad.Refused):
        return plans.KEY_UNAVAILABLE if e.code == "key_unavailable" else plans.LIBRARY_ERROR
    if _unreachable(e) or airgap.enabled():
        return plans.OFFLINE
    if isinstance(e, auth.AuthError):
        signed_out = e.revoked or e.code in ("invalid_grant", "invalid_token", "not_logged_in")
        return plans.SIGN_IN if signed_out else plans.LIBRARY_ERROR
    try:
        ent = license.current(refresh=False, store=store)
    except Exception:  # noqa: BLE001 - no entitlement to read
        return _no_entitlement_why(store)
    share_ = link(db, plan_id)
    if share_ is not None and (ent.sub != share_["owner"] or share_["org_id"] not in (None, ent.org_id)):
        return plans.OTHER_ACCOUNT
    return plans.OFFLINE if ent.in_grace else plans.LIBRARY_ERROR


def _note_unconfirmed(db: DB, plan_id: str, e: Exception, store) -> None:
    """A background pass failed on a plan someone tried to approve: keep the reason on screen current."""
    if db.conn.execute("SELECT 1 FROM plan_shares WHERE plan_id=? AND (unconfirmed IS NOT NULL OR approve_wanted"
                       " IS NOT NULL)", (plan_id,)).fetchone() is None:
        return                  # nobody is waiting on it: not worth asking the sign-in why
    why = why_unconfirmed(db, plan_id, e, store)
    with db.tx() as c:
        changed = c.execute("UPDATE plan_shares SET unconfirmed=? WHERE plan_id=? AND unconfirmed IS NOT ?",
                            (why, plan_id, why)).rowcount
    if changed:
        p = plans.get(db, plan_id)
        plan_docs._changed(db, p, p["rev"])


def account(store=None) -> str | None:
    """Who is signed in here, by the verified entitlement held; None if that can't be said."""
    from brindle.pro import license

    try:
        return license.current(refresh=False, store=store).sub
    except Exception:  # noqa: BLE001 - nobody can be named
        return None


def may_stop(row, who: str | None) -> bool:
    """Whether ``who`` may stop sharing a plan the Library can't confirm: the
    account that shared it, and only if no review state seen here ever had
    required reviewers or org rules, and the last answer was neither a
    refusal (of access or by a rule) nor unreadable. No answer yet can't show
    there are no rules."""
    review = json.loads(row["review"])
    return (row["unconfirmed"] is not None and who == row["owner"] and not row["review_asked"]
            and row["access_refused"] is None and not review.get(review_policies.REFUSED)
            and not plans.demands_review(review))


def stop_sharing(db: DB, plan_id: str, rev: Any, *, store=None) -> None:
    """Make a plan the Library can't confirm a personal doc, so it can be
    approved as one. The check and the change are one transaction, so a sync
    landing a reviewer can't slip in between. Teammates lose the document at
    the next pass that reaches the Library (:func:`_send_unshares`)."""
    p = plan_docs._plan(db, plan_id)
    plan_docs._on_screen(p, rev)
    who = account(store)
    with db.tx() as c:
        row = c.execute("SELECT * FROM plan_shares WHERE plan_id=?", (p["id"],)).fetchone()
        allowed = p["state"] == "open" and row is not None and row["doc_id"] is not None and may_stop(row, who)
        if allowed:
            c.execute("INSERT OR REPLACE INTO plan_unshares (doc_id, owner, created_at) VALUES (?,?,?)",
                      (row["doc_id"], row["owner"], time.time()))
            c.execute("DELETE FROM plan_share_threads WHERE plan_id=?", (p["id"],))
            c.execute("DELETE FROM plan_shares WHERE plan_id=?", (p["id"],))
    if not allowed:
        raise ServiceError("stop_sharing_refused", "This plan's sharing can't be stopped from here: its review has "
                           "to be confirmed by the Library.", 409)
    plan_docs._changed(db, p, p["rev"])


def _gone(e: Exception) -> bool:
    return isinstance(e, ad.Refused) and e.status == 404


def _unshare(doc_id: str, client, store) -> None:
    """Take a document out of every scope but its owner's. Already gone counts as done."""
    from urllib.parse import quote

    try:
        doc = ad.api("GET", ad._doc_path(doc_id), client=client, store=store)
    except ad.Refused as e:
        if _gone(e):
            return
        raise
    owner = doc.get("scope")
    if not isinstance(owner, dict):
        raise RuntimeError("the Library didn't say who owns this document")
    for scope in doc.get("scopes") or []:
        if scope == owner:
            continue
        path = ad._doc_path(doc_id, f"/share/{quote(scope['kind'], safe='')}/{quote(scope['id'], safe='')}")
        try:
            ad.api("DELETE", path, client=client, store=store)
        except ad.Refused as e:
            if not _gone(e):
                raise


def _send_unshares(db: DB, owner: str, client, store) -> None:
    """Unshare what this account stopped sharing. One failure leaves that one for the next pass."""
    for row in db.conn.execute("SELECT doc_id FROM plan_unshares WHERE owner=?", (owner,)).fetchall():
        try:
            _unshare(row["doc_id"], client, store)
        except Exception:  # noqa: BLE001 - offline, the org's key revoked: the next pass tries again
            log.debug("brindle: couldn't unshare %s yet", row["doc_id"], exc_info=True)
            continue
        with db.tx() as c:
            c.execute("DELETE FROM plan_unshares WHERE doc_id=?", (row["doc_id"],))


def _note_lapses(db: DB, client, store) -> None:
    """Without Team, get the server's word if any of this account's plans in
    review needs it: one it hasn't confirmed ended, or confirmed a while ago.
    A pass with none of those doesn't ask."""
    from brindle.pro import license

    try:
        stored = license.current(refresh=False, store=store)
    except Exception:  # noqa: BLE001 - logged out: no account whose plan could be said to have ended
        return
    due = db.conn.execute("SELECT s.plan_id FROM plan_shares s JOIN plans p ON p.id=s.plan_id"
                          " WHERE s.doc_id IS NOT NULL AND (s.lapsed_at IS NULL OR s.lapsed_at<?) AND s.owner=?"
                          " AND s.org_id=? AND p.state IN ('open','redirected')",
                          (time.time() - LAPSE_CHECK_AFTER, stored.sub, stored.org_id)).fetchone()
    if due is not None:
        _note_ended(db, plans.get(db, due["plan_id"]), _issued(client, store))


def sync_open(db: DB, *, client=None, store=None) -> int:
    """Sync every shared plan still in review, for the daemon's background
    pass (a building plan's review is closed). One plan's failure doesn't
    stop the rest. Without Team nothing is synced. With it, a plan whose Team
    plan had ended picks its document back up. An approval held for the
    Library is settled once it has answered. Returns how many synced."""
    client, store = ad._session(client, store)
    try:
        ent = _require_team(store)
    except Exception as e:  # noqa: BLE001 - logged out, air-gapped or no Team plan: nothing to bring back
        _note_lapses(db, client, store)
        for row in db.conn.execute("SELECT plan_id FROM plan_shares WHERE unconfirmed IS NOT NULL"
                                   " OR approve_wanted IS NOT NULL").fetchall():
            _note_unconfirmed(db, row["plan_id"], e, store)
        return 0
    _send_unshares(db, ent.sub, client, store)
    done = 0
    for row in db.conn.execute("SELECT s.plan_id FROM plan_shares s JOIN plans p ON p.id=s.plan_id"
                               " WHERE s.doc_id IS NOT NULL AND p.state IN ('open','redirected')").fetchall():
        try:
            sync(db, row["plan_id"], client=client, store=store)
            done += 1
        except Exception as e:  # noqa: BLE001 - offline, another account's plan: the next pass tries again
            log.debug("brindle: couldn't sync shared plan %s", row["plan_id"], exc_info=True)
            _note_unconfirmed(db, row["plan_id"], e, store)
            continue
        try:
            plans.settle_held_approval(db, row["plan_id"])
        except Exception:  # noqa: BLE001 - it stays held: the next pass looks again
            log.warning("brindle: couldn't settle plan %s's held approval", row["plan_id"], exc_info=True)
    return done
