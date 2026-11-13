# Feature request: row history for sqlite-utils (change tracking, time travel, undo)

You are working in a checkout of [sqlite-utils](https://github.com/simonw/sqlite-utils),
the Python library and command-line tool for working with SQLite databases.

People use sqlite-utils to maintain small databases by hand and by script, and
keep asking the same questions afterwards: *what changed in this table, and
when? What did it look like before last night's import? Can I put it back?*
Today the answer is "restore a backup". We want sqlite-utils to answer them
itself: a table can have **history** enabled, after which every change to its
rows is recorded in the database, can be read back, and can be reversed.

Implement the ten parts below. They build on each other: Part 2 defines what
is recorded, Parts 3 to 6 read and use the records, Parts 7 to 9 keep the
records right when the table itself changes, and Part 10 exposes it all on the
command line and in the documentation.

Ground rules:

- Everything described here is public API. Put the new machinery in a new
  module, `sqlite_utils/history.py`, and expose it through methods on the
  existing `sqlite_utils.db.Database` and `sqlite_utils.db.Table` classes,
  exactly as named below.
- It must work for in-memory databases and for database files, on the SQLite
  that ships with Python 3.10 and later, without new dependencies.
- History lives in the database itself. It survives closing and reopening the
  file and is visible to every connection.
- Existing behaviour and the existing test suite must keep working unchanged
  for tables that do not use history.
- Add tests for your work to the repository's own test suite, and follow the
  project's conventions (`black` formatting, the documentation layout under
  `docs/`, the `Justfile`).

---

## Shared concepts

**Tracked table.** A table that has history enabled.

**Change.** One row of a tracked table being inserted, updated or deleted. A
statement that affects five rows produces five changes.

**Version.** Every change gets a version number. Versions are database-wide:
the first change ever recorded in a database is version 1 and each later
change, in any tracked table, is exactly one higher. A version number is never
used twice, not even after history is disabled or pruned. `db.history_version`
is the version of the newest change (0 if nothing has ever been recorded), so
"the database as of version *N*" means: with changes 1 to *N* applied and
nothing after.

**Primary key values.** Rows are identified the way the rest of the library
does it (`table.get()`, `pks_and_rows_where()`): a single value for a
single-column primary key, a tuple for a compound primary key (in
`table.pks` order), and the `rowid` for a table without a primary key. Methods
that take `pk_values` accept a list or a tuple for compound keys.

**Recorded columns.** The columns of a tracked table that history records:
all of them, unless some are excluded (Part 8). Wherever this document speaks
of a row "as recorded" it means a `dict` of the recorded columns, in the
table's current column order. The `rowid` of a table without a primary key is
not a column and is not in that dict.

**Timestamps** are UTC strings formatted `YYYY-MM-DD HH:MM:SS.mmm`, for
example `2026-11-13 09:30:00.123`.

**Classes.** `sqlite_utils.history` exports:

- `HistoryError(Exception)`: raised whenever a history operation cannot be
  carried out.
- `HistoryConflict(HistoryError)`: see Part 6.
- `Change`, `Changeset` and `RowDiff`: plain value objects with the
  attributes listed in Parts 2, 6 and 4. Two objects of the same class with
  equal attributes compare equal (a dataclass does this).

**Storage.** How you store history is up to you, with these constraints: every
table, trigger and index the feature creates has a name that starts with
`_history_`, and the feature creates nothing in a database until history or a
changeset is first used in it.

---

## Part 1: enabling and disabling history

```python
Table.enable_history(exclude=None, import_legacy=True) -> Table   # returns the table
Table.disable_history() -> Table                                  # returns the table
Table.has_history        # property, bool
Database.history_tables() -> list[str]
Database.history_version # property, int
```

(`exclude` is Part 8 and `import_legacy` is Part 9.)

- `enable_history()` makes the table a tracked table. The rows already in the
  table become its starting point: each is recorded as an `insert` change, in
  `rowid` order.
- It raises `HistoryError`, and enables nothing, if the table does not exist,
  is a virtual table, is a `WITHOUT ROWID` table, or is one of the feature's
  own `_history_` tables.
- Calling `enable_history()` on a table that is already tracked is not an
  error. It keeps everything recorded so far and brings the tracking up to
  date with the table (Part 7); if nothing is out of date it records nothing.
- `disable_history()` stops recording changes to the table, removes whatever
  was attached to the table to record them (afterwards `table.triggers` shows
  none of yours), and deletes the table's recorded history. It raises
  `HistoryError` if the table is not tracked. The database-wide version
  counter is not reset.
- `has_history` is `True` for a tracked table and `False` otherwise, including
  for a table that does not exist.
- `db.history_tables()` lists the names of the tracked tables in the order
  history was enabled for them.
- Every method in Parts 3, 4, 5 and 8 that reads or uses a table's history
  raises `HistoryError` when called on a table that is not tracked.
- `db.enable_counts()` (the existing cached-counts feature) must leave the
  `_history_` tables alone: no count triggers on them and no entries for them
  in `db.cached_counts()`.

## Part 2: what is recorded

Each change is described by a `sqlite_utils.history.Change` with these
attributes:

| attribute   | meaning |
|-------------|---------|
| `version`   | the change's version number |
| `table`     | the table's name |
| `op`        | `"insert"`, `"update"` or `"delete"` |
| `pk`        | the row's primary key value(s), as described above |
| `row`       | the row as recorded: after the change for an insert or update, as it was just before for a delete |
| `changed`   | for an update, a dict `{column: (old, new)}` of the recorded columns whose value changed; `{}` for inserts and deletes |
| `changeset` | the ID of the changeset the change belongs to (Part 6), or `None` |
| `at`        | timestamp of when the change was recorded |

Rules:

- **Every** insert, update and delete of a row in a tracked table is recorded,
  however it is made: through the library's methods (`insert`, `insert_all`,
  `upsert`, `update`, `delete`, `delete_where`, ...), through SQL passed to
  `db.execute()`, or by a different connection or program writing to the same
  database file. (In practice this means SQLite triggers.)
- One change per affected row, numbered in the order the rows were changed.
- An update that leaves every recorded column of a row equal to what it was
  records nothing. So an upsert of identical data, or `UPDATE t SET x = x`,
  leaves no trace. An insert that is ignored (`ignore=True`) records nothing.
  "Equal" means the same value of the same SQLite storage class: where a
  column's type lets both be stored, the integer `1`, the real `1.0` and the
  text `'1'` are three different values, and going from one to another is a
  change.
- An update that changes a row's primary key (or the `rowid` of a table
  without one) is recorded as two changes: a `delete` of the row under its old
  key, then an `insert` under the new key.
- Replacing an existing row (`insert(..., replace=True)`, `INSERT OR REPLACE`)
  is recorded as a `delete` of the old row followed by an `insert` of the new
  one. (This relies on recursive triggers being on, which is the library's
  default.)
- Changes that are rolled back are not recorded and use up no version
  numbers.
- Values are recorded exactly: `NULL`, integers (the full 64-bit range),
  floats (bit for bit), text and BLOBs come back from history with the same
  value and the same Python type the table would give.
- Table and column names can be anything SQLite allows, including names with
  spaces, quotes and SQL keywords.
- Recording must not disturb the library: `table.last_pk` and
  `table.last_rowid` after an insert are what they would be without history,
  and other triggers on the table (for example the ones `enable_fts(...,
  create_triggers=True)` creates) keep working.

## Part 3: reading the recorded changes

```python
Table.changes(since=0, until=None, changeset=None)     # generator of Change
Table.history(pk_values) -> list[Change]
Database.changes(since=0, until=None, changeset=None)  # generator of Change
Database.history_version_at(timestamp) -> int
```

- `table.changes()` yields the table's changes in ascending version order.
  `since` is exclusive (only versions greater than it), `until` is inclusive
  (only versions up to and including it), and `changeset` restricts the
  result to one changeset ID.
- `table.history(pk_values)` returns the changes of one row, oldest first. It
  keeps working after the row is deleted, and returns `[]` for a key that has
  no recorded changes.
- `db.changes()` yields the changes of all tracked tables merged into one
  ascending sequence, with the same three filters. With no tracked tables it
  yields nothing. A caller can poll with
  `db.changes(since=last_version_seen)`.
- `db.history_version_at(timestamp)` returns the highest version among the
  recorded changes whose `at` is at or before `timestamp`, or 0 if there is
  none. `timestamp` is a `datetime.datetime` or an ISO 8601 string (a date, or
  a date and time separated by a space or `T`, optionally with fractional
  seconds and a UTC offset). Values without a timezone are UTC; values with
  one are converted. A date alone means midnight at the start of that day. A
  string that cannot be parsed raises `ValueError`.

## Part 4: time travel

```python
Table.as_of(version)                        # generator of dict
Table.get_as_of(pk_values, version) -> dict
Table.diff(from_version, to_version=None) -> list[RowDiff]
```

- `table.as_of(version)` yields the rows of the table as recorded, the way
  they were in the database as of `version`, ordered by primary key (by
  `rowid` for tables without one). `as_of(0)` yields nothing. A version at or
  beyond `db.history_version` gives the current state, so
  `list(table.as_of(db.history_version))` always equals the table's own rows
  in primary key order (restricted to the recorded columns).
- `table.get_as_of(pk_values, version)` returns one row as it was at that
  version, and raises `sqlite_utils.db.NotFoundError` if the row did not exist
  then.
- `table.diff(from_version, to_version)` returns one
  `sqlite_utils.history.RowDiff` for each row whose state at `from_version`
  differs from its state at `to_version`, ordered by primary key. `to_version`
  defaults to the current version. A `RowDiff` has:
  - `pk`: the row's primary key value(s);
  - `before`: the row at `from_version`, or `None` if it did not exist;
  - `after`: the row at `to_version`, or `None` if it did not exist;
  - `op`: `"insert"` if `before` is `None`, `"delete"` if `after` is `None`,
    otherwise `"update"`.

  Rows that are the same at both versions are left out, even if they changed
  in between. `from_version` may be the later of the two, which gives the
  reverse diff.
- History is append-only: later changes never alter what `as_of()` reports
  for an earlier version.

## Part 5: restoring

```python
Table.revert(pk_values, version) -> bool
Table.restore(version) -> int
```

- `table.revert(pk_values, version)` makes one row what it was at `version`:
  it writes back the old values if the row has changed since, re-creates the
  row if it has been deleted since (for a table without a primary key, with
  the same `rowid`), and deletes the row if it did not exist at that version.
  It returns `True` if it changed the table and `False` if there was nothing
  to do (including for a key that never existed).
- `table.restore(version)` does the same for the whole table and returns the
  number of rows it inserted, updated or deleted. Rows that already match are
  not written. It does all of its work in one transaction: if any write
  fails, the error propagates and the table is exactly as it was.
- Both write to the table in the normal way, so what they do is itself
  recorded as new changes. Nothing is ever removed from history by restoring,
  and a restore can be reversed by restoring to the version from before it.

## Part 6: changesets and undo

```python
Database.changeset(message=None, author=None)   # context manager, yields a Changeset
Database.changesets() -> list[Changeset]
Database.undo_changeset(changeset_id, force=False, author=None) -> Changeset | None
```

A **changeset** groups changes and gives them a description:

```python
with db.changeset("Import March data", author="simon") as changeset:
    db["places"].insert_all(rows)
    db.execute("update places set checked = 0")
```

- A `sqlite_utils.history.Changeset` has `id` (an integer; later changesets
  have higher IDs), `message`, `author` and `created_at` (a timestamp).
- Every change recorded while the block is running on this database object,
  in any tracked table and by any means, has that changeset's ID as its
  `Change.changeset`. Changes recorded outside a block have `None`.
- The block is a transaction. If it raises, everything done inside it is
  rolled back, no changeset is kept, and the exception propagates.
- A changeset in which no change was recorded is discarded when the block
  ends: it does not appear in `db.changesets()`.
- Changesets cannot be nested: entering a second block while one is open on
  the same database raises `HistoryError`.
- `db.changeset()` works in a database with no tracked tables (the changeset
  is then always discarded).
- `db.changesets()` returns the changesets that were kept, ordered by ID.

**Undo.** `db.undo_changeset(changeset_id)` puts every row that the changeset
changed back to the state it had just before the changeset's first change to
that row, across all tables: rows it inserted are deleted, rows it deleted
come back, rows it updated get their earlier values.

- The undo runs inside a new changeset with the message
  `Undo changeset <id>` and the given `author`, and returns that new
  `Changeset`. Its changes are recorded like any others, so an undo can itself
  be undone (which redoes the original). If the undo did not need to change
  anything it returns `None`.
- If any row the changeset changed has been changed again afterwards by
  something that is not part of that changeset, undoing would silently
  discard that later work. In that case `undo_changeset` raises
  `sqlite_utils.history.HistoryConflict` and changes nothing. The exception's
  `conflicts` attribute is a list of `(table_name, pk)` tuples naming those
  rows. With `force=True` the undo goes ahead anyway.
- An unknown `changeset_id` raises `HistoryError`.

**Migrations.** `sqlite_utils.Migrations.apply()` runs each transactional
migration in a transaction. When the database has at least one tracked table,
that transaction must be a changeset with the message
`Migration <set name>:<migration name>`, so that data changed by a migration
can be traced and undone. If a changeset is already open when `apply()` is
called, the migrations' changes simply join it. Databases with no tracked
tables must be left exactly as before: applying migrations there creates no
`_history_` tables.

## Part 7: history and schema changes

History has to stay correct when the tracked table itself changes. After each
of the operations below, the table is still tracked, recording continues, and
`list(table.as_of(db.history_version))` still equals the live table.

- **`table.add_column()`** (and anything built on it, such as
  `insert(..., alter=True)`): the new column is a recorded column from then
  on. For versions from before the column existed, history reports `None` for
  it.
- **`table.transform()`**: the recorded history is kept.
  - A renamed column is renamed throughout the recorded history: earlier
    versions report their old values under the new name.
  - A dropped column disappears from the recorded history.
  - Rows from history list their columns in the table's current column
    order, so they follow a `column_order=` change.
  - If the primary key changes (including a table without one gaining one),
    history is addressed by the new key from then on: `history()`,
    `get_as_of()`, `revert()` and friends take values of the new primary key
    and find the row's earlier changes by the values recorded in those
    columns.
  - With `keep_table=`, the kept copy of the old table is not tracked:
    writes to it record nothing.
  - A `transform()` that fails leaves the table tracked and recording as
    before.
  - `transform()` is built on by other methods (`extract()`,
    `add_foreign_key()`, `convert(..., drop=True)`, ...); they must keep
    history in step too.
- **`db.rename_table()`**: the history moves with the table. The new name is
  tracked, the old name is not, and changes read back from history carry the
  new name as `Change.table`. A rename that fails leaves the table tracked
  under its old name.
- **`table.drop()`**: the table's recorded history is deleted with it. A new
  table created later under the same name starts out untracked.

A schema change records no changes by itself. The one exception is when it
alters stored values, because history must still end in the table's real
state: rows whose recorded columns no longer match their last recorded state
get an `update` change, in `rowid` order. Two cases where this happens:
`add_column(..., not_null_default=...)` gives every existing row a value in the
new column, and `transform(types=...)` can convert values (the text `'5'`
becoming the integer `5`).

**Changes made behind the library's back.** If the table is altered some
other way (a raw `ALTER TABLE ... ADD COLUMN`, or someone dropping your
triggers), history cannot know until `table.enable_history()` is called
again. Until then a raw-added column is not a recorded column: it is not in
`history_columns`, changes to it are not recorded, and everything else
(`changes()`, time travel, `diff()`, restoring, recording changes to the
other columns) keeps working as if the column were not there. That call must
repair things:

- the recorded columns are brought in line with the table's columns, and
- the table is compared with its last recorded state, and every difference
  is recorded as a change: first a `delete` for each row that is recorded but
  no longer in the table, then an `update` for each row whose recorded
  columns differ, then an `insert` for each row that is in the table but not
  recorded, each group in `rowid` order.

## Part 8: excluded columns and pruning

**Excluded columns.** `table.enable_history(exclude=["hits", "last_seen"])`
leaves those columns out of the history.

```python
Table.history_columns   # property: list of the recorded column names, in table order
```

- Excluded columns never appear in `Change.row`, `Change.changed`, or rows
  returned by `as_of()`, `get_as_of()` and `diff()`.
- A write that only changes excluded columns records nothing.
- `revert()`, `restore()` and `undo_changeset()` leave the excluded columns
  of existing rows as they are. A row they re-create gets the column's
  default (normally `NULL`) for excluded columns.
- `exclude` must name existing columns, cannot include a primary key column,
  and cannot cover every column of the table. Otherwise `HistoryError` is
  raised and history is not enabled.
- Calling `enable_history()` again with `exclude=None` keeps the current
  setting. Passing a list (including `[]`) replaces it:
  - a column that becomes excluded is removed from the recorded history, and
    no change is recorded;
  - a column that becomes recorded follows the rule from Part 7: earlier
    versions report `None` for it, and each row that currently holds a
    non-`NULL` value in it gets an `update` change.
- An excluded column that is renamed with `transform()` stays excluded under
  its new name.

**Pruning.** History grows with every change. Pruning trades old detail for
space.

```python
Table.prune_history(version) -> int
Table.history_floor      # property, int: 0 until the table is pruned
```

- `table.prune_history(version)` forgets the table's individual changes up
  to and including `version`, keeping exactly one starting point per row:
  - each row that existed at `version` keeps the last change made to it at
    or before `version`. That record keeps its version number, timestamp and
    changeset, holds the row's state at `version`, and from now on reads as
    an `insert` (with empty `changed`);
  - every other change with a version at or below `version` is removed,
    including all trace of rows that had been deleted by then;
  - changes after `version` are untouched.

  It returns the number of change records removed, and `history_floor`
  becomes `version`.
- After pruning, everything from `history_floor` onwards works as before:
  `as_of()`, `get_as_of()`, `diff()`, `revert()` and `restore()` give the same
  answers they gave before pruning for versions at or above the floor. Given
  a version below the floor, each of them raises `HistoryError`.
- A `version` at or below the current floor does nothing and returns 0. A
  `version` beyond `db.history_version` is treated as `db.history_version`.
- Pruning one table does not affect others, and never changes the
  database-wide version counter.
- `db.undo_changeset()` raises `HistoryError`, changing nothing, if any of
  the changeset's changes has been pruned away or turned into a starting
  point.

## Part 9: importing history from the sqlite-history plugin

Some databases already have change history, recorded by the older
`sqlite-history` plugin. Those users must not lose it when they move to the
built-in feature.

For a table named `T`, the plugin keeps a table `_T_history` (with an index
`idx_T_history_rowid`) and three triggers on `T` named `T_insert_history`,
`T_update_history` and `T_delete_history`. The legacy table has:

| column     | meaning |
|------------|---------|
| `_rowid`   | the `rowid` of the row in `T` that the record is about |
| *(one column for each column `T` had, with the same names)* | values, see below |
| `_version` | a per-row counter: 1, 2, 3, ... for each `_rowid` |
| `_updated` | when the record was written, in milliseconds since the Unix epoch (UTC) |
| `_mask`    | which value columns are meaningful, see below |

The value columns are every column other than those four, and their order in
the legacy table defines bit positions: the first value column is bit 0 (value
1), the second is bit 1 (value 2), the third is bit 2 (value 4), and so on.
Three kinds of record exist:

- an **insert** record holds the whole new row; all mask bits are set;
- an **update** record holds only the columns that changed: `_mask` has the
  bits of those columns, and every other value column is `NULL` and must be
  ignored. A column whose bit is set and whose value is `NULL` really was set
  to `NULL`;
- a **delete** record has `_mask = -1` and holds the row's last values in
  every value column.

The first time `enable_history()` is called for a table (that is, when it is
not tracked yet), and a table `_T_history` with those four special columns
exists, its records are imported:

- Records are imported in the order they were written (the legacy table's
  own `rowid` order). Each becomes one change, with consecutive version
  numbers continuing from `db.history_version`, an `at` converted from
  `_updated`, and no changeset.
- A record with `_mask = -1` becomes a `delete`, whose row is the values in
  that record. Otherwise the first record for a `_rowid`, or the first one
  after a delete of that `_rowid`, becomes an `insert`. Any other record
  becomes an `update` whose row is the row's previous state with the masked
  columns replaced.
- As in Part 2, an imported update that changes the value of a primary key
  column becomes two changes: a `delete` of the old row and an `insert` of
  the new one.
- Columns are matched by name. Legacy value columns that `T` no longer has
  are ignored (but still count for bit positions); recorded columns of `T`
  that the legacy table lacks are `None`.
- After the import, the legacy table (with its index) and the three legacy
  triggers are dropped, and the table is compared with the imported history
  exactly as in "Changes made behind the library's back" in Part 7, so that
  history ends in the table's real state.

`enable_history(import_legacy=False)` skips all of this: the legacy table
and triggers are left completely untouched and history starts from the
table's current rows, as in Part 1. A table that happens to be called
`_T_history` but lacks those four columns is not legacy history and is left
alone.

## Part 10: command line and documentation

Add these commands to the `sqlite-utils` CLI. Each takes the database file
as its first argument (`PATH`, which must exist) and reports failures the way
the existing commands do: a message starting with `Error:` and exit code 1.
That covers every `HistoryError`, a table name that is not a table in the
database, and a `VERSION` that is neither a number nor a timestamp.

Where a command takes a `VERSION`, it accepts a version number or a timestamp
in the forms `history_version_at()` accepts; a timestamp stands for
`db.history_version_at(timestamp)`. Where a command takes primary key values
they arrive as strings and must find rows whose keys are integers or floats
(convert them according to the primary key columns' types).

| command | does |
|---------|------|
| `enable-history PATH TABLE...` | `enable_history()` for one or more tables. Options: `-x/--exclude COLUMN` (repeatable; an error unless exactly one table is given), `--no-import-legacy`. At least one table is required. If any named table is not a table, nothing is enabled for any of them. Prints nothing. |
| `disable-history PATH TABLE...` | `disable_history()` for one or more tables. Prints nothing. |
| `history PATH [TABLE [PK_VALUE...]]` | Output changes: of all tracked tables (an empty list if there are none), of one table, or of one row. Options: `--since N`, `--until N`, `--changeset ID`, and the same output options as `query`/`rows` (`--nl`, `--arrays`, `--csv`, `--tsv`, `--no-headers`, `-t/--table`, `--fmt`, `--json-cols`, `--ascii`). |
| `as-of PATH TABLE VERSION` | Output the table's rows as of `VERSION`, with the same output options, columns in table order. |
| `restore PATH TABLE VERSION [PK_VALUE...]` | `restore(version)`, or with primary key values `revert(pk_values, version)`. Prints the number of rows changed (so `1` or `0` for a single row). |
| `changesets PATH` | Output the changesets, with the same output options. In a database that has never used history this outputs an empty list and creates nothing. |
| `undo PATH CHANGESET_ID` | `undo_changeset()`. Options: `--force`, `--author TEXT`. Prints the new changeset's ID, or nothing if there was nothing to undo. A conflict is an error that names the tables involved. |
| `prune-history PATH TABLE VERSION` | `prune_history(version)`. Prints the number of change records removed. |

Output shapes:

- `history` outputs one object per change with the keys `version`, `table`,
  `op`, `pk`, `at`, `changeset`, `row`, `changed`, in that order. By default
  this is JSON, like `query`: `pk` is a value, or an array for a compound
  key; `row` is an object; `changed` is an object mapping each column to a
  two-element array `[old, new]`. In the CSV, TSV and table formats, `row`
  and `changed` (and a compound `pk`) are written as JSON text in their cell.
- `as-of` outputs the rows the way `rows` does.
- `changesets` outputs objects with the keys `id`, `message`, `author`,
  `created_at`, in that order.

Also extend two existing commands: `insert` and `upsert` get a
`--changeset MESSAGE` option. With it, everything the command writes is
wrapped in `db.changeset(MESSAGE)`, so the rows it changes in tracked tables
can later be listed with `history --changeset` and reversed with `undo`.

**Documentation.** The feature is not done until it is documented where
users will look:

- `docs/python-api.rst`: a section explaining history, which names every
  method and property introduced above, the `import_legacy` option and
  `HistoryConflict`. Write methods as calls on `table` or `db`, the way the
  rest of that page does: ``table.revert(pk_values, version)``,
  ``table.history(pk_values)``, ``db.changes()``, ``db.changeset()`` and so
  on.
- `docs/cli.rst`: a section that shows an example invocation of every new
  command (the existing `tests/test_docs.py` enforces this for every command)
  and describes the `--changeset` option.
- `docs/cli-reference.rst` is generated from the commands' `--help` output by
  `cog`; regenerate it (see the `Justfile`) so that it contains the help of
  every new command and the updated help of `insert` and `upsert`. Every
  command needs help text.
