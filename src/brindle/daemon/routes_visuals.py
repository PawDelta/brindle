"""The daemon's visuals routes (brindle.visuals): a session's visuals, their
pages, the person's answer, Keep, and the kept store. Every route is behind
the daemon Guard (the token only the shell holds) and refuses while the
visuals capability is off. A page is served with the visuals CSP; the shell's
``brindle-visual://`` scheme is its only intended reader."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from brindle import visuals, visual_offers
from brindle.daemon.routes_actions import _endpoint, _get
from brindle.daemon.routes_read import _json
from brindle.db import DB
from brindle.service.errors import ServiceError, from_exception

log = logging.getLogger(__name__)


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
    return [visuals.to_dto(v) for v in visuals.for_session(db, root)]


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
    text = _get(body, "text", required=True)
    if len(text) > 32000:
        raise ServiceError("invalid", "input is too long", 400)
    visual_offers.person_spoke(db, agent, text)
    return {"observed": True}


@_page
def visual_page(db: DB, request: Request) -> Path:
    return visuals.page(db, request.path_params["id"], request.path_params["n"])


def answer(db: DB, params: dict, body: dict) -> Any:
    visuals.require_enabled()
    rev = body.get("rev")
    return visuals.to_dto(visuals.answer(db, params["id"], rev, _get(body, "option", required=True),
                                         _get(body, "note")))


def keep(db: DB, params: dict, body: dict) -> Any:
    visuals.require_enabled()
    return visuals.to_dto(visuals.keep(db, params["id"]))


def unkeep(db: DB, params: dict, body: dict) -> Any:
    visuals.require_enabled()
    return visuals.to_dto(visuals.unkeep(db, params["id"]))


def mark_read(db: DB, params: dict, body: dict) -> Any:
    visuals.require_enabled()
    return visuals.to_dto(visuals.mark_read(db, params["id"]))


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
    Route("/v1/sessions/{id:path}/visual-input", _endpoint(visual_input), methods=["POST"]),
    Route("/v1/sessions/{id:path}/visual-offer", session_offer),
    Route("/v1/sessions/{id:path}/visual-offer", _endpoint(resolve_offer), methods=["POST"]),
    Route("/v1/sessions/{id:path}/visuals", session_visuals),
    Route("/v1/visuals/{id}/r/{n}", visual_page),
    Route("/v1/visuals/{id}/answer", _endpoint(answer), methods=["POST"]),
    Route("/v1/visuals/{id}/keep", _endpoint(keep), methods=["POST"]),
    Route("/v1/visuals/{id}/keep", _endpoint(unkeep), methods=["DELETE"]),
    Route("/v1/visuals/{id}/read", _endpoint(mark_read), methods=["POST"]),
    Route("/v1/kept-visuals", kept_list),
    Route("/v1/kept-visuals/{id}", kept_one),
    Route("/v1/kept-visuals/{id}/html", kept_page),
]
