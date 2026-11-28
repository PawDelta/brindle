"""Deleting the Library doc an open plan is shared through used to leave the
plan syncing against a deleted doc: every pass a 404, sign-offs that could
never arrive, a plan that could never be approved. The delete now warns first
and, on confirm, unshares the plan here so it is a personal doc again."""

import pytest

from brindle import plans
from brindle.approval_sources import PERSON_IN_APP
from brindle.daemon import routes_actions
from brindle.pro import local_library
from brindle.pro import shared_docs as sd
from brindle.service.errors import ServiceError
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401 - fixture
from test_plans import session  # noqa: F401 - fixture
from test_shared_plan_docs import BASE, DOC, TEAM, parts  # noqa: F401 - fixture


def test_deleting_a_shared_plans_doc_warns_then_unshares_the_plan_so_it_can_be_approved(db, parts, monkeypatch):
    pid, _, net = parts
    sd.share(db, pid, TEAM, reviewers=[{"sub": "u_a", "required": True}], **net)
    assert sd.link(db, pid)["doc_id"] == DOC
    sent = []
    monkeypatch.setattr(routes_actions, "docs_api", lambda method, path, *a, **k: sent.append((method, path)) or {})
    monkeypatch.setattr(local_library, "get", lambda doc_id, store=None: None)

    with pytest.raises(ServiceError) as refused:      # unconfirmed: nothing is deleted
        routes_actions.delete_doc(db, {"id": DOC}, {})
    assert refused.value.code == "shared_plan"
    assert sent == [] and sd.link(db, pid) is not None

    routes_actions.delete_doc(db, {"id": DOC}, {"confirm": True})
    assert sent == [("DELETE", BASE)]
    assert sd.link(db, pid) is None
    plans.decide(db, pid, 1, True, approval=PERSON_IN_APP)
    assert plans.get(db, pid)["state"] == "approved"
