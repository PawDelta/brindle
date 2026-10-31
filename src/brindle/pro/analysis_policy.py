"""Cached enterprise/group analysis-doc switches and their effective values."""
from __future__ import annotations

import json
import os
import tempfile

from brindle.pro import auth, license
from brindle.pro._files import private_dir, read_private

SWITCHES = ("store", "share", "assist", "prompt_analysis", "import_upload")
STATES = ("off", "default_on", "enforced_on", "enforced_off")
PLAN_DEFAULT_ON = frozenset({"store", "share", "assist", "import_upload"})   # on for paid plans; prompt_analysis is opt-in
MAX_CACHE = 1024 * 1024


def cache_path(org: str):
    if not auth.ORG_ID_RE.fullmatch(org):
        raise auth.AuthError("invalid org id", code="bad_request")
    return private_dir() / f"analysis-policy-{org}.json"


def _policy(body: dict) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get("switches"), dict) \
            or not isinstance(body.get("groups"), dict):
        raise auth.AuthError("backend returned no analysis-doc policy", code="bad_response")
    for switches in [body["switches"], *(g.get("switches") if isinstance(g, dict) else None
                                       for g in body["groups"].values())]:
        if not isinstance(switches, dict) or any(v not in STATES for v in switches.values()):
            raise auth.AuthError("malformed analysis-doc switches", code="bad_response")
    return body


def save_cached(org: str, policy: dict) -> None:
    path = cache_path(org)
    data = json.dumps(_policy(policy)).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_cached(org: str) -> dict:
    raw = read_private(cache_path(org), MAX_CACHE)
    return _policy(json.loads(raw)) if raw is not None else {"switches": {}, "groups": {}}


def request(client, store, method: str, path: str, body: dict | None = None) -> dict:
    status, got = auth.authed(client, store, method, path,
                              auth.JSONBody(body) if body is not None else None)
    if status != 200:
        raise auth._error(status, got)
    return got


def fetch(org: str, client, store) -> dict:
    cache_path(org)  # validate before sending
    policy = _policy(request(client, store, "GET", f"/orgs/{org}/analysis-docs/policy"))
    save_cached(org, policy)
    return policy


def update(org: str, client, store, policy: dict) -> dict:
    cache_path(org)
    got = _policy(request(client, store, "PUT", f"/orgs/{org}/analysis-docs/policy", policy))
    save_cached(org, got)
    return got


def resolve_switch(name: str, user: dict | None = None, *, group: str | None = None,
                   policy: dict | None = None, org: str | None = None,
                   plan: str | None = None, store=None) -> bool:
    """Resolve enforced enterprise, enforced team, user, team/org defaults, plan.

    No group id means no team policy. Resolution uses only the selected org's
    cached policy and never performs a network request.
    """
    if org is None or plan is None:
        try:
            ent = license.current(refresh=False, store=store)
        except license.LicenseError:
            ent = None
        org = org if org is not None else (ent.org_id if ent else None)
        plan = plan if plan is not None else (ent.plan if ent else "free")
    if policy is None:
        policy = load_cached(org) if org is not None else {"switches": {}, "groups": {}}
    enterprise = policy.get("switches", {}).get(name)
    team = policy.get("groups", {}).get(group, {}).get("switches", {}).get(name) if group else None
    for value in (enterprise, team):
        if value in ("enforced_on", "enforced_off"):
            return value == "enforced_on"
    choice = (user or {}).get(name)
    if choice in ("on", "off"):
        return choice == "on"
    for value in (team, enterprise):
        if value in ("default_on", "off"):
            return value == "default_on"
    return name in PLAN_DEFAULT_ON and plan in ("pro", "team", "enterprise")
