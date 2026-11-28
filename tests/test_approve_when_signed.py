"""A person's approval follows the required reviewers of the revision they read."""
import pytest
from starlette.testclient import TestClient

from brindle import plan_docs, plans
from brindle.daemon.app import build_app
from brindle.pro import shared_docs
from brindle.pro import analysis_docs as ad, auth
from brindle.service import inbox
from brindle.service.errors import ServiceError
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401
from test_plans import AUTH, TOKEN, _shared, _signed_off, messages, outage, session  # noqa: F401
from test_shared_plan_docs import TEAM, parts  # noqa: F401


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
            response = app.post(path + "/approval/withdraw", json={"rev": 1})
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
        if event == "cancel":
            notice = db.conn.execute("SELECT approval_notice FROM plans WHERE id=?", (line.pid,)).fetchone()[0]
            assert notice and any(item.visual_id == line.pid and item.reason == notice for item in inbox.items(db, None))


def test_stale_withdrawal_preserves_new_revision_approval(db, outage):
    line = outage
    line.down = None
    line.view = {**line.view, "reviewers": [{"sub": "Maya", "required": True}]}
    path = f"/v1/plans/{line.pid}"
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        assert app.post(path + "/decide", json={"rev": 1, "approved": True}).json()["gate"]["approval_held"]
        plan_docs.edit(db, line.pid, 1, "s1", 0, "", "Revised. ")
        line.view["rev"] = 2
        _shared(db, line.pid, line.view, local_rev=2, remote_rev=2)
        assert app.post(path + "/decide", json={"rev": 2, "approved": True}).json()["gate"]["approval_held"]
        assert app.post(path + "/approval/withdraw", json={"rev": 1}).status_code == 409
        assert app.get(path).json()["gate"]["approval_held"]["rev"] == 2


def test_stale_import_cannot_reopen_objection_after_new_revision_approval(db, parts, monkeypatch):
    pid, library, net = parts
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (net["client"], net["store"]))
    shared_docs.share(db, pid, TEAM, reviewers=[{"sub": "u_a", "required": True}], **net)
    thread = {"id": 99, "kind": "comment", "rev": 1, "author": "u_a", "text": "Revise.",
              "status": "open", "at": 5.0, "replies": [], "anchor": {"kind": "section", "section": "summary"}}
    library.threads.append(thread)
    shared_docs.sync(db, pid, **net)
    pull = shared_docs._pull

    def pull_after_revision(db, p, share, view, **kwargs):
        monkeypatch.setattr(shared_docs, "_pull", pull)
        library.threads[0] = {**thread, "status": "resolved"}
        plan_docs.edit(db, pid, 1, "summary", 0, "", "Revised. ")
        shared_docs.sync(db, pid, **net)
        plans.decide(db, pid, 2, True, {"d1": "repo"}, person=True)
        return pull(db, p, share, view, **kwargs)

    monkeypatch.setattr(shared_docs, "_pull", pull_after_revision)
    try:
        shared_docs.sync(db, pid, **net)
    except ServiceError as error:
        assert error.code == "stale"
    gate = plans.gate(db, plans.get(db, pid))
    assert gate["approval_held"] == {"rev": 2, "waiting_on": ["u_a"]}
    assert db.conn.execute("SELECT status FROM plan_threads WHERE plan_id=? AND author='teammate:u_a'",
                           (pid,)).fetchone()[0] == "resolved"


def test_imported_objection_withdraws_even_if_outbound_write_fails(db, parts, monkeypatch):
    pid, library, net = parts
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (net["client"], net["store"]))
    shared_docs.share(db, pid, TEAM, reviewers=[{"sub": "u_a", "required": True}], **net)
    plans.decide(db, pid, 1, True, {"d1": "repo"}, person=True)
    plan_docs.comment(db, pid, 1, {"kind": "section", "section": "summary"}, "A question?", question=True)
    library.threads.append({"id": 99, "kind": "comment", "rev": 1, "author": "u_a", "text": "Revise.",
                            "status": "open", "at": 5.0, "replies": [],
                            "anchor": {"kind": "section", "section": "summary"}})
    library.lose_answer = True
    with pytest.raises(auth.AuthError):
        shared_docs.sync(db, pid, **net)
    assert plans.gate(db, plans.get(db, pid))["approval_held"] is None
    library.lose_answer = False
    library.threads[0]["status"] = "resolved"
    library.sign_offs.append({"rev": 1, "sub": "u_a", "at": 8.0})
    shared_docs.sync(db, pid, **net)
    assert plans.get(db, pid)["state"] == "open"
