"""Part 10: the command line and the documentation."""

import csv
import io
import json
import pathlib
import re

import pytest
from click.testing import CliRunner

from sqlite_utils import Database, cli

NEW_COMMANDS = [
    "enable-history",
    "disable-history",
    "history",
    "as-of",
    "restore",
    "changesets",
    "undo",
    "prune-history",
]

ANN = {"id": 1, "name": "Ann", "team": "red", "score": 9}
BO = {"id": 2, "name": "Bo", "team": "blue", "score": 7}
CY = {"id": 3, "name": "Cy", "team": "red", "score": None}


@pytest.fixture(autouse=True)
def scratch_directory(tmp_path, monkeypatch):
    """Commands run in an empty directory, never in the checkout."""
    monkeypatch.chdir(tmp_path)


def run(*args, input=None):
    return CliRunner().invoke(cli.cli, [str(arg) for arg in args], input=input)


def ok(*args, input=None):
    result = run(*args, input=input)
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def plain_path(tmp_path):
    """A database file with untracked people and pets tables."""
    path = str(tmp_path / "plain.db")
    db = Database(path)
    db.table("people", pk="id").insert_all([ANN, BO, CY])
    db.table("pets", pk="id").insert({"id": 1, "name": "Rex", "notes": "x"})
    db.close()
    return path


@pytest.fixture
def edited_path(db_path):
    """db_path after versions 4-6: update Bo, delete Ann, insert Di."""
    db = Database(db_path)
    people = db.table("people")
    people.update(2, {"score": 8})
    people.delete(1)
    people.insert({"id": 4, "name": "Di", "team": "blue", "score": 1})
    db.close()
    return db_path


def test_enable_history_command(plain_path):
    assert ok("enable-history", plain_path, "people", "pets") == ""
    db = Database(plain_path)
    assert db.history_tables() == ["people", "pets"]
    assert db.history_version == 4
    db.close()


def test_enable_history_rejects_unknown_tables(plain_path):
    result = run("enable-history", plain_path, "people", "nope")
    assert result.exit_code == 1
    assert "nope" in result.output
    db = Database(plain_path)
    assert db.history_tables() == []
    db.close()
    assert run("enable-history", plain_path).exit_code == 2


def test_enable_history_exclude_option(plain_path):
    ok("enable-history", plain_path, "pets", "--exclude", "notes")
    db = Database(plain_path)
    assert db.table("pets").history_columns == ["id", "name"]
    db.close()
    result = run("enable-history", plain_path, "people", "pets", "-x", "notes")
    assert result.exit_code == 1
    result = run("enable-history", plain_path, "people", "-x", "id")
    assert result.exit_code == 1
    assert result.output.startswith("Error")


def test_enable_history_no_import_legacy_option(tmp_path):
    path = str(tmp_path / "legacy.db")
    db = Database(path)
    db.executescript("""
        CREATE TABLE docs (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE _docs_history (
            _rowid INTEGER, id INTEGER, title TEXT,
            _version INTEGER, _updated INTEGER, _mask INTEGER
        );
        INSERT INTO docs VALUES (1, 'New');
        INSERT INTO _docs_history VALUES (1, 1, 'Old', 1, 1704164645678, 3);
        INSERT INTO _docs_history VALUES (1, NULL, 'New', 2, 1704164646678, 2);
    """)
    db.close()
    ok("enable-history", path, "docs", "--no-import-legacy")
    db = Database(path)
    assert db.history_version == 1
    assert "_docs_history" in db.table_names()
    db.table("docs").disable_history()
    db.close()
    ok("enable-history", path, "docs")
    db = Database(path)
    assert [c.op for c in db.table("docs").changes()] == ["insert", "update"]
    assert "_docs_history" not in db.table_names()
    db.close()


def test_disable_history_command(db_path):
    assert ok("disable-history", db_path, "people") == ""
    db = Database(db_path)
    assert db.history_tables() == []
    db.close()
    result = run("disable-history", db_path, "people")
    assert result.exit_code == 1
    assert result.output.startswith("Error")


def test_history_command_outputs_changes_as_json(edited_path):
    changes = json.loads(ok("history", edited_path))
    assert [list(change) for change in changes[:1]] == [
        ["version", "table", "op", "pk", "at", "changeset", "row", "changed"]
    ]
    assert [(c["version"], c["table"], c["op"], c["pk"]) for c in changes] == [
        (1, "people", "insert", 1),
        (2, "people", "insert", 2),
        (3, "people", "insert", 3),
        (4, "people", "update", 2),
        (5, "people", "delete", 1),
        (6, "people", "insert", 4),
    ]
    assert changes[3]["row"] == dict(BO, score=8)
    assert changes[3]["changed"] == {"score": [7, 8]}
    assert changes[3]["changeset"] is None
    assert changes[4]["changed"] == {}


def test_history_command_filters(edited_path):
    def versions(*args):
        return [c["version"] for c in json.loads(ok("history", edited_path, *args))]

    assert versions("people") == [1, 2, 3, 4, 5, 6]
    assert versions("people", "2") == [2, 4]
    assert versions("people", "--since", "4") == [5, 6]
    assert versions("--since", "2", "--until", "4") == [3, 4]
    assert versions("people", "2", "--since", "2") == [4]
    assert versions("people", "99") == []


def test_history_command_other_formats(edited_path):
    lines = ok("history", edited_path, "people", "--since", "4", "--nl").splitlines()
    assert [json.loads(line)["op"] for line in lines] == ["delete", "insert"]
    rows = list(csv.reader(io.StringIO(ok("history", edited_path, "people", "2", "--csv"))))
    assert rows[0] == ["version", "table", "op", "pk", "at", "changeset", "row", "changed"]
    assert [row[:4] for row in rows[1:]] == [
        ["2", "people", "insert", "2"],
        ["4", "people", "update", "2"],
    ]
    assert json.loads(rows[2][6]) == dict(BO, score=8)
    assert json.loads(rows[2][7]) == {"score": [7, 8]}


def test_history_command_compound_primary_key(tmp_path):
    path = str(tmp_path / "stock.db")
    db = Database(path)
    stock = db.table("stock", pk=("shop", "item"))
    stock.insert_all(
        [
            {"shop": "north", "item": 7, "qty": 4},
            {"shop": "south", "item": 7, "qty": 6},
        ]
    )
    stock.enable_history()
    stock.update(("south", 7), {"qty": 5})
    db.close()
    changes = json.loads(ok("history", path, "stock", "south", "7"))
    assert [(c["version"], c["pk"]) for c in changes] == [
        (2, ["south", 7]),
        (3, ["south", 7]),
    ]


def test_history_command_errors(plain_path, db_path):
    result = run("history", plain_path, "people")
    assert result.exit_code == 1
    assert result.output.startswith("Error")
    assert run("history", db_path, "nope").exit_code == 1
    assert json.loads(ok("history", plain_path)) == []


def test_as_of_command(edited_path):
    assert json.loads(ok("as-of", edited_path, "people", "3")) == [ANN, BO, CY]
    assert json.loads(ok("as-of", edited_path, "people", "5")) == [dict(BO, score=8), CY]
    assert json.loads(ok("as-of", edited_path, "people", "0")) == []
    rows = list(csv.reader(io.StringIO(ok("as-of", edited_path, "people", "4", "--csv"))))
    assert rows[0] == ["id", "name", "team", "score"]
    assert rows[1] == ["1", "Ann", "red", "9"]


def test_as_of_command_accepts_a_timestamp(edited_path):
    now = json.loads(ok("as-of", edited_path, "people", "2999-01-01"))
    assert [row["id"] for row in now] == [2, 3, 4]
    assert json.loads(ok("as-of", edited_path, "people", "1999-01-01 12:00")) == []
    result = run("as-of", edited_path, "people", "yesterday-ish")
    assert result.exit_code == 1
    assert result.output.startswith("Error")


def test_restore_command_restores_the_table(edited_path):
    assert ok("restore", edited_path, "people", "3").strip() == "3"
    db = Database(edited_path)
    assert list(db.table("people").rows_where(order_by="id")) == [ANN, BO, CY]
    assert db.history_version == 9
    db.close()
    assert ok("restore", edited_path, "people", "9").strip() == "0"


def test_restore_command_restores_one_row(edited_path):
    assert ok("restore", edited_path, "people", "3", "2").strip() == "1"
    assert ok("restore", edited_path, "people", "3", "2").strip() == "0"
    db = Database(edited_path)
    assert db.table("people").get(2) == BO
    assert db.table("people").count == 3
    assert db.history_version == 7
    db.close()


def test_insert_and_upsert_changeset_option(db_path):
    rows = [{"id": 4, "name": "Di", "team": "blue", "score": 1}, dict(ANN, score=10)]
    ok("upsert", db_path, "people", "-", "--pk", "id", "--changeset", "Weekly sync", input=json.dumps(rows))
    ok("insert", db_path, "people", "-", "--pk", "id", "--changeset", "One more",
       input=json.dumps({"id": 5, "name": "Ed", "team": "red", "score": 0}))
    ok("insert", db_path, "people", "-", "--pk", "id",
       input=json.dumps({"id": 6, "name": "Flo", "team": "red", "score": 0}))
    listed = json.loads(ok("changesets", db_path))
    assert [list(item) for item in listed[:1]] == [["id", "message", "author", "created_at"]]
    assert [(item["message"], item["author"]) for item in listed] == [
        ("Weekly sync", None),
        ("One more", None),
    ]
    first, second = (item["id"] for item in listed)
    tagged = json.loads(ok("history", db_path, "--changeset", first))
    assert sorted((c["op"], c["pk"]) for c in tagged) == [("insert", 4), ("update", 1)]
    everything = json.loads(ok("history", db_path, "--since", "3"))
    assert [c["changeset"] for c in everything] == [first, first, second, None]


def test_changesets_command_on_database_without_history(plain_path):
    assert json.loads(ok("changesets", plain_path)) == []
    db = Database(plain_path)
    assert not [name for name in db.table_names() if name.startswith("_history_")]
    db.close()


def test_undo_command(db_path):
    ok("upsert", db_path, "people", "-", "--pk", "id", "--changeset", "Oops",
       input=json.dumps([dict(ANN, score=0), {"id": 4, "name": "Di", "team": "x", "score": 1}]))
    changeset_id = json.loads(ok("changesets", db_path))[0]["id"]
    output = ok("undo", db_path, changeset_id, "--author", "bea")
    listed = json.loads(ok("changesets", db_path))
    assert [item["message"] for item in listed] == ["Oops", f"Undo changeset {changeset_id}"]
    assert output.strip() == str(listed[1]["id"])
    assert listed[1]["author"] == "bea"
    db = Database(db_path)
    assert list(db.table("people").rows_where(order_by="id")) == [ANN, BO, CY]
    db.close()


def test_undo_command_conflicts_and_force(db_path):
    ok("upsert", db_path, "people", "-", "--pk", "id", "--changeset", "Oops",
       input=json.dumps(dict(ANN, score=0)))
    changeset_id = json.loads(ok("changesets", db_path))[0]["id"]
    db = Database(db_path)
    db.table("people").update(1, {"team": "green"})
    db.close()
    result = run("undo", db_path, changeset_id)
    assert result.exit_code == 1
    assert result.output.startswith("Error")
    assert "people" in result.output
    assert run("undo", db_path, changeset_id + 50).exit_code == 1
    ok("undo", db_path, changeset_id, "--force")
    db = Database(db_path)
    assert db.table("people").get(1) == ANN
    db.close()


def test_prune_history_command(edited_path):
    assert ok("prune-history", edited_path, "people", "5").strip() == "3"
    db = Database(edited_path)
    assert db.table("people").history_floor == 5
    assert [c.version for c in db.table("people").changes()] == [3, 4, 6]
    db.close()
    result = run("as-of", edited_path, "people", "4")
    assert result.exit_code == 1
    assert result.output.startswith("Error")
    assert ok("prune-history", edited_path, "people", "2999-01-01").strip() == "0"
    db = Database(edited_path)
    assert db.table("people").history_floor == 6
    db.close()


@pytest.fixture(scope="module")
def docs():
    return pathlib.Path(cli.__file__).parent.parent / "docs"


@pytest.mark.parametrize("command", NEW_COMMANDS)
def test_commands_are_documented_in_the_cli_guide(docs, command):
    documented = re.findall(r"(?:\$ |    )sqlite-utils (\S+)", (docs / "cli.rst").read_text())
    assert command in documented


@pytest.mark.parametrize("command", NEW_COMMANDS + ["insert", "upsert"])
def test_cli_reference_matches_the_help_output(docs, command):
    reference = {
        line.strip() for line in (docs / "cli-reference.rst").read_text().splitlines()
    }
    # The reference page shows the program under its real name
    help_text = ok(command, "--help").replace("Usage: cli ", "Usage: sqlite-utils ")
    assert help_text.startswith(f"Usage: sqlite-utils {command} ")
    if command in ("insert", "upsert"):
        assert "--changeset" in help_text
    missing = [
        line.strip()
        for line in help_text.splitlines()
        if line.strip() and line.strip() not in reference
    ]
    assert not missing


def test_python_api_is_documented(docs):
    text = (docs / "python-api.rst").read_text()
    for name in (
        "enable_history",
        "disable_history",
        "has_history",
        "history_columns",
        "history_floor",
        "history_tables",
        "history_version",
        "history_version_at",
        ".changes(",
        ".history(",
        "as_of",
        "get_as_of",
        ".diff(",
        ".revert(",
        ".restore(",
        "prune_history",
        ".changeset(",
        ".changesets(",
        "undo_changeset",
        "import_legacy",
        "HistoryConflict",
    ):
        assert name in text, name
