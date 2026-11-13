"""Part 4: time travel."""

import pytest

from sqlite_utils.db import NotFoundError
from sqlite_utils.history import HistoryError, RowDiff

ANN = {"id": 1, "name": "Ann", "team": "red", "score": 9}
BO = {"id": 2, "name": "Bo", "team": "blue", "score": 7}
CY = {"id": 3, "name": "Cy", "team": "red", "score": None}
DI = {"id": 4, "name": "Di", "team": "blue", "score": 1}


@pytest.fixture
def busy(db, people):
    """Version 4 updates Bo, 5 deletes Ann, 6 inserts Di, 7 updates Bo again."""
    people.update(2, {"score": 8})
    people.delete(1)
    people.insert(DI)
    people.update(2, {"team": "green"})
    return people


def test_as_of_rebuilds_every_version(busy):
    bo_8 = dict(BO, score=8)
    expected = {
        0: [],
        1: [ANN],
        2: [ANN, BO],
        3: [ANN, BO, CY],
        4: [ANN, bo_8, CY],
        5: [bo_8, CY],
        6: [bo_8, CY, DI],
        7: [dict(bo_8, team="green"), CY, DI],
    }
    for version, rows in expected.items():
        assert list(busy.as_of(version)) == rows, version


def test_as_of_current_or_later_version_matches_the_table(db, busy):
    assert list(busy.as_of(db.history_version)) == list(busy.rows)
    assert list(busy.as_of(db.history_version + 100)) == list(busy.rows)


def test_as_of_is_not_changed_by_later_writes(db, busy):
    before = list(busy.as_of(4))
    busy.delete_where()
    busy.insert(dict(ANN, name="Annie"))
    assert list(busy.as_of(4)) == before


def test_as_of_orders_rows_by_primary_key(db):
    table = db.table("items", pk="id")
    table.insert({"id": 30, "label": "c"})
    table.enable_history()
    table.insert({"id": 10, "label": "a"})
    table.insert({"id": 20, "label": "b"})
    assert [row["id"] for row in table.as_of(3)] == [10, 20, 30]
    assert [row["id"] for row in table.as_of(2)] == [10, 30]


def test_as_of_keeps_exact_values(db):
    table = db.table("samples", pk="id")
    table.create({"id": int, "f": float, "b": bytes, "t": str})
    table.enable_history()
    row = {"id": 1, "f": 1e-7 / 3, "b": b"\x01\x00\x02", "t": "ünï"}
    table.insert(row)
    table.update(1, {"f": 2.0, "b": None})
    assert list(table.as_of(1)) == [row]
    assert isinstance(next(table.as_of(1))["b"], bytes)
    assert list(table.as_of(2)) == [{"id": 1, "f": 2.0, "b": None, "t": "ünï"}]


def test_as_of_for_rowid_table(db):
    table = db.table("notes")
    table.insert_all([{"text": "one"}, {"text": "two"}])
    table.enable_history()
    db.execute("delete from notes where rowid = 1")
    table.insert({"text": "three"})
    assert list(table.as_of(2)) == [{"text": "one"}, {"text": "two"}]
    assert list(table.as_of(4)) == [{"text": "two"}, {"text": "three"}]


def test_as_of_without_history_raises(plain):
    with pytest.raises(HistoryError):
        list(plain.as_of(1))


def test_get_as_of_returns_the_row_at_that_version(busy):
    assert busy.get_as_of(2, 3) == BO
    assert busy.get_as_of(2, 4) == dict(BO, score=8)
    assert busy.get_as_of(1, 4) == ANN


def test_get_as_of_raises_when_the_row_did_not_exist(busy):
    with pytest.raises(NotFoundError):
        busy.get_as_of(1, 5)
    with pytest.raises(NotFoundError):
        busy.get_as_of(4, 5)
    with pytest.raises(NotFoundError):
        busy.get_as_of(99, 7)


def test_get_as_of_with_compound_primary_key(db):
    table = db.table("stock", pk=("shop", "item"))
    table.insert({"shop": "north", "item": "tea", "qty": 4})
    table.enable_history()
    table.update(("north", "tea"), {"qty": 3})
    assert table.get_as_of(("north", "tea"), 1)["qty"] == 4
    assert table.get_as_of(("north", "tea"), 2)["qty"] == 3


def test_diff_reports_inserted_updated_and_deleted_rows(busy):
    diffs = busy.diff(3, 7)
    assert all(isinstance(row_diff, RowDiff) for row_diff in diffs)
    assert [(d.pk, d.op) for d in diffs] == [
        (1, "delete"),
        (2, "update"),
        (4, "insert"),
    ]
    deleted, updated, inserted = diffs
    assert (deleted.before, deleted.after) == (ANN, None)
    assert updated.before == BO
    assert updated.after == {"id": 2, "name": "Bo", "team": "green", "score": 8}
    assert (inserted.before, inserted.after) == (None, DI)


def test_diff_defaults_to_the_current_version(busy):
    assert busy.diff(6) == busy.diff(6, 7)
    assert [(d.pk, d.op) for d in busy.diff(6)] == [(2, "update")]
    assert busy.diff(7) == []


def test_diff_backwards_swaps_before_and_after(busy):
    forwards = busy.diff(3, 7)
    backwards = busy.diff(7, 3)
    assert [(d.pk, d.op) for d in backwards] == [
        (1, "insert"),
        (2, "update"),
        (4, "delete"),
    ]
    assert [(d.before, d.after) for d in backwards] == [
        (d.after, d.before) for d in forwards
    ]


def test_diff_leaves_out_rows_that_end_up_the_same(db, busy):
    start = db.history_version
    busy.update(3, {"score": 5})
    busy.update(3, {"score": None})
    busy.insert({"id": 8, "name": "Tmp", "team": "x", "score": 0})
    busy.delete(8)
    assert db.history_version == start + 4
    assert busy.diff(start) == []
    assert [(d.pk, d.op) for d in busy.diff(start, start + 1)] == [(3, "update")]
