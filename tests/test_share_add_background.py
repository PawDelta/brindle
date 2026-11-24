"""The share panel's Add answers at once (202) and the daemon lands the invite
in the background (brindle/pro/share_invites.py): offline it waits and tries
again, a lost answer never makes a second doc, a restart resumes it, and a
refusal stays on the row until the person asks again."""

import json

import pytest

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


def offline(backend, library, db, *, after_commit=False):
    """The Library's create can't be reached; ``after_commit``: it commits, and its answer is lost.
    Returns the reconnect, by which the unanswered create's claim has aged out (shared_docs.CLAIM_FOR)."""
    commit = library.create

    def create(form, headers):
        if after_commit:
            commit(form, headers)
        raise auth.TransportError("cannot reach brindle Pro backend: timed out")

    def back():
        backend.routes["POST /analysis-docs"] = commit
        db.conn.execute("UPDATE plan_shares SET claimed_at=claimed_at-? WHERE doc_id IS NULL", (sd.CLAIM_FOR + 1,))

    backend.routes["POST /analysis-docs"] = create
    return back


def test_invite_answers_before_the_library_is_asked(panel, backend, db):
    pid, library, app = panel
    got = routes_visuals.plan_person_invite(db, {"id": pid}, dict(ANN))
    assert got.status_code == 202
    assert json.loads(got.body)["invite"]["state"] == "sharing"
    assert not [k for k, _, _ in backend.calls if "analysis-docs" in k]       # nothing sent yet
    assert person(app, pid, "ann")["state"] == "sharing"                    # the row shows at once

    assert share_invites.run(db, pid) == 1
    ann = person(app, pid, "ann")
    assert ann["access"] == "comment" and "state" not in ann                # the Library's own row now


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


def test_a_lost_answer_and_a_second_add_make_one_doc_and_one_person(panel, backend, db, clock):
    pid, library, app = panel
    back = offline(backend, library, db, after_commit=True)
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202
    back()
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202     # Add again: the same row

    clock[0] += share_invites.backoff(1)
    share_invites.run(db)
    assert len(sent(backend, "POST /analysis-docs")) == 1                   # the committed doc was found
    assert [p["sub"] for p in app.get(f"/v1/plans/{pid}/access").json()["people"]] == ["ann"]


def test_a_restart_resumes_a_pending_invite(panel, backend, db, clock):
    pid, library, app = panel
    back = offline(backend, library, db)
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202
    back()
    share_invites._locks.clear()                    # a new daemon: only the database remembers it
    clock[0] += share_invites.backoff(1)
    lifecycle._send_share_invites()                 # its watch's first tick
    assert person(app, pid, "ann")["access"] == "comment"
    assert share_invites.pending(db, pid) == []


def test_a_refusal_is_shown_and_not_sent_again_until_asked(panel, backend, db, clock):
    pid, library, app = panel
    library.create_status = 403
    assert app.post(f"/v1/plans/{pid}/people", json=ANN).status_code == 202
    ann = person(app, pid, "ann")
    assert ann["state"] == "failed" and ann["error"]

    clock[0] += share_invites.BACKOFF_MOST * 10
    share_invites.run(db)
    assert len(sent(backend, "POST /analysis-docs")) == 1                   # never sent by itself again

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
    share = backend.routes[f"POST {BASE}/share"]

    def lost(form, headers):
        share(form, headers)
        raise auth.TransportError("cannot reach brindle Pro backend: timed out")

    backend.routes[f"POST {BASE}/share"] = lost
    assert app.post(f"/v1/plans/{pid}/people", json={"sub": "bo", "access": "view"}).status_code == 202
    assert app.delete(f"/v1/plans/{pid}/people/bo").status_code == 202
    assert person(app, pid, "bo") is None                                   # shown as gone at once
    clock[0] += share_invites.BACKOFF_MOST
    share_invites.run(db)
    assert len(sent(backend, f"DELETE {BASE}/share/user/bo")) == 1
    assert person(app, pid, "bo") is None and share_invites.pending(db, pid) == []
