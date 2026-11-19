"""The team half of the Inbox: the Library's per-person feed (sign-off progress,
shared-doc comments) and review requests with their sender, read state kept
per person in the Library."""

import pytest

from brindle import bus, inbox_team
from brindle.pro import review_queue
from brindle.service import inbox

ME = "me"
CHEN = {"id": "u_chen", "name": "Chen"}


class FakeLibrary:
    """The Library for one account: its inbox feed, and the read state it keeps per person."""

    def __init__(self, rows):
        self.rows, self.reads = rows, set()

    def __call__(self, method, path, body=None, token=None):
        assert token == "tok"
        if method == "GET" and path == inbox_team.FEED:
            return 200, {"items": [{**r, "read": r["key"] in self.reads} for r in self.rows]}
        if method == "POST" and path == inbox_team.READ:
            self.reads.add(body["key"])
            return 200, {}
        return 404, {}


def _comment(key, thread, comments, updated_at):
    return {"kind": "shared_doc_comment", "key": key, "doc_id": "doc1", "thread_id": thread,
            "title": "Engine plan", "from": CHEN, "comments": comments, "updated_at": updated_at}


SIGNOFF = {"kind": "signoff_progress", "key": "s1", "doc_id": "doc1", "title": "Engine plan",
           "from": CHEN, "signed": 2, "required": 3, "updated_at": 1.0}


@pytest.fixture(autouse=True)
def _no_cached_feeds():
    inbox_team.clear()
    review_queue.clear()
    yield
    inbox_team.clear()
    review_queue.clear()


def test_each_row_kind_carries_its_fields(db, monkeypatch):
    monkeypatch.setattr(inbox, "_my_sub", lambda: ME)
    review_queue._seen[ME] = [{"doc_id": "doc2", "title": "Ana's plan", "from": {"id": "u_ana", "name": "Ana"}}]
    lib = FakeLibrary([SIGNOFF, _comment("t1", "t1", 1, 2.0)])
    assert inbox_team.poll(db, ME, lib, "tok")

    review, = inbox._review_items(db, None)
    assert (review.kind, review.section, review.detail, review.doc) == ("review_requested", "from_team", "Ana's plan", "doc2")
    assert (review.from_person.id, review.from_person.name) == ("u_ana", "Ana")

    signoff, comment = inbox_team.items(ME)
    assert (signoff.kind, signoff.reason, signoff.section) == ("signoff_progress", "2 of 3 required", "from_team")
    assert (signoff.from_person.id, signoff.detail, signoff.item_key) == ("u_chen", "Engine plan", "s1")
    assert (comment.kind, comment.reason, comment.from_person.name) == ("shared_doc_comment", "Commented", "Chen")


def test_a_row_read_on_one_device_is_read_on_another(db):
    lib = FakeLibrary([SIGNOFF])
    assert inbox_team.poll(db, ME, lib, "tok")
    inbox_team.mark_read(db, ME, "s1", lib, "tok")      # device A
    assert bus.tail(db, 0, kinds=(inbox_team.CHANGED,))

    inbox_team.clear()                                  # device B starts with nothing read here
    inbox_team.poll(db, ME, lib, "tok")
    [item] = inbox_team.items(ME)
    assert item.read is True


def test_a_second_comment_on_a_thread_adds_no_row(db):
    lib = FakeLibrary([_comment("c1", "t1", 1, 1.0), _comment("c2", "t1", 1, 2.0)])
    inbox_team.poll(db, ME, lib, "tok")
    [item] = inbox_team.items(ME)
    assert (item.reason, item.item_key) == ("2 comments", "comment:doc1:t1")
