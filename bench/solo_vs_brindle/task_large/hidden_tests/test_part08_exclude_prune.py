"""Part 8: excluded columns and pruning."""

import pytest

from sqlite_utils.db import NotFoundError
from sqlite_utils.history import HistoryConflict, HistoryError

ANN = {"id": 1, "name": "Ann", "team": "red", "score": 9}
BO = {"id": 2, "name": "Bo", "team": "blue", "score": 7}
CY = {"id": 3, "name": "Cy", "team": "red", "score": None}


@pytest.fixture
def partial(db, plain):
    """people with the score column left out of its history."""
    plain.enable_history(exclude=["score"])
    return plain


@pytest.fixture
def busy(db, people):
    """Versions 4-8: update Bo, delete Ann, insert Di, update Bo, update Cy."""
    people.update(2, {"score": 8})
    people.delete(1)
    people.insert({"id": 4, "name": "Di", "team": "blue", "score": 1})
    people.update(2, {"team": "green"})
    people.update(3, {"score": 3})
    return people


def test_history_columns_lists_what_is_recorded(db, partial):
    assert partial.history_columns == ["id", "name", "team"]
    pets = db.table("pets", pk="id")
    pets.insert({"id": 1, "name": "Rex"})
    assert pets.enable_history().history_columns == ["id", "name"]


def test_excluded_column_is_left_out_of_changes_and_time_travel(db, partial):
    partial.update(1, {"name": "Annie", "score": 10})
    change = list(partial.changes())[-1]
    assert change.row == {"id": 1, "name": "Annie", "team": "red"}
    assert change.changed == {"name": ("Ann", "Annie")}
    assert list(partial.as_of(3))[0] == {"id": 1, "name": "Ann", "team": "red"}
    assert partial.get_as_of(1, 4) == {"id": 1, "name": "Annie", "team": "red"}


def test_writes_to_excluded_columns_only_are_not_recorded(db, partial):
    partial.update(1, {"score": 10})
    db.execute("update people set score = 0")
    assert db.history_version == 3


def test_restoring_leaves_excluded_columns_alone(db, partial):
    partial.update(1, {"name": "Annie", "score": 10})
    partial.update(2, {"team": "green", "score": 20})
    assert partial.revert(1, 3) is True
    assert partial.get(1) == dict(ANN, score=10)
    assert partial.restore(3) == 1
    assert partial.get(2) == dict(BO, score=20)


def test_reinserted_row_gets_the_default_for_excluded_columns(db, partial):
    partial.delete(2)
    partial.revert(2, 3)
    assert partial.get(2) == dict(BO, score=None)


def test_invalid_exclude_is_rejected(db, plain):
    with pytest.raises(HistoryError):
        plain.enable_history(exclude=["id"])
    with pytest.raises(HistoryError):
        plain.enable_history(exclude=["nope"])
    assert plain.has_history is False
    notes = db.table("notes")
    notes.insert({"text": "one", "tag": "a"})
    with pytest.raises(HistoryError):
        notes.enable_history(exclude=["text", "tag"])
    assert db.history_tables() == []


def test_enabling_again_without_exclude_keeps_the_setting(db, partial):
    partial.enable_history()
    assert partial.history_columns == ["id", "name", "team"]
    partial.update(1, {"score": 10})
    assert db.history_version == 3


def test_enabling_again_can_start_recording_a_column(db, partial, brief):
    partial.enable_history(exclude=[])
    assert partial.history_columns == ["id", "name", "team", "score"]
    assert brief(partial.changes(since=3)) == [(4, "update", 1), (5, "update", 2)]
    assert list(partial.changes())[3].changed == {"score": (None, 9)}
    assert list(partial.as_of(db.history_version)) == [ANN, BO, CY]
    assert list(partial.as_of(3))[0] == dict(ANN, score=None)


def test_enabling_again_can_stop_recording_a_column(db, people):
    people.update(1, {"team": "green"})
    people.enable_history(exclude=["team", "score"])
    assert people.history_columns == ["id", "name"]
    assert db.history_version == 4
    assert list(people.as_of(4)) == [
        {"id": 1, "name": "Ann"},
        {"id": 2, "name": "Bo"},
        {"id": 3, "name": "Cy"},
    ]
    people.update(1, {"team": "blue"})
    assert db.history_version == 4


def test_renamed_excluded_column_stays_excluded(db, partial):
    partial.transform(rename={"score": "points"})
    assert partial.history_columns == ["id", "name", "team"]
    partial.update(1, {"points": 1})
    assert db.history_version == 3


def test_prune_squashes_older_changes_into_a_starting_point(db, busy, brief):
    assert busy.history_floor == 0
    removed = busy.prune_history(6)
    assert removed == 3
    assert busy.history_floor == 6
    changes = list(busy.changes())
    assert brief(changes) == [
        (3, "insert", 3),
        (4, "insert", 2),
        (6, "insert", 4),
        (7, "update", 2),
        (8, "update", 3),
    ]
    assert changes[1].row == dict(BO, score=8)
    assert changes[1].changed == {}
    assert changes[3].changed == {"team": ("blue", "green")}
    assert db.history_version == 8


def test_time_travel_still_works_from_the_floor_onwards(db, busy):
    expected = {version: list(busy.as_of(version)) for version in (6, 7, 8)}
    busy.prune_history(6)
    for version, rows in expected.items():
        assert list(busy.as_of(version)) == rows
    assert busy.get_as_of(2, 6) == dict(BO, score=8)
    assert [(d.pk, d.op) for d in busy.diff(6, 8)] == [(2, "update"), (3, "update")]


def test_versions_before_the_floor_are_out_of_reach(db, busy):
    busy.prune_history(6)
    with pytest.raises(HistoryError):
        list(busy.as_of(5))
    with pytest.raises(HistoryError):
        busy.get_as_of(2, 5)
    with pytest.raises(HistoryError):
        busy.diff(5, 8)
    with pytest.raises(HistoryError):
        busy.revert(2, 5)
    with pytest.raises(HistoryError):
        busy.restore(0)
    assert busy.count == 3


def test_rows_deleted_before_the_floor_are_forgotten(db, busy):
    busy.prune_history(6)
    assert busy.history(1) == []
    with pytest.raises(NotFoundError):
        busy.get_as_of(1, 6)


def test_restoring_to_the_floor_works(db, busy):
    busy.prune_history(6)
    assert busy.restore(6) == 2
    assert busy.get(2) == dict(BO, score=8)
    assert busy.get(3) == CY


def test_prune_never_moves_the_floor_back(db, busy):
    assert busy.prune_history(6) == 3
    assert busy.prune_history(4) == 0
    assert busy.history_floor == 6
    assert busy.prune_history(6) == 0


def test_prune_beyond_the_newest_version_stops_at_it(db, busy, brief):
    busy.prune_history(1000)
    assert busy.history_floor == 8
    assert brief(busy.changes()) == [(6, "insert", 4), (7, "insert", 2), (8, "insert", 3)]
    busy.update(4, {"score": 2})
    assert brief(busy.changes())[-1] == (9, "update", 4)
    assert list(busy.as_of(9)) == list(busy.rows_where(order_by="id"))


def test_pruned_changeset_cannot_be_undone(db, people):
    with db.changeset("old") as old:
        people.update(1, {"score": 1})
    with db.changeset("new") as new:
        people.update(2, {"score": 2})
    people.prune_history(4)
    with pytest.raises(HistoryError):
        db.undo_changeset(old.id)
    assert people.get(1)["score"] == 1
    db.undo_changeset(new.id)
    assert people.get(2)["score"] == 7


def test_changeset_with_changes_pruned_away_cannot_be_undone(db, people):
    with db.changeset("old") as old:
        people.update(1, {"score": 1})
    people.update(1, {"score": 2})
    people.prune_history(5)
    with pytest.raises(HistoryError) as excinfo:
        db.undo_changeset(old.id, force=True)
    assert not isinstance(excinfo.value, HistoryConflict)
    assert people.get(1)["score"] == 2
    assert db.history_version == 5


def test_prune_only_affects_its_own_table(db, busy):
    pets = db.table("pets", pk="id")
    pets.insert({"id": 1, "name": "Rex"})
    pets.enable_history()
    pets.update(1, {"name": "Max"})
    busy.prune_history(10)
    assert pets.history_floor == 0
    assert [c.version for c in pets.changes()] == [9, 10]
    assert list(pets.as_of(9)) == [{"id": 1, "name": "Rex"}]
