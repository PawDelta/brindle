"""Part 5: restoring rows and tables."""

import sqlite3

import pytest

ANN = {"id": 1, "name": "Ann", "team": "red", "score": 9}
BO = {"id": 2, "name": "Bo", "team": "blue", "score": 7}
CY = {"id": 3, "name": "Cy", "team": "red", "score": None}


def test_revert_puts_old_values_back(db, people, brief):
    people.update(2, {"score": 8, "team": "green"})
    people.update(2, {"name": "Bob"})
    assert people.revert(2, 3) is True
    assert people.get(2) == BO
    last = list(people.changes())[-1]
    assert (last.version, last.op, last.pk) == (6, "update", 2)
    assert last.changed == {
        "name": ("Bob", "Bo"),
        "team": ("green", "blue"),
        "score": (8, 7),
    }
    assert people.revert(2, 4) is True
    assert people.get(2) == dict(BO, score=8, team="green")


def test_revert_when_nothing_differs_returns_false(db, people):
    people.update(1, {"score": 1})
    assert people.revert(2, 3) is False
    assert people.revert(1, 4) is False
    assert db.history_version == 4


def test_revert_brings_back_a_deleted_row(db, people, brief):
    people.delete(1)
    assert people.revert(1, 3) is True
    assert people.get(1) == ANN
    assert brief(people.history(1)) == [
        (1, "insert", 1),
        (4, "delete", 1),
        (5, "insert", 1),
    ]


def test_revert_removes_a_row_that_did_not_exist_yet(db, people, brief):
    people.insert({"id": 4, "name": "Di", "team": "blue", "score": 1})
    assert people.revert(4, 3) is True
    assert people.count == 3
    assert brief(people.history(4)) == [(4, "insert", 4), (5, "delete", 4)]


def test_revert_of_a_row_that_never_existed_does_nothing(db, people):
    assert people.revert(99, 3) is False
    assert db.history_version == 3


def test_revert_keeps_the_rowid_of_a_rowid_table(db):
    table = db.table("notes")
    table.insert_all([{"text": "one"}, {"text": "two"}, {"text": "three"}])
    table.enable_history()
    db.execute("delete from notes where rowid = 2")
    assert table.revert(2, 3) is True
    assert list(db.query("select rowid, text from notes order by rowid")) == [
        {"rowid": 1, "text": "one"},
        {"rowid": 2, "text": "two"},
        {"rowid": 3, "text": "three"},
    ]


def test_revert_with_compound_primary_key(db):
    table = db.table("stock", pk=("shop", "item"))
    table.insert({"shop": "north", "item": "tea", "qty": 4})
    table.enable_history()
    table.update(("north", "tea"), {"qty": 0})
    assert table.revert(("north", "tea"), 1) is True
    assert table.get(("north", "tea"))["qty"] == 4


def test_restore_puts_the_whole_table_back(db, people):
    people.update(2, {"score": 8})
    people.delete(1)
    people.insert({"id": 4, "name": "Di", "team": "blue", "score": 1})
    people.update(3, {"team": "blue"})
    assert people.restore(3) == 4
    assert list(people.rows_where(order_by="id")) == [ANN, BO, CY]
    assert db.history_version == 11
    assert list(people.as_of(db.history_version)) == [ANN, BO, CY]
    assert [row["id"] for row in people.as_of(7)] == [2, 3, 4]


def test_restore_only_touches_rows_that_differ(db, people, brief):
    people.update(2, {"score": 8})
    assert people.restore(3) == 1
    assert brief(people.changes(since=4)) == [(5, "update", 2)]
    assert people.restore(db.history_version) == 0
    assert db.history_version == 5


def test_restore_to_version_zero_empties_the_table(db, people):
    assert people.restore(0) == 3
    assert people.count == 0
    assert [c.op for c in people.changes(since=3)] == ["delete"] * 3


def test_restore_can_be_undone_by_restoring_again(db, people):
    people.update(1, {"name": "Annie"})
    people.delete(3)
    after_edits = db.history_version
    people.restore(3)
    people.restore(after_edits)
    assert list(people.rows_where(order_by="id")) == [dict(ANN, name="Annie"), BO]


def test_failed_restore_changes_nothing(db, people):
    people.update(1, {"score": 0})
    people.delete(2)
    db.execute(
        "create trigger no_bo before insert on people when new.id = 2 "
        "begin select raise(abort, 'Bo is not welcome'); end"
    )
    before = db.history_version
    with pytest.raises(sqlite3.DatabaseError):
        people.restore(3)
    assert list(people.rows_where(order_by="id")) == [dict(ANN, score=0), CY]
    assert db.history_version == before
