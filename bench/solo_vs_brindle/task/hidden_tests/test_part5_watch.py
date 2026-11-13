"""Part 5: change notifications with Table.watch."""

import pytest

from tinydb import where
from tinydb.table import Document


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, op, doc_id, old, new):
        self.events.append((op, doc_id, old, new))


@pytest.fixture
def table(db):
    table = db.table("items")
    table.insert_multiple([{"k": "a", "n": 1}, {"k": "b", "n": 2}])
    return table


def test_insert_and_insert_multiple_report_new_documents_in_order(table):
    rec = Recorder()
    table.watch(rec)
    table.insert({"k": "c", "n": 3})
    table.insert_multiple([{"k": "d"}, {"k": "e"}])
    assert rec.events == [
        ("insert", 3, None, {"k": "c", "n": 3}),
        ("insert", 4, None, {"k": "d"}),
        ("insert", 5, None, {"k": "e"}),
    ]
    assert all(type(e[1]) is int for e in rec.events)


def test_update_reports_old_and_new_contents(table):
    rec = Recorder()
    table.watch(rec)
    table.update({"n": 10}, where("k") == "b")
    table.update(lambda doc: doc.update(n=doc["n"] + 1), doc_ids=[1])
    assert rec.events == [
        ("update", 2, {"k": "b", "n": 2}, {"k": "b", "n": 10}),
        ("update", 1, {"k": "a", "n": 1}, {"k": "a", "n": 2}),
    ]


def test_update_reports_only_documents_whose_contents_changed(table):
    rec = Recorder()
    table.watch(rec)
    assert sorted(table.update({"n": 2})) == [1, 2]
    table.update({"k": "a"}, where("k") == "a")
    assert rec.events == [("update", 1, {"k": "a", "n": 1}, {"k": "a", "n": 2})]


def test_update_multiple_and_upsert_report_their_changes(table):
    rec = Recorder()
    table.watch(rec)
    table.update_multiple([({"n": 5}, where("k") == "a"), ({"n": 6}, where("k") == "b")])
    table.upsert({"k": "z", "n": 0}, where("k") == "z")
    table.upsert(Document({"n": 7}, doc_id=1))
    assert rec.events == [
        ("update", 1, {"k": "a", "n": 1}, {"k": "a", "n": 5}),
        ("update", 2, {"k": "b", "n": 2}, {"k": "b", "n": 6}),
        ("insert", 3, None, {"k": "z", "n": 0}),
        ("update", 1, {"k": "a", "n": 5}, {"k": "a", "n": 7}),
    ]


def test_remove_and_truncate_report_removals(table):
    table.insert({"k": "c"})
    rec = Recorder()
    table.watch(rec)
    table.remove(where("k") == "b")
    table.truncate()
    assert rec.events == [
        ("remove", 2, {"k": "b", "n": 2}, None),
        ("remove", 1, {"k": "a", "n": 1}, None),
        ("remove", 3, {"k": "c"}, None),
    ]


def test_unwatch_stops_notifications_and_is_idempotent(table):
    first, second = Recorder(), Recorder()
    unwatch = table.watch(first)
    table.watch(second)
    table.insert({"k": "c"})
    unwatch()
    unwatch()
    table.remove(doc_ids=[1])
    assert [e[0] for e in first.events] == ["insert"]
    assert [e[0] for e in second.events] == ["insert", "remove"]


def test_several_watchers_are_called_in_registration_order(table):
    calls = []
    table.watch(lambda op, doc_id, old, new: calls.append(("first", op, doc_id)))
    table.watch(lambda op, doc_id, old, new: calls.append(("second", op, doc_id)))
    table.insert({"k": "c"})
    assert calls == [("first", "insert", 3), ("second", "insert", 3)]


def test_watchers_only_hear_about_their_own_table(db, table):
    rec = Recorder()
    table.watch(rec)
    other = db.table("other")
    other.insert({"k": "x"})
    other.truncate()
    db.insert({"k": "y"})
    assert rec.events == []


def test_callback_sees_the_new_state_even_through_a_cached_search(table):
    query = where("n") == 99
    assert table.search(query) == []
    seen = []

    def callback(op, doc_id, old, new):
        seen.append((table.search(query), table.get(doc_id=doc_id), len(table)))

    table.watch(callback)
    table.update({"n": 99}, doc_ids=[2])
    assert seen == [([{"k": "b", "n": 99}], {"k": "b", "n": 99}, 2)]


def test_payloads_are_copies_that_cannot_change_the_database(db):
    table = db.table("nested")
    table.insert({"tags": ["a"], "meta": {"v": 1}})

    def vandal(op, doc_id, old, new):
        for doc in (old, new):
            if doc is not None:
                doc["tags"].append("vandal")
                doc["meta"]["v"] = -1
                doc["extra"] = True

    table.watch(vandal)
    table.update({"meta": {"v": 2}})
    table.insert({"tags": ["b"], "meta": {"v": 3}})
    assert table.all() == [
        {"tags": ["a"], "meta": {"v": 2}},
        {"tags": ["b"], "meta": {"v": 3}},
    ]


def test_failed_writes_report_nothing(table):
    rec = Recorder()
    table.watch(rec)
    with pytest.raises(ValueError):
        table.insert_multiple([{"k": "d"}, "not a mapping"])
    with pytest.raises(ValueError):
        table.insert(Document({"k": "dup"}, doc_id=1))

    def zero_then_fail(doc):
        if doc["k"] == "b":
            raise RuntimeError("update failed half way")
        doc["n"] = 0

    with pytest.raises(RuntimeError):
        table.update(zero_then_fail)
    assert rec.events == []
    assert table.all() == [{"k": "a", "n": 1}, {"k": "b", "n": 2}]


def test_watch_works_on_json_storage_through_the_database(json_db):
    rec = Recorder()
    json_db.watch(rec)
    json_db.insert({"v": 1})
    json_db.update({"v": 2})
    json_db.remove(doc_ids=[1])
    assert rec.events == [
        ("insert", 1, None, {"v": 1}),
        ("update", 1, {"v": 1}, {"v": 2}),
        ("remove", 1, {"v": 2}, None),
    ]
