"""Serialized desktop requests against signed identities and fake account responses."""
from copy import deepcopy

import pytest

from brindle.daemon import peer
from brindle.pro import auth
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401
from test_daemon_actions import _send
from test_pro_team import ORG, login, team, team_claims  # noqa: F401

PREFIX = "/v1/org-permissions"
GRANT = {"allow": True, "kinds": ["bash"], "overridable": [], "max_minutes": 60,
         "max_uses": 20, "deny": ["bash prefix rm "], "scopes": ["branch", "remote", "repo", "all"],
         "lifetimes": ["session", "time", "forever"], "allow_persistent": True}


@pytest.fixture
def account(team, monkeypatch):
    monkeypatch.setattr(auth, "UrllibTransport", lambda: team)
    login(team, team_claims(plan="enterprise", role="admin", policy_role="security"))
    team.roles = {"org_id": ORG, "permissions": ["roles", "control", "org_spend"], "builtin": [
        {"name": "admin", "builtin": True, "permissions": ["roles", "org_spend"], "holders": []},
        {"name": "member", "builtin": True, "permissions": [], "holders": ["target"]}], "custom": [
        {"name": "security", "builtin": False, "permissions": ["control"], "holders": ["user_1"], "team_weight": 0.5}]}
    team.members = {"org_id": ORG, "members": [
        {"sub": "user_1", "email": "admin@example.test", "role": "admin", "policy_role": "security"},
        {"sub": "target", "email": "person@example.test", "role": "member"}]}
    team.policy_body.update(policy={"permission_grants": GRANT, "roles": {
        "security": {"allowed_models": ["model"], "permission_grants": {**GRANT, "deny": []}}},
        "protected_paths": ["infra/**"], "member_overrides": {"hidden": {"paused": True}},
        "spend_rules": {"preserve": True}}, role="admin", policy_role="security", visibility={"org_spend": True})
    team.routes[f"GET /orgs/{ORG}/policy"] = lambda f, h: (200, {
        "role": "admin", "policy_role": "security", "visibility": {"org_spend": True}, **deepcopy(team.policy_body)})
    team.routes[f"GET /orgs/{ORG}/roles"] = lambda f, h: (200, deepcopy(team.roles))
    team.routes[f"GET /orgs/{ORG}/members"] = lambda f, h: (200, deepcopy(team.members))

    def save(form, headers):
        if form["if_version"] != team.policy_body["version"]:
            return 409, {"error": "version_conflict", "version": team.policy_body["version"]}
        preserved = {k: v for k, v in team.policy_body["policy"].items()
                     if k in ("spend_rules", "spend_notify", "budget_layers")}
        if preserved.keys() & form.keys():
            return 422, {"error": "invalid_request"}  # not fields of account PolicyUpdate
        team.policy_body = {"org_id": ORG, "version": form["if_version"] + 1,
                            "policy": {**preserved, **{k: v for k, v in form.items() if k != "if_version"}}}
        return 200, deepcopy(team.policy_body)

    def assign(form, headers):
        if form["role"] == "owner":
            return 403, {"error": "forbidden", "error_description": "role ceiling"}
        person = team.members["members"][1]
        person.update(role="member", policy_role=form["role"] if form["role"] == "security" else None)
        return 200, {"org_id": ORG, "sub": "target", "role": form["role"]}

    team.routes[f"PUT /orgs/{ORG}/policy"] = save
    team.routes[f"PUT /orgs/{ORG}/members/target/role-assignment"] = assign
    return team


def test_inventory_and_unified_assignment_use_account_identity(account):
    code, state = _send("GET", PREFIX)
    assert code == 200
    assert state["identity"] == {"sub": "user_1", "org_id": ORG, "role": "admin", "policy_role": "security"}
    assert [r["name"] for r in state["roles"]] == ["admin", "member", "security"]
    assert state["can_edit_policy"] and state["members"][1]["sub"] == "target"
    body = {"org_id": ORG, "actor_sub": "user_1", "sub": "target", "role": "security"}
    assert _send("PUT", PREFIX + "/role-assignment", body)[0] == 200
    assert _send("GET", PREFIX)[1]["members"][1]["policy_role"] == "security"
    assert _send("PUT", PREFIX + "/role-assignment", {**body, "role": "member"})[0] == 200
    assert _send("GET", PREFIX)[1]["members"][1]["policy_role"] is None
    code, error = _send("PUT", PREFIX + "/role-assignment", {**body, "role": "owner"})
    assert code == 403 and error["error"]["code"] == "forbidden"
    account.routes[f"PUT /orgs/{ORG}/members/target/role-assignment"] = lambda f, h: (409, {"error": "last_owner"})
    code, error = _send("PUT", PREFIX + "/role-assignment", body)
    assert code == 409 and error["error"]["code"] == "last_owner"
    assert all(call[2]["Authorization"].startswith("Bearer ") for call in account.calls)


def test_versioned_base_and_role_edits_preserve_other_policy(account):
    body = {"org_id": ORG, "actor_sub": "user_1", "if_version": 3, "role": "security",
            "permission_grants": {**GRANT, "deny": [], "max_uses": 5}}
    code, result = _send("PUT", PREFIX + "/policy", body)
    assert code == 200 and result["version"] == 4
    policy = account.policy_body["policy"]
    assert policy["roles"]["security"] == {"allowed_models": ["model"], "permission_grants": body["permission_grants"]}
    assert policy["protected_paths"] == ["infra/**"] and policy["member_overrides"]["hidden"]["paused"]
    assert policy["spend_rules"] == {"preserve": True}
    assert policy["permission_grants"]["deny"] == GRANT["deny"]
    from brindle.pro.team_policy import parse_policy, with_role_overrides

    effective = with_role_overrides(parse_policy(ORG, account.policy_body), "admin", "security").effective
    assert effective.permission_grants.deny == () and effective.permission_grants.max_uses == 5
    assert effective.protected_paths == ("infra/**",)
    code, error = _send("PUT", PREFIX + "/policy", body)
    assert code == 409 and error["error"]["version"] == 4
    assert account.policy_body["policy"] == policy
    body.update(if_version=4, permission_grants=None)
    assert _send("PUT", PREFIX + "/policy", body)[0] == 200
    assert account.policy_body["policy"]["roles"]["security"] == {"allowed_models": ["model"]}
    body.update(if_version=5, role=None, permission_grants={**GRANT, "allow": False})
    assert _send("PUT", PREFIX + "/policy", body)[0] == 200
    assert not account.policy_body["policy"]["permission_grants"]["allow"]
    # A race after the bridge's read is still rejected by the account's CAS.
    account.routes[f"PUT /orgs/{ORG}/policy"] = lambda f, h: (409, {"error": "version_conflict", "version": 7})
    code, error = _send("PUT", PREFIX + "/policy", {**body, "if_version": 6})
    assert code == 409 and error["error"]["version"] == 7


def test_base_approval_policy_survives_unavailable_role_inventory(account):
    account.routes[f"GET /orgs/{ORG}/roles"] = lambda f, h: (403, {"error": "forbidden"})
    code, state = _send("GET", PREFIX)
    assert code == 200 and state["can_edit_policy"]
    assert state["inventory_error"] and state["roles"] == [] and state["members"] == []
    body = {"org_id": ORG, "actor_sub": "user_1", "if_version": 3, "role": None,
            "permission_grants": {**GRANT, "max_uses": 7}}
    assert _send("PUT", PREFIX + "/policy", body)[0] == 200
    assert account.policy_body["policy"]["permission_grants"]["max_uses"] == 7
    assert account.policy_body["policy"]["member_overrides"]["hidden"]["paused"]
    account.routes[f"PUT /orgs/{ORG}/policy"] = lambda f, h: (403, {"error": "forbidden"})
    assert _send("PUT", PREFIX + "/policy", {**body, "if_version": 4})[0] == 403
    account.policy_body["visibility"] = {"org_spend": False}
    assert not _send("GET", PREFIX)[1]["can_edit_policy"]
    assert _send("PUT", PREFIX + "/policy", {**body, "if_version": 4})[0] == 403


@pytest.mark.parametrize("case,expected", [
    pytest.param("token", 401, marks=pytest.mark.variant("tokenless attempt to write account policy")),
    pytest.param("agent", 403, marks=pytest.mark.variant("agent attempts the new person-only bridge")),
    ("signed_out", 401), ("changed_role", 409), ("roles_forbidden", 403), ("offline", 503),
    ("identity", 409), ("team", 403), ("control", 403), ("redacted", 403), ("hidden", 403), ("unknown_role", 422),
    ("version", 400)])
def test_policy_authorization_and_validation_fail_without_writes(account, monkeypatch, case, expected):
    body = {"org_id": ORG, "actor_sub": "user_1", "if_version": 3, "role": None, "permission_grants": GRANT}
    options = {}
    if case == "token":
        options["token"] = None
    elif case == "agent":
        options["peer_pid"] = 123
        monkeypatch.setattr(peer, "agent_caller", lambda pid: "worker")
    elif case == "signed_out":
        account.store.save({})
    elif case == "identity":
        body["org_id"] = "org_other"
    elif case == "changed_role":
        account.policy_body.update(role="member", policy_role="security")
    elif case == "roles_forbidden":
        body["role"] = "security"
        account.routes[f"GET /orgs/{ORG}/roles"] = lambda f, h: (403, {"error": "forbidden"})
    elif case == "offline":
        body["role"] = "security"
        account.routes[f"GET /orgs/{ORG}/roles"] = [auth.TransportError("offline")]
    elif case == "team":
        login(account, team_claims(role="admin", policy_role="security"))
    elif case == "control":
        body["role"] = "security"
        account.roles["custom"][0]["permissions"] = []
    elif case == "redacted":
        body["role"] = "security"
        account.roles["builtin"][0]["permissions"] = ["roles"]
    elif case == "hidden":
        account.policy_body["visibility"]["org_spend"] = False
    elif case == "unknown_role":
        body["role"] = "invented"
    elif case == "version":
        body["if_version"] = True
    assert _send("PUT", PREFIX + "/policy", body, **options)[0] == expected
    assert not any(path.startswith("PUT ") for path in account.paths())
