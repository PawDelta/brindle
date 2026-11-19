"""A design's share link (brindle.pro.share_links) against a fake Library."""

import threading

import pytest
from starlette.testclient import TestClient

from brindle import plans, visual_offers
from brindle.daemon.app import build_app
from brindle.pro import analysis_docs as ad
from brindle.pro import auth, credentials, share_links
from brindle.service.errors import ServiceError
from pro_fixtures import backend, pro_env, signing_key  # noqa: F401 - fixtures
from test_plans import AUTH, DECISIONS, TOKEN, session  # noqa: F401 - fixture
from test_pro_remote_learning import login_as

DOC = "doc_1"
LINK = f"/analysis-docs/{DOC}/design/share-link"
URL = "https://pawdelta.com/api/brindle/v1/share-links/dsl_abc"


class Library:
    """One design's link: at most one, a POST replacing it, a DELETE ending it."""

    def __init__(self, remote):
        self.link, self.made = None, 0
        remote.routes.update({f"GET {LINK}": lambda f, h: (200, {"link": self.link}),
                              f"POST {LINK}": self.create,
                              f"DELETE {LINK}": self.revoke})

    def create(self, form, headers):
        self.made += 1
        self.link = {"url": f"{URL}{self.made}", "token": f"dsl_abc{self.made}", "created_by": "user_1",
                     "created_at": 1793491200, "access": "anyone"}
        return 200, {"link": self.link}

    def revoke(self, form, headers):
        self.link = None
        return 200, {"link": None}


@pytest.fixture
def parts(tmp_path, backend):
    def signed_in(features):
        store = credentials.FileStore(tmp_path / "pro")
        login_as(backend, store, features=features)
        return {"client": auth.Client(None, backend), "store": store}
    return signed_in


def _link_calls(backend):
    return [p for p in backend.paths() if p.endswith(LINK)]


def test_copy_makes_one_link_reuses_it_and_revoke_turns_it_off(parts, backend):
    lib, kw = Library(backend), parts(("analysis_docs",))
    assert share_links.read(DOC, **kw) == {"available": True, "link": None}

    first = share_links.copy(DOC, **kw)["link"]
    assert first["url"] == f"{URL}1"
    assert share_links.copy(DOC, **kw)["link"]["url"] == first["url"]   # the same link: making one would replace it
    assert lib.made == 1

    assert share_links.revoke(DOC, **kw)["link"] is None
    assert lib.link is None and share_links.read(DOC, **kw)["link"] is None
    assert _link_calls(backend).count(f"DELETE {LINK}") == 1


def test_overlapping_copies_take_turns_and_get_one_link(parts, backend):
    """The first copy is held inside its read of the link (which saw none) until
    the second is queued behind it, or has finished on its own; then the first
    goes on. Overlapping, the second would make a link and the first another."""
    lib, kw = Library(backend), parts(("analysis_docs",))
    entered, release, finished = threading.Event(), threading.Event(), []

    def first_read(form, headers):
        seen = lib.link
        if not entered.is_set():
            entered.set()
            release.wait()
        return 200, {"link": seen}

    backend.routes[f"GET {LINK}"] = first_read
    urls: list[str] = []
    turns = share_links.TURNS

    def copy():
        urls.append(share_links.copy(DOC, **kw)["link"]["url"])

    def second():
        copy()
        with turns.cond:
            finished.append(True)
            turns.cond.notify_all()

    a = threading.Thread(target=copy)
    a.start()
    entered.wait()
    b = threading.Thread(target=second)
    b.start()
    with turns.cond:
        turns.cond.wait_for(lambda: turns.queued.get(DOC, 0) >= 1 or finished)
    release.set()
    a.join()
    b.join()
    assert lib.made == 1 and urls == [f"{URL}1", f"{URL}1"]


def test_the_plan_reader_finds_its_link_by_plan_id_only_for_the_account_that_shared_it(
        db, session, tmp_path, backend, monkeypatch):
    boss, _ = session
    store = credentials.FileStore(tmp_path / "pro")
    login_as(backend, store, features=("analysis_docs",))
    monkeypatch.setattr(ad, "_session", lambda *a: (auth.Client(None, backend), store))
    lib = Library(backend)
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = plans.propose(db, boss, "# Cloud workers", DECISIONS,
                        sections=[{"id": "summary", "title": "Summary", "markdown": "Workers run here."}])["id"]
    with db.tx() as c:
        c.execute("INSERT INTO plan_shares (plan_id, doc_id, owner) VALUES (?, ?, 'u_other')", (pid, DOC))
    route = f"/v1/plans/{pid}/share-link"
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        assert app.get(route).json() == {"available": False, "link": None}     # another account's share
        assert app.post(route, json={}).status_code == 409
        assert _link_calls(backend) == []
        with db.tx() as c:
            c.execute("UPDATE plan_shares SET owner='user_1' WHERE plan_id=?", (pid,))
        assert app.post(route, json={}).json()["link"]["url"] == f"{URL}1"
        assert app.delete(route).json()["link"] is None
    assert lib.link is None


def test_free_is_refused_before_anything_is_sent(parts, backend):
    Library(backend)
    kw = parts(("learning",))
    assert share_links.read(DOC, **kw) == {"available": False, "link": None}
    for act in (share_links.copy, share_links.revoke):
        with pytest.raises(ServiceError) as e:
            act(DOC, **kw)
        assert e.value.code == "not_entitled"
    assert _link_calls(backend) == []


def test_an_orgs_link_policy_refusal_keeps_the_librarys_reason(parts, backend):
    Library(backend)
    why = "an organization that holds this design does not allow share links"
    backend.routes[f"POST {LINK}"] = lambda f, h: (403, {"error": "share_links_off", "error_description": why})
    with pytest.raises(ServiceError) as e:
        share_links.copy(DOC, **parts(("analysis_docs",)))
    assert (e.value.code, e.value.message, e.value.http_status) == ("share_links_off", why, 403)
