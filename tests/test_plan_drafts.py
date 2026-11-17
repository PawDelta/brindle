"""brindle.plan_drafts: this machine's replica of a shared doc's live draft."""

import pytest

from brindle.crdt import SharedDoc
from brindle.db import DB
from brindle.plan_drafts import DraftGone, DraftReplica, StaleDraft, drop

LIBRARY = SharedDoc.new({"intro": "Hello"}).export_snapshot()


def written_by(replica: DraftReplica, peer: int) -> int:
    return SharedDoc.load(replica.export_updates()).written_by(peer)


@pytest.mark.variant("Restarted replicas must retain peer identity and merge concurrent edits without renumbering authorship")
def test_replica_reloaded_after_restart_keeps_its_peer_id_and_merges(db, tmp_path):
    a = DraftReplica.open(db, "doc1", LIBRARY)
    b = DraftReplica.open(DB(str(tmp_path / "other-machine.db")), "doc1", LIBRARY)
    assert a.peer_id != b.peer_id
    b.apply_remote(a.insert("intro", 5, " world"))

    db.conn.close()                                     # the daemon restarts
    a2 = DraftReplica.open(DB(), "doc1")
    assert a2.peer_id == a.peer_id
    assert a2.text("intro") == "Hello world"

    from_a = a2.insert("intro", 0, ">")                 # typed concurrently
    from_b = b.insert("intro", 11, "!")
    b.apply_remote(from_a)
    a2.apply_remote(from_b)

    assert a2.text("intro") == b.text("intro") == ">Hello world!"
    # " world" then ">": seven letters under one peer id, none renumbered.
    assert written_by(b, a.peer_id) == 7


@pytest.mark.variant("An update received before its dependency must survive a restart and merge when the dependency arrives")
def test_updates_received_out_of_order_merge_across_a_restart(db, tmp_path):
    a = DraftReplica.open(db, "doc1", LIBRARY)
    first, second = a.insert("intro", 5, "1"), a.insert("intro", 6, "2")
    other = str(tmp_path / "other-machine.db")

    assert DraftReplica.open(DB(other), "doc1", LIBRARY).apply_remote(second) is False
    assert DraftReplica.open(DB(other), "doc1").apply_remote(first) is True    # after a restart

    assert DraftReplica.open(DB(other), "doc1").text("intro") == "Hello12"


def test_a_holder_that_missed_a_write_edits_only_what_it_read_and_a_dropped_replica_is_not_reused(db):
    first = DraftReplica.open(db, "doc1", LIBRARY)
    second = DraftReplica.open(DB(), "doc1")             # another process, same replica
    first.insert("intro", 0, ">")
    with pytest.raises(StaleDraft):
        second.delete("intro", 1, 1)                     # "e" in the "Hello" it read
    assert second.text("intro") == ">Hello"              # nothing written; it has the latest now
    second.delete("intro", 2, 1)

    assert DraftReplica.open(DB(), "doc1").text("intro") == ">Hllo"
    assert written_by(second, first.peer_id) == 2

    assert drop(db, "doc1")
    with pytest.raises(DraftGone):
        first.insert("intro", 0, "x")
    again = DraftReplica.open(db, "doc1", LIBRARY)
    assert again.peer_id != first.peer_id
    assert again.text("intro") == "Hello"
