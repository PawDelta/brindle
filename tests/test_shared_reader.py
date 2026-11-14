"""A teammate's reader for a shared design (brindle.pro.shared_reader), through
the daemon's routes, against the fake Library."""

import copy

import pytest
from starlette.testclient import TestClient

from brindle.config import brindle_home
from brindle.daemon.app import build_app
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, credentials, doc_questions
from pro_fixtures import backend, claims, fixed_identity, pro_env, sign, signing_key  # noqa: F401
from test_plans import AUTH, TOKEN
from test_pro_remote_learning import login_as
from test_shared_plan_docs import DESIGN, DOC, SUMMARY, TEAM_LOGIN, Library, sent

ROUTE = f"/v1/shared-designs/{DOC}"
CONTENT = {"lede": "Why we are doing this.", "ask": None, "split": [],
           "sections": [{"id": "summary", "title": "Summary", "kind": "markdown", "markdown": SUMMARY, "caption": None}],
           "decisions": [{"id": "d1", "title": "Where it runs", "mine": "a", "challenge": None,
                          "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}]}
SECTION = {"kind": "section", "section": "summary"}


@pytest.fixture
def lib(db, tmp_path, backend, monkeypatch):
    """A design by another account that this one (user_1) was asked to review."""
    store = credentials.FileStore(tmp_path / "pro")
    login_as(backend, store, features=TEAM_LOGIN)
    monkeypatch.setattr(ad, "_session", lambda *a: (auth.Client(None, backend), store))
    library = Library(backend)
    library.rev, library.content, library.author = 1, copy.deepcopy(CONTENT), "u_author"
    library.permissions = {"user_1": {"can_edit": False, "can_suggest": True, "can_comment": True}}
    library.reviewers = [{"sub": "user_1", "required": True}, {"sub": "u_other", "required": True}]
    library.calls = backend
    return library


@pytest.fixture
def app():
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        yield client


def _home_files():
    return {p: p.read_bytes() for p in brindle_home().rglob("*") if p.is_file() and p.suffix not in (".db", ".db-wal", ".db-shm")}


def test_teammate_reads_writes_as_themselves_and_nothing_is_kept(db, lib, app):
    before = _home_files()
    plans_before = db.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]

    read = app.get(ROUTE).json()
    assert read["markdown"].startswith("Why we are doing this.") and SUMMARY in read["sections"][0]["markdown"]
    assert (read["me"], read["author"], read["can_comment"], read["can_sign_off"]) == ("user_1", "u_author", True, True)
    assert read["can_edit"] is False
    lib.permissions["user_1"]["can_edit"] = True            # granted writing in the live draft
    assert app.get(ROUTE).json()["can_edit"] is True
    assert read["gate"]["waiting_on"] == ["user_1", "u_other"] and [r["you"] for r in read["gate"]["reviewers"]] == [True, False]

    commented = app.post(f"{ROUTE}/comments", json={"rev": 1, "anchor": SECTION, "text": "Which VM?"}).json()
    assert commented["threads"][0]["author"] == "person"
    replied = app.post(f"{ROUTE}/comments", json={"rev": 1, "parent": 1, "text": "Any VM."}).json()
    assert replied["threads"][0]["replies"][0]["text"] == "Any VM."
    suggested = app.post(f"{ROUTE}/suggestions", json={"rev": 1, "section": "summary", "start": 0, "before": "Workers",
                                                       "after": "Hosts"}).json()
    assert suggested["threads"][1]["before"] == "Workers"
    resolved = app.post(f"{ROUTE}/threads/1", json={"rev": 1, "action": "resolve"}).json()
    assert resolved["threads"][0]["status"] == "resolved"
    voted = app.post(f"{ROUTE}/votes", json={"rev": 1, "decision": "d1", "option": "b"}).json()
    assert voted["decisions"][0]["confirmed"] == "b" and voted["decisions"][0]["votes"] == {"b": ["person"]}

    signed = app.post(f"{ROUTE}/sign-offs", json={"rev": 1}).json()
    assert signed["signed_off"] is True and signed["gate"]["signed"] == 1 and lib.sign_offs[0]["sub"] == "user_1"

    # No local plan, no local copy, and no text change from this side.
    assert db.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == plans_before
    assert not db.conn.execute("SELECT 1 FROM plan_shares").fetchone()
    assert _home_files() == before
    assert not [k for k, _, _ in lib.calls.calls if k.startswith((f"POST {DESIGN}/edits", f"POST {DESIGN}/revisions"))]


def test_a_teammates_frozen_version_carries_the_librarys_sign_offs_and_their_own_once_signed(db, lib):
    """A teammate keeps no plan here, so the live channel reads a frozen
    version's sign-offs from the Library itself; once they sign off, their
    window's refresh tells every window the version with their sign-off."""
    from brindle.pro import live_docs
    from test_live_docs import Clock, FakeRelay

    relay = FakeRelay(Clock(), author="u_author")
    relay.frozen = lib.rev = 2                          # version 2 is out for sign-off
    lib.sign_offs = [{"sub": "u_other", "rev": 1, "at": 1.0}]      # signed an earlier version only
    here = live_docs.Manager(relay.transport("user_1"), available=lambda: True, drive=False)
    built = build_app(TOKEN)
    built.state.live_docs = here

    def state(window):
        while "state" not in (frame := window.receive_json()):
            pass
        return frame["state"]

    with TestClient(built, headers=AUTH) as client:
        client.portal.call(here.link(DOC).connect)
        with client.websocket_connect(f"/v1/visuals/{DOC}/live") as window:
            window.send_text(TOKEN)
            first = state(window)
            assert (first["lock"], first["version"]["id"], first["version"]["signOffs"]) == ("sign_off", 2, [])
            assert client.post(f"{ROUTE}/sign-offs", json={"rev": 2}).json()["signed_off"] is True
            window.send_json({"refresh": True})
            assert state(window)["version"]["signOffs"] == ["user_1"]
        client.portal.call(here.stop)
    assert not db.conn.execute("SELECT 1 FROM plan_shares").fetchone()


def test_the_librarys_refusals_reach_the_app(lib, app):
    """A permission refused, a revision that moved, and what a reader may never send."""
    lib.refuse_vote = True
    refused = app.post(f"{ROUTE}/votes", json={"rev": 1, "decision": "d1", "option": "b"})
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "policy_denied")

    stale = app.post(f"{ROUTE}/comments", json={"rev": 7, "anchor": SECTION, "text": "On an old revision."})
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "stale")

    accepted = app.post(f"{ROUTE}/threads/1", json={"rev": 1, "action": "accept"})
    assert accepted.status_code == 400 and not sent(lib.calls, f"POST {DESIGN}/threads/1")

    # An outage says nothing about access. Refused even with a fresh token, the app drops the doc.
    def offline(form, headers):
        raise auth.TransportError("offline")

    lib.calls.routes[f"GET {DESIGN}"] = offline
    assert app.get(ROUTE).status_code == 502

    lib.calls.routes[f"GET {DESIGN}"] = lambda f, h: (401, {"error": "invalid_token"})
    signed_out = app.get(ROUTE)
    assert (signed_out.status_code, signed_out.json()["error"]["code"]) == (402, "signed_out")


def test_ask_the_supervisor_goes_by_doc_and_thread_after_the_reply(lib, app, monkeypatch):
    asked = []
    monkeypatch.setattr(doc_questions, "after_reply", lambda **kw: asked.append(kw))
    app.post(f"{ROUTE}/comments", json={"rev": 1, "anchor": SECTION, "text": "An earlier question?", "question": True})
    got = app.post(f"{ROUTE}/comments", json={"rev": 1, "anchor": SECTION, "text": "Why these workers?", "question": True,
                                              "repo": "/my/repo"}).json()
    assert got["threads"][1]["question"] is True
    assert asked[-1] == {"doc_id": DOC, "thread_id": 2, "owner": "user_1", "repo_root": "/my/repo"}
