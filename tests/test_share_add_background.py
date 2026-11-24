"""The share panel's Add answers at once (202) and the daemon lands the invite
in the background (brindle/pro/share_invites.py): only once the app sends back
the answer's receipt, as the account that asked; offline it waits and tries
again, a restart resumes it, and a refusal stays until the person asks again."""

import asyncio
import json

import pytest

from brindle.db import DB
from brindle.daemon import lifecycle
from brindle.daemon.app import build_app
from brindle.pro import auth, share_invites
from brindle.pro import shared_docs as sd
from pro_fixtures import backend, claims, pro_env, sign, signing_key  # noqa: F401
from test_plans import AUTH, TOKEN, session  # noqa: F401
from test_share_panel_access import acked, add_person, panel, person  # noqa: F401
from test_shared_plan_docs import BASE, parts, sent  # noqa: F401

ANN = {"sub": "ann", "access": "comment"}


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(share_invites, "clock", lambda: now[0])
    return now


def offline(backend, library, db):
    """The Library's create can't be reached. Returns the reconnect, by which
    the unanswered create's claim has aged out (shared_docs.CLAIM_FOR)."""
    commit = library.create

    def create(form, headers):
        raise auth.TransportError("cannot reach brindle Pro backend: timed out")

    def back():
        backend.routes["POST /analysis-docs"] = commit
        db.conn.execute("UPDATE plan_shares SET claimed_at=claimed_at-? WHERE doc_id IS NULL", (sd.CLAIM_FOR + 1,))

    backend.routes["POST /analysis-docs"] = create
    return back


def lose_share_answers(backend):
    """POST /share commits in the Library, and its answer is lost."""
    share = backend.routes[f"POST {BASE}/share"]

    def lost(form, headers):
        share(form, headers)
        raise auth.TransportError("cannot reach brindle Pro backend: timed out")

    backend.routes[f"POST {BASE}/share"] = lost
    return lambda: backend.routes.__setitem__(f"POST {BASE}/share", share)


def queued(app, pid):
    return {p["sub"]: p for p in app.get(f"/v1/plans/{pid}/invites").json()["people"]}


def receipt(got):
    return got.json()["invite"]["receipt"]


def ack(app, pid, *receipts):
    assert app.post(f"/v1/plans/{pid}/invites/receipts", json={"receipts": list(receipts)}).status_code == 200


@pytest.mark.variant("the delivery gate, through the daemon's ASGI app: a 202 whose write is blocked and whose "
                     "client disconnects is never sent, by the watch or a restart; other tests always acknowledge")
def test_an_add_whose_answer_never_reached_the_app_is_never_sent_by_itself(panel, backend, db):
    pid, library, app = panel
    body = json.dumps(ANN).encode()

    async def cut_off():
        written, messages = asyncio.Event(), [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if messages:
                return messages.pop()
            await written.wait()
            return {"type": "http.disconnect"}          # the app went away while the answer was being written

        async def send(message):
            if message["type"] == "http.response.body":
                written.set()
                await asyncio.Event().wait()            # the write never completes

        path = f"/v1/plans/{pid}/people"
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                 "server": ("127.0.0.1", 8123), "client": ("127.0.0.1", 50000),
                 "headers": [(b"authorization", f"Bearer {TOKEN}".encode()), (b"content-type", b"application/json")]}
        task = asyncio.create_task(build_app(TOKEN, port=8123)(scope, receive, send))
        await written.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cut_off())
    share_invites._locks.clear()                    # and a restart: neither the watch nor it sends it
    lifecycle._send_share_invites()
    assert not [k for k, _, _ in backend.calls if "analysis-docs" in k]
    ann = queued(app, pid)["ann"]
    assert ann["receipt"]                           # still the person's to send (or discard) from the panel
    assert person(app, pid, "ann")["state"] == "sharing"

    ack(app, pid, ann["receipt"])                   # Send
    assert person(app, pid, "ann")["access"] == "comment" and share_invites.pending(db, pid) == []


def test_overlapping_answers_send_each_acknowledged_change_once(panel, backend, db):
    """A then B for the same person, both out; A's answer arrives, B's never does:
    A is sent. A receipt for a change already superseded is let go."""
    pid, library, app = panel
    first = app.post(f"/v1/plans/{pid}/people", json=ANN)
    second = app.put(f"/v1/plans/{pid}/people/ann", json={"access": "edit"})
    assert second.status_code == 202
    assert queued(app, pid)["ann"]["access"] == "edit"                      # the newest is what shows
    shares = lambda: [f["access"] for f in sent(backend, f"POST {BASE}/share")]   # noqa: E731
    ack(app, pid, receipt(first))
    assert shares() == ["comment"]                                          # A, though B is newer and unanswered
    ack(app, pid, receipt(second))
    assert shares() == ["comment", "edit"] and share_invites.pending(db, pid) == []
    assert person(app, pid, "ann")["access"] == "edit"

    # B' then a late receipt for an older change: nothing older is sent over the newer one.
    newer = app.put(f"/v1/plans/{pid}/people/ann", json={"access": "view"})
    assert newer.status_code == 200                                         # landed: written straight to the Library
    ack(app, pid, receipt(first))
    assert person(app, pid, "ann")["access"] == "view"


def test_stop_sharing_after_an_earlier_change_landed_is_sent_not_dropped(panel, backend, db):
    """A lands while B (newer, unconfirmed) is still out; Stop sharing then
    must revoke the grant A made, not just drop B."""
    pid, library, app = panel
    first = app.post(f"/v1/plans/{pid}/people", json={"sub": "bo", "access": "view"})
    second = app.put(f"/v1/plans/{pid}/people/bo", json={"access": "edit"})
    ack(app, pid, receipt(first))                                           # A lands; B is still unconfirmed
    assert [f["scope"]["id"] for f in sent(backend, f"POST {BASE}/share")] == ["bo"]
    assert second.status_code == 202
    removed = app.delete(f"/v1/plans/{pid}/people/bo")
    assert removed.status_code == 202                                       # a removal to send, not a drop
    ack(app, pid, receipt(removed))
    assert len(sent(backend, f"DELETE {BASE}/share/user/bo")) == 1
    assert person(app, pid, "bo") is None and share_invites.pending(db, pid) == []


@pytest.mark.variant("an invite sent again before its backoff is due (due_at ignored) hammers an offline Library, "
                     "and one a restart doesn't resume never lands; other tests only send again when asked")
def test_an_offline_invite_lands_after_a_restart_once_the_library_is_back(panel, backend, db, clock):
    pid, library, app = panel
    back = offline(backend, library, db)
    assert add_person(app, pid, ANN).status_code == 202
    assert person(app, pid, "ann")["state"] == "sharing"                    # still on its way, not failed
    tried = len(sent(backend, "POST /analysis-docs"))

    assert share_invites.run(db) == 0                                       # not due yet: it waits its backoff
    assert len(sent(backend, "POST /analysis-docs")) == tried

    back()
    share_invites._locks.clear()                    # and a restart meanwhile: only the database remembers it
    clock[0] += share_invites.backoff(1)
    lifecycle._send_share_invites()                 # the new daemon's watch, first tick
    assert person(app, pid, "ann")["access"] == "comment"
    assert share_invites.pending(db, pid) == []


@pytest.mark.parametrize("when", [
    pytest.param("before", marks=pytest.mark.variant("signed in as someone else before the send starts")),
    pytest.param("during", marks=pytest.mark.variant("switched after the send's check, before its first Library call")),
])
def test_an_invite_is_never_sent_as_another_account(panel, parts, backend, db, monkeypatch, when):
    pid, library, app = panel
    store = parts[2]["store"]

    def switch():
        store.save({**store.load(), "entitlement": sign(backend.key, claims(sub="someone_else",
                                                                            features=["analysis_docs", "analysis_docs_team"]))})

    got = app.post(f"/v1/plans/{pid}/people", json=ANN)
    if when == "before":
        switch()
    else:
        checked = sd._require_team

        def then_switch(s):
            ent = checked(s)
            switch()                                # once the send has checked, before it asks the Library
            return ent

        monkeypatch.setattr(sd, "_require_team", then_switch)
    ack(app, pid, receipt(got))
    assert not [k for k, _, _ in backend.calls if "analysis-docs" in k]     # nothing uploaded or granted
    assert queued(app, pid)["ann"]["state"] == "failed"


def test_a_change_to_a_pending_person_is_only_the_asking_accounts(panel, db):
    pid, library, app = panel
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202
    library.actor = "someone_else"                  # the require_entitlement the panel fixture reads
    assert app.put(f"/v1/plans/{pid}/people/ann", json={"access": "edit"}).status_code == 409
    assert app.delete(f"/v1/plans/{pid}/people/ann").status_code == 409
    assert queued(app, pid)["ann"]["access"] == "comment"


@pytest.mark.parametrize("write", ["change", "withdraw"])
@pytest.mark.variant("another account's Add lands between the write's sign-in read and its transaction")
def test_a_pending_write_never_takes_over_another_accounts_newer_change(panel, db, monkeypatch, write):
    pid, library, app = panel
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202
    asked = share_invites._asker

    def then_someone_else_adds(db_, plan_id, sub, store):
        who = asked(db_, plan_id, sub, store)
        library.actor = "someone_else"
        share_invites.record(db_, plan_id, sub, "view", False)              # their Add, in between
        library.actor = "user_1"
        return who

    monkeypatch.setattr(share_invites, "_asker", then_someone_else_adds)
    got = (app.put(f"/v1/plans/{pid}/people/ann", json={"access": "edit"}) if write == "change"
           else app.delete(f"/v1/plans/{pid}/people/ann"))
    assert got.status_code == 409
    newest = share_invites.pending(db, pid)[0]
    assert (newest["owner"], newest["access"]) == ("someone_else", "view")     # theirs, untouched


def test_a_refusal_is_shown_and_not_sent_again_until_asked(panel, backend, db, clock):
    pid, library, app = panel
    library.create_status = 403
    assert add_person(app, pid, ANN).status_code == 202
    ann = person(app, pid, "ann")
    assert ann["state"] == "failed" and ann["error"]

    clock[0] += share_invites.BACKOFF_MOST * 10
    share_invites.run(db)
    assert len(sent(backend, "POST /analysis-docs")) == 1                   # never sent by itself again
    # A change with no value isn't a change: it neither answers 202 nor clears the refusal.
    assert app.put(f"/v1/plans/{pid}/people/ann", json={}).status_code == 400
    assert app.put(f"/v1/plans/{pid}/people/ann/sign-off", json={"must": None}).status_code == 400
    assert queued(app, pid)["ann"]["state"] == "failed"

    library.create_status = 200
    assert add_person(app, pid, ANN).status_code == 202                     # Try again
    assert person(app, pid, "ann")["access"] == "comment"
    assert share_invites.pending(db, pid) == []


def test_changes_while_an_invite_is_out_land_after_it_in_order(panel, backend, db, clock):
    pid, library, app = panel
    back = offline(backend, library, db)
    assert add_person(app, pid, ANN).status_code == 202
    assert acked(app, pid, app.put(f"/v1/plans/{pid}/people/ann", json={"access": "edit"})).status_code == 202
    assert acked(app, pid, app.put(f"/v1/plans/{pid}/people/ann/sign-off", json={"must": True})).status_code == 202
    back()
    clock[0] += share_invites.BACKOFF_MOST
    share_invites.run(db)
    ann = person(app, pid, "ann")
    assert (ann["access"], ann["sign_off"]) == ("edit", "required")

    # Stop sharing with someone whose invite may have landed (its answer lost): a removal, sent after it.
    lose_share_answers(backend)
    assert add_person(app, pid, {"sub": "bo", "access": "view"}).status_code == 202
    assert acked(app, pid, app.delete(f"/v1/plans/{pid}/people/bo")).status_code == 202
    assert person(app, pid, "bo") is None                                   # shown as gone at once
    clock[0] += share_invites.BACKOFF_MOST
    share_invites.run(db)
    assert len(sent(backend, f"DELETE {BASE}/share/user/bo")) == 1
    assert person(app, pid, "bo") is None and share_invites.pending(db, pid) == []


@pytest.mark.variant("the race the id guard exists for: a newer change acknowledged while the older send is out; "
                     "deleting the person's rows on that answer would drop the change unsent")
def test_a_change_made_while_the_invite_is_being_sent_goes_out_after_it(panel, backend, db, clock):
    pid, library, app = panel
    assert add_person(app, pid, {"sub": "bo", "access": "view"}).status_code == 202
    share = backend.routes[f"POST {BASE}/share"]

    def meanwhile(form, headers):
        # The person makes Ann an editor while her invite is out (another request, on its own connection).
        if form["scope"]["id"] == "ann" and form["access"] == "comment":
            own = DB()
            row = share_invites.change(own, pid, "ann", "access", "edit")
            share_invites.ack(own, pid, [row["receipt"]])
            own.conn.close()
        return share(form, headers)

    backend.routes[f"POST {BASE}/share"] = meanwhile
    assert add_person(app, pid, ANN).status_code == 202
    assert [f["access"] for f in sent(backend, f"POST {BASE}/share") if f["scope"]["id"] == "ann"] == ["comment", "edit"]
    assert person(app, pid, "ann")["access"] == "edit" and share_invites.pending(db, pid) == []


def test_try_again_on_a_refused_removal_removes_and_never_grants(panel, backend, db, clock):
    pid, library, app = panel
    assert add_person(app, pid, ANN).status_code == 202                     # shared, so removals reach it
    restore = lose_share_answers(backend)
    assert add_person(app, pid, {"sub": "bo", "access": "view"}).status_code == 202
    restore()
    revoke = backend.routes[f"DELETE {BASE}/share/user/bo"]
    backend.routes[f"DELETE {BASE}/share/user/bo"] = lambda f, h: (403, {"error": "forbidden"})
    assert acked(app, pid, app.delete(f"/v1/plans/{pid}/people/bo")).status_code == 202
    bo = queued(app, pid)["bo"]
    assert (bo["op"], bo["state"]) == ("remove", "failed")
    assert person(app, pid, "bo")["state"] == "failed"                     # still listed: they still have it

    backend.routes[f"DELETE {BASE}/share/user/bo"] = revoke
    assert acked(app, pid, app.delete(f"/v1/plans/{pid}/people/bo")).status_code == 202     # Try again: the removal
    assert person(app, pid, "bo") is None and share_invites.pending(db, pid) == []
    assert [f["scope"]["id"] for f in sent(backend, f"POST {BASE}/share")] == ["ann", "bo"]   # never granted again
