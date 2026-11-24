"""The share panel's Add answers at once (202) and the daemon lands the invite
in the background (brindle/pro/share_invites.py): only once that answer is
delivered, as the account that asked; offline it waits and tries again, a
restart resumes it, and a refusal stays on the row until the person asks again."""

import asyncio
import json

import pytest

from brindle.db import DB
from brindle.daemon import lifecycle, routes_visuals
from brindle.pro import auth, share_invites
from brindle.pro import shared_docs as sd
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401
from test_plans import AUTH, TOKEN, session  # noqa: F401
from test_share_panel_access import panel, person  # noqa: F401
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


def test_an_invite_is_sent_only_once_its_answer_is_delivered(panel, backend, db):
    """Through the real response: while its write is blocked the watch sends
    nothing; a cancelled write puts it back; a delivered one lands it."""
    pid, library, app = panel

    async def answer(release):
        response = routes_visuals.plan_person_invite(db, {"id": pid}, dict(ANN))
        assert response.status_code == 202 and json.loads(response.body)["invite"]["state"] == "sharing"
        entered, go = asyncio.Event(), asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body":
                entered.set()
                await go.wait()

        task = asyncio.create_task(response({"type": "http"}, None, send))
        await entered.wait()
        assert queued(app, pid)["ann"]["state"] == "sharing"             # the person sees it at once
        assert await asyncio.to_thread(lifecycle._send_share_invites) is None   # the watch, meanwhile
        assert not [k for k, _, _ in backend.calls if "analysis-docs" in k]
        if release:
            go.set()
            await task
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(answer(release=False))
    assert share_invites.pending(db, pid) == [] and not sent(backend, "POST /analysis-docs")   # never told: undone
    asyncio.run(answer(release=True))
    assert person(app, pid, "ann")["access"] == "comment" and share_invites.pending(db, pid) == []


@pytest.mark.variant("an invite sent again before its backoff is due (due_at ignored) hammers an offline Library; "
                     "other tests only send again when the person asks")
def test_an_offline_invite_lands_once_the_library_is_back(panel, backend, db, clock):
    pid, library, app = panel
    back = offline(backend, library, db)
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202
    assert person(app, pid, "ann")["state"] == "sharing"                    # still on its way, not failed
    tried = len(sent(backend, "POST /analysis-docs"))

    assert share_invites.run(db) == 0                                       # not due yet: it waits its backoff
    assert len(sent(backend, "POST /analysis-docs")) == tried

    back()
    clock[0] += share_invites.backoff(1)
    assert share_invites.run(db) == 1
    assert person(app, pid, "ann")["access"] == "comment"
    assert share_invites.pending(db, pid) == []


def test_an_invite_is_never_sent_as_another_account(panel, backend, db):
    pid, library, app = panel
    response = routes_visuals.plan_person_invite(db, {"id": pid}, dict(ANN))
    library.actor = "someone_else"                  # signed in to another account before it went out

    async def written(message):
        pass

    asyncio.run(response({"type": "http"}, None, written))
    assert not [k for k, _, _ in backend.calls if "analysis-docs" in k]     # nothing uploaded or granted
    assert sd.link(db, pid) is None
    assert queued(app, pid)["ann"]["state"] == "failed"


def test_a_restart_resumes_a_pending_invite_and_undoes_one_never_answered(panel, backend, db, clock):
    pid, library, app = panel
    back = offline(backend, library, db)
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202
    # Bo's request was still being answered when that daemon stopped: no answer can come now.
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "bo", "access": "view"}).status_code == 202
    db.conn.execute("UPDATE plan_invites SET ready=0, boot='earlier', undo='\"new\"' WHERE sub='bo'")
    back()
    share_invites._locks.clear()                    # a new daemon: only the database remembers it
    clock[0] += share_invites.backoff(1)
    lifecycle._send_share_invites()                 # its watch's first tick
    assert person(app, pid, "ann")["access"] == "comment"
    assert person(app, pid, "bo") is None and share_invites.pending(db, pid) == []
    assert [f["scope"]["id"] for f in sent(backend, f"POST {BASE}/share")] == ["ann"]


def test_a_refusal_is_shown_and_not_sent_again_until_asked(panel, backend, db, clock):
    pid, library, app = panel
    library.create_status = 403
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202
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
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202     # Try again
    assert person(app, pid, "ann")["access"] == "comment"
    assert share_invites.pending(db, pid) == []


def test_changes_while_an_invite_is_out_land_after_it_in_order(panel, backend, db, clock):
    pid, library, app = panel
    back = offline(backend, library, db)
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202
    assert app.put(f"/v1/plans/{pid}/people/ann", json={"access": "edit"}).status_code == 202
    assert app.put(f"/v1/plans/{pid}/people/ann/sign-off", json={"must": True}).status_code == 202
    back()
    clock[0] += share_invites.BACKOFF_MOST
    share_invites.run(db)
    ann = person(app, pid, "ann")
    assert (ann["access"], ann["sign_off"]) == ("edit", "required")

    # Stop sharing with someone whose invite may have landed (its answer lost): a removal, sent after it.
    lose_share_answers(backend)
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "bo", "access": "view"}).status_code == 202
    assert app.delete(f"/v1/plans/{pid}/people/bo").status_code == 202
    assert person(app, pid, "bo") is None                                   # shown as gone at once
    clock[0] += share_invites.BACKOFF_MOST
    share_invites.run(db)
    assert len(sent(backend, f"DELETE {BASE}/share/user/bo")) == 1
    assert person(app, pid, "bo") is None and share_invites.pending(db, pid) == []


def test_a_change_made_while_the_invite_is_being_sent_goes_out_after_it(panel, backend, db, clock):
    pid, library, app = panel
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "bo", "access": "view"}).status_code == 202
    share = backend.routes[f"POST {BASE}/share"]

    def meanwhile(form, headers):
        # The person makes Ann an editor while her invite is out (another request, on its own connection).
        if form["scope"]["id"] == "ann" and form["access"] == "comment":
            own = DB()
            row = share_invites.change(own, pid, "ann", "access", "edit")
            own.conn.execute("UPDATE plan_invites SET ready=1 WHERE seq=?", (row["seq"],))     # its answer delivered
            own.conn.close()
        return share(form, headers)

    backend.routes[f"POST {BASE}/share"] = meanwhile
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202
    assert [f["access"] for f in sent(backend, f"POST {BASE}/share") if f["scope"]["id"] == "ann"] == ["comment", "edit"]
    assert person(app, pid, "ann")["access"] == "edit" and share_invites.pending(db, pid) == []


def test_try_again_on_a_refused_removal_removes_and_never_grants(panel, backend, db, clock):
    pid, library, app = panel
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202       # shared, so removals reach it
    restore = lose_share_answers(backend)
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "bo", "access": "view"}).status_code == 202
    restore()
    revoke = backend.routes[f"DELETE {BASE}/share/user/bo"]
    backend.routes[f"DELETE {BASE}/share/user/bo"] = lambda f, h: (403, {"error": "forbidden"})
    assert app.delete(f"/v1/plans/{pid}/people/bo").status_code == 202
    bo = queued(app, pid)["bo"]
    assert (bo["op"], bo["state"]) == ("remove", "failed")
    assert person(app, pid, "bo")["state"] == "failed"                     # still listed: they still have it

    backend.routes[f"DELETE {BASE}/share/user/bo"] = revoke
    assert app.delete(f"/v1/plans/{pid}/people/bo").status_code == 202    # Try again: the removal
    assert person(app, pid, "bo") is None and share_invites.pending(db, pid) == []
    assert [f["scope"]["id"] for f in sent(backend, f"POST {BASE}/share")] == ["ann", "bo"]   # never granted again
