"""Desktop organization administration via the authenticated account API.

No local profile grants account authority. Identity comes from the verified
entitlement; every remote read/write also checks live account membership/RBAC.
Only approval-limit blocks are editable here, through the account's scoped
save (PUT /policy with update_scope="permission_grants"). Never a full-policy
PUT: GET redacts fields (budgets, member overrides) that a full PUT replaces.
"""
from urllib.parse import quote

from brindle.pro import auth, credentials, license
from brindle.service.dto import (
    OrgApprovalPolicyDTO, OrgAssignmentDTO, OrgAssignmentRequestDTO,
    OrgPermissionsDTO, OrgPolicyRequestDTO, OrgRoleDTO,
)
from brindle.service.errors import ServiceError


class Account:
    def __init__(self, expected: OrgAssignmentRequestDTO | OrgPolicyRequestDTO | None = None):
        self.store = credentials.default_store()
        self.client = auth.Client((self.store.load() or {}).get("base_url"))
        try:
            self.ent = license.current(store=self.store, client=self.client)
        except license.LicenseError as e:
            raise ServiceError("signed_out", str(e), 401) from e
        if "team" not in self.ent.features or not self.ent.role:
            raise ServiceError("team_required", "Sign in to an organization account.", 403)
        if expected and (expected.org_id != self.ent.org_id or expected.actor_sub != self.ent.sub):
            raise ServiceError("identity_changed", "The account changed. Reload organization settings.", 409)
        self.prefix = f"/orgs/{quote(self.ent.org_id, safe='')}"

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            status, got = auth.authed(self.client, self.store, method, self.prefix + path,
                                      auth.JSONBody(body) if body is not None else None)
        except auth.TransportError as e:
            raise ServiceError(e.code, str(e), 503) from e
        if status != 200:
            error = auth._error(status, got)
            extra = {"version": got["version"]} if type(got.get("version")) is int else {}
            if isinstance(got.get("fields"), list):
                extra["fields"] = [f for f in got["fields"] if isinstance(f, str)][:20]
            raise ServiceError(error.code, str(error), status, **extra)
        if got.get("org_id") != self.ent.org_id:
            raise ServiceError("invalid_account_response", "Account response belongs to another organization.", 502)
        return got

    def roles(self) -> list[OrgRoleDTO]:
        got = self.call("GET", "/roles")
        return [OrgRoleDTO.model_validate(r) for r in got["builtin"] + got["custom"]]

    def policy(self) -> dict:
        got = self.call("GET", "/policy")
        if (got.get("role"), got.get("policy_role")) != (self.ent.role, self.ent.policy_role):
            raise ServiceError("identity_changed", "Your account role changed. Refresh your sign-in before editing.", 409)
        return got

    def policy_reason(self, roles: list[OrgRoleDTO] | None) -> str | None:
        if self.ent.plan != "enterprise":
            return "Approval limits require Enterprise."
        names = {self.ent.role, self.ent.policy_role}
        held = {permission for role in roles or [] if role.name in names for permission in role.permissions}
        # Inventory requires a separate permission. When it is unavailable,
        # do not infer authority from a role name: PUT enforces live control.
        # org_spend is not needed: the scoped save keeps hidden budgets server-side.
        if roles is not None and "control" not in held:
            return "Editing approval limits requires the control permission."
        return None


def policy_view(got: dict) -> dict:
    policy = got["policy"]
    return OrgApprovalPolicyDTO(version=got["version"], permission_grants=policy.get("permission_grants"),
                                roles={name: over.get("permission_grants")
                                       for name, over in (policy.get("roles") or {}).items()}).to_json()


def snapshot() -> dict:
    account = Account()
    got = account.policy()
    roles = None
    members = []
    inventory_error = None
    try:
        roles = account.roles()
        members = account.call("GET", "/members")["members"]
    except ServiceError as e:
        if e.http_status not in (403, 404) and e.http_status < 500:
            raise
        roles = None
        inventory_error = str(e)
    reason = account.policy_reason(roles)
    return OrgPermissionsDTO(
        identity={key: getattr(account.ent, key) for key in ("sub", "org_id", "role", "policy_role")},
        roles=roles or [], members=members, inventory_error=inventory_error, policy=policy_view(got),
        can_edit_policy=reason is None, policy_readonly_reason=reason,
    ).to_json()


def assign(body: dict) -> dict:
    req = OrgAssignmentRequestDTO.model_validate(body)
    account = Account(req)
    # The unified endpoint handles built-ins, custom roles, ceilings and the
    # last owner. Do not emulate it using the legacy policy-role endpoint.
    got = account.call("PUT", f"/members/{quote(req.sub, safe='')}/role-assignment", {"role": req.role})
    return OrgAssignmentDTO.model_validate(got).to_json()


def save_policy(body: dict) -> dict:
    req = OrgPolicyRequestDTO.model_validate(body)
    account = Account(req)
    # Base edits need no role inventory; the policy writer is the authority
    # for control permission. A targeted override still needs a known role.
    roles = account.roles() if req.role is not None else None
    account.policy()  # rechecks the live role identity before writing
    if reason := account.policy_reason(roles):
        raise ServiceError("forbidden", reason, 403)
    if req.role is not None and req.role not in {r.name for r in roles or []}:
        raise ServiceError("unknown_role", "Choose an existing organization role.", 422)
    # The supplied block replaces that block whole (null inherits); omitted
    # blocks and every other policy field stay as stored. The account's CAS
    # on if_version rejects a stale draft.
    grant = req.permission_grants.to_json() if req.permission_grants else None
    scoped = {"update_scope": "permission_grants", "if_version": req.if_version}
    if req.role is None:
        scoped["permission_grants"] = grant
    else:
        scoped["roles"] = {req.role: {"permission_grants": grant}}
    try:
        return policy_view(account.call("PUT", "/policy", scoped))
    except ServiceError as e:
        if e.http_status == 409:
            raise ServiceError("version_conflict", "The policy changed. Reload it and review your draft.",
                               409, **e.extra) from e
        if e.http_status != 422:
            raise
        fields = e.extra.get("fields") or []
        # An account API without the scoped save rejects update_scope as an
        # unknown field. Never fall back to a full-policy PUT.
        if "<unknown>" in fields or "update_scope" in fields:
            raise ServiceError("scoped_save_unsupported", "The account server does not support saving approval "
                               "limits alone yet. Nothing was saved; edit them on the account website.", 422) from e
        raise ServiceError(e.code, f"The account rejected the approval limits ({', '.join(fields) or e.message}).",
                           422, fields=fields) from e
