"""Serialized desktop requests against signed identities and a fake account API."""
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
HIDDEN = ("member_overrides", "spend_rules")  # redacted from GET without org_spend
SCOPED = {"update_scope", "if_version", "permission_grants", "roles"}


@pytest.fixture
def account(team, monkeypatch):
    monkeypatch.setattr(auth, "UrllibTransport", lambda: team)
    login(team, team_claims(plan="enterprise", role="admin", policy_role="security"))
    # The signed-in role holds control but not org_spend.
    team.roles = {"org_id": ORG, "permissions": ["roles", "control"], "builtin": [
        {"name": "admin", "builtin": True, "permissions": ["roles"], "holders": []},
        {"name": "member", "builtin": True, "permissions": [], "holders": ["target"]}], "custom": [
        {"name": "security", "builtin": False, "permissions": ["control"], "holders": ["user_1"], "team_weight": 0.5}]}
    team.members = {"org_id": ORG, "members": [
        {"sub": "user_1", "email": "admin@example.test", "role": "admin", "policy_role": "security"},
        {"sub": "target", "email": "person@example.test", "role": "member"}]}
    team.policy_body.update(policy={"permission_grants": GRANT, "roles": {
        "security": {"allowed_models": ["model"], "permission_grants": {**GRANT, "deny": []}}},
        "protected_paths": ["infra/**"], "member_overrides": {"hidden": {"paused": True}},
        "spend_rules": {"preserve": True}})
    team.visibility = {"org_spend": False}

    def view():
        stored = team.policy_body
        return {"org_id": ORG, "version": stored["version"], "role": "admin", "policy_role": "security",
                "visibility": dict(team.visibility),
                "policy": {k: deepcopy(v) for k, v in stored["policy"].items() if k not in HIDDEN}}

    def save(form, headers):
        # account-api 793e6ec7 _put_permission_grants, against stored policy.
        if form.get("update_scope") != "permission_grants" or set(form) - SCOPED or any(
                set(over) != {"permission_grants"} for over in (form.get("roles") or {}).values()):
            return 422, {"error": "invalid_request", "fields": []}
        if form["if_version"] != team.policy_body["version"]:
            return 409, {"error": "version_conflict", "version": team.policy_body["version"]}
        policy = deepcopy(team.policy_body["policy"])
        if "permission_grants" in form:
            policy["permission_grants"] = form["permission_grants"]
        for name, over in (form.get("roles") or {}).items():
            if over["permission_grants"] is not None:
                policy.setdefault("roles", {}).setdefault(name, {})["permission_grants"] = over["permission_grants"]
            else:
                policy.get("roles", {}).get(name, {}).pop("permission_grants", None)
        team.policy_body = {"org_id": ORG, "version": form["if_version"] + 1, "policy": policy}
        return 200, view()

    def assign(form, headers):
        if form["role"] == "owner":
            return 403, {"error": "forbidden", "error_description": "role ceiling"}
        person = team.members["members"][1]
        person.update(role="member", policy_role=form["role"] if form["role"] == "security" else None)
        return 200, {"org_id": ORG, "sub": "target", "role": form["role"]}

    team.routes[f"GET /orgs/{ORG}/policy"] = lambda f, h: (200, view())
    team.routes[f"GET /orgs/{ORG}/roles"] = lambda f, h: (200, deepcopy(team.roles))
    team.routes[f"GET /orgs/{ORG}/members"] = lambda f, h: (200, deepcopy(team.members))
    team.routes[f"PUT /orgs/{ORG}/policy"] = save
    team.routes[f"PUT /orgs/{ORG}/members/target/role-assignment"] = assign
    return team


def _assignment(account):
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


def _scoped_edits(account):
    # Control without org_spend edits a role block; hidden budgets survive.
    body = {"org_id": ORG, "actor_sub": "user_1", "if_version": 3, "role": "security",
            "permission_grants": {**GRANT, "deny": [], "max_uses": 5, "controls": [
                {"group": "dev/install", "decision": "deny", "reason": "platform review"}, {"rule": "bash glob *prod*",
                                                                                         "decision": "ask"}]}}
    code, result = _send("PUT", PREFIX + "/policy", body)
    assert code == 200 and result["version"] == 4 and result["roles"]["security"]["max_uses"] == 5
    policy = account.policy_body["policy"]
    # Each control as given (no null fields), the org's name left out while unset.
    sent = {**body["permission_grants"], "controls": [
        {"group": "dev/install", "decision": "deny", "reason": "platform review"},
        {"rule": "bash glob *prod*", "decision": "ask", "reason": ""}]}
    assert policy["roles"]["security"] == {"allowed_models": ["model"], "permission_grants": sent}
    assert policy["protected_paths"] == ["infra/**"] and policy["member_overrides"]["hidden"]["paused"]
    assert policy["spend_rules"] == {"preserve": True} and policy["permission_grants"] == GRANT
    from brindle.pro.team_policy import parse_policy, with_role_overrides

    effective = with_role_overrides(parse_policy(ORG, account.policy_body), "admin", "security").effective
    assert effective.permission_grants.deny == () and effective.permission_grants.max_uses == 5
    assert [(c.decision, c.group, c.rule) for c in effective.permission_grants.controls] == [
        ("deny", "dev/install", None), ("ask", None, "bash glob *prod*")]
    body.update(if_version=4, permission_grants=None)
    assert _send("PUT", PREFIX + "/policy", body)[0] == 200
    assert account.policy_body["policy"]["roles"]["security"] == {"allowed_models": ["model"]}
    body.update(if_version=5, role=None, permission_grants={**GRANT, "allow": False})
    assert _send("PUT", PREFIX + "/policy", body)[0] == 200
    assert not account.policy_body["policy"]["permission_grants"]["allow"]
    assert account.policy_body["policy"]["spend_rules"] == {"preserve": True}
    puts = [call[1] for call in account.calls if call[0].startswith("PUT ")]
    assert puts == [
        {"update_scope": "permission_grants", "if_version": 3,
         "roles": {"security": {"permission_grants": sent}}},
        {"update_scope": "permission_grants", "if_version": 4, "roles": {"security": {"permission_grants": None}}},
        {"update_scope": "permission_grants", "if_version": 5, "permission_grants": {**GRANT, "allow": False}}]


def _no_inventory(account):
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


FLOWS = {"assignment": _assignment, "scoped_edits": _scoped_edits, "no_inventory": _no_inventory}
REFUSED = {"stale": "version_conflict", "rejected": "invalid_request", "unsupported": "scoped_save_unsupported"}


@pytest.mark.parametrize("case,expected", [
    ("assignment", 200), ("scoped_edits", 200), ("no_inventory", 200),
    ("stale", 409), ("rejected", 422), ("unsupported", 422),
    pytest.param("token", 401, marks=pytest.mark.variant("tokenless attempt to write account policy")),
    pytest.param("agent", 403, marks=pytest.mark.variant("agent attempts the new person-only bridge")),
    ("signed_out", 401), ("changed_role", 409), ("roles_forbidden", 403), ("offline", 503),
    ("identity", 409), ("team", 403), ("control", 403), ("unknown_role", 422), ("version", 400)])
def test_org_permissions_bridge(account, monkeypatch, case, expected):
    if case in FLOWS:
        return FLOWS[case](account)
    body = {"org_id": ORG, "actor_sub": "user_1", "if_version": 3, "role": None, "permission_grants": GRANT}
    options = {}
    if case == "stale":
        account.policy_body["version"] = 4
    elif case == "rejected":
        account.routes[f"PUT /orgs/{ORG}/policy"] = lambda f, h: (
            422, {"error": "invalid_request", "fields": ["permission_grants"]})
    elif case == "unsupported":
        # An account API before 793e6ec7 forbids the unknown update_scope field.
        account.routes[f"PUT /orgs/{ORG}/policy"] = lambda f, h: (
            422, {"error": "invalid_request", "fields": ["<unknown>"]})
    elif case == "token":
        options["token"] = None
    elif case == "agent":
        options["peer_pid"] = 123
        monkeypatch.setattr(peer, "agent_caller", lambda pid: "worker")
    elif case == "signed_out":
        account.store.save({})
    elif case == "identity":
        body["org_id"] = "org_other"
    elif case == "changed_role":
        account.routes[f"GET /orgs/{ORG}/policy"] = lambda f, h: (
            200, {**account.policy_body, "role": "member", "policy_role": "security"})
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
    elif case == "unknown_role":
        body["role"] = "invented"
    elif case == "version":
        body["if_version"] = True
    before = deepcopy(account.policy_body["policy"])
    code, error = _send("PUT", PREFIX + "/policy", body, **options)
    assert code == expected
    puts = [call[1] for call in account.calls if call[0].startswith("PUT ")]
    if case in REFUSED:
        # One scoped attempt, never a full-policy fallback.
        assert puts == [{"update_scope": "permission_grants", "if_version": 3, "permission_grants": GRANT}]
        assert error["error"]["code"] == REFUSED[case]
        assert error["error"].get("version") == (4 if case == "stale" else None)
    else:
        assert not puts
    assert account.policy_body["policy"] == before
