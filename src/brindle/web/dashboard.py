"""The page itself and the snapshot it polls: workspaces and agents (what
the sidebar shows) and the session's goal with each milestone's last check."""

from __future__ import annotations

import time
from importlib import resources

from brindle import tmux, view
from brindle.db import DB
from brindle.web.server import Request, Response, json_response, route

# The tail of a milestone check's output the page shows.
OUTPUT_TAIL = 4000
_STATIC = {"index.html": "text/html; charset=utf-8",
           "app.js": "text/javascript; charset=utf-8"}


def snapshot(db: DB, repo_root: str | None, root_id: str | None,
             panes: dict[str, bool] | None = None) -> dict:
    if panes is None:
        panes = tmux.list_panes()
    pilot = view.autopilot_entry(db, repo_root, panes=panes, root_id=root_id)
    if pilot:
        last = {m.position: m for m in db.milestones(pilot["root_id"])}
        for m in pilot["milestones"]:
            r = last.get(m["position"])
            m.update(checked_at=r and r.checked_at, checked_sha=r and r.checked_sha,
                     output=(r.output or "")[-OUTPUT_TAIL:] if r else "")
    return {"now": time.time(), "repo_root": repo_root, "root_id": root_id,
            "workspaces": view.snapshot(db, repo_root, panes=panes), "autopilot": pilot}


def _static(name: str) -> Response:
    body = resources.files("brindle.web").joinpath("static", name).read_bytes()
    return Response(200, body, _STATIC[name])


@route("GET", "/")
def index(req: Request) -> Response:
    return _static("index.html")


@route("GET", "/app.js")
def app_js(req: Request) -> Response:
    return _static("app.js")


@route("GET", "/api/snapshot")
def api_snapshot(req: Request) -> Response:
    return json_response(snapshot(req.db, req.app.repo_root, req.app.root_id))
