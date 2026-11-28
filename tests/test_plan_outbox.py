"""A shared reader's pending changes survive separate daemon requests."""

import pytest

from brindle.pro import analysis_docs, shared_docs
from brindle.redact import mask_data
from test_pro_remote_learning import login_as
from brindle.pro.auth import TransportError
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_shared_reader import DESIGN, ROUTE, SECTION, app, lib  # noqa: F401


def test_offline_comment_sent_once(app, lib, db):
    login_as(lib.calls, analysis_docs._session()[1], features=("analysis_docs",))
    route = f"POST {DESIGN}/comments"
    send = lib.calls.routes[route]
    lib.calls.routes[route] = [TransportError("offline", network="offline")]
    body = {"client_id": "comment-one", "rev": 1, "anchor": SECTION, "text": "Which VM?"}
    waiting = app.post(f"{ROUTE}/comments", json=body)
    assert waiting.is_success
    assert waiting.json()["outbox"][0]["state"] == "waiting"

    lib.calls.routes[route] = send
    shared_docs.sync_open(db)
    assert len(lib.threads) == 1
    delivered = app.get(ROUTE).json()
    assert [thread["text"] for thread in delivered["threads"]] == ["Which VM?"]
    assert app.post(f"{ROUTE}/comments", json=body).is_success
    assert len(app.get(ROUTE).json()["threads"]) == 1


def test_refused_vote_leaves_following_signoff_deliverable(app, lib):
    route = f"POST {DESIGN}/votes"

    def offline(fields, headers):
        raise TransportError("offline", network="offline")

    lib.calls.routes[route] = offline
    assert app.post(f"{ROUTE}/votes", json={"client_id": "pick", "rev": 1,
                                            "decision": "d1", "option": "a"}).is_success
    assert app.post(f"{ROUTE}/sign-offs", json={"client_id": "sign", "rev": 1}).is_success
    assert not lib.sign_offs
    lib.calls.routes[route] = [(409, {"error": "stale"})]
    delivered = app.get(ROUTE).json()
    assert delivered["signed_off"]
    assert next(item for item in delivered["outbox"] if item["client_id"] == "pick")["state"] == "refused"


@pytest.mark.parametrize("supported", [False, True])
@pytest.mark.parametrize("tail", [
    "comments",
    pytest.param("suggestions", marks=pytest.mark.variant(
        "Suggestion keys have their own feature flag and legacy matching uses the revision and edit range.")),
])
def test_lost_response_after_save(app, lib, supported, tail):
    feature = "comment_idempotency" if tail == "comments" else "suggestion_idempotency"
    lib.calls.routes[f"GET {DESIGN}"] = lambda *_: (200, {**lib.view(), "features": [feature] if supported else []})
    route = f"POST {DESIGN}/{tail}"
    save = lib.calls.routes[route]
    keys = set()
    lose_response = True

    def library_write(form, headers):
        nonlocal lose_response
        key = form.get("idempotency_key")
        if not supported and key is not None:
            return 422, {"error": "unknown_field"}
        if key and key in keys:
            return lib.answer()
        answer = save(form, headers)
        keys.add(key)
        if lose_response:
            lose_response = False
            raise TransportError("connection lost", network="offline")
        return answer

    lib.calls.routes[route] = library_write
    content = {"anchor": SECTION} if tail == "comments" else {
        "section": "summary", "start": 0, "before": "Workers", "after": "Hosts"}
    sensitive = "ghp_" + "a" * 36
    body = {"rev": 1, "text": sensitive, **content}
    if tail == "suggestions":
        body["after"] = sensitive
    # An identical earlier change must not suppress the new one.
    save(mask_data(body), {})
    waiting = app.post(f"{ROUTE}/{tail}", json={**body, "client_id": "lost-response"}).json()
    assert waiting["outbox"][0]["state"] == "waiting"
    sent = app.get(ROUTE).json()
    assert len(sent["threads"]) == 2
    assert sent["outbox"][0]["state"] == "sent"
    assert keys == ({"lost-response"} if supported else {None})


def test_revoked_reader_gets_own_receipt_without_document(app, lib):
    lib.calls.routes[f"POST {DESIGN}/votes"] = [TransportError("offline", network="offline")]
    body = {"client_id": "vote", "rev": 1, "decision": "d1", "option": "b"}
    assert app.post(f"{ROUTE}/votes", json=body).is_success
    lib.calls.routes[f"GET {DESIGN}"] = lambda *_: (403, {"error": "forbidden"})
    response = app.get(ROUTE)
    assert response.status_code == 403 and "threads" not in response.json()
    assert response.json()["error"]["refused_changes"] == [{"client_id": "vote", "tail": "/votes", "rev": 1}]
