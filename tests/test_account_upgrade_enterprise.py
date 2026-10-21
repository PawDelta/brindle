"""`brindle account upgrade --enterprise`: Enterprise is set up by contact, never a checkout."""
import pytest

from test_pro_team import ORG, run, team  # noqa: F401 - fixtures
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures

CONTACT = ("Enterprise is set up with PawDelta, not bought here. Email support@pawdelta.com "
           "with your team size and where agents will run; we'll turn it on for your org.")


@pytest.fixture
def checkout(team):
    team.routes["POST /billing/checkout"] = lambda f, h: (200, {"url": "https://checkout.stripe.test/c/x"})
    return team


def test_enterprise_prints_the_contact_message_and_exits_zero(checkout):
    code, out, err = run(checkout, "upgrade", "--enterprise", "--seats", "25", "--org", ORG)
    assert code == 0 and out.strip() == CONTACT and err == ""
    assert "POST /billing/checkout" not in checkout.paths()


def test_enterprise_trial_is_accepted_and_ignored(checkout):
    code, out, _ = run(checkout, "upgrade", "--enterprise", "--trial", "--seats", "11", "--org", ORG)
    assert code == 0 and out.strip() == CONTACT
    assert "POST /billing/checkout" not in checkout.paths()


@pytest.mark.parametrize("args", [
    ("upgrade", "--enterprise"),
    ("upgrade", "--enterprise", "--seats", "3"),
    ("upgrade", "--enterprise", "--org", ORG),
    ("upgrade", "--enterprise", "--trial"),
    ("upgrade", "--enterprise", "--seats", "0", "--org", ORG),
])
def test_enterprise_old_flag_combinations_still_exit_zero(checkout, args):
    code, out, _ = run(checkout, *args)
    assert code == 0 and out.strip() == CONTACT
    assert "POST /billing/checkout" not in checkout.paths()


def test_enterprise_seats_must_still_be_a_number(checkout):
    code, _, err = run(checkout, "upgrade", "--enterprise", "--seats", "x", "--org", ORG)
    assert code == 2 and "upgrade" in err
    assert "POST /billing/checkout" not in checkout.paths()


def test_enterprise_with_team_is_still_a_usage_error(checkout):
    assert run(checkout, "upgrade", "--enterprise", "--team", "--seats", "3", "--org", ORG)[0] == 2
    assert "POST /billing/checkout" not in checkout.paths()


def test_team_upgrade_is_unchanged(checkout):
    code, out, _ = run(checkout, "upgrade", "--team", "--seats", "5", "--org", ORG)
    assert code == 0 and "https://checkout.stripe.test/c/x" in out
