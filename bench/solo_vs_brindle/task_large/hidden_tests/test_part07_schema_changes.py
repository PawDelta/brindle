"""Part 7: history keeps working when the table's schema changes."""

import sqlite3

import pytest

ANN = {"id": 1, "name": "Ann", "team": "red", "score": 9}
BO = {"id": 2, "name": "Bo", "team": "blue", "score": 7}
CY = {"id": 3, "name": "Cy", "team": "red", "score": None}


def in_step(table):
    """History's idea of the present must equal the live table."""
    db = table.db
    pks = ", ".join(f'"{pk}"' for pk in table.pks)
    live = list(table.rows_where(order_by=pks))
    return list(table.as_of(db.history_version)) == live


def test_add_column_is_tracked_from_then_on(db, people):
    people.add_column("city", str)
    assert db.history_version == 3
    assert people.history_columns == ["id", "name", "team", "score", "city"]
    people.update(1, {"city": "Oslo"})
    change = list(people.changes())[-1]
    assert change.changed == {"city": (None, "Oslo")}
    assert change.row == dict(ANN, city="Oslo")


def test_added_column_is_none_in_earlier_versions(db, people):
    people.add_column("city", str)
    people.update(1, {"city": "Oslo"})
    assert list(people.as_of(3)) == [
        dict(ANN, city=None),
        dict(BO, city=None),
        dict(CY, city=None),
    ]
    assert people.get_as_of(1, 4)["city"] == "Oslo"


def test_add_column_with_default_records_the_new_values(db, people, brief):
    people.add_column("active", int, not_null_default=1)
    assert brief(people.changes(since=3)) == [
        (4, "update", 1),
        (5, "update", 2),
        (6, "update", 3),
    ]
    assert list(people.changes())[-1].changed == {"active": (None, 1)}
    assert in_step(people)


def test_insert_with_alter_tracks_the_new_column(db, people):
    people.insert({"id": 4, "name": "Di", "team": "blue", "score": 1, "city": "Rome"}, alter=True)
    change = list(people.changes())[-1]
    assert (change.op, change.pk) == ("insert", 4)
    assert change.row["city"] == "Rome"
    assert in_step(people)


def test_transform_rename_keeps_values_under_the_new_name(db, people):
    people.update(2, {"team": "green"})
    people.transform(rename={"team": "squad"})
    assert db.history_version == 4
    assert people.history_columns == ["id", "name", "squad", "score"]
    assert people.get_as_of(2, 3) == {"id": 2, "name": "Bo", "squad": "blue", "score": 7}
    assert list(people.changes())[3].changed == {"squad": ("blue", "green")}
    people.update(2, {"squad": "gold"})
    assert list(people.changes())[-1].changed == {"squad": ("green", "gold")}


def test_transform_drop_removes_the_column_from_history(db, people):
    people.transform(drop=["team"])
    assert db.history_version == 3
    assert people.history_columns == ["id", "name", "score"]
    assert list(people.as_of(2)) == [
        {"id": 1, "name": "Ann", "score": 9},
        {"id": 2, "name": "Bo", "score": 7},
    ]
    people.update(1, {"score": 1})
    assert list(people.changes())[-1].row == {"id": 1, "name": "Ann", "score": 1}


def test_transform_reorders_columns_in_history(db, people):
    people.transform(column_order=["id", "score", "team", "name"])
    assert list(next(people.as_of(1))) == ["id", "score", "team", "name"]
    assert list(list(people.changes())[0].row) == ["id", "score", "team", "name"]
    assert db.history_version == 3


def test_transform_that_converts_values_records_them(db):
    table = db.table("readings", pk="id")
    table.insert_all([{"id": 1, "value": "5"}, {"id": 2, "value": "n/a"}, {"id": 3, "value": "7"}])
    table.enable_history()
    table.transform(types={"value": int})
    changes = list(table.changes(since=3))
    assert [(c.op, c.pk) for c in changes] == [("update", 1), ("update", 3)]
    assert changes[0].changed == {"value": ("5", 5)}
    assert in_step(table)


def test_transform_can_add_a_primary_key_to_a_rowid_table(db, brief):
    table = db.table("notes")
    table.insert_all([{"id": 10, "text": "one"}, {"id": 20, "text": "two"}])
    table.enable_history()
    db.execute("update notes set text = 'TWO' where id = 20")
    table.transform(pk="id")
    assert db.history_version == 3
    assert brief(table.history(20)) == [(2, "insert", 20), (3, "update", 20)]
    table.update(10, {"text": "ONE"})
    assert brief(table.history(10)) == [(1, "insert", 10), (4, "update", 10)]
    assert table.revert(20, 2) is True
    assert table.get(20)["text"] == "two"


def test_transform_keep_table_copy_is_not_tracked(db, people):
    people.transform(rename={"team": "squad"}, keep_table="people_old")
    db.execute("update people_old set score = 0")
    db.execute("delete from people_old where id = 3")
    assert db.history_version == 3
    assert db.table("people_old").has_history is False
    assert db.history_tables() == ["people"]
    people.update(1, {"score": 1})
    assert db.history_version == 4


def test_failed_transform_leaves_tracking_working(db, people):
    with pytest.raises(sqlite3.IntegrityError):
        people.transform(not_null={"score"})
    assert people.has_history is True
    people.update(3, {"score": 3})
    assert list(people.changes())[-1].changed == {"score": (None, 3)}
    assert db.history_version == 4


def test_extract_keeps_history_in_step(db, people):
    people.extract("team")
    assert "team_id" in people.history_columns
    assert "team" not in people.history_columns
    assert in_step(people)
    people.update(1, {"score": 0})
    assert list(people.changes())[-1].changed == {"score": (9, 0)}


def test_add_foreign_key_keeps_history_in_step(db, people):
    db.table("teams", pk="name").insert_all([{"name": "red"}, {"name": "blue"}])
    people.add_foreign_key("team", "teams", "name")
    assert db.history_version == 3
    assert people.has_history is True
    assert in_step(people)
    people.update(1, {"team": "blue"})
    assert list(people.changes())[-1].changed == {"team": ("red", "blue")}


def test_rename_table_takes_its_history_along(db, people, brief):
    people.update(1, {"score": 1})
    db.rename_table("people", "folk")
    folk = db.table("folk")
    assert folk.has_history is True
    assert db.history_tables() == ["folk"]
    assert db.table("people").has_history is False
    assert brief(folk.changes())[-1] == (4, "update", 1)
    assert {change.table for change in folk.changes()} == {"folk"}
    folk.delete(2)
    assert brief(db.changes(since=4)) == [(5, "delete", 2)]
    assert list(folk.as_of(3)) == [ANN, BO, CY]


def test_failed_rename_keeps_tracking(db, people):
    db.table("taken").insert({"id": 1})
    with pytest.raises(sqlite3.OperationalError):
        db.rename_table("people", "taken")
    assert db.history_tables() == ["people"]
    people.update(1, {"score": 1})
    assert db.history_version == 4


def test_drop_discards_the_history(db, people, brief):
    people.drop()
    assert db.history_tables() == []
    assert list(db.changes()) == []
    again = db.table("people", pk="id")
    again.insert({"id": 1, "name": "New"})
    assert again.has_history is False
    again.enable_history()
    assert brief(again.history(1)) == [(4, "insert", 1)]


def test_enable_again_picks_up_columns_added_with_raw_sql(db, people):
    db.execute("alter table people add column city text")
    people.update(1, {"city": "Oslo"})
    assert db.history_version == 3
    people.enable_history()
    assert people.history_columns == ["id", "name", "team", "score", "city"]
    assert [(c.version, c.op, c.pk) for c in people.changes(since=3)] == [
        (4, "update", 1)
    ]
    assert in_step(people)
    people.update(2, {"city": "Rome"})
    assert list(people.changes())[-1].changed == {"city": (None, "Rome")}


def test_history_keeps_working_before_a_raw_added_column_is_picked_up(db, people):
    db.execute("alter table people add column city text default 'Oslo'")
    assert people.history_columns == ["id", "name", "team", "score"]
    assert [change.row for change in people.changes()] == [ANN, BO, CY]
    assert list(people.as_of(3)) == [ANN, BO, CY]
    assert people.get_as_of(2, 3) == BO
    people.update(2, {"score": 70, "city": "Rome"})
    change = list(people.changes())[-1]
    assert (change.version, change.row) == (4, dict(BO, score=70))
    assert change.changed == {"score": (7, 70)}
    assert [(d.pk, d.before, d.after) for d in people.diff(3)] == [
        (2, BO, dict(BO, score=70))
    ]
    assert people.revert(2, 3) is True
    assert people.get(2) == dict(BO, city="Rome")


def test_enable_again_reconciles_untracked_writes(db, people):
    for trigger in people.triggers:
        db.execute(f'drop trigger "{trigger.name}"')
    db.execute("delete from people where id = 1")
    db.execute("update people set score = 70 where id = 2")
    db.execute("insert into people (id, name) values (4, 'Di')")
    assert db.history_version == 3
    people.enable_history()
    assert [(c.version, c.op, c.pk) for c in people.changes(since=3)] == [
        (4, "delete", 1),
        (5, "update", 2),
        (6, "insert", 4),
    ]
    assert in_step(people)
    people.update(4, {"score": 4})
    assert db.history_version == 7
