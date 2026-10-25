"""``brindle daemon schema``: the v1 API as a JSON document for the TypeScript client.

It lists every route of :mod:`brindle.daemon.app` and the JSON Schema of each
DTO in :mod:`brindle.service.dto`. The committed copy at ``api-schema.json``
must match :func:`render`; a test fails when the two drift apart."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from starlette.routing import Route

from brindle.daemon.app import API_VERSION, ROUTES
from brindle.service import dto

SCHEMA_PATH = Path(__file__).with_name("api-schema.json")


def _routes() -> list[dict[str, str]]:
    out = []
    for route in ROUTES:
        methods = sorted(getattr(route, "methods", None) or ["GET"]) if isinstance(route, Route) else ["WS"]
        out.extend({"method": m, "path": route.path} for m in methods if m != "HEAD")
    return sorted(out, key=lambda r: (r["path"], r["method"]))


def _models() -> dict[str, Any]:
    found = {}
    for name, obj in vars(dto).items():
        if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj.__module__ == dto.__name__ \
                and obj is not dto._DTO:
            model = obj.model_json_schema(mode="serialization")
            # Recursive DTOs are emitted as a root reference to their own
            # definition. Expose the object at the root too, so consumers
            # lifting definitions don't replace it with a self-only alias.
            if model.get("$ref") == f"#/$defs/{name}":
                model = {**model, **model["$defs"][name]}
                del model["$ref"]
            found[name] = model
    return dict(sorted(found.items()))


def build() -> dict[str, Any]:
    return {"api_version": API_VERSION, "routes": _routes(), "models": _models()}


def render() -> str:
    """The exact text of ``api-schema.json``."""
    return json.dumps(build(), indent=2, sort_keys=True) + "\n"


def write(path: Path = SCHEMA_PATH) -> Path:
    path.write_text(render())
    return path
