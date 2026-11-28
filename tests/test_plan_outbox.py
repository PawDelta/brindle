"""A shared reader's pending changes survive separate daemon requests."""

from types import SimpleNamespace

import pytest

from brindle import plan_outbox
from brindle.pro import analysis_docs, shared_docs
from brindle.redact import mask_data
from test_pro_remote_learning import login_as
from brindle.pro.auth import TransportError
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_shared_reader import DESIGN, ROUTE, SECTION, app, lib  # noqa: F401


def test_offline_comment_sent_once(app, lib, db, monkeypatch):
    now = 100.0
    monkeypatch.setattr(plan_outbox, "time", SimpleNamespace(time=lambda: now))
    login_as(lib.calls, analysis_docs._session()[1], features=("analysis_docs",))
    route = f"POST {DESIGN}/comments"
    send = lib.calls.routes[route]
    features = ["comment_idempotency"]
    lib.calls.routes[f"GET {DESIGN}"] = lambda *_: (200, {**lib.view(), "features": features})
    body = {"client_id": "comment-one", "rev": 1, "anchor": SECTION, "text": "Which VM?"}
    offline = True

    def library_write(form, headers):
        if not features or form.get("idempotency_key") != body["client_id"]:
            return 422, {"error": "invalid_key"}
        if offline:
            raise TransportError("offline", network="offline")
        return send(form, headers)

    lib.calls.routes[route] = library_write
    waiting = app.post(f"{ROUTE}/comments", json=body)
    assert waiting.is_success
    assert waiting.json()["outbox"][0]["state"] == "waiting"

    offline = False
    features.clear()
    shared_docs.sync_open(db)
    assert not lib.threads
    features.append("comment_idempotency")
    shared_docs.sync_open(db)
    assert len(lib.threads) == 1
    now += 30
    delivered = app.get(ROUTE).json()
    assert [(r["client_id"], r["state"]) for r in delivered.get("outbox", [])] == [("comment-one", "sent")]
    assert [thread["text"] for thread in delivered["threads"]] == ["Which VM?"]
    # The app discarded that response when a newer visibility read superseded it.
    now += 30
    accepted = app.get(ROUTE).json()
    assert accepted.get("outbox") == delivered["outbox"]
    assert app.post(f"{ROUTE}/receipts", json={"owner": accepted["me"], "client_ids": ["comment-one"]}).is_success
    now += 30
    assert not app.get(ROUTE).json().get("outbox")
    assert app.post(f"{ROUTE}/comments", json=body).is_success
    assert len(app.get(ROUTE).json()["threads"]) == 1


@pytest.mark.parametrize("attempted", [pytest.param(False, marks=pytest.mark.variant("a refused vote never sent must not block the sign-off queued after it")), True])
def test_refused_vote_leaves_following_signoff_deliverable(app, lib, attempted):
    route = f"POST {DESIGN}/votes"

    def offline(fields, headers):
        raise TransportError("offline", network="offline")

    lib.calls.routes[route] = offline
    if not attempted:
        read = lib.calls.routes[f"GET {DESIGN}"]
        lib.calls.routes[f"GET {DESIGN}"] = offline
        assert app.post(f"{ROUTE}/comments", json={"client_id": "comment", "rev": 1, "anchor": SECTION,
                                                  "text": "Check this"}).is_success
    assert app.post(f"{ROUTE}/votes", json={"client_id": "pick", "rev": 1,
                                            "decision": "d1", "option": "a"}).is_success
    assert app.post(f"{ROUTE}/sign-offs", json={"client_id": "sign", "rev": 1}).is_success
    assert not lib.sign_offs
    if not attempted:
        lib.calls.routes[f"GET {DESIGN}"] = read
    lib.calls.routes[route] = [(409, {"error": "stale"})]
    delivered = app.get(ROUTE).json()
    assert delivered["signed_off"]
    state = next(item for item in delivered["outbox"] if item["client_id"] == "pick")["state"]
    assert state == ("unconfirmed" if attempted else "refused")
    if attempted:
        # A later read can supply the missing evidence without another POST.
        lib.votes["d1"] = {"a": ["user_1"]}
        confirmed = app.get(ROUTE).json()
        assert next(item for item in confirmed["outbox"] if item["client_id"] == "pick")["state"] == "sent"


def test_signoff_confirmed_from_history_after_plan_advances(app, lib):
    route = f"POST {DESIGN}/sign-offs"
    save = lib.calls.routes[route]

    def lose_response(form, headers):
        save(form, headers)
        raise TransportError("connection lost", network="offline")

    lib.calls.routes[route] = lose_response
    assert app.post(f"{ROUTE}/sign-offs", json={"rev": 1, "client_id": "signed"}).is_success
    lib.rev = 2
    lib.calls.routes[route] = lambda *_: (409, {"error": "stale"})
    receipt = app.get(ROUTE).json()["outbox"][0]
    assert receipt["state"] == "sent" and receipt["body"]["rev"] == 1


@pytest.mark.parametrize("tail,supported,view_only", [
    pytest.param("comments", False, True, marks=pytest.mark.variant("an older server without keys: a lost comment response is matched by content, not resent")),
    pytest.param("comments", True, True, marks=pytest.mark.variant("a view-only reader's keyed comment replay confirms without a write")),
    pytest.param("comments", True, False, marks=pytest.mark.variant(
        "With write access a keyed replay must confirm the original client ID without creating another comment.")),
    pytest.param("suggestions", False, True, marks=pytest.mark.variant("Legacy suggestions match the edit range and replacement.")),
    pytest.param("suggestions", True, True, marks=pytest.mark.variant("Suggestions require their own capability for keyed replay.")),
])
def test_lost_response_after_save(app, lib, supported, tail, view_only, db):
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
    read = lib.calls.routes[f"GET {DESIGN}"]
    lib.calls.routes[f"GET {DESIGN}"] = lambda *_: (403, {"error": "forbidden"})
    shared_docs.sync_open(db)
    denied = app.get(ROUTE)
    assert denied.status_code == 403
    details = denied.json()["error"]
    assert not details.get("refused_changes")
    assert details["unconfirmed_changes"] == [{"client_id": "lost-response", "tail": f"/{tail}", "rev": 1}]
    lib.calls.routes[f"GET {DESIGN}"] = read
    if view_only:
        lib.calls.routes[route] = lambda *_: (403, {"error": "forbidden"})
    else:
        # The acknowledgement is for the key; the newest identical comment is from elsewhere.
        save(mask_data(body), {})
    sent = app.get(ROUTE).json()
    assert len(sent["threads"]) == (2 if view_only else 3)
    assert sent["outbox"][0]["state"] == ("unconfirmed" if supported and view_only else "sent")
    if not view_only:
        assert "thread_item" not in sent["outbox"][0]["body"]
    assert keys == ({"lost-response"} if supported else {None})


@pytest.mark.parametrize("keyed", [pytest.param(True, marks=pytest.mark.variant("a keyed server keeps two identical comments with different client ids apart")), pytest.param(False, marks=pytest.mark.variant("without keys, an identical comment from another client id is still sent"))])
def test_identical_comment_with_another_client_id(app, lib, keyed):
    lib.calls.routes[f"GET {DESIGN}"] = lambda *_: (200, {**lib.view(), "features": ["comment_idempotency"] if keyed else []})
    route = f"POST {DESIGN}/comments"
    save = lib.calls.routes[route]
    lib.calls.routes[route] = [TransportError("offline", network="offline"), (403, {"error": "forbidden"})]
    body = {"rev": 1, "anchor": SECTION, "text": "Same comment"}
    assert app.post(f"{ROUTE}/comments", json={**body, "client_id": "first"}).is_success
    assert app.get(ROUTE).json()["outbox"][0]["state"] == "unconfirmed"
    lib.calls.routes[route] = save
    assert app.post(f"{ROUTE}/comments", json={**body, "client_id": "second"}).is_success
    view = app.get(ROUTE).json()
    receipts = {item["client_id"]: item for item in view["outbox"]}
    assert len(view["threads"]) == 1
    assert receipts["second"]["body"]["thread_item"] == view["threads"][0]["id"]
    assert receipts["first"]["state"] == "unconfirmed"
    if keyed:
        # Another device has no local receipt to exclude. Its words still cannot confirm this key.
        save(body, {})
        assert app.get(ROUTE).json()["outbox"][0]["state"] == "unconfirmed"


@pytest.mark.variant("a reader whose access was revoked still sees their own receipt, without the document")
def test_revoked_reader_gets_own_receipt_without_document(app, lib):
    # This earlier comment can't read its anchor, so neither write reaches a POST.
    lib.calls.routes[f"GET {DESIGN}"] = [TransportError("offline", network="offline") for _ in range(2)]
    assert app.post(f"{ROUTE}/comments", json={"client_id": "comment", "rev": 1, "anchor": SECTION,
                                              "text": "Check this"}).is_success
    body = {"client_id": "vote", "rev": 1, "decision": "d1", "option": "b"}
    assert app.post(f"{ROUTE}/votes", json=body).is_success
    lib.calls.routes[f"GET {DESIGN}"] = lambda *_: (403, {"error": "forbidden"})
    response = app.get(ROUTE)
    assert response.status_code == 403 and "threads" not in response.json()
    assert [r["client_id"] for r in response.json()["error"]["refused_changes"]] == ["comment", "vote"]
