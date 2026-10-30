"""Per-role team policy: the member's effective policy is what the plugin
enforces, profiles can be restricted, and the cached copy follows the
member's role."""

import pytest

from brindle.pro import auth, team_policy
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_team import (  # noqa: F401 - fixtures/helpers
    ORG, POLICY, assign, login, plugin, run, team, team_claims,
)

GET = f"GET /orgs/{ORG}/policy"
ROLES = {"member": {"allowed_providers": ["claude"], "max_parallel_workers": 2},
         "contractor": {"allowed_profiles": ["developer"], "require_human_review": True}}
MEMBER = {**POLICY, "allowed_profiles": None, "allowed_providers": ["claude"],
          "max_parallel_workers": 2}
CONTRACTOR = {**POLICY, "allowed_profiles": ["developer"]}
GRANT_POLICY = {"allow": True, "kinds": ["bash"], "overridable": ["git-push"], "max_minutes": 5, "max_uses": 2,
                "deny": ["bash prefix terraform apply"]}


def role_policy(team, effective=MEMBER, role="member", policy_role=None, **base):
    """The backend's answer with per-role overrides and the caller's effective policy."""
    team.policy_body = {"org_id": ORG, "version": 3,
                        "policy": {**POLICY, "allowed_profiles": None, **base, "roles": ROLES},
                        "effective": effective, "role": role, "policy_role": policy_role}


# -- the effective policy is the one enforced ------------------------------------------------------


@pytest.mark.parametrize("effective", [
    "strict", {**MEMBER, "max_parallel_workers": 0}, {**MEMBER, "require_human_review": "no"},
])
def test_a_malformed_effective_policy_fails_closed(team, effective):
    role_policy(team, effective=effective)
    assert not plugin(team).check_assign(assign()).allowed


def test_a_copy_cached_for_another_role_is_never_a_fallback(team, monkeypatch):
    from brindle import permission_grants

    broad = {"allow": True, "kinds": ["bash"], "overridable": ["git-push"],
             "max_minutes": 60, "max_uses": 20, "allow_persistent": True, "scopes": ["repo"]}
    narrow = {**broad, "max_minutes": 5, "max_uses": 1, "allow_persistent": False,
              "scopes": ["remote"], "lifetimes": ["session"]}
    role_policy(team, effective={**MEMBER, "permission_grants": narrow}, permission_grants=broad)
    monkeypatch.setattr(permission_grants, "_plugin", lambda repo: plugin(team))
    limits = permission_grants.org_limits(assign().repo_root)
    assert limits.max_minutes == 5 and not limits.allow_persistent
    assert limits.scopes == ("remote",) and limits.lifetimes == ("session",)
    cached = team_policy.load_cached(ORG)
    assert cached.enforced.permission_grants.max_uses == 1
    assert not cached.enforced.permission_grants.allow_persistent
    assert cached.enforced.permission_grants.scopes == ("remote",)
    assert cached.enforced.permission_grants.lifetimes == ("session",)
    # A role change must reject even this previously valid cached policy.
    assert plugin(team).check_assign(assign()).allowed
    login(team, team_claims(policy_role="contractor"))
    team.routes[GET] = [auth.TransportError("down")]
    assert not plugin(team).check_assign(assign()).allowed


@pytest.mark.parametrize("roles", [
    ["member"], {"member": "strict"}, {"member": {"allowed_models": "gpt-5"}},
    {"member": {"permission_grants": {"allow": True}}},
])
def test_malformed_role_overrides_fail_closed(team, roles):
    role_policy(team)
    team.policy_body["policy"]["roles"] = roles
    assert not plugin(team).check_assign(assign()).allowed


# -- offline (air-gap) policy files ----------------------------------------------------------------


@pytest.mark.parametrize("base_grants,block,ok", [
    (True, None, True),
    pytest.param(False, None, True, marks=pytest.mark.variant("role-only grant policy")),
    (None, GRANT_POLICY, True),
    (None, {**GRANT_POLICY, "expires": 1}, False),
    pytest.param(None, {k: v for k, v in GRANT_POLICY.items() if k != "max_uses"}, False,
                 marks=pytest.mark.variant("a block an older server sends, missing a cap: never unbounded")),
    (None, {**GRANT_POLICY, "kinds": ["shell"]}, False),
    (None, {**GRANT_POLICY, "max_minutes": 0}, False),
    (None, {**GRANT_POLICY, "deny": ["bash regex x"]}, False),
    (None, {**GRANT_POLICY, "allow": "yes"}, False),
    (None, {**GRANT_POLICY, "allow_persistent": "yes"}, False),
    (None, {**GRANT_POLICY, "scopes": ["remote"], "lifetimes": ["session"]}, True),
    (None, {**GRANT_POLICY, "scopes": ["unknown"]}, False),
    (None, {**GRANT_POLICY, "destinations": ["https://example.com/*"]}, False),
    pytest.param(None, {**GRANT_POLICY, "destinations": "https://example.com/o/r.git"}, False,
                 marks=pytest.mark.variant("a destination filter must not be interpreted as characters or a substring")),
])
def test_offline_files_apply_role_overrides_by_the_backends_rule(base_grants, block, ok):
    if block is not None:
        body = {"version": 2, "policy": {"permission_grants": block, "roles": {"member": {"max_parallel_workers": 2}}}}
        if not ok:
            with pytest.raises(team_policy.PolicyUnavailable):
                team_policy.parse_policy("org_g1", body)
            return
        parsed = team_policy.parse_policy("org_g1", body)
        assert team_policy.parse_policy("org_g1", parsed.to_json()).permission_grants == parsed.permission_grants
        assert team_policy.with_role_overrides(parsed, "member", None).enforced.permission_grants == parsed.permission_grants
        return
    grants = {"allow": True, "kinds": ["bash"], "overridable": ["git-push"],
              "max_minutes": 60, "max_uses": 20, "deny": ["bash prefix git push protected"]}
    member_grants = {**grants, "max_minutes": 5, "deny": ["bash exact deploy"]}
    contractor_grants = {**grants, "max_uses": 1, "deny": [],
                         "lifetimes": [], "scopes": []}
    roles = {name: {**rules, "permission_grants": override} for name, rules, override in (
        ("member", ROLES["member"], member_grants),
        ("contractor", ROLES["contractor"], contractor_grants))}
    base = team_policy.parse_policy(ORG, {"version": 1, "policy": {
        **POLICY, "allowed_profiles": None, "max_parallel_workers": 8,
        "permission_grants": grants if base_grants else None, "roles": roles}}, 0)
    member = team_policy.with_role_overrides(base, "member", None).enforced
    assert member.allowed_providers == ("claude",) and member.max_parallel_workers == 2
    contractor = team_policy.with_role_overrides(base, "member", "contractor").enforced
    assert contractor.allowed_profiles == ("developer",) and contractor.require_human_review is True
    assert contractor.max_parallel_workers == 2                     # member's override still applies
    assert team_policy.with_role_overrides(base, "owner", None).enforced.max_parallel_workers == 8
    assert member.permission_grants.max_minutes == 5
    assert contractor.permission_grants.max_minutes == 60 and contractor.permission_grants.max_uses == 1
    assert member.permission_grants.deny == tuple(member_grants["deny"])
    assert contractor.permission_grants.deny == ()
    assert contractor.permission_grants.lifetimes == () and contractor.permission_grants.scopes == ()
    restored = team_policy.parse_policy(ORG, base.to_json())
    assert team_policy.with_role_overrides(restored, "member", "contractor").enforced == contractor


# -- brindle account org policy ----------------------------------------------------------------------


def test_org_policy_shows_base_overrides_and_effective(team):
    role_policy(team, effective=CONTRACTOR, policy_role="contractor")
    login(team, team_claims(policy_role="contractor"))
    code, out, _ = run(team, "org", "policy")
    assert code == 0 and "version 3" in out
    base, _, rest = out.partition("Role overrides")
    overrides, _, effective = rest.partition("Your effective policy")
    assert "providers              claude, codex" in base and "profiles               any" in base
    assert "contractor" in overrides and "profiles               developer" in overrides
    assert "member" in overrides and "max parallel workers   2" in overrides
    assert "role member, policy role contractor" in effective
    assert "profiles               developer" in effective
    assert "max parallel workers   4" in effective


# -- brindle account org member policy-role -----------------------------------------------------------

SUB = "user_2"
ROLE_PATH = f"/orgs/{ORG}/members/{SUB}/policy-role"


def policy_role(team):
    seen = []

    def put(form, headers):
        assert set(form) == {"policy_role"}
        seen.append(form["policy_role"])
        return 200, {"org_id": ORG, "sub": SUB, "policy_role": form["policy_role"]}

    team.routes[f"PUT {ROLE_PATH}"] = put
    return seen


def test_set_and_clear_a_policy_role(team):
    seen = policy_role(team)
    code, out, _ = run(team, "org", "member", "policy-role", SUB, "contractor", "--org", ORG)
    assert code == 0 and f"Member {SUB} of {ORG} now has the policy role contractor" in out
    code, out, _ = run(team, "org", "member", "policy-role", SUB, "none")
    assert code == 0 and "no policy role" in out
    assert seen == ["contractor", None]


def test_policy_role_below_enterprise(team):
    team.routes[f"PUT {ROLE_PATH}"] = [(403, {"error": "enterprise_required"})]
    code, _, err = run(team, "org", "member", "policy-role", SUB, "contractor", "--org", ORG)
    assert code == 1 and "enterprise_required" in err and "brindle Enterprise" in err


def test_bad_policy_roles_and_members_are_rejected_before_any_call(team):
    policy_role(team)
    for member, role in ((SUB, "Contractor"), (SUB, "9lives"), (SUB, "a" * 33), ("a b", "ok"),
                         ("x" * 129, "ok")):
        code, _, err = run(team, "org", "member", "policy-role", member, role, "--org", ORG)
        assert code == 1 and "invalid" in err, (member, role)
    assert not [c for c in team.paths() if "policy-role" in c]


def test_a_member_id_is_escaped_in_the_path(team):
    team.routes[f"PUT /orgs/{ORG}/members/a%2Fb%3Fc/policy-role"] = lambda f, h: (
        200, {"org_id": ORG, "sub": "a/b?c", "policy_role": "ops"})
    code, out, _ = run(team, "org", "member", "policy-role", "a/b?c", "ops", "--org", ORG)
    assert code == 0 and "a/b?c" in out


def test_an_unconfirmed_policy_role_is_refused(team):
    team.routes[f"PUT {ROLE_PATH}"] = [(200, {"org_id": ORG, "sub": SUB, "policy_role": "other"})]
    code, _, err = run(team, "org", "member", "policy-role", SUB, "contractor", "--org", ORG)
    assert code == 1 and "didn't confirm" in err
