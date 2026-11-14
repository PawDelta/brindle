"""Fixtures for the hidden acceptance tests (copied into the checkout by grade.sh)."""

import sqlite3

import pytest

from sqlite_utils import Database

PEOPLE = [
    {"id": 1, "name": "Ann", "team": "red", "score": 9},
    {"id": 2, "name": "Bo", "team": "blue", "score": 7},
    {"id": 3, "name": "Cy", "team": "red", "score": None},
]


@pytest.fixture
def db():
    database = Database(memory=True)
    yield database
    database.close()


@pytest.fixture
def plain(db):
    """The people table (primary key id, three rows) without history."""
    table = db.table("people", pk="id")
    table.insert_all(PEOPLE)
    return table


@pytest.fixture
def people(plain):
    """The people table with history enabled: versions 1-3 are its three rows."""
    plain.enable_history()
    return plain


@pytest.fixture
def db_path(tmp_path):
    """Path of a database file holding the tracked people table."""
    path = str(tmp_path / "data.db")
    database = Database(path)
    database.table("people", pk="id").insert_all(PEOPLE)
    database.table("people").enable_history()
    database.close()
    return path


@pytest.fixture
def brief():
    """Reduce changes to (version, op, pk) tuples."""

    def _brief(changes):
        return [(change.version, change.op, change.pk) for change in changes]

    return _brief


@pytest.fixture
def raw_connect():
    """Open a plain sqlite3 connection that knows nothing about sqlite-utils."""
    connections = []

    def _connect(path):
        connection = sqlite3.connect(path)
        connections.append(connection)
        return connection

    yield _connect
    for connection in connections:
        connection.close()
