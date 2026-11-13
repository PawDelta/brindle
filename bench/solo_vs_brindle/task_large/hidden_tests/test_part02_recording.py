"""Part 2: what gets recorded."""

import re

import pytest

from sqlite_utils import Database


def test_insert_is_recorded(db, people):
    people.insert({"id": 4, "name": "Di", "team": "blue", "score": 1})
    change = list(people.changes())[-1]
    assert (change.version, change.table, change.op, change.pk) == (
        4,
        "people",
        "insert",
        4,
    )
    assert change.row == {"id": 4, "name": "Di", "team": "blue", "score": 1}
    assert change.changed == {}
    assert change.changeset is None
    assert db.history_version == 4


def test_insert_all_records_one_change_per_row_in_order(db, people, brief):
    people.insert_all(
        {"id": n, "name": f"p{n}", "team": "new", "score": n} for n in range(10, 260)
    )
    changes = list(people.changes(since=3))
    assert len(changes) == 250
    assert [change.version for change in changes] == list(range(4, 254))
    assert [change.pk for change in changes] == list(range(10, 260))
    assert db.history_version == 253


def test_update_is_recorded_with_the_new_row(db, people):
    people.update(2, {"score": 8, "team": "green"})
    change = list(people.changes())[-1]
    assert (change.version, change.op, change.pk) == (4, "update", 2)
    assert change.row == {"id": 2, "name": "Bo", "team": "green", "score": 8}
    assert change.changed == {"score": (7, 8), "team": ("blue", "green")}


def test_update_that_changes_nothing_is_not_recorded(db, people):
    people.update(2, {"score": 7, "name": "Bo"})
    db.execute("update people set team = team")
    assert db.history_version == 3
    assert len(list(people.changes())) == 3


def test_delete_is_recorded_with_the_last_values(db, people):
    people.update(1, {"score": 10})
    people.delete(1)
    change = list(people.changes())[-1]
    assert (change.version, change.op, change.pk) == (5, "delete", 1)
    assert change.row == {"id": 1, "name": "Ann", "team": "red", "score": 10}
    assert change.changed == {}


def test_delete_where_records_every_deleted_row(db, people, brief):
    people.delete_where("team = ?", ["red"])
    assert brief(people.changes(since=3)) == [(4, "delete", 1), (5, "delete", 3)]
    people.delete_where()
    assert brief(people.changes(since=5)) == [(6, "delete", 2)]


def test_raw_sql_writes_are_recorded(db, people, brief):
    db.execute("update people set score = coalesce(score, 0) + 1")
    db.execute("insert into people (id, name) values (9, 'Zed')")
    db.execute("delete from people where id = 2")
    assert brief(people.changes(since=3)) == [
        (4, "update", 1),
        (5, "update", 2),
        (6, "update", 3),
        (7, "insert", 9),
        (8, "delete", 2),
    ]
    assert list(people.changes())[-2].row == {
        "id": 9,
        "name": "Zed",
        "team": None,
        "score": None,
    }


def test_writes_from_another_connection_are_recorded(db_path, raw_connect, brief):
    other = raw_connect(db_path)
    other.execute("update people set name = 'Bob' where id = 2")
    other.execute("insert into people (id, name) values (4, 'Di')")
    other.commit()
    other.close()
    db = Database(db_path)
    changes = list(db.table("people").changes(since=3))
    assert brief(changes) == [(4, "update", 2), (5, "insert", 4)]
    assert changes[0].changed == {"name": ("Bo", "Bob")}
    assert changes[0].changeset is None
    db.close()


def test_upsert_records_updates_and_inserts(db, people, brief):
    people.upsert({"id": 2, "name": "Bo", "team": "blue", "score": 70}, pk="id")
    people.upsert({"id": 5, "name": "Eve", "team": "red", "score": 2}, pk="id")
    people.upsert({"id": 1, "name": "Ann", "team": "red", "score": 9}, pk="id")
    people.insert({"id": 1, "name": "Other", "team": "x", "score": 0}, ignore=True)
    assert brief(people.changes(since=3)) == [(4, "update", 2), (5, "insert", 5)]
    assert people.get(1)["name"] == "Ann"


def test_replace_is_recorded_as_delete_then_insert(db, people, brief):
    people.insert({"id": 1, "name": "Anna", "team": "red", "score": 9}, replace=True)
    changes = list(people.changes(since=3))
    assert brief(changes) == [(4, "delete", 1), (5, "insert", 1)]
    assert changes[0].row["name"] == "Ann"
    assert changes[1].row["name"] == "Anna"


def test_primary_key_change_is_recorded_as_delete_then_insert(db, people, brief):
    db.execute("update people set id = 20, score = 1 where id = 2")
    changes = list(people.changes(since=3))
    assert brief(changes) == [(4, "delete", 2), (5, "insert", 20)]
    assert changes[0].row == {"id": 2, "name": "Bo", "team": "blue", "score": 7}
    assert changes[1].row == {"id": 20, "name": "Bo", "team": "blue", "score": 1}


def test_rolled_back_writes_leave_no_trace(db, people):
    with pytest.raises(ZeroDivisionError):
        with db.atomic():
            people.insert({"id": 4, "name": "Di", "team": "blue", "score": 1})
            people.update(1, {"score": 0})
            1 / 0
    assert db.history_version == 3
    assert len(list(people.changes())) == 3
    people.update(1, {"score": 1})
    assert list(people.changes())[-1].version == 4


def test_values_are_recorded_exactly(db):
    table = db.table("samples", pk="id")
    table.create({"id": int, "i": int, "f": float, "t": str, "b": bytes, "n": str})
    table.enable_history()
    row = {
        "id": 1,
        "i": 2**53 + 1,
        "f": 0.1 + 0.2,
        "t": "naïve ✓ 'quoted'",
        "b": b"\x00\xff\x10",
        "n": None,
    }
    table.insert(row)
    recorded = list(table.changes())[0].row
    assert recorded == row
    assert [type(recorded[key]) for key in row] == [
        int,
        int,
        float,
        str,
        bytes,
        type(None),
    ]
    table.update(1, {"f": 0.5, "b": b"", "t": ""})
    assert list(table.changes())[-1].changed == {
        "f": (0.1 + 0.2, 0.5),
        "b": (b"\x00\xff\x10", b""),
        "t": ("naïve ✓ 'quoted'", ""),
    }


def test_rowid_table_uses_rowid_as_pk(db, brief):
    table = db.table("notes")
    table.insert_all([{"text": "one"}, {"text": "two"}])
    table.enable_history()
    table.insert({"text": "three"})
    db.execute("update notes set text = 'TWO' where rowid = 2")
    db.execute("delete from notes where rowid = 1")
    changes = list(table.changes())
    assert brief(changes) == [
        (1, "insert", 1),
        (2, "insert", 2),
        (3, "insert", 3),
        (4, "update", 2),
        (5, "delete", 1),
    ]
    assert changes[3].row == {"text": "TWO"}
    assert changes[4].row == {"text": "one"}


def test_compound_primary_key_is_a_tuple(db, brief):
    table = db.table("stock", pk=("shop", "item"))
    table.insert_all(
        [
            {"shop": "north", "item": "tea", "qty": 4},
            {"shop": "south", "item": "tea", "qty": 6},
        ]
    )
    table.enable_history()
    table.update(("south", "tea"), {"qty": 5})
    table.delete(("north", "tea"))
    assert brief(table.changes()) == [
        (1, "insert", ("north", "tea")),
        (2, "insert", ("south", "tea")),
        (3, "update", ("south", "tea")),
        (4, "delete", ("north", "tea")),
    ]


def test_awkward_table_and_column_names(db, brief):
    table = db.table('my "odd" table', pk="the id")
    table.insert({"the id": 1, "select": "a", "first name": "Ann", "it's": 1})
    table.enable_history()
    table.update(1, {"select": "b", "it's": 2})
    changes = list(table.changes())
    assert brief(changes) == [(1, "insert", 1), (2, "update", 1)]
    assert changes[1].table == 'my "odd" table'
    assert changes[1].row == {
        "the id": 1,
        "select": "b",
        "first name": "Ann",
        "it's": 2,
    }
    assert changes[1].changed == {"select": ("a", "b"), "it's": (1, 2)}


def test_versions_are_database_wide(db, people, brief):
    pets = db.table("pets", pk="id")
    pets.insert({"id": 1, "name": "Rex"})
    pets.enable_history()
    people.update(1, {"score": 1})
    pets.update(1, {"name": "Max"})
    people.delete(3)
    assert brief(pets.changes()) == [(4, "insert", 1), (6, "update", 1)]
    assert brief(people.changes(since=3)) == [(5, "update", 1), (7, "delete", 3)]
    assert db.history_version == 7


def test_last_pk_is_unaffected_by_recording(db, people):
    assert people.insert({"name": "Di", "team": "blue", "score": 1}).last_pk == 4
    assert people.insert({"name": "Ed", "team": "blue", "score": 2}).last_rowid == 5
    assert list(people.changes())[-1].pk == 5


def test_change_timestamps_are_utc_with_milliseconds(db, people):
    import datetime

    people.update(1, {"score": 1})
    at = list(people.changes())[-1].at
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}", at)
    recorded = datetime.datetime.strptime(at, "%Y-%m-%d %H:%M:%S.%f").replace(
        tzinfo=datetime.timezone.utc
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    assert abs((now - recorded).total_seconds()) < 120


def test_recording_coexists_with_full_text_search_triggers(db, people):
    people.enable_fts(["name"], create_triggers=True)
    people.update(2, {"name": "Roberta"})
    people.insert({"id": 4, "name": "Dinah", "team": "blue", "score": 1})
    assert [row["id"] for row in people.search("Roberta")] == [2]
    assert [row["id"] for row in people.search("Dinah")] == [4]
    assert [(c.op, c.pk) for c in people.changes(since=3)] == [
        ("update", 2),
        ("insert", 4),
    ]
