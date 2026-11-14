"""The daemon's visuals routes (brindle.visuals): a session's visuals, their
pages, the person's answer, Keep, and the kept store. Every route is behind
the daemon Guard (the token only the shell holds) and refuses while the
visuals capability is off. A page is served with the visuals CSP; the shell's
``brindle-visual://`` scheme is its only intended reader."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import wraps
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from brindle import visuals, visual_offers
from brindle.daemon.routes_actions import _endpoint, _get
from brindle.daemon.routes_read import _json
from brindle.db import DB
from brindle.service.errors import ServiceError, from_exception

log = logging.getLogger(__name__)


class _LiveRoom:
    """Local subscribers and transient presence; durable text lives in DraftReplica."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0
        self.clients: set[WebSocket] = set()
        self.presence: dict[str, Any] = {}


class _WindowSink:
    """An app window's socket as the relay link (pro.live_docs) writes to it."""

    def __init__(self, socket: WebSocket) -> None:
        self.socket = socket

    async def send(self, frame: dict) -> None:
        await self.socket.send_json(frame)

    async def close(self, code: int) -> None:
        await self.socket.close(code=code)


# A Library doc id, as a teammate's reader (SharedDesignReader) opens a design shared with them by it.
_LIBRARY_DOC = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _teammate_doc(visual_id: str) -> bool:
    """Whether the live socket was opened on a design someone else shared,
    by its Library doc id: there is no plan here, and the relay alone holds
    its text (pro.live_docs fills the replica at its first join)."""
    return not visuals.ID.fullmatch(visual_id) and bool(_LIBRARY_DOC.fullmatch(visual_id))


def _live_doc(db: DB, visual_id: str) -> tuple[str, dict]:
    from brindle import plan_docs
    from brindle.pro import shared_docs

    p = plan_docs._plan(db, visual_id)
    share = shared_docs.link(db, visual_id)
    if share is None or not share["doc_id"] or share["access_refused"]:
        raise ServiceError("not_found", "no live text for this visual", 404)
    plan_docs._editable(p)
    return share["doc_id"], p


async def visual_live(socket: WebSocket) -> None:
    """Guard consumed the token. Merge text locally and relay ephemeral carets."""
    from brindle.crdt import CrdtError, Presence, SharedDoc
    from brindle.plan_drafts import DraftGone, DraftReplica
    from brindle.pro import live_docs

    await socket.accept()
    relay = getattr(socket.app.state, "live_docs", None)    # the daemon's relay links (pro.live_docs)
    lease = None
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="brindle-visual-live")
    db = None
    replica = None
    room = None
    rooms = getattr(socket.app.state, "visual_live_rooms", None)
    if rooms is None:
        rooms = socket.app.state.visual_live_rooms = {}
    doc_id = None
    # A design a teammate shared, opened by its Library doc id: no plan here to seed from or to check.
    teammate = relay is not None and _teammate_doc(socket.path_params["id"])

    async def work(fn, *args):
        # Each submission gets its own context, including cancellation/deadline
        # tokens belonging to the socket's request.
        return await loop.run_in_executor(pool, copy_context().run, fn, *args)

    def open_doc():
        nonlocal db
        db = DB()
        if teammate:
            return socket.path_params["id"]
        return _live_doc(db, socket.path_params["id"])[0]

    def current_plan():
        if teammate:
            return None     # the relay checks who may write; presence may come before the text does
        current_id, p = _live_doc(db, socket.path_params["id"])
        if current_id != doc_id:
            raise DraftGone("the visual's shared document changed")
        return p

    def bootstrap():
        nonlocal replica
        if teammate:
            # Only the relay's draft: until the link's first join stores it there is nothing to
            # send, and the join closes this window with RELOAD to open it again (pro.live_docs).
            if db.conn.execute("SELECT 1 FROM plan_drafts WHERE doc_id=?", (doc_id,)).fetchone() is None:
                return []
            replica = DraftReplica.open(db, doc_id)
            return replica.export_bootstrap()
        p = current_plan()
        # Only the first holder seeds; open prefers an existing stored replica.
        sections = {"lede": p["lede"], **{s["id"]: s["markdown"] for s in p["sections"]
                                         if s["kind"] == "markdown"}}
        exists = db.conn.execute("SELECT 1 FROM plan_drafts WHERE doc_id=?", (doc_id,)).fetchone()
        initial = None if exists else SharedDoc.new(sections).export_snapshot()
        replica = DraftReplica.open(db, doc_id, initial)
        if not exists:
            # Seeded here, not by the relay: its first join re-types local edits from this text.
            live_docs.note_seed(db, doc_id, sections, replica.version())
        return replica.export_bootstrap()

    def apply_update(data):
        """The update this machine's other windows need for ``data``: itself,
        or, if the replica was rebuilt under this window, its change re-typed
        onto the new one (DraftReplica.receive); None for nothing yet."""
        current_plan()
        if data is not None:
            if replica is None:     # a teammate's window before the relay's text: it has nothing to edit
                raise ValueError("no text to edit yet")
            return replica.receive(data)[1]
        return None

    def unfilled():
        """Why the author's Freeze can't go: its template's required sections
        still empty in the text typed so far (brindle.doc_templates), else None."""
        from brindle import doc_templates, plan_docs

        p = current_plan()
        try:
            doc_templates.require(db, p["id"], doc_templates.SIGN_OFF, plan_docs.current(db, p, replica)["sections"])
        except ServiceError as e:
            return e.message
        return None

    def close_db():
        if db is not None:
            db.conn.close()

    try:
        doc_id = await work(open_doc)
        if relay is not None and not await relay.prepare(doc_id):
            # Initial 4404, before any live frame, selects the reader's HTTP path.
            raise ServiceError("not_found", "no live text for this design here", 404)
        room = rooms.setdefault(doc_id, _LiveRoom())
        room.users += 1
        async with room.lock:
            if relay is not None:
                # The number this window writes under, before its snapshot: the app sets it
                # once the snapshot is in (as a string: relay numbers pass 2**53).
                lease = await relay.open_window(doc_id, _WindowSink(socket))
                if lease is not None:
                    await socket.send_json({"peer": None if lease.peer is None else str(lease.peer)})
                    await socket.send_json(relay.link(doc_id).state_frame())
            if teammate and lease is None:
                # Signed out or no Team plan here: a teammate's design has no live text without the relay.
                raise ServiceError("not_found", "no live text for this design here", 404)
            for update in await work(bootstrap):
                await socket.send_json({"update": base64.b64encode(update).decode("ascii")})
            for section, presence in room.presence.items():
                await socket.send_json({"section": section, "awareness": base64.b64encode(presence.encode()).decode("ascii")})
            room.clients.add(socket)
        while True:
            frame = await socket.receive_text()
            said = json.loads(frame)
            if not isinstance(said, dict):
                raise ValueError("a live frame must be an object")
            if lease is not None and (said.get("freeze") is True or said.get("reopen") is True):
                # The author's Freeze or Reopen: the Library makes or withdraws the version (it
                # refuses anyone else); not under the room's lock, so typing here never waits on it.
                reopen = said.get("reopen") is True
                refused = None if reopen or teammate else await work(unfilled)
                if refused is None:
                    refused = await relay.freeze(doc_id, reopen=reopen)
                if refused is not None:     # to this window only: its Freeze or Reopen didn't happen
                    await socket.send_json({"refused": {"action": "reopen" if reopen else "freeze", "reason": refused}})
                continue
            if lease is not None and said.get("refresh") is True:
                # The window's person signed off (or otherwise changed the review): every window
                # here is told the frozen version's sign-offs as the Library has them now.
                await relay.link(doc_id).refresh()
                continue
            async with room.lock:
                if isinstance(said.get("update"), str):
                    data = base64.b64decode(said["update"], validate=True)
                    forward = await work(apply_update, data)
                    if lease is not None:
                        await relay.changed(doc_id)
                    if forward is None:
                        continue
                    if forward != data:     # this window is on a replaced replica: pass on the translation
                        frame = json.dumps({"update": base64.b64encode(forward).decode("ascii")})
                elif isinstance(said.get("awareness"), str) and isinstance(said.get("section"), str):
                    await work(apply_update, None)
                    presence = room.presence.setdefault(said["section"], Presence())
                    presence.apply(base64.b64decode(said["awareness"], validate=True))
                    if lease is not None:
                        await relay.awareness(lease, said["section"], said["awareness"])
                else:
                    raise ValueError("not a live update or awareness frame")
                for other in tuple(room.clients):
                    if other is socket:
                        continue
                    try:
                        await other.send_text(frame)
                    except (WebSocketDisconnect, OSError, RuntimeError):
                        room.clients.discard(other)
    except WebSocketDisconnect:
        pass
    except ServiceError as e:
        await socket.close(code=4409 if e.http_status == 409 else 4404 if e.http_status == 404 else 4403)
    except DraftGone:
        await socket.close(code=4404)
    except (CrdtError, ValueError, binascii.Error):
        await socket.close(code=1003)
    finally:
        if lease is not None:
            try:
                await relay.close_window(lease)
            except Exception:  # noqa: BLE001 - the relay link must never keep the socket from ending
                log.debug("brindle: the live relay didn't hear a window close", exc_info=True)
        if room is not None:
            room.clients.discard(socket)
            room.users -= 1
            if not room.users and rooms.get(doc_id) is room:
                del rooms[doc_id]
        try:
            # Queue closure behind any in-flight operation, even if the socket
            # was cancelled before opening or writing finished.
            await asyncio.shield(work(close_db))
        finally:
            pool.shutdown(wait=False)


def _page(fn: Callable[[DB, Request], Path]) -> Callable[[Request], Response]:
    """A sync handler returning a page's file, sent as HTML with the visuals CSP."""
    @wraps(fn)
    def handler(request: Request) -> Response:
        try:
            visuals.require_enabled()
            db = DB()
            try:
                body = fn(db, request).read_bytes()
            finally:
                db.conn.close()
        except Exception as e:  # noqa: BLE001 - every engine error maps to a ServiceError
            err = from_exception(e)
            if err.code == "internal":
                log.exception("brindle: daemon route %s failed", request.url.path)
            return JSONResponse(err.to_json(), status_code=err.http_status)
        return Response(body, media_type="text/html; charset=utf-8", headers=visuals.PAGE_HEADERS)
    return handler


@_json
def session_visuals(db: DB, request: Request) -> Any:
    visuals.require_enabled()
    root = request.path_params["id"]
    if db.get_agent(root) is None:
        raise ServiceError("not_found", f"no session {root}", 404)
    visuals._unlocked(root)
    return [visuals.to_dto(db, v) for v in visuals.for_session(db, root)]


@_json
def session_offer(db: DB, request: Request) -> Any:
    return visual_offers.current(db, request.path_params["id"])


def resolve_offer(db: DB, params: dict, body: dict) -> Any:
    return visual_offers.resolve(db, params["id"], _get(body, "offer", required=True),
                                 _get(body, "accept", bool, required=True))


def visual_input(db: DB, params: dict, body: dict) -> Any:
    """Authenticated shell input, observed before the PTY receives its submit key."""
    root = params["id"]
    visuals._unlocked(root)
    agent = db.get_agent(root)
    if agent is None:
        raise ServiceError("not_found", "no such session", 404)
    expires_at = _get(body, "expires_at", (int, float))
    if expires_at is not None and (isinstance(expires_at, bool) or not 0 < expires_at < float("inf")):
        raise ServiceError("invalid", "expires_at must be a finite timestamp", 400)
    if _get(body, "begin", bool):
        return {"generation": visual_offers.begin_input(db, agent, expires_at)}
    submitted_at = _get(body, "submitted_at", (int, float), required=True)
    if isinstance(submitted_at, bool) or not 0 < submitted_at < float("inf"):
        raise ServiceError("invalid", "submitted_at must be a finite timestamp", 400)
    observer = _get(body, "refresh", int)
    if observer is not None:
        return {"generation": visual_offers.refresh_input(db, agent, observer, submitted_at, expires_at)}
    typed_words = _get(body, "typed_words")
    if typed_words is not None:
        if expires_at is None or len(typed_words) > 32000:
            raise ServiceError("invalid", "typed_words needs expires_at and a sensible length", 400)
        visual_offers.keep_typed(db, agent, typed_words, expires_at, submitted_at)
        return {"observed": True}
    text = _get(body, "text", required=True)
    if len(text) > 32000:
        raise ServiceError("invalid", "input is too long", 400)
    # Bare Enter/unknown drafts convey no chat answer. Short answers may be
    # either chat or menu input: fold an offer without changing publication consent.
    if not text.strip():
        return {"observed": False}
    generation = _get(body, "generation", int, required=True)
    sequence = _get(body, "sequence", int, required=True)
    if generation < 1 or sequence < 1:
        raise ServiceError("invalid", "generation and sequence must be positive integers", 400)
    fold_only = text.strip().lower() in ("y", "yes", "n", "no") or text.strip().isdigit()
    visual_offers.person_spoke(db, agent, text, expires_at=expires_at, observation=(generation, sequence),
                               submitted_at=submitted_at, fold_only=fold_only, typed=_get(body, "typed", bool) is True)
    return {"observed": True}


@_page
def visual_page(db: DB, request: Request) -> Path:
    return visuals.page(db, request.path_params["id"], request.path_params["n"])


def answer(db: DB, params: dict, body: dict) -> Any:
    visuals.require_enabled()
    rev = body.get("rev")
    return visuals.to_dto(db, visuals.answer(db, params["id"], rev, _get(body, "option"),
                                             _get(body, "note"), _get(body, "attachments", list)))


def dismiss(db: DB, params: dict, body: dict) -> Any:
    visuals.require_enabled()
    return visuals.to_dto(db, visuals.dismiss(db, params["id"], body.get("rev")))


def keep(db: DB, params: dict, body: dict) -> Any:
    visuals.require_enabled()
    return visuals.to_dto(db, visuals.keep(db, params["id"]))


def unkeep(db: DB, params: dict, body: dict) -> Any:
    visuals.require_enabled()
    return visuals.to_dto(db, visuals.unkeep(db, params["id"]))


def mark_read(db: DB, params: dict, body: dict) -> Any:
    visuals.require_enabled()
    return visuals.to_dto(db, visuals.mark_read(db, params["id"], _get(body, "rev", int)))


def snapshot(db: DB, params: dict, body: dict) -> Any:
    from brindle import visual_export

    return visual_export.snapshot(db, params["id"], body.get("rev"),
                                  _get(body, "html", default=""),
                                  _get(body, "destination", default="download"),
                                  markdown=_get(body, "markdown"))


@_json
def plan(db: DB, request: Request) -> Any:
    from brindle import plans

    visuals.require_enabled()
    p = plans.get(db, request.path_params["id"])
    visuals._unlocked(p["root_id"])
    return plans.to_dto(db, p)


def decide_plan(db: DB, params: dict, body: dict) -> Any:
    """The person's Approve or Redirect. The daemon serves the app and people,
    never agents (its Guard refuses an agent's process), so this is the person."""
    from brindle import plans

    p = plans.decide(db, params["id"], body.get("rev"), _get(body, "approved", bool, required=True),
                     body.get("picks"), body.get("comments"), _get(body, "note"), person=True,
                     attachments=_get(body, "attachments", list))
    return plans.to_dto(db, p)


# The design doc (brindle.plan_docs). Like decide, only the person reaches
# these: each answers with the plan as it is now.


def _plan_now(db: DB, plan_id: str) -> Any:
    from brindle import plans

    return plans.to_dto(db, plans.get(db, plan_id))


@_json
def plan_history(db: DB, request: Request) -> Any:
    from brindle import plan_docs

    return plan_docs.timeline(db, request.path_params["id"])


@_json
def plan_comparison(db: DB, request: Request) -> Any:
    from brindle import plan_docs

    return plan_docs.comparison(db, request.path_params["id"], request.path_params["n"])


@_json
def plan_revision(db: DB, request: Request) -> Any:
    from brindle import plan_docs

    return plan_docs.revision_dto(db, request.path_params["id"], request.path_params["n"])


def plan_comment(db: DB, params: dict, body: dict) -> Any:
    """A comment anchored on the revision on screen, or (``parent``) a reply."""
    from brindle import plan_docs

    question = _get(body, "question", bool, default=False)
    tid = plan_docs.comment(db, params["id"], body.get("rev"), _get(body, "anchor", dict), body.get("text"),
                           _get(body, "attachments", list), parent=_get(body, "parent", int), question=question)
    if question:
        from brindle.pro import doc_questions

        return JSONResponse(_plan_now(db, params["id"]), background=BackgroundTask(
            doc_questions.after_reply, plan_id=params["id"], thread_id=tid))
    return _plan_now(db, params["id"])


def doc_question(db: DB, params: dict, body: dict) -> Any:
    """The remote doc reader has saved a question; route it on this machine."""
    from brindle.pro import doc_questions

    from brindle.pro import shared_docs

    owner = shared_docs._require_team(None).sub
    if not owner:
        raise ServiceError("forbidden", "sign in to ask your supervisor", 403)
    thread = _get(body, "thread", int, required=True)
    return JSONResponse({"state": "queued"}, background=BackgroundTask(
        doc_questions.after_reply, doc_id=params["id"], thread_id=thread, owner=owner,
        repo_root=_get(body, "repo")))


def plan_resolve(db: DB, params: dict, body: dict) -> Any:
    from brindle import plan_docs

    plan_docs.resolve(db, params["id"], params["tid"], _get(body, "resolved", bool, default=True))
    return _plan_now(db, params["id"])


def plan_suggest(db: DB, params: dict, body: dict) -> Any:
    from brindle import plan_docs

    plan_docs.suggest(db, params["id"], body.get("rev"), body.get("section"), body.get("start"),
                      body.get("before"), body.get("after"), body.get("text"), _get(body, "attachments", list))
    return _plan_now(db, params["id"])


def plan_accept(db: DB, params: dict, body: dict) -> Any:
    from brindle import plan_docs

    plan_docs.accept(db, params["id"], params["tid"], body.get("rev"))
    return _plan_now(db, params["id"])


def plan_reject(db: DB, params: dict, body: dict) -> Any:
    from brindle import plan_docs

    plan_docs.reject(db, params["id"], params["tid"])
    return _plan_now(db, params["id"])


def plan_edit(db: DB, params: dict, body: dict) -> Any:
    from brindle import plan_docs

    plan_docs.edit(db, params["id"], body.get("rev"), body.get("section"), body.get("start"),
                   body.get("before"), body.get("after"))
    return _plan_now(db, params["id"])


def plan_confirm(db: DB, params: dict, body: dict) -> Any:
    from brindle import plan_docs

    plan_docs.confirm(db, params["id"], body.get("rev"), body.get("decision"), body.get("option"))
    return _plan_now(db, params["id"])


# Team review through the Library (brindle.pro.shared_docs). Each answers with
# the plan as it is now, its build gate included.


def _library(db: DB, plan_id: str, work: Callable[[Any], Any]) -> Any:
    from brindle import redact
    from brindle.daemon import routes_actions as actions
    from brindle.pro import auth, shared_docs

    try:
        work(shared_docs)
    except (RuntimeError, auth.TransportError) as e:    # unreachable or refused: not signed out
        err = actions.upstream_error(e)
        raise ServiceError(err.code, redact.mask(str(e)), err.http_status) from e
    return _plan_now(db, plan_id)


def plan_share(db: DB, params: dict, body: dict) -> Any:
    return _library(db, params["id"], lambda sd: sd.share(db, params["id"], body.get("scope"),
                                                          reviewers=body.get("reviewers")))


def plan_reviewers(db: DB, params: dict, body: dict) -> Any:
    return _library(db, params["id"], lambda sd: sd.set_reviewers(db, params["id"], body.get("reviewers")))


def plan_sync(db: DB, params: dict, body: dict) -> Any:
    return _library(db, params["id"], lambda sd: sd.sync(db, params["id"]))


def plan_stop_sharing(db: DB, params: dict, body: dict) -> Any:
    from brindle.pro import shared_docs

    shared_docs.stop_sharing(db, params["id"], body.get("rev"))
    return _plan_now(db, params["id"])


def plan_sign_off(db: DB, params: dict, body: dict) -> Any:
    return _library(db, params["id"], lambda sd: sd.sign_off(db, params["id"], body.get("rev")))


def plan_policy_override(db: DB, params: dict, body: dict) -> Any:
    """An org admin's waiver of a review rule, or a stand-in for it. Like
    decide, only the person reaches this; the Library checks they are an admin."""
    from brindle.pro import review_policies

    return _library(db, params["id"], lambda sd: review_policies.override(
        db, params["id"], body.get("rev"), body.get("org_id"), body.get("policy"), body.get("action"),
        body.get("reason"), stand_in=body.get("stand_in"), until=body.get("until")))


def plan_policy_ask(db: DB, params: dict, body: dict) -> Any:
    """An org admin asks a waiting rule's people again. Only the person reaches this."""
    from brindle.pro import review_policies

    return _library(db, params["id"], lambda sd: review_policies.ask_again(
        db, params["id"], body.get("rev"), body.get("org_id"), body.get("policy")))


def plan_policy_preview(db: DB, params: dict, body: dict) -> Any:
    """The org rules that would cover this plan once it is shared, from the
    files its revision on screen says it will change, for the share panel."""
    from brindle import plan_docs
    from brindle.daemon import routes_actions as actions
    from brindle.pro import review_policies

    paths =review_policies.design_paths(plan_docs.revision(db, params["id"]))
    got = actions.docs_org_api("POST", "review-policies/preview", {"paths": paths, "labels": []}).get("policy_reviewers")
    return {"policies": [r for r in map(review_policies.preview_row, got if isinstance(got, list) else []) if r]}


@_json
def review_policies_read(db: DB, request: Request) -> Any:
    """An org admin's settings page: the rules, the teams and roles they can
    name, and the designs each covers now. The Library checks the permission."""
    from brindle.daemon import routes_actions as actions

    repo = request.query_params.get("repo") or None
    rules = actions.docs_org_api("GET", "review-policies", repo=repo)
    org = rules.get("org_id")      # the rest is of the org the rules were, even if the active one changes meanwhile
    read = lambda tail: actions.docs_org_api("GET", tail, repo=repo, org_id=org)  # noqa: E731
    return {**rules, "directory": read("review-policies/directory"), "in_review": read("review-policies/in-review")}


def review_policies_save(db: DB, params: dict, body: dict) -> Any:
    """Replace the org's rules. ``org_id`` is the org the form was loaded for;
    who may save is the Library's to say, so a custom role with the control
    permission is accepted."""
    from brindle.daemon import routes_actions as actions
    from brindle.pro import review_policies

    return actions.docs_org_api("PUT", "review-policies", review_policies.save_body(body), repo=body.get("repo"),
                                org_id=body["org_id"])


def review_policies_impact(db: DB, params: dict, body: dict) -> Any:
    """What saving a draft list would do to the designs in review. Writes nothing."""
    from brindle.daemon import routes_actions as actions
    from brindle.pro import review_policies

    return actions.docs_org_api("POST", "review-policies/draft-impact", review_policies.save_body(body, draft=True),
                                repo=body.get("repo"), org_id=body["org_id"])


# A design shared by someone else (brindle.pro.shared_reader), by its Library
# doc id. Only the person reaches these; each answers with the design as the
# Library has it now, and nothing is kept here.


def _shared_design(repo: str | None, work: Callable[[Any], Any]) -> Any:
    from brindle import redact
    from brindle.daemon import routes_actions as actions
    from brindle.pro import analysis_docs, auth, shared_reader

    actions._docs_entitlement(repo, analysis_docs._session()[1])   # signed out or not entitled: a 402, and the app drops the doc
    try:
        return work(shared_reader)
    except (RuntimeError, auth.TransportError) as e:    # unreachable or refused
        if isinstance(e, analysis_docs.Refused) and e.status == 401:      # still refused after a token refresh; a 401 would read as the daemon's
            raise ServiceError("signed_out", "sign in to brindle again to read this design", 402) from e
        if isinstance(e, analysis_docs.Refused) and e.code == "key_unavailable":
            raise ServiceError(e.code, redact.mask(str(e)), 503) from e   # the org's key is revoked: the doc is gone for now
        err = actions.upstream_error(e)
        raise ServiceError(err.code, redact.mask(str(e)), err.http_status) from e


@_json
def shared_design(db: DB, request: Request) -> Any:
    doc = request.path_params["doc_id"]
    return _shared_design(request.query_params.get("repo") or None, lambda r: r.read(doc))


def shared_design_comment(db: DB, params: dict, body: dict) -> Any:
    """A comment, a reply (``parent``), or a question for the person's own
    supervisor, which starts it only once this answer has been written."""
    doc, repo, parent = params["doc_id"], _get(body, "repo"), _get(body, "parent", int)
    if parent is not None:
        return _shared_design(repo, lambda r: r.reply(doc, body.get("rev"), parent, body.get("text")))
    question = _get(body, "question", bool, default=False)
    design = _shared_design(repo, lambda r: r.comment(doc, body.get("rev"), _get(body, "anchor", dict), body.get("text"),
                                                      question=question))
    if not question:
        return design
    from brindle.pro import doc_questions, shared_reader

    thread = shared_reader.asked_thread(design, body.get("text"))
    if thread is None:
        log.warning("brindle: a question saved on design %s wasn't found in the Library's answer", doc)
        return design
    return JSONResponse(design, background=BackgroundTask(
        doc_questions.after_reply, doc_id=doc, thread_id=thread, owner=design["me"], repo_root=repo))


def shared_design_suggest(db: DB, params: dict, body: dict) -> Any:
    return _shared_design(_get(body, "repo"), lambda r: r.suggest(
        params["doc_id"], body.get("rev"), body.get("section"), body.get("start"), body.get("before"), body.get("after"),
        body.get("text", "")))


def shared_design_thread(db: DB, params: dict, body: dict) -> Any:
    return _shared_design(_get(body, "repo"), lambda r: r.act(params["doc_id"], params["tid"], body.get("rev"), body.get("action")))


def shared_design_vote(db: DB, params: dict, body: dict) -> Any:
    return _shared_design(_get(body, "repo"), lambda r: r.vote(params["doc_id"], body.get("rev"), body.get("decision"),
                                                               body.get("option")))


def shared_design_sign_off(db: DB, params: dict, body: dict) -> Any:
    return _shared_design(_get(body, "repo"), lambda r: r.sign_off(params["doc_id"], body.get("rev")))


@_json
def library_design_plan(db: DB, request: Request) -> Any:
    """The plan a Library design was shared from, when it was shared from this
    machine by the account signed in now: its visual, so the Library opens it
    in the author's own reader (sharing, Freeze, the build). A 404 for anyone
    else's design, for one another account here shared, for one the Library
    refused this account, and when no account can be verified: those open in
    the teammate's reader, with what the Library lets this account do."""
    from brindle.daemon import routes_actions as actions

    visuals.require_enabled()
    row = db.conn.execute("SELECT plan_id, owner, access_refused FROM plan_shares WHERE doc_id=?",
                          (request.path_params["id"],)).fetchone()
    try:
        me = actions._docs_entitlement(None).sub
    except ServiceError:
        me = None
    if row is None or row["access_refused"] or me is None or row["owner"] != me:
        raise ServiceError("not_found", "this design wasn't shared from here by this account", 404)
    v = visuals.get(db, row["plan_id"])
    visuals._unlocked(v["root_id"])
    return visuals.to_dto(db, v)


@_json
def library_people(db: DB, request: Request) -> Any:
    """The people of the person's org, to pick reviewers from, leaving out
    the person (``me``). ``admin_org``: the org whose review rules the doc
    offers them an override of. Only what to show: the Library decides who may."""
    from urllib.parse import quote

    from brindle.daemon import routes_actions as actions

    ent = actions._docs_entitlement(request.query_params.get("repo") or None)
    if not ent.org_id:
        return {"people": []}
    members = actions.docs_api("GET", f"/orgs/{quote(ent.org_id, safe='')}/members").get("members")
    return {"people": [{"sub": m["sub"], "email": m.get("email") or ""} for m in members or []
                       if isinstance(m, dict) and isinstance(m.get("sub"), str) and m["sub"] != ent.sub],
            "me": ent.sub, "admin_org": ent.org_id if ent.role in ("owner", "admin") else None}


@_json
def kept_list(db: DB, request: Request) -> Any:
    visuals.require_enabled()
    return [visuals.kept_to_dto(k) for k in visuals.kept(db, request.query_params.get("repo") or None)]


@_json
def kept_one(db: DB, request: Request) -> Any:
    visuals.require_enabled()
    return visuals.kept_to_dto(visuals.get_kept(db, request.path_params["id"]))


@_page
def kept_page(db: DB, request: Request) -> Path:
    return visuals.kept_page(db, request.path_params["id"])


ROUTES = [
    WebSocketRoute("/v1/visuals/{id}/live", visual_live),
    Route("/v1/library/designs/{id}/question", _endpoint(doc_question), methods=["POST"]),
    Route("/v1/library/designs/{id}/plan", library_design_plan),
    Route("/v1/sessions/{id:path}/visual-input", _endpoint(visual_input), methods=["POST"]),
    Route("/v1/sessions/{id:path}/visual-offer", session_offer),
    Route("/v1/sessions/{id:path}/visual-offer", _endpoint(resolve_offer), methods=["POST"]),
    Route("/v1/sessions/{id:path}/visuals", session_visuals),
    Route("/v1/visuals/{id}/r/{n}", visual_page),
    Route("/v1/visuals/{id}/answer", _endpoint(answer), methods=["POST"]),
    Route("/v1/visuals/{id}/dismiss", _endpoint(dismiss), methods=["POST"]),
    Route("/v1/visuals/{id}/keep", _endpoint(keep), methods=["POST"]),
    Route("/v1/visuals/{id}/keep", _endpoint(unkeep), methods=["DELETE"]),
    Route("/v1/visuals/{id}/read", _endpoint(mark_read), methods=["POST"]),
    Route("/v1/visuals/{id}/snapshot", _endpoint(snapshot), methods=["POST"]),
    Route("/v1/plans/{id}", plan),
    Route("/v1/plans/{id}/decide", _endpoint(decide_plan), methods=["POST"]),
    Route("/v1/plans/{id}/revisions/{n}", plan_revision),
    Route("/v1/plans/{id}/history", plan_history),
    Route("/v1/plans/{id}/revisions/{n}/changes", plan_comparison),
    Route("/v1/plans/{id}/comments", _endpoint(plan_comment), methods=["POST"]),
    Route("/v1/plans/{id}/suggestions", _endpoint(plan_suggest), methods=["POST"]),
    Route("/v1/plans/{id}/edit", _endpoint(plan_edit), methods=["POST"]),
    Route("/v1/plans/{id}/confirm", _endpoint(plan_confirm), methods=["POST"]),
    Route("/v1/plans/{id}/threads/{tid:int}/resolve", _endpoint(plan_resolve), methods=["POST"]),
    Route("/v1/plans/{id}/threads/{tid:int}/accept", _endpoint(plan_accept), methods=["POST"]),
    Route("/v1/plans/{id}/threads/{tid:int}/reject", _endpoint(plan_reject), methods=["POST"]),
    Route("/v1/plans/{id}/share", _endpoint(plan_share), methods=["POST"]),
    Route("/v1/plans/{id}/reviewers", _endpoint(plan_reviewers), methods=["POST"]),
    Route("/v1/plans/{id}/sync", _endpoint(plan_sync), methods=["POST"]),
    Route("/v1/plans/{id}/stop-sharing", _endpoint(plan_stop_sharing), methods=["POST"]),
    Route("/v1/plans/{id}/sign-off", _endpoint(plan_sign_off), methods=["POST"]),
    Route("/v1/plans/{id}/policy-override", _endpoint(plan_policy_override), methods=["POST"]),
    Route("/v1/plans/{id}/policy-ask", _endpoint(plan_policy_ask), methods=["POST"]),
    Route("/v1/plans/{id}/policy-preview", _endpoint(plan_policy_preview), methods=["POST"]),
    Route("/v1/review-policies", review_policies_read),
    Route("/v1/review-policies", _endpoint(review_policies_save), methods=["PUT"]),
    Route("/v1/review-policies/draft-impact", _endpoint(review_policies_impact), methods=["POST"]),
    Route("/v1/library/people", library_people),
    Route("/v1/shared-designs/{doc_id}", shared_design),
    Route("/v1/shared-designs/{doc_id}/comments", _endpoint(shared_design_comment), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/suggestions", _endpoint(shared_design_suggest), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/threads/{tid:int}", _endpoint(shared_design_thread), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/votes", _endpoint(shared_design_vote), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/sign-offs", _endpoint(shared_design_sign_off), methods=["POST"]),
    Route("/v1/kept-visuals", kept_list),
    Route("/v1/kept-visuals/{id}", kept_one),
    Route("/v1/kept-visuals/{id}/html", kept_page),
]
