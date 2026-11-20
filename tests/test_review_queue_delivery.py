"""A review request reaches the reviewer's machine: the daemon's watch asks the
Library's per-person queue, the Inbox gets a "Review requested" card, and the
bus says so, so the app re-reads its Inbox and notifies (app/src/store/sync.ts
re-reads only on an event; app/src/screens/Inbox/notify.ts notifies a new card)."""

import asyncio
import time

import pytest

from brindle import bus
from brindle.daemon import lifecycle
from brindle.daemon.app import build_app
from brindle.daemon.auth import new_token
from brindle.daemon.session_review import ReadinessWatch
from brindle.pro import auth, credentials, review_queue
from brindle.service import inbox
from pro_fixtures import backend, claims, pro_env, sign, signing_key  # noqa: F401 - fixtures

QUEUE = "GET /me/analysis-docs/review-requests"
TEAM = ["analysis_docs", "analysis_docs_team"]
HOUR = 3600


def _library(backend, monkeypatch, requests):
    """Wire the fake Library onto ``backend`` and return its mutable queue."""
    backend.routes["GET /entitlement"] = lambda f, h: (200, {
        "entitlement": sign(backend.key, claims(features=TEAM)), "plan": "pro", "status": "active",
        "features": TEAM, "seats": 5, "expires_at": int(time.time()) + HOUR})
    library = {"requests": list(requests)}
    backend.routes[QUEUE] = lambda f, h: (200 if backend._bearer(h) else 401, dict(library))
    monkeypatch.setattr(auth, "UrllibTransport", lambda timeout=None: backend)   # the daemon's Library client
    review_queue.clear()
    return library


def _save_sign_in(backend, *, away: float = 0):
    now = time.time()
    t = backend.issue()
    credentials.default_store().save({
        "access_token": t["access_token"], "refresh_token": t["refresh_token"], "base_url": backend.base,
        "access_expires_at": now - away + 900,
        "entitlement": sign(backend.key, claims(features=TEAM, iat=int(now - away), exp=int(now - away + HOUR)))})


def _quiet_watch(monkeypatch):
    for quiet in ("_purge_typed_words", "_cull_sweep", "_provider_switch_sweep"):
        monkeypatch.setattr(lifecycle, quiet, lambda: None)
    monkeypatch.setattr(lifecycle, "_keep_codex_hook", lambda settle=0.0: None)
    monkeypatch.setattr(ReadinessWatch, "sweep_safely", lambda self: None)


@pytest.mark.parametrize("away", [
    pytest.param(0, id="signed-in-just-now", marks=pytest.mark.variant(
        "a still-fresh sign-in, so no refresh runs: the watch must poll with the stored token as is")),
    pytest.param(3 * HOUR, id="after-hours-away")])
def test_a_request_on_the_library_becomes_an_inbox_card_within_a_minute(db, backend, monkeypatch, away):
    """``away``: how long ago the stored sign-in was last renewed. Past an hour both its
    access token and its entitlement have expired, as on a fresh daemon after a night."""
    _save_sign_in(backend, away=away)
    library = _library(backend, monkeypatch, [])
    _quiet_watch(monkeypatch)

    clock, appears = [0.0], 2 * lifecycle.SHARED_DOCS_EVERY      # the daemon has been up a while
    since = []

    class Server:
        should_exit = False

    async def wait(seconds):
        here = asyncio.current_task()
        await asyncio.gather(*(task for task in asyncio.all_tasks() if task is not here))   # the Library pass ends
        clock[0] += seconds
        if clock[0] >= appears and not since:
            library["requests"] = [{"doc_id": "ad_7", "title": "Cloud workers for Pro"}]   # the author asks
            since.append(db.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
        if clock[0] >= appears + 60:
            Server.should_exit = True

    asyncio.run(lifecycle._watch(Server(), lifecycle.Activity(build_app(new_token(), port=8123)), False,
                                 wait=wait, clock=lambda: clock[0]))

    cards = [(i.kind, i.doc) for i in inbox._review_items(db, None)]
    assert cards == [("review_requested", "ad_7")]
    assert bus.tail(db, since[0]), "nothing on the bus: the open app never re-reads its Inbox"


@pytest.mark.variant("first watch pass at clock 0: catches shared_due initialized 30 s ahead of start-up")
def test_a_request_already_on_the_library_shows_up_on_the_first_pass(db, backend, monkeypatch):
    """A request waiting on the server when the daemon starts is picked up on
    the very first watch pass (clock 0), not 30 s later."""
    _save_sign_in(backend)
    _library(backend, monkeypatch, [{"doc_id": "ad_7", "title": "Cloud workers for Pro"}])
    _quiet_watch(monkeypatch)
    since = db.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    passes = [0]

    class Server:
        should_exit = False

    async def wait(seconds):
        here = asyncio.current_task()
        await asyncio.gather(*(task for task in asyncio.all_tasks() if task is not here))
        passes[0] += 1
        if passes[0] >= 2:
            Server.should_exit = True

    asyncio.run(lifecycle._watch(Server(), lifecycle.Activity(build_app(new_token(), port=8123)), False,
                                 wait=wait, clock=lambda: 0.0))

    assert [(i.kind, i.doc) for i in inbox._review_items(db, None)] == [("review_requested", "ad_7")]
    assert [e["kind"] for e in bus.tail(db, since)] == ["review_queue.changed"]


def test_a_request_already_on_the_library_shows_up_right_after_sign_in(db, backend, monkeypatch):
    """Signing in through the daemon reads the queue straight away and announces
    ``review_queue.changed`` without waiting for the watch loop."""
    from brindle import company_login
    from brindle.daemon import routes_actions
    from brindle.pro import loopback

    _library(backend, monkeypatch, [{"doc_id": "ad_7", "title": "Cloud workers for Pro"}])
    backend.routes["POST /cli/token"] = lambda f, h: (200, backend.issue())

    class ApprovedCallback:
        redirect_uri = "http://127.0.0.1:49152/callback"

        def __init__(self, state):
            self.state = state

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def finish(self, result):
            pass

        def wait(self, limit):
            return loopback.CallbackResult(code="cac_" + "g" * 43)

    held = []
    monkeypatch.setattr(loopback, "CallbackServer", ApprovedCallback)
    monkeypatch.setattr(company_login, "apply_current", lambda *a, **k: None)
    monkeypatch.setattr(routes_actions, "_spawn", held.append)
    monkeypatch.setattr(routes_actions, "_SIGN_IN", [])
    since = db.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    routes_actions.account_login(db, {}, {})
    assert len(held) == 1
    held[0]()

    assert [(i.kind, i.doc) for i in inbox._review_items(db, None)] == [("review_requested", "ad_7")]
    kinds = [e["kind"] for e in bus.tail(db, since)]
    assert "review_queue.changed" in kinds and "job.done" in kinds
