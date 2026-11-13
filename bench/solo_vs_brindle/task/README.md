# Task kit: solo Fable 5.2 vs brindle

One benchmark task, the same for both arms: a single Claude Code session on
Fable 5.2 ("solo") and a brindle session ("brindle"). Each arm gets a fresh
checkout of the repo below plus `spec.md`, and nothing else. The hidden tests,
the reference patch and the grading scripts stay outside the arm's checkout.

| File | What it is |
|------|------------|
| `repo.json` | Target repo, pinned commit, setup commands and the repo's own test command |
| `spec.md` | The feature request. The only text either arm receives |
| `hidden_tests/` | Acceptance tests (pytest), one file per spec part |
| `reference.patch` | A reference solution against the pinned commit (code plus a small test file) |
| `validate.sh` | Proves the kit is sound (see below). Exit 0 only if everything holds |
| `grade.sh <checkout_dir>` | Scores a finished checkout; prints JSON to stdout |
| `summarize.py` | Helper for `grade.sh`: JUnit report to JSON |

## The repo: TinyDB

[msiemens/tinydb](https://github.com/msiemens/tinydb) at
`edff96aeb2616402e9489d46e90a592c05565621` (the tip of `master` on
2026-11-13; `pyproject.toml` says version 4.9.0).

- **Permissive licence**: MIT.
- **Fast, dependency-light tests**: pure Python with zero runtime
  dependencies; the suite is 228 tests in under a second and needs only
  `pytest`, `pytest-cov` (its `pytest.ini` turns coverage on) and `pyyaml`.
  Setup is one `uv venv` plus one `uv pip install`, a few seconds with a warm
  cache, so the whole `validate.sh` run takes seconds, not minutes.
- **Well known and small enough to read**: about 2,300 lines in `tinydb/`, so
  neither arm spends its budget just finding its way around, but the code has
  real structure to respect (storages, middlewares, the query cache, the
  `TinyDB` → default-table forwarding, the read-modify-write `_update_table`).
- **Realistic room for a multi-part feature**: TinyDB users routinely ask for
  sorting, paging, projections, aggregates, unique fields and change hooks,
  none of which exist. They fit the existing design without new dependencies.

Alternatives considered: `click` and `rich` have larger suites and more
surface to learn; `python-markdown`'s features are extension-shaped and
harder to grade through a public API; `httpie` needs network-shaped tests.

## The task (spec.md)

Five parts that are separable but share code, roughly 2–4 hours for a strong
engineer:

1. `Table.find`: filter, multi-key ordering with missing/null-last and stable
   ties in both directions, `skip`/`limit`.
2. Projection with `find(fields=...)`, keeping nested structure.
3. Aggregation: `distinct`, `aggregate(count/sum/avg/min/max)`, `group_by`.
4. Unique constraints: `ensure_unique` and `UniqueViolation`, enforced
   atomically on every write path.
5. Change notifications: `Table.watch(callback)` with insert/update/remove
   events after the write is committed.

Shared machinery: dotted field paths (parts 1–4) and the table write path
(parts 4–5). The traps a careful engineer has to notice are all stated in the
spec: `MemoryStorage` hands out its live dicts, so a rejected or failing
update must not mutate them in place; callbacks must run after the query
cache is cleared; descending sorts must keep missing values last and ties
stable; booleans are not numbers for `sum`/`avg`; the `TinyDB` instance must
expose everything for its default table.

## Hidden tests

`hidden_tests/test_partN_<name>.py`, one file per part (54 tests: 12, 7, 11,
12, 12). They use only the public API the spec names (`Table.find`,
`distinct`, `aggregate`, `group_by`, `ensure_unique`, `watch`,
`tinydb.UniqueViolation`) plus TinyDB's existing API, and never internal
names. Each part is graded on its own: part 5 does not need part 4, and a
module that fails to import only zeroes its own part (`grade.sh` runs pytest
with `--continue-on-collection-errors`, and counts expected tests from the
test files, so an import error still counts every test in that file as
failed). Part 2 necessarily calls `find`, since `fields` is a `find`
parameter.

## Scripts

```sh
bash validate.sh            # ~5 s with a warm uv cache; exit 0 = kit is sound
bash grade.sh <checkout>    # {"passed", "failed", "total", "per_part": {...}}
```

`validate.sh` clones the repo at the pinned commit into a temp dir, runs the
setup commands, checks every hidden test fails on the base commit, applies
`reference.patch`, checks every hidden test passes, then runs the repo's own
suite (`test_cmd`). `KEEP_WORKDIR=1` keeps the temp dir for inspection.

`grade.sh` copies `hidden_tests/` into `<checkout>/bench_hidden_tests/`
(replacing any earlier copy) and runs it with `<checkout>/.venv/bin/python`,
the interpreter `repo.json`'s setup creates; set `GRADE_PYTHON` to use another.
It needs `python3` on PATH for the summary. Run `setup` in the arm's checkout
first if the arm did not leave a `.venv` behind. Grading an arm's checkout
runs that arm's code: grade in a throwaway copy or sandbox.

`per_part` keys are `part1_find`, `part2_projection`, `part3_aggregation`,
`part4_unique` and `part5_watch`; each holds `passed`, `failed` and `total`.

Requirements: `git`, `python3`, `uv` (with Python 3.12 available or
downloadable), and network access to GitHub and PyPI. No model API calls.
