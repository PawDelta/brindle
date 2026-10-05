"""``copse account org company``: linking orgs into one company, the only
group learning is pooled within."""

from copse.pro import auth
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_team import ORG, run, team  # noqa: F401 - fixtures/helpers

OTHER = "org_team2"
PATH = f"/orgs/{ORG}/company"
COMPANY = "co_" + "a" * 24


def company(team, company_id=None):
    state = {"company_id": company_id}

    def put(form, headers):
        assert form in ({"link_with": OTHER}, {"unlink": True})
        state["company_id"] = COMPANY if "link_with" in form else None
        return 200, {"org_id": ORG, **state}

    team.routes[f"GET {PATH}"] = lambda f, h: (200, {"org_id": ORG, **state})
    team.routes[f"PUT {PATH}"] = put
    return state


def calls(team):
    return [c for c in team.calls if c[0].endswith("/company")]


def test_shows_no_company_by_default(team):
    company(team)
    code, out, _ = run(team, "org", "company", "--org", ORG)
    assert code == 0 and f"Org {ORG} is not linked into a company" in out
    assert "never across companies" in " ".join(out.split()) and "company link" in out
    assert [c[0] for c in calls(team)] == [f"GET {PATH}"]


def test_defaults_to_the_current_org(team):
    company(team, COMPANY)
    code, out, _ = run(team, "org", "company")
    assert code == 0 and f"Org {ORG} is in company {COMPANY}" in out


def test_link_then_unlink(team):
    state = company(team)
    code, out, _ = run(team, "org", "company", "link", OTHER, "--org", ORG)
    assert code == 0 and f"is in company {COMPANY}" in out and state["company_id"] == COMPANY
    code, out, _ = run(team, "org", "company", "unlink", "--org", ORG)
    assert code == 0 and "not linked" in out and state["company_id"] is None
    puts = [c for c in calls(team) if c[0] == f"PUT {PATH}"]
    assert [c[1] for c in puts] == [{"link_with": OTHER}, {"unlink": True}]
    assert all(c[2]["Authorization"].startswith("Bearer ") for c in puts)


def test_linking_needs_the_owner_of_both_orgs(team):
    company(team)
    team.routes[f"PUT {PATH}"] = [(403, {"error": "forbidden"})]
    code, _, err = run(team, "org", "company", "link", OTHER, "--org", ORG)
    assert code == 1 and "forbidden" in err and "owns both orgs" in err


def test_a_non_member_gets_the_servers_refusal(team):
    team.routes[f"GET {PATH}"] = [(404, {"error": "not_found"})]
    code, _, err = run(team, "org", "company", "--org", ORG)
    assert code == 1 and "not_found" in err


def test_bad_org_ids_are_rejected_before_any_call(team):
    company(team)
    for args in (("--org", "../x"), ("link", "../x", "--org", ORG)):
        code, _, err = run(team, "org", "company", *args)
        assert code == 1 and "invalid org id" in err
    assert not calls(team)


def test_bad_arguments_print_usage(team):
    for bad in (("org", "company", "link"), ("org", "company", "unlink", OTHER),
                ("org", "company", "merge", OTHER), ("org", "company", "link", OTHER, "x"),
                ("org", "company", "--admin")):
        code, _, err = run(team, *bad)
        assert code == 2 and "org company" in err, bad


def test_a_malformed_answer_is_refused(team):
    for body in ({"org_id": ORG}, {"org_id": ORG, "company_id": 7}, {"org_id": ORG, "company_id": ""}):
        team.routes[f"GET {PATH}"] = [(200, body)]
        code, _, err = run(team, "org", "company", "--org", ORG)
        assert code == 1 and "no company state" in err


def test_client_calls_return_a_sanitized_state(team):
    company(team)
    c = auth.Client(team.base, team)
    assert auth.get_company(c, team.store, ORG) == {"org_id": ORG, "company_id": None}
    assert auth.link_company(c, team.store, ORG, OTHER) == {"org_id": ORG, "company_id": COMPANY}
    assert auth.unlink_company(c, team.store, ORG) == {"org_id": ORG, "company_id": None}
