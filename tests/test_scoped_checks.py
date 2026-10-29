"""A worker's branch runs only the checks (and tests) its changes reach;
the supervisor's checkout always runs them in full."""
import json
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import autopilot, check_scope, codemap, gates, workspaces
from brindle.config import load_repo_config
from brindle.db import Agent

PYTEST = "uv run pytest -q"
OPTS = "uv run pytest -q -n auto -m 'not serial'"
SELECTS = "uv run pytest -q tests/"
MOD = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
TEST_F = "def helper():\n    return 1\n\n\ndef unlooked():\n    return 1\n\n\ndef test_f():\n    assert helper()\n"


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


OTHER = {"tests/test_other.py": "def test_o(): pass\n"}
NARROWED = f"{PYTEST} tests/test_other.py || [ $? -eq 5 ]"
DYNAMIC_IMPORTS = {
    "fromlist": "__import__('pkg', fromlist=['new'])\n",
    "dynamic-fromlist": "__import__('pkg', fromlist=names)\n",
    "relative-level": "__import__('new', globals(), locals(), [], 1)\n",
    "dynamic-level": "__import__('new', level=level)\n",
}


@pytest.mark.parametrize("where,cmd,changes,ran", [
    ("infer", PYTEST, {"src/pkg/mod.py": MOD.replace("return 2", "return 3"), **OTHER},
     [f"{PYTEST} tests/test_g.py tests/test_other.py || [ $? -eq 5 ]"]),
    ("infer", PYTEST, {"src/pkg/mod.py": MOD.split("\n\n\n", 1)[1]}, [f"{PYTEST} tests/test_f.py || [ $? -eq 5 ]"]),
    ("infer", PYTEST, {"tests/test_f.py": TEST_F.replace("return 1", "return 2", 1)},
     [f"{PYTEST} tests/test_f.py tests/test_g.py || [ $? -eq 5 ]"]),
    ("infer", PYTEST, {"tests/test_f.py": TEST_F.replace("return 1\n\n\ndef test_f", "return 2\n\n\ndef test_f")},
     [PYTEST]),   # graphify failed on unlooked()
    ("infer", PYTEST, {"src/pkg/new.py": "def h():\n    return 0\n",
                       "tests/test_other.py": "from importlib import import_module as load\nload(name)\n"},
     [PYTEST]),   # an unmapped module with an unresolved dynamic test import
    ("infer", PYTEST, {"src/pkg/pro/status.py": "X = 1\n",
                       "tests/test_other.py": "import pkg.pro.status\ndef test_new(): pass\n"},
     [f"{PYTEST} tests/test_direct.py tests/test_from.py tests/test_member.py tests/test_other.py || [ $? -eq 5 ]"]),
    ("fromlist", PYTEST, {"src/pkg/new.py": "X = 1\n"}, [PYTEST]),
    pytest.param("dynamic-fromlist", PYTEST, {"src/pkg/new.py": "X = 1\n"}, [PYTEST],
                 marks=pytest.mark.variant("review requested a nonliteral fromlist bypass")),
    pytest.param("relative-level", PYTEST, {"src/pkg/new.py": "X = 1\n"}, [PYTEST],
                 marks=pytest.mark.variant("review requested positional relative import arguments")),
    pytest.param("dynamic-level", PYTEST, {"src/pkg/new.py": "X = 1\n"}, [PYTEST],
                 marks=pytest.mark.variant("review requested a nonliteral relative import level")),
    ("no-graph", PYTEST, {"tests/test_f.py": TEST_F.replace("return 1", "return 2", 1)},
     [f"{PYTEST} tests/test_f.py tests/test_g.py || [ $? -eq 5 ]"]),
    pytest.param("infer", PYTEST, {"src/café.py": "X = 1\n"}, [PYTEST],
                 marks=pytest.mark.variant("git C-quotes a non-ASCII name without -z; it was once skipped")),
    ("infer", PYTEST, {"tests/conftest.py": "X = 2\n"}, [PYTEST]),
    ("infer", PYTEST, {"pyproject.toml": "[x]\n"}, [PYTEST]),
    ("infer", PYTEST, OTHER, [NARROWED]),
    ("infer", OPTS, OTHER, [f"{OPTS} tests/test_other.py || [ $? -eq 5 ]"]),
    ("infer", SELECTS, OTHER, [SELECTS]),   # names tests itself: runs in full
    ("infer", "make lint", OTHER, ["make lint"]),
    ("infer", PYTEST, {}, []),   # nothing changed: skipped
    ("infer", PYTEST, {"app/x.tsx": "x\n"}, []),   # no Python-relevant change: skipped
    ("infer", PYTEST, {"app/x.tsx": "x\n", "src/pkg/mod.py": MOD.replace("return 2", "return 3")},
     [f"{PYTEST} tests/test_g.py || [ $? -eq 5 ]"]),
    ("infer", PYTEST, {"uv.lock": "x\n"}, [PYTEST]),
    ("unset", PYTEST, {"src/pkg/mod.py": MOD.replace("return 2", "return 3")},
     [f"{PYTEST} tests/test_g.py || [ $? -eq 5 ]"]),
    ("off", PYTEST, OTHER, [PYTEST]),
    ("supervisor", PYTEST, OTHER, [PYTEST]),
], ids=["python-affected", "deleted-function", "test-helper", "lookup-failed", "fallback-unmapped", "unmapped-importers",
        "import-fromlist", "import-dynamic-fromlist", "import-relative-level", "import-dynamic-level", "unmapped-test-helper", "quoted-name",
        "fallback-conftest", "fallback-pyproject", "infer", "infer-options", "infer-selects", "other-command-changed_files_env", "empty-diff",
        "non-python-only", "non-python-plus-python", "lockfile", "unset-mode", "off", "supervisor"])
def test_scoped_checks(db, repo, tmp_path, monkeypatch, where, cmd, changes, ran):
    config = {"checks": [cmd], "scoped_checks": "off" if where == "off" else "infer"}
    if where == "unset":
        del config["scoped_checks"]
    write(repo, {".brindle/config.json": json.dumps(config), "src/pkg/mod.py": MOD, "docs/guide.md": "doc\n",
                 "src/pkg/__init__.py": "", "src/pkg/pro/__init__.py": "",
                 "app/src/view.ts": "export const v = 1\n", "tests/conftest.py": "X = 1\n",
                 "tests/test_direct.py": "import pkg.pro.status\n__import__('pkg.pro.status')\n",
                 "tests/test_from.py": "from pkg.pro import status\n",
                 "tests/test_member.py": "from pkg.pro.status import X\n",
                 "tests/test_unrelated.py": "from pkg.other import status\n",
                 "tests/test_f.py": TEST_F, "tests/test_g.py": "from test_f import helper\n"})
    if where in DYNAMIC_IMPORTS:
        write(repo, {"tests/test_dynamic.py": DYNAMIC_IMPORTS[where]})
    sh("git add -A && git commit -qm base", repo)
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"nodes": [
        {"id": "mod_f", "label": "f()", "source_file": "src/pkg/mod.py"},
        {"id": "mod_g", "label": "g()", "source_file": "src/pkg/mod.py"},
        {"id": "tf", "label": "test_f.py", "source_file": "tests/test_f.py"},
        {"id": "tf_helper", "label": "helper()", "source_file": "tests/test_f.py"},
        {"id": "tf_unlooked", "label": "unlooked()", "source_file": "tests/test_f.py"}]}))
    monkeypatch.setattr(codemap, "graph_path", lambda target: None if where == "no-graph" else graph)
    reach = {"mod_f": {"tests/test_f.py"}, "mod_g": {"tests/test_g.py"}, "tf": set(), "tf_helper": {"tests/test_g.py"},
             "tf_unlooked": None}
    monkeypatch.setattr(check_scope, "affected_tests", lambda g, node_id, cwd: reach[node_id])
    commands, envs = [], []
    monkeypatch.setattr(autopilot, "run_check", lambda cmd, cwd, env, timeout: (
        commands.append(cmd), envs.append(env.get("BRINDLE_CHANGED_FILES")), (True, ""))[2])

    if where != "supervisor":
        ws = workspaces.create(db, str(repo), "feat").workspace
        db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "done", "@0", None, time.time()))
    else:
        ws = workspaces.adopt_root(db, str(repo))
    write(Path(ws.path), changes)
    sh("git add -A && git commit -qm work --allow-empty", Path(ws.path))

    cfg = load_repo_config(ws.repo_root)
    assert all(gates.run_checked(db, ws, cmd, {}, 30)[0] for cmd in cfg.checks)
    assert commands == ran
    assert envs == [None if where == "supervisor" else "\n".join(sorted(changes))] * len(ran)   # the real gate dispatch sets it
