"""Approval closes review edits while access can still be revoked."""

import pytest

from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_plans import session  # noqa: F401
from test_share_panel_access import add_person, as_, panel  # noqa: F401
from test_shared_plan_docs import TEAM, parts  # noqa: F401


def approve(app, route):
    approved = app.post(f"{route}/decide", json={"rev": 1, "approved": True})
    assert approved.status_code == 200, approved.json()
    assert approved.json()["state"] == "approved"


@pytest.mark.variant("an approved plan still allows each form of access removal")
def test_revoke_access_after_approval(panel):
    pid, library, app = panel
    route = f"/v1/plans/{pid}"
    assert add_person(app, pid, {"sub": "ann", "access": "view"}).status_code == 202
    assert add_person(app, pid, {"sub": "bo", "access": "view"}).status_code == 202
    assert app.post(f"{route}/share", json={"scope": TEAM}).status_code == 200
    assert app.post(f"{route}/people", json={"sub": "bo", "access": "comment"}).status_code == 202
    assert as_(library, app, "ann") is not None
    assert as_(library, app, "teammate") is not None
    approve(app, route)

    revoked = app.delete(f"{route}/people/ann")
    assert revoked.status_code == 200, revoked.json()
    assert as_(library, app, "ann") is None
    assert app.delete(f"{route}/scopes/group/t_1").status_code == 200
    assert as_(library, app, "teammate") is None
    assert as_(library, app, "bo") is not None
    stopped = app.delete(f"{route}/access")
    assert stopped.status_code == 200, stopped.json()
    assert stopped.json()["state"] == "approved"
    assert as_(library, app, "bo") is None
    assert app.get(f"{route}/invites").json()["people"] == []


@pytest.mark.variant("an unsent invite cannot bypass the closed-review check for sign-off edits")
def test_closed_review_leaves_pending_invite_unchanged(panel):
    pid, library, app = panel
    route = f"/v1/plans/{pid}"
    assert app.post(f"{route}/share", json={"scope": TEAM, "reviewers": [
        {"sub": "ann", "required": False},
    ]}).status_code == 200
    queued = app.post(f"{route}/people", json={"sub": "bo", "access": "view"})
    assert queued.status_code == 202
    approve(app, route)

    refused = app.post(f"{route}/reviewers", json={"reviewers": []})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "review_closed"
    assert library.reviewers == [{"sub": "ann", "required": False}]
    refused = app.put(f"{route}/people/bo/sign-off", json={"must": True})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "review_closed"
    assert app.get(f"{route}/invites").json()["people"] == [queued.json()["invite"]]


@pytest.mark.variant("a refusal to stop sharing preserves queued invites as well as remote access")
def test_refused_stop_sharing_leaves_pending_invite_unchanged(panel):
    pid, library, app = panel
    route = f"/v1/plans/{pid}"
    assert app.post(f"{route}/share", json={"scope": TEAM}).status_code == 200
    queued = app.post(f"{route}/people", json={"sub": "bo", "access": "view"})
    assert queued.status_code == 202
    approve(app, route)
    library.link, library.unlink_status = {"url": "https://plans.example/dsl_1"}, 403

    refused = app.delete(f"{route}/access")
    assert app.get(f"{route}/invites").json()["people"] == [queued.json()["invite"]]
    assert refused.status_code == 403, refused.json()
    assert as_(library, app, "teammate") is not None
