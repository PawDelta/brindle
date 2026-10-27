"""Scoped checks: a worker's branch runs only the part of a check it can reach.

``checks`` in ``.brindle/config.json`` is a list of command strings. What a
command covers goes in the optional ``check_scopes`` key, by the exact command::

    "check_scopes": {"uv run pytest -q": {"paths": ["src/**", "tests/**"],
     "affected": "pytest", "full_when": ["**/conftest.py", "pyproject.toml"]}}

(A ``checks`` entry may also be an object, ``{"run": ..., "paths": ...}``,
with the same fields; older engines can't run that form.) A command with no
scope runs in full.

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
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)
_AFFECTED_FILE = re.compile(r"\s(\S+\.py):L\d+\s*$", re.M)


@dataclass
class CheckScope:
    paths: list[str]
    affected: str | None = None
    scoped: str | None = None
    relative_to: str = ""
    full_when: list[str] = field(default_factory=list)


def parse_checks(entries: list, check_scopes: object = None) -> tuple[list[str], dict[str, CheckScope]]:
    """``checks`` entries as (commands, {command: scope}). ``checks`` stays a
    list of command strings (older engines run it as is) and the scopes live
    in ``check_scopes``, keyed by the exact command; an object entry in
    ``checks`` (``run`` plus the scope fields) is still accepted and wins over
    ``check_scopes`` for its command. An object without a usable ``run`` is
    dropped; a scope without ``paths`` leaves the command plain."""
    commands: list[str] = []
    scopes: dict[str, CheckScope] = {}
    if isinstance(check_scopes, dict):
        for cmd, spec in check_scopes.items():
            if isinstance(spec, dict) and (scope := _scope(spec)):
                scopes[cmd] = scope
    for entry in entries:
        if not isinstance(entry, dict):
            commands.append(str(entry))
            continue
        cmd = entry.get("run")
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        commands.append(cmd)
        if scope := _scope(entry):
            scopes[cmd] = scope
    return commands, {cmd: s for cmd, s in scopes.items() if cmd in commands}


def _scope(spec: dict) -> CheckScope | None:
    paths = _strings(spec.get("paths"))
    affected = spec.get("affected") if spec.get("affected") in MODES else None
    scoped = spec.get("scoped") if isinstance(spec.get("scoped"), str) else None
    if not paths or (affected == "files" and not scoped):
        return None
    return CheckScope(paths, affected, scoped, str(spec.get("relative_to") or "").strip("/"),
                      _strings(spec.get("full_when")))


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
        changed = git.paths(["diff", "--name-only", "--no-renames", base], ws.path)
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
    files among them, and those the code graph reaches from every changed
    symbol; None when any changed file can't be mapped.

    A change inside a test function is covered by running its own file. Any
    other change to a test file (a helper, a fixture, an import) may reach the
    modules that import from it, so it is mapped like a source change, plus
    the test file's importers; reaching no other test is fine there."""
    from brindle import codemap

    if any(not f.endswith(".py") for f in files):
        return None
    graph = codemap.graph_path(ws)
    if graph is None:
        return None
    nodes = _nodes_by_file(graph)
    tests: set[str] = set()
    must_reach: set[str] = set()    # source symbols: one that reaches no test is unresolved
    optional: set[str] = set()
    for f in files:
        test = is_test_file(f)
        found = _changed_nodes(ws, base, f, nodes.get(f, []), skip_tests=test)
        if found is None or (not test and not found):
            return None
        if not test:
            must_reach |= found
            continue
        tests.add(f)
        if found:
            module = [n["id"] for n in nodes.get(f, []) if n.get("label") == PurePosixPath(f).name]
            if not module:
                return None
            optional |= found | set(module)
    if len(must_reach | optional) > MAX_SYMBOLS:
        return None
    for node_id in sorted(must_reach | optional):
        reached = affected_tests(graph, node_id, ws.path)
        if reached is None or (not reached and node_id in must_reach):
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


def _changed_nodes(ws: Workspace, base: str, path: str, nodes: list[dict],
                   skip_tests: bool = False) -> set[str] | None:
    """The graph nodes of the symbols ``path`` changes (its module node for a
    change outside any function or class): added and changed lines found in
    the file as it is now, removed ones in the file as it was at ``base``.
    None when one has no node, or the file is gone or can't be parsed.
    ``skip_tests``: leave out changes inside test functions and classes."""
    try:
        diff = git.out(["diff", "-U0", "--no-renames", base, "--", path], ws.path)
        old_lines, new_lines = _changed_lines(diff)
        new_text = (Path(ws.path) / path).read_text(encoding="utf-8")
        old_text = git.run(["show", f"{base}:{path}"], ws.path).stdout if old_lines else ""
        new, old = _defs(new_text), _defs(old_text)
    except (OSError, SyntaxError, ValueError, git.GitError):
        return None
    labels: dict[str, list[str]] = {}
    for n in nodes:
        labels.setdefault(str(n.get("label")), []).append(n["id"])
    ids: set[str] = set()
    for defs, text, lines in ((new, new_text, new_lines), (old, old_text, old_lines)):
        source = text.splitlines()
        for line in lines:
            if line > len(source) or not source[line - 1].strip():
                continue   # a blank line changes nothing

            # Innermost first; the first enclosing symbol the graph knows stands for the change.
            chain = [name for start, end, name in sorted(defs, key=lambda d: d[0], reverse=True)
                     if start <= line <= end]
            if skip_tests and any(name.startswith(("test", "Test")) for name in chain):
                continue
            names = [[f"{name}()", f".{name}()", name] for name in chain] or [[PurePosixPath(path).name]]
            hit = next((labels[label] for options in names for label in options if label in labels), None)
            if hit is None:
                return None
            ids.update(hit)
    return ids


def _defs(source: str) -> list[tuple[int, int, str]]:
    """(first line, last line, name) of each function and class in ``source``."""
    return [(min([d.lineno for d in n.decorator_list] + [n.lineno]), n.end_lineno or n.lineno, n.name)
            for n in ast.walk(ast.parse(source))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def _changed_lines(diff: str) -> tuple[list[int], list[int]]:
    """The removed lines (numbered as at the base) and the added ones (as now)."""
    old, new = [], []
    for m in _HUNK.finditer(diff):
        o_start, o_count, n_start, n_count = (int(g) if g else 1 for g in m.groups())
        old.extend(range(o_start, o_start + o_count) if o_count else [])
        new.extend(range(n_start, n_start + n_count) if n_count else [])
    return old, new


def affected_tests(graph: Path, node_id: str, cwd: str) -> set[str] | None:
    """The test files ``graphify affected`` lists for ``node_id``; None when
    the lookup itself failed (not the same as reaching no test)."""
    try:
        proc = subprocess.run(["graphify", "affected", node_id, "--graph", str(graph)], cwd=cwd,
                              capture_output=True, text=True, timeout=GRAPHIFY_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return {f for f in _AFFECTED_FILE.findall(proc.stdout) if is_test_file(f)}
