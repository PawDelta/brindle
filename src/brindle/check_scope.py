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
  the graph can't map, or a symbol it reaches no test from, is resolved
  through the tests that import its module (``Imports``); more tests than
  ``scoped_checks_max_files`` run only the branch's own and namesake tests.
  Only a changed file no import can name (outside any package) runs the full
  suite, after a one-line note saying why.

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

from brindle import git, spawn

if TYPE_CHECKING:
    from brindle.db import DB, Workspace

ALWAYS_FULL = (".brindle/**",)   # the checks themselves (or the rules) changed
CAP_NOTE = ("brindle: this change reaches more than {max} test files (scoped_checks_max_files); ran only the "
            "branch's own and namesake tests ({tests}). The full suite runs on the session branch before its PR.")
FULL_NOTE = "brindle: ran this check in full because {why}."
MAX_SYMBOLS = 30         # more changed symbols than this: use their modules' importing tests instead
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


def command(ws: Workspace, cmd: str, scope: CheckScope | None, diff: tuple[str, list[str]] | None,
            max_files: int = 0) -> str | None:
    """What to run for check ``cmd`` in ``ws`` given its ``changed_files``:
    ``cmd`` itself, a narrower command, or None when nothing changed.
    ``max_files`` (0: no cap): more selected tests than this run only the
    branch's own test files and the changed modules' namesake tests, after a
    one-line note saying so."""
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
    try:
        tests = pytest_targets(ws, base, changed)
    except Unscoped as why:
        return f"printf '%s\\n' {shlex.quote(FULL_NOTE.format(why=why))}; {cmd}"
    if not tests:
        return None
    if max_files > 0 and len(tests) > max_files:
        tests = capped_targets(ws, changed)
        note = shlex.quote(CAP_NOTE.format(max=max_files, tests=" ".join(tests) or "none"))
        echo = f"printf '%s\\n' {note}"
        # Nothing own or namesake to run: only the note, never a bare pytest.
        return f"{echo}; {cmd} {_quoted(tests)} || [ $? -eq 5 ]" if tests else echo
    return f"{cmd} {_quoted(tests)} || [ $? -eq 5 ]"   # 5: none of them selected


def capped_targets(ws: Workspace, changed: list[str]) -> list[str]:
    """The branch's own changed test files plus each changed module's
    namesake tests (``tests/test_<module>.py``, ``tests/test_<module>_*.py``)."""
    root = Path(ws.path)
    tests = {f for f in changed if is_test_file(f)}
    for f in changed:
        if not is_test_file(f):
            stem = PurePosixPath(f).stem
            for pattern in (f"test_{stem}.py", f"test_{stem}_*.py"):
                tests.update(p.relative_to(root).as_posix() for p in (root / "tests").glob(pattern))
    return sorted(t for t in tests if (root / t).is_file())


def _quoted(files: list[str]) -> str:
    return " ".join(shlex.quote(f) for f in files)


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


class Unscoped(Exception):
    """The tests a change reaches can't be worked out; the message says why."""


def pytest_targets(ws: Workspace, base: str, files: list[str]) -> list[str]:
    """The test files to run for a branch that changed ``files``: the test
    files among them, and those the code graph reaches from every changed
    symbol. A file the graph can't map, or a symbol it reaches no test from
    (or whose lookup failed), is resolved through the tests that import its
    module (see ``Imports.tests_reaching``). Raises ``Unscoped`` only when a
    changed file's module can't be named or the tests can't be read.

    A change inside a test function is covered by running its own file. Any
    other change to a test file (a helper, a fixture, an import) may reach the
    modules that import from it, so it is mapped like a source change, plus
    the test file's importers; reaching no other test is fine there."""
    from brindle import codemap

    if any(not f.endswith(".py") for f in files):
        raise Unscoped("a changed file isn't Python")
    graph = codemap.graph_path(ws)
    nodes = _nodes_by_file(graph) if graph else {}
    tests: set[str] = set()
    owners: dict[str, set[str]] = {}   # graph node -> the changed files it stands for
    unmapped: set[str] = set()         # files resolved through test imports
    for f in files:
        test = is_test_file(f)
        if test:
            tests.add(f)
        found = _changed_nodes(ws, base, f, nodes.get(f, []), skip_tests=test)
        if found is None or (not test and not found):
            unmapped.add(f)
            continue
        if test and found:
            module = [n["id"] for n in nodes.get(f, []) if n.get("label") == PurePosixPath(f).name]
            if not module:
                unmapped.add(f)
                continue
            found = found | set(module)
        for node_id in found:
            owners.setdefault(node_id, set()).add(f)
    if len(owners) > MAX_SYMBOLS:
        unmapped.update(*owners.values())
        owners = {}
    for node_id, fs in sorted(owners.items()):
        reached = affected_tests(graph, node_id, ws.path)
        if reached:
            tests |= reached
        elif reached is None or not all(map(is_test_file, fs)):   # a source symbol must reach a test
            unmapped |= fs
    if unmapped:
        imports = Imports(Path(ws.path))
        for f in sorted(unmapped):
            tests |= imports.tests_reaching(f)
    return sorted(t for t in tests if (Path(ws.path) / t).is_file())


def _module_path(root: Path, path: str) -> str | None:
    """Resolve packages, or a standalone pytest test module imported by its filename."""
    file = root / path
    parts = [] if file.stem == "__init__" else [file.stem]
    parent = file.parent
    while parent != root and (parent / "__init__.py").is_file():
        parts.insert(0, parent.name)
        parent = parent.parent
    # A deleted module keeps its name: tests still importing it must run.
    if (parent == file.parent and not is_test_file(path)) or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)


class Imports:
    """The modules each Python file of a checkout may import, from an AST
    scan: ``import a.b``, ``from a import b`` (relative too, and inside
    functions), ``__import__`` and ``importlib.import_module`` calls, dotted
    attribute chains (``brindle.daemon.routes.x`` after ``import
    brindle.daemon``) and dotted strings (``monkeypatch.setattr("brindle.x.y",
    ...)``, ``mock.patch(...)``). An import whose target isn't literal counts
    as importing anything its literal part allows (anything at all when no
    part is), and so does a file that doesn't parse: such a file is run, never
    left out, and never makes the whole scope unresolved."""

    def __init__(self, root: Path):
        self.root = root
        try:
            paths = git.paths(["ls-files", "--cached", "--others", "--exclude-standard", "*.py"], str(root))
        except git.GitError as e:
            raise Unscoped("git couldn't list the checkout's Python files") from e
        self.files = [p for p in paths if (root / p).is_file()]
        self._scans: dict[str, tuple[set[str], set[str]]] = {}

    def tests_reaching(self, path: str) -> set[str]:
        """The test files that may import ``path``'s module. When none names
        it outright (one that only may, through a nonliteral import, doesn't
        count), those importing a module that names it are added (one level)."""
        module = _module_path(self.root, path)
        if module is None:
            raise Unscoped(f"{path} is not in a package, so no import can name it")
        tests = [f for f in self.files if is_test_file(f)]
        found = {t for t in tests if self.imports(t, module)}
        if any(self.imports(t, module, loose=False) for t in found):
            return found
        importers = {m for f in self.files if f != path and not is_test_file(f)
                     and (m := _module_path(self.root, f)) and self.imports(f, module, loose=False)}
        return found | {t for t in tests if any(self.imports(t, m) for m in importers)}

    def imports(self, path: str, module: str, loose: bool = True) -> bool:
        """Whether ``path`` may import ``module`` (``loose``: also through a nonliteral import)."""
        if path not in self._scans:
            self._scans[path] = self._scan(path)
        names, prefixes = self._scans[path]
        return (any(n == module or n.startswith(module + ".") for n in names)
                or loose and any(not p or module == p or module.startswith(p + ".") for p in prefixes))

    def _scan(self, path: str) -> tuple[set[str], set[str]]:
        """(the modules ``path`` names, the packages it may import anything under; "" for all)."""
        names: set[str] = set()
        prefixes: set[str] = set()
        try:
            tree = ast.parse((self.root / path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            return names, {""}
        owner = _module_path(self.root, path)
        package = None if owner is None else owner.split(".")[:None if path.endswith("__init__.py") else -1]
        loaders = {"__import__"} | {n.asname or n.name for node in ast.walk(tree)
                                    if isinstance(node, ast.ImportFrom) and node.module == "importlib"
                                    for n in node.names if n.name == "import_module"}
        bound: dict[str, set[str]] = {}   # a name an import binds -> the absolute names it may stand for
        chains: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(n.name for n in node.names)
                for n in node.names:
                    if n.asname:
                        bound.setdefault(n.asname, set()).add(n.name)
            elif isinstance(node, ast.ImportFrom):
                prefix = _relative(package, node.level, node.module or "")
                if prefix is None:
                    prefixes.add("")
                    continue
                names.add(prefix)
                for n in node.names:
                    if n.name == "*":
                        prefixes.add(prefix)
                    else:
                        names.add(f"{prefix}.{n.name}")
                        bound.setdefault(n.asname or n.name, set()).add(f"{prefix}.{n.name}")
            elif isinstance(node, ast.Call) and (isinstance(node.func, ast.Name) and node.func.id in loaders
                                                 or isinstance(node.func, ast.Attribute)
                                                 and node.func.attr == "import_module"):
                _dynamic(node, package, names, prefixes)
            elif isinstance(node, ast.Attribute) and (dotted := _dotted(node)):
                chains.append(dotted)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "." in node.value \
                    and all(p.isidentifier() for p in node.value.split(".")):
                names.add(node.value)
        for dotted in chains:   # ``d.routes.x`` after ``import a.d as d`` or ``from a import d``: a.d.routes.x
            root, _, rest = dotted.partition(".")
            names.update([dotted, *(f"{target}.{rest}" for target in bound.get(root, ()))])
        return names, prefixes


_UNKNOWN = object()


def _relative(package: list[str] | None, level: int, name: str) -> str | None:
    """The absolute name of a ``level``-dot import of ``name`` from ``package``; None if there is none."""
    if not level:
        return name
    if package is None or level > len(package):
        return None
    return ".".join([*package[:len(package) - level + 1], *([name] if name else [])])


def _literal(node: ast.expr | None, default):
    if node is None:
        return default
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return _UNKNOWN


def _dynamic(call: ast.Call, package: list[str] | None, names: set[str], prefixes: set[str]) -> None:
    """Record what an ``__import__(name, globals, locals, fromlist, level)`` or
    ``import_module(name, package)`` call may import."""
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        prefixes.add("")
        return

    def arg(pos: int, kw: str, default=None):
        node = call.args[pos] if len(call.args) > pos else next(
            (k.value for k in call.keywords if k.arg == kw), None)
        return _literal(node, default)

    name = arg(0, "name")
    if isinstance(call.func, ast.Name) and call.func.id == "__import__":
        level, fromlist = arg(4, "level", 0), arg(3, "fromlist", ())
    else:   # import_module(name, package): a leading dot is relative to ``package``
        level, fromlist = 0, ()
        if isinstance(name, str) and name.startswith("."):
            level, name = len(name) - len(name.lstrip(".")), name.lstrip(".")
            anchor = arg(1, "package")
            package = anchor.split(".") if isinstance(anchor, str) and anchor else None
    module = _relative(package, level, name) if isinstance(name, str) and isinstance(level, int) else None
    if not module:
        prefixes.add("")
    elif isinstance(fromlist, (list, tuple, set)) and all(isinstance(x, str) for x in fromlist):
        names.update([module, *(f"{module}.{x}" for x in fromlist)])
    else:
        prefixes.add(module)


def _dotted(node: ast.Attribute) -> str | None:
    """``a.b.c`` for an attribute chain on a plain name."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.insert(0, node.attr)
        node = node.value
    return ".".join([node.id, *parts]) if isinstance(node, ast.Name) else None


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
        proc = spawn.read(["graphify", "affected", node_id, "--graph", str(graph)], cwd=cwd,
                          timeout=GRAPHIFY_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return {f for f in _AFFECTED_FILE.findall(proc.stdout) if is_test_file(f)}
