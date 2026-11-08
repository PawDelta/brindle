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
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            found[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            found[node.target.id] = node.value
    return found


def _table(arg: ast.expr, consts: dict[str, ast.expr]) -> "list[str] | str":
    value = consts.get(arg.id, arg) if isinstance(arg, ast.Name) else arg
    if isinstance(value, (ast.List, ast.Tuple)):
        return [ast.unparse(e) for e in value.elts]
    return ast.unparse(arg)


def test_tables(source: str) -> dict[str, list]:
    """Test function qualnames in a module (``test_x``, ``TestY::test_x``), each
    with its parametrize tables, one per decorator."""
    tree = ast.parse(source)
    consts = _consts(tree)
    found: dict[str, list] = {}

    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                found[prefix + node.name] = [
                    _table(d.args[1], consts) for d in node.decorator_list
                    if isinstance(d, ast.Call) and ast.unparse(d.func).endswith("mark.parametrize")
                    and len(d.args) > 1]
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                visit(node.body, f"{prefix}{node.name}::")

    visit(tree.body, "")
    return found


def added_positions(old: "list[str] | None", new: list[str]) -> list[int]:
    """Indices in ``new`` of the rows that weren't in ``old``: an inserted or
    replaced row counts even when an equal row exists elsewhere in the table."""
    matcher = difflib.SequenceMatcher(None, old or [], new, autojunk=False)
    return [j for tag, _, _, j1, j2 in matcher.get_opcodes() if tag in ("insert", "replace")
            for j in range(j1, j2)]


def _added_rows(path: str, name: str, old: list | None, new: list) -> list[str]:
    lines = []
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
            head = test_tables((root / path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        try:
            before = test_tables(git.out(["show", f"{base}:{path}"], root))
        except (git.GitError, SyntaxError):
            before = {}
        for name, tables in head.items():
            if name not in before:
                lines.append(f"{path}::{name}")
            lines += _added_rows(path, name, before.get(name), tables)
    return lines


def review_section(root: str | Path, base: str) -> str:
    """The reviewer's brief for the added tests, or "" when the branch adds none."""
    try:
        lines = added_tests(root, base)
    except git.GitError:
        return ""
    if not lines:
        return ""
    return ("\n\nTests this branch adds (each test function and each new parametrize row):\n"
            + "\n".join(f"- {line}" for line in lines)
            + "\n\nFor every item above, say in your summary what bug it would catch that no other "
              "test catches. A test or row with no answer (it repeats another, checks wording, a "
              "constant or a mock's own arguments) is a `blocker`; one marked "
              "`@pytest.mark.variant(\"why\")` needs its reason to be a real input variant.")
