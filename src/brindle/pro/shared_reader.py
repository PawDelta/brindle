"""A teammate's reader for a design someone else shared (brindle Pro, Team plan
and up): the other half of :mod:`brindle.pro.shared_docs`. The contract is
docs/shared-designs-api.md in pawdelta-web.

Everything here is keyed by the Library's document id. A teammate has no plan
on this machine, so nothing is written to it: no plan row, no copy on disk. A
read asks the Library each time and the app holds the answer in memory, so
losing access means losing the doc. Writes go to the Library as the signed-in
account, which decides what that account may do; its refusal (a 403, or a 409
stale for a revision that moved) is passed on for the app to act on.

A teammate reads, comments, suggests, votes and signs off. They never approve
or build, and the reader has no edit mode: nothing here calls ``/edits`` or
``/revisions``, and it only resolves or reopens a thread, because accepting a
suggestion makes a new revision.
"""

from __future__ import annotations

from typing import Any

from brindle import plan_docs, plans
from brindle.pro import analysis_docs as ad
from brindle.pro import shared_docs as sd
from brindle.redact import mask
from brindle.service.dto import SharedDesignDTO
from brindle.service.errors import ServiceError

THREAD_ACTIONS = ("resolve", "reopen")
ANCHOR_FIELDS = ("kind", "section", "start", "end", "quote", "decision", "option", "state")
ANCHOR_KINDS = ("text", "diagram", "decision", "section")
REVISION_SOURCES = ("proposal", "edit", "suggestion")
UNTITLED = "Design doc"


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
        state, reason = "waiting", "brindle can't read this design's review state."
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
    """False only when this account can't be a signer: a covering rule may count
    people the view doesn't name, and for those the Library's answer decides."""
    people = view.get("reviewers") or []
    return me == author or bool(view.get("policy_reviewers")) or any(isinstance(r, dict) and r.get("sub") == me for r in people)


def _dto(doc_id: str, view: dict, me: str) -> dict:
    if not isinstance(view.get("rev"), int):
        raise RuntimeError("the Library's answer has no revision")
    content = sd._readable(view)                 # checked and masked as a supervisor's proposal is: it is drawn here
    votes = {d.get("id"): d.get("votes") or {} for d in view.get("decisions") or [] if isinstance(d, dict)}
    decisions = []
    for d in content["decisions"]:
        cast = {option: [str(s) for s in subs] for option, subs in votes.get(d["id"], {}).items() if isinstance(subs, list)}
        decisions.append({**d, "votes": {option: [_who(s, me) for s in subs] for option, subs in cast.items()},
                          "confirmed": next((option for option, subs in cast.items() if me in subs), None)})
    history = [r for r in view.get("revisions") or [] if isinstance(r, dict) and isinstance(r.get("rev"), int)
               and r.get("source") in REVISION_SOURCES]
    author = history[0].get("author") if history else view.get("author")
    return SharedDesignDTO(
        id=doc_id, session="", title=view.get("title") or UNTITLED, rev=view["rev"], state="open", ask=content["ask"],
        markdown=content["markdown"], lede=content["lede"], sections=content["sections"], decisions=decisions,
        split=content["split"], queued=0, created_at=_at(history[0].get("at")) if history else 0.0,
        revisions=[{"rev": r["rev"], "author": _who(r.get("author"), me), "source": r["source"],
                    "changed": r.get("changed") or [], "at": _at(r.get("at"))} for r in history],
        threads=[t for t in (_thread(x, me) for x in view.get("threads") or []) if t], gate=_gate(view, me), me=me,
        author=_who(author, me) if isinstance(author, str) else None,
        can_edit=_can(view, me, "can_edit"),     # may write in its live draft (the reader's Editing)
        can_comment=_can(view, me, "can_comment"), can_suggest=_can(view, me, "can_suggest"),
        can_sign_off=_can_sign(view, me, author),
        signed_off=any(isinstance(s, dict) and s.get("sub") == me and s.get("rev") == view["rev"]
                       for s in view.get("sign_offs") or [])).to_json()


def _ask(doc_id: str, method: str, tail: str, body: dict | None, client, store) -> dict:
    client, store = ad._session(client, store)
    me = ad.require_entitlement(store).sub
    return _dto(doc_id, ad.api(method, sd._path(doc_id, tail), body, client=client, store=store), me)


def _rev(rev: Any) -> int:
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise ServiceError("invalid", "name the revision on screen", 400)
    return rev


def _text(text: Any) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ServiceError("invalid", "write something first", 400)
    return text


def read(doc_id: str, *, client=None, store=None) -> dict:
    """The design as this account may read it now."""
    return _ask(doc_id, "GET", "", None, client, store)


def timeline(doc_id: str, *, client=None, store=None) -> dict:
    """Read the Library's retained revision metadata and sign-offs without keeping a copy."""
    client, store = ad._session(client, store)
    me = ad.require_entitlement(store).sub
    view = ad.api("GET", sd._path(doc_id), client=client, store=store)
    events = [{"id": f"revision:{r['rev']}", "kind": "revision", "actor": _who(r.get("author"), me),
               "rev": r["rev"], "version": None, "at": _at(r.get("at")),
               "source": r.get("source"), "changed": r.get("changed") or []}
              for r in view.get("revisions") or [] if isinstance(r, dict) and isinstance(r.get("rev"), int)]
    events += [{"id": f"sign_off:{s['rev']}:{s['sub']}", "kind": "sign_off", "actor": _who(s["sub"], me),
                "rev": s["rev"], "version": None, "at": _at(s.get("at"))}
               for s in view.get("sign_offs") or []]
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
        raise ServiceError("invalid", "a comment is anchored on part of the doc", 400)
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


def sign_off(doc_id: str, rev: Any, *, client=None, store=None) -> dict:
    return _ask(doc_id, "POST", "/sign-offs", {"rev": _rev(rev)}, client, store)


def asked_thread(design: dict, text: str) -> int | None:
    """The thread a question just saved made, for :mod:`brindle.pro.doc_questions`:
    the newest question of this account's with those words."""
    return max((t["id"] for t in design["threads"]
                if t["question"] and t["author"] == plan_docs.PERSON and t["text"] == mask(text)), default=None)
