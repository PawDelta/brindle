"""Recipient Inbox rows and the Library access-request contract."""

import pytest

from brindle import inbox_team
from brindle.pro import review_queue
from test_shared_reader import app, lib  # noqa: F401
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401
from test_shared_plan_docs import DESIGN, DOC


def test_viewer_receives_shared_item(db, monkeypatch):
    class Feed:
        def call(self, *args, **kwargs):
            return 200, {"items": [{"kind": "shared_with_you", "item_key": "shared:ad_1",
                                    "doc_id": "ad_1", "title": "Plan", "sender": {"sub": "author"}}]}

    monkeypatch.setattr(review_queue, "account", lambda *a: (Feed(), "viewer", "token"))
    inbox_team.clear()
    try:
        inbox_team.poll(db)
        rows = inbox_team.items("viewer")
        assert len(rows) == 1
        assert rows[0].doc == "ad_1" and rows[0].inbox_group == "from_team"
        assert rows[0].from_person.sub == "author"
        assert inbox_team.items("other") == []
    finally:
        inbox_team.clear()


@pytest.mark.parametrize("action", ["grant", "decline"])
def test_access_request_reaches_author(db, lib, app, monkeypatch, action):
    queued = []

    def request(form, headers):
        queued.append({"kind": "access_requested", "item_key": f"access:{DOC}:user_1",
                       "doc_id": DOC, "title": "Plan", "role": form["role"],
                       "requester": {"sub": "user_1", "name": "Reader"}})
        return 200, lib.view()

    lib.calls.routes[f"POST {DESIGN}/access-requests"] = request
    response = app.post(f"/v1/shared-designs/{DOC}/access-requests", json={"role": "edit"})
    assert response.status_code == 200

    class Feed:
        def call(self, *args, **kwargs):
            return 200, {"items": queued}

    monkeypatch.setattr(review_queue, "account", lambda *a: (Feed(), "u_author", "token"))
    inbox_team.clear()
    try:
        inbox_team.poll(db)
        row, = inbox_team.items("u_author")
        assert row.from_person.sub == "user_1" and row.access_role == "edit"
        assert row.inbox_group == "needs_you"
        lib.calls.routes[f"POST {DESIGN}/access-requests/user_1/{action}"] = lambda f, h: (
            lib.permissions.update({"user_1": {"can_edit": action == "grant"}}) or 200, lib.view())
        resolved = app.post(f"/v1/shared-designs/{DOC}/access-requests/user_1/{action}", json={"role": row.access_role})
        assert resolved.status_code == 200 and resolved.json()["can_edit"] is (action == "grant")
    finally:
        inbox_team.clear()
