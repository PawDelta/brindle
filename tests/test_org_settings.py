"""The desktop organisation editor through the real authenticated routes."""
from copy import deepcopy

import pytest

from brindle.pro import analysis_policy, auth, license
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401
from test_daemon_actions import _send
from test_pro_team import ORG, login, team, team_claims  # noqa: F401


@pytest.fixture
def library(team, monkeypatch):
    monkeypatch.setattr(auth, "UrllibTransport", lambda **kw: team)
    login(team, team_claims(plan="enterprise", role="admin", features=[
        "analysis_docs", "analysis_docs_team", "analysis_docs_enterprise"]))
    team.docs_policy = {"version": 1, "switches": {"prompt_analysis": "enforced_off"},
                        "groups": {"g": {"switches": {"assist": "default_on"}}}, "share_links": "org"}
    team.changes = []

    def save(form, headers):
        version = team.docs_policy["version"]
        team.docs_policy = deepcopy(form)
        team.docs_policy["version"] = version + 1
        team.docs_policy.setdefault("switches", {})
        team.docs_policy.setdefault("share_links", "anyone")
        team.changes.append(deepcopy(form))
        return 200, team.docs_policy

    path = f"/orgs/{ORG}/analysis-docs/policy"
    team.routes[f"GET {path}"] = lambda f, h: (200, team.docs_policy)
    team.routes[f"PUT {path}"] = save
    team.routes["GET /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": {"prompt_analysis": "on"}})
    team.routes["GET /analysis-docs/scopes"] = lambda f, h: (200, {"scopes": []})
    return team


@pytest.mark.parametrize("concurrent", [False, True])
def test_admin_changes_one_setting_and_member_reads_lock(library, concurrent):
    code, state = _send("GET", "/v1/org-settings")
    assert code == 200
    if concurrent:
        path = f"PUT /orgs/{ORG}/analysis-docs/policy"
        accept = library.routes[path]

        def changed(form, headers):
            library.docs_policy["groups"]["g"]["switches"]["assist"] = "enforced_off"
            library.routes[path] = accept
            return 409, {"error": "version_conflict"}

        library.routes[path] = changed
    code, saved = _send("PUT", "/v1/org-settings", {
        "org_id": state["org_id"], "actor_sub": state["actor_sub"],
        "name": "prompt_analysis", "previous": "enforced_off", "value": "enforced_on"})
    assert code == 200
    assert saved["policy"]["switches"]["prompt_analysis"] == "enforced_on"
    assert saved["policy"]["groups"]["g"]["switches"]["assist"] == ("enforced_off" if concurrent else "default_on")
    assert analysis_policy.load_cached(ORG) == saved["policy"]
    login(library, team_claims(plan="enterprise", features=[
        "analysis_docs", "analysis_docs_team", "analysis_docs_enterprise"]))
    license.clear_cache()
    code, personal = _send("GET", "/v1/analysis-docs/switches")
    assert code == 200
    prompt = next(s for s in personal["switches"] if s["name"] == "prompt_analysis")
    assert prompt["locked"] and prompt["on"] and prompt["org"] == "enforced_on"


@pytest.mark.parametrize("case,expected", [("member", 403), ("team", 403), ("identity", 409), ("stale", 409)])
def test_org_edit_refuses_unauthorised_or_outdated_form(library, case, expected):
    code, state = _send("GET", "/v1/org-settings")
    assert code == 200
    if case == "member":
        login(library, team_claims(features=["analysis_docs", "analysis_docs_team"]))
        license.clear_cache()
        assert _send("GET", "/v1/org-settings")[0] == 403
    elif case == "team":
        library.docs_policy.update(switches={}, share_links="anyone")
        login(library, team_claims(role="admin", features=["analysis_docs", "analysis_docs_team"]))
        license.clear_cache()
        code, team_state = _send("GET", "/v1/org-settings")
        assert code == 200 and not team_state["can_edit_org"] and team_state["can_edit_groups"]
    body = {"org_id": ORG, "actor_sub": state["actor_sub"], "name": "prompt_analysis",
            "previous": "enforced_off", "value": "enforced_on"}
    if case == "identity":
        body["org_id"] = "org_other"
    elif case == "stale":
        library.docs_policy["switches"]["prompt_analysis"] = "default_on"
    assert _send("PUT", "/v1/org-settings", body)[0] == expected
    assert library.changes == []
    if case == "team":
        code, saved = _send("PUT", "/v1/org-settings", {**body, "group": "g", "name": "assist", "previous": "default_on"})
        assert code == 200 and saved["policy"]["groups"]["g"]["switches"]["assist"] == "enforced_on"
        assert not saved["policy"]["switches"]
        assert not library.changes[0].get("switches")
