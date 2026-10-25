"""Desktop organization administration via the authenticated account API.

No local profile grants account authority. Identity comes from the verified
entitlement; every remote read/write also checks live account membership/RBAC.
Only the approval-limit block is editable here, never arbitrary policy fields.
"""
from copy import deepcopy
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

    def policy_reason(self, roles: list[OrgRoleDTO], policy: dict) -> str | None:
        if self.ent.plan != "enterprise":
            return "Approval limits require Enterprise."
        names = {self.ent.role, self.ent.policy_role}
        held = {permission for role in roles if role.name in names for permission in role.permissions}
        if "control" not in held:
            return "Editing approval limits requires the control permission."
        if "org_spend" not in held or policy.get("visibility", {}).get("org_spend") is not True:
            # GET hides member overrides without this permission. The existing
            # PUT replaces them; only the account website can restore hidden
            # fields from storage. Never send a redacted full-policy document.
            return ("The account API hides budget settings from this role. Edit approval limits on the "
                    "account website to preserve them; desktop policy saves require org_spend access.")
        return None


def policy_view(got: dict) -> dict:
    policy = got["policy"]
    return OrgApprovalPolicyDTO(version=got["version"], permission_grants=policy.get("permission_grants"),
                                roles={name: over.get("permission_grants")
                                       for name, over in (policy.get("roles") or {}).items()}).to_json()


def snapshot() -> dict:
    account = Account()
    roles = account.roles()
    members = account.call("GET", "/members")
    got = account.policy()
    reason = account.policy_reason(roles, got)
    return OrgPermissionsDTO(
        identity={key: getattr(account.ent, key) for key in ("sub", "org_id", "role", "policy_role")},
        roles=roles, members=members["members"], policy=policy_view(got),
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
    roles = account.roles()
    got = account.policy()
    if reason := account.policy_reason(roles, got):
        raise ServiceError("forbidden", reason, 403)
    if req.role is not None and req.role not in {r.name for r in roles}:
        raise ServiceError("unknown_role", "Choose an existing organization role.", 422)
    if req.if_version != got["version"]:
        raise ServiceError("version_conflict", "The policy changed. Reload it and review your draft.",
                           409, version=got["version"])
    policy = deepcopy(got["policy"])
    # Account-owned documents are not accepted by PolicyUpdate. Its writer
    # preserves these from storage (account-api orgs.put_policy).
    for key in ("spend_rules", "spend_notify", "budget_layers"):
        policy.pop(key, None)
    grant = req.permission_grants.to_json() if req.permission_grants else None
    if req.role is None:
        policy["permission_grants"] = grant
    else:
        overrides = policy.setdefault("roles", {})
        role = overrides.setdefault(req.role, {})
        if grant is None:
            role.pop("permission_grants", None)
            if not role:
                overrides.pop(req.role)
        else:
            role["permission_grants"] = grant
    # The account CAS also catches changes between the read and the write.
    return policy_view(account.call("PUT", "/policy", {**policy, "if_version": req.if_version}))
