"""A teammate's reader for a design someone else shared (brindle Pro, Team plan
and up): the other half of :mod:`brindle.pro.shared_docs`. The contract is
docs/shared-designs-api.md in pawdelta-web.

Everything here is keyed by the Library's document id. A teammate has no plan
on this machine, so nothing is written to it: no plan row, no copy on disk. A
read asks the Library each time and the app holds the answer in memory, so
losing access means losing the doc. Review changes stay in the signed-in
account's outbox until the Library takes or refuses them. The reader gets
delivery receipts for the revision and item it changed.

A teammate reads, comments, suggests, votes and signs off. They never approve
or build, and the reader has no edit mode: nothing here calls ``/edits`` or
``/revisions``, and it only resolves or reopens a thread, because accepting a
suggestion makes a new revision.
"""

from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any
from urllib.parse import quote, urlencode

from brindle import plan_docs, plans
from brindle.pro import analysis_docs as ad
from brindle.pro import shared_docs as sd
from brindle.redact import mask
from brindle.service.dto import SharedDesignDTO, SharePeopleDTO
from brindle.service.errors import ServiceError

THREAD_ACTIONS = ("resolve", "reopen")
ANCHOR_FIELDS = ("kind", "section", "start", "end", "quote", "decision", "option", "state")
ANCHOR_KINDS = ("text", "diagram", "decision", "section")
REVISION_SOURCES = ("proposal", "edit", "suggestion")
UNTITLED = "Plan"
ON_PLAN_ROLES = ("author", "required", "optional", "editor", "suggester", "commenter")   # SharePersonDTO's


def _who(sub: Any, me: str) -> str:
    return plan_docs.PERSON if sub == me else str(sub)


def _at(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _anchor(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or raw.get("kind") not in ANCHOR_KINDS:
        return None
    out = {k: raw[k] for k in ANCHOR_FIELDS if raw.get(k) is not None}
    if "quote" in out:
        out["quote"] = mask(out["quote"])
    return out


def _reply(r: Any, me: str) -> dict | None:
    if not isinstance(r, dict) or not isinstance(r.get("id"), int) or not isinstance(r.get("author"), str):
        return None
    return {"id": r["id"], "author": _who(r["author"], me), "text": mask(r.get("text") or ""), "at": _at(r.get("at")),
            "from_supervisor": r.get("from_supervisor") is True}


def _thread(t: Any, me: str) -> dict | None:
    """A Library thread as the doc shows it, or None for a row this brindle can't read."""
    if (not isinstance(t, dict) or not isinstance(t.get("id"), int) or t.get("kind") not in ("comment", "suggestion")
            or t.get("status") not in sd.STATUSES or not isinstance(t.get("author"), str)):
        return None
    anchor = _anchor(t.get("anchor"))
    if anchor is None:
        return None
    out = {"id": t["id"], "kind": t["kind"], "rev": t["rev"] if isinstance(t.get("rev"), int) else 0,
           "author": _who(t["author"], me), "text": mask(t.get("text") or ""), "anchor": anchor,
           "status": t["status"], "at": _at(t.get("at")), "question": t.get("question") is True,
           "replies": [r for r in (_reply(x, me) for x in t.get("replies") or []) if r]}
    if t["kind"] == "suggestion":
        out.update(before=anchor.get("quote", ""), after=mask(t.get("after") or ""))
    return out


def _gate(view: dict, me: str) -> dict:
    """The review's standing as this account sees it, counted as the author's gate is."""
    read = plans._review_rows(view)
    people, rules = read or ({}, [])
    asked: dict[str, float] = {}
    for t in view.get("threads") or []:
        if isinstance(t, dict) and t.get("status") == "open" and not t.get("question") and isinstance(t.get("author"), str):
            asked[t["author"]] = max(asked.get(t["author"], 0.0), _at(t.get("at")))
    rows, policies, required, met, unmet, changes = plans.tally(people, rules, plans._signed(view), asked,
                                                                read is not None, me)
    if read is None:
        state, reason = "waiting", "brindle can't read this plan's review state."
    elif changes:
        state, reason = plans.CHANGES, f"Changes requested by {', '.join(changes)}."
    elif unmet:
        state, reason = "waiting", f"Waiting on {', '.join(unmet)} ({met} of {len(required)} required sign-offs)."
    else:
        state, reason = "ready", None
    return {"state": state, "reason": reason, "required": len(required), "signed": met, "waiting_on": unmet,
            "reviewers": rows, "policies": policies}


def _can(view: dict, me: str, name: str) -> bool:
    """What the Library says this account may do: its own flags, else its row of the grants."""
    if isinstance(view.get(name), bool):
        return view[name]
    grants = view.get("permissions")
    own = grants.get(me) if isinstance(grants, dict) else None
    return isinstance(own, dict) and bool(own.get(name) or own.get("can_edit"))


def _can_sign(view: dict, me: str, author: Any) -> bool:
    """Use the Library's decision, or infer eligibility on older servers."""
    if "can_sign_off" in view:
        return view["can_sign_off"] is True
    people = view.get("reviewers") or []
    return me == author or bool(view.get("policy_reviewers")) or any(isinstance(r, dict) and r.get("sub") == me for r in people)


def _answers(view: dict, decision: str, cast: dict[str, list[str]], me: str) -> list[dict]:
    """A decision's own answers (brindle.plan_answers), each locked once someone else picked it."""
    from brindle import plan_answers

    return [{"id": a["id"], "text": mask(a["text"])[:plan_answers.MAX_TEXT], "author": _who(a["author"], me),
             "at": _at(a.get("at")), "locked": any(s != a["author"] for s in cast.get(a["id"], []))}
            for d, a in plan_answers._theirs(view.get("decisions")) if d == decision]


def _dto(doc_id: str, view: dict, me: str, *, review: bool = True, names: dict | None = None) -> dict:
    if not isinstance(view.get("rev"), int):
        raise RuntimeError("the Library's answer has no revision")
    content = sd._readable(view)                 # checked and masked as a supervisor's proposal is: it is drawn here
    votes = {d.get("id"): d.get("votes") or {} for d in view.get("decisions") or [] if isinstance(d, dict)}
    decisions = []
    for d in content["decisions"]:
        cast = {option: [str(s) for s in subs] for option, subs in votes.get(d["id"], {}).items() if isinstance(subs, list)}
        decisions.append({**d, "votes": {option: [_who(s, me) for s in subs] for option, subs in cast.items()},
                          "confirmed": next((option for option, subs in cast.items() if me in subs), None),
                          "answers": _answers(view, d["id"], cast, me)})
    history = [r for r in view.get("revisions") or [] if isinstance(r, dict) and isinstance(r.get("rev"), int)
               and r.get("source") in REVISION_SOURCES]
    author = history[0].get("author") if history else view.get("author")
    threads = [t for t in (_thread(x, me) for x in view.get("threads") or []) if t]
    for thread in threads:
        for row in [thread, *thread["replies"]]:
            row["author_name"] = (names or {}).get(row["author"], "A teammate") if row["author"] != plan_docs.PERSON else None
    sign_off = view.get("sign_off") if review else None
    raw_sign_offs = view.get("sign_offs") if review else None
    sign_offs = [{"sub": s["sub"], "rev": s["rev"], "at": _at(s.get("at")), "optional": s.get("optional")}
                 for s in (raw_sign_offs or []) if isinstance(s, dict)
                 and isinstance(s.get("sub"), str) and isinstance(s.get("rev"), int)]
    return SharedDesignDTO(
        id=doc_id, session="", title=view.get("title") or UNTITLED, rev=view["rev"], state="open", ask=content["ask"],
        markdown=content["markdown"], lede=content["lede"], sections=content["sections"], decisions=decisions,
        split=[], queued=0, created_at=_at(history[0].get("at")) if history else 0.0,
        revisions=[{"rev": r["rev"], "author": _who(r.get("author"), me), "source": r["source"],
                    "changed": r.get("changed") or [], "at": _at(r.get("at"))} for r in history],
        threads=threads, teammate_names=names or {},
        gate=_gate(view, me) if review else None, me=me,
        author=_who(author, me) if isinstance(author, str) else None,
        can_edit=_can(view, me, "can_edit"),     # may write in its live draft (the reader's Editing)
        can_comment=_can(view, me, "can_comment"), can_suggest=_can(view, me, "can_suggest"),
        can_sign_off=review and _can_sign(view, me, author),
        sign_off=sign_off if sign_off in ("required", "optional") else None,
        sign_offs=sign_offs,
        access_request=view.get("access_request"),
        signed_off=any(s["sub"] == me and s["rev"] == view["rev"] for s in sign_offs)).to_json()


def _title(doc_id: str, client, store) -> str | None:
    """Read the title from the doc's own entry, when available."""
    from brindle.pro import auth

    try:
        meta = ad.api("GET", ad._doc_path(doc_id), client=client, store=store)
    except (RuntimeError, auth.AuthError, auth.TransportError):
        return None
    title = meta.get("title")
    return mask(title) if isinstance(title, str) and title.strip() else None


def _titled(doc_id: str, view: dict, client, store) -> dict:
    if isinstance(view.get("title"), str) and view["title"].strip():
        return view
    title = _title(doc_id, client, store)
    return {**view, "title": title} if title else view


def _deliver(db, me, doc_id, client, store, **options):
    from brindle import plan_outbox

    def call(path, payload):
        return ad.api("POST", sd._path(doc_id, path), payload, client=client, store=store)

    def load():
        return ad.api("GET", sd._path(doc_id), client=client, store=store)

    return plan_outbox.deliver(db, me, doc_id, call, load, **options)


def drain_pending(db, owner, client, store):
    """Deliver a signed-in reviewer's changes on the daemon's shared-plan sync pass."""
    docs = db.conn.execute("SELECT doc FROM plan_outbox WHERE owner=? AND state='waiting' "
                           "GROUP BY doc ORDER BY min(seq)", (owner,)).fetchall()
    for row in docs:
        try:
            _deliver(db, owner, row["doc"], client, store)
        except RuntimeError:
            logging.getLogger(__name__).warning("Library plan delivery is still pending for %s", row["doc"])


def _ask(doc_id: str, method: str, tail: str, body: dict | None, client, store) -> dict:
    from brindle import plan_outbox

    if method == "POST" and tail in ("/comments", "/suggestions", "/votes", "/sign-offs") and not (body or {}).get("question"):
        client, store = ad._session(client, store)
        me = ad.require_entitlement(store).sub
        with plan_outbox.destination() as (db, client_id, repo, legacy):
            view, items = _deliver(db, me, doc_id, client, store, item=(client_id, tail, body, repo), legacy=legacy)
        if view is None:
            return {"id": doc_id, "me": me, "outbox": items}
        names = sd.member_names(doc_id, client=client, store=store) if _can(view, me, "can_edit") else None
        return {**_dto(doc_id, _titled(doc_id, view, client, store), me, names=names), "outbox": items}
    return _asked(doc_id, lambda call: call(method, tail, body), client, store)


def _asked(doc_id: str, work, client, store) -> dict:
    """The design as ``work`` leaves it: ``work(call)`` makes its Library calls
    with ``call(method, tail, body)``, under the design, and returns the view."""
    client, store = ad._session(client, store)
    me = ad.require_entitlement(store).sub
    view = work(lambda method, tail, body=None: ad.api(method, sd._path(doc_id, tail), body,
                                                       client=client, store=store))
    names = sd.member_names(doc_id, client=client, store=store) if _can(view, me, "can_edit") else None
    return _dto(doc_id, _titled(doc_id, view, client, store), me, names=names)


def _rev(rev: Any) -> int:
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise ServiceError("invalid", "name the revision on screen", 400)
    return rev


def _text(text: Any) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ServiceError("invalid", "write something first", 400)
    return text


def read(doc_id: str, *, client=None, store=None, db=None) -> dict:
    """The design as this account may read it now."""
    client, store = ad._session(client, store)
    me = ad.require_entitlement(store).sub
    items = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        title = pool.submit(copy_context().run, _title, doc_id, client, store)
        if db is None:
            view = ad.api("GET", sd._path(doc_id), client=client, store=store)
        else:
            view, items = _deliver(db, me, doc_id, client, store, read=True)
        # The design carries the current permission. The directory requires edit access.
        names = sd.member_names(doc_id, client=client, store=store) if _can(view, me, "can_edit") else None
        if not (isinstance(view.get("title"), str) and view["title"].strip()) and (name := title.result()):
            view = {**view, "title": name}
    return {**_dto(doc_id, view, me, names=names), **({"outbox": items} if items else {})}


def request_access(doc_id: str, role: Any, sub: str | None = None, *, action: str = "grant", client=None, store=None) -> dict:
    """Ask as the signed-in reader, or grant a request as its author; the Library checks both."""
    if role not in ("comment", "edit"):
        raise ServiceError("invalid", "request comment or edit access", 400)
    if action not in ("grant", "decline"):
        raise ServiceError("invalid", "grant or decline the access request", 400)
    tail = "/access-requests" + (f"/{quote(sub, safe='')}/{action}" if sub is not None else "")
    return _ask(doc_id, "POST", tail, {"role": role}, client, store)


def _str(raw: Any) -> str:
    return raw if isinstance(raw, str) else ""


def _person(raw: Any) -> dict | None:
    """One row of the Library's people search as the share panel reads it, or
    None for a row nobody could be added by (no account id). A field an older
    Library doesn't send takes its empty value, and a plan role a newer one
    names that this brindle doesn't know reads as none (``on_plan`` still
    says they have it): one such row must never fail the whole search."""
    if not isinstance(raw, dict) or not _str(raw.get("id")):
        return None
    sub, email, role = raw["id"], _str(raw.get("email")), raw.get("on_plan_role")
    teams = raw.get("teams") if isinstance(raw.get("teams"), list) else []
    return {"id": sub, "display_name": _str(raw.get("display_name")).strip() or email or sub, "email": email,
            "teams": [{"id": t["id"], "name": _str(t.get("name")) or t["id"]} for t in teams
                      if isinstance(t, dict) and _str(t.get("id"))],
            "role": _str(raw.get("role")),
            "on_plan": raw["on_plan"] if isinstance(raw.get("on_plan"), bool) else bool(role),
            "on_plan_role": role if role in ON_PLAN_ROLES else None}


def people(doc_id: str, filters: dict, *, client=None, store=None) -> dict:
    """Search people the Library says can already read this design, or with
    ``include_unshared`` also the org's members who could be given it."""
    client, store = ad._session(client, store)
    ad.require_entitlement(store)
    query = urlencode({k: v for k, v in filters.items() if k in ("q", "team", "role", "on_plan", "include_unshared")})
    got = ad.api("GET", sd._path(doc_id, "/people") + (f"?{query}" if query else ""), client=client, store=store)
    rows = got.get("people") if isinstance(got.get("people"), list) else []
    return SharePeopleDTO.model_validate({"people": [p for p in map(_person, rows) if p]}).model_dump()


def reviewer(doc_id: str, sub: str, required: bool | None, *, client=None, store=None) -> dict:
    """Change one person without replacing anyone else's invitation."""
    if required is None:
        return _ask(doc_id, "DELETE", f"/reviewers/{quote(sub, safe='')}", None, client, store)
    return _asked(doc_id, lambda call: sd.ask_to_sign(call, sub, required), client, store)


def timeline(doc_id: str, *, client=None, store=None) -> dict:
    """Read the Library's retained revision metadata and sign-offs without keeping a copy."""
    client, store = ad._session(client, store)
    me = ad.require_entitlement(store).sub
    view = ad.api("GET", sd._path(doc_id), client=client, store=store)
    revisions, sign_offs = view.get("revisions"), view.get("sign_offs")
    events = [{"id": f"revision:{r['rev']}", "kind": "revision", "actor": _who(r.get("author"), me),
               "rev": r["rev"], "version": None, "at": _at(r.get("at")),
               "source": r.get("source"), "changed": r.get("changed") or []}
              for r in (revisions if isinstance(revisions, list) else [])
              if isinstance(r, dict) and isinstance(r.get("rev"), int)]
    events += [{"id": f"sign_off:{s['rev']}:{s['sub']}", "kind": "sign_off", "actor": _who(s["sub"], me),
                "rev": s["rev"], "version": None, "at": _at(s.get("at"))}
               for s in (sign_offs if isinstance(sign_offs, list) else [])
               if isinstance(s, dict) and isinstance(s.get("rev"), int) and isinstance(s.get("sub"), str)]
    return {"events": sorted(events, key=lambda e: (e["at"], e["id"]), reverse=True)}


def comparison(doc_id: str, rev: int, *, client=None, store=None) -> dict:
    """Fetch immutable Library revisions under this account's current access checks."""
    client, store = ad._session(client, store)
    ad.require_entitlement(store)
    view = ad.api("GET", sd._path(doc_id), client=client, store=store)
    retained = [r["rev"] for r in view.get("revisions") or [] if isinstance(r, dict)
                and isinstance(r.get("rev"), int) and r["rev"] < rev]

    def revision(n: int) -> dict:
        content = ad.api("GET", sd._path(doc_id, f"/revisions/{n}"), client=client, store=store)
        return {"rev": n, **sd._readable(content)}

    return plan_docs.compare_revisions(revision(rev), revision(max(retained)) if retained else None)


def comment(doc_id: str, rev: Any, anchor: Any, text: Any, *, question: bool = False, client=None, store=None) -> dict:
    """A comment on the revision on screen, or (``question``) one for this person's own supervisor."""
    if not isinstance(anchor, dict):
        raise ServiceError("invalid", "a comment is anchored on part of the plan", 400)
    body = {"rev": _rev(rev), "anchor": anchor, "text": _text(text), **({"question": True} if question else {})}
    return _ask(doc_id, "POST", "/comments", body, client, store)


def reply(doc_id: str, rev: Any, parent: Any, text: Any, *, client=None, store=None) -> dict:
    if not isinstance(parent, int):
        raise ServiceError("invalid", "a reply names its thread", 400)
    return _ask(doc_id, "POST", "/comments", {"rev": _rev(rev), "parent": parent, "text": _text(text)}, client, store)


def suggest(doc_id: str, rev: Any, section: Any, start: Any, before: Any, after: Any, text: Any = "", *,
            client=None, store=None) -> dict:
    """``before`` at ``start`` of ``section`` becomes ``after``; an empty ``before`` inserts."""
    if not isinstance(section, str) or not isinstance(start, int) or not isinstance(before, str) or not isinstance(after, str):
        raise ServiceError("invalid", "a suggestion names a section, a place and the new text", 400)
    body = {"rev": _rev(rev), "section": section, "start": start, "before": before, "after": after,
            "text": text if isinstance(text, str) else ""}
    return _ask(doc_id, "POST", "/suggestions", body, client, store)


def act(doc_id: str, thread: int, rev: Any, action: Any, *, client=None, store=None) -> dict:
    """Resolve or reopen a comment; accepting a suggestion changes the text and isn't offered here."""
    if action not in THREAD_ACTIONS:
        raise ServiceError("invalid", "this reader can only resolve or reopen a thread", 400)
    return _ask(doc_id, "POST", f"/threads/{thread}", {"rev": _rev(rev), "action": action}, client, store)


def vote(doc_id: str, rev: Any, decision: Any, option: Any, *, client=None, store=None) -> dict:
    return _ask(doc_id, "POST", "/votes", {"rev": _rev(rev), "decision": decision, "option": option}, client, store)


def add_answer(doc_id: str, rev: Any, decision: Any, text: Any, *, client=None, store=None) -> dict:
    """This account's own answer on a decision, and its pick of it (brindle.plan_answers)."""
    from brindle import plan_answers

    rev, text = _rev(rev), plan_answers._text(text)
    answer_id = plan_answers.new_id()
    client, store = ad._session(client, store)
    ad.api("POST", sd._path(doc_id, "/answers"), {"rev": rev, "decision": decision, "id": answer_id, "text": text},
           client=client, store=store)
    return vote(doc_id, rev, decision, answer_id, client=client, store=store)


def _answer_id(answer_id: Any) -> str:
    from brindle import plan_answers

    if not isinstance(answer_id, str) or not plan_answers.ID.fullmatch(answer_id):
        raise ServiceError("invalid", "name one of the answers on this plan", 400)
    return answer_id


def edit_answer(doc_id: str, rev: Any, answer_id: Any, text: Any, *, client=None, store=None) -> dict:
    """Reword this account's own answer; the Library refuses once someone else picked it (409 picked)."""
    from brindle import plan_answers

    return _ask(doc_id, "PUT", f"/answers/{_answer_id(answer_id)}", {"rev": _rev(rev), "text": plan_answers._text(text)},
                client, store)


def remove_answer(doc_id: str, rev: Any, answer_id: Any, *, client=None, store=None) -> dict:
    return _ask(doc_id, "DELETE", f"/answers/{_answer_id(answer_id)}?{urlencode({'rev': _rev(rev)})}", None,
                client, store)


def sign_off(doc_id: str, rev: Any, *, client=None, store=None) -> dict:
    return _ask(doc_id, "POST", "/sign-offs", {"rev": _rev(rev)}, client, store)


def asked_thread(design: dict, text: str) -> int | None:
    """The thread a question just saved made, for :mod:`brindle.pro.doc_questions`:
    the newest question of this account's with those words."""
    return max((t["id"] for t in design["threads"]
                if t["question"] and t["author"] == plan_docs.PERSON and t["text"] == mask(text)), default=None)


# -- a design opened from its share link (the app's brindle://plan/… deep link) -------------------------

# A share link's token: dsl_ and 43 URL-safe characters (docs/share-links-api.md in pawdelta-web).
LINK_TOKEN = re.compile(r"dsl_[A-Za-z0-9_-]{43}")
# A Library document id, as a link names it for a reader the design is also shared with.
DOC_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
LINK_REFUSALS = ("sign_in_required", "org_only", "team_only", "share_links_off", "sso_required")
NO_LINK = "This link doesn't open a plan for this account. It may have been turned off or replaced."


def link_id(token: str) -> str:
    """What the app keys a link's design by: never the token itself, which is the link's whole secret."""
    return "link_" + hashlib.sha256(token.encode()).hexdigest()[:16]


def _signed_in(store) -> str:
    """This account's id, before anything is sent: a link opens for whoever is signed in, on any plan."""
    from brindle import airgap, keystore
    from brindle.pro import license

    if airgap.enabled():
        raise ServiceError("air_gapped", "air-gap mode is on: brindle sends nothing, so it can't open this link", 403)
    try:
        return license.current(refresh=False, store=store).sub
    except license.LicenseError as e:
        if isinstance(e.__cause__, keystore.KeychainReadBlocked):
            raise e.__cause__
        raise ServiceError("signed_out", "sign in to brindle to open this link", 402) from e


def _link_refused(status: int, resp: dict) -> ServiceError | ad.Refused:
    refused = ad.Refused.from_response(status, resp)
    if 400 <= status < 500 and refused.code in LINK_REFUSALS:
        return ad.Refused(str(refused), 403, refused.code)
    if refused.code == "key_unavailable":
        return refused
    if status in (403, 404):
        return ServiceError("not_found", NO_LINK, 404)
    if status == 429:
        return ad.Refused("HTTP 429", status, "rate_limited")
    return refused


def read_link(token: Any, *, client=None, store=None) -> dict:
    """The design a share link opens, read through the link as the account
    signed in now: the Library runs the link's own checks (revoked, the org's
    policy, an org or team scope), the same as for the link's web page, so
    this opens nothing the browser wouldn't. Read only: no comments, votes or
    sign-offs from here, and nothing is kept. When the Library names the
    design (it does only for someone the design is shared with), it opens in
    the shared reader with what this account may do there (:func:`_as_member`)."""
    from brindle.pro import auth

    if not isinstance(token, str) or not LINK_TOKEN.fullmatch(token):
        raise ServiceError("invalid_link", "that isn't a brindle plan link", 400)
    client, store = ad._session(client, store)
    me = _signed_in(store)
    status, resp = auth.authed(client, store, "GET", f"/share-links/{token}/design")
    resp = resp if isinstance(resp, dict) else {}
    if status != 200:
        raise _link_refused(status, resp)
    doc_id = resp.get("doc_id")
    if doc_id is not None:
        if not isinstance(doc_id, str) or not DOC_ID.fullmatch(doc_id):
            raise ServiceError("upstream", "the Library named a plan brindle can't read", 502)
        member = _as_member(doc_id, resp.get("title"), client, store)
        if member is not None:
            return member
    view = {**resp, "can_edit": False, "can_comment": False, "can_suggest": False}
    return _dto(link_id(token), view, me, review=False)


def _as_member(doc_id: str, title: Any, client, store) -> dict | None:
    """The design a link named for someone it is also shared with (through a
    scope or a reviewer grant): the Library names it only for them. It opens
    in the shared reader, as the Library's own design view says this account
    may use it, never more. A refusal there is a not-found, as on the web.
    None when this account has no Library here (no Team plan): the link's own
    read-only view, which the link already allows."""
    try:
        me = ad.require_entitlement(store).sub
    except ad.NotEntitled:
        return None
    try:
        view = ad.api("GET", sd._path(doc_id), client=client, store=store)
    except ad.Refused as e:
        if e.status in (403, 404):
            raise ServiceError("not_found", NO_LINK, 404) from e
        raise
    named = {**view, "title": title} if isinstance(title, str) and title.strip() else view
    names = sd.member_names(doc_id, client=client, store=store) if _can(view, me, "can_edit") else None
    return _dto(doc_id, _titled(doc_id, named, client, store), me, names=names)
