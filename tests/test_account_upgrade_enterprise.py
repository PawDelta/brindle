"""`brindle account upgrade --enterprise [--trial] --seats N --org ORG`."""
import pytest

from test_pro_team import ORG, run, team  # noqa: F401 - fixtures
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


def test_trial_over_ten_seats_is_refused_locally(checkout):
    code, _, err = run(checkout, "upgrade", "--enterprise", "--trial", "--seats", "11", "--org", ORG)
    assert code == 1 and "limited to 10 seats" in err and "11" in err
    assert "POST /billing/checkout" not in checkout.paths()


def test_enterprise_needs_seats_and_an_org(checkout):
    code, _, err = run(checkout, "upgrade", "--enterprise", "--seats", "3")
    assert code == 1 and "no team org selected" in err
    assert run(checkout, "upgrade", "--enterprise", "--org", ORG)[0] == 2
    assert run(checkout, "upgrade", "--enterprise", "--seats", "0", "--org", ORG)[0] == 2
    assert run(checkout, "upgrade", "--enterprise", "--seats", "x", "--org", ORG)[0] == 2
    assert "POST /billing/checkout" not in checkout.paths()
