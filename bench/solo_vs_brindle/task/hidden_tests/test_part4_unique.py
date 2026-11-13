"""Part 4: unique constraints."""

import pytest

from tinydb import UniqueViolation, where
from tinydb.table import Document


@pytest.fixture
def users(db):
    table = db.table("users")
    table.insert_multiple([
        {"email": "a@x", "name": "A", "profile": {"handle": "aa"}},
        {"email": "b@x", "name": "B", "profile": {"handle": "bb"}},
        {"name": "C"},
        {"email": None, "name": "D"},
    ])
    table.ensure_unique("email")
    return table


def snapshot(table):
    return [(doc.doc_id, dict(doc)) for doc in table.all()]


def test_unique_violation_is_a_value_error():
    assert issubclass(UniqueViolation, ValueError)


def test_insert_of_a_duplicate_is_rejected_and_stores_nothing(users):
    before = snapshot(users)
    with pytest.raises(UniqueViolation):
        users.insert({"email": "a@x", "name": "again"})
    assert snapshot(users) == before
    users.insert({"email": "c@x"})
    assert users.count(where("email") == "c@x") == 1


def test_missing_and_null_values_are_exempt(users):
    users.insert({"name": "E"})
    users.insert({"email": None, "name": "F"})
    assert users.count(where("name").exists()) == 6


def test_equal_values_of_different_types_and_unhashable_values_clash(db):
    table = db.table("t")
    table.ensure_unique("n")
    table.ensure_unique("tags")
    table.insert({"n": 1, "tags": ["a", "b"]})
    with pytest.raises(UniqueViolation):
        table.insert({"n": 1.0})
    with pytest.raises(UniqueViolation):
        table.insert({"tags": ["a", "b"]})
    table.insert({"n": 2, "tags": ["b", "a"]})
    assert len(table) == 2


def test_equal_hashable_and_unhashable_values_clash_in_either_order(db):
    # {1, 2} is unhashable but equals the hashable frozenset({1, 2}).
    for name, first, second in [
        ("set_first", {1, 2}, frozenset({1, 2})),
        ("frozenset_first", frozenset({1, 2}), {1, 2}),
    ]:
        table = db.table(name)
        table.ensure_unique("v")
        table.insert({"v": first})
        with pytest.raises(UniqueViolation):
            table.insert({"v": second})
        assert len(table) == 1, name


def test_nested_paths_and_several_constraints_are_each_enforced(users):
    users.ensure_unique("profile.handle")
    users.ensure_unique("profile.handle")
    with pytest.raises(UniqueViolation):
        users.insert({"email": "new@x", "profile": {"handle": "bb"}})
    with pytest.raises(UniqueViolation):
        users.insert({"email": "b@x", "profile": {"handle": "new"}})
    users.insert({"email": "new@x", "profile": {"handle": "new"}})
    assert len(users) == 5


def test_ensure_unique_on_existing_duplicates_raises_and_adds_nothing(db):
    table = db.table("t")
    table.insert_multiple([{"k": 1}, {"k": 1}])
    with pytest.raises(UniqueViolation):
        table.ensure_unique("k")
    table.insert({"k": 1})
    assert len(table) == 3


def test_insert_multiple_is_all_or_nothing(users):
    before = snapshot(users)
    with pytest.raises(UniqueViolation):
        users.insert_multiple([{"email": "c@x"}, {"email": "d@x"}, {"email": "c@x"}])
    with pytest.raises(UniqueViolation):
        users.insert_multiple([{"email": "e@x"}, {"email": "a@x"}])
    assert snapshot(users) == before


def test_update_with_a_mapping_cannot_create_a_duplicate(users):
    before = snapshot(users)
    with pytest.raises(UniqueViolation):
        users.update({"email": "a@x"}, where("name") == "B")
    with pytest.raises(UniqueViolation):
        users.update({"email": "z@x"}, doc_ids=[3, 4])
    with pytest.raises(UniqueViolation):
        users.update({"email": "same@x"})
    assert snapshot(users) == before


def test_rejected_callable_update_leaves_in_memory_documents_untouched(users):
    before = snapshot(users)

    def rename(doc):
        doc["name"] = doc["name"] + "!"
        doc["email"] = "dup@x"

    with pytest.raises(UniqueViolation):
        users.update(rename, where("name").one_of(["A", "C"]))
    with pytest.raises(UniqueViolation):
        users.update_multiple([
            ({"email": "m@x"}, where("name") == "A"),
            ({"email": "m@x"}, where("name") == "B"),
        ])
    assert snapshot(users) == before


def test_upsert_respects_the_constraint_both_ways(users):
    with pytest.raises(UniqueViolation):
        users.upsert({"email": "a@x"}, where("name") == "C")
    with pytest.raises(UniqueViolation):
        users.upsert({"email": "b@x", "name": "Z"}, where("name") == "Z")
    with pytest.raises(UniqueViolation):
        users.upsert(Document({"email": "a@x"}, doc_id=4))
    assert len(users) == 4
    assert users.get(doc_id=4) == {"email": None, "name": "D"}


def test_rewriting_a_document_with_its_own_value_is_allowed(users):
    assert users.update({"email": "a@x", "name": "A2"}, where("name") == "A") == [1]
    users.upsert({"email": "b@x"}, where("email") == "b@x")
    assert users.get(doc_id=1)["name"] == "A2"
    users.remove(where("email") == "a@x")
    users.insert({"email": "a@x"})
    assert users.count(where("email") == "a@x") == 1


def test_constraint_holds_on_json_storage_and_through_the_database(json_db):
    json_db.ensure_unique("sku")
    json_db.insert({"sku": "p1"})
    with pytest.raises(UniqueViolation):
        json_db.insert_multiple([{"sku": "p2"}, {"sku": "p1"}])
    assert [doc["sku"] for doc in json_db.all()] == ["p1"]
    default = json_db.table(json_db.default_table_name)
    with pytest.raises(UniqueViolation):
        default.insert({"sku": "p1"})
    default.insert({"sku": "p2"})
    assert sorted(doc["sku"] for doc in json_db.all()) == ["p1", "p2"]
