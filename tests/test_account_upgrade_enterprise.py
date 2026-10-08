"""`brindle account upgrade --enterprise [--trial] --seats N --org ORG`."""

import io

import pytest

from brindle.pro import account
from test_pro_team import ORG, REPO, run, team  # noqa: F401 - fixtures
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures

URL = "https://checkout.stripe.test/c/ent"


@pytest.fixture
def checkout(team):
    seen = []
    team.routes["POST /billing/checkout"] = lambda f, h: (seen.append(dict(f)), (200, {"url": URL}))[1]
    team.seen = seen
    return team


def test_enterprise_posts_the_plan_and_trial_false(checkout):
    code, out, _ = run(checkout, "upgrade", "--enterprise", "--seats", "25", "--org", ORG)
    assert code == 0 and out.strip() == URL
    assert checkout.seen == [{"plan": "enterprise", "seats": 25, "org_id": ORG, "trial": False}]


def test_enterprise_trial_posts_trial_true(checkout):
    code, out, _ = run(checkout, "upgrade", "--enterprise", "--trial", "--seats", "10", "--org", ORG)
    assert code == 0 and out.strip() == URL
    assert checkout.seen == [{"plan": "enterprise", "seats": 10, "org_id": ORG, "trial": True}]


def test_enterprise_defaults_to_the_current_org(checkout):
    checkout.store.save({**checkout.store.load(), "org_id": ORG})
    assert run(checkout, "upgrade", "--enterprise", "--seats", "3")[0] == 0
    assert checkout.seen[0]["org_id"] == ORG


def test_trial_over_ten_seats_is_refused_locally(checkout):
    code, _, err = run(checkout, "upgrade", "--enterprise", "--trial", "--seats", "11", "--org", ORG)
    assert code == 1 and "limited to 10 seats" in err and "11" in err
    assert "POST /billing/checkout" not in checkout.paths()


def test_over_ten_seats_is_fine_without_a_trial(checkout):
    assert run(checkout, "upgrade", "--enterprise", "--seats", "11", "--org", ORG)[0] == 0
    assert checkout.seen[0]["seats"] == 11


def test_enterprise_needs_seats_and_an_org(checkout):
    code, _, err = run(checkout, "upgrade", "--enterprise", "--seats", "3")
    assert code == 1 and "no team org selected" in err
    assert run(checkout, "upgrade", "--enterprise", "--org", ORG)[0] == 2
    assert run(checkout, "upgrade", "--enterprise", "--seats", "0", "--org", ORG)[0] == 2
    assert run(checkout, "upgrade", "--enterprise", "--seats", "x", "--org", ORG)[0] == 2
    assert "POST /billing/checkout" not in checkout.paths()


def test_flag_combinations_are_rejected(checkout):
    assert run(checkout, "upgrade", "--trial", "--seats", "3", "--org", ORG)[0] == 2
    assert run(checkout, "upgrade", "--team", "--trial", "--seats", "3", "--org", ORG)[0] == 2
    assert run(checkout, "upgrade", "--team", "--enterprise", "--seats", "3", "--org", ORG)[0] == 2
    assert run(checkout, "upgrade", "--enterprise", "--trial")[0] == 2
    assert "POST /billing/checkout" not in checkout.paths()


def test_team_and_pro_upgrades_are_unchanged(checkout):
    assert run(checkout, "upgrade", "--team", "--seats", "5", "--org", ORG)[0] == 0
    assert run(checkout, "upgrade")[0] == 0
    assert checkout.seen == [{"plan": "team", "seats": 5, "org_id": ORG}, {}]


def test_help_lists_enterprise_upgrade():
    out = io.StringIO()
    code = account.ProAccount(REPO, out=out, err=io.StringIO()).run(["--help"])
    assert code == 0
    assert "upgrade --enterprise [--trial] --seats N" in out.getvalue()
