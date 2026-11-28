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
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import quote

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
            raise ServiceError("not_found", "no live text for this plan here", 404)
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
                raise ServiceError("not_found", "no live text for this plan here", 404)
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
    # typed_words: what the person keyed in, or null when the app saw the keys but can't say the text
    # (a suggestion they accepted, an edit it can't follow). typed=false: a submission that wasn't typed.
    if "typed_words" in body:
        typed_words = _get(body, "typed_words")
        if expires_at is None or (typed_words is not None and len(typed_words) > 32000):
            raise ServiceError("invalid", "typed_words needs expires_at and a sensible length", 400)
        visual_offers.keep_typed(db, agent, typed_words, expires_at, submitted_at,
                                 typed=_get(body, "typed", bool) is not False)
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
                                             _get(body, "note"), _get(body, "attachments", list), body.get("picks")))


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
                     attachments=_get(body, "attachments", list), version=body.get("version"))
    return plans.to_dto(db, p)


def cancel_plan(db: DB, params: dict, body: dict) -> Any:
    """The person cancels an open or redirected plan (brindle.plans.cancel): closed, nothing approved."""
    from brindle import plans

    return plans.to_dto(db, plans.cancel(db, params["id"], body.get("rev"), person=True))


def stop_alongside(db: DB, params: dict, body: dict) -> Any:
    """The person's Stop on a task running alongside an open plan (brindle.plans.stop_alongside):
    a queued one is cancelled, a started one's workspace removed."""
    from brindle import plans

    return plans.to_dto(db, plans.stop_alongside(db, params["id"], params["task"]))


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
    from brindle.pro import doc_questions, shared_docs

    repo = _get(body, "repo")
    owner = _shared_design(repo, lambda _: shared_docs._require_team(None).sub, require_entitlement=False)
    if not owner:
        raise ServiceError("forbidden", "sign in to ask your supervisor", 403)
    thread = _get(body, "thread", int, required=True)
    return JSONResponse({"state": "queued"}, background=BackgroundTask(
        doc_questions.after_reply, doc_id=params["id"], thread_id=thread, owner=owner,
        repo_root=repo))


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


def plan_answer_add(db: DB, params: dict, body: dict) -> Any:
    """The person's own answer on a decision, picked as it is added (brindle.plan_answers)."""
    from brindle import plan_answers

    plan_answers.add(db, params["id"], body.get("rev"), body.get("decision"), body.get("text"))
    return _plan_now(db, params["id"])


def plan_answer_change(db: DB, params: dict, body: dict) -> Any:
    """Reword the person's own answer ({rev, text}) or take it back ({rev, remove: true})."""
    from brindle import plan_answers

    if body.get("remove") is True:
        plan_answers.remove(db, params["id"], body.get("rev"), params["answer"])
    else:
        plan_answers.edit(db, params["id"], body.get("rev"), params["answer"], body.get("text"))
    return _plan_now(db, params["id"])


# Team review through the Library (brindle.pro.shared_docs). Each answers with
# the plan as it is now, its build gate included.


def _library_call(work: Callable[[Any], Any]) -> Any:
    from brindle import keystore, redact
    from brindle.daemon import routes_actions as actions
    from brindle.pro import auth, shared_docs

    try:
        return work(shared_docs)
    except (RuntimeError, auth.AuthError, keystore.KeychainReadBlocked) as e:
        err = actions.upstream_error(e)
        raise ServiceError(err.code, redact.mask(err.message), err.http_status) from e


def _library(db: DB, plan_id: str, work: Callable[[Any], Any]) -> Any:
    _library_call(work)
    return _plan_now(db, plan_id)


def plan_share(db: DB, params: dict, body: dict) -> Any:
    return _library(db, params["id"], lambda sd: sd.share(db, params["id"], body.get("scope"),
                                                          reviewers=body.get("reviewers"),
                                                          permissions=_get(body, "permissions", dict)))


def plan_reviewers(db: DB, params: dict, body: dict) -> Any:
    return _library(db, params["id"], lambda sd: sd.set_reviewers(db, params["id"], body.get("reviewers")))


def plan_reviewer_put(db: DB, params: dict, body: dict) -> Any:
    return _library(db, params["id"], lambda sd: sd.set_reviewer(
        db, params["id"], params["sub"], _get(body, "required", bool, default=True, required=True)))


def plan_reviewer_delete(db: DB, params: dict, body: dict) -> Any:
    return _library(db, params["id"], lambda sd: sd.set_reviewer(db, params["id"], params["sub"], None))


def _kept_here() -> bool | None:
    """Whether the person's plans are kept on this Mac, so a first share uploads
    one; None when that can't be said now."""
    from brindle.daemon import routes_actions as actions
    from brindle.pro import auth

    try:
        return actions.library_on_this_mac()
    except (ServiceError, RuntimeError, auth.AuthError, auth.TransportError):
        return None


@_json
def plan_access(db: DB, request: Request) -> Any:
    """Who has the plan and what each may do (pro/share_access.py)."""
    from brindle.pro import share_access, share_invites

    plan_id = request.path_params["id"]
    got = _library_call(lambda sd: share_access.plan_access(db, plan_id))
    got = share_invites.merge(got, share_invites.pending(db, plan_id))     # with the invites still on their way
    return {**got, "kept_here": None if got["shared"] else _kept_here()}


@_json
def plan_invites(db: DB, request: Request) -> Any:
    """The share panel's changes still on their way to the Library, read
    from this Mac alone: shown even while the Library can't be reached."""
    from brindle import plan_docs
    from brindle.pro import share_invites

    plan_id = request.path_params["id"]
    plan_docs._plan(db, plan_id)
    return {"people": [share_invites.dto(r) for r in share_invites.pending(db, plan_id)]}


def plan_invite_dismiss(db: DB, params: dict, body: dict) -> Any:
    """Let a refused change to one person go, or discard one never confirmed;
    the Library keeps what it has."""
    from brindle import plan_docs
    from brindle.pro import share_invites

    plan_docs._plan(db, params["id"])
    _library_call(lambda sd: share_invites.dismiss(db, params["id"], params["sub"], params.get("rev")))
    return _plan_now(db, params["id"])


def _access_write(db: DB, plan_id: str, work: Callable[[Any], Any], first: bool = False) -> Any:
    from brindle.pro import share_access

    return _library(db, plan_id, lambda sd: share_access.plan_write(db, plan_id, work, first=first))


def _invite_out(db: DB, plan_id: str, row: dict) -> JSONResponse:
    """Answered before the Library hears of it (202): the plan now and the
    person's row as it will show, with the receipt the app sends back
    (plan_invite_receipts) once it has this answer; only then is it sent."""
    from brindle.pro import share_invites

    return JSONResponse({**_plan_now(db, plan_id), "invite": share_invites.dto(row)}, 202)


def plan_invite_receipts(db: DB, params: dict, body: dict) -> Any:
    """The app has the answers that named these receipts: the daemon sends
    those changes once this is answered (pro/share_invites.py)."""
    from brindle import plan_docs
    from brindle.pro import share_invites

    plan_docs._plan(db, params["id"])
    got = share_invites.ack(db, params["id"], body.get("receipts"))
    return JSONResponse({"acknowledged": got}, background=BackgroundTask(share_invites.kick, params["id"]))


def plan_person_invite(db: DB, params: dict, body: dict) -> Any:
    """Give one person the plan (its first share, if nobody has it yet), in
    the background, as the account signed in now: pro/share_invites.py."""
    from brindle.pro import share_invites

    sign_off = _get(body, "sign_off", bool, default=False)
    return _invite_out(db, params["id"], _library_call(lambda sd: share_invites.record(
        db, params["id"], body.get("sub"), body.get("access"), sign_off)))


def plan_person_put(db: DB, params: dict, body: dict) -> Any:
    from brindle.pro import share_access as sa
    from brindle.pro import share_invites

    if row := _library_call(lambda sd: share_invites.change(db, params["id"], params["sub"], "access",
                                                             body.get("access"))):
        return _invite_out(db, params["id"], row)       # after their invite, still out
    return _access_write(db, params["id"], lambda call: sa.change(call, params["sub"], body.get("access")))


def plan_person_sign_off(db: DB, params: dict, body: dict) -> Any:
    from brindle.pro import share_access as sa
    from brindle.pro import share_invites

    if row := _library_call(lambda sd: share_invites.change(db, params["id"], params["sub"], "sign_off",
                                                             body.get("must"))):
        return _invite_out(db, params["id"], row)
    return _access_write(db, params["id"], lambda call: sa.sign_off(call, params["sub"], body.get("must")))


def plan_person_delete(db: DB, params: dict, body: dict) -> Any:
    from brindle.pro import share_access as sa
    from brindle.pro import share_invites

    row = _library_call(lambda sd: share_invites.withdraw(db, params["id"], params["sub"]))
    if row:
        return _invite_out(db, params["id"], row)
    if row == {}:
        return _plan_now(db, params["id"])              # never sent: nothing to take back
    return _access_write(db, params["id"], lambda call: sa.revoke(call, params["sub"]))


def plan_scope_delete(db: DB, params: dict, body: dict) -> Any:
    from brindle.pro import share_access as sa

    return _access_write(db, params["id"], lambda call: sa.revoke_scope(call, params["kind"], params["scope"]))


def plan_unshare(db: DB, params: dict, body: dict) -> Any:
    """Stop sharing the plan with everyone it was shared with, invites still
    out included: one being sent lands first, and is stopped with the rest."""
    from brindle.pro import share_access as sa
    from brindle.pro import share_invites

    with share_invites.held(params["id"]):
        share_invites.drop_all(db, params["id"])
        return _access_write(db, params["id"], sa.stop)


@_json
def plan_people(db: DB, request: Request) -> Any:
    """Per-doc search for older clients: the Library's search of its doc, or the
    org's member list when the search is for anyone in the org and the doc
    can't answer it. That is a plan with no Library doc of this account yet
    (a first share still uploading, or one whose upload timed out), or a
    sharer the Library won't let list people through the doc (that needs a
    Members-page permission, 403 forbidden) though the org's member list is
    open to every member. The Library still decides who may be added."""
    from brindle import plan_docs
    from brindle.pro import share_links

    plan_id, repo, query = request.path_params["id"], request.query_params.get("repo") or None, dict(request.query_params)
    plan_docs._plan(db, plan_id)
    anyone = query.get("include_unshared") == "true" and not any(query.get(k) for k in ("team", "role", "on_plan"))

    def search(reader) -> dict | None:
        # Resolved once, after the repo's configuration and the entitlement are checked.
        doc = share_links.plan_doc(db, plan_id)
        if doc is None and not anyone:
            raise ServiceError("not_in_library", "this plan isn't in your Library, so it has no link", 409)
        return None if doc is None else reader.people(doc, query)

    try:
        got = _shared_design(repo, search)
    except ServiceError as e:
        if not (anyone and e.http_status == 403 and e.code == "forbidden"):
            raise
        got = None
    return _org_people(request, query.get("q") or "") if got is None else got


def _org_people(request: Request, q: str) -> dict:
    """The org's people matching ``q``, as the Library's search answers. Who
    already has the plan isn't known here: the panel marks them from its access list."""
    from brindle.service.dto import SharePeopleDTO

    _, members = _org_members(request)
    rows = []
    for m in members:
        name = f"{m.get('first_name') or ''} {m.get('last_name') or ''}".strip() or m.get("name") \
            or m.get("display_name") or m["email"] or m["sub"]
        if q.strip().casefold() in f"{name} {m['email']}".casefold():
            rows.append({"id": m["sub"], "display_name": name, "email": m["email"], "teams": [],
                         "role": m.get("role") or "", "on_plan": False})
    rows.sort(key=lambda r: (r["display_name"].casefold(), r["id"]))
    return SharePeopleDTO.model_validate({"people": rows}).model_dump()


def plan_sync(db: DB, params: dict, body: dict) -> Any:
    from brindle.pro import share_invites

    plan_id = params["id"]
    include_access = _get(body, "include_access", bool, default=False)
    got = _library_call(lambda sd: sd.sync(db, plan_id, include_access=True) if include_access
                        else sd.sync(db, plan_id))
    plan = _plan_now(db, plan_id)
    if include_access and (access := got.get("access")) is not None:
        plan["access"] = {**share_invites.merge(access, share_invites.pending(db, plan_id)), "kept_here": None}
    return plan


def plan_stop_sharing(db: DB, params: dict, body: dict) -> Any:
    from brindle.pro import shared_docs

    shared_docs.stop_sharing(db, params["id"], body.get("rev"))
    return _plan_now(db, params["id"])


def plan_sign_off(db: DB, params: dict, body: dict) -> Any:
    return _library(db, params["id"], lambda sd: sd.sign_off(db, params["id"], body.get("rev"),
                                                             version=body.get("version")))


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
    from concurrent.futures import ThreadPoolExecutor

    from brindle import spawn
    from brindle.daemon import routes_actions as actions

    repo = request.query_params.get("repo") or None
    rules = actions.docs_org_api("GET", "review-policies", repo=repo)
    org = rules.get("org_id")      # the rest is of the org the rules were, even if the active one changes meanwhile
    read = lambda tail: actions.docs_org_api("GET", tail, repo=repo, org_id=org)  # noqa: E731
    # The directory and the designs in review are two Library round trips: both at once.
    with ThreadPoolExecutor(max_workers=2) as pool:
        directory, in_review = pool.map(spawn.carry(read), ("review-policies/directory", "review-policies/in-review"))
    return {**rules, "directory": directory, "in_review": in_review}


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


def _shared_design(repo: str | None, work: Callable[[Any], Any], *, require_entitlement: bool = True) -> Any:
    from pydantic import ValidationError

    from brindle import airgap, config, keystore
    from brindle.daemon import routes_actions as actions
    from brindle.pro import analysis_docs, auth, shared_reader

    if repo:
        config.load_repo_config(repo)      # its air-gap setting applies before any credential is read
    try:
        if require_entitlement:
            actions._docs_entitlement(repo, None if airgap.enabled() else analysis_docs._session()[1])
        return work(shared_reader)
    except (RuntimeError, auth.AuthError, keystore.KeychainReadBlocked, ValidationError) as e:
        raise actions.upstream_error(e) from e


@_json
def shared_design(db: DB, request: Request) -> Any:
    doc = request.path_params["doc_id"]
    return _shared_design(request.query_params.get("repo") or None, lambda r: r.read(doc))


@_json
def design_people(db: DB, request: Request) -> Any:
    return _shared_design(request.query_params.get("repo") or None,
                          lambda r: r.people(request.path_params["id"], dict(request.query_params)))


@_json
def design_access(db: DB, request: Request) -> Any:
    from brindle.pro import share_access

    return {**_shared_design(request.query_params.get("repo") or None,
                             lambda r: share_access.design_access(request.path_params["id"])), "kept_here": None}


def _design_access_write(doc_id: str, repo: str | None, work: Callable[[Any], Any]) -> Any:
    from brindle.pro import share_access

    return _shared_design(repo, lambda r: (share_access.design_write(doc_id, work), r.read(doc_id))[1])


def design_person_invite(db: DB, params: dict, body: dict) -> Any:
    from brindle.pro import share_access as sa

    return _design_access_write(params["id"], _get(body, "repo"), lambda call: sa.invite(
        call, body.get("sub"), body.get("access"), _get(body, "sign_off", bool, default=False)))


def design_person_put(db: DB, params: dict, body: dict) -> Any:
    from brindle.pro import share_access as sa

    return _design_access_write(params["id"], _get(body, "repo"),
                                lambda call: sa.change(call, params["sub"], body.get("access")))


def design_person_sign_off(db: DB, params: dict, body: dict) -> Any:
    from brindle.pro import share_access as sa

    return _design_access_write(params["id"], _get(body, "repo"),
                                lambda call: sa.sign_off(call, params["sub"], body.get("must")))


def design_person_delete(db: DB, params: dict, body: dict) -> Any:
    from brindle.pro import share_access as sa

    return _design_access_write(params["id"], params.get("repo") or None, lambda call: sa.revoke(call, params["sub"]))


def design_scope_delete(db: DB, params: dict, body: dict) -> Any:
    from brindle.pro import share_access as sa

    return _design_access_write(params["id"], params.get("repo") or None,
                                lambda call: sa.revoke_scope(call, params["kind"], params["scope"]))


def design_reviewer_put(db: DB, params: dict, body: dict) -> Any:
    return _shared_design(_get(body, "repo"), lambda r: r.reviewer(
        params["id"], params["sub"], _get(body, "required", bool, default=True, required=True)))


def design_reviewer_delete(db: DB, params: dict, body: dict) -> Any:
    return _shared_design(params.get("repo") or None, lambda r: r.reviewer(params["id"], params["sub"], None))


@_json
def shared_design_history(db: DB, request: Request) -> Any:
    return _shared_design(request.query_params.get("repo") or None,
                          lambda r: r.timeline(request.path_params["doc_id"]))


@_json
def shared_design_comparison(db: DB, request: Request) -> Any:
    return _shared_design(request.query_params.get("repo") or None,
                          lambda r: r.comparison(request.path_params["doc_id"], request.path_params["n"]))


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


def shared_design_answer_add(db: DB, params: dict, body: dict) -> Any:
    return _shared_design(_get(body, "repo"), lambda r: r.add_answer(params["doc_id"], body.get("rev"),
                                                                     body.get("decision"), body.get("text")))


def shared_design_answer_change(db: DB, params: dict, body: dict) -> Any:
    if body.get("remove") is True:
        return _shared_design(_get(body, "repo"), lambda r: r.remove_answer(params["doc_id"], body.get("rev"),
                                                                            params["answer"]))
    return _shared_design(_get(body, "repo"), lambda r: r.edit_answer(params["doc_id"], body.get("rev"),
                                                                      params["answer"], body.get("text")))


def shared_design_sign_off(db: DB, params: dict, body: dict) -> Any:
    """A teammate's sign-off. On a live doc the app names the frozen version on
    screen: that version is what they read (its own text), so it is the one
    signed, even if their read of the revision came before the freeze."""
    version = body.get("version")
    return _shared_design(_get(body, "repo"), lambda r: r.sign_off(
        params["doc_id"], body.get("rev") if version is None else version))


def shared_design_access(db: DB, params: dict, body: dict) -> Any:
    return _shared_design(_get(body, "repo"), lambda r: r.request_access(
        params["doc_id"], body.get("role"), params.get("sub")))


def shared_design_access_decline(db: DB, params: dict, body: dict) -> Any:
    return _shared_design(_get(body, "repo"), lambda r: r.request_access(
        params["doc_id"], body.get("role"), params["sub"], action="decline"))


def share_link_design(db: DB, params: dict, body: dict) -> Any:
    """A plan opened from its share link (the app's brindle://plan/… deep link),
    read through the link as the account signed in now. The token comes in the
    body, never the path: a path can end up in a log, and the token is the
    link's whole secret."""
    # A signed-in account can open a share link on any plan.
    return _shared_design(None, lambda r: r.read_link(body.get("token")), require_entitlement=False)


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
        raise ServiceError("not_found", "this plan wasn't shared from here by this account", 404)
    v = visuals.get(db, row["plan_id"])
    visuals._unlocked(v["root_id"])
    return visuals.to_dto(db, v)


def _share_link(repo: str | None, work: Callable[[Any], Any]) -> Any:
    """A design's share link (brindle.pro.share_links), by its Library doc id.
    Below Pro a read answers ``available: false`` and nothing is sent; the
    Library's refusal (an org's link policy, not an editor) keeps its reason."""
    from brindle import config, keystore, redact
    from brindle.daemon import routes_actions as actions
    from brindle.pro import analysis_docs, auth, share_links

    if repo:
        config.load_repo_config(repo)        # its air-gap setting applies first
    try:
        return work(share_links)
    except (analysis_docs.Refused, analysis_docs.SignInBusy, auth.AuthError, keystore.KeychainReadBlocked) as e:
        err = actions.upstream_error(e)
        raise ServiceError(err.code, redact.mask(err.message), err.http_status) from e


@_json
def share_link(db: DB, request: Request) -> Any:
    return _share_link(request.query_params.get("repo") or None, lambda s: s.read(request.path_params["id"]))


def share_link_copy(db: DB, params: dict, body: dict) -> Any:
    """The link to copy: the design's own, or a new one when it has none."""
    return _share_link(_get(body, "repo"), lambda s: s.copy(params["id"], scope=_get(body, "scope"), team=_get(body, "team"),
                                                        permission=_get(body, "permission")))


def share_link_update(db: DB, params: dict, body: dict) -> Any:
    return _share_link(_get(body, "repo"), lambda s: s.update(params["id"], body))


def share_link_revoke(db: DB, params: dict, body: dict) -> Any:
    return _share_link(params.get("repo") or None, lambda s: s.revoke(params["id"]))


# The same link for a plan opened in its session: its Library doc, when this
# account shared it from here. A plan not in the Library has no link to offer.


def _plan_doc(db: DB, plan_id: str) -> str:
    from brindle.pro import share_links

    doc = share_links.plan_doc(db, plan_id)
    if doc is None:
        raise ServiceError("not_in_library", "this plan isn't in your Library, so it has no link", 409)
    return doc


@_json
def plan_share_link(db: DB, request: Request) -> Any:
    repo = request.query_params.get("repo") or None
    doc = _share_link(repo, lambda s: s.plan_doc(db, request.path_params["id"]))
    return {"available": False, "link": None} if doc is None else _share_link(repo, lambda s: s.read(doc))


def plan_share_link_copy(db: DB, params: dict, body: dict) -> Any:
    return _share_link(_get(body, "repo"), lambda s: s.copy(_plan_doc(db, params["id"]),
                                                        scope=_get(body, "scope"), team=_get(body, "team"),
                                                        permission=_get(body, "permission")))


def plan_share_link_update(db: DB, params: dict, body: dict) -> Any:
    return _share_link(_get(body, "repo"), lambda s: s.update(_plan_doc(db, params["id"]), body))


def plan_share_link_revoke(db: DB, params: dict, body: dict) -> Any:
    return _share_link(params.get("repo") or None, lambda s: s.revoke(_plan_doc(db, params["id"])))


@_json
def library_people(db: DB, request: Request) -> Any:
    """The people of the person's org, to pick reviewers from, leaving out
    the person (``me``). ``admin_org``: the org whose review rules the doc
    offers them an override of. Only what to show: the Library decides who may."""
    ent, members = _org_members(request)
    if not ent.org_id:
        return {"people": []}
    return {"people": [{k: v for k, v in m.items() if k != "role"} for m in members],
            "me": ent.sub, "admin_org": ent.org_id if ent.role in ("owner", "admin") else None}


class OrgMembersCache:
    """Keep the active sign-in's directory briefly and coalesce concurrent reads."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._key: tuple[str, str, str] | None = None
        self._expires = 0.0
        self._members: list[dict] = []

    def read(self, client, store, ent) -> list[dict]:
        from brindle.pro import analysis_docs

        key = client.base, ent.sub, ent.org_id
        with self._lock:
            if key == self._key and monotonic() < self._expires:
                return self._members
            path = f"/orgs/{quote(ent.org_id, safe='')}/members"
            response = analysis_docs.api("GET", path, client=client, store=store)
            self._members = _directory_rows(response.get("members"), ent.sub)
            self._key = key
            self._expires = monotonic() + 60
            return self._members


def _org_members(request: Request) -> tuple[Any, list[dict]]:
    """Check entitlement on every read, even when the directory is cached."""

    from brindle.daemon import routes_actions as actions
    from brindle.pro import analysis_docs

    ent = actions._docs_entitlement(request.query_params.get("repo") or None)
    if not ent.org_id:
        return ent, []

    def read(_):
        client, store = analysis_docs._session()
        pinned = actions._docs_entitlement(None, store)
        return pinned, request.app.state.org_members.read(client, store, pinned) if pinned.org_id else []

    return _library_call(read)


def _directory_rows(members: Any, sub: str) -> list[dict]:
    return [{"sub": m["sub"], "email": m.get("email") or "",
             **{k: m[k] for k in ("display_name", "first_name", "last_name", "name") if isinstance(m.get(k), str)},
             "role": next((m[k] for k in ("policy_role", "role") if isinstance(m.get(k), str) and m[k]), "")}
            for m in members or []
            if isinstance(m, dict) and isinstance(m.get("sub"), str) and m["sub"] != sub]


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
    Route("/v1/library/designs/{id}/people", design_people),
    Route("/v1/library/designs/{id}/reviewers/{sub}", _endpoint(design_reviewer_put), methods=["PUT"]),
    Route("/v1/library/designs/{id}/reviewers/{sub}", _endpoint(design_reviewer_delete), methods=["DELETE"]),
    Route("/v1/library/designs/{id}/share-link", _endpoint(share_link_update), methods=["PUT"]),
    Route("/v1/library/designs/{id}/access", design_access),
    Route("/v1/library/designs/{id}/people", _endpoint(design_person_invite), methods=["POST"]),
    Route("/v1/library/designs/{id}/people/{sub}", _endpoint(design_person_put), methods=["PUT"]),
    Route("/v1/library/designs/{id}/people/{sub}", _endpoint(design_person_delete), methods=["DELETE"]),
    Route("/v1/library/designs/{id}/people/{sub}/sign-off", _endpoint(design_person_sign_off), methods=["PUT"]),
    Route("/v1/library/designs/{id}/scopes/{kind}/{scope}", _endpoint(design_scope_delete), methods=["DELETE"]),
    Route("/v1/plans/{id}/access", plan_access),
    Route("/v1/plans/{id}/access", _endpoint(plan_unshare), methods=["DELETE"]),
    Route("/v1/plans/{id}/invites", plan_invites),
    Route("/v1/plans/{id}/invites/receipts", _endpoint(plan_invite_receipts), methods=["POST"]),
    Route("/v1/plans/{id}/invites/{sub}", _endpoint(plan_invite_dismiss), methods=["DELETE"]),
    Route("/v1/plans/{id}/people", plan_people),
    Route("/v1/plans/{id}/people", _endpoint(plan_person_invite), methods=["POST"]),
    Route("/v1/plans/{id}/people/{sub}", _endpoint(plan_person_put), methods=["PUT"]),
    Route("/v1/plans/{id}/people/{sub}", _endpoint(plan_person_delete), methods=["DELETE"]),
    Route("/v1/plans/{id}/people/{sub}/sign-off", _endpoint(plan_person_sign_off), methods=["PUT"]),
    Route("/v1/plans/{id}/scopes/{kind}/{scope}", _endpoint(plan_scope_delete), methods=["DELETE"]),
    Route("/v1/plans/{id}/reviewers/{sub}", _endpoint(plan_reviewer_put), methods=["PUT"]),
    Route("/v1/plans/{id}/reviewers/{sub}", _endpoint(plan_reviewer_delete), methods=["DELETE"]),
    Route("/v1/plans/{id}/share-link", _endpoint(plan_share_link_update), methods=["PUT"]),
    WebSocketRoute("/v1/visuals/{id}/live", visual_live),
    Route("/v1/library/designs/{id}/question", _endpoint(doc_question), methods=["POST"]),
    Route("/v1/library/designs/{id}/plan", library_design_plan),
    Route("/v1/library/designs/{id}/share-link", share_link),
    Route("/v1/library/designs/{id}/share-link", _endpoint(share_link_copy), methods=["POST"]),
    Route("/v1/library/designs/{id}/share-link", _endpoint(share_link_revoke), methods=["DELETE"]),
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
    Route("/v1/plans/{id}/cancel", _endpoint(cancel_plan), methods=["POST"]),
    Route("/v1/plans/{id}/alongside/{task}/stop", _endpoint(stop_alongside), methods=["POST"]),
    Route("/v1/plans/{id}/revisions/{n}", plan_revision),
    Route("/v1/plans/{id}/history", plan_history),
    Route("/v1/plans/{id}/revisions/{n}/changes", plan_comparison),
    Route("/v1/plans/{id}/comments", _endpoint(plan_comment), methods=["POST"]),
    Route("/v1/plans/{id}/suggestions", _endpoint(plan_suggest), methods=["POST"]),
    Route("/v1/plans/{id}/edit", _endpoint(plan_edit), methods=["POST"]),
    Route("/v1/plans/{id}/confirm", _endpoint(plan_confirm), methods=["POST"]),
    Route("/v1/plans/{id}/answers", _endpoint(plan_answer_add), methods=["POST"]),
    Route("/v1/plans/{id}/answers/{answer}", _endpoint(plan_answer_change), methods=["POST"]),
    Route("/v1/plans/{id}/threads/{tid:int}/resolve", _endpoint(plan_resolve), methods=["POST"]),
    Route("/v1/plans/{id}/threads/{tid:int}/accept", _endpoint(plan_accept), methods=["POST"]),
    Route("/v1/plans/{id}/threads/{tid:int}/reject", _endpoint(plan_reject), methods=["POST"]),
    Route("/v1/plans/{id}/share", _endpoint(plan_share), methods=["POST"]),
    Route("/v1/plans/{id}/reviewers", _endpoint(plan_reviewers), methods=["POST"]),
    Route("/v1/plans/{id}/sync", _endpoint(plan_sync), methods=["POST"]),
    Route("/v1/plans/{id}/share-link", plan_share_link),
    Route("/v1/plans/{id}/share-link", _endpoint(plan_share_link_copy), methods=["POST"]),
    Route("/v1/plans/{id}/share-link", _endpoint(plan_share_link_revoke), methods=["DELETE"]),
    Route("/v1/plans/{id}/stop-sharing", _endpoint(plan_stop_sharing), methods=["POST"]),
    Route("/v1/plans/{id}/sign-off", _endpoint(plan_sign_off), methods=["POST"]),
    Route("/v1/plans/{id}/policy-override", _endpoint(plan_policy_override), methods=["POST"]),
    Route("/v1/plans/{id}/policy-ask", _endpoint(plan_policy_ask), methods=["POST"]),
    Route("/v1/plans/{id}/policy-preview", _endpoint(plan_policy_preview), methods=["POST"]),
    Route("/v1/review-policies", review_policies_read),
    Route("/v1/review-policies", _endpoint(review_policies_save), methods=["PUT"]),
    Route("/v1/review-policies/draft-impact", _endpoint(review_policies_impact), methods=["POST"]),
    Route("/v1/library/people", library_people),
    Route("/v1/share-links/design", _endpoint(share_link_design), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}", shared_design),
    Route("/v1/shared-designs/{doc_id}/history", shared_design_history),
    Route("/v1/shared-designs/{doc_id}/revisions/{n:int}/changes", shared_design_comparison),
    Route("/v1/shared-designs/{doc_id}/comments", _endpoint(shared_design_comment), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/suggestions", _endpoint(shared_design_suggest), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/threads/{tid:int}", _endpoint(shared_design_thread), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/votes", _endpoint(shared_design_vote), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/answers", _endpoint(shared_design_answer_add), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/answers/{answer}", _endpoint(shared_design_answer_change), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/sign-offs", _endpoint(shared_design_sign_off), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/access-requests", _endpoint(shared_design_access), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/access-requests/{sub}/grant", _endpoint(shared_design_access), methods=["POST"]),
    Route("/v1/shared-designs/{doc_id}/access-requests/{sub}/decline", _endpoint(shared_design_access_decline), methods=["POST"]),
    Route("/v1/kept-visuals", kept_list),
    Route("/v1/kept-visuals/{id}", kept_one),
    Route("/v1/kept-visuals/{id}/html", kept_page),
]
