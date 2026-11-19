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


@pytest.mark.parametrize("away", [0, 3 * HOUR], ids=["signed-in-just-now", "after-hours-away"])
def test_a_request_on_the_library_becomes_an_inbox_card_within_a_minute(db, backend, monkeypatch, away):
    """``away``: how long ago the stored sign-in was last renewed. Past an hour both its
    access token and its entitlement have expired, as on a fresh daemon after a night."""
    now = time.time()
    t = backend.issue()
    credentials.default_store().save({
        "access_token": t["access_token"], "refresh_token": t["refresh_token"], "base_url": backend.base,
        "access_expires_at": now - away + 900,
        "entitlement": sign(backend.key, claims(features=TEAM, iat=int(now - away), exp=int(now - away + HOUR)))})
    backend.routes["GET /entitlement"] = lambda f, h: (200, {
        "entitlement": sign(backend.key, claims(features=TEAM)), "plan": "pro", "status": "active",
        "features": TEAM, "seats": 5, "expires_at": int(time.time()) + HOUR})
    library = {"requests": []}
    backend.routes[QUEUE] = lambda f, h: (200 if backend._bearer(h) else 401, dict(library))
    monkeypatch.setattr(auth, "UrllibTransport", lambda timeout=None: backend)   # the daemon's Library client
    for quiet in ("_purge_typed_words", "_cull_sweep", "_provider_switch_sweep"):
        monkeypatch.setattr(lifecycle, quiet, lambda: None)
    monkeypatch.setattr(lifecycle, "_keep_codex_hook", lambda settle=0.0: None)
    monkeypatch.setattr(ReadinessWatch, "sweep_safely", lambda self: None)
    review_queue.clear()

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
