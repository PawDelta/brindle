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
from brindle import airgap, spawn, visuals
from brindle.config import db_path
from brindle.daemon import routes_read
from brindle.daemon.auth import Guard
from brindle.daemon.events import events
from brindle.daemon.holds import ROUTES as HOLD_ROUTES
from brindle.daemon.launch_env import ROUTES as LAUNCH_ENV_ROUTES
from brindle.daemon.pty_view import pty_view
from brindle.daemon.routes_actions import ROUTES as ACTIONS
from brindle.daemon.routes_org_permissions import ROUTES as ORG_PERMISSION_ROUTES
from brindle.daemon.routes_visuals import ROUTES as VISUAL_ROUTES

API_VERSION = "1.0"
REQUEST_DEADLINE = 50.0   # seconds a read request's processes get (the app waits 60)


async def meta(request: Request) -> JSONResponse:
    """What the app checks before anything else: which engine and API this is."""
    recovery = getattr(request.app.state, "recovery", None)
    return JSONResponse({
        "engine_version": brindle.__version__,
        "api_version": API_VERSION,
        "pid": os.getpid(),
        "db_path": str(db_path()),
        "tmux": shutil.which("tmux") is not None,
        "airgap": airgap.enabled(),
        "pty_view": True,       # WS /v1/pty_view: the Windows app's WSL bridge
        "visuals": visuals.enabled(),   # the visuals capability: the app shows the feature only when on
        "resuming": getattr(request.app.state, "resuming", False),
        "recovered_at": getattr(request.app.state, "recovered_at", None),
        "recovery": recovery.progress() if recovery else None,
    })


async def prioritize_recovery(request: Request) -> JSONResponse:
    """The person has opened a session recovery hasn't reached: it goes next.
    Always 200: if it has begun or none is under way, there's nothing to do."""
    recovery = getattr(request.app.state, "recovery", None)
    root = (await request.json()).get("root")
    if recovery and isinstance(root, str):
        recovery.prioritize(root)
    return JSONResponse({"ok": True})


async def viewed_session(request: Request) -> JSONResponse:
    """The app is showing session ``root``: kept on disk, so the next daemon's
    recovery brings it back first (lifecycle.record_viewed)."""
    from brindle.daemon import lifecycle

    root = (await request.json()).get("root")
    if not isinstance(root, str) or not root:
        return JSONResponse({"error": "root must be a session id"}, status_code=400)
    lifecycle.record_viewed(root)   # on the loop, a few bytes: switches land in the order they were made
    return JSONResponse({"ok": True})


ROUTES = [Route("/v1/meta", meta), Route("/v1/recovery/prioritize", prioritize_recovery, methods=["POST"]),
          Route("/v1/recovery/viewed", viewed_session, methods=["POST"]),
          *routes_read.ROUTES,
          WebSocketRoute("/v1/events", events), WebSocketRoute("/v1/pty_view", pty_view),
          *ACTIONS, *HOLD_ROUTES, *LAUNCH_ENV_ROUTES, *ORG_PERMISSION_ROUTES, *VISUAL_ROUTES]


class Deadlines:
    """ASGI middleware: each HTTP request gets REQUEST_DEADLINE seconds,
    under the app's own 60 second limit, through
    :func:`brindle.spawn.deadline`. The processes its handler starts as reads
    get only the time left, and are stopped when it runs out, when the client
    disconnects, or when the request ends or is cancelled, so no read
    outlives the request that asked for it. An action never starts past the
    deadline and is never stopped once started (brindle.spawn); a job
    (routes_actions.start_job) runs on its own thread, with no deadline."""

    def __init__(self, app: ASGIApp, seconds: float | None = None) -> None:
        self.app = app
        self.seconds = REQUEST_DEADLINE if seconds is None else seconds

    @property
    def state(self):
        """The wrapped app's state (heavy_read_lane, poller), as if unwrapped."""
        return self.app.state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
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
                # Ended or cancelled: a handler thread asyncio.to_thread left
                # running must not keep its reads going until the deadline.
                dl.stop("the request ended")
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)


def build_app(token: str, port: int | None = None, poller=None) -> ASGIApp:
    """The daemon's ASGI app. ``port``: its TCP port, if it listens on one
    (pages served from there count as its own Origin). ``poller``: the Pro
    status poller whose bar text and notices ``/v1/events`` relays."""
    app = Starlette(routes=ROUTES)
    app.state.poller = poller
    app.state.heavy_read_lane = routes_read.HeavyReadLane()
    return Guard(Deadlines(app), token, port)
