"""``brindle account org learning-share``: the opt-in to pooling learning within a company."""


from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_team import ORG, run, team  # noqa: F401 - fixtures/helpers

PATH = f"/orgs/{ORG}/learning-sharing"


def sharing(team, enabled=False, updated_at=None):
    state = {"enabled": enabled, "updated_at": updated_at}

    def get(form, headers):
        return 200, {"org_id": ORG, **state}

    def put(form, headers):
        assert set(form) == {"enabled"} and isinstance(form["enabled"], bool)
        state.update(enabled=form["enabled"], updated_at=1_700_000_000)
        return 200, {"org_id": ORG, **state}

    team.routes[f"GET {PATH}"] = get
    team.routes[f"PUT {PATH}"] = put
    return state


def test_defaults_to_the_current_org(team):
    sharing(team, enabled=True, updated_at=1_700_000_000)
    code, out, _ = run(team, "org", "learning-share")
    assert code == 0 and ORG in out and "ON" in out and "changed" in out


def test_a_non_admin_gets_the_servers_refusal(team):
    sharing(team)
    team.routes[f"PUT {PATH}"] = [(403, {"error": "forbidden"})]
    code, _, err = run(team, "org", "learning-share", "on", "--org", ORG)
    assert code == 1 and "forbidden" in err


def test_a_malformed_answer_is_refused(team):
    team.routes[f"GET {PATH}"] = [(200, {"org_id": ORG})]
    code, _, err = run(team, "org", "learning-share", "--org", ORG)
    assert code == 1 and "no learning-sharing state" in err


def test_bare_account_shows_the_state(team):
    sharing(team, enabled=True)
    code, out, _ = run(team)
    assert code == 0 and "Learning sharing: ON" in out
    sharing(team, enabled=False)
    code, out, _ = run(team)
    assert "Learning sharing: off" in out
