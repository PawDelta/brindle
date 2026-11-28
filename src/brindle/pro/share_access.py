"""Who can open a shared plan and what each of them may do: the share panel's
reads and writes (brindle Pro, Team plan and up). The contract is
docs/shared-designs-api.md in pawdelta-web.

Every write changes one person or one scope with the Library's own atomic
operation, never by reading and resending the permission map, so two editors
sharing at once can't undo each other:

- ``POST /analysis-docs/{id}/share`` with a user scope invites one person with
  their access (view, comment or edit) and sign-off (none or required);
- ``PUT /design/permissions/{sub}`` changes that person's access or sign-off;
  the one not sent is kept as it is, in the same transaction;
- ``DELETE /analysis-docs/{id}/share/user/{sub}`` stops sharing with them, and
  ``DELETE /analysis-docs/{id}/share/{group|org}/{id}`` with a team or the org.

The Library gives anyone who signs off at least comment and suggest.

A plan on this machine is uploaded by its first share (shared_docs.ensure_doc);
a design someone else shared is changed by an editor of it, by its doc id.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any
from urllib.parse import quote

from brindle import plan_docs
from brindle.db import DB
from brindle.pro import analysis_docs as ad
from brindle.pro import review_policies
from brindle.pro import shared_docs as sd
from brindle.service.errors import ServiceError

ACCESS = ("view", "comment", "edit")
SCOPES = ("group", "org")

Call = Callable[..., dict]


def _access(raw: Any) -> str:
    if raw not in ACCESS:
        raise ServiceError("invalid", "access is view, comment or edit", 400)
    return raw


def _sub(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ServiceError("invalid", "name the person by their account id", 400)
    return raw


def _level(grant: Any) -> str:
    grant = grant if isinstance(grant, dict) else {}
    return "edit" if grant.get("can_edit") else "comment" if grant.get("can_comment") else "view"


def summary(meta: dict, view: dict, names: dict[str, str] | None = None) -> dict:
    """``{scopes: [{kind, id}], people: [{sub, name, access, sign_off}]}``:
    the teams and org it is shared into, and each person given it or asked to
    sign off. ``sign_off`` is required, optional or null; the owner isn't listed."""
    owner = meta.get("scope") if isinstance(meta.get("scope"), dict) else {}
    left_out = {owner.get("id") if owner.get("kind") == "user" else None, view.get("author")}
    scopes = [s for s in meta.get("scopes") or [] if isinstance(s, dict) and isinstance(s.get("id"), str)]
    grants = view.get("permissions") if isinstance(view.get("permissions"), dict) else {}
    reviewers = {r["sub"]: r for r in view.get("reviewers") or []
                 if isinstance(r, dict) and isinstance(r.get("sub"), str)}
    subs = [s["id"] for s in scopes if s.get("kind") == "user"] + list(grants) + list(reviewers)
    people = []
    for sub in dict.fromkeys(subs):
        if sub in left_out:
            continue
        signer = reviewers.get(sub)
        people.append({"sub": sub, "name": (names or {}).get(sub), "access": _level(grants.get(sub)),
                       "sign_off": None if signer is None else "required" if signer.get("required", True) else "optional"})
    return {"scopes": [{"kind": s["kind"], "id": s["id"]} for s in scopes if s.get("kind") in SCOPES],
            "people": people}


def read(call: Call, names: dict[str, str] | None = None) -> dict:
    return summary(call("GET", ""), call("GET", "/design"), names)


def _person(sub: str) -> str:
    return f"/design/permissions/{quote(sub, safe='')}"


def _must(raw: Any) -> str:
    if not isinstance(raw, bool):
        raise ServiceError("invalid", "sign_off is true or false", 400)
    return "required" if raw else "none"


def _design(call: Call, answer: dict) -> dict:
    """The design view a share or unshare answered with, read again when the
    Library's answer leaves it out (one older than per-person sharing): the
    review state kept here must show the sign-off that write just asked for."""
    design = answer.get("design")
    return design if isinstance(design, dict) else call("GET", "/design")


def invite(call: Call, sub: Any, access: Any, sign_off: Any) -> dict:
    """Give one person the plan at ``access``, with or without sign-off, in
    one Library transaction: read access never shows before the sign-off it
    comes with. The design view after it."""
    sub, access = _sub(sub), _access(access)
    return _design(call, call("POST", "/share", {"scope": {"kind": "user", "id": sub}, "access": access,
                                                 "sign_off": _must(sign_off)}))


def change(call: Call, sub: Any, access: Any) -> dict:
    """One person's access. Sign-off isn't sent, so the Library keeps it as
    it is, in the same transaction: a refusal leaves both as they were."""
    return call("PUT", _person(_sub(sub)), {"access": _access(access)})


def sign_off(call: Call, sub: Any, must: Any) -> dict:
    """Whether the build waits for this person, leaving their access alone."""
    return call("PUT", _person(_sub(sub)), {"sign_off": _must(must)})


def _unshare(call: Call, sub: Any) -> dict:
    return call("DELETE", f"/share/user/{quote(_sub(sub), safe='')}")


def revoke(call: Call, sub: Any) -> dict:
    """Stop sharing with one person: their share, grants and sign-off go together."""
    return _design(call, _unshare(call, sub))


def revoke_scope(call: Call, kind: Any, scope_id: Any) -> dict:
    if kind not in SCOPES or not isinstance(scope_id, str) or not scope_id:
        raise ServiceError("invalid", "a scope is a team (group) or the org, by id", 400)
    call("DELETE", f"/share/{kind}/{quote(scope_id, safe='')}")
    return call("GET", "/design")


def stop(call: Call) -> dict:
    """Stop sharing with everyone: its share link first, then each person and
    each scope, one at a time. Any refusal stops it there and is raised, so
    nobody is told it stopped while someone still has it. The owner keeps it,
    and an org rule that asks for review still asks."""
    try:
        call("DELETE", "/design/share-link")
    except ad.Refused as e:
        if e.status != 404:          # no link to turn off
            raise
    now = read(call)
    for person in now["people"]:
        _unshare(call, person["sub"])        # no read between: the one at the end says where it all stands
    for scope in now["scopes"]:
        call("DELETE", f"/share/{scope['kind']}/{quote(scope['id'], safe='')}")
    return call("GET", "/design")


# -- a plan on this machine --------------------------------------------------------------


DOCUMENT_READS = ("", "/design")       # the reads of the plan's document itself, not of something under it


def _plan_call(db: DB, p: dict, doc_id: str, client, store) -> Call:
    def call(method: str, tail: str, body: dict | None = None) -> dict:
        # Only a refusal to read the document is about access to it: a 404 on
        # a write under it (a person already removed) is about that one thing.
        document = method == "GET" and tail in DOCUMENT_READS
        return review_policies.ask(db, p, method, ad._doc_path(doc_id, tail), body, document=document,
                                   design=document and tail == "/design", client=client, store=store)
    return call


def plan_access(db: DB, plan_id: str, *, client=None, store=None) -> dict:
    """Who has the plan now, read from the Library; nobody before its first share."""
    p = plan_docs._plan(db, plan_id)
    share_ = sd.link(db, p["id"])
    if share_ is None or share_["doc_id"] is None:
        return {"shared": False, "scopes": [], "people": []}
    client, store = ad._session(client, store)
    share_ = sd._shared(db, p["id"], store)
    meta, view, names = plan_reads(db, p, share_["doc_id"], client, store)
    assert meta is not None
    return {"shared": True, **summary(meta, view, names)}


def plan_reads(db: DB, p: dict, doc_id: str, client, store, *, metadata_optional: bool = False
               ) -> tuple[dict | None, dict, dict[str, str] | None]:
    """Read the panel's three inputs together, then record review state on the DB's thread."""
    from brindle.pro import auth, credentials

    with ThreadPoolExecutor(max_workers=3) as pool:
        meta = pool.submit(copy_context().run, ad._call, "GET", ad._doc_path(doc_id),
                           client=client, store=store)
        design = pool.submit(copy_context().run, ad._call, "GET", sd._path(doc_id),
                             client=client, store=store)
        directory = pool.submit(copy_context().run, sd.member_names, doc_id, client=client, store=store)
        if metadata_optional:
            # Sync depends on the design. Metadata is only needed for the access summary.
            view = review_policies.checked(db, p, design.result(), document=True, design=True)
            try:
                result = meta.result()
            except (RuntimeError, auth.AuthError, credentials.CredentialError):
                result = None
            checked = (review_policies.checked(db, p, result, document=True)
                       if result is not None and result[0] == 200 else None)
            return checked, view, directory.result()
        result = review_policies.checked(db, p, meta.result(), document=True)
        view = review_policies.checked(db, p, design.result(), document=True, design=True)
        return result, view, directory.result()


def plan_write(db: DB, plan_id: str, work: Callable[[Call], dict | None], *, first: bool = False,
               revoking: bool = False, notify: bool = True, client=None, store=None) -> dict | None:
    """Run one write on the plan's Library doc and keep the review state it
    answered with. ``first``: the write may be its first share, which
    uploads the plan. ``revoking`` allows access removal after review closes.
    Queued invites publish once their queue entry is settled."""
    p = plan_docs._plan(db, plan_id)
    if not revoking:
        sd._review_open(p)
    client, store = ad._session(client, store)
    doc_id = (sd.ensure_doc(db, p, None, None, client=client, store=store) if first
              else sd._shared(db, p["id"], store)["doc_id"])
    design = work(_plan_call(db, p, doc_id, client, store))
    changed = isinstance(design, dict) and sd._remember(db, p["id"], design)
    if notify and (changed or first):
        plan_docs._changed(db, p, p["rev"])
    return sd.link(db, p["id"])


# -- a design someone else shared, changed by one of its editors ---------------------------


def _design_call(doc_id: str, client, store) -> Call:
    def call(method: str, tail: str, body: dict | None = None) -> dict:
        return ad.api(method, ad._doc_path(doc_id, tail), body, client=client, store=store)
    return call


def design_access(doc_id: str, *, client=None, store=None) -> dict:
    client, store = ad._session(client, store)
    ad.require_entitlement(store)
    names = sd.member_names(doc_id, client=client, store=store)
    return {"shared": True, **read(_design_call(doc_id, client, store), names)}


def design_write(doc_id: str, work: Callable[[Call], Any], *, client=None, store=None) -> None:
    client, store = ad._session(client, store)
    ad.require_entitlement(store)
    work(_design_call(doc_id, client, store))
