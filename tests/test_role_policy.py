"""Per-role team policy: the member's effective policy is what the plugin
enforces, profiles can be restricted, and the cached copy follows the
member's role."""
import json

import pytest

from copse.pro import auth, team_policy
from copse.pro._files import private_dir
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_team import (  # noqa: F401 - fixtures/helpers
    ORG, POLICY, assign, login, merge, plugin, run, team, team_claims,
)

GET = f"GET /orgs/{ORG}/policy"
ROLES = {"member": {"allowed_providers": ["claude"], "max_parallel_workers": 2},
         "contractor": {"allowed_profiles": ["developer"], "require_human_review": True}}
MEMBER = {**POLICY, "allowed_profiles": None, "allowed_providers": ["claude"],
          "max_parallel_workers": 2}
CONTRACTOR = {**POLICY, "allowed_profiles": ["developer"]}


def role_policy(team, effective=MEMBER, role="member", policy_role=None, **base):
    """The backend's answer with per-role overrides and the caller's effective policy."""
    team.policy_body = {"org_id": ORG, "version": 3,
                        "policy": {**POLICY, "allowed_profiles": None, **base, "roles": ROLES},
                        "effective": effective, "role": role, "policy_role": policy_role}


def fetches(team):
    return team.paths().count(GET)


# -- the effective policy is the one enforced ------------------------------------------------------


def test_the_effective_policy_wins_over_the_base(team):
    role_policy(team)
    p = plugin(team)
    assert p.check_assign(assign()).allowed
    d = p.check_assign(assign(provider="codex", model="gpt-5"))      # the base allows codex
    assert not d.allowed and "'codex'" in d.reason and "only claude" in d.reason
    d = p.check_assign(assign(running=2))                            # the base cap is 4
    assert not d.allowed and "at most 2 parallel worker" in d.reason


def test_the_effective_policy_can_be_looser_than_the_base(team):
    role_policy(team, effective={**MEMBER, "require_human_review": False}, role="owner")
    login(team, team_claims(role="owner"))
    assert plugin(team).check_merge(merge("pipeline")).allowed


def test_the_base_policy_applies_without_an_effective_one(team):
    role_policy(team)
    del team.policy_body["effective"]
    p = plugin(team)
    assert p.check_assign(assign(provider="codex", model="gpt-5")).allowed
    assert p.check_assign(assign(running=3)).allowed


@pytest.mark.parametrize("effective", [
    "strict", {**MEMBER, "allowed_providers": "claude"}, {**MEMBER, "max_parallel_workers": 0},
    {**MEMBER, "require_human_review": "no"}, {**MEMBER, "allowed_profiles": [1]},
])
def test_a_malformed_effective_policy_fails_closed(team, effective):
    role_policy(team, effective=effective)
    assert not plugin(team).check_assign(assign()).allowed


@pytest.mark.parametrize("roles", [
    ["member"], {"Member": {}}, {"member": "strict"}, {"member": {"allowed_models": "gpt-5"}},
    {"a" * 33: {}},
])
def test_malformed_role_overrides_fail_closed(team, roles):
    role_policy(team)
    team.policy_body["policy"]["roles"] = roles
    assert not plugin(team).check_assign(assign()).allowed


# -- allowed_profiles ------------------------------------------------------------------------------


def test_a_profile_outside_the_list_is_denied(team):
    role_policy(team, effective=CONTRACTOR, policy_role="contractor")
    login(team, team_claims(policy_role="contractor"))
    p = plugin(team)
    assert p.check_assign(assign()).allowed
    d = p.check_assign(assign(profile="architect"))
    assert not d.allowed and "'architect'" in d.reason and "only developer" in d.reason
    assert not p.check_assign(assign(profile=None)).allowed


def test_base_allowed_profiles_apply_too(team):
    team.policy_body["policy"]["allowed_profiles"] = []
    d = plugin(team).check_assign(assign())
    assert not d.allowed and "no profiles" in d.reason


def test_null_allowed_profiles_allow_any(team):
    role_policy(team)
    assert plugin(team).check_assign(assign(profile="anything")).allowed


# -- the cache follows the member's role ---------------------------------------------------------


def test_the_cache_keeps_the_effective_policy_and_its_key(team):
    role_policy(team)
    plugin(team).check_assign(assign())
    assert not plugin(team).check_assign(assign(provider="codex", model="gpt-5")).allowed
    assert fetches(team) == 1
    saved = json.loads((private_dir() / f"policy-{ORG}.json").read_text())
    assert saved["effective"]["allowed_providers"] == ["claude"]
    assert saved["policy"]["roles"]["member"] == ROLES["member"]
    assert saved["cached_for"] == ["member", None]


def test_a_new_policy_role_refetches_at_the_same_version(team):
    role_policy(team)
    assert plugin(team).check_assign(assign(profile="architect")).allowed
    role_policy(team, effective=CONTRACTOR, policy_role="contractor")      # version still 3
    login(team, team_claims(policy_role="contractor"))
    assert not plugin(team).check_assign(assign(profile="architect")).allowed
    assert fetches(team) == 2
    assert plugin(team).check_assign(assign()).allowed
    assert fetches(team) == 2                                              # cached under the new key


def test_a_new_role_refetches_at_the_same_version(team):
    role_policy(team)
    assert not plugin(team).check_assign(assign(running=2)).allowed
    role_policy(team, effective={**POLICY, "allowed_profiles": None}, role="admin")
    login(team, team_claims(role="admin"))
    assert plugin(team).check_assign(assign(running=2)).allowed
    assert fetches(team) == 2


def test_dropping_the_policy_role_refetches(team):
    role_policy(team, effective=CONTRACTOR, policy_role="contractor")
    login(team, team_claims(policy_role="contractor"))
    assert not plugin(team).check_assign(assign(profile="architect")).allowed
    role_policy(team)
    login(team, team_claims())                    # the claim is absent when no role is set
    assert plugin(team).check_assign(assign(profile="architect")).allowed
    assert fetches(team) == 2


def test_a_cache_from_before_roles_is_refetched_once(team):
    team_policy.save_cached(team_policy.parse_policy(ORG, {"version": 3, "policy": POLICY}))
    role_policy(team)
    assert not plugin(team).check_assign(assign(provider="codex", model="gpt-5")).allowed
    plugin(team).check_assign(assign())
    assert fetches(team) == 1


def test_a_copy_cached_for_another_role_is_never_a_fallback(team):
    # After a role change, a failed refetch must not fall back to the old
    # role's (possibly looser) rules: it fails closed.
    role_policy(team)
    assert plugin(team).check_assign(assign()).allowed
    login(team, team_claims(policy_role="contractor"))
    team.routes[GET] = [auth.TransportError("down")]
    assert not plugin(team).check_assign(assign()).allowed


def test_a_stale_copy_for_the_same_role_is_still_a_fallback(team):
    role_policy(team)
    assert plugin(team).check_assign(assign()).allowed
    login(team, team_claims(policy_version=4))      # newer version, same role
    team.routes[GET] = [auth.TransportError("down")]
    assert plugin(team).check_assign(assign()).allowed


# -- offline (air-gap) policy files ----------------------------------------------------------------


def test_offline_files_apply_role_overrides_by_the_backends_rule():
    base = team_policy.parse_policy(ORG, {"version": 1, "policy": {
        **POLICY, "allowed_profiles": None, "max_parallel_workers": 8, "roles": ROLES}}, 0)
    member = team_policy.with_role_overrides(base, "member", None).enforced
    assert member.allowed_providers == ("claude",) and member.max_parallel_workers == 2
    contractor = team_policy.with_role_overrides(base, "member", "contractor").enforced
    assert contractor.allowed_profiles == ("developer",) and contractor.require_human_review is True
    assert contractor.max_parallel_workers == 2                     # member's override still applies
    assert team_policy.with_role_overrides(base, "owner", None).enforced.max_parallel_workers == 8


def test_an_offline_effective_policy_is_kept_as_is():
    p = team_policy.parse_policy(ORG, {"version": 1, "policy": {**POLICY, "roles": ROLES},
                                       "effective": MEMBER}, 0)
    assert team_policy.with_role_overrides(p, "member", "contractor") is p


# -- copse account org policy ----------------------------------------------------------------------


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


def test_org_policy_without_overrides_or_an_effective_policy(team):
    code, out, _ = run(team, "org", "policy")
    assert code == 0 and "Role overrides: none" in out
    assert "Your policy (role member): the org policy above." in out


def test_org_policy_caches_under_the_members_key(team):
    role_policy(team)
    assert run(team, "org", "policy")[0] == 0
    plugin(team).check_assign(assign())
    assert fetches(team) == 1


# -- copse account org member policy-role -----------------------------------------------------------

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
    assert code == 1 and "enterprise_required" in err and "copse Enterprise" in err


def test_policy_role_for_a_non_admin(team):
    team.routes[f"PUT {ROLE_PATH}"] = [(403, {"error": "forbidden"})]
    code, _, err = run(team, "org", "member", "policy-role", SUB, "contractor", "--org", ORG)
    assert code == 1 and "forbidden" in err


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


def test_bad_arguments_print_usage(team):
    for bad in (("org", "member"), ("org", "member", "policy-role", SUB),
                ("org", "member", "role", SUB, "admin"),
                ("org", "member", "policy-role", SUB, "ops", "x"),
                ("org", "member", "policy-role", SUB, "ops", "--admin")):
        code, _, err = run(team, *bad)
        assert code == 2 and "policy-role" in err, bad


def test_unset_role_override_fields_arrive_as_null_and_mean_not_set():
    # The backend's real answer: every override field present, unset ones null.
    body = {"org_id": ORG, "version": 1, "policy": {**POLICY, "allowed_profiles": None, "roles": {
        "member": {"allowed_providers": None, "allowed_models": None, "allowed_profiles": ["developer"],
                   "require_human_review": None, "max_parallel_workers": None}}}}
    p = team_policy.parse_policy(ORG, body, 0)
    assert p.roles == {"member": {"allowed_profiles": ("developer",)}} or \
        p.roles == {"member": {"allowed_profiles": ["developer"]}}
    eff = team_policy.with_role_overrides(p, "member", None).enforced
    assert eff.allowed_profiles == ("developer",) and eff.require_human_review == POLICY["require_human_review"]
