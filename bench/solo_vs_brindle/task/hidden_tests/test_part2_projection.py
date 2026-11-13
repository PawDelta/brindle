"""Part 2: projection with find(fields=...)."""

from tinydb import where
from tinydb.table import Document

ADA = {
    "name": "Ada",
    "age": 36,
    "address": {"city": "London", "zip": "W1", "geo": {"lat": 51.5, "lon": -0.1}},
    "tags": ["math", "engines"],
}


def test_projection_keeps_only_listed_top_level_fields_and_doc_id(db):
    table = db.table("t")
    table.insert({"x": 0})
    table.insert(ADA)
    docs = table.find(where("name") == "Ada", fields=["name", "tags"])
    assert docs == [{"name": "Ada", "tags": ["math", "engines"]}]
    assert isinstance(docs[0], Document)
    assert docs[0].doc_id == 2


def test_nested_paths_keep_their_nesting_and_merge_under_a_parent(db):
    table = db.table("t")
    table.insert(ADA)
    [doc] = table.find(fields=["address.city", "address.geo.lat", "age"])
    assert doc == {"address": {"city": "London", "geo": {"lat": 51.5}}, "age": 36}


def test_missing_fields_are_left_out_but_null_fields_are_kept(db):
    table = db.table("t")
    table.insert_multiple([
        {"name": "a", "nick": None, "address": "Main St 1"},
        {"other": 1},
    ])
    docs = table.find(fields=["name", "nick", "address.city"])
    assert docs == [{"name": "a", "nick": None}, {}]
    assert [doc.doc_id for doc in docs] == [1, 2]


def test_ordering_and_filtering_use_fields_that_are_not_projected(people):
    docs = people.find(where("team") == "blue", order_by="-age", fields=["name"])
    assert docs == [{"name": "Fi"}, {"name": "Bo"}, {"name": "Ed"}]
    assert [doc.doc_id for doc in docs] == [6, 2, 5]


def test_projection_combines_with_pagination(people):
    docs = people.find(order_by="name", skip=4, fields=("name", "team"))
    assert docs == [{"name": "Ed", "team": "blue"}, {"name": "Fi", "team": "blue"}]


def test_projected_documents_are_independent_copies(db):
    table = db.table("t")
    table.insert(ADA)
    [doc] = table.find(fields=["address.geo", "tags"])
    doc["address"]["geo"]["lat"] = 0
    doc["tags"].append("x")
    stored = table.get(doc_id=1)
    assert stored["address"]["geo"] == {"lat": 51.5, "lon": -0.1}
    assert stored["tags"] == ["math", "engines"]


def test_projection_leaves_the_stored_document_whole(json_db):
    table = json_db.table("t")
    table.insert(ADA)
    assert table.find(fields=["name"]) == [{"name": "Ada"}]
    assert table.all() == [ADA]
