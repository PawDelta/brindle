"""Scoped checks: with ``scoped_checks: "infer"`` a worker's branch runs only
the tests its changes reach for a plain pytest check.

``checks`` in ``.brindle/config.json`` is a list of command strings, and stays
one. A check that is a bare pytest invocation (``pytest ...``, ``python -m
pytest ...``, ``uv run pytest ...``, options only) is narrowed; a command that
names tests itself, and every other command, runs in full.

On a worker's branch (never the supervisor's checkout or a session branch,
which always run every check in full) the files the branch changed since it
left its base decide what runs:

- none changed: the check is skipped (it passes);
- one matches ``INFER_FULL_WHEN`` (conftest, pytest and project config, a
  lockfile) or is under ``.brindle/``, or the scope can't be worked out: the
  command runs in full;
- otherwise changed files that aren't ``.py`` are ignored (pytest can't be
  affected by them), and if none is left the check is skipped; the command
  runs on the test files the branch touched plus the
  tests ``graphify affected`` reaches from each changed symbol. A changed file
  the graph can't map is resolved through test imports of its module path;
  an unresolved module or import runs the full suite instead.

Every check on a worker's branch also gets ``BRINDLE_CHANGED_FILES`` (see
``changed_files``), so a script can narrow itself.

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
MAX_SYMBOLS = 30          # more changed symbols than this: run everything
GRAPHIFY_SECONDS = 60
SKIPPED = "skipped: "     # run_checked's output for a skipped check starts with this
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)
_AFFECTED_FILE = re.compile(r"\s(\S+\.py):L\d+\s*$", re.M)


@dataclass
class CheckScope:
    full_when: list[str] = field(default_factory=list)


_PYTEST_PREFIX = ("pytest",), ("python", "-m", "pytest"), ("python3", "-m", "pytest"), ("uv", "run", "pytest"), \
    ("uv", "run", "python", "-m", "pytest"), ("uv", "run", "python3", "-m", "pytest")
_VALUE_OPTIONS = {"-n", "-m", "-k", "-p", "-o", "-c", "--maxfail", "--tb", "--durations", "--rootdir", "--ignore",
                  "--deselect", "--dist", "--cov", "--timeout", "--junitxml", "-W", "--basetemp"}
INFER_FULL_WHEN = ["**/conftest.py", "**/pyproject.toml", "**/setup.cfg", "**/pytest.ini", "**/tox.ini",
                   "**/*.lock", "**/*lock.json", "**/requirements*.txt"]
NOTICE = ("brindle now runs only the tests a worker branch's changes reach for a plain pytest check (`{cmd}`), "
          "using the code graph. To always run it in full, set "
          '`"scoped_checks": "off"` in .brindle/config.json (or ~/.brindle/config.json).')


def pytest_selectors(cmd: str) -> bool | None:
    """None when ``cmd`` is not a plain pytest command (no shell composition);
    else whether it names tests itself (a positional argument, not an option
    or an option's value)."""
    if "$" in cmd or "`" in cmd:
        return None
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return None
    if any(set(t) <= set("();<>|&") for t in toks):   # an unquoted shell operator
        return None
    head = next((h for h in _PYTEST_PREFIX if tuple(toks[:len(h)]) == h), None)
    if head is None:
        return None
    selects, value = False, False
    for tok in toks[len(head):]:
        if value:
            value = False
        elif tok.startswith("-"):
            value = tok in _VALUE_OPTIONS
        else:
            selects = True
    return selects


def inferred(cmd: str, mode: str | None) -> CheckScope | None:
    """The scope ``scoped_checks: "infer"`` gives ``cmd``: pytest on the
    affected tests when it is a plain pytest command that picks no tests
    itself (appending targets would not narrow it), else None."""
    if mode in (None, "infer") and pytest_selectors(cmd) is False:
        return CheckScope(INFER_FULL_WHEN)
    return None


def notice_command(cfg) -> str | None:
    """The first pytest check of ``cfg`` when ``scoped_checks`` is unset."""
    if cfg.scoped_checks is not None:
        return None
    return next((c for c in cfg.checks if pytest_selectors(c) is not None), None)


def deliver_notice(db: DB, repo_root: str, to_id: str) -> bool:
    """Tell the person in supervisor chat ``to_id``, once per repo, that
    inferred scoping exists. False when there was nothing to tell. The repo
    is claimed with an exclusive create before sending, so concurrent
    sidebars (and other repos' claims) can't announce it twice or overwrite
    each other."""
    import hashlib
    import os

    from brindle import agents
    from brindle.config import brindle_home, load_repo_config

    try:
        cmd = notice_command(load_repo_config(repo_root))
    except ValueError:
        return False
    if cmd is None:
        return False
    claim = brindle_home() / "scoped-checks-notice" / hashlib.sha256(str(repo_root).encode()).hexdigest()
    claim.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    try:
        agents.send_message(db, to_id, "[brindle: a notice for the person to read; just mention it to them.] "
                            + NOTICE.format(cmd=cmd), sender_id=None)
    except agents.AgentError:
        claim.unlink(missing_ok=True)   # no supervisor right now: try again later
        return False
    return True


def matches(path: str, patterns) -> bool:
    return any(fnmatch(path, p) or (p.startswith("**/") and fnmatch(path, p[3:])) for p in patterns)


def is_worker_branch(db: DB, ws: Workspace) -> bool:
    from brindle import agents

    return ws.kind == "worktree" and bool(ws.base_branch) and agents.workspace_worker(db, ws) is not None


def changed_files(db: DB, ws: Workspace) -> tuple[str, list[str]] | None:
    """(merge base, repo-relative files changed since it) on a worker's
    branch; None anywhere else, or when git can't say."""
    if not is_worker_branch(db, ws):
        return None
    try:
        base = git.out(["merge-base", ws.base_branch, "HEAD"], ws.path)
        return base, git.paths(["diff", "--name-only", "--no-renames", base], ws.path)
    except git.GitError:
        return None


def command(ws: Workspace, cmd: str, scope: CheckScope | None, diff: tuple[str, list[str]] | None) -> str | None:
    """What to run for check ``cmd`` in ``ws`` given its ``changed_files``:
    ``cmd`` itself, a narrower command, or None when nothing changed."""
    if scope is None or diff is None:
        return cmd
    base, changed = diff
    if not changed:
        return None
    if any(matches(f, ALWAYS_FULL) or matches(f, scope.full_when) for f in changed):
        return cmd
    changed = [f for f in changed if f.endswith(".py")]   # pytest can't be affected by the rest
    if not changed:
        return None
    tests = pytest_targets(ws, base, changed)
    if tests is None:
        return cmd
    return f"{cmd} {_quoted(tests)} || [ $? -eq 5 ]" if tests else None   # 5: none of them selected


def _quoted(files: list[str]) -> str:
    return " ".join(shlex.quote(f) for f in files)


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def pytest_targets(ws: Workspace, base: str, files: list[str]) -> list[str] | None:
    """The test files to run for a branch that changed ``files``: the test
    files among them, and those the code graph reaches from every changed
    symbol, or imports of an unmapped source module; None when unresolved.

    A change inside a test function is covered by running its own file. Any
    other change to a test file (a helper, a fixture, an import) may reach the
    modules that import from it, so it is mapped like a source change, plus
    the test file's importers; reaching no other test is fine there."""
    from brindle import codemap

    if any(not f.endswith(".py") for f in files):
        return None
    graph = codemap.graph_path(ws)
    nodes = _nodes_by_file(graph) if graph else {}
    tests: set[str] = set()
    must_reach: set[str] = set()    # source symbols: one that reaches no test is unresolved
    optional: set[str] = set()
    for f in files:
        test = is_test_file(f)
        found = _changed_nodes(ws, base, f, nodes.get(f, []), skip_tests=test)
        if found is None or (not test and not found):
            imported = _importing_tests(Path(ws.path), f)
            if imported is None:
                return None
            tests |= imported
            if test:
                tests.add(f)
            continue
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
    return sorted(t for t in tests if (Path(ws.path) / t).is_file())


def _module_path(root: Path, path: str) -> str | None:
    """Resolve packages, or a standalone pytest test module imported by its filename."""
    file = root / path
    parts = [] if file.stem == "__init__" else [file.stem]
    parent = file.parent
    while parent != root and (parent / "__init__.py").is_file():
        parts.insert(0, parent.name)
        parent = parent.parent
    if ((parent == file.parent and not is_test_file(path))
            or not file.is_file() or not all(p.isidentifier() for p in parts)):
        return None
    return ".".join(parts)


def _importing_tests(root: Path, path: str) -> set[str] | None:
    """Find static test imports by full module path; uncertain imports require full checks."""
    module = _module_path(root, path)
    if module is None:
        return None
    tests: set[str] = set()
    try:
        paths = git.paths(["ls-files", "--cached", "--others", "--exclude-standard", "*.py"], str(root))
        for test in paths:
            if not is_test_file(test) or not (root / test).is_file():
                continue
            tree = ast.parse((root / test).read_text(encoding="utf-8"))
            dynamic = {"__import__"}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "importlib":
                    dynamic.update(n.asname or n.name for n in node.names if n.name == "import_module")
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Name) and node.func.id in dynamic
                    or isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
                ):
                    # fromlist, level and package arguments can change the imported module.
                    # Only a lone absolute name is resolved here.
                    if len(node.args) > 1 or any(kw.arg != "name" for kw in node.keywords):
                        return None
                    name = node.args[0] if node.args else next(
                        (kw.value for kw in node.keywords if kw.arg == "name"), None)
                    if not isinstance(name, ast.Constant) or not isinstance(name.value, str) or name.value.startswith("."):
                        return None
                    imports = [name.value]
                elif isinstance(node, ast.Import):
                    imports = [n.name for n in node.names]
                elif isinstance(node, ast.ImportFrom):
                    prefix = node.module or ""
                    if node.level:
                        owner = _module_path(root, test)
                        if owner is None or node.level >= len(owner.split(".")):
                            return None
                        package = owner.split(".")[:-node.level]
                        prefix = ".".join([*package, prefix]).rstrip(".")
                    imports = [prefix, *(f"{prefix}.{n.name}" for n in node.names)]
                else:
                    continue
                if any(name == module or name.startswith(module + ".") for name in imports):
                    tests.add(test)
    except (OSError, SyntaxError, ValueError, git.GitError):
        return None
    return tests


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
