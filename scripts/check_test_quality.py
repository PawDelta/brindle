#!/usr/bin/env python3
"""Check collected test count, parametrization products and real sleep calls.

Collection executes test imports and fixtures' parametrization, but no tests.
Run with ``uv run python scripts/check_test_quality.py``. The checked-in baseline
grandfathers specific legacy findings; new findings fail. ``--strict`` shows all
findings. ``--dump-baseline`` prints proposed baseline JSON for manual review;
it never edits the baseline. The default budget
freezes the current collected count. Change TEST_BUDGET in review when growing
the suite deliberately; --budget is available for isolated checks.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAX_PERMUTATIONS = 12
# Freeze the collected suite, including this guardrail's tests. Raising this
# ceiling is an explicit review decision, rather than an automatic baseline.
TEST_BUDGET = 4148


class Finding(NamedTuple):
    key: str
    message: str


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return ""


def sleep_issues(path: Path, root: Path | None = None) -> list[Finding]:
    """Inspect calls, ignoring strings/comments and lexically mocked sleeps.

    Recognize unittest.mock.patch/patch.object contexts and decorators, and
    preceding monkeypatch.setattr calls or explicitly started patchers in the
    same scope. Patching the time module does not replace direct sleep imports.
    Fixture-installed
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
                    # A direct import retains its original function object when
                    # time.sleep is later replaced. Keep its binding separate.
                    binding = alias.asname or alias.name
                    aliases[binding] = f"imported:{binding}"

    def name(node):
        value = dotted(node)
        head, _, tail = value.partition(".")
        return aliases.get(head, head) + (f".{tail}" if tail else "")

    def patch_target(node):
        if not isinstance(node, ast.Call):
            return frozenset()
        target = dotted(node.func)
        args = node.args
        if target.endswith("patch") and args:
            if isinstance(args[0], ast.Constant) and args[0].value == "time.sleep":
                return frozenset({"time.sleep"})
        if target.endswith("patch.object") and len(args) >= 2:
            if name(args[0]) == "time" and isinstance(args[1], ast.Constant) and args[1].value == "sleep":
                return frozenset({"time.sleep"})
        return frozenset()

    def immediate_target(node, patchers):
        if not isinstance(node, ast.Call):
            return frozenset()
        if dotted(node.func).endswith(".setattr") and len(node.args) >= 2:
            if name(node.args[0]) == "time" and isinstance(node.args[1], ast.Constant) and node.args[1].value == "sleep":
                return frozenset({"time.sleep"})
        if isinstance(node.func, ast.Attribute) and node.func.attr == "start":
            return patch_target(node.func.value) or patchers.get(dotted(node.func.value), frozenset())
        return frozenset()

    issues = []

    relative = path.relative_to(root).as_posix() if root else path.name

    def visit(node, mocked=frozenset(), scope=()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mocked = mocked.union(*(patch_target(d) for d in node.decorator_list))
        if isinstance(node, (ast.With, ast.AsyncWith)):
            mocked = mocked.union(*(patch_target(i.context_expr) for i in node.items))
        binding = name(node.func) if isinstance(node, ast.Call) else ""
        if (binding == "time.sleep" or binding.startswith("imported:")) and binding not in mocked:
            # Stable across unrelated line edits, but specific to the scope,
            # call expression and number of occurrences. A new sleep in another
            # test or another identical call cannot borrow a file-wide waiver.
            key = f"sleep:{relative}::{'.'.join(scope)}::{ast.unparse(node)}"
            issues.append(Finding(key, f"{path}:{node.lineno}: unmocked time.sleep"))
        # Process statement lists in order: a patch after a sleep cannot excuse it.
        for _, value in ast.iter_fields(node):
            if isinstance(value, list):
                active = mocked
                patchers = {}
                for child in value:
                    if isinstance(child, ast.AST):
                        visit(child, active, scope)
                        if isinstance(child, ast.Assign):
                            targets = patch_target(child.value)
                            for target in child.targets:
                                if isinstance(target, ast.Name):
                                    patchers[target.id] = targets
                        if isinstance(child, ast.Expr):
                            active = active | immediate_target(child.value, patchers)
                            call = child.value
                            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "stop":
                                active = active - patchers.get(dotted(call.func.value), frozenset())
            elif isinstance(value, ast.AST):
                visit(value, mocked, scope)

    visit(tree)
    return issues


class Collection:
    def __init__(self, root=ROOT):
        self.root = root
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
                relative = Path(item.path).resolve().relative_to(self.root).as_posix()
                qualified = "::".join([*item.parent.nodeid.split("::")[1:], key[-1]])
                identity = f"parametrize:{relative}::{qualified}::{product}"
                self.issues.append(Finding(identity,
                    f"{item.path}:{item.location[1] + 1}: {qualified}: "
                    f"stacked parametrize generates {product} permutations "
                    f"(limit {MAX_PERMUTATIONS})"))


def load_baseline(path: Path) -> Counter:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("expected baseline version 1")
    entries = data.get("violations")
    if not isinstance(entries, dict) or any(
        not isinstance(key, str) or not key.startswith(("sleep:", "parametrize:"))
        or type(count) is not int or count < 1 for key, count in entries.items()
    ):
        raise ValueError("expected violation keys with positive integer counts")
    return Counter(entries)


def new_findings(findings: list[Finding], baseline: Counter) -> list[Finding]:
    remaining = baseline.copy()
    new = []
    for finding in findings:
        if remaining[finding.key] > 0:
            remaining[finding.key] -= 1
        else:
            new.append(finding)
    return new


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--budget", type=int, default=TEST_BUDGET)
    parser.add_argument("--baseline", type=Path, help="use an explicit baseline JSON file")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--strict", action="store_true", help="ignore legacy baseline")
    modes.add_argument("--dump-baseline", action="store_true", help="print proposed baseline JSON only")
    args = parser.parse_args(argv)
    if args.budget < 1:
        parser.error("--budget must be positive")
    root = args.root.resolve()
    tests = root / "tests"
    baseline_path = args.baseline or root / "scripts" / "test_quality_baseline.json"
    baseline = Counter()
    if not args.strict and not args.dump_baseline and (args.baseline or baseline_path.exists()):
        try:
            baseline = load_baseline(baseline_path)
        except (OSError, ValueError) as error:
            print(f"Test quality: invalid baseline {baseline_path}: {error}")
            return 1
    collection = Collection(root)
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
        issues.extend(sleep_issues(path, root))
    if args.dump_baseline:
        print(json.dumps({"version": 1, "violations": dict(sorted(Counter(i.key for i in issues).items()))},
                         indent=2))
        return 0
    new = new_findings(issues, baseline)
    failures = [i.message for i in new]
    if collection.count > args.budget:
        failures.append(f"total test count {collection.count} exceeds budget {args.budget}")
    for issue in failures:
        print(issue)
    print(f"Test quality: {collection.count}/{args.budget} tests; {len(failures)} new issue(s); "
          f"{len(issues) - len(new)} grandfathered")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
