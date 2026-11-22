"""Per-test coverage maps: which tests executed each function of a checkout.

With ``scoped_checks: "coverage"`` (see ``brindle.check_scope``), every full
run of a plain pytest check that passes on a clean commit records per-test
coverage (pytest-cov's ``--cov-context=test``) and leaves behind a map, under
``brindle_home()/coverage-maps``, of the tests (pytest node ids,
``tests/test_x.py::TestY::test_z``, parameters dropped so every case of a
parametrized test is one entry) whose setup, call or
teardown executed each function, class and module-level line of every
tracked Python file at the commit it ran on. A later, narrowed run of that
check selects the tests that executed what changed since that commit (the
branch's change and anything else that moved since the map was recorded, so
the map's attribution always describes the code being diffed):

- a changed line inside a function or class selects the tests that executed
  that function (its nested functions included) or any method of that class;
- a module-level statement added, removed, edited or moved (statements are
  compared as a sequence, so order counts) that binds names (an assignment,
  an ``import``, a def) selects the tests that executed the functions that
  read them, in its own file and in the files importing them from there
  (star imports included), and transitively through module-level names built
  from them, and through classes and defs that read them on import (a class
  attribute: every ``obj.attr`` read; a decorator or default: the def's users);
- a class member added or removed (or a class-level value changed) selects
  the readers of that attribute anywhere and the tests of any method of the
  same name it may override;
- any other changed module-level statement (a call, an ``if`` block, a star
  import) selects every test that executed anything in its file, and the
  readers of every name it touches;
- a change inside a test function selects nothing here (the test file runs
  anyway, as do all test files changed since the map's commit);
- a function the change adds reaches its tests through the changed code that
  calls it.

``targets`` raises ``Stale`` (the check runs in full and records a fresh map)
when the map's commit is gone or the change reaches code the map doesn't know.

Code a test reaches only through a child process is not attributed to the test;
``check_scope`` adds each changed module's namesake tests for that reason.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from brindle import git

VERSION = 2   # 2: tests are node ids (1 had test files; such a map counts as missing and is recorded again)
COVERAGE_ARGS = ("--cov=.", "--cov-context=test", "--cov-report=")
PYTEST_CONFIG = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
LEFTOVER_SECONDS = 86400   # a run directory older than this belongs to a run that died
_STAR = re.compile(r"\bimport[\s\\(]*\*")   # ``from m import *``, however spaced or continued
_WORDS = re.compile(r"\w+")


def _file_words(text: str) -> set[str]:
    """Word tokens (``\\w+``) of ``text``, for cached membership checks."""
    return set(_WORDS.findall(text))


class Stale(Exception):
    """The map can't say which tests a change reaches; the message says why."""


def map_dir(repo_root: str | Path) -> Path:
    from brindle.config import brindle_home

    return brindle_home() / "coverage-maps" / hashlib.sha256(str(repo_root).encode()).hexdigest()[:16]


def map_path(repo_root: str | Path, check: str) -> Path:
    return map_dir(repo_root) / f"{hashlib.sha256(check.encode()).hexdigest()[:16]}.json"


def load(repo_root: str | Path, check: str) -> dict | None:
    """The map the last recorded full run of ``check`` left, or None."""
    try:
        data = json.loads(map_path(repo_root, check).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != VERSION or data.get("check") != check:
        return None
    return data


@dataclass
class Recording:
    """A full run of ``check`` that records per-test coverage: run ``command``
    with ``env``, then ``save`` the map (or just ``discard`` the data)."""
    repo_root: str
    check: str
    command: str
    data: Path
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def start(cls, repo_root: str, root: str, check: str, run: str) -> Recording:
        base = map_dir(repo_root)
        base.mkdir(parents=True, exist_ok=True)
        _remove_leftovers(base)
        work = Path(tempfile.mkdtemp(prefix="run-", dir=base))
        args = [*COVERAGE_ARGS, *(["--dist", "no"] if _dist_without_workers(Path(root), check) else [])]
        data = work / ".coverage"
        return cls(repo_root, check, f"{run} {' '.join(shlex.quote(a) for a in args)}", data,
                   {"COVERAGE_FILE": str(data)})

    def save(self, root: str, sha: str) -> bool:
        """Build the map from this run's data for ``root`` at commit ``sha``
        and store it in place of the old one. False when the run left no data."""
        if not self.data.is_file():
            return False
        try:
            built = build(Path(root), self.data, sha, self.check)
        except (OSError, sqlite3.Error, git.GitError):
            return False
        _write_atomic(map_path(self.repo_root, self.check), json.dumps(built, separators=(",", ":")))
        return True

    def discard(self) -> None:
        shutil.rmtree(self.data.parent, ignore_errors=True)


def _remove_leftovers(base: Path) -> None:
    cutoff = time.time() - LEFTOVER_SECONDS
    for old in base.glob("run-*"):
        try:
            if old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass


def _dist_without_workers(root: Path, check: str) -> bool:
    """Whether the run needs ``--dist no``: pytest-cov records no per-test
    context when a ``--dist`` mode is set (``addopts``) without ``-n`` workers."""
    try:
        toks = shlex.split(check)
    except ValueError:
        return False
    if any(t.startswith(("-n", "--numprocesses")) for t in toks):
        return False
    texts = (os.environ.get("PYTEST_ADDOPTS", ""), *(_read(root / name) for name in PYTEST_CONFIG))
    return any("--dist" in text for text in texts)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` through a unique temporary file beside
    it, so concurrent writers and readers never see half a map."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# -- building a map ------------------------------------------------------------------


def build(root: Path, data_file: Path, sha: str, check: str) -> dict:
    """The map of a coverage data file (coverage.py's SQLite format, read
    directly so brindle needn't depend on coverage) for checkout ``root``."""
    cases: dict[str, set[str]] = {}
    executed = _executed(root, data_file, cases)
    index: dict[str, int] = {}
    files: dict[str, dict[str, list[int]]] = {}
    for path in git.paths(["ls-files", "*.py"], root):   # (git.paths adds -z last: no "--" before the spec)
        text = _read(root / path)
        try:
            scopes = line_scopes(text)
        except (SyntaxError, ValueError):
            scopes = [""] * (len(text.splitlines()) + 2)
        entry: dict[str, set[int]] = {q: set() for q in scopes}
        for line, ran in executed.get(path, {}).items():
            qual = scopes[line] if 0 < line < len(scopes) else ""
            entry.setdefault(qual, set()).update(index.setdefault(t, len(index)) for t in ran)
        files[path] = {q: sorted(ids) for q, ids in entry.items()}
    tests = sorted(index, key=index.get)
    return {"version": VERSION, "check": check, "commit": sha, "built": time.time(), "tests": tests,
            # How many cases of each test ran (a parametrized one's; ids aren't kept: they may change).
            "cases": [len(cases.get(t, ())) or 1 for t in tests], "files": files}


def _executed(root: Path, data_file: Path,
              cases: dict[str, set[str]] | None = None) -> dict[str, dict[int, set[str]]]:
    """{repo-relative file: {line: tests (``node``) whose context executed it}}.
    ``cases`` gets each test's cases (its parametrized ids) as they ran."""
    roots = {str(root), str(root.resolve())}
    con = sqlite3.connect(f"file:{data_file}?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
        paths = {}
        for fid, path in con.execute("SELECT id, path FROM file"):
            rel = next((path[len(r) + 1:] for r in roots if path.startswith(r + os.sep)), None)
            if rel is not None:
                paths[fid] = PurePosixPath(Path(rel)).as_posix()
        contexts = {}
        for cid, ctx in con.execute("SELECT id, context FROM context"):
            if test := node(ctx):
                contexts[cid] = test
                if cases is not None:
                    case, bar, phase = ctx.rpartition("|")
                    cases.setdefault(test, set()).add(case if bar and phase in _PHASES else ctx)
        out: dict[str, dict[int, set[str]]] = {}

        def add(fid, cid, line):
            if fid in paths and cid in contexts and line > 0:
                out.setdefault(paths[fid], {}).setdefault(line, set()).add(contexts[cid])

        if meta.get("has_arcs") in ("1", "True", "true"):
            for fid, cid, a, b in con.execute("SELECT file_id, context_id, fromno, tono FROM arc"):
                add(fid, cid, a)
                add(fid, cid, b)
        else:
            for fid, cid, bits in con.execute("SELECT file_id, context_id, numbits FROM line_bits"):
                for i, byte in enumerate(bits):
                    for bit in range(8):
                        if byte & (1 << bit):
                            add(fid, cid, i * 8 + bit)
        return out
    finally:
        con.close()


_PHASES = ("setup", "run", "teardown")


def node(context: str) -> str:
    """The test a pytest-cov context (``tests/t.py::C::test_x[a-1]|run``)
    names, as a node id pytest selects every case of: no phase, no
    parameters (their ids may not be stable from run to run). "" for a
    context that is no test's (collection, module import)."""
    test, bar, phase = context.rpartition("|")
    if not bar or phase not in _PHASES:
        test = context
    file, sep, name = test.partition("::")
    if not sep or not file or not name:
        return ""
    return f"{file}::{name.split('[', 1)[0]}"


def test_file(selector: str) -> str:
    """The file of a test selector: a node id's file, or the file itself."""
    return selector.split("::", 1)[0]


def line_scopes(text: str) -> list[str]:
    """The qualified name of the innermost function or class around each line
    (index = line number, decorators included); "" at module level."""
    tree = ast.parse(text)
    out = [""] * (len(text.splitlines()) + 2)

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}.{child.name}" if prefix else child.name
                start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                for line in range(start, min(child.end_lineno or child.lineno, len(out) - 1) + 1):
                    out[line] = qual
                visit(child, qual)
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


# -- selecting tests -------------------------------------------------------------------


def changed_since(root: str | Path, data: dict) -> list[str]:
    """The files that differ between the map's commit and the checkout at
    ``root``: what a narrowed run must account for, so that code that moved
    since the map was recorded (a new caller, say) is selected too."""
    commit = str(data.get("commit") or "")
    if not commit or not git.ok(["cat-file", "-e", f"{commit}^{{commit}}"], root):
        raise Stale("the commit the coverage map was recorded at is gone")
    return git.paths(["diff", "--name-only", "--no-renames", commit], root)


def targets(root: str | Path, data: dict, files: list[str], hits: dict[str, int] | None = None) -> set[str]:
    """The tests ``data`` (a ``load``ed map) says executed what changed in
    ``files`` (see ``changed_since``) between the map's commit and the
    checkout at ``root``: the map's node ids, and whole test files for a
    change the map can't attribute to single tests (a table, a fixture).
    Raises ``Stale`` when a change reaches code the map
    doesn't know. ``hits``, when given, counts for each selected test how
    many of the changed scopes selected it (for ``rank``)."""
    root = Path(root)
    commit = str(data["commit"])
    tests = data.get("tests") or []
    known = data.get("files") or {}
    found: set[str] = set()

    def add(selected: set[str]) -> None:
        found.update(selected)
        if hits is not None:
            for t in selected:
                hits[t] = hits.get(t, 0) + 1

    def reach(path: str, qual: str) -> set[str]:
        entry = known.get(path) or {}
        return {tests[i] for k in entry if not qual or k == qual or k.startswith(qual + ".")
                for i in entry[k] if 0 <= i < len(tests)}

    words_cache: dict[str, set[str]] = {}
    bindings: dict[str, tuple[set[str], set[str], set[str]]] = {}   # file -> (names rebound, names new, members)
    for f in files:
        if not f.endswith(".py"):
            continue
        entry = known.get(f)
        for change in changed_scopes(root, commit, f):
            qual = change.qual
            if entry is None:
                if change.at_base:
                    raise Stale(f"{f} is newer than the coverage map")
                continue
            if qual and change.at_base and not any(k == qual or k.startswith(qual + ".") for k in entry):
                raise Stale(f"{qual}() in {f} is newer than the coverage map")
            if change.tables:
                add(tests_of_tables(root, set(change.tables), words_cache=words_cache))
            if qual or change.whole:
                add(reach(f, qual))   # a function, or a module-level statement of unknown reach
            shared, local, members = bindings.setdefault(f, (set(), set(), set()))
            shared |= change.names
            local |= change.new
            members |= change.attrs
    conftest_names: dict[str, set[str]] = {}   # conftest file -> top-level names a change reaches
    for f in files:
        if PurePosixPath(f).name == "conftest.py":
            if f not in known or not (root / f).is_file():   # its collection-time code reaches every test below
                raise Stale(f"{f} was added or removed")
            changes = changed_scopes(root, commit, f)
            # pytest reads a conftest's module-level settings while collecting, directly or through names
            # they are built from: no reader to follow there.
            if any(not c.qual for c in changes):
                raise Stale(f"module-level code of {f} changed")
            conftest_names[f] = {c.qual.split(".")[0] for c in changes if c.qual}
    if any(any(b) for b in bindings.values()):
        readers = Readers(root, words_cache=words_cache)
        for f, (shared, local, members) in sorted(bindings.items()):
            if not (shared or local or members):
                continue
            # (A member added or removed that overrides or uncovers another of its name is called as
            # ``obj.x``: its readers are those callers.)
            quals, whole = readers.of(f, shared, local, members)
            for path, qs in quals.items():
                for q in qs:
                    add(reach(path, q))
                if PurePosixPath(path).name == "conftest.py":
                    conftest_names.setdefault(path, set()).update(q.split(".")[0] for q in qs)
            for path in whole:
                add(reach(path, ""))
    for path, names in sorted(conftest_names.items()):
        add(conftest_tests(root, path, names, commit, words_cache=words_cache))
    return found


def test_counts(data: dict) -> dict[str, int]:
    """How many test cases each of the map's tests (every case of a
    parametrized one) and each test file holds, as the map's run saw them."""
    tests = data.get("tests") or []
    cases = data.get("cases") or []
    out: dict[str, int] = {}
    for i, t in enumerate(tests):
        if "::" in t:
            got = cases[i] if i < len(cases) else 1
            got = len(got) if isinstance(got, list) else got   # (a list of case ids, as an early map kept)
            out[t] = got if isinstance(got, int) and got > 0 else 1
            out[test_file(t)] = out.get(test_file(t), 0) + out[t]
    return out


def rank(data: dict, tests: set[str], hits: dict[str, int]) -> list[str]:
    """``tests`` (``targets``'s, with its ``hits``), most specific to the
    change first: by the share of the scopes (functions, classes, module
    level) each test executed in ``data`` that the change selected it
    through, so a test that runs only the changed code ranks above one that
    passes through it on the way to everything else. A test the map has no
    scopes for (selected by a table or fixture name) counts as fully
    specific. Ties keep more hits first, then name order."""
    names = data.get("tests") or []
    covered = [0] * len(names)
    for entry in (data.get("files") or {}).values():
        for ids in entry.values():
            for i in ids:
                if 0 <= i < len(names):
                    covered[i] += 1
    scopes = dict(zip(names, covered))

    def key(t: str):
        n = hits.get(t, 0)
        return -n / max(scopes.get(t, 0), n, 1), -n, t

    return sorted(tests, key=key)


def conftest_tests(root: Path, path: str, names: set[str], commit: str, *,
                   words_cache: dict[str, set[str]] | None = None) -> set[str]:
    """For a change reaching ``names`` (its top-level functions) in the
    conftest ``path``: the test files below it that name a fixture it reaches
    or a helper it changed, since the map credits a session or module
    fixture's setup only to the first test that ran it. A fixture is reached
    when it is changed, calls a reached helper, or requests a reached fixture
    (transitively), and is named as tests request it (``name=`` when given).
    Raises ``Stale`` for a pytest hook or an ``autouse`` fixture reached, in
    either version, which every test there goes through."""
    from brindle import check_scope

    if hook := next((n for n in sorted(names) if n.startswith("pytest_")), None):
        raise Stale(f"the pytest hook {hook}() in {path} changed")
    if not names:
        return set()
    _, old, new = _texts(root, commit, path)
    below = str(PurePosixPath(path).parent)
    files = git.paths(["ls-files", "*.py"], root)
    # Fixtures in conftests further down may request this one's (an override there only adds tests).
    nested: dict[str, FixtureDef] = {}
    for other in files:
        if other != path and PurePosixPath(other).name == "conftest.py" and (below == "." or other.startswith(below + "/")):
            try:
                tree = ast.parse(_read(root / other))
            except (SyntaxError, ValueError) as e:
                raise Stale(f"{other} can't be parsed") from e
            nested.update({f"{other}::{k}": v for k, v in _conftest_defs(tree).items()})
    words: set[str] = set()
    for text in (old or "", new):
        defs = {**_conftest_defs(ast.parse(text)), **nested}
        reached = set(names)

        def called(keys: set[str]) -> set[str]:   # the names code uses to reach ``keys``: function and fixture names
            return {k.rsplit("::", 1)[-1] for k in keys} | {defs[k][0] for k in keys if k in defs and defs[k][0]}

        while True:   # helpers calling reached code, fixtures requesting reached fixtures, in any conftest below
            known = called(reached)
            more = {d for d, (_, _, uses, _) in defs.items() if d not in reached and uses & known}
            if not more:
                break
            reached |= more
        for d in reached:
            if d in defs and defs[d][1]:
                raise Stale(f"the autouse fixture {d.rsplit('::', 1)[-1]}() is reached by the change to {path}")
            if d in defs and defs[d][3]:
                raise Stale(f"{d.rsplit('::', 1)[-1]}() (a hook, or a decorator brindle can't resolve) is reached "
                            f"by the change to {path}")
        words |= called(reached)
    plain = {w for w in words if _WORDS.fullmatch(w)}
    other = words - plain
    other_re = re.compile(r"\b(" + "|".join(map(re.escape, sorted(other))) + r")\b") if other else None

    def matches(t: str) -> bool:
        if words_cache is not None and t in words_cache:
            if plain & words_cache[t]:
                return True
            return bool(other_re and other_re.search(_read(root / t)))
        text = _read(root / t)
        tw = _file_words(text)
        if words_cache is not None:
            words_cache[t] = tw
        return bool(plain & tw or (other_re and other_re.search(text)))

    return {t for t in files if check_scope.is_test_file(t)
            and (below == "." or t.startswith(below + "/")) and matches(t)}


FixtureDef = tuple[str | None, bool, set[str], bool]


def _conftest_defs(tree: ast.Module) -> dict[str, FixtureDef]:
    """{top-level function: (the name tests request it by when it is a
    fixture, whether it is autouse, the names it uses: its parameters, every
    name it reads and its strings (``getfixturevalue("x")``), and whether its
    kind is unknown: a decorator that is neither the ``fixture`` of one of the
    ``FIXTURE_MODULES`` nor from ``SAFE_DECORATOR_MODULES``, or a fixture
    whose name is an expression)}. ``conftest_tests`` runs the check in full
    when the change reaches an unknown one."""
    from brindle import check_scope

    modules, fixture_names, safe = _decorator_imports(tree)
    out: dict[str, FixtureDef] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        public, autouse, unknown = None, False, False
        for d in node.decorator_list:
            callee = d.func if isinstance(d, ast.Call) else d
            chain = check_scope._dotted(callee) if isinstance(callee, ast.Attribute) else getattr(callee, "id", None)
            head, _, rest = (chain or "").partition(".")
            # pytest's fixture or pytest_asyncio's, however aliased
            if head in fixture_names and not rest or head in modules and rest == "fixture":
                public = node.name
                for kw in d.keywords if isinstance(d, ast.Call) else []:
                    literal = isinstance(kw.value, ast.Constant)
                    if kw.arg == "name":
                        public = kw.value.value if literal and isinstance(kw.value.value, str) else public
                        unknown |= not (literal and isinstance(kw.value.value, str))
                    elif kw.arg == "autouse":
                        autouse |= not literal or bool(kw.value.value)
            elif head not in safe:
                unknown = True   # any decorator but a fixture or one from SAFE_DECORATOR_MODULES
        params = {a.arg for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
        reads = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        strings = {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        out[node.name] = (public, autouse, params | reads | strings, unknown or node.name.startswith("pytest_"))
    return out


SAFE_DECORATOR_MODULES = {"functools", "contextlib", "typing"}   # decorators known not to make a fixture
FIXTURE_MODULES = {"pytest", "_pytest", "pytest_asyncio"}   # whose ``fixture`` is pytest's, with its name= and autouse


def _decorator_imports(tree: ast.Module) -> tuple[set[str], set[str], set[str]]:
    """(local names of the ``FIXTURE_MODULES``, local names of their
    ``fixture``, local names from ``SAFE_DECORATOR_MODULES``) in ``tree``,
    by absolute imports only: a local wrapper named ``fixture`` may set its
    own name or autouse, so it stays unknown."""
    modules, fixtures, safe = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if a.asname is None or a.name == top:   # ``import a.b as c`` names a.b, not a
                    local = a.asname or top
                    if top in FIXTURE_MODULES:
                        modules.add(local)
                    elif top in SAFE_DECORATOR_MODULES:
                        safe.add(local)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            source = (node.module or "").split(".")[0]
            for a in node.names:
                local = a.asname or a.name
                if source in FIXTURE_MODULES and a.name == "fixture":
                    fixtures.add(local)
                elif source in FIXTURE_MODULES and a.name in FIXTURE_MODULES:   # ``from _pytest import pytest``
                    modules.add(local)
                elif source in SAFE_DECORATOR_MODULES:
                    safe.add(local)
    return modules, fixtures, safe


class Readers:
    """Where the code of a checkout reads a module-level name: the functions
    (by qualified name) that load it, in its own file, in the files that import
    it from there (``from m import x``, ``m.x``, ``"x"`` in a file naming m),
    and through module-level names built from it or re-exported, transitively.
    A file that reads it in a module-level statement binding nothing counts
    whole. Over-approximates: a superset of the real readers."""

    def __init__(self, root: Path, *, words_cache: dict[str, set[str]] | None = None):
        self.root = root
        self.files = git.paths(["ls-files", "*.py"], root)
        self._trees: dict[str, ast.Module | None] = {}
        self._texts: dict[str, str] = {}
        self._words: dict[str, set[str]] = {} if words_cache is None else words_cache
        self._stars: dict[str, bool] = {}
        self._index: dict[str, tuple[dict[str, list], dict[str, list]]] = {}
        self._import_asts: dict[str, tuple[list[str] | None, list[ast.Import | ast.ImportFrom]]] = {}
        self._statements: dict[str, dict[int, frozenset[str] | None]] = {}
        self.attrs: set[str] = set()   # class members whose readers (``obj.x`` anywhere) are wanted
        self._scanned: set[str] = set()

    def indexed(self, path: str, tree: ast.Module) -> tuple[dict[str, list], dict[str, list]]:
        """(reads by bare name/string, reads by attribute/class-body name/string) in ``tree``."""
        if path not in self._index:
            by_load: dict[str, list] = {}
            by_attr: dict[str, list] = {}
            for node, qual, at_run, owner in _walk_scoped(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    entry = (node.lineno, qual, at_run, owner)
                    by_load.setdefault(node.id, []).append(entry)
                    # In a class body a member reads another by its bare name (``doubled = threshold * 2``).
                    if not at_run and owner is not None and owner[0] == "attr":
                        by_attr.setdefault(node.id, []).append(entry)
                elif isinstance(node, ast.Attribute):
                    by_attr.setdefault(node.attr, []).append((node.lineno, qual, at_run, owner))
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    entry = (node.lineno, qual, at_run, owner)
                    by_load.setdefault(node.value, []).append(entry)
                    by_attr.setdefault(node.value, []).append(entry)
                    tail = node.value.rsplit(".", 1)[-1]
                    if tail != node.value:
                        by_attr.setdefault(tail, []).append(entry)
            self._index[path] = (by_load, by_attr)
        return self._index[path]

    def text(self, path: str) -> str:
        if path not in self._texts:
            self._texts[path] = _read(self.root / path)
        return self._texts[path]

    def words(self, path: str) -> set[str]:
        if path not in self._words:
            self._words[path] = _file_words(self.text(path))
        return self._words[path]

    def has_star(self, path: str) -> bool:
        if path not in self._stars:
            self._stars[path] = bool(_STAR.search(self.text(path)))
        return self._stars[path]

    def _import_nodes(self, path: str, tree: ast.Module) -> tuple[list[str] | None, list[ast.Import | ast.ImportFrom]]:
        if path not in self._import_asts:
            from brindle import check_scope

            owner = check_scope._module_path(self.root, path)
            package = None if owner is None else owner.split(".")[:None if path.endswith("__init__.py") else -1]
            nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
            self._import_asts[path] = (package, nodes)
        return self._import_asts[path]

    def tree(self, path: str) -> ast.Module | None:
        if path not in self._trees:
            try:
                self._trees[path] = ast.parse(self.text(path))
            except (SyntaxError, ValueError):
                self._trees[path] = None
        return self._trees[path]

    def of(self, path: str, names: set[str], new: set[str] = frozenset(),
           attrs: set[str] = frozenset()) -> tuple[dict[str, set[str]], set[str]]:
        """({file: the functions reading them}, files reading them in a
        module-level statement of unknown reach) for module-level ``names``
        of ``path``, and class members ``attrs`` (read as ``obj.x`` or
        ``"x"`` in any file); ``new`` names, which ``path`` didn't bind
        before, are looked up in ``path`` alone: no other file's existing
        code reads them."""
        from brindle import check_scope

        quals: dict[str, set[str]] = {}
        whole: set[str] = set()
        seen: set[tuple[str, str]] = set()
        work: list[tuple[str, set[str], set[str]]] = [(path, set(names) | set(new), set())]   # (file, names, attrs)
        self.attrs |= set(attrs)
        while work or self.attrs - self._scanned:
            if not work:   # members: every file that may read them
                fresh = self.attrs - self._scanned
                self._scanned |= fresh
                for g in self.files:
                    if fresh & self.words(g):
                        self._read_in(g, set(), fresh, quals, whole, work)
                continue
            f, loads, attrs = work.pop()
            loads = {n for n in loads if (f, n) not in seen}
            seen.update((f, n) for n in loads)
            if loads or attrs:
                self._read_in(f, loads, attrs, quals, whole, work)
            module = check_scope._module_path(self.root, f)
            shared = loads - set(new) if f == path else loads
            if not shared or module is None:
                continue
            for g in self.files:   # (a star import re-exports names it never spells)
                if g == f or not (shared & self.words(g) or self.has_star(g)):
                    continue
                tree = self.tree(g)
                if tree is None:
                    whole.add(g)
                    continue
                local = self._imported(g, tree, module, shared)
                if local:
                    work.append((g, local, set()))
                if module + "." in self.text(g) or self._imports(g, tree, module):   # ``m.x``, ``"x"``, ``"pkg.m.x"``
                    self._read_in(g, set(), shared, quals, whole, work)
        return quals, whole

    def _imports(self, path: str, tree: ast.Module, module: str) -> bool:
        """Whether ``path`` imports ``module`` itself (or a package it can
        reach it from by attribute), not just names from it."""
        from brindle import check_scope

        package, nodes = self._import_nodes(path, tree)
        for node in nodes:
            if isinstance(node, ast.Import):
                if any(module == a.name or module.startswith(a.name + ".") for a in node.names):
                    return True
            else:
                source = check_scope._relative(package, node.level, node.module or "")
                if source is None:
                    return True
                if any(module == f"{source}.{a.name}" or module.startswith(f"{source}.{a.name}.")
                       for a in node.names):
                    return True
        return False

    def _imported(self, path: str, tree: ast.Module, module: str, names: set[str]) -> set[str]:
        """The local names ``path`` binds by importing any of ``names`` from ``module``."""
        from brindle import check_scope

        package, nodes = self._import_nodes(path, tree)
        local: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.ImportFrom) and check_scope._relative(package, node.level,
                                                                          node.module or "") == module:
                for alias in node.names:
                    if alias.name == "*":
                        local |= names
                    elif alias.name in names:
                        local.add(alias.asname or alias.name)
        return local

    def _read_in(self, path: str, loads: set[str], attrs: set[str], quals: dict[str, set[str]],
                 whole: set[str], work: list) -> None:
        tree = self.tree(path)
        if tree is None:
            whole.add(path)
            return
        if path not in self._statements:
            self._statements[path] = _module_statements_of(tree)
        statements = self._statements[path]
        by_load, by_attr = self.indexed(path, tree)
        hits = [entry for n in loads for entry in by_load.get(n, ())]
        if attrs:
            hits += [entry for n in attrs for entry in by_attr.get(n, ())]
        for lineno, qual, at_run, owner in hits:
            if at_run:   # read when its function runs: the tests that ran it
                quals.setdefault(path, set()).add(qual)
            elif owner:   # a class body or a def's decorators and defaults, read on import: their users
                if qual:
                    quals.setdefault(path, set()).add(qual)
                kind, bound, cls = owner
                dunders = {b for b in bound if b.startswith("__") and b.endswith("__")} if kind == "attr" else set()
                if kind == "attr":
                    self.attrs |= bound - dunders
                if kind == "name" or dunders:   # protocol members are used through the class itself
                    work.append((path, set(bound) if kind == "name" else {cls.split(".")[0]}, set()))
            elif bound := statements.get(lineno):
                work.append((path, set(bound), set()))   # a module-level name built from it
            else:   # e.g. ``register(x)``, ``CONFIG.update(x)``: the file, and whatever the statement names
                whole.add(path)
                work.append((path, _loads(_statement_at(tree, lineno)), set()))


Owner = tuple[str, frozenset, str]   # ("name", module-level names, "") or ("attr", attribute names, class qual)


def _walk_scoped(tree: ast.Module) -> list[tuple[ast.AST, str, bool, Owner | None]]:
    """Every node of ``tree`` with (the qualified name of the function or
    class around it, whether it is evaluated when a function runs rather than
    on import, and for an import-time read: whose users see what it read).
    That is, for a top-level def's or class's decorators, defaults, bases and
    the like, the module-level name it binds; for a member of a class body,
    the attribute names it binds (``obj.x`` anywhere reads them)."""
    out: list[tuple[ast.AST, str, bool, Owner | None]] = []

    def walk(node: ast.AST, qual: str, at_run: bool, owner: Owner | None) -> None:
        out.append((node, qual, at_run, owner))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            inner = f"{qual}.{node.name}" if qual else node.name
            own = None if at_run else owner or ("name", frozenset({node.name}), "")
            for name in ("decorator_list", "args", "returns", "bases", "keywords"):
                value = getattr(node, name, None)
                for child in value if isinstance(value, list) else [value] if value is not None else []:
                    walk(child, qual, at_run, own)
            if not isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    walk(stmt, inner, True, None)
                return
            for stmt in node.body:
                bound = _member_names(stmt)
                walk(stmt, inner, at_run, None if at_run else ("attr", bound, inner) if bound else own)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, qual, at_run, owner)

    walk(tree, "", False, None)
    return out


def _member_names(stmt: ast.stmt) -> frozenset[str]:
    """The attributes a statement of a class body binds (none: unknown)."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return frozenset({stmt.name})
    if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return frozenset(n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
    return frozenset()


def _statement_at(tree: ast.Module, line: int) -> ast.stmt | None:
    return next((s for s in tree.body if s.lineno <= line <= (s.end_lineno or s.lineno)), None)


def _loads(node: ast.AST | None) -> set[str]:
    """The names ``node`` reads or binds (``x.y = ...`` and ``x.update()`` change ``x``)."""
    return set() if node is None else {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


class Change(NamedTuple):
    """What a change to a file edits: a function or class (``qual``), or a
    module-level statement (``qual`` ""): every test that ran the file when
    ``whole``, and the readers of the module-level ``names`` it rebinds or
    removes (anywhere) and of the ``new`` names it adds (in its own file)."""
    qual: str
    at_base: bool
    whole: bool = False
    names: frozenset = frozenset()
    new: frozenset = frozenset()
    attrs: frozenset = frozenset()   # class members it changes: read as ``obj.x`` anywhere
    tables: frozenset = frozenset()  # SQL tables a schema string only gained (``added_ddl``): their tests


def changed_scopes(root: Path, base: str, path: str) -> set[Change]:
    """What a change to ``path`` since ``base`` edits (see ``Change``).
    Functions and classes come from the changed lines (not blank or
    comment-only ones, nor those inside a test file's test functions);
    module-level statements from comparing the sequence of statements, so one
    that moved counts as changed as well as one that was edited, added or
    removed (the module docstring aside). An ``import`` edited in place counts
    only the names whose source changed."""
    from brindle import check_scope

    try:
        diff, old_text, new_text = _texts(root, base, path)
        old_lines, new_lines = check_scope._changed_lines(diff)
        at_base = old_text is not None
        old_text = old_text or ""
        old_scopes, new_scopes = line_scopes(old_text), line_scopes(new_text)
        old_tree, new_tree = ast.parse(old_text), ast.parse(new_text)
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError, git.GitError) as e:
        raise Stale(f"{path} couldn't be read or parsed") from e
    in_base = set(old_scopes) if at_base else set()
    test_file = check_scope.is_test_file(path)
    old_names = set(_module_bindings(old_tree))
    both = set(old_scopes) & set(new_scopes)
    out: set[Change] = set()
    for text, lines, scopes, tree, old in ((old_text, old_lines, old_scopes, old_tree, True),
                                           (new_text, new_lines, new_scopes, new_tree, False)):
        source = text.splitlines()
        owners = _import_time_owners(tree)
        for line in lines:
            if line > len(source) or not source[line - 1].strip() or source[line - 1].lstrip().startswith("#"):
                continue
            qual = scopes[line]
            if qual and not (test_file and any(p.startswith(("test", "Test")) for p in qual.split("."))):
                out.add(Change(qual, old or qual in in_base))
            if owner := owners.get(line):   # read on import: what the class, def or member binds changed
                out |= _owner_change(owner, qual, both, old_names, at_base)
    out |= _module_changes(old_tree, new_tree, at_base)
    return out


def _owner_change(owner: Owner, qual: str, both: set[str], old_names: set[str], at_base: bool) -> set[Change]:
    """What changing a line read on import inside ``owner`` changes for others.
    A member of a class that is new (or gone) changes nothing existing code
    can reach but the class itself (found as a module-level change); a method
    in both versions reaches its users through the tests that ran it."""
    kind, bound, cls = owner
    if kind == "name":
        return {Change("", at_base, names=frozenset(bound & old_names), new=frozenset(bound - old_names))}
    if cls not in both or (qual.rsplit(".", 1)[-1] in bound and qual in both):
        return set()
    dunders = frozenset(b for b in bound if b.startswith("__") and b.endswith("__"))
    out = {Change("", at_base, attrs=bound - dunders)} if bound - dunders else set()
    if dunders:   # called by protocol (``C(...)``, ``==``), never read as an attribute: the class's users
        top = cls.split(".")[0]
        out.add(Change("", at_base, names=frozenset({top} & old_names), new=frozenset({top} - old_names)))
    return out


def _import_time_owners(tree: ast.Module) -> dict[int, Owner]:
    """{line: ``_walk_scoped``'s owner} for each line read on import inside a
    class body or a def's decorators, defaults and annotations."""
    out: dict[int, Owner] = {}
    for node, _, at_run, owner in _walk_scoped(tree):
        if at_run or not owner or not hasattr(node, "lineno"):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.setdefault(node.lineno, owner)   # the ``def`` line of a member (its body runs later)
            continue
        for line in range(node.lineno, (getattr(node, "end_lineno", None) or node.lineno) + 1):
            out.setdefault(line, owner)
    return out


def _module_changes(old_tree: ast.Module, new_tree: ast.Module, at_base: bool) -> set[Change]:
    old_bindings, new_bindings = _module_bindings(old_tree), _module_bindings(new_tree)
    old_body, new_body = _sequence(old_tree), _sequence(new_tree)
    matcher = difflib.SequenceMatcher(None, [k for _, k in old_body], [k for _, k in new_body], autojunk=False)
    # A def or import found unchanged in both versions, only elsewhere, matters only to what reads it on import.
    in_both = {k for _, k in old_body} & {k for _, k in new_body}
    read_on_import = _import_time_reads(old_tree) | _import_time_reads(new_tree)
    keys = {id(s): k for s, k in old_body + new_body}
    out: set[Change] = set()

    def names_of(stmts: list[ast.stmt], paired: bool = False) -> None:
        for stmt in stmts:
            moved = keys[id(stmt)] in in_both
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound = {stmt.name}   # added, removed or moved: its users (a body change is found by line)
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)) and not any(a.name == "*" for a in stmt.names):
                bound = {a.asname or a.name.split(".")[0] for a in stmt.names}
                if paired:   # edited in place: only the names whose source changed
                    bound = {n for n in bound if old_bindings.get(n) != new_bindings.get(n)}
            else:
                bound = None
            if bound is not None:
                if moved:   # unless its final binding (the order of a name's bindings) or an import-time read moved
                    bound = {n for n in bound if n in read_on_import or old_bindings.get(n) != new_bindings.get(n)}
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and \
                    all(isinstance(t, ast.Name) for t in getattr(stmt, "targets", [getattr(stmt, "target", None)])):
                bound = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            else:   # a call, a block, a star import: the whole file, and every name it touches
                out.add(Change("", at_base, whole=True, names=frozenset(_loads(stmt))))
                continue
            shared = frozenset(n for n in bound if n in old_bindings)
            if bound:
                out.add(Change("", at_base, names=shared, new=frozenset(bound) - shared))

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        olds, news = [s for s, _ in old_body[i1:i2]], [s for s, _ in new_body[j1:j2]]
        for o in olds[:]:   # a SQL schema string that only gained tables or columns: their tests
            n = next((n for n in news if _string_target(n) is not None and _string_target(n) == _string_target(o)),
                     None)
            tables = None if n is None else added_ddl(o.value.value, n.value.value)
            if tables:
                olds.remove(o)
                news.remove(n)
                out.add(Change("", at_base, tables=frozenset(tables)))
        for o in olds[:]:   # an import edited in place: pair it with the new one of the same source
            source = _import_source(o)
            n = next((n for n in news if source is not None and _import_source(n) == source), None)
            if n is not None:
                olds.remove(o)
                news.remove(n)
                names_of([o, n], paired=True)
        names_of(olds)
        names_of(news)
    return out


def _string_target(stmt: ast.stmt) -> str | None:
    """``NAME`` for a statement ``NAME = "a string literal"``."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) \
            and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
        return stmt.targets[0].id
    return None


_CREATE_TABLE = re.compile(r"create\s+table\s+(?:if\s+not\s+exists\s+)?[\"`\[]?(\w+)", re.I)
_ADD_COLUMN = re.compile(r"alter\s+table\s+[\"`\[]?(\w+)[\"`\]]?\s+add\s+(?:column\s+)?", re.I)


_CLOSE = {"'": "'", '"': '"', "`": "`", "[": "]"}


def _sql_statements(text: str) -> list[str]:
    """``text``'s SQL statements, with ``--`` and ``/* */`` comments dropped
    and whitespace outside quotes collapsed; quoted text (strings and quoted
    names) is kept exactly as it is."""
    statements: list[str] = []
    current: list[str] = []
    space = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in _CLOSE:   # a quoted run, its doubled closing quotes included
            close = _CLOSE[c]
            j = i + 1
            while j < n:
                if text[j] == close:
                    if close != "]" and j + 1 < n and text[j + 1] == close:
                        j += 2
                        continue
                    break
                j += 1
            if space and current:
                current.append(" ")
            space = False
            current.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            space = True
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            space = True
        elif c == ";":
            if current:
                statements.append("".join(current))
            current, space = [], False
            i += 1
        elif c.isspace():
            space = True
            i += 1
        else:
            if space and current:
                current.append(" ")
            space = False
            current.append(c)
            i += 1
    if current:
        statements.append("".join(current))
    return statements


def added_ddl(old: str, new: str) -> set[str] | None:
    """The tables a SQL schema text gains from ``old`` to ``new`` when the
    change only adds DDL: new ``CREATE TABLE`` statements for tables it didn't
    have, and ``ALTER TABLE ... ADD COLUMN``. None for anything else (a
    statement removed or edited, a drop, a rename, a changed constraint, or a
    text that isn't SQL at all), which then reaches its readers as usual."""
    before, after = _sql_statements(old), _sql_statements(new)
    if not any(_CREATE_TABLE.match(s) for s in before):
        return None   # not a schema
    if any(s not in after for s in before):
        return None
    had = {m.group(1).lower() for s in before if (m := _CREATE_TABLE.match(s))}
    tables: set[str] = set()
    for s in after:
        if s in before:
            continue
        if (m := _CREATE_TABLE.match(s)) and m.group(1).lower() not in had:
            tables.add(m.group(1))
        elif m := _ADD_COLUMN.match(s):
            tables.add(m.group(1))
        else:
            return None
    return tables or None


SCHEMA_TESTS = re.compile(r"schema|migrat", re.I)


def tests_of_tables(root: Path, tables: set[str], *,
                    words_cache: dict[str, set[str]] | None = None) -> set[str]:
    """The tracked test files naming any of ``tables``, and the schema and
    migration tests (``schema`` or ``migrat`` in their file name)."""
    from brindle import check_scope

    def matches(t: str) -> bool:
        if words_cache is not None and t in words_cache:
            tw = words_cache[t]
        else:
            tw = _file_words(_read(root / t))
            if words_cache is not None:
                words_cache[t] = tw
        return bool(tables & tw)

    return {t for t in git.paths(["ls-files", "*.py"], root) if check_scope.is_test_file(t)
            and (SCHEMA_TESTS.search(PurePosixPath(t).name) or matches(t))}


def _import_time_reads(tree: ast.Module) -> set[str]:
    """The names ``tree`` reads on import: in module-level statements, class
    bodies and the decorators, defaults and annotations of defs."""
    return {node.id for node, _, at_run, _ in _walk_scoped(tree)
            if not at_run and isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}


def _sequence(tree: ast.Module) -> list[tuple[ast.stmt, str]]:
    """The module-level statements of ``tree`` with a key that is equal when
    two versions' statements bind the same way (a def by its name only: a
    change to its body is found by its lines); the docstring left out."""
    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return [(s, f"def {s.name}" if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
             else ast.dump(s)) for s in body]


def _import_source(stmt: ast.stmt) -> tuple | None:
    if isinstance(stmt, ast.ImportFrom):
        return ("from", stmt.level, stmt.module)
    if isinstance(stmt, ast.Import):
        return ("import",)
    return None


def _module_bindings(tree: ast.Module) -> dict[str, tuple]:
    """What each module-level name of ``tree`` is bound to, comparable across
    versions: the imported source, the assignment statement (positions left
    out), or just "a def" (a function's own change is found by its lines)."""
    out: dict[str, list] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                out.setdefault(a.asname or a.name.split(".")[0], []).append(("import", a.name))
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.setdefault(a.asname or a.name, []).append(("from", node.level, node.module, a.name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.setdefault(node.name, []).append(("def",))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            dump = ast.dump(node)
            for n in ast.walk(node):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    out.setdefault(n.id, []).append(dump)
    return {k: tuple(v) for k, v in out.items()}


def _texts(root: Path, base: str, path: str) -> tuple[str, str | None, str]:
    """(``path``'s ``git diff -U0`` from ``base`` to the checkout, its text at
    ``base`` or None where it didn't exist, its text now: "" when it's gone)."""
    diff = git.out(["diff", "-U0", "--no-renames", base, "--", path], root)
    here = root / path
    new_text = here.read_text(encoding="utf-8") if here.is_file() else ""
    if not git.ok(["cat-file", "-e", f"{base}:{path}"], root):
        return diff, None, new_text
    return diff, git.run(["show", f"{base}:{path}"], root).stdout, new_text


def _module_statements_of(tree: ast.Module) -> dict[int, frozenset[str] | None]:
    """For each line of a module-level statement (not a function or class):
    the names an assignment or ``import`` binds; an empty set for the module
    docstring; None for anything else (a call, a block, a star import), whose
    reach isn't a set of names."""
    out: dict[int, frozenset[str] | None] = {}
    for i, node in enumerate(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        names: frozenset[str] | None
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound = [a.asname or a.name.split(".")[0] for a in node.names]
            names = None if "*" in bound else frozenset(bound)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            names = frozenset(n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
            names = names or None   # ``x.y = ...``, ``x[0] = ...``: changes something else
        elif i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            names = frozenset()
        else:
            names = None
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
            out[line] = names
    return out
