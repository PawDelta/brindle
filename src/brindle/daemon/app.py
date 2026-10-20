"""The daemon's routes, all under ``/v1``, behind :class:`brindle.daemon.auth.Guard`."""

from __future__ import annotations

import os
import shutil

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp

import brindle
from brindle import airgap
from brindle.config import db_path
from brindle.daemon import routes_read
from brindle.daemon.auth import Guard

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
    })


def build_app(token: str, port: int | None = None) -> ASGIApp:
    """The daemon's ASGI app. ``port``: its TCP port, if it listens on one
    (pages served from there count as its own Origin)."""
    return Guard(Starlette(routes=[Route("/v1/meta", meta), *routes_read.ROUTES]), token, port)
