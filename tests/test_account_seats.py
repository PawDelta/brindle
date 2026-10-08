"""``brindle account seats N [--org ORG]``: the request it sends, the org it
acts on by default, what it prints, and how each refusal reads."""
import io
import time

import pytest

from brindle.pro import account, auth, credentials
from pro_fixtures import BASE, backend, claims, pro_env, signing_key  # noqa: F401 - fixtures

ORG = "org_team1"
REPO = "/work/seats-project"
PATH = f"POST /orgs/{ORG}/seats"


@pytest.fixture
def team(backend):
    """The fake backend with a logged-in team member whose current org is ORG."""
    backend.store = credentials.default_store()
    t = backend.issue()
    backend.store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                        "access_expires_at": time.time() + 900, "base_url": BASE,
                        "org_id": ORG, "entitlement": claims(org_id=ORG, plan="team")})
    return backend


def run(backend, *args):
    out, err = io.StringIO(), io.StringIO()
    code = account.ProAccount(REPO, store=backend.store, transport=backend, out=out, err=err).run(list(args))
    return code, out.getvalue(), err.getvalue()


def answer(backend, status, body, path=PATH):
    """Make ``path`` answer ``status``/``body``; return the list the request forms land in."""
    seen = []

    def route(form, headers):
        seen.append(dict(form))
        return status, body
    backend.routes[path] = route
    return seen


# -- request shape and default org ----------------------------------------------------------------


def test_seats_posts_the_count_as_json_to_the_org(team):
    seen = answer(team, 200, {"seats": 12, "used": 7})
    code, _, _ = run(team, "seats", "12", "--org", ORG)
    assert code == 0
    assert seen == [{"seats": 12}]
    assert team.paths().count(PATH) == 1


def test_seats_defaults_to_the_current_org(team):
    seen = answer(team, 200, {"seats": 3, "used": 1})
    code, _, _ = run(team, "seats", "3")
    assert code == 0 and seen == [{"seats": 3}]
    assert team.paths().count(PATH) == 1


def test_seats_refuses_a_malformed_org_id_before_sending(team):
    code, _, err = run(team, "seats", "2", "--org", "org a/b")
    assert code == 1 and "invalid org id" in err
    assert not any(c[0].startswith("POST /orgs/") for c in team.calls)


def test_seats_without_an_org_refuses(backend):
    backend.store = credentials.default_store()
    t = backend.issue()
    backend.store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                        "access_expires_at": time.time() + 900, "base_url": BASE})
    code, _, err = run(backend, "seats", "4")
    assert code == 1 and "no team org selected" in err
    assert not any(c[0] == PATH for c in backend.calls)


# -- success output -------------------------------------------------------------------------------


def test_seats_success_prints_the_count_and_the_proration(team):
    answer(team, 200, {"seats": 12, "used": 7})
    code, out, err = run(team, "seats", "12", "--org", ORG)
    assert code == 0 and err == ""
    assert out.strip() == "Seats: 12 (7 used). Stripe prorates the change on your next invoice."


def test_seats_success_without_a_used_count_still_prints(team):
    answer(team, 200, {"seats": 4})
    code, out, _ = run(team, "seats", "4", "--org", ORG)
    assert code == 0 and out.strip() == "Seats: 4. Stripe prorates the change on your next invoice."


# -- local validation (no request sent) -----------------------------------------------------------


@pytest.mark.parametrize("n", ["0", "-3", "1.5", "x", "2e3", "²"])
def test_seats_validates_n_before_sending(team, n):
    code, _, err = run(team, "seats", n, "--org", ORG)
    assert code == 1 and "seats must be a whole number, at least 1" in err
    assert not any(c[0] == PATH for c in team.calls)


def test_seats_needs_exactly_one_count(team):
    assert run(team, "seats")[0] == 2
    assert run(team, "seats", "3", "4")[0] == 2
    assert run(team, "seats", "3", "--seats", "3")[0] == 2
    assert not any(c[0] == PATH for c in team.calls)


# -- error mapping --------------------------------------------------------------------------------


def test_403_says_you_need_billing_rights(team):
    answer(team, 403, {"error": "forbidden"})
    code, _, err = run(team, "seats", "5", "--org", ORG)
    assert code == 1 and "billing rights in this org" in err


def test_below_used_says_deactivate_someone_first(team):
    answer(team, 409, {"error": "below_used", "error_description": "7 seats in use"})
    code, _, err = run(team, "seats", "5", "--org", ORG)
    assert code == 1 and "5 is below the members using seats" in err
    assert "deactivate someone first" in err


def test_over_max_passes_the_servers_message_through(team):
    answer(team, 409, {"error": "over_max", "error_description": "Your plan allows at most 50 seats."})
    code, _, err = run(team, "seats", "99", "--org", ORG)
    assert code == 1 and "Your plan allows at most 50 seats." in err


def test_over_max_without_a_message_still_reads(team):
    answer(team, 409, {"error": "over_max"})
    code, _, err = run(team, "seats", "99", "--org", ORG)
    assert code == 1 and "seat cap for this plan" in err


def test_no_subscription_points_at_upgrade(team):
    answer(team, 409, {"error": "no_subscription"})
    code, _, err = run(team, "seats", "5", "--org", ORG)
    assert code == 1 and "no Team or Enterprise subscription" in err
    assert "brindle account upgrade" in err


def test_400_says_seats_must_be_a_whole_number(team):
    answer(team, 400, {"error": "bad_request"})
    code, _, err = run(team, "seats", "5", "--org", ORG)
    assert code == 1 and "seats must be a whole number, at least 1" in err


def test_other_errors_fall_back_to_the_generic_message(team):
    answer(team, 500, {"error": "boom", "error_description": "try later"})
    code, _, err = run(team, "seats", "5", "--org", ORG)
    assert code == 1 and "boom" in err


# -- docs -----------------------------------------------------------------------------------------


def test_usage_and_readme_document_the_seats_command():
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert "\n  seats N [--org ORG]" in account.USAGE
    assert "brindle account seats N" in readme
