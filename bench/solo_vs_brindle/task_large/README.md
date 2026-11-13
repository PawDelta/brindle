# Large task kit: solo Fable 5.2 vs brindle

The second benchmark task for the solo-vs-brindle comparison, with the same
layout and scripts contract as the small kit in `../task/`. Fable 5.2 working
alone finished the small kit (TinyDB) in 7 minutes for $3, which is too short
to say anything about coordination. This kit is sized to keep one Fable 5.2
session busy for about an hour: one feature request in ten dependent parts
that cuts across a real, medium-sized codebase.

Each arm gets a fresh checkout of the repo below plus `spec.md`, and nothing
else. The hidden tests, the reference patch and the grading scripts stay
outside the arm's checkout.

| File | What it is |
|------|------------|
| `repo.json` | `"name": "large"`, target repo, pinned commit, setup commands and the repo's own test command |
| `spec.md` | The feature request. The only text either arm receives |
| `hidden_tests/` | Acceptance tests (pytest), one file per spec part |
| `reference.patch` | A full reference solution against the pinned commit (code, docs and a test file) |
| `validate.sh` | Proves the kit is sound (see below). Exit 0 only if everything holds |
| `grade.sh <checkout_dir>` | Scores a finished checkout; prints JSON to stdout |

`grade.sh` reuses the small kit's `../task/summarize.py` (JUnit report to
JSON) instead of carrying a copy, so the two kit directories must stay
siblings.

## The repo: sqlite-utils

[simonw/sqlite-utils](https://github.com/simonw/sqlite-utils) at
`9c66bafa52ae23d40499c74b0db3b635bcddb876` (the tip of `main` on 2026-11-13;
`pyproject.toml` says version 4.2.1).

- **Permissive licence**: Apache-2.0.
- **Medium size**: 11,248 lines of Python in `sqlite_utils/` (`db.py` is
  5,547, `cli.py` 3,813), plus about 13,000 lines of tests and 8,000 of
  documentation. Large enough that an arm cannot hold it all in its head and
  has to find the right seams; small enough to learn in one sitting.
- **Fast, self-contained suite**: 1,497 tests pass in about 10 seconds
  (16 skip without SpatiaLite). No network, no services: everything runs on
  the SQLite inside Python. Setup is one `uv venv` and one `uv pip install`.
- **Well known**, with a real library API, a Click CLI built on it, a plugin
  hook, a migrations module and Sphinx docs that the test suite polices
  (`tests/test_docs.py` fails when a command is undocumented or has no help).
  A cross-cutting feature therefore has to land in all of those places, the
  way a real pull request would.
- **A feature users actually want and that is hard to get right**: change
  history. The author ships it as a separate plugin (`sqlite-history`), which
  also gives the task a natural migration concern: importing that plugin's
  data.

Alternatives considered: `click`, `jinja` and `flask` have no natural
storage-plus-CLI-plus-docs feature of this size; `rich` and `werkzeug` have
slower suites and more surface than a one-hour task should need;
`mkdocs` and `invoke` pull in heavier dependency sets; built-in migrations,
the obvious sqlite-utils feature, already exist at this commit.

## The task (spec.md)

"Row history": a table can have history enabled, after which every change to
its rows is recorded in the database, can be read back, and can be reversed.
Ten parts, 538 lines of spec:

1. Enabling and disabling history; what cannot be tracked.
2. What is recorded: every write by any means (so: triggers), database-wide
   version numbers, exact values, no-op updates, key changes, replaces.
3. Reading: `table.changes()`, `table.history(pk)`, `db.changes()`,
   `db.history_version_at(timestamp)`.
4. Time travel: `table.as_of(version)`, `get_as_of`, `diff`.
5. Restoring: `table.revert(pk, version)`, `table.restore(version)`.
6. Changesets (`with db.changeset(...)`), `db.undo_changeset()` with conflict
   detection, and migrations recorded as changesets.
7. Schema changes: history must survive `add_column`, `transform` (rename,
   drop, reorder, new primary key, type changes, `keep_table`),
   `rename_table`, `drop`, and repair itself after changes made behind the
   library's back.
8. Excluded columns and pruning old history.
9. Importing history written by the `sqlite-history` plugin (masked partial
   records that have to be replayed).
10. Eight new CLI commands, a `--changeset` option on `insert`/`upsert`, and
    the documentation, including the generated CLI reference.

How it meets the brief:

- **A new subsystem plus integration into existing code paths.** The
  subsystem is a new module, `sqlite_utils/history.py`. It has to be wired
  into `Table.transform`, `Table.add_column`, `Table.drop`,
  `Database.rename_table`, `Database.enable_counts`, `Migrations.apply` and
  the CLI's shared insert/upsert implementation.
- **Spans at least four modules.** The reference touches `history.py` (new),
  `db.py`, `cli.py` and `migrations.py`, plus five documentation files and a
  test file.
- **A config and CLI surface.** `exclude=` / `--exclude`, `import_legacy=` /
  `--no-import-legacy`, `--changeset`, eight commands with the project's
  shared output options.
- **A migration and compatibility concern.** Part 9 (legacy data must be
  converted, not dropped), Part 7 (existing tracked tables must survive
  schema changes), and the standing rule that untracked tables and the
  existing 1,497 tests behave exactly as before.
- **Docs.** Three hand-written documentation sections and a `cog`-generated
  reference page that must be regenerated.
- **Parts depend on each other.** Parts 3 to 6 read what Part 2 records.
  Restore (5) is built on diff (4); undo (6) is built on restore; pruning (8)
  constrains 4, 5 and 6; the legacy import (9) ends in the reconciliation
  defined in Part 7; the CLI (10) needs everything.

The traps a careful engineer has to notice are all stated in the spec, but
each is easy to get subtly wrong: `transform()` replaces the table, which
silently drops its triggers, and with `keep_table=` would leave them
recording the wrong table; history must stay equal to the live table after
schema changes that alter values; storing row images as JSON loses BLOBs and
float precision; a changeset marker has to be visible to triggers but never
to other connections; undo must refuse to discard later work; a legacy
update record's `NULL` means "unchanged" unless its mask bit is set.

## Hidden tests

`hidden_tests/test_partNN_<name>.py`, one file per part: 168 test functions
(184 cases with parametrization), a little above the 100 to 150 the brief
aimed for, because ten parts at about 15 tests each is what it took to cover
every rule in the spec once.

| Part | File | Tests |
|------|------|-------|
| 1 | `test_part01_tracking.py` | 17 |
| 2 | `test_part02_recording.py` | 20 |
| 3 | `test_part03_reading.py` | 12 |
| 4 | `test_part04_time_travel.py` | 14 |
| 5 | `test_part05_restoring.py` | 12 |
| 6 | `test_part06_changesets.py` | 20 |
| 7 | `test_part07_schema_changes.py` | 18 |
| 8 | `test_part08_exclude_prune.py` | 20 |
| 9 | `test_part09_legacy_import.py` | 13 |
| 10 | `test_part10_cli_docs.py` | 22 |

They use only what the spec names: the new `Table` and `Database` methods,
the classes exported by `sqlite_utils.history`, the CLI through Click's test
runner, and sqlite-utils' existing public API. They never look at how history
is stored (beyond the `_history_` name prefix the spec requires), so an arm
is free to design its own tables and triggers. Part 10 also checks the three
documentation deliverables the spec asks for: every new command has an
example in `docs/cli.rst`, `docs/cli-reference.rst` contains each command's
current `--help`, and `docs/python-api.rst` names the API.

Parts are graded separately, but as in real work the later parts cannot pass
without the earlier ones: almost every test needs `enable_history()` (Part 1)
and reads results through `changes()` (Parts 2 and 3). A module that fails to
import only zeroes its own part (`grade.sh` runs pytest with
`--continue-on-collection-errors`, and expected tests are counted from the
test files).

## Reference solution

`reference.patch` is a complete solution: all 168 hidden tests pass and the
repo's own suite passes with it (1,530 passed, 16 skipped).

| | Files | Lines added |
|---|---|---|
| New subsystem: `sqlite_utils/history.py` | 1 | 1,128 |
| Integration: `db.py`, `cli.py`, `migrations.py` | 3 | 721 (6 removed) |
| Tests: `tests/test_history.py` | 1 | 231 |
| Hand-written docs: `cli.rst`, `python-api.rst`, `reference.rst`, `migrations.rst` | 4 | 353 |
| Generated docs: `cli-reference.rst` (via `cog`) | 1 | 275 |
| **Total** | **10** | **2,708 added, 6 removed** |

That is about five and a half times the small kit's reference (536 patch
lines against 2,959), over ten files instead of two.

**Expected solo time: about an hour, not measured.** No model was run to
build this kit. The estimate scales the small kit's 7 minutes by the size of
the reference (about 40 minutes), then allows for what does not scale
linearly: two source files of 4,000 to 5,500 lines to read, ten parts whose
rules interact, a full-suite run of about 10 seconds after every integration
change, and the docs and `cog` step. A plausible range is 45 to 90 minutes.
If a first solo run comes in well under an hour, the cheapest ways to
lengthen the task without redesigning it are to require the same guarantees
for `WITHOUT ROWID` tables (currently refused) and a database-wide
`db.as_of(version)` / `db.restore(version)` across tables.

## Scripts

```sh
bash validate.sh            # under a minute with a warm uv cache; exit 0 = kit is sound
bash grade.sh <checkout>    # {"passed", "failed", "total", "per_part": {...}}
```

`validate.sh` clones the repo at the pinned commit into a temp dir, runs the
setup commands, checks every hidden test fails on the base commit, applies
`reference.patch`, checks every hidden test passes, then runs the repo's own
suite (`test_cmd`). `KEEP_WORKDIR=1` keeps the temp dir for inspection.

`grade.sh` copies `hidden_tests/` into `<checkout>/bench_hidden_tests/`
(replacing any earlier copy) and runs it with `<checkout>/.venv/bin/python`,
the interpreter `repo.json`'s setup creates; set `GRADE_PYTHON` to use
another, and `GRADE_PYTEST_ARGS` to pass extra pytest options. It needs
`python3` on PATH for the summary. Run `setup` in the arm's checkout first if
the arm did not leave a `.venv` behind. Grading an arm's checkout runs that
arm's code: grade in a throwaway copy or sandbox.

`per_part` keys are `part01_tracking`, `part02_recording`, `part03_reading`,
`part04_time_travel`, `part05_restoring`, `part06_changesets`,
`part07_schema_changes`, `part08_exclude_prune`, `part09_legacy_import` and
`part10_cli_docs`; each holds `passed`, `failed` and `total`.

`repo.json`'s setup pins `click==8.5.1` and `tabulate==0.10.0`: the generated
CLI reference is the commands' `--help` output, whose wrapping depends on the
Click version, and Part 10 compares the two. `cogapp` is installed so an arm
can regenerate that page.

Requirements: `git`, `python3`, `uv` (with Python 3.12 available or
downloadable), and network access to GitHub and PyPI. No model API calls.
