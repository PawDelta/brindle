"""The person's permission editor, behind the daemon's existing Guard."""

from starlette.requests import Request
from starlette.routing import Route

from brindle import permission_editor
from brindle.daemon.routes_actions import _endpoint, _get
from brindle.daemon.routes_read import _json, _need_repo
from brindle.db import DB


@_json
def permission_editor_snapshot(db: DB, request: Request) -> dict:
    return permission_editor.snapshot(_need_repo(request))


def permission_editor_review(db: DB, params: dict, body: dict) -> dict:
    return permission_editor.review(body)


def permission_editor_save(db: DB, params: dict, body: dict) -> dict:
    from brindle import antigravity

    result = permission_editor.save(body)
    antigravity.resync_permissions()
    return result


def permission_editor_verify(db: DB, params: dict, body: dict) -> dict:
    return permission_editor.verify(_get(body, "tool", required=True))


PREFIX = "/v1/permission-editor"
ROUTES = [Route(PREFIX, permission_editor_snapshot),
          Route(PREFIX + "/review", _endpoint(permission_editor_review), methods=["POST"]),
          Route(PREFIX + "/save", _endpoint(permission_editor_save), methods=["POST"]),
          Route(PREFIX + "/verify", _endpoint(permission_editor_verify), methods=["POST"])]
