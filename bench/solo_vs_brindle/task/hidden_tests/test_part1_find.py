"""Part 1: Table.find filtering, ordering and pagination."""

import pytest

from tinydb import where
from tinydb.table import Document


def names(docs):
    return [doc["name"] for doc in docs]


def test_find_without_arguments_returns_every_document_in_table_order(people):
    docs = people.find()
    assert names(docs) == ["Ann", "Bo", "Cy", "Di", "Ed", "Fi"]
    assert [doc.doc_id for doc in docs] == [1, 2, 3, 4, 5, 6]
    assert all(isinstance(doc, Document) for doc in docs)


def test_find_filters_with_a_query(people):
    docs = people.find(where("team") == "red")
    assert names(docs) == ["Ann", "Cy", "Di"]
    assert [doc.doc_id for doc in docs] == [1, 3, 4]


def test_ascending_order_puts_missing_and_null_last_and_keeps_ties_stable(people):
    # Bo and Di tie on 25; Cy has no age, Ed's age is null.
    assert names(people.find(order_by="age")) == ["Bo", "Di", "Ann", "Fi", "Cy", "Ed"]


def test_descending_order_still_puts_missing_last_and_keeps_ties_stable(people):
    assert names(people.find(order_by="-age")) == ["Fi", "Ann", "Bo", "Di", "Cy", "Ed"]


def test_later_sort_keys_break_ties_of_earlier_ones(people):
    # score: Ann 9, Bo 7, Cy 7, Ed 3; Di is null and Fi is missing, which tie.
    docs = people.find(order_by=["-score", "-name"])
    assert names(docs) == ["Ann", "Cy", "Bo", "Ed", "Fi", "Di"]
    docs = people.find(order_by=("team", "age"))
    assert names(docs) == ["Bo", "Fi", "Ed", "Di", "Ann", "Cy"]


def test_order_by_a_nested_field_path(db):
    table = db.table("places")
    table.insert_multiple([
        {"name": "a", "address": {"city": "Oslo"}},
        {"name": "b", "address": "Main St 1"},
        {"name": "c", "address": {"city": "Bergen"}},
        {"name": "d"},
        {"name": "e", "address": {"city": "Alta"}},
    ])
    assert names(table.find(order_by="address.city")) == ["e", "c", "a", "b", "d"]


def test_skip_and_limit_apply_after_filtering_and_ordering(people):
    by_name = dict(order_by="-name")
    assert names(people.find(**by_name, skip=1, limit=2)) == ["Ed", "Di"]
    assert names(people.find(where("team") == "blue", order_by="name", skip=1)) == ["Ed", "Fi"]
    assert names(people.find(**by_name, limit=3)) == ["Fi", "Ed", "Di"]
    assert people.find(limit=0) == []
    assert people.find(skip=10) == []


def test_negative_skip_or_limit_is_rejected(people):
    with pytest.raises(ValueError):
        people.find(skip=-1)
    with pytest.raises(ValueError):
        people.find(limit=-1)


def test_incomparable_values_raise_type_error(db):
    table = db.table("mixed")
    table.insert_multiple([{"v": 1}, {"v": "one"}])
    with pytest.raises(TypeError):
        table.find(order_by="v")


def test_returned_documents_are_independent_copies(db):
    table = db.table("t")
    table.insert({"name": "x", "tags": ["a"], "meta": {"n": 1}})
    first = table.find()[0]
    first["name"] = "changed"
    first["tags"].append("b")
    first["meta"]["n"] = 2
    assert table.get(doc_id=1) == {"name": "x", "tags": ["a"], "meta": {"n": 1}}
    assert table.find() == [{"name": "x", "tags": ["a"], "meta": {"n": 1}}]


def test_find_on_json_storage_gives_integer_doc_ids(json_db):
    table = json_db.table("people")
    table.insert_multiple([{"name": "a", "n": 2}, {"name": "b", "n": 1}])
    docs = table.find(order_by="n")
    assert names(docs) == ["b", "a"]
    assert [doc.doc_id for doc in docs] == [2, 1]


def test_find_works_on_the_default_table_through_the_database(cached_db):
    cached_db.insert_multiple([{"name": "z", "n": 1}, {"name": "y", "n": 2}])
    assert names(cached_db.find(order_by="-n", limit=1)) == ["y"]
