"""The daemon's routes, all under ``/v1``, behind :class:`brindle.daemon.auth.Guard`."""

from __future__ import annotations

import asyncio
import os
import shutil

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import brindle
from brindle import airgap, spawn
from brindle.config import db_path
from brindle.daemon import routes_read
from brindle.daemon.auth import Guard
from brindle.daemon.events import events
from brindle.daemon.holds import ROUTES as HOLD_ROUTES
from brindle.daemon.routes_actions import ROUTES as ACTIONS
from brindle.daemon.routes_org_permissions import ROUTES as ORG_PERMISSION_ROUTES

API_VERSION = "1.0"
REQUEST_DEADLINE = 50.0   # seconds a read request's processes get (the app waits 60)


async def meta(request: Request) -> JSONResponse:
    """What the app checks before anything else: which engine and API this is."""
    return JSONResponse({
        "engine_version": brindle.__version__,
        "api_version": API_VERSION,
        "pid": os.getpid(),
        "db_path": str(db_path()),
        "tmux": shutil.which("tmux") is not None,
        "airgap": airgap.enabled(),
    })


ROUTES = [Route("/v1/meta", meta), *routes_read.ROUTES,
          WebSocketRoute("/v1/events", events), *ACTIONS, *HOLD_ROUTES, *ORG_PERMISSION_ROUTES]


class Deadlines:
    """ASGI middleware: each read request (GET, HEAD) gets REQUEST_DEADLINE
    seconds, under the app's own 60 second limit, through
    :func:`brindle.spawn.deadline`. The processes its handler starts as reads
    get only the time left, and are stopped when it runs out or when the
    client disconnects, so no read outlives the request that asked for it.
    Other methods are actions and get no deadline: an action is never
    stopped once it has started (brindle.spawn)."""

    def __init__(self, app: ASGIApp, seconds: float | None = None) -> None:
        self.app = app
        self.seconds = REQUEST_DEADLINE if seconds is None else seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        with spawn.deadline(self.seconds) as dl:
            inbox: asyncio.Queue[Message] = asyncio.Queue()

            async def listen() -> None:
                # The only reader of ``receive``: the route reads ``inbox``,
                # and a disconnect (the client gave up, or the response is
                # done) stops whatever reads are still running. Once the body
                # is in, a server's receive waits for the disconnect; one that
                # answers again at once can't report it, so stop listening.
                complete = False
                while True:
                    message = await receive()
                    await inbox.put(message)
                    if message["type"] == "http.disconnect":
                        dl.stop("the client went away")
                        return
                    if complete:
                        return
                    complete = not message.get("more_body", False)

            listener = asyncio.create_task(listen())
            try:
                await self.app(scope, inbox.get, send)
            finally:
                listener.cancel()


def build_app(token: str, port: int | None = None, poller=None) -> ASGIApp:
    """The daemon's ASGI app. ``port``: its TCP port, if it listens on one
    (pages served from there count as its own Origin). ``poller``: the Pro
    status poller whose bar text and notices ``/v1/events`` relays."""
    app = Starlette(routes=ROUTES)
    app.state.poller = poller
    return Guard(Deadlines(app), token, port)
