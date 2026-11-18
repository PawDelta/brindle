#!/usr/bin/env python3
"""Hold each test and parametrize row a branch adds to one rule.

A new test function, or a new row of a parametrized test, must cover a line or
branch (of src/brindle or scripts/) that no other test covers, or say why it
doesn't need to with ``@pytest.mark.variant("reason")`` (on the test, or
``pytest.param(..., marks=pytest.mark.variant("reason"))`` on the row): an input
variant such as a bypass attempt, a race, or an input taken from a real incident.
There is no limit on how many tests a branch adds; each one has to be worth
committing. Coverage can't tell whether a test is worth keeping, so the reviewer
is asked what bug each new test or row would catch (see src/brindle/new_tests.py).

Two ways to run it:

* CI, with the suite's per-test coverage on the pull request
  (``pytest --cov=brindle --cov=scripts --cov-branch --cov-context=test``):
  ``check_new_tests.py --base <sha> [--coverage .coverage]``. A maintainer in
  APPROVERS can waive it by labelling the pull request ``tests-approved``; CI
  passes who did as ``--approved-by``.
* ``--scoped``, on a worker's branch before review: runs the branch's added and
  changed test files, then the test files that name the functions a new test
  alone covers, with per-test coverage. Judged against that subset only, it can
  miss a repeat but never invents one. The base comes from BRINDLE_BASE_BRANCH.
  It also runs every new or changed test (and the changed vitest files) FLAKY_RUNS
  times, in parallel and in random order, and fails any test that fails in one run.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from brindle import new_tests as inventory  # noqa: E402  (the reviewer's listing shares the row matching)

APPROVERS = {"hmlerner"}
HERE = Path(__file__).resolve().parent
FLAKY_RUNS = 5             # scoped mode: runs of every new or changed test
MAX_RELATED_FILES = 12   # scoped mode: related test files run beyond the changed ones
COMMON_NAME = 40           # a name found in more test files than this picks no tests
SKIP_NAMES = {"__init__", "main", "run"}


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def changed_test_files(base: str) -> list[str]:
    changed = _git("diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD", "--", "tests").split()
    return [p for p in changed if re.search(r"(^|/)test_[^/]*\.py$", p) and (ROOT / p).is_file()]


def base_paths(base: str) -> dict[str, str]:
    """Each test file the branch renamed (or moved), mapped to its path at ``base``:
    its tests are judged against the file it was, not as all new."""
    return inventory.renames(_git(*inventory.RENAMES, f"{base}...HEAD", "--", "tests"))


# -- listing the tests: pytest's own collection, so parametrize ids and marks are real


class _Collect:
    def __init__(self):
        self.found: dict[str, str | None] = {}

    def pytest_collection_modifyitems(self, items):
        for item in items:
            marker = item.get_closest_marker("variant")
            reason = marker.args[0] if marker and marker.args else None
            self.found[item.nodeid] = reason if isinstance(reason, str) and reason.strip() else None


def _use_source(src: str) -> None:
    """Import brindle from ``src`` (a base's own source): drop any brindle already
    imported and the editable install's import hook, which would otherwise win over
    sys.path and load the branch's code under the base's tests."""
    sys.meta_path[:] = [f for f in sys.meta_path if "editable" not in repr(f).lower()]
    for name in [m for m in sys.modules if m == "brindle" or m.startswith("brindle.")]:
        del sys.modules[name]
    sys.path.insert(0, src)


def collect_here(files: list[str]) -> dict:
    """pytest's exit status and every test id in ``files`` (relative to the
    current directory) with its variant reason."""
    import pytest

    if os.environ.get("BRINDLE_COLLECT_SRC"):
        _use_source(os.environ["BRINDLE_COLLECT_SRC"])

    plugin = _Collect()
    with contextlib.redirect_stdout(io.StringIO()):
        status = pytest.main([*files, "--collect-only", "-q", "-o", "addopts=", "-m", "", "-k", "",
                              "-p", "no:cacheprovider"], plugins=[plugin])
    return {"status": int(status), "tests": plugin.found}


def collect_in(root: Path, files: list[str], src: Path | None = None) -> dict[str, str | None]:
    """The tests of ``files`` as listed by a separate process started in ``root``,
    importing brindle from ``src`` when given (a base's own source, so its tests
    collect against the code they were written for).
    A collection error or a missing file stops the check: an empty list must not
    pass for "no new tests"."""
    if not files:
        return {}
    env = None
    if src is not None:
        env = {**os.environ, "BRINDLE_COLLECT_SRC": str(src)}
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--collect", *files],
                          cwd=root, capture_output=True, text=True, env=env)
    try:
        listed = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        listed = {"status": -1, "tests": {}}
    if listed["status"] not in (0, 5):
        raise SystemExit(f"could not list the tests in {', '.join(files)} "
                         f"(pytest exit {listed['status']}):\n{proc.stderr[-2000:]}")
    return listed["tests"]


def tests_at(base: str, files: list[str]) -> dict[str, str | None]:
    """The test ids ``files`` had at ``base`` (files that didn't exist have none)."""
    data = subprocess.run(["git", "archive", "--format=tar", base, "src", "tests", "scripts", "pyproject.toml"],
                          cwd=ROOT, check=True, capture_output=True).stdout
    tmp = Path(tempfile.mkdtemp(prefix="base-tests-"))
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(tmp, filter="data")
        return collect_in(tmp, [f for f in files if (tmp / f).is_file()], src=tmp / "src")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def new_tests(base: str) -> dict[str, str | None]:
    """Ids of the test functions and parametrize rows added since ``base``, each
    mapped to its variant reason."""
    files = changed_test_files(base)
    was = base_paths(base)
    head = collect_in(ROOT, files)
    before = tests_at(base, [was.get(p, p) for p in files])
    added: dict[str, str | None] = {}
    for path in files:
        old_path = was.get(path, path)
        try:
            old_tables = inventory.scan(_git("show", f"{base}:{old_path}"))
        except subprocess.CalledProcessError:
            old_tables = {}
        new_tables = inventory.scan((ROOT / path).read_text(encoding="utf-8"))
        found: list[str] = []
        old_by = _by_function(before, old_path)
        for qual, ids in _by_function(head, path).items():
            old_ids = old_by.get(qual, [])
            candidates = _new_ids(ids, old_ids, old_tables.get(qual), new_tables.get(qual))
            # A function that shrinks or stays level adds nothing: its surviving rows
            # got new ids, they are not new tests. Otherwise only the growth counts.
            growth = max(0, len(ids) - len(old_ids))
            found += candidates[len(candidates) - growth:] if growth else []
        # Tests folded in from tests deleted in the same diff don't count unless
        # the file as a whole collects more than it did at the base.
        # Only a file that lost a whole test function is capped this way; unrelated
        # tables shrinking elsewhere in the file must not hide a table that grew.
        if set(old_by) - set(_by_function(head, path)):
            file_growth = max(0, sum(1 for n in head if n.startswith(path + "::"))
                              - sum(1 for n in before if n.startswith(old_path + "::")))
            found = found[len(found) - file_growth:] if file_growth else []
        for n in found:
            added[n] = head[n]
    return added


def _by_function(tests, path: str) -> dict[str, list[str]]:
    """Test ids under ``path``, in collection order, grouped by function qualname."""
    found: dict[str, list[str]] = {}
    for n in tests:
        if n.startswith(path + "::"):
            found.setdefault(n[len(path) + 2:].split("[", 1)[0], []).append(n)
    return found


def _new_ids(ids: list[str], old_ids: list[str], old, new) -> list[str]:
    """The ids of one test function that are new; ``old`` and ``new`` are its
    (tables, inputs) as ``inventory.scan`` reads them. A row is identified by its
    place in the table's source, not by its id: pytest numbers non-literal
    parameters by position, so inserting a row shifts the ids of those after it.
    When an assignment the tables use changed, or the table can't be matched to
    the ids, every row of the function is judged."""
    if not old_ids or old is None:
        return ids
    (old_tables, old_inputs), (new_tables, new_inputs) = old, new
    if old_inputs != new_inputs:
        return ids
    if len(new_tables) == 1 and isinstance(new_tables[0], list) and len(ids) == len(new_tables[0]):
        before = old_tables[0] if old_tables and isinstance(old_tables[0], list) else None
        return [ids[p] for p in inventory.added_positions(before, new_tables[0])]
    # Stable ids don't mean unchanged rows (a replaced value in one of two stacked
    # tables keeps `0-x0`): judge the function whenever its tables or its ids changed.
    return ids if ids != old_ids or old_tables != new_tables else []


# -- judging them by coverage


def _owned_contexts(contexts: list[tuple[str, int]], node_ids) -> dict[str, list[int]]:
    """For each test id, the ids of its coverage contexts (``<id>|run``, and its
    rows' ``<id>[x]|setup`` ...); ``contexts`` is sorted by name."""
    names = [c for c, _ in contexts]
    owned: dict[str, list[int]] = {}
    for node_id in node_ids:
        own = re.compile(re.escape(node_id) + r"(\[.*\])?\|")
        i = bisect.bisect_left(names, node_id)
        found = owned.setdefault(node_id, [])
        while i < len(names) and names[i].startswith(node_id):
            if own.match(names[i]):
                found.append(contexts[i][1])
            i += 1
    return owned


def analyse(coverage_file: Path, node_ids) -> dict[str, set | str]:
    """For each test id: "unmeasured" (it ran no measured code), "redundant"
    (it covers nothing no other test covers) or the set of (file, line) only it
    covers.

    Reads the coverage database directly, touching only what the new tests ran:
    their arcs (or lines), and for each of those how many contexts in all ran
    it. A test covers one alone when every context that ran it is the test's own.
    After two passes over the data, each test costs one indexed query."""
    if not Path(coverage_file).is_file():
        raise SystemExit(f"no coverage data at {coverage_file}")
    con = sqlite3.connect(f"file:{Path(coverage_file).resolve()}?mode=ro", uri=True)
    try:
        contexts = sorted(con.execute("select context, id from context"))
        owned = _owned_contexts(contexts, node_ids)
        con.execute("create temp table mine (context_id integer, test integer)")
        con.executemany("insert into mine values (?, ?)",
                        [(c, t) for t, n in enumerate(owned) for c in owned[n]])
        con.execute("create index temp.mine_context on mine (context_id)")
        has_arcs = con.execute("select value from meta where key = 'has_arcs'").fetchone()
        if has_arcs and int(has_arcs[0]):
            _arc_counts(con)
        else:
            _line_counts(con)
        con.execute("create index temp.own_test on own (test)")
        verdicts: dict[str, set | str] = {}
        for t, node_id in enumerate(owned):
            rows = con.execute(
                "select f.path, o.fromno, o.tono, o.n = a.n from own o"
                " join everyone a on a.file_id = o.file_id and a.fromno = o.fromno and a.tono = o.tono"
                " join file f on f.id = o.file_id where o.test = ?", (t,)).fetchall()
            if not any(n > 0 for _, *arc, _ in rows for n in arc):
                verdicts[node_id] = "unmeasured"
                continue
            only = [(path, arc) for path, *arc, alone in rows if alone]
            verdicts[node_id] = ({(path, n) for path, arc in only for n in arc if n > 0}
                                 if only else "redundant")
        return verdicts
    finally:
        con.close()


def _arc_counts(con) -> None:
    """Temp tables ``own`` (each new test's arcs, with how many of its contexts
    ran each) and ``everyone`` (how many contexts in all ran each of those arcs)."""
    # cross join: one pass over arc, looking each row's context up in `mine`
    con.execute("create temp table own as select m.test, a.file_id, a.fromno, a.tono, count(*) as n"
                " from arc a cross join mine m where m.context_id = a.context_id group by 1, 2, 3, 4")
    con.execute("create index temp.own_arc on own (file_id, fromno, tono)")
    con.execute("create temp table everyone as select a.file_id, a.fromno, a.tono, count(*) as n from arc a"
                " where exists (select 1 from own o where o.file_id = a.file_id and o.fromno = a.fromno"
                " and o.tono = a.tono) group by 1, 2, 3")
    con.execute("create index temp.everyone_arc on everyone (file_id, fromno, tono)")


def _line_counts(con) -> None:
    """The same for line data, each line stored as the arc (line, line)."""
    from coverage.numbits import numbits_to_nums

    own: dict[tuple, int] = {}
    for test, file_id, bits in con.execute(
            "select m.test, l.file_id, l.numbits from line_bits l join mine m on m.context_id = l.context_id"):
        for n in numbits_to_nums(bits):
            own[test, file_id, n] = own.get((test, file_id, n), 0) + 1
    files = {file_id for _, file_id, _ in own}
    everyone: dict[tuple, int] = {}
    for file_id, bits in con.execute("select file_id, numbits from line_bits"):
        if file_id in files:
            for n in numbits_to_nums(bits):
                everyone[file_id, n] = everyone.get((file_id, n), 0) + 1
    con.execute("create temp table own (test integer, file_id integer, fromno integer, tono integer, n integer)")
    con.executemany("insert into own values (?, ?, ?, ?, ?)", [(t, f, n, n, c) for (t, f, n), c in own.items()])
    con.execute("create temp table everyone (file_id integer, fromno integer, tono integer, n integer)")
    con.executemany("insert into everyone values (?, ?, ?, ?)", [(f, n, n, c) for (f, n), c in everyone.items()])
    con.execute("create index temp.everyone_arc on everyone (file_id, fromno, tono)")


def uncovered_alone(coverage_file: Path, node_ids) -> tuple[list[str], list[str]]:
    """(tests covering nothing no other test covers, tests that ran no code)."""
    verdicts = analyse(coverage_file, node_ids)
    return ([n for n, v in verdicts.items() if v == "redundant"],
            [n for n, v in verdicts.items() if v == "unmeasured"])


# -- the scoped run


def enclosing_names(lines) -> set[str]:
    """Names of the innermost functions (and classes) holding the given (file, line) pairs."""
    names: set[str] = set()
    by_file: dict[str, set[int]] = {}
    for f, n in lines:
        by_file.setdefault(f, set()).add(n)
    for f, wanted in by_file.items():
        try:
            tree = ast.parse(Path(f).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        defs = [n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        for line in wanted:
            holders = [d for d in defs if d.lineno <= line <= (d.end_lineno or d.lineno)]
            if holders:
                names.add(max(holders, key=lambda d: d.lineno).name)
    return names - SKIP_NAMES


def modules_of(lines) -> set[str]:
    """Module names (``agents``) of the src/brindle and scripts files in (file, line) pairs."""
    return {Path(f).stem for f, _ in lines
            if re.search(r"/(src/brindle|scripts)/", f.replace("\\", "/")) and Path(f).stem != "__init__"}


def coverage_sources(lines) -> list[str]:
    """``--cov`` sources measuring just the files holding (file, line) pairs: a
    src/brindle file by its module name (``brindle.native.tools``), scripts/ as a whole."""
    found = set()
    for f, _ in lines:
        posix = f.replace("\\", "/")
        if m := re.search(r"/src/(brindle/.+)\.py$", posix):
            found.add(re.sub(r"\.__init__$", "", m[1].replace("/", ".")))
        elif re.search(r"/scripts/", posix):
            found.add("scripts")
    return sorted(found)


def related_test_files(names: set[str], modules: set[str], skip: set[str],
                       tests_dir: Path | None = None) -> list[str]:
    """Test files that mention the functions a new test alone covers (names found
    in too many files, like ``status``, pick nothing), then those that mention the
    modules holding them: the fallback when the name is a ``main``, an alias or a
    private helper no test names. Most mentions first."""
    tests_dir = tests_dir or ROOT / "tests"
    # each file's words once: a set lookup per name, not a regex scan of every file per name
    words_in = {p.relative_to(tests_dir.parent).as_posix():
                set(re.findall(r"\w+", p.read_text(encoding="utf-8", errors="replace")))
                for p in sorted(tests_dir.glob("test_*.py"))}
    score: dict[str, int] = {}
    for words, weight in ((names, 100), (modules, 1)):
        for word in words:
            hit = [p for p, found in words_in.items() if word in found]
            if weight == 100 and len(hit) > COMMON_NAME:
                continue
            for p in hit:
                score[p] = score.get(p, 0) + weight
    ranked = sorted((p for p in score if p not in skip), key=lambda p: (-score[p], p))
    return ranked[:MAX_RELATED_FILES]


def _plugin_env(**extra: str) -> dict[str, str]:
    path = os.pathsep.join(filter(None, [str(HERE), os.environ.get("PYTHONPATH", "")]))
    return {**os.environ, "PYTHONPATH": path, **extra}


def run_with_coverage(files: list[str], coverage_file: Path, append: bool, report: Path | None = None,
                      sources: list[str] | None = None) -> set[str]:
    """Run test files with per-test coverage: the parallel tests, then the serial ones.
    A failing test is the other checks' business; a pytest that couldn't run the
    files (collection error, bad usage, crash) stops this check. Returns the full
    node id of every test pytest reported as skipped. With ``report``, the run is
    also the first of the flaky check's runs, its results written to
    ``<report>-<pass>.xml``. It keeps collection order: a shuffled order moves
    first-call lines between tests, which changes the related files picked and so
    the verdicts. ``sources`` narrows what is measured (by default all of brindle
    and scripts/)."""
    skip_file = coverage_file.parent / "skipped.txt"
    env = _plugin_env(COVERAGE_FILE=str(coverage_file), BRINDLE_SKIPPED_FILE=str(skip_file))
    cov = [*(f"--cov={s}" for s in sources or ["brindle", "scripts"]),
           "--cov-branch", "--cov-context=test", "--cov-report="]
    passes = [["-n", "2", "-m", "not serial"]] + ([["-m", "serial"]] if _has_serial(files) else [])
    for i, args in enumerate(passes):
        extra = ["--cov-append"] if append or i else []
        extra += [f"--junitxml={report}-{i}.xml"] if report else []
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                               "-p", "new_tests_plugin", *args, *cov, *extra, *files], cwd=ROOT, env=env,
                              capture_output=True, text=True)
        if proc.returncode not in (0, 1, 5):
            raise SystemExit(f"pytest exited {proc.returncode} running {len(files)} test files "
                             f"with coverage:\n{proc.stdout[-1500:]}{proc.stderr[-500:]}")
    return set(skip_file.read_text(encoding="utf-8").split("\n")) if skip_file.exists() else set()


def _has_serial(files: list[str]) -> bool:
    """Whether any of ``files`` may hold a serial test: a ``….serial`` attribute
    anywhere in it (on a test, its class, ``pytestmark`` or a row of a table built
    elsewhere in the module), or a file that can't be read. The serial pass is a
    pytest start-up, skipped only when no file can need it."""
    for path in files:
        try:
            tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            return True
        if any(isinstance(node, ast.Attribute) and node.attr == "serial" for node in ast.walk(tree)):
            return True
    return False


def _was_skipped(node_id: str, skipped) -> bool:
    return node_id in skipped


def scoped_redundant(base: str, to_check: list[str],
                     first_run: set[str] | None = None) -> tuple[list[str], list[str], list[str]]:
    """Judge new tests by running only the changed test files, then the files naming
    what a still-unique new test alone covers. Returns (redundant, skipped,
    missing): a test that recorded no coverage and wasn't reported skipped never
    ran, which is a failure, not a pass. Given ``first_run``, the run of the
    changed files doubles as the flaky check's first run: the tests that failed
    in it are added to that set."""
    work = Path(tempfile.mkdtemp(prefix="new-tests-cov-"))
    coverage_file = work / ".coverage"
    try:
        ran = changed_test_files(base)
        if first_run is None:
            skipped = run_with_coverage(ran, coverage_file, append=False)
        else:
            skipped = run_with_coverage(ran, coverage_file, append=False, report=work / "run1")
            first_run |= _junit_failures(sorted(work.glob("run1-*.xml")))
        if not coverage_file.exists():
            raise SystemExit("no coverage was recorded running the changed test files")
        verdicts = analyse(coverage_file, to_check)
        unique = {n: v for n, v in verdicts.items() if isinstance(v, set)}
        if unique:
            lines = set().union(*unique.values())
            more = related_test_files(enclosing_names(lines), modules_of(lines), set(ran))
            if more:
                # only the files holding the lines still in question need measuring
                skipped |= run_with_coverage(more, coverage_file, append=True, sources=coverage_sources(lines))
                verdicts.update(analyse(coverage_file, list(unique)))
        unmeasured = [n for n, v in verdicts.items() if v == "unmeasured"]
        return ([n for n, v in verdicts.items() if v == "redundant"],
                [n for n in unmeasured if _was_skipped(n, skipped)],
                [n for n in unmeasured if not _was_skipped(n, skipped)])
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -- flaky tests: the new and changed tests, repeated


def _function_dumps(source: str) -> dict[str, str]:
    found: dict[str, str] = {}

    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                # decorators (parametrize tables) are left out: rows are judged as
                # added ids, so a trimmed table doesn't make the whole test "changed"
                found[prefix + node.name] = ast.dump(node.args) + ast.dump(ast.Module(body=node.body, type_ignores=[]))
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                visit(node.body, f"{prefix}{node.name}::")

    visit(ast.parse(source).body, "")
    return found


def selected_python(base: str, added) -> list[str]:
    """Node ids of the new tests and rows, and of every test function a changed test
    file changed (all its rows)."""
    selected = set()
    was = base_paths(base)
    for path in changed_test_files(base):
        try:
            old = _function_dumps(_git("show", f"{base}:{was.get(path, path)}"))
        except subprocess.CalledProcessError:
            old = {}
        new = _function_dumps((ROOT / path).read_text(encoding="utf-8"))
        selected |= {f"{path}::{q}" for q, dump in new.items() if old.get(q) != dump}
    selected |= {n for n in added if n.split("[", 1)[0] not in selected}
    return sorted(selected)


def _junit_failures(reports) -> set[str]:
    """``classname.name`` of each test that failed or errored in pytest junit reports."""
    from xml.etree import ElementTree

    found = set()
    for report in reports:
        if Path(report).exists():
            for case in ElementTree.parse(report).iter("testcase"):
                if case.find("failure") is not None or case.find("error") is not None:
                    found.add(f"{case.get('classname')}.{case.get('name')}")
    return found


def _junit_name(node_id: str) -> str:
    """A node id as pytest's junit report names it: ``tests.test_a.TestX.test_y[p]``."""
    path, bracket, params = node_id.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    return ".".join(names) + bracket + params


def flaky_python(selected: list[str], first_run: set[str] | None = None) -> dict[str, int]:
    """Run ``selected`` FLAKY_RUNS times, each time in parallel (-n 2) and in a
    different random order; returns how many runs each failing test failed in.
    ``first_run``: the failures of a run already made (the scoped coverage run,
    in parallel and in collection order), which then counts as the first of the
    FLAKY_RUNS; the others are shuffled with seeds 2 and up."""
    if not selected:
        return {}
    serial = _has_serial(sorted({n.partition("::")[0] for n in selected}))
    failed: dict[str, int] = {}
    if first_run is not None:   # only the selected tests' failures: the rest of their files is not repeated
        wanted = {_junit_name(n) for n in selected}
        for name in first_run:
            if name in wanted or name.split("[", 1)[0] in wanted:
                failed[name] = 1
    work = Path(tempfile.mkdtemp(prefix="new-tests-flaky-"))
    try:
        for run in range(1 if first_run is None else 2, FLAKY_RUNS + 1):
            env = _plugin_env(BRINDLE_SHUFFLE_SEED=str(run))
            reports = []
            for part, args in enumerate((["-n", "2", "-m", "not serial"], ["-m", "serial"])):
                if part and not serial:
                    continue
                reports.append(work / f"run{run}-{part}.xml")
                proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                       "-p", "new_tests_plugin", f"--junitxml={reports[-1]}", *args, *selected],
                                      cwd=ROOT, env=env, capture_output=True, text=True)
                if proc.returncode not in (0, 1, 5):
                    raise SystemExit(f"pytest exited {proc.returncode} repeating the new and changed tests "
                                     f"(run {run}):\n{proc.stdout[-1500:]}{proc.stderr[-500:]}")
            for name in _junit_failures(reports):
                failed[name] = failed.get(name, 0) + 1
        return failed
    finally:
        shutil.rmtree(work, ignore_errors=True)


def flaky_vitest(base: str) -> dict[str, int]:
    """The same for the vitest files the branch changed: FLAKY_RUNS runs in a random order each."""
    names = _git("diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD", "--", "app").split()
    files = [n[len("app/"):] for n in names
             if re.search(r"\.(test|spec)\.[jt]sx?$", n) and (ROOT / n).is_file()]
    if not files:
        return {}
    app = ROOT / "app"
    if not (app / "node_modules").is_dir():
        subprocess.run(["npm", "ci", "--silent"], cwd=app, check=True, capture_output=True)
    failed: dict[str, int] = {}
    work = Path(tempfile.mkdtemp(prefix="new-tests-vitest-"))
    try:
        for run in range(1, FLAKY_RUNS + 1):
            report = work / f"run{run}.json"
            proc = subprocess.run(["npx", "vitest", "run", *files, "--sequence.shuffle",
                                   f"--sequence.seed={run}", "--reporter=json", f"--outputFile={report}"],
                                  cwd=app, capture_output=True, text=True)
            if not report.exists():
                raise SystemExit(f"vitest wrote no report (exit {proc.returncode}) repeating the changed "
                                 f"test files (run {run}):\n{proc.stdout[-1000:]}{proc.stderr[-1000:]}")
            data = json.loads(report.read_text(encoding="utf-8"))
            this_run = set()
            for suite in data.get("testResults", []):
                file = Path(suite.get("name", "")).name
                named = [f"{file}: {c.get('fullName')}" for c in suite.get("assertionResults", [])
                         if c.get("status") == "failed"]
                if not named and suite.get("status") == "failed":
                    named = [f"{file}: the suite failed ({(suite.get('message') or '')[:200]})"]
                this_run.update(named)
            if not this_run and (proc.returncode != 0 or data.get("success") is False):
                this_run.add(f"vitest (exit {proc.returncode}): the run failed without naming a test")
            for name in this_run:
                failed[name] = failed.get(name, 0) + 1
        return failed
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.environ.get("BRINDLE_BASE_BRANCH", ""),
                        help="the base commit (default: BRINDLE_BASE_BRANCH)")
    parser.add_argument("--coverage", type=Path, default=ROOT / ".coverage")
    parser.add_argument("--approved-by", default="", help="who labelled the pull request tests-approved")
    parser.add_argument("--scoped", action="store_true",
                        help="run the related tests here instead of reading a finished coverage file")
    parser.add_argument("--collect", nargs="+", metavar="FILE", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.collect:
        sys.__stdout__.write(json.dumps(collect_here(args.collect)) + "\n")
        return 0
    if not args.base:
        if args.scoped:
            print("no base branch (BRINDLE_BASE_BRANCH unset): new tests not checked")
            return 0
        parser.error("--base is required")

    added = new_tests(args.base)
    to_check = [n for n, reason in added.items() if reason is None]
    missing: list[str] = []
    first_run: set[str] | None = None   # the scoped coverage run, reused as the first flaky run
    if not to_check:
        redundant, unmeasured = [], []
    elif args.scoped:
        first_run = set()
        redundant, unmeasured, missing = scoped_redundant(args.base, to_check, first_run)
    else:
        redundant, unmeasured = uncovered_alone(args.coverage, to_check)
    failures = [f"{n} covers no line or branch that other tests don't; delete it, fold it into "
                f"the test it repeats, or mark it @pytest.mark.variant(\"why\")" for n in redundant]
    failures += [f"{n} recorded no coverage and wasn't reported skipped: it never ran here" for n in missing]
    if args.scoped:
        flaky = flaky_python(selected_python(args.base, added), first_run)
        for n, count in {**flaky, **flaky_vitest(args.base)}.items():
            failures.append(f"{n} is flaky: it failed in {count} of {FLAKY_RUNS} runs "
                            "(repeated in parallel, in random order); make it deterministic")

    for n in unmeasured:
        print(f"note: {n} ran no measured code (skipped here?); not checked")
    for n, reason in added.items():
        if reason is not None:
            print(f"variant: {n}: {reason}")
    for failure in failures:
        print(failure)
    print(f"New tests: {len(added)} functions or rows; {len(failures)} problem(s)")
    if failures and args.approved_by in APPROVERS:
        print(f"waived: labelled tests-approved by {args.approved_by}")
        return 0
    if failures and not args.scoped:
        print("A maintainer can waive this by labelling the pull request tests-approved and re-running the job.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
