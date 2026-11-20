"""A plan share the Library refuses tells the owner the way out (plans.gate's
``unconfirmed``): a refusal of access offers edit reviewers, and when the
Library has no document for the share (404 not_found), the owner can stop
sharing and build."""

import pytest

from brindle import plans
from brindle.pro import analysis_docs as ad
from brindle.pro import shared_docs as sd
from brindle.service.errors import ServiceError
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401 - fixture
from test_plans import session  # noqa: F401 - fixture
from test_shared_plan_docs import BASE, TEAM, parts  # noqa: F401 - fixture


def _owner_shares(db, pid, net, monkeypatch):
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (net["client"], net["store"]))
    monkeypatch.setattr(ad, "on_merged", lambda *a: None)
    monkeypatch.setattr(sd, "account", lambda store=None: "user_1")   # the account that shared it
    with pytest.raises(RuntimeError):
        sd.share(db, pid, TEAM, reviewers=[{"sub": "u_a", "required": True}])


@pytest.mark.variant("a Library access refusal must not let the owner stop sharing around the required review")
def test_a_share_the_library_refuses_offers_the_owner_a_way_out(db, parts, monkeypatch):
    pid, lib, net = parts
    lib.share_status = 404
    _owner_shares(db, pid, net, monkeypatch)
    unconfirmed = plans.gate(db, plans.get(db, pid))["unconfirmed"]
    assert unconfirmed["reason"] == plans.ACCESS_REFUSED
    assert unconfirmed["can_stop_sharing"] is False   # a refusal never opens a way around the review
    assert unconfirmed["can_edit_reviewers"] is True


def test_a_share_the_library_has_no_document_for_can_be_stopped_and_approved(db, parts, backend, monkeypatch):
    pid, _, net = parts
    backend.routes[f"POST {BASE}/share"] = lambda form, headers: (404, {"error": "not_found"})
    _owner_shares(db, pid, net, monkeypatch)
    unconfirmed = plans.gate(db, plans.get(db, pid))["unconfirmed"]
    assert unconfirmed["can_stop_sharing"] is True
    with pytest.raises(ServiceError):   # the Library has nothing to edit: the error says so
        sd.set_reviewers(db, pid, [{"sub": "u_a", "required": True}], **net)
    sd.stop_sharing(db, pid, 1)
    assert plans.gate(db, plans.get(db, pid)) is None
    plans.decide(db, pid, 1, True, person=True)
    assert plans.get(db, pid)["state"] == "approved"
