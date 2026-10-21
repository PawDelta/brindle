"""`brindle account upgrade --enterprise`: Enterprise is set up by contact, never a checkout."""
import pytest

from test_pro_team import ORG, run, team  # noqa: F401 - fixtures
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures

CONTACT = ("Enterprise is set up with PawDelta, not bought here. Email support@pawdelta.com "
           "with your team size and where agents will run; we'll turn it on for your org.")


@pytest.mark.parametrize("args", [
    ("upgrade", "--enterprise", "--seats", "25", "--org", ORG),
    ("upgrade", "--enterprise", "--trial", "--seats", "11", "--org", ORG),
    ("upgrade", "--enterprise", "--seats", "3"),
    ("upgrade", "--enterprise", "--org", ORG),
    ("upgrade", "--enterprise", "--trial"),
    ("upgrade", "--enterprise"),
])
def test_enterprise_prints_the_contact_message_and_exits_zero(team, args):
    team.routes["POST /billing/checkout"] = lambda f, h: (200, {"url": "https://checkout.stripe.test/c/x"})
    code, out, err = run(team, *args)
    assert code == 0 and out.strip() == CONTACT and err == ""
    assert "POST /billing/checkout" not in team.paths()
