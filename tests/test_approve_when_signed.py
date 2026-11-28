"""A person's approval follows the required reviewers of the revision they read."""
import pytest
from starlette.testclient import TestClient

from brindle import plan_docs
from brindle.daemon.app import build_app
from brindle.pro import shared_docs
from test_plans import AUTH, TOKEN, _signed_off, messages, outage, session  # noqa: F401


@pytest.mark.parametrize("event", ["sign", "optional", "changes", "revise", "withdraw", "cancel"])
def test_approve_when_signed(db, session, outage, event):
    line = outage
    line.down = None
    line.view = {**line.view, "reviewers": [{"sub": "Maya", "required": True},
                                             {"sub": "Lee", "required": False}]}
    path = f"/v1/plans/{line.pid}"
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        response = app.post(path + "/decide", json={"approved": True, "rev": 1})
        assert response.status_code == 200, response.text
        assert response.json()["gate"]["approval_held"] == {"rev": 1, "waiting_on": ["Maya"]}
        shared_docs.sync_open(db)
        assert session[1] == []
        before = messages(db)
        if event == "sign":
            repeated = app.post(path + "/decide", json={"approved": True, "rev": 1, "note": "Second click"})
            assert repeated.json()["gate"]["approval_held"] == response.json()["gate"]["approval_held"]
            line.view["sign_offs"] = _signed_off("Maya")
        elif event == "optional":
            line.view["reviewers"][0]["required"] = False
        elif event == "changes":
            plan_docs.comment(db, line.pid, 1, {"kind": "section", "section": "s1"}, "Revise this.",
                              author=plan_docs.TEAMMATE + "Maya")
        elif event == "revise":
            plan_docs.edit(db, line.pid, 1, "s1", 0, "", "New text")
        elif event == "withdraw":
            response = app.post(path + "/approval/withdraw", json={})
            assert response.status_code == 200
            line.view["sign_offs"] = _signed_off("Maya")
        else:
            response = app.post(path + "/cancel", json={"rev": 1})
            assert response.status_code == 200
        shared_docs.sync_open(db)
        plan = app.get(path).json()
        assert not (plan["gate"] or {}).get("approval_held")
        builds = event in ("sign", "optional")
        assert session[1] == (["build it"] if builds else [])
        assert plan["state"] == ("approved" if builds else "cancelled" if event == "cancel" else "open")
        if event in ("changes", "revise", "cancel"):
            assert messages(db) != before
        if event == "sign":
            assert plan["verdict"]["note"] != "Second click"
