"""check_scope narrows a pytest check to the tests that import a change,
however they import it, instead of falling back to the full suite."""
import json
import shlex
from types import SimpleNamespace

import pytest

from conftest import sh
from brindle import check_scope, codemap

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
