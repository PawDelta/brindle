"""Part 3: distinct, aggregate and group_by."""

import pytest

from tinydb import where
from tinydb.table import Document


def test_distinct_returns_first_seen_order_without_missing_or_null(people):
    assert people.distinct("age") == [31, 25, 40]
    assert people.distinct("team") == ["red", "blue"]


def test_distinct_handles_unhashable_values_nested_paths_and_a_query(db):
    table = db.table("t")
    table.insert_multiple([
        {"k": "a", "v": {"tags": ["x", "y"]}},
        {"k": "b", "v": {"tags": ["z"]}},
        {"k": "a", "v": {"tags": ["x", "y"]}},
        {"k": "a", "v": {"tags": {"deep": 1}}},
        {"k": "a", "v": "flat"},
    ])
    assert table.distinct("v.tags") == [["x", "y"], ["z"], {"deep": 1}]
    assert table.distinct("v.tags", where("k") == "b") == [["z"]]


def test_count_and_sum_ignore_missing_and_null(people):
    assert people.aggregate("age", "count") == 4
    assert people.aggregate("score", "sum") == 26
    assert people.aggregate("score", "sum", where("team") == "red") == 16


def test_avg_min_and_max(people):
    assert people.aggregate("age", "avg") == pytest.approx(30.25)
    assert isinstance(people.aggregate("age", "avg", where("name") == "Ann"), float)
    assert people.aggregate("age", "min") == 25
    assert people.aggregate("age", "max") == 40
    assert people.aggregate("name", "max", where("team") == "red") == "Di"


def test_aggregates_over_no_values(people):
    nobody = where("team") == "green"
    assert people.aggregate("age", "count", nobody) == 0
    assert people.aggregate("age", "sum", nobody) == 0
    assert people.aggregate("age", "avg", nobody) is None
    assert people.aggregate("age", "min", nobody) is None
    assert people.aggregate("nope", "max") is None


def test_sum_and_avg_reject_non_numbers_including_booleans(db):
    table = db.table("t")
    table.insert_multiple([{"n": 1, "flag": True, "s": "x"}, {"n": 2, "flag": False, "s": "y"}])
    for path, op in [("flag", "sum"), ("flag", "avg"), ("s", "sum"), ("s", "avg")]:
        with pytest.raises(TypeError):
            table.aggregate(path, op)
    assert table.aggregate("n", "sum") == 3


def test_unknown_operation_is_rejected_even_on_an_empty_table(db):
    with pytest.raises(ValueError):
        db.table("empty").aggregate("n", "median")


def test_group_by_groups_documents_in_table_order(people):
    groups = people.group_by("team")
    assert list(groups) == ["red", "blue"]
    assert [[d["name"] for d in docs] for docs in groups.values()] == [
        ["Ann", "Cy", "Di"],
        ["Bo", "Ed", "Fi"],
    ]
    assert [doc.doc_id for doc in groups["blue"]] == [2, 5, 6]
    assert all(isinstance(doc, Document) for doc in groups["red"])


def test_group_by_puts_missing_and_null_under_none_and_honours_a_query(people):
    groups = people.group_by("age")
    assert list(groups) == [31, 25, None, 40]
    assert [d["name"] for d in groups[None]] == ["Cy", "Ed"]
    assert [d["name"] for d in groups[25]] == ["Bo", "Di"]
    red = people.group_by("age", where("team") == "red")
    assert {k: [d["name"] for d in v] for k, v in red.items()} == {
        31: ["Ann"], None: ["Cy"], 25: ["Di"],
    }


def test_group_by_unhashable_value_raises_type_error(db):
    table = db.table("t")
    table.insert({"tags": ["a"]})
    with pytest.raises(TypeError):
        table.group_by("tags")


def test_aggregation_works_through_the_database_on_json_storage(json_db):
    json_db.insert_multiple([{"n": 1.5}, {"n": 2}, {"n": 1.5}])
    assert json_db.distinct("n") == [1.5, 2]
    assert json_db.aggregate("n", "sum") == 5
    assert list(json_db.group_by("n")) == [1.5, 2]
