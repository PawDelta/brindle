"""Shared design docs (brindle Pro, Team plan and up): a plan doc
(:mod:`brindle.plan_docs`) in the Library, where teammates on other machines
read it, comment, suggest, vote and sign off. The contract is
docs/shared-designs-api.md in pawdelta-web.

:func:`share` saves the plan to the person's Library (the default ``Designs``
folder, unless a folder is named) and shares it with a team or the org.
:func:`set_reviewers` edits the required and optional reviewers afterwards.
:func:`sync` brings comments, suggestions, votes and the review state back,
then sends the plan's newest revision if the Library doesn't have it.

The Library numbers its own revisions, so ``plan_shares`` keeps which local
revision its head holds. Everything pulled is stored under ``teammate:<sub>``,
whoever wrote it: a pulled row is never the local person's, so a vote from the
Library can't become their confirmed pick. A teammate's open comments and
suggestions are how they ask for changes (the API has no verdict of its own):
Request changes carries them to the supervisor (:func:`plan_docs.pending`).

The Library's head moving on without this plan (a teammate with edit rights
changed the text) isn't merged: the next send is refused as stale and says so.

Calls raise ``RuntimeError`` with a readable reason, as ``brindle docs`` does.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from brindle import plan_docs
from brindle.db import DB
from brindle.pro import analysis_docs as ad
from brindle.redact import mask, mask_data

log = logging.getLogger(__name__)

TEAM_FEATURE = "analysis_docs_team"
SCOPE_KINDS = ("group", "org")
CLAIM_FOR = 60.0             # seconds a create or push in flight holds the plan; then it is presumed dead
REVIEW = ("rev", "reviewers", "sign_offs", "pending_reviewers", "ready")
STATUSES = ("open", "resolved", "accepted", "rejected")
REVIEWER_GRANT = {"can_suggest": True, "can_comment": True}   # a reviewer must be able to answer


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
    review = json.dumps({k: view.get(k) for k in REVIEW}, sort_keys=True)
    with db.tx() as c:
        return c.execute("UPDATE plan_shares SET review=? WHERE plan_id=? AND review<>?",
                         (review, plan_id, review)).rowcount == 1


def _grant(doc_id: str, reviewers: list[dict], permissions: dict | None, client, store) -> None:
    """Scope members start read-only, and ``PUT /permissions`` replaces the
    list: add to what is there, giving each reviewer the right to comment and
    suggest. A reviewer taken off the list keeps the grant."""
    held = ad.api("GET", _path(doc_id), client=client, store=store).get("permissions") or {}
    grants = {**held, **(permissions or {})}
    for r in reviewers:
        grants[r["sub"]] = {"can_edit": False, **grants.get(r["sub"], {}), **REVIEWER_GRANT}
    if grants != held:
        ad.api("PUT", _path(doc_id, "/permissions"), {"permissions": grants}, client=client, store=store)


# -- sharing -----------------------------------------------------------------------------


def _claim_new(db: DB, plan_id: str, owner: str, now: float) -> dict:
    """The plan's link, claimed if the Library has no document for it yet: a
    second share while the first is creating it is refused, so one plan never
    becomes two documents. A claim older than CLAIM_FOR is presumed dead."""
    with db.tx() as c:
        c.execute("DELETE FROM plan_shares WHERE plan_id=? AND doc_id IS NULL AND claimed_at<?",
                  (plan_id, now - CLAIM_FOR))
        won = c.execute("INSERT OR IGNORE INTO plan_shares (plan_id, owner, claimed_at) VALUES (?,?,?)",
                        (plan_id, owner, now)).rowcount == 1
    share = link(db, plan_id)
    if share["doc_id"] is None and not won:
        raise RuntimeError("this plan is already being shared")
    return share


def _create(db: DB, p: dict, owner: str, client, store) -> dict:
    """Save the plan to ``owner``'s Library. The link is written the moment
    the document exists, so a failure after this point doesn't make another."""
    cur = plan_docs.revision(db, p["id"])
    try:
        body = {"scope": {"kind": "user", "id": owner},
                "files": [{"name": f"plan-{p['id']}.md", "md": cur["markdown"]}],
                "source": {"kind": "goal", "ref": p["id"], "session": p["root_id"]}, "design": _content(cur)}
        made = ad.api("POST", "/analysis-docs", body, client=client, store=store)
        with db.tx() as c:
            c.execute("UPDATE plan_shares SET doc_id=?, remote_rev=?, local_rev=?, claimed_at=NULL WHERE plan_id=?",
                      (made["id"], made["version"], cur["rev"], p["id"]))
    except Exception:
        with db.tx() as c:
            c.execute("DELETE FROM plan_shares WHERE plan_id=? AND doc_id IS NULL", (p["id"],))
        raise
    return link(db, p["id"])


def share(db: DB, plan_id: str, scope: Any, *, reviewers: Any = None, permissions: dict | None = None,
          folder: dict | None = None, client=None, store=None, now: float | None = None) -> dict:
    """Save the plan to the Library and share it with ``scope`` (a team or
    the org). Safe to run again: a plan keeps its one document, so this also
    adds a scope or finishes a share that failed partway. ``reviewers``:
    ``[{sub, required?}]``; ``permissions``: ``{sub: {can_edit, can_suggest,
    can_comment}}`` for other teammates; ``folder``: ``{scope, id}`` instead
    of the default ``Designs`` folder. Returns the link."""
    p = plan_docs._plan(db, plan_id)
    client, store = ad._session(client, store)
    ent = _require_team(store)
    scope = _scope(scope)
    people = None if reviewers is None else _reviewers(reviewers)
    row = _claim_new(db, p["id"], ent.sub, time.time() if now is None else now)
    _own(row, ent)
    doc_id = (row if row["doc_id"] else _create(db, p, ent.sub, client, store))["doc_id"]
    ad.api("POST", ad._doc_path(doc_id, "/share"), {"scope": scope}, client=client, store=store)
    if people or permissions:
        _grant(doc_id, people or [], permissions, client, store)
    if people is not None:
        _remember(db, p["id"], ad.api("PUT", _path(doc_id, "/reviewers"), {"reviewers": people},
                                      client=client, store=store))
    if folder is not None:
        _remember(db, p["id"], ad.api("PUT", _path(doc_id, "/folder"), {"folder": folder},
                                      client=client, store=store))
    return link(db, p["id"])


def set_reviewers(db: DB, plan_id: str, reviewers: Any, *, client=None, store=None) -> dict:
    """Replace the required and optional reviewers of a shared plan. Returns the link."""
    p = plan_docs._plan(db, plan_id)
    client, store = ad._session(client, store)
    share_ = _shared(db, p["id"], store)
    people = _reviewers(reviewers)
    _grant(share_["doc_id"], people, None, client, store)
    if _remember(db, p["id"], ad.api("PUT", _path(share_["doc_id"], "/reviewers"), {"reviewers": people},
                                     client=client, store=store)):
        plan_docs._changed(db, p, p["rev"])
    return link(db, p["id"])


# -- pulling what teammates wrote --------------------------------------------------------


def _author(sub: Any) -> str | None:
    return plan_docs.TEAMMATE + sub if isinstance(sub, str) and sub else None


def _when(at: Any, now: float) -> float:
    return float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else now


def _anchor(raw: Any, cur: dict) -> dict | None:
    """A pulled anchor in the local shape, or None when it isn't one (a bad
    row would otherwise break reading the whole plan). The Library doesn't
    return an insertion point's context, so it is taken from the revision."""
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
    body = plan_docs._body(cur, section)
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


def _pull_thread(c, plan_id: str, rev: int, cur: dict, known: dict, t: Any, now: float) -> int:
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
    return changes


def _pull_votes(c, plan_id: str, rev: int, decisions: list, now: float) -> int:
    """Make the teammates' votes the Library's current ones (it answers with
    the head revision's only, so a vote from an older revision goes)."""
    wanted = {(d["id"], plan_docs.TEAMMATE + sub): option for d in decisions
              for option, subs in (d.get("votes") or {}).items() for sub in subs}
    pattern = plan_docs.TEAMMATE + "%"
    held = {(r["decision_id"], r["voter"]): r["option_id"] for r in c.execute(
        "SELECT decision_id, voter, option_id FROM plan_votes WHERE plan_id=? AND voter LIKE ?", (plan_id, pattern))}
    if held == wanted:
        return 0
    c.execute("DELETE FROM plan_votes WHERE plan_id=? AND voter LIKE ?", (plan_id, pattern))
    c.executemany("INSERT INTO plan_votes (plan_id, decision_id, voter, option_id, rev, at) VALUES (?,?,?,?,?,?)",
                  [(plan_id, d, voter, option, rev, now) for (d, voter), option in wanted.items()])
    return 1


def _pull(db: DB, p: dict, share_: dict, view: dict) -> int:
    rev, now = share_["local_rev"], time.time()
    cur = plan_docs.revision(db, p["id"], rev)
    changes = 0
    with db.tx() as c:
        known = {r["remote_id"]: {"local_id": r["local_id"], "status": r["status"]} for r in c.execute(
            "SELECT remote_id, local_id, status FROM plan_share_threads WHERE plan_id=?", (p["id"],))}
        for t in view.get("threads") or []:
            changes += _pull_thread(c, p["id"], rev, cur, known, t, now)
        changes += _pull_votes(c, p["id"], rev, view.get("decisions") or [], now)
    changes += _remember(db, p["id"], view)
    if changes:
        plan_docs._changed(db, p, p["rev"])
    return changes


# -- sending the newest revision ---------------------------------------------------------


def _push(db: DB, p: dict, view: dict, client, store, now: float) -> bool:
    """Send the plan's newest revision, on the head this plan last saw. One
    send at a time: while another holds the plan this one leaves it alone.
    Returns whether the Library took a new revision."""
    with db.tx() as c:
        held = c.execute("UPDATE plan_shares SET claimed_at=? WHERE plan_id=? AND (claimed_at IS NULL OR claimed_at<?)",
                         (now, p["id"], now - CLAIM_FOR)).rowcount
    if not held:
        return False
    try:
        share_, cur = link(db, p["id"]), plan_docs.revision(db, p["id"])
        if share_["local_rev"] == cur["rev"]:
            return False
        content = _content(cur)
        if view.get("rev") != share_["remote_rev"] and _content(view) == mask_data(content):
            remote = view["rev"]      # an earlier send landed and only its answer was lost
        else:
            status, resp = ad._call("POST", _path(share_["doc_id"], "/revisions"),
                                    {"rev": share_["remote_rev"], **content}, client=client, store=store)
            if status == 409 and resp.get("error") == "stale":
                raise RuntimeError(f"the Library's copy has a newer revision (r{resp.get('rev')}) that this plan "
                                   f"doesn't have, so r{cur['rev']} wasn't sent")
            if status != 200:
                raise RuntimeError(resp.get("error") or f"HTTP {status}")
            remote = resp["rev"]
            _remember(db, p["id"], resp)
        with db.tx() as c:
            c.execute("UPDATE plan_shares SET remote_rev=?, local_rev=? WHERE plan_id=?", (remote, cur["rev"], p["id"]))
        return True
    finally:
        with db.tx() as c:
            c.execute("UPDATE plan_shares SET claimed_at=NULL WHERE plan_id=?", (p["id"],))


def sync(db: DB, plan_id: str, *, client=None, store=None, now: float | None = None) -> dict:
    """Bring back what teammates wrote, then send the plan's newest revision
    if the Library doesn't have it. Pulled first, so a refused send still
    leaves their comments here. Returns the link, plus ``pulled`` (how many
    things changed here) and ``pushed``."""
    p = plan_docs._plan(db, plan_id)
    client, store = ad._session(client, store)
    share_ = _shared(db, p["id"], store)
    view = ad.api("GET", _path(share_["doc_id"]), client=client, store=store)
    pulled = _pull(db, p, share_, view)
    pushed = share_["local_rev"] != p["rev"] and _push(db, p, view, client, store,
                                                       time.time() if now is None else now)
    return {**link(db, p["id"]), "pulled": pulled, "pushed": pushed}
