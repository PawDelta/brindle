"""The daemon's routes, all under ``/v1``, behind :class:`brindle.daemon.auth.Guard`."""

from __future__ import annotations

import os
import shutil

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp

import brindle
from brindle import airgap
from brindle.config import db_path
from brindle.daemon import routes_read
from brindle.daemon.auth import Guard
from brindle.daemon.events import events
from brindle.daemon.holds import ROUTES as HOLD_ROUTES
from brindle.daemon.pty_view import pty_view
from brindle.daemon.routes_actions import ROUTES as ACTIONS
from brindle.daemon.routes_org_permissions import ROUTES as ORG_PERMISSION_ROUTES

API_VERSION = "1.0"


async def meta(request: Request) -> JSONResponse:
    """What the app checks before anything else: which engine and API this is."""
    return JSONResponse({
        "engine_version": brindle.__version__,
        "api_version": API_VERSION,
        "pid": os.getpid(),
        "db_path": str(db_path()),
        "tmux": shutil.which("tmux") is not None,
        "airgap": airgap.enabled(),
        "pty_view": True,       # WS /v1/pty_view: the Windows app's WSL bridge
    })


ROUTES = [Route("/v1/meta", meta), *routes_read.ROUTES,
          WebSocketRoute("/v1/events", events), WebSocketRoute("/v1/pty_view", pty_view),
          *ACTIONS, *HOLD_ROUTES, *ORG_PERMISSION_ROUTES]


def build_app(token: str, port: int | None = None, poller=None) -> ASGIApp:
    """The daemon's ASGI app. ``port``: its TCP port, if it listens on one
    (pages served from there count as its own Origin). ``poller``: the Pro
    status poller whose bar text and notices ``/v1/events`` relays."""
    app = Starlette(routes=ROUTES)
    app.state.poller = poller
    return Guard(app, token, port)
