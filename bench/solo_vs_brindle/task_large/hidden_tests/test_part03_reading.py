"""Part 3: reading the recorded changes."""

import datetime

import pytest


@pytest.fixture
def busy(db, people):
    """people after versions 4-8: update 2, delete 1, insert 4, update 2, update 4."""
    people.update(2, {"score": 8})
    people.delete(1)
    people.insert({"id": 4, "name": "Di", "team": "blue", "score": 1})
    people.update(2, {"team": "green", "score": 9})
    people.update(4, {"name": "Dee"})
    return people


def test_changes_are_listed_oldest_first(busy, brief):
    assert brief(busy.changes()) == [
        (1, "insert", 1),
        (2, "insert", 2),
        (3, "insert", 3),
        (4, "update", 2),
        (5, "delete", 1),
        (6, "insert", 4),
        (7, "update", 2),
        (8, "update", 4),
    ]


def test_since_is_exclusive_and_until_is_inclusive(busy):
    assert [c.version for c in busy.changes(since=5)] == [6, 7, 8]
    assert [c.version for c in busy.changes(until=2)] == [1, 2]
    assert [c.version for c in busy.changes(since=3, until=6)] == [4, 5, 6]
    assert list(busy.changes(since=8)) == []
    assert list(busy.changes(since=6, until=6)) == []


def test_changed_only_lists_columns_that_differ(busy):
    by_version = {change.version: change for change in busy.changes()}
    assert by_version[7].changed == {"team": ("blue", "green"), "score": (8, 9)}
    assert by_version[8].changed == {"name": ("Di", "Dee")}
    assert by_version[5].changed == {}
    assert by_version[6].changed == {}


def test_changed_reports_values_going_to_and_from_null(db, people):
    people.update(3, {"score": 4})
    people.update(3, {"score": None, "team": None})
    first, second = list(people.changes(since=3))
    assert first.changed == {"score": (None, 4)}
    assert second.changed == {"score": (4, None), "team": ("red", None)}


def test_history_lists_one_rows_changes(busy, brief):
    assert brief(busy.history(2)) == [(2, "insert", 2), (4, "update", 2), (7, "update", 2)]
    assert brief(busy.history(4)) == [(6, "insert", 4), (8, "update", 4)]
    assert isinstance(busy.history(2), list)
    assert busy.history(99) == []


def test_history_of_a_deleted_row_is_still_available(busy, brief):
    history = busy.history(1)
    assert brief(history) == [(1, "insert", 1), (5, "delete", 1)]
    assert history[-1].row["name"] == "Ann"


def test_history_with_compound_primary_key(db, brief):
    table = db.table("stock", pk=("shop", "item"))
    table.insert_all(
        [
            {"shop": "north", "item": "tea", "qty": 4},
            {"shop": "north", "item": "jam", "qty": 2},
        ]
    )
    table.enable_history()
    table.update(("north", "jam"), {"qty": 1})
    assert brief(table.history(("north", "jam"))) == [
        (2, "insert", ("north", "jam")),
        (3, "update", ("north", "jam")),
    ]
    assert brief(table.history(["north", "tea"])) == [(1, "insert", ("north", "tea"))]


def test_database_changes_merge_every_tracked_table(db, busy):
    pets = db.table("pets", pk="id")
    pets.insert({"id": 1, "name": "Rex"})
    pets.enable_history()
    busy.update(3, {"score": 3})
    pets.update(1, {"name": "Max"})
    merged = [(c.version, c.table, c.op, c.pk) for c in db.changes(since=7)]
    assert merged == [
        (8, "people", "update", 4),
        (9, "pets", "insert", 1),
        (10, "people", "update", 3),
        (11, "pets", "update", 1),
    ]
    assert [c.version for c in db.changes()] == list(range(1, 12))
    assert [c.version for c in db.changes(since=2, until=4)] == [3, 4]


def test_database_changes_supports_polling(db, busy):
    seen = db.history_version
    assert list(db.changes(since=seen)) == []
    busy.insert({"id": 7, "name": "Gus", "team": "red", "score": 0})
    fresh = list(db.changes(since=seen))
    assert [(c.table, c.op, c.pk) for c in fresh] == [("people", "insert", 7)]


def test_database_without_any_history_reads_as_empty(db, plain):
    assert list(db.changes()) == []
    assert db.history_version == 0
    assert db.history_version_at("2999-01-01") == 0


def test_history_version_at_bounds(db, busy):
    assert db.history_version_at("2999-01-01") == 8
    assert db.history_version_at("1999-01-01 00:00:00") == 0
    assert db.history_version_at(datetime.datetime(2999, 1, 1)) == 8
    aware = datetime.datetime(1999, 1, 1, tzinfo=datetime.timezone.utc)
    assert db.history_version_at(aware) == 0
    last = list(busy.changes())[-1].at
    assert db.history_version_at(last) == 8


def test_history_version_at_rejects_garbage(db, busy):
    with pytest.raises(ValueError):
        db.history_version_at("not a timestamp")
