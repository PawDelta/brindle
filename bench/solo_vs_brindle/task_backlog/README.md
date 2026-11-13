# Backlog task kit: solo Fable 5.2 vs brindle

The third benchmark task for the solo-vs-brindle comparison, with the same
layout and scripts contract as `../task/` and `../task_large/`. The large kit
is one feature in ten parts that depend on each other; this kit is the
opposite shape: a **backlog of five independent tickets** in one repo. Each
ticket is in a different area of the code and touches different files from
every other ticket, so the work splits cleanly into five parallel pieces. It
measures what parallel workers buy when nothing forces them to wait on each
other, against one session working through the list.

Each arm gets a fresh checkout of the repo below plus `spec.md`, and nothing
else. The hidden tests, the reference patch and the grading scripts stay
outside the arm's checkout.

| File | What it is |
|------|------------|
| `repo.json` | `"name": "backlog"`, target repo, pinned commit, setup commands and the repo's own test command |
| `spec.md` | The five tickets. The only text either arm receives |
| `hidden_tests/` | Acceptance tests (pytest), one file per ticket |
| `reference.patch` | A reference solution for all five tickets against the pinned commit (code and tests) |
| `ticket_files.txt` | Which ticket each file of the reference belongs to; `validate.sh` uses it to prove the tickets are independent |
| `validate.sh` | Proves the kit is sound (see below). Exit 0 only if everything holds |
| `grade.sh <checkout_dir>` | Scores a finished checkout; prints JSON to stdout, one `per_part` entry per ticket |

`grade.sh` reuses the small kit's `../task/summarize.py` (JUnit report to
JSON) instead of carrying a copy, so the kit directories must stay siblings.

## The repo: Click

[pallets/click](https://github.com/pallets/click) at
`e38188e9e205005d5af5f9e59df51904c984586a` (the tip of `main` on 2026-11-12;
`pyproject.toml` says version 8.6.0.dev).

- **Permissive licence**: BSD-3-Clause.
- **Well known**: the command line toolkit behind Flask, Black, pip-tools and
  thousands of other tools. Neither arm needs the project explained.
- **Clearly separate areas, one module each**: terminal styling
  (`termui.py`, with the ANSI helpers in `_compat.py`), parameter types
  (`types.py`), help formatting (`formatting.py`), utilities such as the
  application folder (`utils.py`), and the test runner (`testing.py`), next to
  the parser, shell completion and the core command classes. A ticket in one
  area does not need to touch another, which is the property this kit is
  about. Each area also has its own existing test module, so the tests an arm
  adds stay disjoint too.
- **Fast, self-contained suite**: 2,625 tests pass in about 3 seconds
  (25 skip on this platform; a stress marker is deselected by the project's
  own configuration). Zero runtime dependencies: setup is one `uv venv` and
  one `uv pip install` of the checkout plus pytest.
- **Mature code with real gaps**: every ticket is something users plausibly
  ask for and the pinned commit does not have (checked against the code, not
  assumed), and the bug tickets reproduce on it.

Alternatives considered: TinyDB and sqlite-utils are excluded by the brief
(the other two kits use them). `python-dotenv` has the right shape (CLI,
parser, core, variable expansion) but only four modules, and its CLI tests
shell out. `jinja` and `rich` are well separated but larger, with slower
suites and more surface to learn per ticket. `httpie` and `cookiecutter` need
network-shaped or subprocess-heavy tests. `humanize` is well separated but
has no CLI, config or rendering layer to spread tickets across.

## The tickets (spec.md)

Two bug tickets and three features, about 270 lines of spec. Each is sized at
10 to 20 minutes of work for a strong model.

1. **Hyperlinks in styled text** (feature): `click.style(..., link=URL)`
   emits OSC 8 hyperlinks; `unstyle`, `echo` and width measurement treat them
   like other escape sequences; control characters in a URL are rejected.
2. **A duration parameter type** (feature): `click.Duration(min, max)`
   parses `90s`, `1h30m`, `1.5d` and bare seconds into `timedelta`, with
   strict grammar rules, bounds, metavar, info dict, and `type=timedelta` /
   timedelta defaults mapped to it.
3. **Lists in help text are run together** (bug): bulleted and numbered
   lists in command help, epilogs and option help keep one line per item with
   hanging indentation; whitespace-only lines separate paragraphs. This one
   changes the expected output of one existing test, which the spec says to
   update.
4. **`get_app_dir` returns relative paths, and has no data or cache
   folders** (bug and small feature): empty or relative `XDG_*_HOME` values
   are ignored per the XDG spec, and a new `kind=` parameter gives data,
   cache and state folders on Unix, macOS and Windows.
5. **Let `CliRunner` emulate an interactive terminal** (feature):
   `CliRunner(tty=..., terminal_width=...)` and per-call overrides, so tests
   can see colours, progress bars, `click.clear()` and narrow or wide help.

### Files per ticket

From the reference solution (`ticket_files.txt`). No file appears under two
tickets; `validate.sh` checks this, and checks that each ticket passes with
only its own files applied.

| Ticket | Source files | Test files (repo suite) | Lines added / removed |
|--------|--------------|-------------------------|-----------------------|
| 1. Hyperlinks | `src/click/termui.py`, `src/click/_compat.py` | `tests/test_utils/test_style.py` | 41 / 2 |
| 2. Duration type | `src/click/types.py`, `src/click/__init__.py` | `tests/test_types/test_Duration.py` (new) | 201 / 0 |
| 3. Help lists | `src/click/formatting.py` | `tests/test_formatting.py` | 101 / 10 |
| 4. App folders | `src/click/utils.py` | `tests/test_utils/test_get_app_dir.py` (new) | 120 / 11 |
| 5. CliRunner terminal | `src/click/testing.py` | `tests/test_testing.py` | 113 / 5 |

`_compat.py` holds Click's ANSI-stripping regex, which Ticket 1 needs and no
other ticket touches. The spec tells both arms not to edit `CHANGES.md` or
`docs/`, the two places every ticket would otherwise share.

An arm may of course solve a ticket differently and touch other files; the
table is what a natural solution needs, and the hidden tests only use public
APIs. The one place two tickets meet at runtime is harmless: Ticket 1 changes
how wide styled text measures, Ticket 3 uses that measurement when wrapping.
Neither ticket's tests need the other's change.

## Hidden tests

`hidden_tests/test_partNN_<name>.py`, one file per ticket, 89 test functions
(163 cases with parametrization):

| Ticket | File | Tests |
|--------|------|-------|
| 1 | `test_part01_hyperlinks.py` | 17 |
| 2 | `test_part02_duration.py` | 19 |
| 3 | `test_part03_help_lists.py` | 19 |
| 4 | `test_part04_app_dirs.py` | 15 |
| 5 | `test_part05_cli_runner.py` | 19 |

They test behaviour through public APIs only: `click.style`, `secho`,
`unstyle`, `echo`, `wrap_text`, `click.Duration` and `ParamType.convert`,
`get_app_dir`, `CliRunner` and its `invoke` / `isolation`, and commands run
through `CliRunner` or `Command.main`. Ticket 4's tests fake the platform the
way the spec says it is decided (`sys.platform`) and point `HOME` at a
temporary folder; the Windows rows of its table are specified but not tested,
since `click._compat.WIN` is fixed at import time.

Every hidden test fails on the pinned commit, including the bug tickets'
(each bug test reproduces the reported symptom). A module that fails to import
only zeroes its own ticket (`grade.sh` runs pytest with
`--continue-on-collection-errors`, and expected tests are counted from the
test files).

## Reference solution

`reference.patch` solves all five tickets: all 89 hidden tests pass, and the
repo's own suite passes with it (2,659 passed, 25 skipped), and also with
each ticket's files applied alone. It is 953 patch lines, 576 lines added and
28 removed over 12 files: 7 source files (399 added) and 5 test files
(177 added, one existing test's expectation changed for Ticket 3).

**Expected solo time: about an hour, not measured.** No model was run to
build this kit. Each ticket is 23 to 150 lines of reference source (plus its
tests) in one or two files, comparable to the small kit (TinyDB, 7 minutes
solo) per ticket;
five of them plus learning five different corners of Click, writing tests for
each and re-running a 3-second suite gives 45 to 75 minutes. A parallel arm's
floor is the slowest ticket (probably Ticket 2 or 3) plus coordination.

## Scripts

```sh
bash validate.sh            # about a minute with a warm uv cache; exit 0 = kit is sound
bash grade.sh <checkout>    # {"passed", "failed", "total", "per_part": {...}}
```

`validate.sh` clones the repo at the pinned commit into a temp dir, runs the
setup commands, and checks, in order: every hidden test fails on the base
commit; `ticket_files.txt` lists exactly the files `reference.patch` changes,
each under one ticket; the patch applies; for each ticket, with only that
ticket's files taken from the reference, that ticket's hidden tests all pass
and the repo's own suite passes; with the whole patch, every hidden test
passes and the repo's own suite (`test_cmd`) passes. `KEEP_WORKDIR=1` keeps
the temp dir for inspection.

`grade.sh` copies `hidden_tests/` into `<checkout>/bench_hidden_tests/`
(replacing any earlier copy) and runs it with `<checkout>/.venv/bin/python`,
the interpreter `repo.json`'s setup creates; set `GRADE_PYTHON` to use
another, and `GRADE_PYTEST_ARGS` to pass extra pytest options. It needs
`python3` on PATH for the summary. Run `setup` in the arm's checkout first if
the arm did not leave a `.venv` behind. Grading an arm's checkout runs that
arm's code: grade in a throwaway copy or sandbox.

`per_part` has one key per ticket: `part01_hyperlinks`, `part02_duration`,
`part03_help_lists`, `part04_app_dirs` and `part05_cli_runner`; each holds
`passed`, `failed` and `total`.

Requirements: `git`, `python3`, `uv` (with Python 3.12 available or
downloadable), and network access to GitHub and PyPI. No model API calls.
