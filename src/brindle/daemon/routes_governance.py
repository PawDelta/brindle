"""Organization governance for the desktop app, behind the daemon Guard.

GET /v1/governance: who manages this device and whether its signed policy
verifies, and where it came from (the managed device token, the account, the
last saved copy or an offline policy export), or the problem and what to do.
POST /v1/governance/offline-policy: an offline policy export from the account
(POST /orgs/{id}/policy/offline), written to ~/.brindle/policy.json.
POST /v1/governance/device-file: a managed device file (managed.json) from the
account (POST /orgs/{id}/managed-devices) for an Enterprise admin to save.
GET/PUT /v1/governance/plan: plan approvals: Auto / Always / Never, the
person's default and a project's override (config ``plan``).

The org context, managed device file and policy cache are imported when
asked for: an engine without them answers status "unavailable"."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
from pathlib import Path
from types import ModuleType

from starlette.requests import Request
from starlette.routing import Route

from brindle import config
from brindle.daemon.routes_actions import _endpoint, _get
from brindle.daemon.routes_read import _json, _repo
from brindle.db import DB
from brindle.service.dto import DeviceFileDTO, GovernanceStatusDTO, OfflinePolicyExportDTO, PlanSettingDTO
from brindle.service.errors import ServiceError

MAX_DAYS = 90
POLICY_FILE = "policy.json"
PLANS = ("auto", "always", "never")

# What to do about a blocked policy, by where it came from.
_FIX = {
    "snapshot": "Connect to the internet so brindle can fetch a fresh signed policy, "
                "or ask your organization's admin for an offline policy export.",
    "offline": "Ask your organization's admin for a new offline policy export.",
}
_FIX_OTHER = "Reconnect to your organization's account, or ask its admin."
_UNVERIFIED = ("Your organization's signed policy hasn't been verified on this device.",
               "Connect to the internet so brindle can fetch it, or ask your organization's admin "
               "for an offline policy export.")


def _backend() -> tuple[ModuleType, ModuleType, ModuleType] | None:
    try:
        return tuple(importlib.import_module(f"brindle.pro.{name}")
                     for name in ("org_context", "managed", "policy_cache"))
    except ImportError:
        return None


def _enterprise_admin() -> bool:
    from brindle.pro import auth, license

    try:
        ent = license.current()
    except (license.LicenseError, auth.AuthError):
        return False
    return ent.plan == "enterprise" and ent.role in ("owner", "admin")


def governance_status() -> GovernanceStatusDTO:
    backend = _backend()
    if backend is None:
        return GovernanceStatusDTO(status="unavailable", problem="This engine can't check organization policy yet.",
                                   action="Update brindle.")
    org_context, managed_mod, policy_cache = backend
    admin = _enterprise_admin()
    managed = managed_mod.load()
    if isinstance(managed, managed_mod.Invalid):
        return GovernanceStatusDTO(status="problem", problem=f"This device's managed device file can't be used: {managed.reason}",
                                   action="Ask your organization's admin for a new device file.", enterprise_admin=admin)
    ctx = org_context.OrgContext.resolve()
    source = ctx.source()
    org_id = ctx.org_id
    if source is None and managed is None:
        return GovernanceStatusDTO(status="unmanaged", enterprise_admin=admin)
    found = {"org_id": org_id, "org_name": ctx.org_name, "source": source,
             "enterprise_admin": admin}
    if blocked := ctx.blocked_reason():
        return GovernanceStatusDTO(status="problem", problem=blocked, action=_FIX.get(source, _FIX_OTHER), **found)
    if org_id is None or not ctx.verified:
        return GovernanceStatusDTO(status="problem", problem=_UNVERIFIED[0], action=_UNVERIFIED[1], **found)
    return GovernanceStatusDTO(status="managed", verified=True, **found)


@_json
def status(db: DB, request: Request) -> dict:
    return governance_status().to_json()


def _write_json(path: Path, data: dict) -> None:
    """Replace ``path`` atomically through a temporary file of its own beside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _org_post(path: str, form: dict, signed: str, *, verify=None) -> dict:
    """POST to this org's account ``path`` (org_permissions.Account: the signed-in
    org member, checked as for every org admin request). These responses carry
    no top-level org_id, so the ``signed`` document in them must name ours."""
    from brindle.org_permissions import Account
    from brindle.pro import auth

    account = Account()
    try:
        status, got = auth.authed(account.client, account.store, "POST", account.prefix + path, auth.JSONBody(form))
    except auth.TransportError as e:
        raise ServiceError(e.code, str(e), 503) from e
    if status != 200:
        error = auth._error(status, got)
        raise ServiceError(error.code, str(error), status)
    doc = verify(got) if verify else got.get(signed)
    if not isinstance(doc, dict) or doc.get("org_id") != account.ent.org_id:
        raise ServiceError("invalid_account_response", "Account response belongs to another organization.", 502)
    return got


def offline_policy(db: DB, params: dict, body: dict) -> dict:
    days = _get(body, "days", int, required=True)
    if not 1 <= days <= MAX_DAYS:
        raise ServiceError("invalid", f"days must be 1 to {MAX_DAYS}")
    got = _org_post("/policy/offline", {"days": days}, "body")
    if not isinstance(got.get("token"), str):
        raise ServiceError("invalid_account_response", "The account's offline policy export is incomplete.", 502)
    path = config.brindle_home() / POLICY_FILE
    _write_json(path, {"token": got["token"], "body": got["body"]})
    return OfflinePolicyExportDTO(path=str(path), days=days).to_json()


def device_file(db: DB, params: dict, body: dict) -> dict:
    from brindle.pro import managed, signed, team_policy

    def verify(got):
        try:
            device = managed.verify_file(json.dumps(got).encode())
        except (managed.ManagedError, signed.SignedError, team_policy.PolicyUnavailable) as e:
            raise ServiceError("invalid_account_response", f"The account's device file did not verify: {e}", 502) from e
        return {"org_id": device.org_id}

    name = (_get(body, "name", required=True) or "").strip()
    if not name or len(name) > 100:
        raise ServiceError("invalid", "name must be 1 to 100 characters")
    form = {"name": name, "allow_airgap": _get(body, "allow_airgap", bool, default=False)}
    if (sink := _get(body, "audit_sink", (str, dict))) is not None:
        form["audit_sink"] = sink
    got = _org_post("/managed-devices", form, "managed", verify=verify)
    if not isinstance(got.get("device_id"), str):
        raise ServiceError("invalid_account_response", "The account's device file is incomplete.", 502)
    document = {k: got[k] for k in ("managed", "policy_body", "device_id")}
    return DeviceFileDTO(filename="managed.json", device_id=got["device_id"], managed=document).to_json()


def _project_plan(repo: str) -> dict:
    """The plan ``repo``'s own files set, by the first file with a plan or an
    old plan_first (local over shared), whatever the person's default is;
    which file that is; and the shared file's, which a cleared local override
    reads through to."""
    layers = config.plan_layers(repo)
    source = next((s for s in ("local", "repo") if layers[s]), None)
    return {"project": layers[source] if source else None,
            "project_source": {"local": "local", "repo": "shared"}.get(source), "shared": layers["repo"]}


def _user_plan() -> str:
    mine = config.user_settings()
    if mine.get("plan") in PLANS:
        return mine["plan"]
    if "plan_first" in mine:
        return "always" if mine["plan_first"] else "never"
    return "auto"


def plan_setting(repo: str | None) -> dict:
    if repo is None:
        return PlanSettingDTO(user=_user_plan()).to_json()
    return PlanSettingDTO(user=_user_plan(), effective=config.load_repo_config(repo).plan, **_project_plan(repo)).to_json()


@_json
def get_plan(db: DB, request: Request) -> dict:
    return plan_setting(_repo(request))


def put_plan(db: DB, params: dict, body: dict) -> dict:
    """``scope`` "user" sets the person's default; "repo" this project's
    override in its .brindle/config.local.json, cleared with plan null."""
    scope = _get(body, "scope", default="user")
    plan = _get(body, "plan")
    if plan is not None and plan not in PLANS:
        raise ServiceError("invalid", "plan must be auto, always or never")
    if scope == "user":
        if plan is None:
            raise ServiceError("invalid", "plan is required")
        config.set_user("plan", plan)
        return plan_setting(None)
    if scope != "repo":
        raise ServiceError("invalid", "scope must be user or repo")
    repo = _get(body, "repo", required=True)
    if plan is None:
        for key in ("plan", "plan_first"):
            config.unset_local(repo, key)
    else:
        config.set_local(repo, "plan", plan)
    return plan_setting(repo)


PREFIX = "/v1/governance"
ROUTES = [Route(PREFIX, status),
          Route(PREFIX + "/offline-policy", _endpoint(offline_policy), methods=["POST"]),
          Route(PREFIX + "/device-file", _endpoint(device_file), methods=["POST"]),
          Route(PREFIX + "/plan", get_plan),
          Route(PREFIX + "/plan", _endpoint(put_plan), methods=["PUT"])]
