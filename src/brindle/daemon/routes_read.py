"""The daemon's read-only routes. Each opens its own ``DB`` (a request runs on a
worker thread) and answers with ``service.dto`` shapes; engine errors become
``ServiceError`` JSON. Secrets leave only as key names and their last 4 characters."""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from brindle import (
    agents,
    config,
    cost,
    doctor,
    git,
    kb,
    keystore,
    knowledge,
    profiles,
    sessions,
    tmux,
    view,
    workspaces,
)
from brindle.db import DB
from brindle.service import inbox
from brindle.service.dto import (
    AccountDTO,
    AgentDTO,
    DecisionDTO,
    GoalDTO,
    ProfileDTO,
    SessionDTO,
    WorkspaceDTO,
)
from brindle.service.errors import ServiceError, from_exception

log = logging.getLogger(__name__)

COST_DAYS = 30


def _json(fn: Callable[[Request], Any]) -> Callable[[Request], JSONResponse]:
    """A sync handler (Starlette runs it on a thread) whose return value is the
    JSON body and whose exceptions become the service's error JSON."""
    @wraps(fn)
    def handler(request: Request) -> JSONResponse:
        try:
            return JSONResponse(fn(request))
        except Exception as e:  # noqa: BLE001 - every engine error maps to a ServiceError
            err = from_exception(e)
            if err.code == "internal":
                log.exception("brindle: daemon route %s failed", request.url.path)
            return JSONResponse(err.to_json(), status_code=err.http_status)
    return handler


def _repo(request: Request) -> str | None:
    return request.query_params.get("repo") or None


def _need_repo(request: Request) -> str:
    repo = _repo(request)
    if repo is None:
        raise ServiceError("invalid", "repo is required", 400)
    return repo


def _last4(value: str) -> str:
    return value[-4:] if len(value) > 8 else ""


@_json
def repos(request: Request) -> Any:
    db = DB()
    roots = sorted({ws.repo_root for ws in db.find_workspaces(None)})
    return [{"repo_root": r, "name": r.rstrip("/").rsplit("/", 1)[-1]} for r in roots]


@_json
def list_sessions(request: Request) -> Any:
    db, repo = DB(), _need_repo(request)
    live = [SessionDTO.from_agent(s.root, s.goal).to_json() for s in sessions.live(db, repo)]
    paused = [SessionDTO.from_agent(s.root, sessions.goal_line(db, s.root)).to_json()
              for s in sessions.paused(db, repo)]
    return live + paused


@_json
def snapshot(request: Request) -> Any:
    return [WorkspaceDTO.model_validate(ws).to_json() for ws in view.snapshot(DB(), _repo(request))]


@_json
def goal(request: Request) -> Any:
    db = DB()
    entry = view.autopilot_entry(db, _need_repo(request), panes=tmux.list_panes(),
                                 root_id=request.query_params.get("root"))
    ap = entry and db.get_autopilot(entry["root_id"])
    if not ap:
        return None
    return GoalDTO.from_autopilot(ap, db.milestones(ap.root_id)).to_json()


@_json
def inbox_(request: Request) -> Any:
    return [i.to_json() for i in inbox.items(DB(), _repo(request))]


@_json
def agent(request: Request) -> Any:
    db = DB()
    try:
        a = agents.get(db, request.path_params["id"])
    except agents.AgentError as e:
        raise ServiceError("not_found", str(e), 404) from e
    return AgentDTO.model_validate(view.agent_entry(db, a, detail=True, panes=tmux.list_panes())).to_json()


@_json
def diff(request: Request) -> Any:
    db = DB()
    ws = db.get_workspace(request.path_params["id"])
    if ws is None:
        raise ServiceError("not_found", "no such workspace", 404)
    base = workspaces.require_base(ws)
    return {"workspace_id": ws.id, "base": base, "diff": git.diff(ws.path, base),
            "stat": git.diff(ws.path, base, stat=True)}


@_json
def decisions(request: Request) -> Any:
    return [DecisionDTO.from_row(d).to_json()
            for d in knowledge.decisions(DB(), knowledge.repo_key(_need_repo(request)))]


@_json
def kb_sources(request: Request) -> Any:
    keep = ("id", "kind", "scope", "mode", "visibility", "credential_name", "retention_days",
            "added_at", "docs", "quarantined")
    return [{k: s[k] for k in keep if k in s}
            for s in kb.sources(DB(), knowledge.repo_key(_need_repo(request)))]


@_json
def list_profiles(request: Request) -> Any:
    return [ProfileDTO.from_profile(p).to_json() for p in profiles.list_profiles(_repo(request))]


@_json
def get_config(request: Request) -> Any:
    repo = _repo(request)
    settings = {"user": config.user_settings()}
    if repo:
        settings["repo"] = dataclasses.asdict(config.load_repo_config(repo))
    keys = []
    for name in keystore.known_names(repo):
        value = keystore.get_key(name)
        keys.append({"name": name, "set": value is not None, "last4": _last4(value or "")})
    return {**settings, "keys": keys}


@_json
def get_doctor(request: Request) -> Any:
    return [dataclasses.asdict(c) for c in doctor.checks(_repo(request))]


@_json
def account(request: Request) -> Any:
    from brindle.pro import license

    try:
        ent = license.current(refresh=False)
    except license.LicenseError:
        return AccountDTO(signed_in=False).to_json()
    return AccountDTO(signed_in=True, plan=ent.plan).to_json()


@_json
def get_cost(request: Request) -> Any:
    cost.require_entitled()
    days = int(request.query_params.get("days") or COST_DAYS)
    s = cost.summary(DB(), _repo(request), now=time.time(), days=days)

    def bucket(b: cost.Bucket) -> dict:
        return {"dollars": b.dollars, "tokens": b.tokens, "unknown": b.unknown, "free": b.free}

    return {"days": s.days, "total": bucket(s.total),
            "by_model": {m: bucket(b) for m, b in s.by_model.items()}}


ROUTES = [
    Route("/v1/repos", repos),
    Route("/v1/sessions", list_sessions),
    Route("/v1/snapshot", snapshot),
    Route("/v1/goal", goal),
    Route("/v1/inbox", inbox_),
    Route("/v1/agents/{id}", agent),
    Route("/v1/workspaces/{id:path}/diff", diff),
    Route("/v1/decisions", decisions),
    Route("/v1/kb/sources", kb_sources),
    Route("/v1/profiles", list_profiles),
    Route("/v1/config", get_config),
    Route("/v1/doctor", get_doctor),
    Route("/v1/account", account),
    Route("/v1/cost", get_cost),
]
