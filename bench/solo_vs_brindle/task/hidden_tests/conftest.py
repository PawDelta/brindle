"""Fixtures for the hidden acceptance tests (copied into the checkout by grade.sh)."""

import pytest

from tinydb import JSONStorage, TinyDB
from tinydb.middlewares import CachingMiddleware
from tinydb.storages import MemoryStorage

PEOPLE = [
    {"name": "Ann", "age": 31, "team": "red", "score": 9},
    {"name": "Bo", "age": 25, "team": "blue", "score": 7},
    {"name": "Cy", "team": "red", "score": 7},
    {"name": "Di", "age": 25, "team": "red", "score": None},
    {"name": "Ed", "age": None, "team": "blue", "score": 3},
    {"name": "Fi", "age": 40, "team": "blue"},
]


@pytest.fixture
def db():
    """An in-memory database; MemoryStorage hands out its live dicts."""
    return TinyDB(storage=MemoryStorage)


@pytest.fixture
def json_db(tmp_path):
    database = TinyDB(tmp_path / "db.json", storage=JSONStorage)
    yield database
    database.close()


@pytest.fixture
def cached_db():
    return TinyDB(storage=CachingMiddleware(MemoryStorage))


@pytest.fixture
def people(db):
    """A table holding PEOPLE as documents 1..6."""
    table = db.table("people")
    table.insert_multiple(PEOPLE)
    return table
