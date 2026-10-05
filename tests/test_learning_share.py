"""``frith account org learning-share``: the opt-in to pooling learning within a company."""

import json

from frith import airgap
from frith.pro import auth
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


def test_off_by_default_and_says_so(team):
    sharing(team)
    code, out, _ = run(team, "org", "learning-share", "--org", ORG)
    assert code == 0 and "off" in out and "Off by default" in out
    assert "task text, paths" in out and "own hashes" in out and "own data still wins" in out
    assert "built-in profile names" in out and "fades out over time" in out
    flat = " ".join(out.split())
    assert "only with the other orgs in the same company, never with other companies" in flat
    assert "cross-org" not in out
    for number in ("10", "5 orgs", "8 tasks", "60"):
        assert number not in out
    assert [c[0] for c in team.calls if "learning-sharing" in c[0]] == [f"GET {PATH}"]


def test_turn_on_then_off(team):
    state = sharing(team)
    code, out, _ = run(team, "org", "learning-share", "on", "--org", ORG)
    assert code == 0 and "ON" in out and state["enabled"] is True
    code, out, _ = run(team, "org", "learning-share", "off", "--org", ORG)
    assert code == 0 and "ON" not in out and state["enabled"] is False
    puts = [c for c in team.calls if c[0] == f"PUT {PATH}"]
    assert [c[1] for c in puts] == [{"enabled": True}, {"enabled": False}]
    assert all(c[2]["Authorization"].startswith("Bearer ") for c in puts)


def test_defaults_to_the_current_org(team):
    sharing(team, enabled=True, updated_at=1_700_000_000)
    code, out, _ = run(team, "org", "learning-share")
    assert code == 0 and ORG in out and "ON" in out and "changed" in out


def test_a_non_admin_gets_the_servers_refusal(team):
    sharing(team)
    team.routes[f"PUT {PATH}"] = [(403, {"error": "forbidden"})]
    code, _, err = run(team, "org", "learning-share", "on", "--org", ORG)
    assert code == 1 and "forbidden" in err


def test_bad_arguments_print_usage(team):
    for bad in (("org", "learning-share", "maybe"), ("org", "learning-share", "on", "off"),
                ("org", "learning-share", "--admin")):
        code, _, err = run(team, *bad)
        assert code == 2 and "learning-share" in err


def test_bad_org_id_is_rejected_before_any_call(team):
    code, _, err = run(team, "org", "learning-share", "--org", "../x")
    assert code == 1 and "invalid org id" in err
    assert not [c for c in team.calls if "learning-sharing" in c[0]]


def test_a_malformed_answer_is_refused(team):
    team.routes[f"GET {PATH}"] = [(200, {"org_id": ORG})]
    code, _, err = run(team, "org", "learning-share", "--org", ORG)
    assert code == 1 and "no learning-sharing state" in err


def test_refused_in_airgap_mode(team, monkeypatch):
    sharing(team)
    monkeypatch.setenv(airgap.ENV, "1")
    airgap.reset()
    try:
        for args in (("on",), ()):
            code, _, err = run(team, "org", "learning-share", *args, "--org", ORG)
            assert code == 1 and "air-gap" in err
        assert not [c for c in team.calls if "learning-sharing" in c[0]]
    finally:
        monkeypatch.delenv(airgap.ENV)
        airgap.reset()


def test_bare_account_shows_the_state(team):
    sharing(team, enabled=True)
    code, out, _ = run(team)
    assert code == 0 and "Learning sharing: ON" in out
    sharing(team, enabled=False)
    code, out, _ = run(team)
    assert "Learning sharing: off" in out


def test_bare_account_survives_an_unreachable_backend(team):
    team.routes[f"GET {PATH}"] = [auth.TransportError("down")]
    code, out, _ = run(team)
    assert code == 0 and "Learning sharing" not in out


def test_client_calls_return_a_sanitized_state(team):
    team.routes[f"GET {PATH}"] = [(200, {"org_id": ORG, "enabled": True, "updated_at": "x"})]
    got = auth.get_learning_sharing(auth.Client(team.base, team), team.store, ORG)
    assert got == {"org_id": ORG, "enabled": True, "updated_at": None}
    json.dumps(got)
