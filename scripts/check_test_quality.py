#!/usr/bin/env python3
"""Check collected test count, parametrization products and real sleep calls.

Collection executes test imports and fixtures' parametrization, but no tests.
Run with ``uv run python scripts/check_test_quality.py``. All findings fail the
check; existing violations are not silently grandfathered. The default budget
freezes the current collected count. Change TEST_BUDGET in review when growing
the suite deliberately; --budget is available for isolated checks.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stdout
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAX_PERMUTATIONS = 12
# Freeze the collected suite, including this guardrail's tests. Raising this
# ceiling is an explicit review decision, rather than an automatic baseline.
TEST_BUDGET = 4142


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return ""


def sleep_issues(path: Path) -> list[str]:
    """Inspect calls, ignoring strings/comments and lexically mocked sleeps.

    Recognize unittest.mock.patch/patch.object contexts and decorators, and
    preceding monkeypatch.setattr calls in the same scope. Fixture-installed
    mocks cannot be proven statically; keep direct sleep calls in mock scopes.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = {"time": "time"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "time":
                    aliases[alias.asname or alias.name] = "time"
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            for alias in node.names:
                if alias.name == "sleep":
                    aliases[alias.asname or alias.name] = "time.sleep"

    def name(node):
        value = dotted(node)
        head, _, tail = value.partition(".")
        return aliases.get(head, head) + (f".{tail}" if tail else "")

    def mocks(node):
        if not isinstance(node, ast.Call):
            return False
        target = dotted(node.func)
        args = node.args
        if target.endswith("patch") and args:
            return isinstance(args[0], ast.Constant) and args[0].value == "time.sleep"
        if target.endswith(("patch.object", ".setattr")) and len(args) >= 2:
            return name(args[0]) == "time" and isinstance(args[1], ast.Constant) and args[1].value == "sleep"
        return False

    issues = []

    def visit(node, mocked=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mocked = mocked or any(mocks(d) for d in node.decorator_list)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            mocked = mocked or any(mocks(i.context_expr) for i in node.items)
        if isinstance(node, ast.Call) and name(node.func) == "time.sleep" and not mocked:
            issues.append(f"{path}:{node.lineno}: unmocked time.sleep")
        # Process statement lists in order: a patch after a sleep cannot excuse it.
        for _, value in ast.iter_fields(node):
            if isinstance(value, list):
                active = mocked
                for child in value:
                    if isinstance(child, ast.AST):
                        visit(child, active)
                        if isinstance(child, ast.Expr) and mocks(child.value):
                            active = True
            elif isinstance(value, ast.AST):
                visit(value, mocked)

    visit(tree)
    return issues


class Collection:
    def __init__(self):
        self.count = 0
        self.issues = []

    def pytest_collection_finish(self, session):
        self.count = len(session.items)
        groups = {}
        for item in session.items:
            key = (str(item.path), item.parent.nodeid, getattr(item, "originalname", item.name))
            marks = list(item.iter_markers("parametrize"))
            if len(marks) < 2:
                continue
            names = []
            for mark in marks:
                argnames = mark.args[0] if mark.args else mark.kwargs["argnames"]
                if isinstance(argnames, str):
                    names.extend(n.strip() for n in argnames.split(","))
                else:
                    names.extend(argnames)
            # Collected indices also support generators and dynamically built
            # values. Deduplicate fixture expansions unrelated to these marks.
            combinations, _ = groups.setdefault(key, (set(), item))
            combinations.add(tuple(item.callspec.indices[n] for n in names))
        for key, (combinations, item) in groups.items():
            product = len(combinations)
            if product > MAX_PERMUTATIONS:
                self.issues.append(f"{item.path}:{item.location[1] + 1}: {key[-1]}: "
                                   f"stacked parametrize generates {product} permutations "
                                   f"(limit {MAX_PERMUTATIONS})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--budget", type=int, default=TEST_BUDGET)
    args = parser.parse_args(argv)
    if args.budget < 1:
        parser.error("--budget must be positive")
    tests = args.root.resolve() / "tests"
    collection = Collection()
    output = io.StringIO()
    with redirect_stdout(output):
        status = pytest.main([str(tests), "--collect-only", "-q", "-o", "addopts=", "-m", "", "-k", ""],
                             plugins=[collection])
    if status != 0:
        print(output.getvalue(), end="")
        print(f"Test quality: collection failed (exit {status})")
        return 1
    issues = list(collection.issues)
    for path in sorted(tests.rglob("*.py")):
        issues.extend(sleep_issues(path))
    if collection.count > args.budget:
        issues.append(f"total test count {collection.count} exceeds budget {args.budget}")
    for issue in issues:
        print(issue)
    print(f"Test quality: {collection.count}/{args.budget} tests; {len(issues)} issue(s)")
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
