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

What the person writes after sharing goes up as theirs: comments, replies,
suggestions, thread actions and votes. What they wrote before stays here. A
suggestion they accepted goes up as the Library's own ``accept``, which makes
the revision and closes the thread together.

Calls raise ``RuntimeError`` with a readable reason, as ``brindle docs`` does.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

from brindle import plan_docs, plans, visuals
from brindle.db import DB
from brindle.pro import analysis_docs as ad
from brindle.redact import mask, mask_data
from brindle.service.errors import ServiceError

log = logging.getLogger(__name__)

TEAM_FEATURE = "analysis_docs_team"
SCOPE_KINDS = ("group", "org")
CLAIM_FOR = 60.0             # seconds a create or push in flight holds the plan; then it is presumed dead
FIRST_REVISION = 1           # what the Library numbers a new document's head
REVIEW = ("rev", "reviewers", "sign_offs", "pending_reviewers", "ready", "policy_reviewers", "pending_policies")
STATUSES = ("open", "resolved", "accepted", "rejected")
REVIEWER_GRANT = {"can_suggest": True, "can_comment": True}   # a reviewer must be able to answer
# What the person's change to a thread is called in the Library. Not an accept:
# that is sent only ahead of the revision holding it (_send_accepts).
THREAD_ACTIONS = {("comment", "resolved"): "resolve", ("comment", "open"): "reopen",
                  ("suggestion", "rejected"): "reject"}


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


def _claim_new(db: DB, plan_id: str, owner: str, now: float) -> tuple[dict, bool]:
    """The plan's link, claimed if the Library has no document for it yet: a
    second share while the first is creating it is refused, so one plan never
    becomes two documents. A claim older than CLAIM_FOR is taken over, and
    the second value says so: that create may have reached the Library."""
    with db.tx() as c:
        resumed = c.execute("UPDATE plan_shares SET claimed_at=? WHERE plan_id=? AND doc_id IS NULL AND claimed_at<?",
                            (now, plan_id, now - CLAIM_FOR)).rowcount == 1
        won = c.execute("INSERT OR IGNORE INTO plan_shares (plan_id, owner, claimed_at) VALUES (?,?,?)",
                        (plan_id, owner, now)).rowcount == 1
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
    row, resumed = _claim_new(db, p["id"], ent.sub, time.time() if now is None else now)
    _own(row, ent)
    doc_id = row["doc_id"] or _create(db, p, ent.sub, cur, content, resumed, client, store)
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
    _review_open(p)
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


def _link_own(c, plan_id: str, owner: str, known: dict, threads: Any) -> None:
    """Pair what the Library holds from this account with the person's rows
    not linked yet, oldest first: what was sent from here, whether or not its
    answer arrived, comes back as itself instead of as a teammate's, and
    isn't sent twice. Text is compared as it was sent, secrets masked."""
    waiting: dict[tuple, list[int]] = {}
    for r in c.execute("SELECT id, parent_id, kind, body, after FROM plan_threads WHERE plan_id=? AND author=?"
                       " AND id NOT IN (SELECT local_id FROM plan_share_threads WHERE plan_id=?) ORDER BY id",
                       (plan_id, plan_docs.PERSON, plan_id)):
        waiting.setdefault((r["parent_id"], r["kind"], mask(r["body"]), mask(r["after"])), []).append(r["id"])

    def pair(remote_id: int, key: tuple, status: Any) -> None:
        if waiting.get(key) and remote_id not in known:
            local = waiting[key].pop(0)
            c.execute("INSERT INTO plan_share_threads (plan_id, remote_id, local_id, status) VALUES (?,?,?,?)",
                      (plan_id, remote_id, local, status))
            known[remote_id] = {"local_id": local, "status": status}

    for t in threads or []:
        if not isinstance(t, dict) or not isinstance(t.get("id"), int):
            continue
        if t.get("author") == owner:
            pair(t["id"], (None, t.get("kind"), t.get("text") or "", t.get("after")), t.get("status"))
        parent = known.get(t["id"])
        for r in (t.get("replies") or []) if parent else []:
            if isinstance(r, dict) and isinstance(r.get("id"), int) and r.get("author") == owner:
                pair(r["id"], (parent["local_id"], "reply", r.get("text") or "", None), "open")


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
    and answers with the whole view after it."""

    def __init__(self, doc_id: str, view: dict, client, store):
        self.doc_id, self.view, self.client, self.store = doc_id, view, client, store

    @property
    def rev(self) -> Any:
        return self.view.get("rev")

    def write(self, tail: str, body: dict) -> tuple[int, dict]:
        status, resp = ad._call("POST", _path(self.doc_id, tail), {"rev": self.rev, **body},
                                client=self.client, store=self.store)
        if status == 200:
            self.view = resp
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


def _send_accepts(db: DB, plan_id: str, head: _Head, newest: int) -> bool:
    """Send each suggestion accepted here since the head last held this plan.
    The Library's accept makes the revision and closes the thread, and can't
    be sent later: on a head that already holds the change it would apply it
    again, and it refuses ``resolve`` on a suggestion. So only ahead of the
    revision; one it refuses here is carried by that revision instead."""
    share_ = link(db, plan_id)
    since = plan_docs.revision(db, plan_id, share_["local_rev"])["created_at"]
    rows = db.conn.execute(
        "SELECT s.remote_id FROM plan_threads t JOIN plan_share_threads s ON s.plan_id=t.plan_id AND s.local_id=t.id"
        " WHERE t.plan_id=? AND t.kind='suggestion' AND t.status='accepted' AND s.status='open' AND s.remote_id>0"
        " AND t.updated_at>? ORDER BY t.updated_at", (plan_id, since)).fetchall()
    sent = False
    local = share_["local_rev"]
    for row in rows:
        if not head.send(f"/threads/{row['remote_id']}", {"action": "accept"}):
            continue
        sent = True
        local = _landed(db, plan_id, local, newest, head.view) or local
        with db.tx() as c:
            c.execute("UPDATE plan_share_threads SET status='accepted' WHERE plan_id=? AND remote_id=?",
                      (plan_id, row["remote_id"]))
            _set_head(c, plan_id, head.rev, local)
    return sent


def _send_text(db: DB, plan_id: str, head: _Head) -> bool:
    """Bring the Library's head up to the plan's newest revision, on the head
    this plan last saw. If the head moved without this plan it is left alone:
    that is the gate's ``conflict`` (or a redirected plan's text, which isn't
    imported). Returns whether the head holds a new revision from here."""
    share_, cur = link(db, plan_id), plan_docs.revision(db, plan_id)
    moved = False
    if head.rev != share_["remote_rev"]:
        held = _landed(db, plan_id, share_["local_rev"], cur["rev"], head.view)
        if held is None:
            return False
        with db.tx() as c:        # an earlier send landed and only its answer was lost
            _set_head(c, plan_id, head.rev, held)
        share_, moved = link(db, plan_id), True
    if share_["local_rev"] == cur["rev"]:
        return moved
    moved = _send_accepts(db, plan_id, head, cur["rev"]) or moved
    if link(db, plan_id)["local_rev"] == cur["rev"]:
        return moved
    status, resp = head.write("/revisions", _content(cur))
    if status == 409 and resp.get("error") == "stale":
        head.view = {**head.view, "rev": resp.get("rev")}        # moved since it was read: a conflict for now
        return moved
    if status != 200:
        raise RuntimeError(resp.get("error") or f"HTTP {status}")
    with db.tx() as c:
        _set_head(c, plan_id, head.rev, cur["rev"])
    return True


def _send_threads(db: DB, plan_id: str, owner: str, head: _Head) -> None:
    """Send the person's new comments, suggestions and replies. A reply waits
    for its thread to be in the Library (a supervisor's never will be)."""
    cur = plan_docs.revision(db, plan_id)
    rows = db.conn.execute(
        "SELECT * FROM plan_threads WHERE plan_id=? AND author=?"
        " AND id NOT IN (SELECT local_id FROM plan_share_threads WHERE plan_id=?) ORDER BY id",
        (plan_id, plan_docs.PERSON, plan_id)).fetchall()
    for t in rows:
        if t["parent_id"] is not None:
            parent = db.conn.execute("SELECT remote_id FROM plan_share_threads WHERE plan_id=? AND local_id=?"
                                     " AND remote_id>0", (plan_id, t["parent_id"])).fetchone()
            if parent is None:
                continue
            tail, body = "/comments", {"parent": parent["remote_id"], "text": t["body"]}
        else:
            where = plan_docs._where(cur, json.loads(t["anchor"]))
            if t["status"] != "open" or where["state"] == "detached":
                continue
            if t["kind"] == "comment":
                anchor = {k: v for k, v in where.items() if k not in ("state", "prefix", "suffix")}
                tail, body = "/comments", {"anchor": anchor, "text": t["body"]}
            else:
                tail, body = "/suggestions", {"section": where["section"], "start": where["start"],
                                              "before": where["quote"], "after": t["after"], "text": t["body"]}
        if head.send(tail, body):
            with db.tx() as c:
                _link_own(c, plan_id, owner, _known(c, plan_id), head.view.get("threads"))


def _send_actions(db: DB, plan_id: str, head: _Head) -> None:
    """Send what the person did to threads the Library has: resolved, reopened, rejected."""
    rows = db.conn.execute(
        "SELECT t.kind, t.status, s.remote_id FROM plan_threads t JOIN plan_share_threads s"
        " ON s.plan_id=t.plan_id AND s.local_id=t.id"
        " WHERE t.plan_id=? AND t.parent_id IS NULL AND s.remote_id>0 AND t.status<>s.status", (plan_id,)).fetchall()
    for r in rows:
        action = THREAD_ACTIONS.get((r["kind"], r["status"]))
        if action and head.send(f"/threads/{r['remote_id']}", {"action": action}):
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
        head = _Head(share_["doc_id"], view, client, store)
        pushed = _send_text(db, p["id"], head)
        _send_threads(db, p["id"], share_["owner"], head)
        _send_actions(db, p["id"], head)
        _send_votes(db, p["id"], head)
        if head.rev != view.get("rev"):        # a new head: votes on the old one no longer count
            local_rev = link(db, p["id"])["local_rev"]
            with db.tx() as c:
                _pull_votes(c, p["id"], share_["owner"], local_rev, head.view.get("decisions") or [], now)
        if head.view is not view:
            _remember(db, p["id"], head.view)
        return pushed
    finally:
        with db.tx() as c:
            c.execute("UPDATE plan_shares SET claimed_at=NULL WHERE plan_id=?", (p["id"],))


def sync(db: DB, plan_id: str, *, client=None, store=None, now: float | None = None) -> dict:
    """Take a teammate's new text if the plan hasn't changed here, bring back
    what teammates wrote, then send what was done here. Pulled first, so a
    refused send still leaves their comments here. Returns the link, plus
    ``pulled`` (how many things changed here) and ``pushed``."""
    p = plan_docs._plan(db, plan_id)
    client, store = ad._session(client, store)
    share_ = _shared(db, p["id"], store)
    view = ad.api("GET", _path(share_["doc_id"]), client=client, store=store)
    imported = _import(db, p, share_, view)
    share_ = link(db, p["id"])
    if imported:
        p = plan_docs._plan(db, p["id"])
    pulled = imported + _pull(db, p, share_, view)
    pushed = _send(db, p, view, client, store, time.time() if now is None else now)
    if pushed or imported:
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
    if _remember(db, p["id"], ad.api("POST", _path(share_["doc_id"], "/sign-offs"), {"rev": share_["remote_rev"]},
                                     client=client, store=store)):
        plan_docs._changed(db, p, p["rev"])
    return link(db, p["id"])


def sync_open(db: DB, *, client=None, store=None) -> int:
    """Sync every shared plan still in review, for the daemon's background
    pass (a building plan's review is closed). One plan's failure doesn't
    stop the rest. Returns how many synced."""
    client, store = ad._session(client, store)
    try:
        _require_team(store)
    except Exception:  # noqa: BLE001 - logged out, air-gapped or no Team plan: nothing to bring back
        return 0
    done = 0
    for row in db.conn.execute("SELECT s.plan_id FROM plan_shares s JOIN plans p ON p.id=s.plan_id"
                               " WHERE s.doc_id IS NOT NULL AND p.state IN ('open','redirected')").fetchall():
        try:
            sync(db, row["plan_id"], client=client, store=store)
            done += 1
        except Exception:  # noqa: BLE001 - offline, another account's plan: the next pass tries again
            log.debug("brindle: couldn't sync shared plan %s", row["plan_id"], exc_info=True)
    return done
