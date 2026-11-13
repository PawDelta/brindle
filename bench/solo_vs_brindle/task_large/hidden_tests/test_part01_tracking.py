"""Part 1: enabling and disabling history."""

import pytest

from sqlite_utils import Database
from sqlite_utils.history import HistoryError


def test_history_module_exports():
    from sqlite_utils import history

    assert issubclass(history.HistoryError, Exception)
    assert issubclass(history.HistoryConflict, history.HistoryError)
    for name in ("Change", "Changeset", "RowDiff"):
        assert isinstance(getattr(history, name), type)


def test_nothing_is_tracked_by_default(db, plain):
    assert plain.has_history is False
    assert db.history_tables() == []
    assert db.history_version == 0


def test_enable_returns_the_table_and_marks_it_tracked(db, plain):
    assert plain.enable_history() is plain
    assert plain.has_history is True
    assert db.table("people").has_history is True
    assert db.history_tables() == ["people"]


def test_existing_rows_become_the_starting_point(db, plain, brief):
    plain.enable_history()
    changes = list(plain.changes())
    assert brief(changes) == [(1, "insert", 1), (2, "insert", 2), (3, "insert", 3)]
    assert changes[0].row == {"id": 1, "name": "Ann", "team": "red", "score": 9}
    assert changes[2].row == {"id": 3, "name": "Cy", "team": "red", "score": None}
    assert db.history_version == 3


def test_starting_point_follows_rowid_order(db, brief):
    table = db.table("words", pk="word")
    table.insert_all([{"word": "pear"}, {"word": "apple"}, {"word": "fig"}])
    table.enable_history()
    assert brief(table.changes()) == [
        (1, "insert", "pear"),
        (2, "insert", "apple"),
        (3, "insert", "fig"),
    ]


def test_enable_on_missing_table_raises(db):
    with pytest.raises(HistoryError):
        db.table("nope").enable_history()
    assert db.history_tables() == []


def test_enable_on_virtual_table_raises(db, plain):
    plain.enable_fts(["name"])
    with pytest.raises(HistoryError):
        db.table("people_fts").enable_history()


def test_enable_on_without_rowid_table_raises(db):
    db.execute("create table codes (code text primary key, label text) without rowid")
    with pytest.raises(HistoryError):
        db.table("codes").enable_history()
    assert db.table("codes").has_history is False


def test_internal_tables_use_the_reserved_prefix(db, plain):
    before = set(db.table_names())
    plain.enable_history()
    created = set(db.table_names()) - before
    assert created
    assert all(name.startswith("_history_") for name in created)
    for name in created:
        with pytest.raises(HistoryError):
            db.table(name).enable_history()


def test_enabling_again_keeps_history_and_records_nothing(db, people, brief):
    people.insert({"id": 4, "name": "Di", "team": "blue", "score": 1})
    before = brief(people.changes())
    assert people.enable_history() is people
    assert brief(people.changes()) == before
    assert db.history_version == 4


def test_disable_stops_recording_and_forgets_history(db, people):
    assert people.disable_history() is people
    assert people.has_history is False
    assert db.history_tables() == []
    assert people.triggers == []
    people.insert({"id": 4, "name": "Di", "team": "blue", "score": 1})
    assert people.count == 4
    assert db.history_version == 3
    with pytest.raises(HistoryError):
        list(people.changes())


def test_disable_without_history_raises(plain):
    with pytest.raises(HistoryError):
        plain.disable_history()


def test_reading_history_of_untracked_table_raises(plain):
    with pytest.raises(HistoryError):
        list(plain.changes())
    with pytest.raises(HistoryError):
        plain.history(1)


def test_versions_are_never_reused_after_disable(db, people, brief):
    people.disable_history()
    people.delete(3)
    people.enable_history()
    assert brief(people.changes()) == [(4, "insert", 1), (5, "insert", 2)]
    assert db.history_version == 5


def test_history_tables_lists_tables_in_the_order_enabled(db, plain):
    db.table("zebras", pk="id").insert({"id": 1})
    db.table("ants", pk="id").insert({"id": 1})
    db.table("zebras").enable_history()
    plain.enable_history()
    db.table("ants").enable_history()
    assert db.history_tables() == ["zebras", "people", "ants"]
    db.table("people").disable_history()
    assert db.history_tables() == ["zebras", "ants"]


def test_enable_counts_ignores_internal_tables(db, people):
    db.enable_counts()
    assert db.cached_counts() == {"people": 3}
    internal = [name for name in db.table_names() if name.startswith("_history_")]
    assert internal
    for name in internal:
        assert db.table(name).has_counts_triggers is False
    people.insert({"id": 4, "name": "Di", "team": "blue", "score": 1})
    assert db.cached_counts() == {"people": 4}


def test_history_is_stored_in_the_database_file(tmp_path, db_path, brief):
    reopened = Database(db_path)
    table = reopened.table("people")
    assert table.has_history is True
    assert reopened.history_tables() == ["people"]
    assert reopened.history_version == 3
    table.delete(2)
    reopened.close()
    again = Database(db_path)
    assert brief(again.table("people").changes())[-1] == (4, "delete", 2)
    again.close()
