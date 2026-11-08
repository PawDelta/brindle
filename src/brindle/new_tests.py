"""The tests (and parametrize rows) a branch adds, listed for its reviewer.

Coverage can say a new test repeats another, but not whether it is worth
keeping, so the reviewer is asked what bug each one would catch. This is a
static listing: test functions new since the base, and rows added to a
``pytest.mark.parametrize`` table, found by comparing the two versions' syntax.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from brindle import git

ROW_WIDTH = 100


def _rows(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    rows = []
    for deco in fn.decorator_list:
        if (isinstance(deco, ast.Call) and ast.unparse(deco.func).endswith("mark.parametrize")
                and len(deco.args) > 1 and isinstance(deco.args[1], (ast.List, ast.Tuple))):
            rows += [ast.unparse(row) for row in deco.args[1].elts]
    return rows


def test_rows(source: str) -> dict[str, list[str]]:
    """Test function qualnames in a module (``test_x``, ``TestY::test_x``), each
    with the source of its parametrize rows."""
    found: dict[str, list[str]] = {}

    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                found[prefix + node.name] = _rows(node)
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                visit(node.body, f"{prefix}{node.name}::")

    visit(ast.parse(source).body, "")
    return found


def added_tests(root: str | Path, base: str) -> list[str]:
    """One line per test function or parametrize row added since ``base``."""
    root = Path(root)
    names = git.out(["diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD", "--", "tests"], root).split()
    lines: list[str] = []
    for path in names:
        if not re.search(r"(^|/)test_[^/]*\.py$", path):
            continue
        try:
            head = test_rows((root / path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        try:
            before = test_rows(git.out(["show", f"{base}:{path}"], root))
        except (git.GitError, SyntaxError):
            before = {}
        for name, rows in head.items():
            if name not in before:
                lines.append(f"{path}::{name}" + (f" ({len(rows)} rows)" if rows else ""))
                continue
            lines += [f"{path}::{name} new row: {row[:ROW_WIDTH]}" for row in rows if row not in before[name]]
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
