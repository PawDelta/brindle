"""A plan share the Library refuses tells the owner the way out (plans.gate's
``unconfirmed``): a refusal of access offers edit reviewers, and when the
Library has no document for the share (404 not_found), the owner can stop
sharing and build."""

import json

import pytest

from brindle import plans, visual_offers
from brindle.pro import analysis_docs as ad
from brindle.pro import shared_docs as sd
from brindle.service.errors import ServiceError
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401 - fixture
from test_plans import DECISIONS, session  # noqa: F401 - fixture
from test_shared_plan_docs import BASE, DESIGN, TEAM, parts, sent  # noqa: F401 - fixture


def _owner_shares(db, pid, net, monkeypatch, backend, status, error):
    """The owner shares the plan, then reading its document is refused with
    ``status``: only a read of the document itself says what access is left."""
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (net["client"], net["store"]))
    monkeypatch.setattr(ad, "on_merged", lambda *a: None)
    monkeypatch.setattr(sd, "account", lambda store=None: "user_1")   # the account that shared it
    sd.share(db, pid, TEAM, reviewers=[{"sub": "u_a", "required": True}])
    backend.routes[f"GET {DESIGN}"] = lambda form, headers: (status, {"error": error})
    with pytest.raises(RuntimeError):
        sd.sync(db, pid)


@pytest.mark.variant("a Library access refusal must not let the owner stop sharing around the required review")
def test_a_share_the_library_refuses_offers_the_owner_a_way_out(db, parts, backend, monkeypatch):
    pid, lib, net = parts
    _owner_shares(db, pid, net, monkeypatch, backend, 403, "forbidden")
    unconfirmed = plans.gate(db, plans.get(db, pid))["unconfirmed"]
    assert unconfirmed["reason"] == plans.ACCESS_REFUSED
    assert unconfirmed["can_stop_sharing"] is False   # a refusal never opens a way around the review
    assert unconfirmed["can_edit_reviewers"] is True


FIREWALL_PAGE = {"text": "<html><head><title>403 Forbidden</title></head></html>", "content_type": "text/html"}


@pytest.mark.parametrize("refusing", ["POST /analysis-docs", f"POST {BASE}/share"])
def test_a_share_the_firewall_refuses_says_so(db, session, parts, backend, refusing):
    """pawdelta.com's firewall answers a request body over 8 KB with its own
    403 page, before the Library reads it (no Library error code). The share
    of a large plan names that and the request's size, not "HTTP 403", and
    the daemon doesn't pass it on as a 403, which the app reads as the Library
    refusing the person's account."""
    from brindle.daemon import routes_visuals

    boss, _ = session
    pid, _, net = parts
    tests = "| 1.1 | Open session comes back first | Restart to update; the open session shows first. | Both |\n" * 120
    visual_offers.person_spoke(db, boss, "Make the planning doc a test plan.")
    plans.propose(db, boss, "# 0.6.5 test plan", [{**DECISIONS[0], "section": "tests"}], plan_id=pid,
                  sections=[{"id": "tests", "title": "The tests", "markdown": tests}])
    passing = backend.routes[refusing]

    def firewall(form, headers):
        size = len(json.dumps(form, separators=(",", ":")).encode())
        return (403, FIREWALL_PAGE) if size > 8192 or refusing.endswith("/share") else passing(form, headers)

    backend.routes[refusing] = firewall
    with pytest.raises(ServiceError) as e:
        routes_visuals._library(db, pid, lambda s: s.share(db, pid, TEAM, reviewers=[{"sub": "u_d", "required": False}],
                                                            **net))
    assert e.value.http_status == 502 and "firewall" in e.value.message
    if refusing == "POST /analysis-docs":
        create = sent(backend, "POST /analysis-docs")[-1]
        assert f"{-(-len(json.dumps(create, separators=(',', ':')).encode()) // 1024)} KB" in e.value.message
        assert sd.link(db, pid) is None        # nothing was created: the claim is given up, a later share starts afresh


def test_a_share_the_library_has_no_document_for_can_be_stopped_and_approved(db, parts, backend, monkeypatch):
    pid, _, net = parts
    _owner_shares(db, pid, net, monkeypatch, backend, 404, "not_found")
    unconfirmed = plans.gate(db, plans.get(db, pid))["unconfirmed"]
    assert unconfirmed["can_stop_sharing"] is True
    with pytest.raises(ServiceError):   # the Library has nothing to edit: the error says so
        sd.set_reviewers(db, pid, [{"sub": "u_a", "required": True}], **net)
    sd.stop_sharing(db, pid, 1)
    assert plans.gate(db, plans.get(db, pid)) is None
    plans.decide(db, pid, 1, True, person=True)
    assert plans.get(db, pid)["state"] == "approved"
