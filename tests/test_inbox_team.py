"""The team half of the Inbox through the daemon: the watch polls the Library's
per-person feed (GET /me/inbox) next to the review queue, GET /v1/inbox shows
its rows, and POST /v1/inbox/read marks them read in the Library, so read
state is the person's on every machine. The Library here is a fake of the
contract in the web repo's docs/shared-designs-api.md."""

import hashlib
import json
import time

import pytest
from starlette.testclient import TestClient

from brindle import autopilot, bus, inbox_team, workspaces
from brindle.daemon import lifecycle
from brindle.daemon.app import build_app
from brindle.db import Agent
from brindle.pro import review_queue
from brindle.service import inbox

ME = "u_me"
TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ANA, BO, CHEN = ({"sub": s, "name": n} for s, n in (("u_ana", "Ana"), ("u_bo", None), ("u_chen", "Chen")))


class Library:
    """The Library for one account: its review queue, its Inbox feed, and the
    read state it keeps per person (whatever machine asks)."""

    def __init__(self):
        self.queue = [{"doc_id": "ad_2", "title": "Ana's plan", "rev": 1, "author": "u_ana"},
                      {"doc_id": "ad_3", "title": "Sam's plan", "rev": 1, "author": "u_sam"}]
        self.items = [
            {"item_key": "review:ad_2", "kind": "review_requested", "created_at": 30, "doc_id": "ad_2",
             "title": "Ana's plan", "sender": ANA},
            # Ana, then Chen, signed in the same second: the Library lists a tie by key, Ana's first.
            {"item_key": "signoff:ad_1:2:u_ana", "kind": "signed_off", "created_at": 20, "doc_id": "ad_1",
             "title": "Engine plan", "rev": 2, "signer": ANA, "signed": 1, "required": 3, "waiting": [BO, CHEN]},
            {"item_key": "signoff:ad_1:2:u_chen", "kind": "signed_off", "created_at": 20, "doc_id": "ad_1",
             "title": "Engine plan", "rev": 2, "signer": CHEN, "signed": 2, "required": 3, "waiting": [BO]},
            {"item_key": "thread:ad_1:7", "kind": "shared_doc_comment", "created_at": 15, "doc_id": "ad_1",
             "title": "Engine plan", "thread_id": 7, "suggestion": False, "author": CHEN,
             "excerpt": "Does a rented VM count?", "count": 2},
        ]
        self.read, self.asked = set(), []

    def call(self, method, path, body=None, token=None):
        assert token == "tok"
        if (method, path) == ("GET", review_queue.PATH):
            return 200, {"requests": self.queue}
        if (method, path) == ("GET", inbox_team.FEED):
            return 200, {"items": [{**i, "read": i["item_key"] in self.read} for i in self.items]}
        if (method, path) == ("POST", inbox_team.READ):
            self.asked.append(body["item_keys"])
            mine = [k for k in body["item_keys"] if any(i["item_key"] == k for i in self.items)]
            self.read.update(mine)
            return 200, {"read": mine}
        return 404, {}


@pytest.fixture
def library(db, monkeypatch):
    lib = Library()
    inbox_team.clear()
    review_queue.clear()
    monkeypatch.setattr(review_queue, "account", lambda client=None, store=None: (lib, ME, "tok"))
    monkeypatch.setattr(inbox, "_my_sub", lambda: ME)
    monkeypatch.setattr(inbox, "_setup_items", lambda: [])
    monkeypatch.setattr(inbox, "_org_notices", lambda: [])
    yield lib
    inbox_team.clear()
    review_queue.clear()


@pytest.fixture
def daemon(library):
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        yield client


def _inbox(daemon, kind=None):
    r = daemon.get("/v1/inbox")
    assert r.status_code == 200, r.json()
    return [i for i in r.json() if kind is None or i["kind"] == kind]


def test_each_row_kind_carries_its_fields(db, library, daemon):
    lifecycle._poll_review_queue()
    assert bus.tail(db, 0, kinds=(inbox_team.CHANGED,))

    asked, fallback = _inbox(daemon, "review_requested")
    assert (asked["doc"], asked["inbox_group"], asked["item_key"], asked["read"]) == (
        "ad_2", "from_team", "review:ad_2", False)
    assert asked["from_person"] == {"sub": "u_ana", "name": "Ana"}
    # A request the feed doesn't carry yet: the queue's author stands in for the sender.
    assert (fallback["doc"], fallback["from_person"]["sub"], fallback["item_key"]) == ("ad_3", "u_sam", None)

    # Sign-offs come one per signer; the Inbox shows one row per design, as of the newest.
    [progress] = _inbox(daemon, "signoff_progress")
    assert (progress["doc"], progress["detail"], progress["item_key"], progress["inbox_group"]) == (
        "ad_1", "Engine plan", "signoffs:ad_1", "from_team")
    assert progress["from_person"] == {"sub": "u_chen", "name": "Chen"}
    assert progress["signoff"] == {"rev": 2, "signed": 2, "required": 3, "waiting": [{"sub": "u_bo", "name": None}]}

    [comment] = _inbox(daemon, "shared_doc_comment")
    assert (comment["item_key"], comment["thread_id"], comment["excerpt"], comment["from_person"]["sub"]) == (
        "thread:ad_1:7", 7, "Does a rented VM count?", "u_chen")
    assert (comment["reason"], comment["read"]) == ("2 comments", False)


def test_a_question_card_carries_the_question(db, repo, library, daemon):
    ws = workspaces.create(db, str(repo), "feat/ask").workspace
    db.add_agent(Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, time.time()))
    db.add_autopilot("r1")
    autopilot.need_user(db, "r1", "Which deployment target should I use?", ask=True)

    [asked] = _inbox(daemon, "question")
    assert (asked["agent_id"], asked["detail"], asked["inbox_group"]) == (
        "r1", "Which deployment target should I use?", "needs_you")


def test_rows_marked_read_stay_read_on_another_machine(db, library, daemon):
    lifecycle._poll_review_queue()
    keys = ["signoffs:ad_1", "thread:ad_1:7", "review:ad_2"]
    r = daemon.post("/v1/inbox/read", json={"item_keys": keys})
    assert r.status_code == 200 and sorted(r.json()["read"]) == sorted(keys), r.json()
    # The Library is only ever sent its own keys: the progress row's stands for both sign-offs.
    assert sorted(library.asked[0]) == sorted(
        ["signoff:ad_1:2:u_chen", "signoff:ad_1:2:u_ana", "thread:ad_1:7", "review:ad_2"])

    inbox_team.clear()                       # another machine: nothing read here yet
    review_queue.clear()
    lifecycle._poll_review_queue()
    shown = {i["kind"]: i["read"] for i in _inbox(daemon) if i["item_key"] in keys}
    assert shown == {"signoff_progress": True, "shared_doc_comment": True, "review_requested": True}


def _legacy_key(item: dict) -> str:
    """The dismissal key an Inbox card had before the team Inbox: its fields then, hashed."""
    before = ("kind", "reason", "agent_id", "workspace_id", "root_id", "detail", "notice_id", "request_id",
              "doc", "section", "fix", "permission", "action", "visual_id", "plan_offer", "url")
    return hashlib.sha256(json.dumps({k: item.get(k) for k in before}, sort_keys=True).encode()).hexdigest()


def test_a_dismissal_stored_before_the_team_inbox_still_hides_its_card(db, library, daemon):
    lifecycle._poll_review_queue()
    old = {"kind": "review_requested", "reason": "Review requested", "doc": "ad_2", "detail": "Ana's plan"}
    with db.tx() as c:
        c.execute("INSERT INTO inbox_dismissed (key) VALUES (?)", (_legacy_key(old),))
    assert [i["doc"] for i in _inbox(daemon, "review_requested")] == ["ad_3"]

    # A team row dismissed as the app shows it stays dismissed once it is read.
    [comment] = _inbox(daemon, "shared_doc_comment")
    assert daemon.post("/v1/inbox/x/dismiss", json={"item": comment}).status_code == 200
    daemon.post("/v1/inbox/read", json={"item_keys": [comment["item_key"]]})
    lifecycle._poll_review_queue()
    assert _inbox(daemon, "shared_doc_comment") == []


def test_the_inbox_group_and_a_doc_requests_section_are_independent(db, repo, library, daemon):
    ws = workspaces.create(db, str(repo), "feat/read").workspace
    db.add_agent(Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, time.time()))
    with db.tx() as c:
        c.execute("INSERT INTO doc_requests (id, agent_id, doc_id, title, section, why, created_at)"
                  " VALUES ('q1', 'r1', 'ad_1', 'Engine plan', 'Storage', 'Check the storage.', 1)")
    [doc] = _inbox(daemon, "doc")
    assert (doc["section"], doc["inbox_group"]) == ("Storage", "needs_you")
