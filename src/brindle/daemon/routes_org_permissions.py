"""Person-only organization bridge, behind the daemon Guard (no hook routes)."""
from starlette.requests import Request
from starlette.routing import Route

from brindle import org_permissions
from brindle.daemon.routes_actions import _endpoint
from brindle.daemon.routes_read import _json
from brindle.db import DB


@_json
def inventory(db: DB, request: Request) -> dict:
    return org_permissions.snapshot()


def assign(db: DB, params: dict, body: dict) -> dict:
    return org_permissions.assign(body)


def policy(db: DB, params: dict, body: dict) -> dict:
    return org_permissions.save_policy(body)


PREFIX = "/v1/org-permissions"
ROUTES = [Route(PREFIX, inventory),
          Route(PREFIX + "/role-assignment", _endpoint(assign), methods=["PUT"]),
          Route(PREFIX + "/policy", _endpoint(policy), methods=["PUT"])]
