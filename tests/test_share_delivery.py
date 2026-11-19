"""A plan share the Library refuses tells the owner the way out: stop sharing,
or edit the reviewers (plans.gate's ``unconfirmed``)."""

from brindle import plans
from brindle.pro import analysis_docs as ad
from brindle.pro import shared_docs as sd
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401 - fixture
from test_plans import session  # noqa: F401 - fixture
from test_shared_plan_docs import TEAM, parts  # noqa: F401 - fixture


def test_a_share_the_library_refuses_offers_the_owner_a_way_out(db, parts, monkeypatch):
    pid, lib, net = parts
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (net["client"], net["store"]))
    lib.share_status = 404
    try:
        sd.share(db, pid, TEAM, reviewers=[{"sub": "u_a", "required": True}])
    except RuntimeError:
        pass   # the Library refused the share: the owner's plan keeps its link and its refusal
    unconfirmed = plans.gate(db, plans.get(db, pid))["unconfirmed"]
    assert unconfirmed["reason"] == plans.ACCESS_REFUSED
    assert unconfirmed["can_stop_sharing"] is False   # a refusal never opens a way around the review
    assert unconfirmed["can_edit_reviewers"] is True
