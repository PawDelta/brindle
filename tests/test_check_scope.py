"""check_scope narrows a pytest check to the tests that import a change,
however they import it, instead of falling back to the full suite."""
import json
import shlex
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import sh
from brindle import autopilot, check_scope, codemap, gates, session_sync, workspaces
from brindle.db import Agent

PYTEST = "uv run pytest -q"
SOURCES = {
    "src/brindle/__init__.py": "", "src/brindle/daemon/__init__.py": "",
    "src/brindle/agents.py": "from brindle import low\n\n\ndef f():\n    return 1\n",
    "src/brindle/low.py": "X = 1\n", "src/brindle/daemon/routes_read.py": "X = 1\n",
    "src/brindle/loader.py": "import importlib\n\n\ndef load(name):\n    return importlib.import_module(name)\n",
}
TESTS = {
    "tests/test_agents.py": "from brindle import agents\n",
    "tests/test_member.py": "from brindle.agents import f\n",
    "tests/test_patch.py": "def test_p(monkeypatch):\n    monkeypatch.setattr('brindle.agents.f', None)\n",
    "tests/test_dotted.py": "import brindle.daemon.routes_read\n",
    "tests/test_lazy.py": "def test_l():\n    from brindle.daemon import routes_read\n",
    "tests/test_attr.py": "import brindle.daemon\n\n\ndef test_a():\n    brindle.daemon.routes_read.X\n",
    "tests/test_alias.py": "import brindle.daemon as d\n\n\ndef test_a():\n    d.routes_read.X\n",
    "tests/test_bound.py": "from brindle import daemon\n\n\ndef test_b():\n    daemon.routes_read.X\n",
    "tests/test_any.py": "import importlib\n\n\ndef test_any(name):\n    importlib.import_module(name)\n",   # may be anything
    # A real repo has one of these; it once made every module unresolved.
    "tests/test_dynamic.py": "__import__('brindle.other', fromlist=names)\n",
    "tests/test_unrelated.py": "import os\n",
    "tests/test_loader.py": "from brindle import loader\n",   # loader may import low, but so may anything
}
CAPPED = check_scope.CAP_NOTE.format(max=2, tests="tests/test_agents.py")


def run(tests):
    return f"{PYTEST} {' '.join(tests)}"


@pytest.mark.parametrize("changed,max_files,ran", [
    ("src/brindle/agents.py", 2, f"printf '%s\\n' {shlex.quote(CAPPED)}; {run(['tests/test_agents.py'])}"),
], ids=["capped"])
def test_imports_scope_the_check(repo, tmp_path, monkeypatch, changed, max_files, ran):
    for name, text in {**SOURCES, **TESTS}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    sh("git add -A && git commit -qm base", repo)
    base = sh("git rev-parse HEAD", repo)
    (repo / changed).write_text((repo / changed).read_text().replace("1", "2"))
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"nodes": [{"id": "agents_f", "label": "f()", "source_file": "src/brindle/agents.py"}]}))
    monkeypatch.setattr(codemap, "graph_path", lambda ws: graph)
    monkeypatch.setattr(check_scope, "affected_tests", lambda g, node_id, cwd: set())
    ws = SimpleNamespace(path=str(repo))

    assert check_scope.command(ws, PYTEST, check_scope.CheckScope(), (base, [changed]), max_files) == ran


def test_tests_pytest_would_not_collect_are_not_named(repo, monkeypatch):
    """Naming a path bypasses norecursedirs and testpaths: parked tests under dev/ must not run."""
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\nnorecursedirs = ["dev", ".*"]\n')
    for name in ("dev/x/test_a.py", "tests/test_b.py"):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("def test_t():\n    pass\n")
    sh("git add -A && git commit -qm base", repo)
    base = sh("git rev-parse HEAD", repo)
    monkeypatch.setattr(codemap, "graph_path", lambda ws: None)
    ws = SimpleNamespace(path=str(repo))
    scope = check_scope.CheckScope()

    both = check_scope.command(ws, PYTEST, scope, (base, ["dev/x/test_a.py", "tests/test_b.py"]))
    assert both == run(["tests/test_b.py"])
    assert check_scope.command(ws, PYTEST, scope, (base, ["dev/x/test_a.py"])) is None   # nothing left: a pass


HUB = ("from math import floor as operation\n\nLIMIT = 1\n\n\ndef a():\n    return LIMIT\n\n\n"
       "def b():\n    return operation(2.5)\n\n\ndef helper():\n    return 0\n")
# Every test imports the hub module; each runs only part of it (by line, as coverage records it): helper() none.
RAN = {"tests/test_a.py::test_a": [7], "tests/test_b.py::test_b": [11], "tests/test_c.py::test_c": []}
HUB_TESTS = {"tests/test_a.py": "def test_a():\n    assert hub.a()\n", "tests/test_b.py": "def test_b():\n    assert hub.b()\n",
             "tests/test_c.py": "def test_c():\n    assert hub\n"}


def fake_coverage_run(commands):
    """autopilot.run_check: records the command and, for a run with per-test
    coverage, the data pytest-cov would leave at COVERAGE_FILE."""
    from coverage import CoverageData

    def run(cmd, cwd, env, timeout):
        commands.append(cmd)
        if "--cov-context=test" in cmd:
            data = CoverageData(env["COVERAGE_FILE"])
            hub = str(Path(cwd).resolve() / "src/pkg/hub.py")
            data.set_context("")
            data.add_lines({hub: [1, 3, 6, 10, 14]})   # the import, during collection
            for test, lines in RAN.items():
                data.set_context(f"{test}|run")
                data.add_lines({hub: lines})
            data.write()
        return True, ""
    return run


@pytest.mark.parametrize("where,drift,old,new,ran", [
    pytest.param("worker", None, "return LIMIT\n", "return LIMIT + 1\n", "tests/test_a.py", marks=pytest.mark.variant(
        "A function-body change on a worker's branch must run only the tests that executed it, not every importer")),
    pytest.param("merge-main", None, "operation(2.5)", "operation(3.5)", "tests/test_b.py", marks=pytest.mark.variant(
        "The session checkout's check after a merge of main must scope by the map too, from the pre-merge commit")),
    # A module-level value or import runs the tests of the code reading it, though they never name it
    # (a module-level assignment the same way: see the reordered rows below).
    ("worker", None, "floor as", "ceil as", "tests/test_b.py"),
    # Since the map, main made a() call helper(): a change to helper() reaches test_a through a().
    pytest.param("worker", ("return LIMIT\n", "return LIMIT + helper()\n"), "return 0", "return 1", "tests/test_a.py",
                 marks=pytest.mark.variant("A map older than the code must follow calls added since, or it misses "
                                           "the tests reaching the change through them")),
], ids=["worker", "merge-main", "import", "outdated-map"])
def test_coverage_map_selects_the_tests_that_ran_the_changed_function(db, repo, monkeypatch, where, drift, old, new,
                                                                       ran):
    """A full run records which tests executed each function; a change to one
    function of a module every test imports then runs only the tests that
    executed it, on a worker's branch and after a merge of main alike."""
    config = {"checks": [PYTEST], "scoped_checks": "coverage"}
    files = {".brindle/config.json": json.dumps(config), "src/pkg/__init__.py": "", "src/pkg/hub.py": HUB,
             **{name: "from pkg import hub\n\n\n" + text for name, text in HUB_TESTS.items()}}
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    sh("git add -A && git commit -qm base", repo)
    commands = []
    monkeypatch.setattr(autopilot, "run_check", fake_coverage_run(commands))
    root = workspaces.adopt_root(db, str(repo))
    assert gates.run_checked(db, root, PYTEST, {}, 30)[0]   # the full run records the map
    before = sh("git rev-parse HEAD", repo)
    hub = HUB.replace(*drift) if drift else HUB
    if drift:
        (repo / "src/pkg/hub.py").write_text(hub)
        sh("git commit -qam drift", repo)

    if where == "worker":
        ws = workspaces.create(db, str(repo), "feat").workspace
        db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "done", "@0", None, time.time()))
        (Path(ws.path) / "src/pkg/hub.py").write_text(hub.replace(old, new))
        sh("git commit -qam work", Path(ws.path))
        assert gates.run_checked(db, ws, PYTEST, {}, 30)[0]
    else:   # main brought in a change to b(); the session checkout's check covers just that
        (repo / "src/pkg/hub.py").write_text(HUB.replace(old, new))
        sh("git commit -qam 'merge origin/main'", repo)
        monkeypatch.setattr(session_sync, "_tell", lambda *a: None)
        session_sync.run_checks(db, root.id, before)

    assert "--cov-context=test" in commands[0]
    assert commands[1:] == [f"{PYTEST} {ran}"]


RUN_Y = "\n\n\ndef run():\n    return Y\n"


@pytest.mark.parametrize("files,reader,edit", [
    # Read on import in a class body, through another member; run() reads the class attribute later.
    ({"pkg/hub.py": "LIMIT = 1\n\n\nclass Settings:\n    threshold = LIMIT\n    doubled = threshold * 2\n\n\n"
                    "def run():\n    return Settings.doubled\n"},
     "pkg/hub.py", ("pkg/hub.py", "LIMIT = 1", "LIMIT = 2")),
    # Re-exported by a star import (oddly spaced) that never spells the name.
    ({"pkg/hub.py": "LIMIT = 1\n", "pkg/api.py": "from pkg.hub import  *\n",
      "pkg/consumer.py": "from pkg.api import LIMIT\n\n\ndef run():\n    return LIMIT\n"},
     "pkg/consumer.py", ("pkg/hub.py", "LIMIT = 1", "LIMIT = 2")),
    # Only the order changes: Y now takes X's second value.
    ({"pkg/hub.py": "X = 1\nY = X\nX = 2" + RUN_Y}, "pkg/hub.py", ("pkg/hub.py", "Y = X\nX = 2", "X = 2\nY = X")),
    # Only the order of two imports of one name changes: Y is now floor, read when run() runs.
    ({"pkg/hub.py": "from math import floor as Y\nfrom math import ceil as Y" + RUN_Y}, "pkg/hub.py",
     ("pkg/hub.py", "floor as Y\nfrom math import ceil", "ceil as Y\nfrom math import floor")),
], ids=["class-body", "star-reexport", "reordered", "reordered-imports"])
def test_module_level_change_reaches_the_tests_of_its_readers(repo, files, reader, edit):
    """The one test that ran ``run()``, which reads the changed value only
    indirectly, is selected; nothing else ran anything."""
    from brindle import coverage_map

    for name, text in {"pkg/__init__.py": "", **files}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    sh("git add -A && git commit -qm base", repo)
    known = {name: {q: [] for q in coverage_map.line_scopes(text)} for name, text in files.items()}
    known[reader]["run"] = [0]
    data = {"commit": sh("git rev-parse HEAD", repo), "tests": ["tests/test_run.py"], "files": known}
    path, old, new = edit
    (repo / path).write_text((repo / path).read_text().replace(old, new))

    assert coverage_map.targets(repo, data, coverage_map.changed_since(repo, data)) == {"tests/test_run.py"}


DB_PY = ('SCHEMA = """\nCREATE TABLE IF NOT EXISTS items (\n    x TEXT CHECK (x != \'a  b\')\n);\n"""\n\n\n'
         'def open_db():\n    return SCHEMA\n')
SCHEMA_TESTS = {"tests/test_db_schema.py": "def test_s(): pass\n", "tests/test_jobs.py": "Q = 'SELECT * FROM jobs'\n",
                "tests/test_items.py": "Q = 'SELECT x FROM items'\n", "tests/test_x.py": "def test_x(): pass\n",
                "tests/test_y.py": "def test_y(): pass\n"}


@pytest.mark.parametrize("old,new,ran", [
    ('\n"""', '\nCREATE TABLE IF NOT EXISTS jobs (id TEXT);\n"""', {"tests/test_db_schema.py", "tests/test_jobs.py"}),
    ('\n"""', '\nALTER TABLE items ADD COLUMN y TEXT;\n"""', {"tests/test_db_schema.py", "tests/test_items.py"}),
    pytest.param("x TEXT", "x TEXT NOT NULL", {"tests/test_x.py", "tests/test_y.py"},   # every test that opens a DB
                 marks=pytest.mark.variant("A changed (not added) constraint must fall back to every test opening "
                                           "a DB, not be scoped like an addition")),
    # A quoted literal of an existing constraint changes (only in its spacing) alongside an added table.
    pytest.param("'a  b')\n);\n", "'a b')\n);\nCREATE TABLE IF NOT EXISTS jobs (id TEXT);\n",
                 {"tests/test_x.py", "tests/test_y.py"},
                 marks=pytest.mark.variant("Whitespace normalisation must not hide a change inside a quoted literal "
                                           "behind an added table")),
], ids=["added-table", "added-column", "changed-constraint", "changed-literal-and-table"])
def test_added_ddl_runs_the_tests_of_its_tables(repo, old, new, ran):
    """A schema string that only gains a table or a column runs the tests
    naming it and the schema tests, not every test that opens a database."""
    from brindle import coverage_map

    for name, text in {"pkg/__init__.py": "", "pkg/db.py": DB_PY, **SCHEMA_TESTS}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    sh("git add -A && git commit -qm base", repo)
    known = {"pkg/db.py": {q: [] for q in coverage_map.line_scopes(DB_PY)}}
    known["pkg/db.py"]["open_db"] = [0, 1]
    data = {"commit": sh("git rev-parse HEAD", repo), "tests": ["tests/test_x.py", "tests/test_y.py"], "files": known}
    (repo / "pkg/db.py").write_text(DB_PY.replace(old, new, 1))

    assert coverage_map.targets(repo, data, coverage_map.changed_since(repo, data)) == ran


CONFTEST = ("import pytest\nimport pytest_asyncio\nimport wrapt\nfrom pytest import fixture as fx\nfrom .helpers import fixture as own_fixture\n\nIGNORED = ['test_off.py']\ncollect_ignore = IGNORED\n\n\n"
            "@pytest.fixture\ndef db():\n    return 1\n\n\n"
            "@pytest.fixture(scope='session')\ndef cache():\n    return 1\n\n\n"
            "@pytest.fixture\ndef client(cache):\n    return cache\n\n\n"
            "@pytest.fixture(scope='session', name='store')\ndef make_store():\n    return 1\n\n\n"
            "@fx(scope='session', name='blob')\ndef make_blob():\n    return 1\n\n\n"
            "@pytest_asyncio.fixture(scope='session', name='conn')\nasync def make_conn():\n    return 1\n\n\n"
            "@pytest_asyncio.fixture(autouse=True)\nasync def loop_guard():\n    return 1\n\n\n"
            "@wrapt.decorator\ndef flaky():\n    return 1\n\n\n"
            "@own_fixture\ndef backing():\n    return 1\n\n\n"
            "@pytest.fixture\ndef wrapped(flaky):\n    return 1\n\n\n"
            "@pytest.fixture(autouse=True)\ndef clean():\n    yield 1\n\n\n"
            "def pytest_collection_modifyitems(items):\n    return None\n")
FIXTURE_TESTS = {"tests/test_db.py": "def test_d(db):\n    assert db\n", "tests/test_cache_a.py": "def test_a(cache): pass\n",
                 "tests/test_cache_b.py": "def test_b(cache): pass\n", "tests/test_client.py": "def test_c(client): pass\n",
                 "tests/test_store.py": "def test_s(store): pass\n", "tests/test_store_b.py": "def test_t(store): pass\n",
                 "tests/test_blob_a.py": "def test_ba(blob): pass\n", "tests/test_blob_b.py": "def test_bb(blob): pass\n",
                 "tests/test_conn_a.py": "def test_ca(conn): pass\n", "tests/test_conn_b.py": "def test_cb(conn): pass\n",
                 "tests/sub/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef sub_client(cache):\n    return cache\n",
                 "tests/sub/test_sa.py": "def test_sa(sub_client): pass\n",
                 "tests/sub/test_sb.py": "def test_sb(sub_client): pass\n",
                 "tests/test_plain.py": "def test_p(): pass\n",
                 "tests/test_off.py": "def test_off():\n    assert False\n"}


def _fixture_row(old, new, ran, why):
    return pytest.param(old, new, ran, marks=pytest.mark.variant(why))


@pytest.mark.parametrize("old,new,ran", [
    _fixture_row("def db():\n    return 1", "def db():\n    return 2", {"tests/test_db.py"},
                 "A function-scoped fixture change must run only its requesters, not the whole suite"),
    # The map credits a session fixture's setup to the first test only: the others requesting it, directly or
    # through another fixture (here and in a conftest further down), run too.
    _fixture_row("def cache():\n    return 1", "def cache():\n    return 2",
                 {"tests/test_cache_a.py", "tests/test_cache_b.py", "tests/test_client.py", "tests/sub/test_sa.py",
                  "tests/sub/test_sb.py"},
                 "A session fixture's later and transitive requesters must run though the map credits only the first"),
    # Requested by its name=, not its function's name.
    _fixture_row("def make_store():\n    return 1", "def make_store():\n    return 2",
                 {"tests/test_store.py", "tests/test_store_b.py"},
                 "A fixture renamed by name= must select the tests requesting that name, not its function name"),
    # The same through an aliased decorator.
    _fixture_row("def make_blob():\n    return 1", "def make_blob():\n    return 2",
                 {"tests/test_blob_a.py", "tests/test_blob_b.py"},
                 "A fixture decorator imported under an alias must still be recognised as a fixture"),
    # A plugin's fixture, requested by its name=.
    _fixture_row("def make_conn():\n    return 1", "def make_conn():\n    return 2",
                 {"tests/test_conn_a.py", "tests/test_conn_b.py"},
                 "A plugin's fixture decorator (pytest_asyncio) must be recognised as a fixture"),
    _fixture_row("yield 1", "yield 2", None,   # autouse: every test
                 "An autouse fixture change must run every test, not only those naming it"),
    _fixture_row("def loop_guard():\n    return 1", "def loop_guard():\n    return 2", None,   # a plugin's autouse
                 "A plugin fixture's autouse= must run every test, as pytest's own autouse does"),
    _fixture_row("def flaky():\n    return 1", "def flaky():\n    return 2", None,
                 "A decorator brindle can't tell isn't a fixture must fail closed to the full suite"),
    # A local wrapper named fixture may set its own name= or autouse.
    _fixture_row("def backing():\n    return 1", "def backing():\n    return 2", None,
                 "A local wrapper named fixture must fail closed: it may set its own name= or autouse"),
    ("items):\n    return None", "items):\n    items.clear()", None),   # a hook: what is collected at all
    ("['test_off.py']", "[]", None),   # a collection setting, through a name: test_off.py is collected now
], ids=["fixture", "session-fixture", "named-fixture", "aliased-fixture", "plugin-fixture", "autouse", "plugin-autouse",
        "unknown-decorator", "local-fixture-wrapper", "hook", "collect-ignore"])
def test_a_conftest_change_runs_the_tests_using_its_fixtures(repo, monkeypatch, old, new, ran):
    """With per-test coverage a conftest change no longer runs everything."""
    from brindle import coverage_map

    for name, text in {"tests/conftest.py": CONFTEST, **FIXTURE_TESTS}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    sh("git add -A && git commit -qm base", repo)
    known = {"tests/conftest.py": {q: [] for q in coverage_map.line_scopes(CONFTEST)}}
    # cache's and store's setups are credited to the first test that ran them; clean ran for every test.
    known["tests/conftest.py"].update(db=[0], cache=[1], client=[3], make_store=[4], make_blob=[6], make_conn=[7],
                                      clean=[0, 1, 2, 3, 4, 5, 6, 7], loop_guard=[0, 1, 2, 3, 4, 5, 6, 7])
    tests = ["tests/test_db.py", "tests/test_cache_a.py", "tests/test_cache_b.py", "tests/test_client.py",
             "tests/test_store.py", "tests/test_plain.py", "tests/test_blob_a.py", "tests/test_conn_a.py"]
    base = sh("git rev-parse HEAD", repo)
    monkeypatch.setattr(coverage_map, "load", lambda root, check: {"commit": base, "tests": tests, "files": known})
    (repo / "tests/conftest.py").write_text(CONFTEST.replace(old, new))
    ws = SimpleNamespace(path=str(repo), repo_root=str(repo))

    run = check_scope.command(ws, PYTEST, check_scope.inferred(PYTEST, "coverage"), (base, ["tests/conftest.py"]))
    if ran is None:
        assert run.startswith("printf") and run.endswith(f"; {PYTEST}")   # in full, saying why
    else:
        assert run == f"{PYTEST} {' '.join(sorted(ran))}"


def test_a_script_changed_since_the_map_runs_the_tests_that_run_it(repo, monkeypatch):
    """Main changed a script after the map was recorded and the branch changes
    only another module: the script's tests (it runs as a child process, so
    the map never saw them) run as well as those of the branch's change."""
    from brindle import coverage_map

    files = {"pkg/__init__.py": "", "pkg/mod.py": "def f():\n    return 1\n", "scripts/build.py": "X = 1\n",
             "tests/test_driver.py": "def test_d(): pass\n",
             "tests/test_runs_build.py": "ARGV = ['python', 'scripts/build.py']\n"}
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    sh("git add -A && git commit -qm base", repo)
    known = {name: {q: [] for q in coverage_map.line_scopes(text)} for name, text in files.items() if "/test_" not in name}
    known["pkg/mod.py"]["f"] = [0]
    data = {"commit": sh("git rev-parse HEAD", repo), "tests": ["tests/test_driver.py"], "files": known}
    monkeypatch.setattr(coverage_map, "load", lambda root, check: data)
    (repo / "scripts/build.py").write_text("X = 2\n")
    sh("git commit -qam 'main changed the script'", repo)
    base = sh("git rev-parse HEAD", repo)
    (repo / "pkg/mod.py").write_text("def f():\n    return 2\n")
    ws = SimpleNamespace(path=str(repo), repo_root=str(repo))

    run = check_scope.command(ws, PYTEST, check_scope.CheckScope(coverage=True), (base, ["pkg/mod.py"]))
    assert run == f"{PYTEST} tests/test_driver.py tests/test_runs_build.py"


def test_a_hub_change_runs_only_the_tests_most_specific_to_it(repo, monkeypatch):
    """A change to a function nearly every test passes through runs at most
    ``max_files`` tests: the branch's own and namesake ones, then those whose
    coverage is mostly the changed code, not the 100 the map credits."""
    from brindle import coverage_map

    hub = "def hub():\n    return 1\n"
    own = "def test_o():\n    assert 1\n"
    specific = [f"tests/test_s{i}.py" for i in range(10)]
    generic = [f"tests/test_g{i:02}.py" for i in range(88)]
    names = ["tests/test_hub.py", "tests/test_own.py", *specific, *generic]
    files = {"pkg/__init__.py": "", "pkg/hub.py": hub, **{t: "def test_x(): pass\n" for t in names},
             "tests/test_own.py": own}
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    sh("git add -A && git commit -qm base", repo)
    everyone = list(range(len(names)))
    broad = [i for i, t in enumerate(names) if t not in specific]   # these run much else too
    known = {"pkg/hub.py": {"": everyone, "hub": everyone},
             "pkg/big.py": {f"f{i}": broad for i in range(10)},
             "tests/test_own.py": {q: [1] for q in coverage_map.line_scopes(own)}}
    base = sh("git rev-parse HEAD", repo)
    monkeypatch.setattr(coverage_map, "load", lambda root, check: {"commit": base, "tests": names, "files": known})
    (repo / "pkg/hub.py").write_text(hub.replace("1", "2"))
    (repo / "tests/test_own.py").write_text(own.replace("1", "2"))
    ws = SimpleNamespace(path=str(repo), repo_root=str(repo))

    run = check_scope.command(ws, PYTEST, check_scope.CheckScope(coverage=True),
                              (base, ["pkg/hub.py", "tests/test_own.py"]), 40)
    ran = shlex.split(run.rsplit(f"; {PYTEST}", 1)[-1])
    assert run.startswith("printf") and len(ran) == 40
    assert {"tests/test_hub.py", "tests/test_own.py", *specific} <= set(ran)
