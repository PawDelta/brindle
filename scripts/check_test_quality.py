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
TEST_BUDGET = 4164


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
        if isinstance(node.func, ast.Attribute) and node.func.attr == "start":
            return patch_target(node.func.value)
        target = dotted(node.func)
        args = node.args
        if target.endswith("patch") and args:
            if isinstance(args[0], ast.Constant) and args[0].value == "time.sleep":
                return frozenset({"time.sleep"})
        if target.endswith("patch.object") and len(args) >= 2:
            if name(args[0]) == "time" and isinstance(args[1], ast.Constant) and args[1].value == "sleep":
                return frozenset({"time.sleep"})
        return frozenset()

    issues = []

    relative = path.relative_to(root).as_posix() if root else path.name

    class State:
        def __init__(self, patchers=None, started=None, unnamed_started=None, mp_exemptions=None):
            self.patchers = dict(patchers) if patchers else {}
            self.started = set(started) if started else set()
            self.unnamed_started = set(unnamed_started) if unnamed_started else set()
            self.mp_exemptions = {k: set(v) for k, v in mp_exemptions.items()} if mp_exemptions else {}

        def clone(self):
            return State(self.patchers, self.started, self.unnamed_started, self.mp_exemptions)

        def join(self, other: State) -> State:
            patchers = {
                k: v for k, v in self.patchers.items()
                if k in other.patchers and other.patchers[k] == v
            }
            started = self.started & other.started
            unnamed_started = self.unnamed_started & other.unnamed_started
            mp_exemptions = {}
            for mp in set(self.mp_exemptions.keys()) & set(other.mp_exemptions.keys()):
                common = self.mp_exemptions[mp] & other.mp_exemptions[mp]
                if common:
                    mp_exemptions[mp] = set(common)
            return State(patchers, started, unnamed_started, mp_exemptions)

        def apply(self, other: State):
            self.patchers = dict(other.patchers)
            self.started = set(other.started)
            self.unnamed_started = set(other.unnamed_started)
            self.mp_exemptions = {k: set(v) for k, v in other.mp_exemptions.items()}

        @property
        def active_started(self) -> frozenset[str]:
            active = set(self.unnamed_started)
            for p in self.started:
                active.update(self.patchers.get(p, ()))
            return frozenset(active)

        @property
        def active_mp(self) -> frozenset[str]:
            if not self.mp_exemptions:
                return frozenset()
            return frozenset().union(*self.mp_exemptions.values())

        @property
        def active_mocks(self) -> frozenset[str]:
            return self.active_started | self.active_mp

    def setattr_target(call):
        if not isinstance(call, ast.Call):
            return frozenset()
        if dotted(call.func).endswith(".setattr") and len(call.args) >= 2:
            if name(call.args[0]) == "time" and isinstance(call.args[1], ast.Constant) and call.args[1].value == "sleep":
                return frozenset({"time.sleep"})
            if isinstance(call.args[0], ast.Constant) and call.args[0].value == "time.sleep":
                return frozenset({"time.sleep"})
        return frozenset()

    def evaluate_expr_mutations(expr, state):
        if expr is None:
            return
        if isinstance(expr, ast.Lambda):
            # Lambdas are deferred expressions; their bodies are NOT evaluated at definition.
            return
        if isinstance(expr, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(expr, ast.IfExp):
            evaluate_expr_mutations(expr.test, state)
            s_body = state.clone()
            evaluate_expr_mutations(expr.body, s_body)
            s_else = state.clone()
            evaluate_expr_mutations(expr.orelse, s_else)
            state.apply(s_body.join(s_else))
            return
        if isinstance(expr, ast.BoolOp):
            if not expr.values:
                return
            evaluate_expr_mutations(expr.values[0], state)
            current_path = state.clone()
            joined = state.clone()
            for val in expr.values[1:]:
                s_val = current_path.clone()
                evaluate_expr_mutations(val, s_val)
                joined = joined.join(s_val)
                current_path = s_val
            state.apply(joined)
            return
        if isinstance(expr, ast.Call):
            for arg in expr.args:
                evaluate_expr_mutations(arg, state)
            for kw in expr.keywords:
                evaluate_expr_mutations(kw.value, state)
            call_name = dotted(expr.func)
            if call_name.endswith(".stopall"):
                state.started.clear()
                state.unnamed_started.clear()
            elif isinstance(expr.func, ast.Attribute):
                attr = expr.func.attr
                if attr == "start":
                    receiver = dotted(expr.func.value)
                    if receiver in state.patchers:
                        state.started.add(receiver)
                    else:
                        tgt = patch_target(expr.func.value)
                        if tgt:
                            state.unnamed_started.update(tgt)
                elif attr == "stop":
                    receiver = dotted(expr.func.value)
                    state.started.discard(receiver)
                elif attr == "setattr" and len(expr.args) >= 2:
                    tgt = setattr_target(expr)
                    if tgt:
                        mp_name = dotted(expr.func.value)
                        state.mp_exemptions.setdefault(mp_name, set()).update(tgt)
                elif attr == "undo":
                    mp_name = dotted(expr.func.value)
                    state.mp_exemptions.pop(mp_name, None)
            return
        for _, value in ast.iter_fields(expr):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        evaluate_expr_mutations(item, state)
            elif isinstance(value, ast.AST):
                evaluate_expr_mutations(value, state)

    def update_mutations(stmt, state):
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            rhs = stmt.value
            if rhs is None:
                return
            targets = patch_target(rhs)
            bindings = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            assigned_start = False
            if isinstance(rhs, ast.Call) and isinstance(rhs.func, ast.Attribute) and rhs.func.attr == "start":
                assigned_start = True
                receiver = dotted(rhs.func.value)
                for b in bindings:
                    if isinstance(b, ast.Name):
                        if targets:
                            state.patchers[b.id] = targets
                        if receiver in state.patchers:
                            state.started.add(receiver)
                        else:
                            state.started.add(b.id)
            else:
                for b in bindings:
                    if isinstance(b, ast.Name) and targets:
                        state.patchers[b.id] = targets
            if not assigned_start:
                evaluate_expr_mutations(rhs, state)
            return

        if isinstance(stmt, ast.Expr):
            evaluate_expr_mutations(stmt.value, state)
            return

        for _, value in ast.iter_fields(stmt):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        evaluate_expr_mutations(item, state)
            elif isinstance(value, ast.AST):
                evaluate_expr_mutations(value, state)

    def check_calls(node, lexical_mocks, scope, state):
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                binding = name(child.func)
                if binding == "time.sleep" or binding.startswith("imported:"):
                    active = lexical_mocks | state.active_mocks
                    if binding not in active:
                        key = f"sleep:{relative}::{'.'.join(scope)}::{ast.unparse(child)}"
                        issues.append(Finding(key, f"{path}:{child.lineno}: unmocked time.sleep"))

    def visit_stmt(stmt, lexical_mocks, scope, state):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_scope = (*scope, stmt.name)
            fn_lexical = lexical_mocks | frozenset().union(*(patch_target(d) for d in stmt.decorator_list))
            fn_state = state.clone()
            visit_statements(stmt.body, fn_lexical, fn_scope, fn_state)
            return

        if isinstance(stmt, ast.ClassDef):
            cls_scope = (*scope, stmt.name)
            cls_lexical = lexical_mocks | frozenset().union(*(patch_target(d) for d in stmt.decorator_list))
            cls_state = state.clone()
            visit_statements(stmt.body, cls_lexical, cls_scope, cls_state)
            return

        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            context_mp_names = set()
            for item in stmt.items:
                check_calls(item.context_expr, lexical_mocks, scope, state)
                if isinstance(item.context_expr, ast.Call) and dotted(item.context_expr.func).endswith(".context"):
                    if item.optional_vars:
                        for v in ast.walk(item.optional_vars):
                            if isinstance(v, ast.Name):
                                context_mp_names.add(v.id)
            with_lexical = lexical_mocks | frozenset().union(*(
                patch_target(i.context_expr) or state.patchers.get(dotted(i.context_expr), frozenset())
                for i in stmt.items
            ))
            visit_statements(stmt.body, with_lexical, scope, state)
            for name in context_mp_names:
                state.mp_exemptions.pop(name, None)
            return

        if isinstance(stmt, ast.Try):
            state_try = state.clone()
            visit_statements(stmt.body, lexical_mocks, scope, state_try)

            handler_states = []
            if stmt.handlers:
                state_handler_entry = state.join(state_try)
                for handler in stmt.handlers:
                    if handler.type:
                        check_calls(handler.type, lexical_mocks, scope, state_handler_entry)
                    h_state = state_handler_entry.clone()
                    visit_statements(handler.body, lexical_mocks, scope, h_state)
                    handler_states.append(h_state)

            if stmt.orelse:
                state_else = state_try.clone()
                visit_statements(stmt.orelse, lexical_mocks, scope, state_else)
                normal_state = state_else
            else:
                normal_state = state_try

            if handler_states:
                joined = normal_state
                for h_state in handler_states:
                    joined = joined.join(h_state)
            else:
                joined = normal_state

            if stmt.finalbody:
                visit_statements(stmt.finalbody, lexical_mocks, scope, joined)

            state.apply(joined)
            return

        if isinstance(stmt, ast.If):
            check_calls(stmt.test, lexical_mocks, scope, state)
            state_body = state.clone()
            visit_statements(stmt.body, lexical_mocks, scope, state_body)
            if stmt.orelse:
                state_else = state.clone()
                visit_statements(stmt.orelse, lexical_mocks, scope, state_else)
            else:
                state_else = state.clone()
            state.apply(state_body.join(state_else))
            return

        if isinstance(stmt, ast.While):
            check_calls(stmt.test, lexical_mocks, scope, state)
            state_body = state.clone()
            visit_statements(stmt.body, lexical_mocks, scope, state_body)
            if stmt.orelse:
                state_else = state.clone()
                visit_statements(stmt.orelse, lexical_mocks, scope, state_else)
                state_zero = state_else
            else:
                state_zero = state.clone()
            state.apply(state_zero.join(state_body))
            return

        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            check_calls(stmt.iter, lexical_mocks, scope, state)
            state_body = state.clone()
            visit_statements(stmt.body, lexical_mocks, scope, state_body)
            if stmt.orelse:
                state_else = state.clone()
                visit_statements(stmt.orelse, lexical_mocks, scope, state_else)
                state_zero = state_else
            else:
                state_zero = state.clone()
            state.apply(state_zero.join(state_body))
            return

        check_calls(stmt, lexical_mocks, scope, state)
        update_mutations(stmt, state)

    def visit_statements(stmts, lexical_mocks, scope, state):
        for stmt in stmts:
            visit_stmt(stmt, lexical_mocks, scope, state)

    visit_statements(tree.body, frozenset(), (), State())
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
