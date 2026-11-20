"""The app's theme, as the app tells the engine.

``PUT /v1/app/theme`` takes ``{"theme": "light" | "dark"}`` (the app's
effective theme, sent at start and on every change) and answers
``{"theme": ...}``. The engine remembers it and keeps brindle's own Claude
Code config folder's theme to match (``providers.set_app_theme``).
"""

from __future__ import annotations

from typing import Any

from starlette.routing import Route

from brindle import providers
from brindle.daemon.routes_actions import _endpoint
from brindle.db import DB
from brindle.service.errors import ServiceError


def put_theme(db: DB, params: dict, body: dict) -> Any:
    theme = body.get("theme")
    if theme not in providers.CLAUDE_THEMES:
        raise ServiceError("invalid", "theme must be light or dark")
    providers.set_app_theme(theme)
    return {"theme": theme}


ROUTES = [Route("/v1/app/theme", _endpoint(put_theme), methods=["PUT"])]
