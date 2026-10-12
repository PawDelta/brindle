#!/usr/bin/env python3
"""Hold the tests a pull request adds to two rules.

1. Each new test function must cover a line or branch (of src/brindle or
   scripts/) that no other test covers, or say why it doesn't need to with
   ``@pytest.mark.variant("reason")`` (an input variant: a bypass attempt, a
   race, an input taken from a real incident).
2. A pull request may add at most MAX_NEW_TESTS test functions, and grow the
   tests by no more lines (net) than it adds to the source, or
   TEST_LINE_ALLOWANCE, whichever is larger.

Both rules are waived when a maintainer in APPROVERS has labelled the pull
request ``tests-approved``; CI works out who added the label and passes it as
``--approved-by``. Coverage comes from the suite's own CI run with per-test
contexts (``pytest --cov=brindle --cov=scripts --cov-branch --cov-context=test``).

    uv run python scripts/check_new_tests.py --base <sha> [--coverage .coverage]
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_NEW_TESTS = 15
TEST_LINE_ALLOWANCE = 150
APPROVERS = {"hmlerner"}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def _variant_reason(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for deco in fn.decorator_list:
        if (isinstance(deco, ast.Call) and ast.unparse(deco.func) == "pytest.mark.variant"
                and deco.args and isinstance(deco.args[0], ast.Constant)
                and isinstance(deco.args[0].value, str) and deco.args[0].value.strip()):
            return deco.args[0].value
    return None


def test_functions(source: str) -> dict[str, str | None]:
    """Test function qualnames (``test_x``, ``TestY::test_x``) in a module,
    each mapped to its variant reason, if it has one."""
    found: dict[str, str | None] = {}
    tree = ast.parse(source)

    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                found[prefix + node.name] = _variant_reason(node)
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                visit(node.body, f"{prefix}{node.name}::")

    visit(tree.body, "")
    return found


def new_tests(base: str) -> dict[str, str | None]:
    """Node ids (``tests/test_a.py::test_x``) of test functions added since
    ``base``, each mapped to its variant reason."""
    changed = _git("diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD", "--", "tests").split()
    added: dict[str, str | None] = {}
    for path in changed:
        if not re.search(r"(^|/)test_[^/]*\.py$", path):
            continue
        head = test_functions((ROOT / path).read_text(encoding="utf-8"))
        try:
            before = test_functions(_git("show", f"{base}:{path}"))
        except subprocess.CalledProcessError:
            before = {}
        added.update({f"{path}::{name}": reason for name, reason in head.items() if name not in before})
    return added


def line_counts(base: str, path: str) -> tuple[int, int]:
    """Lines added and removed under ``path`` since ``base``."""
    added = removed = 0
    for row in _git("diff", "--numstat", f"{base}...HEAD", "--", path).splitlines():
        plus, minus, _ = row.split("\t", 2)
        added += int(plus) if plus.isdigit() else 0
        removed += int(minus) if minus.isdigit() else 0
    return added, removed


def uncovered_alone(coverage_file: Path, node_ids) -> tuple[list[str], list[str]]:
    """(tests covering nothing no other test covers, tests that ran no code)."""
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

    redundant, unmeasured = [], []
    for node_id in node_ids:
        own = re.escape(node_id) + r"(\[.*\])?\|"
        own_lines, own_arcs = measured("^" + own)
        if not own_lines:
            unmeasured.append(node_id)
            continue
        other_lines, other_arcs = measured(f"^(?!{own})")
        if not (own_lines - other_lines or own_arcs - other_arcs):
            redundant.append(node_id)
    return redundant, unmeasured


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="the pull request's base commit")
    parser.add_argument("--coverage", type=Path, default=ROOT / ".coverage")
    parser.add_argument("--approved-by", default="", help="who labelled the pull request tests-approved")
    args = parser.parse_args(argv)

    added = new_tests(args.base)
    plus, minus = line_counts(args.base, "tests")
    test_added = plus - minus   # net: rewriting tests smaller is never held back
    src_added = line_counts(args.base, "src")[0]
    failures = []
    if len(added) > MAX_NEW_TESTS:
        failures.append(f"adds {len(added)} test functions (limit {MAX_NEW_TESTS})")
    if test_added > max(src_added, TEST_LINE_ALLOWANCE):
        failures.append(f"adds {test_added} test lines for {src_added} source lines "
                        f"(limit: the source lines, or {TEST_LINE_ALLOWANCE})")
    to_check = [n for n, reason in added.items() if reason is None]
    redundant, unmeasured = uncovered_alone(args.coverage, to_check) if to_check else ([], [])
    failures += [f"{n} covers no line or branch that other tests don't; delete it, fold it into "
                 f"the test it repeats, or mark it @pytest.mark.variant(\"why\")" for n in redundant]

    for n in unmeasured:
        print(f"note: {n} ran no measured code (skipped here?); not checked")
    for n, reason in added.items():
        if reason is not None:
            print(f"variant: {n}: {reason}")
    for failure in failures:
        print(failure)
    print(f"New tests: {len(added)} functions, {test_added} test lines, {src_added} source lines; "
          f"{len(failures)} problem(s)")
    if failures and args.approved_by in APPROVERS:
        print(f"waived: labelled tests-approved by {args.approved_by}")
        return 0
    if failures:
        print("A maintainer can waive this by labelling the pull request tests-approved and re-running the job.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
