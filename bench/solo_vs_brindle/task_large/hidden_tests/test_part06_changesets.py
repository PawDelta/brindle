"""Part 6: changesets and undo."""

import re

import pytest

from sqlite_utils import Migrations
from sqlite_utils.history import Changeset, HistoryConflict, HistoryError

ANN = {"id": 1, "name": "Ann", "team": "red", "score": 9}
BO = {"id": 2, "name": "Bo", "team": "blue", "score": 7}
CY = {"id": 3, "name": "Cy", "team": "red", "score": None}
DI = {"id": 4, "name": "Di", "team": "blue", "score": 1}


def rows(table):
    return list(table.rows_where(order_by="id"))


@pytest.fixture
def edited(db, people):
    """One changeset that updates Ann, deletes Bo and inserts Di (versions 4-6)."""
    with db.changeset("Spring tidy", author="amy") as changeset:
        people.update(1, {"score": 10})
        people.delete(2)
        people.insert(DI)
    return changeset


def test_changes_inside_the_block_belong_to_the_changeset(db, people, edited):
    assert isinstance(edited, Changeset)
    people.update(3, {"score": 3})
    tags = [(c.version, c.changeset) for c in people.changes()]
    assert tags == [
        (1, None),
        (2, None),
        (3, None),
        (4, edited.id),
        (5, edited.id),
        (6, edited.id),
        (7, None),
    ]


def test_changesets_are_listed_with_their_details(db, people, edited):
    with db.changeset("Second") as second:
        people.update(3, {"score": 1})
    listed = db.changesets()
    assert [c.id for c in listed] == [edited.id, second.id]
    assert second.id > edited.id
    first = listed[0]
    assert (first.message, first.author) == ("Spring tidy", "amy")
    assert (listed[1].message, listed[1].author) == ("Second", None)
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}", first.created_at)
    assert first == edited


def test_changeset_covers_every_table_and_raw_sql(db, people):
    pets = db.table("pets", pk="id")
    pets.insert({"id": 1, "name": "Rex"})
    pets.enable_history()
    with db.changeset("mixed") as changeset:
        db.execute("update people set team = 'grey' where id = 1")
        pets.insert({"id": 2, "name": "Tom"})
    tagged = [(c.table, c.op, c.pk) for c in db.changes(changeset=changeset.id)]
    assert tagged == [("people", "update", 1), ("pets", "insert", 2)]
    assert [c.pk for c in pets.changes(changeset=changeset.id)] == [2]
    assert list(people.changes(changeset=changeset.id + 50)) == []


def test_failed_block_rolls_back_changes_and_changeset(db, people):
    with pytest.raises(RuntimeError):
        with db.changeset("doomed"):
            people.update(1, {"score": 0})
            people.insert(DI)
            raise RuntimeError("stop")
    assert rows(people) == [ANN, BO, CY]
    assert db.changesets() == []
    assert db.history_version == 3
    people.update(1, {"score": 1})
    assert list(people.changes())[-1].changeset is None


def test_changeset_without_changes_is_discarded(db, people):
    with db.changeset("nothing happened"):
        people.update(1, {"score": 9})
    assert db.changesets() == []
    with db.changeset("real"):
        people.update(1, {"score": 8})
    assert [c.message for c in db.changesets()] == ["real"]


def test_changesets_cannot_be_nested(db, people):
    with pytest.raises(HistoryError):
        with db.changeset("outer"):
            people.update(1, {"score": 0})
            with db.changeset("inner"):
                people.update(2, {"score": 0})
    assert rows(people) == [ANN, BO, CY]
    assert db.changesets() == []


def test_changeset_on_database_without_history(db, plain):
    with db.changeset("no tracked tables"):
        plain.update(1, {"score": 0})
    assert db.changesets() == []
    assert plain.get(1)["score"] == 0
    assert db.history_tables() == []


def test_undo_reverses_inserts_updates_and_deletes(db, people, edited, brief):
    assert rows(people) == [dict(ANN, score=10), CY, DI]
    undo = db.undo_changeset(edited.id)
    assert rows(people) == [ANN, BO, CY]
    assert isinstance(undo, Changeset)
    assert undo.message == f"Undo changeset {edited.id}"
    assert undo.id != edited.id
    assert [c.id for c in db.changesets()] == [edited.id, undo.id]
    undone = list(people.changes(changeset=undo.id))
    assert sorted((c.op, c.pk) for c in undone) == [
        ("delete", 4),
        ("insert", 2),
        ("update", 1),
    ]
    assert [c.version for c in undone] == [7, 8, 9]


def test_undo_spans_tables(db, people):
    pets = db.table("pets", pk="id")
    pets.insert({"id": 1, "name": "Rex"})
    pets.enable_history()
    with db.changeset("both") as changeset:
        people.delete(3)
        pets.update(1, {"name": "Max"})
        pets.insert({"id": 2, "name": "Tom"})
    db.undo_changeset(changeset.id)
    assert rows(people) == [ANN, BO, CY]
    assert rows(pets) == [{"id": 1, "name": "Rex"}]


def test_undo_goes_back_to_before_the_first_change_in_the_changeset(db, people):
    people.update(1, {"score": 5})
    with db.changeset("several steps") as changeset:
        people.update(1, {"score": 6})
        people.update(1, {"score": 7, "team": "gold"})
        people.insert(DI)
        people.update(4, {"score": 2})
    db.undo_changeset(changeset.id)
    assert people.get(1) == dict(ANN, score=5)
    assert people.count == 3


def test_undo_leaves_other_rows_alone_and_records_the_author(db, people, edited):
    people.update(3, {"score": 33})
    undo = db.undo_changeset(edited.id, author="bea")
    assert people.get(3)["score"] == 33
    assert undo.author == "bea"
    assert db.changesets()[-1].author == "bea"


def test_undo_refuses_to_overwrite_later_changes(db, people, edited):
    people.update(1, {"team": "green"})
    before = db.history_version
    with pytest.raises(HistoryConflict) as excinfo:
        db.undo_changeset(edited.id)
    assert excinfo.value.conflicts == [("people", 1)]
    assert rows(people) == [dict(ANN, score=10, team="green"), CY, DI]
    assert db.history_version == before
    assert len(db.changesets()) == 1


def test_undo_conflict_when_a_deleted_row_was_recreated(db, people, edited):
    people.insert(dict(BO, name="Other Bo"))
    with pytest.raises(HistoryConflict) as excinfo:
        db.undo_changeset(edited.id)
    assert excinfo.value.conflicts == [("people", 2)]


def test_forced_undo_overwrites_later_changes(db, people, edited):
    people.update(1, {"team": "green"})
    undo = db.undo_changeset(edited.id, force=True)
    assert rows(people) == [ANN, BO, CY]
    assert undo is not None


def test_undo_of_unknown_changeset_raises(db, people, edited):
    with pytest.raises(HistoryError):
        db.undo_changeset(edited.id + 100)


def test_undoing_twice_conflicts_and_undoing_the_undo_redoes(db, people, edited):
    undo = db.undo_changeset(edited.id)
    with pytest.raises(HistoryConflict):
        db.undo_changeset(edited.id)
    redo = db.undo_changeset(undo.id)
    assert rows(people) == [dict(ANN, score=10), CY, DI]
    assert redo.message == f"Undo changeset {undo.id}"


def test_undo_that_changes_nothing_returns_none(db, people):
    with db.changeset("there and back") as changeset:
        people.update(1, {"score": 1})
        people.update(1, {"score": 9})
    assert db.undo_changeset(changeset.id) is None
    assert [c.id for c in db.changesets()] == [changeset.id]


def test_migrations_run_inside_changesets(db, people):
    migrations = Migrations("crm")

    @migrations()
    def rename_red_team(db):
        db.execute("update people set team = 'crimson' where team = 'red'")

    @migrations()
    def add_index(db):
        db.table("people").create_index(["team"])

    migrations.apply(db)
    listed = db.changesets()
    assert [c.message for c in listed] == ["Migration crm:rename_red_team"]
    tagged = [(c.op, c.pk) for c in db.changes(changeset=listed[0].id)]
    assert tagged == [("update", 1), ("update", 3)]
    assert [m.name for m in migrations.applied(db)] == ["rename_red_team", "add_index"]
    db.undo_changeset(listed[0].id)
    assert people.get(1)["team"] == "red"


def test_migrations_leave_databases_without_history_alone(db, plain):
    migrations = Migrations("crm")

    @migrations()
    def bump(db):
        db.execute("update people set score = 0")

    migrations.apply(db)
    assert plain.get(1)["score"] == 0
    assert not [name for name in db.table_names() if name.startswith("_history_")]
    assert db.changesets() == []


def test_migration_inside_an_open_changeset_joins_it(db, people):
    migrations = Migrations("crm")

    @migrations()
    def bump(db):
        db.execute("update people set score = 0 where id = 1")

    with db.changeset("deploy 42") as changeset:
        migrations.apply(db)
        people.update(2, {"score": 0})
    assert [c.message for c in db.changesets()] == ["deploy 42"]
    assert [c.pk for c in db.changes(changeset=changeset.id)] == [1, 2]
