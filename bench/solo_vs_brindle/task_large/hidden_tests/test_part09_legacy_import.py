"""Part 9: importing history recorded by the sqlite-history plugin."""

import pytest

# 2024-01-02 03:04:05.678 UTC, as milliseconds since the Unix epoch
T0 = 1704164645678
MINUTE = 60_000

LEGACY_SCHEMA = """
CREATE TABLE docs (id INTEGER PRIMARY KEY, title TEXT, body TEXT);
CREATE TABLE _docs_history (
    _rowid INTEGER,
    id INTEGER,
    title TEXT,
    body TEXT,
    _version INTEGER,
    _updated INTEGER,
    _mask INTEGER
);
CREATE INDEX idx_docs_history_rowid ON _docs_history (_rowid);
CREATE TRIGGER docs_insert_history AFTER INSERT ON docs
BEGIN
    INSERT INTO _docs_history (_rowid, id, title, body, _version, _updated, _mask)
    VALUES (new.rowid, new.id, new.title, new.body, 1, 0, 7);
END;
CREATE TRIGGER docs_update_history AFTER UPDATE ON docs
BEGIN
    INSERT INTO _docs_history (_rowid, id, title, body, _version, _updated, _mask)
    VALUES (new.rowid, null, new.title, new.body, 2, 0, 6);
END;
CREATE TRIGGER docs_delete_history AFTER DELETE ON docs
BEGIN
    INSERT INTO _docs_history (_rowid, id, title, body, _version, _updated, _mask)
    VALUES (old.rowid, old.id, old.title, old.body, 3, 0, -1);
END;
"""

# (_rowid, id, title, body, _version, _updated, _mask) in the order written.
# Mask bits: 1 = id, 2 = title, 4 = body.
LEGACY_RECORDS = [
    (1, 1, "Intro", "hello", 1, T0, 7),
    (2, 2, "Usage", "run it", 1, T0 + MINUTE, 7),
    (1, None, "Introduction", None, 2, T0 + 2 * MINUTE, 2),
    (2, None, None, None, 2, T0 + 3 * MINUTE, 4),
    (3, 3, "Draft", "tbd", 1, T0 + 4 * MINUTE, 7),
    (3, 3, "Draft", "tbd", 2, T0 + 5 * MINUTE, -1),
    (1, None, "Intro again", "hi", 3, T0 + 6 * MINUTE, 6),
]

LIVE_ROWS = [
    {"id": 1, "title": "Intro again", "body": "hi"},
    {"id": 2, "title": "Usage", "body": None},
]


@pytest.fixture
def legacy(db):
    """A docs table tracked the old way, with seven legacy records."""
    db.executescript(LEGACY_SCHEMA)
    db.conn.executemany(
        "insert into _docs_history values (?, ?, ?, ?, ?, ?, ?)", LEGACY_RECORDS
    )
    for trigger in ("insert", "update", "delete"):
        db.execute(f"drop trigger docs_{trigger}_history")
    db.conn.executemany(
        "insert into docs (id, title, body) values (:id, :title, :body)", LIVE_ROWS
    )
    db.conn.commit()
    # Put the triggers back the way the plugin left them
    for statement in LEGACY_SCHEMA.split("CREATE TRIGGER")[1:]:
        db.execute("CREATE TRIGGER" + statement.rsplit(";", 1)[0])
    return db.table("docs")


def test_legacy_records_become_changes_in_the_order_written(db, legacy, brief):
    legacy.enable_history()
    assert brief(legacy.changes()) == [
        (1, "insert", 1),
        (2, "insert", 2),
        (3, "update", 1),
        (4, "update", 2),
        (5, "insert", 3),
        (6, "delete", 3),
        (7, "update", 1),
    ]
    assert db.history_version == 7
    assert all(change.changeset is None for change in legacy.changes())


def test_legacy_timestamps_are_kept(db, legacy):
    legacy.enable_history()
    stamps = [change.at for change in legacy.changes()]
    assert stamps[0] == "2024-01-02 03:04:05.678"
    assert stamps[1] == "2024-01-02 03:05:05.678"
    assert stamps[6] == "2024-01-02 03:10:05.678"


def test_partial_update_records_are_filled_in_from_earlier_ones(db, legacy):
    legacy.enable_history()
    changes = list(legacy.changes())
    assert changes[2].row == {"id": 1, "title": "Introduction", "body": "hello"}
    assert changes[2].changed == {"title": ("Intro", "Introduction")}
    assert changes[6].row == {"id": 1, "title": "Intro again", "body": "hi"}
    assert changes[6].changed == {
        "title": ("Introduction", "Intro again"),
        "body": ("hello", "hi"),
    }


def test_masked_null_is_a_real_change_to_null(db, legacy):
    legacy.enable_history()
    change = list(legacy.changes())[3]
    assert change.row == {"id": 2, "title": "Usage", "body": None}
    assert change.changed == {"body": ("run it", None)}


def test_legacy_delete_keeps_the_last_values(db, legacy):
    legacy.enable_history()
    change = list(legacy.changes())[5]
    assert (change.op, change.pk) == ("delete", 3)
    assert change.row == {"id": 3, "title": "Draft", "body": "tbd"}


def test_time_travel_through_imported_history(db, legacy):
    legacy.enable_history()
    assert list(legacy.as_of(2)) == [
        {"id": 1, "title": "Intro", "body": "hello"},
        {"id": 2, "title": "Usage", "body": "run it"},
    ]
    assert [row["id"] for row in legacy.as_of(5)] == [1, 2, 3]
    assert list(legacy.as_of(7)) == LIVE_ROWS
    assert legacy.revert(1, 1) is True
    assert legacy.get(1) == {"id": 1, "title": "Intro", "body": "hello"}


def test_legacy_objects_are_removed_after_import(db, legacy):
    legacy.enable_history()
    assert "_docs_history" not in db.table_names()
    names = {trigger.name for trigger in db.triggers}
    assert not names & {
        "docs_insert_history",
        "docs_update_history",
        "docs_delete_history",
    }
    index_names = {
        row[0] for row in db.execute("select name from sqlite_master where type = 'index'")
    }
    assert "idx_docs_history_rowid" not in index_names
    legacy.enable_history()
    assert db.history_version == 7
    legacy.insert({"id": 9, "title": "New", "body": "x"})
    assert [(c.version, c.op, c.pk) for c in legacy.changes(since=7)] == [
        (8, "insert", 9)
    ]


def test_version_at_uses_imported_timestamps(db, legacy):
    legacy.enable_history()
    assert db.history_version_at("2024-01-02 03:04:05.677") == 0
    assert db.history_version_at("2024-01-02 03:04:05.678") == 1
    assert db.history_version_at("2024-01-02T03:06:00") == 2
    assert db.history_version_at("2024-01-02 04:07:05.678+01:00") == 4
    assert db.history_version_at("2024-01-02") == 0
    assert db.history_version_at("2024-01-03") == 7


def test_live_table_wins_where_legacy_history_is_stale(db, legacy):
    for trigger in ("insert", "update", "delete"):
        db.execute(f"drop trigger docs_{trigger}_history")
    db.execute("update docs set body = 'edited' where id = 2")
    db.execute("insert into docs (id, title, body) values (4, 'Extra', 'e')")
    legacy.enable_history()
    assert [(c.version, c.op, c.pk) for c in legacy.changes(since=7)] == [
        (8, "update", 2),
        (9, "insert", 4),
    ]
    assert list(legacy.changes())[7].changed == {"body": (None, "edited")}
    assert list(legacy.as_of(db.history_version)) == list(
        legacy.rows_where(order_by="id")
    )


def test_import_can_be_declined(db, legacy, brief):
    legacy.enable_history(import_legacy=False)
    assert brief(legacy.changes()) == [(1, "insert", 1), (2, "insert", 2)]
    assert db.table("_docs_history").count == len(LEGACY_RECORDS)
    assert {"docs_insert_history", "docs_update_history", "docs_delete_history"} <= {
        trigger.name for trigger in db.triggers
    }


def test_legacy_columns_are_matched_by_name(db):
    db.executescript("""
        CREATE TABLE gadgets (id INTEGER PRIMARY KEY, name TEXT, price REAL);
        CREATE TABLE _gadgets_history (
            _rowid INTEGER, id INTEGER, colour TEXT, name TEXT,
            _version INTEGER, _updated INTEGER, _mask INTEGER
        );
        INSERT INTO gadgets VALUES (1, 'Widget', 2.5);
    """)
    db.conn.executemany(
        "insert into _gadgets_history values (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 1, "red", "Wodget", 1, T0, 7),
            # bit 2 is colour, bit 4 is name in this legacy table
            (1, None, "blue", None, 2, T0 + 1, 2),
            (1, None, None, "Widget", 3, T0 + 2, 4),
        ],
    )
    db.conn.commit()
    gadgets = db.table("gadgets")
    gadgets.enable_history()
    changes = list(gadgets.changes())
    assert [(c.version, c.op) for c in changes] == [
        (1, "insert"),
        (2, "update"),
        (3, "update"),
        (4, "update"),
    ]
    assert changes[0].row == {"id": 1, "name": "Wodget", "price": None}
    assert changes[2].changed == {"name": ("Wodget", "Widget")}
    assert changes[3].changed == {"price": (None, 2.5)}


def test_legacy_primary_key_change_is_a_delete_and_an_insert(db):
    db.executescript("""
        CREATE TABLE tags (slug TEXT PRIMARY KEY, label TEXT);
        CREATE TABLE _tags_history (
            _rowid INTEGER, slug TEXT, label TEXT,
            _version INTEGER, _updated INTEGER, _mask INTEGER
        );
        INSERT INTO tags VALUES ('python', 'Python!');
    """)
    db.conn.executemany(
        "insert into _tags_history values (?, ?, ?, ?, ?, ?)",
        [
            (1, "py", "Python", 1, T0, 3),
            (1, "python", None, 2, T0 + 1, 1),
            (1, None, "Python!", 3, T0 + 2, 2),
        ],
    )
    db.conn.commit()
    tags = db.table("tags")
    tags.enable_history()
    changes = list(tags.changes())
    assert [(c.version, c.op, c.pk) for c in changes] == [
        (1, "insert", "py"),
        (2, "delete", "py"),
        (3, "insert", "python"),
        (4, "update", "python"),
    ]
    assert changes[1].row == {"slug": "py", "label": "Python"}
    assert changes[2].row == {"slug": "python", "label": "Python"}
    assert db.history_version == 4


def test_table_that_only_looks_like_legacy_history_is_left_alone(db, plain):
    db.execute("create table _people_history (note text)")
    db.execute("insert into _people_history values ('mine')")
    plain.enable_history()
    assert db.history_version == 3
    assert db.table("_people_history").count == 1
