"""The tests (and parametrize rows) a branch adds, listed for its reviewer.

Coverage can say a new test repeats another, but not whether it is worth
keeping, so the reviewer is asked what bug each one would catch. This is a
static listing: test functions new since the base, and rows added to a
``pytest.mark.parametrize`` table, found by comparing the two versions' syntax.
A table is a list of row sources, or, when it is not a literal (or a module-level
name for one), the source of the expression that builds it.
"""
from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path

from brindle import git

ROW_WIDTH = 100


def _consts(tree: ast.Module) -> dict[str, ast.expr]:
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            # every name a chained or unpacking assignment binds (A = B = 1; A, B = f())
            for target in node.targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        found[name.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            found[node.target.id] = node.value
    return found


def _table(arg: ast.expr, consts: dict[str, ast.expr]) -> "list[str] | str":
    value = consts.get(arg.id, arg) if isinstance(arg, ast.Name) else arg
    if isinstance(value, (ast.List, ast.Tuple)):
        return [ast.unparse(e) for e in value.elts]
    return f"{arg.id} = {ast.unparse(value)}" if isinstance(arg, ast.Name) and value is not arg else ast.unparse(arg)


def _statements(tree: ast.Module) -> dict[str, str]:
    """Each module-level name mapped to the source of the whole statement that binds it."""
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value:   # `A: int` alone binds nothing
            targets = [node.target]
        else:
            continue
        for target in targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name):
                    found[name.id] = ast.unparse(node)
    return found


def _inputs(args: list[ast.expr], consts: dict[str, ast.expr], statements: dict[str, str]) -> str:
    """The module-level assignments the tables use, directly or through other
    assignments by name, as source lines: when one changes, the rows may too."""
    seen: set[str] = set()
    todo = list(args)
    while todo:
        for node in ast.walk(todo.pop()):
            if isinstance(node, ast.Name) and node.id in consts and node.id not in seen:
                seen.add(node.id)
                todo.append(consts[node.id])
    # the whole statement, so `A, B = 1, 2` and `B, A = 1, 2` differ
    return "\n".join(sorted({statements.get(name) or f"{name} = {ast.unparse(consts[name])}" for name in seen}))


def scan(source: str) -> dict[str, tuple[list, str]]:
    """Test function qualnames in a module (``test_x``, ``TestY::test_x``), each
    with its parametrize tables (one per decorator) and the assignments they use."""
    tree = ast.parse(source)
    consts = _consts(tree)
    statements = _statements(tree)
    found: dict[str, tuple[list, str]] = {}

    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                args = [d.args[1] for d in node.decorator_list
                        if isinstance(d, ast.Call) and ast.unparse(d.func).endswith("mark.parametrize")
                        and len(d.args) > 1]
                found[prefix + node.name] = ([_table(a, consts) for a in args], _inputs(args, consts, statements))
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                visit(node.body, f"{prefix}{node.name}::")

    visit(tree.body, "")
    return found


def test_tables(source: str) -> dict[str, list]:
    return {name: tables for name, (tables, _) in scan(source).items()}


def added_positions(old: "list[str] | None", new: list[str]) -> list[int]:
    """Indices in ``new`` of the rows that weren't in ``old``: an inserted or
    replaced row counts even when an equal row exists elsewhere in the table."""
    matcher = difflib.SequenceMatcher(None, old or [], new, autojunk=False)
    return [j for tag, _, _, j1, j2 in matcher.get_opcodes() if tag in ("insert", "replace")
            for j in range(j1, j2)]


def _added_rows(path: str, name: str, old: list | None, new: list, inputs_changed: bool = False) -> list[str]:
    lines = []
    if inputs_changed:
        lines.append(f"{path}::{name} parametrize inputs changed (an assignment its tables use): "
                     "review every row it produces")
    for i, table in enumerate(new):
        before = old[i] if old is not None and i < len(old) else None
        if isinstance(table, str):
            if before != table:
                lines.append(f"{path}::{name} parametrize table `{table[:ROW_WIDTH]}` is not a literal: "
                             "review every row it produces")
            continue
        base_rows = before if isinstance(before, list) else None
        lines += [f"{path}::{name} new row: {table[j][:ROW_WIDTH]}" for j in added_positions(base_rows, table)]
    return lines


def added_tests(root: str | Path, base: str) -> list[str]:
    """One line per test function (and per row of a new function's tables) or
    parametrize row added since ``base``."""
    root = Path(root)
    names = git.out(["diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD", "--", "tests"], root).split()
    lines: list[str] = []
    for path in names:
        if not re.search(r"(^|/)test_[^/]*\.py$", path):
            continue
        try:
            head = scan((root / path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        try:
            before = scan(git.out(["show", f"{base}:{path}"], root))
        except (git.GitError, SyntaxError):
            before = {}
        for name, (tables, inputs) in head.items():
            if name not in before:
                lines.append(f"{path}::{name}")
            old_tables, old_inputs = before.get(name, (None, inputs))
            lines += _added_rows(path, name, old_tables, tables, old_inputs != inputs)
    return lines


def review_section(root: str | Path, base: str) -> str:
    """Shared test guidance and the branch's added tests for its reviewer."""
    from brindle.agents import TEST_GUIDANCE

    brief = ("\n\n" + TEST_GUIDANCE
             + "\nA missing feature test or regression test is a blocker. An added test or row "
               "that catches no distinct bug is also a blocker.")
    try:
        lines = added_tests(root, base)
    except git.GitError:
        return brief
    if not lines:
        return brief
    return (brief + "\n\nTests this branch adds (each test function and each new parametrize row):\n"
            + "\n".join(f"- {line}" for line in lines)
            + "\n\nFor every item above, say in your summary what bug it would catch that no other "
              "test catches. A test or row with no answer is a blocker; one marked "
              "`@pytest.mark.variant(\"why\")` needs its reason to be a real input variant.")
