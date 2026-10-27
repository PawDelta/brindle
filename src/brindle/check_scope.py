"""Scoped checks: a worker's branch runs only the part of a check it can reach.

A ``checks`` entry in ``.brindle/config.json`` is a command, or an object
that also says what the command covers::

    {"run": "uv run pytest -q", "paths": ["src/**", "tests/**"],
     "affected": "pytest", "full_when": ["**/conftest.py", "pyproject.toml"]}

On a worker's branch (never the supervisor's checkout or a session branch,
which always run every check in full) the files the branch changed since it
left its base decide what runs:

- none of them match ``paths``: the check is skipped (it passes);
- one matches ``full_when`` (or is under ``.brindle/``), or the scope can't be
  worked out: ``run`` in full;
- ``affected: "pytest"``: ``run`` on the test files the branch touched plus
  the tests ``graphify affected`` reaches from each changed symbol. A changed
  file the graph can't map to a test runs the full suite instead;
- ``affected: "files"``: ``scoped``, a command with ``{files}`` standing for
  the changed files in ``paths`` (relative to ``relative_to``, when given);
- no ``affected``: ``run`` in full whenever anything in ``paths`` changed.

Patterns are ``fnmatch`` globs on repo-relative paths, where ``*`` also
crosses ``/`` and a leading ``**/`` may match nothing.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from brindle import git

if TYPE_CHECKING:
    from brindle.db import DB, Workspace

ALWAYS_FULL = (".brindle/**",)   # the checks themselves (or the rules) changed
MODES = ("pytest", "files")
MAX_SYMBOLS = 30          # more changed symbols than this: run everything
GRAPHIFY_SECONDS = 60
SKIPPED = "skipped: "     # run_checked's output for a skipped check starts with this
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
_AFFECTED_FILE = re.compile(r"\s(\S+\.py):L\d+\s*$", re.M)


@dataclass
class CheckScope:
    paths: list[str]
    affected: str | None = None
    scoped: str | None = None
    relative_to: str = ""
    full_when: list[str] = field(default_factory=list)


def parse_checks(entries: list) -> tuple[list[str], dict[str, CheckScope]]:
    """``checks`` entries as (commands, {command: scope}); an object without
    a usable ``run`` is dropped, one without ``paths`` is a plain command."""
    commands: list[str] = []
    scopes: dict[str, CheckScope] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            commands.append(str(entry))
            continue
        cmd = entry.get("run")
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        commands.append(cmd)
        paths = _strings(entry.get("paths"))
        affected = entry.get("affected") if entry.get("affected") in MODES else None
        scoped = entry.get("scoped") if isinstance(entry.get("scoped"), str) else None
        if not paths or (affected == "files" and not scoped):
            continue
        scopes[cmd] = CheckScope(paths, affected, scoped, str(entry.get("relative_to") or "").strip("/"),
                                 _strings(entry.get("full_when")))
    return commands, scopes


def _strings(value: object) -> list[str]:
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def matches(path: str, patterns) -> bool:
    return any(fnmatch(path, p) or (p.startswith("**/") and fnmatch(path, p[3:])) for p in patterns)


def is_worker_branch(db: DB, ws: Workspace) -> bool:
    from brindle import agents

    return ws.kind == "worktree" and bool(ws.base_branch) and agents.workspace_worker(db, ws) is not None


def command(db: DB, ws: Workspace, cmd: str, scope: CheckScope | None) -> str | None:
    """What to run for check ``cmd`` in ``ws``: ``cmd`` itself, a narrower
    command, or None when the branch changes nothing the check covers."""
    if scope is None or not is_worker_branch(db, ws):
        return cmd
    try:
        base = git.out(["merge-base", ws.base_branch, "HEAD"], ws.path)
        changed = git.out(["diff", "--name-only", "--no-renames", base], ws.path).splitlines()
    except git.GitError:
        return cmd
    if any(matches(f, ALWAYS_FULL) or matches(f, scope.full_when) for f in changed):
        return cmd
    relevant = [f for f in changed if matches(f, scope.paths)]
    if not relevant:
        return None
    if scope.affected == "pytest":
        tests = pytest_targets(ws, base, relevant)
        return f"{cmd} {_quoted(tests)} || [ $? -eq 5 ]" if tests else cmd   # 5: none of them selected
    if scope.affected == "files":
        present = [f for f in relevant if (Path(ws.path) / f).is_file()]
        if not present:
            return cmd
        rel = [str(PurePosixPath(f).relative_to(scope.relative_to)) if scope.relative_to else f for f in present]
        return scope.scoped.replace("{files}", _quoted(rel))
    return cmd


def _quoted(files: list[str]) -> str:
    return " ".join(shlex.quote(f) for f in files)


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def pytest_targets(ws: Workspace, base: str, files: list[str]) -> list[str] | None:
    """The test files to run for a branch that changed ``files``: the test
    files among them that still exist, and those the code graph reaches from
    every changed symbol; None when any changed file can't be mapped."""
    from brindle import codemap

    tests: set[str] = set()
    sources = []
    for f in files:
        if not f.endswith(".py"):
            return None
        if is_test_file(f):
            tests.update([f] if (Path(ws.path) / f).is_file() else [])
        else:
            sources.append(f)
    if sources:
        graph = codemap.graph_path(ws)
        if graph is None:
            return None
        nodes = _nodes_by_file(graph)
        ids: set[str] = set()
        for f in sources:
            found = _changed_nodes(ws, base, f, nodes.get(f, []))
            if not found:
                return None
            ids |= found
        if len(ids) > MAX_SYMBOLS:
            return None
        for node_id in sorted(ids):
            reached = affected_tests(graph, node_id, ws.path)
            if not reached:
                return None
            tests |= reached
    return sorted(t for t in tests if (Path(ws.path) / t).is_file()) or None


def _nodes_by_file(graph: Path) -> dict[str, list[dict]]:
    try:
        data = json.loads(graph.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, list[dict]] = {}
    for n in data.get("nodes") or []:
        if isinstance(n, dict) and n.get("source_file") and n.get("id"):
            out.setdefault(n["source_file"], []).append(n)
    return out


def _changed_nodes(ws: Workspace, base: str, path: str, nodes: list[dict]) -> set[str] | None:
    """The graph nodes of the symbols ``path`` changes (its module node for a
    change outside any function or class); None when one has no node."""
    file = Path(ws.path) / path
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"))
        diff = git.out(["diff", "-U0", "--no-renames", base, "--", path], ws.path)
    except (OSError, SyntaxError, ValueError, git.GitError):
        return None
    defs = [(min([d.lineno for d in n.decorator_list] + [n.lineno]), n.end_lineno or n.lineno, n.name)
            for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    labels: dict[str, list[str]] = {}
    for n in nodes:
        labels.setdefault(str(n.get("label")), []).append(n["id"])
    ids: set[str] = set()
    for line in _changed_lines(diff):
        # Innermost first; the first enclosing symbol the graph knows stands for the change.
        chain = sorted((d for d in defs if d[0] <= line <= d[1]), key=lambda d: d[0], reverse=True)
        names = [[f"{name}()", f".{name}()", name] for _, _, name in chain] or [[file.name]]
        hit = next((labels[label] for options in names for label in options if label in labels), None)
        if hit is None:
            return None
        ids.update(hit)
    return ids


def _changed_lines(diff: str) -> list[int]:
    lines = []
    for m in _HUNK.finditer(diff):
        start, count = int(m.group(1)), int(m.group(2) or 1)
        lines.extend(range(start, start + count) if count else [max(start, 1)])
    return lines


def affected_tests(graph: Path, node_id: str, cwd: str) -> set[str]:
    """The test files ``graphify affected`` lists for ``node_id``."""
    try:
        proc = subprocess.run(["graphify", "affected", node_id, "--graph", str(graph)], cwd=cwd,
                              capture_output=True, text=True, timeout=GRAPHIFY_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:
        return set()
    return {f for f in _AFFECTED_FILE.findall(proc.stdout) if is_test_file(f)}
