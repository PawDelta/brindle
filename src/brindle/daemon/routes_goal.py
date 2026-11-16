"""What routing aims for (brindle.routing_goal): the Agents screen's triangle.

``GET /v1/routing/goal?repo=&session=`` answers ``{weights, scope, locked,
preview}``: the goal in force for ``repo`` (in session ``session``, a root
agent id, when given), the level it comes from ("default", "org",
"personal", "repo" or "session"), whether the org locks it, and the
triangle's numbers. With no goal set anywhere ``weights`` is Balanced and
``scope`` "default": routing then runs as it did before goals.

``PUT /v1/routing/goal`` takes ``{repo, scope, weights}`` (and ``session``
for scope "session"): scope "personal" (~/.brindle/config.json), "repo"
(the repo's shared .brindle/config.json) or "session"; ``weights`` null
clears that level. Refused (403) while the org locks the goal or its policy
can't be read. Answers as GET does.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.routing import Route

from brindle import config, routing_goal
from brindle.daemon.routes_actions import _endpoint, _get
from brindle.daemon.routes_read import _json, _need_repo
from brindle.db import DB
from brindle.service.errors import ServiceError

ROLE_KINDS = ("light", "medium", "heavy", "advanced", "reviewer", "supervisor")


def _roles(repo: str) -> list[dict]:
    """What runs each role today. ``profile`` is what the goal would run:
    today's pick until hosted preview estimates exist (routing goals §4: no
    lever moves on evidence brindle doesn't have)."""
    cfg = config.load_repo_config(repo)
    today = {t: (cfg.routing.get(t) or [None])[0] for t in config.WEIGHTS}
    today["reviewer"] = cfg.review_profile or cfg.reviewer
    today["supervisor"] = cfg.supervisor_profile or "supervisor"
    return [{"kind": k, "profile": today.get(k), "previous": today.get(k)} for k in ROLE_KINDS]


def goal_view(db: DB, repo: str, session: str | None) -> dict:
    org, error = routing_goal.org_goal(repo)
    got = routing_goal.resolve(repo, db=db, root_id=session, org=org, org_known=True)
    weights = got.weights or dict(routing_goal.PRESETS["balanced"])
    return {"weights": weights, "scope": got.scope, "locked": got.locked or error is not None,
            "preview": routing_goal.preview(weights, _roles(repo))}


@_json
def get_goal(db: DB, request: Request) -> Any:
    return goal_view(db, _need_repo(request), request.query_params.get("session") or None)


def put_goal(db: DB, params: dict, body: dict) -> Any:
    repo = _get(body, "repo", required=True)
    scope = _get(body, "scope", required=True)
    scope = "personal" if scope == "user" else scope
    if scope not in routing_goal.SCOPES:
        raise ServiceError("invalid", f"scope must be one of {', '.join(routing_goal.SCOPES)}")
    if "weights" not in body:
        raise ServiceError("invalid", "weights is required (null clears this level)")
    raw = body["weights"]
    try:
        weights = None if raw is None else routing_goal.normalize(raw)
    except routing_goal.GoalError as e:
        raise ServiceError("invalid", str(e)) from None
    org, error = routing_goal.org_goal(repo)
    if error:
        raise ServiceError("forbidden", f"the routing goal can't be changed now: {error}", 403)
    if org is not None and org.locked:
        raise ServiceError("forbidden", "your org's policy locks the routing goal; "
                                        "only an org admin can change it", 403)
    session = None
    if scope == "session":
        session = _get(body, "session", required=True)
        if db.get_agent(session) is None:
            raise ServiceError("not_found", f"no session {session}", 404)
        db.set_session_routing_goal(session, weights)
    elif scope == "repo":
        if weights is None:
            config.unset_repo(repo, routing_goal.SETTING)
        else:
            config.set_repo(repo, routing_goal.SETTING, weights)
    elif weights is None:
        config.forget_user(routing_goal.SETTING)
    else:
        config.set_user(routing_goal.SETTING, weights)
    return goal_view(db, repo, session or _get(body, "session"))


PATH = "/v1/routing/goal"
ROUTES = [Route(PATH, get_goal), Route(PATH, _endpoint(put_goal), methods=["PUT"])]
