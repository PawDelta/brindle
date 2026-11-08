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
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from brindle import new_tests as inventory  # noqa: E402  (the reviewer's listing shares the row matching)

APPROVERS = {"hmlerner"}
MAX_RELATED_FILES = 12    # scoped mode: related test files run beyond the changed ones
COMMON_NAME = 40           # a name found in more test files than this picks no tests
SKIP_NAMES = {"__init__", "main", "run"}


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def changed_test_files(base: str) -> list[str]:
    changed = _git("diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD", "--", "tests").split()
    return [p for p in changed if re.search(r"(^|/)test_[^/]*\.py$", p) and (ROOT / p).is_file()]


# -- listing the tests: pytest's own collection, so parametrize ids and marks are real


class _Collect:
    def __init__(self):
        self.found: dict[str, str | None] = {}

    def pytest_collection_modifyitems(self, items):
        for item in items:
            marker = item.get_closest_marker("variant")
            reason = marker.args[0] if marker and marker.args else None
            self.found[item.nodeid] = reason if isinstance(reason, str) and reason.strip() else None


def collect_here(files: list[str]) -> dict:
    """pytest's exit status and every test id in ``files`` (relative to the
    current directory) with its variant reason."""
    import pytest

    plugin = _Collect()
    with contextlib.redirect_stdout(io.StringIO()):
        status = pytest.main([*files, "--collect-only", "-q", "-o", "addopts=", "-m", "", "-k", "",
                              "-p", "no:cacheprovider"], plugins=[plugin])
    return {"status": int(status), "tests": plugin.found}


def collect_in(root: Path, files: list[str]) -> dict[str, str | None]:
    """The tests of ``files`` as listed by a separate process started in ``root``.
    A collection error or a missing file stops the check: an empty list must not
    pass for "no new tests"."""
    if not files:
        return {}
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--collect", *files],
                          cwd=root, capture_output=True, text=True)
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
    data = subprocess.run(["git", "archive", "--format=tar", base, "tests", "scripts", "pyproject.toml"],
                          cwd=ROOT, check=True, capture_output=True).stdout
    tmp = Path(tempfile.mkdtemp(prefix="base-tests-"))
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(tmp, filter="data")
        return collect_in(tmp, [f for f in files if (tmp / f).is_file()])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def new_tests(base: str) -> dict[str, str | None]:
    """Ids of the test functions and parametrize rows added since ``base``, each
    mapped to its variant reason."""
    files = changed_test_files(base)
    head = collect_in(ROOT, files)
    before = tests_at(base, files)
    added: dict[str, str | None] = {}
    for path in files:
        try:
            old_tables = inventory.test_tables(_git("show", f"{base}:{path}"))
        except subprocess.CalledProcessError:
            old_tables = {}
        new_tables = inventory.test_tables((ROOT / path).read_text(encoding="utf-8"))
        for qual, ids in _by_function(head, path).items():
            old_ids = _by_function(before, path).get(qual, [])
            for n in _new_ids(ids, old_ids, old_tables.get(qual), new_tables.get(qual)):
                added[n] = head[n]
    return added


def _by_function(tests, path: str) -> dict[str, list[str]]:
    """Test ids under ``path``, in collection order, grouped by function qualname."""
    found: dict[str, list[str]] = {}
    for n in tests:
        if n.startswith(path + "::"):
            found.setdefault(n[len(path) + 2:].split("[", 1)[0], []).append(n)
    return found


def _new_ids(ids: list[str], old_ids: list[str], old_tables, new_tables) -> list[str]:
    """The ids of one test function that are new. A row is identified by its place
    in the table's source, not by its id: pytest numbers non-literal parameters
    by position, so inserting a row shifts the ids of those after it. Where the
    table can't be matched to the ids, every row of a function whose ids changed
    is judged."""
    if not old_ids or old_tables is None:
        return ids
    if new_tables and len(new_tables) == 1 and isinstance(new_tables[0], list) and len(ids) == len(new_tables[0]):
        old = old_tables[0] if old_tables and isinstance(old_tables[0], list) else None
        return [ids[p] for p in inventory.added_positions(old, new_tables[0])]
    # Stable ids don't mean unchanged rows (a replaced value in one of two stacked
    # tables keeps `0-x0`): judge the function whenever its tables or its ids changed.
    return ids if ids != old_ids or old_tables != new_tables else []


# -- judging them by coverage


def analyse(coverage_file: Path, node_ids) -> dict[str, set | str]:
    """For each test id: "unmeasured" (it ran no measured code), "redundant"
    (it covers nothing no other test covers) or the set of (file, line) only it
    covers."""
    from coverage import CoverageData

    data = CoverageData(str(coverage_file))
    data.read()
    files = sorted(data.measured_files())

    def measured(pattern):
        data.set_query_contexts([pattern])
        lines, arcs = set(), set()
        for f in files:
            lines.update((f, n) for n in data.lines(f) or ())
            if data.has_arcs():
                arcs.update((f, a) for a in data.arcs(f) or ())
        return lines, arcs

    verdicts: dict[str, set | str] = {}
    for node_id in node_ids:
        own = re.escape(node_id) + r"(\[.*\])?\|"
        own_lines, own_arcs = measured("^" + own)
        if not own_lines:
            verdicts[node_id] = "unmeasured"
            continue
        other_lines, other_arcs = measured(f"^(?!{own})")
        only_lines = own_lines - other_lines
        only_arcs = own_arcs - other_arcs
        if not (only_lines or only_arcs):
            verdicts[node_id] = "redundant"
            continue
        verdicts[node_id] = only_lines | {(f, n) for f, arc in only_arcs for n in arc if n > 0}
    return verdicts


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


def related_test_files(names: set[str], modules: set[str], skip: set[str],
                       tests_dir: Path | None = None) -> list[str]:
    """Test files that mention the functions a new test alone covers (names found
    in too many files, like ``status``, pick nothing), then those that mention the
    modules holding them: the fallback when the name is a ``main``, an alias or a
    private helper no test names. Most mentions first."""
    tests_dir = tests_dir or ROOT / "tests"
    texts = {p.relative_to(tests_dir.parent).as_posix(): p.read_text(encoding="utf-8", errors="replace")
             for p in sorted(tests_dir.glob("test_*.py"))}
    score: dict[str, int] = {}
    for words, weight in ((names, 100), (modules, 1)):
        for word in words:
            hit = [p for p, text in texts.items() if re.search(rf"\b{re.escape(word)}\b", text)]
            if weight == 100 and len(hit) > COMMON_NAME:
                continue
            for p in hit:
                score[p] = score.get(p, 0) + weight
    ranked = sorted((p for p in score if p not in skip), key=lambda p: (-score[p], p))
    return ranked[:MAX_RELATED_FILES]


def run_with_coverage(files: list[str], coverage_file: Path, append: bool) -> set[tuple[str, str]]:
    """Run test files with per-test coverage: the parallel tests, then the serial ones.
    A failing test is the other checks' business; a pytest that couldn't run the
    files (collection error, bad usage, crash) stops this check. Returns the
    (classname, name) of every test pytest reported as skipped."""
    from xml.etree import ElementTree

    env = {**os.environ, "COVERAGE_FILE": str(coverage_file)}
    cov = ["--cov=brindle", "--cov=scripts", "--cov-branch", "--cov-context=test", "--cov-report="]
    skipped: set[tuple[str, str]] = set()
    for i, args in enumerate((["-n", "2", "-m", "not serial"], ["-m", "serial"])):
        extra = ["--cov-append"] if append or i else []
        report = coverage_file.parent / f"junit-{len(list(coverage_file.parent.glob('junit-*.xml')))}.xml"
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                               f"--junitxml={report}", *args, *cov, *extra, *files], cwd=ROOT, env=env,
                              capture_output=True, text=True)
        if proc.returncode not in (0, 1, 5):
            raise SystemExit(f"pytest exited {proc.returncode} running {len(files)} test files "
                             f"with coverage:\n{proc.stdout[-1500:]}{proc.stderr[-500:]}")
        if report.exists():
            for case in ElementTree.parse(report).iter("testcase"):
                if case.find("skipped") is not None:
                    skipped.add((case.get("classname", ""), case.get("name", "")))
    return skipped


def _was_skipped(node_id: str, skipped) -> bool:
    stem = Path(node_id.split("::")[0]).stem
    return any(name == node_id.split("::")[-1] and stem in classname.split(".") for classname, name in skipped)


def scoped_redundant(base: str, to_check: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Judge new tests by running only the changed test files, then the files naming
    what a still-unique new test alone covers. Returns (redundant, skipped,
    missing): a test that recorded no coverage and wasn't reported skipped never
    ran, which is a failure, not a pass."""
    work = Path(tempfile.mkdtemp(prefix="new-tests-cov-"))
    coverage_file = work / ".coverage"
    try:
        ran = changed_test_files(base)
        skipped = run_with_coverage(ran, coverage_file, append=False)
        if not coverage_file.exists():
            raise SystemExit("no coverage was recorded running the changed test files")
        verdicts = analyse(coverage_file, to_check)
        unique = {n: v for n, v in verdicts.items() if isinstance(v, set)}
        if unique:
            lines = set().union(*unique.values())
            more = related_test_files(enclosing_names(lines), modules_of(lines), set(ran))
            if more:
                skipped |= run_with_coverage(more, coverage_file, append=True)
                verdicts.update(analyse(coverage_file, list(unique)))
        unmeasured = [n for n, v in verdicts.items() if v == "unmeasured"]
        return ([n for n, v in verdicts.items() if v == "redundant"],
                [n for n in unmeasured if _was_skipped(n, skipped)],
                [n for n in unmeasured if not _was_skipped(n, skipped)])
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
    if not to_check:
        redundant, unmeasured = [], []
    elif args.scoped:
        redundant, unmeasured, missing = scoped_redundant(args.base, to_check)
    else:
        redundant, unmeasured = uncovered_alone(args.coverage, to_check)
    failures = [f"{n} covers no line or branch that other tests don't; delete it, fold it into "
                f"the test it repeats, or mark it @pytest.mark.variant(\"why\")" for n in redundant]
    failures += [f"{n} recorded no coverage and wasn't reported skipped: it never ran here" for n in missing]

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
