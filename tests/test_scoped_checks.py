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
APP = "cd app && npx vitest run"
CONFIG = {"checks": [
    {"run": PYTEST, "paths": ["src/**", "tests/**"], "affected": "pytest", "full_when": ["**/conftest.py"]},
    {"run": APP, "paths": ["app/**"], "affected": "files", "relative_to": "app",
     "scoped": "cd app && npx vitest related --run {files}"},
]}
MOD = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
TEST_F = "def helper():\n    return 1\n\n\ndef unlooked():\n    return 1\n\n\ndef test_f():\n    assert helper()\n"


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


@pytest.mark.parametrize("where,changes,ran", [
    ("worker", {"docs/guide.md": "more\n"}, []),
    ("worker", {"app/src/view.ts": "export const v = 2\n"}, ["cd app && npx vitest related --run src/view.ts"]),
    ("worker", {"src/pkg/mod.py": MOD.replace("return 2", "return 3"), "tests/test_other.py": "def test_o(): pass\n"},
     [f"{PYTEST} tests/test_g.py tests/test_other.py || [ $? -eq 5 ]"]),
    ("worker", {"src/pkg/mod.py": MOD.split("\n\n\n", 1)[1]}, [f"{PYTEST} tests/test_f.py || [ $? -eq 5 ]"]),
    ("worker", {"tests/test_f.py": TEST_F.replace("return 1", "return 2", 1)},
     [f"{PYTEST} tests/test_f.py tests/test_g.py || [ $? -eq 5 ]"]),
    ("worker", {"tests/test_f.py": TEST_F.replace("return 1\n\n\ndef test_f", "return 2\n\n\ndef test_f")},
     [PYTEST]),   # graphify failed on unlooked()
    ("worker", {"src/pkg/new.py": "def h():\n    return 0\n"}, [PYTEST]),   # not in the graph
    pytest.param("worker", {"src/pkg/café.py": "X = 1\n"}, [PYTEST],
                 marks=pytest.mark.variant("git C-quotes a non-ASCII name without -z; it was once skipped")),
    ("worker", {"tests/conftest.py": "X = 2\n"}, [PYTEST]),
    ("supervisor", {"docs/guide.md": "more\n"}, [PYTEST, APP]),
    ("worker-strings", {"app/src/view.ts": "export const v = 2\n"}, ["cd app && npx vitest related --run src/view.ts"]),
], ids=["docs-only", "app-only", "python-affected", "deleted-function", "test-helper", "lookup-failed",
        "fallback-unmapped", "quoted-name", "fallback-conftest", "supervisor", "strings-and-check-scopes"])
def test_scoped_checks(db, repo, tmp_path, monkeypatch, where, changes, ran):
    config = CONFIG
    if where == "worker-strings":   # the form older engines can run: plain commands, scopes beside them
        config = {"checks": [c["run"] for c in CONFIG["checks"]],
                  "check_scopes": {c["run"]: {k: v for k, v in c.items() if k != "run"} for c in CONFIG["checks"]}}
        assert all(isinstance(c, str) for c in config["checks"])
        where = "worker"
    write(repo, {".brindle/config.json": json.dumps(config), "src/pkg/mod.py": MOD, "docs/guide.md": "doc\n",
                 "app/src/view.ts": "export const v = 1\n", "tests/conftest.py": "X = 1\n",
                 "tests/test_f.py": TEST_F, "tests/test_g.py": "from test_f import helper\n"})
    sh("git add -A && git commit -qm base", repo)
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"nodes": [
        {"id": "mod_f", "label": "f()", "source_file": "src/pkg/mod.py"},
        {"id": "mod_g", "label": "g()", "source_file": "src/pkg/mod.py"},
        {"id": "tf", "label": "test_f.py", "source_file": "tests/test_f.py"},
        {"id": "tf_helper", "label": "helper()", "source_file": "tests/test_f.py"},
        {"id": "tf_unlooked", "label": "unlooked()", "source_file": "tests/test_f.py"}]}))
    monkeypatch.setattr(codemap, "graph_path", lambda target: graph)
    reach = {"mod_f": {"tests/test_f.py"}, "mod_g": {"tests/test_g.py"}, "tf": set(), "tf_helper": {"tests/test_g.py"},
             "tf_unlooked": None}
    monkeypatch.setattr(check_scope, "affected_tests", lambda g, node_id, cwd: reach[node_id])
    commands = []
    monkeypatch.setattr(autopilot, "run_check", lambda cmd, cwd, env, timeout: (commands.append(cmd), (True, ""))[1])

    if where == "worker":
        ws = workspaces.create(db, str(repo), "feat").workspace
        db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "done", "@0", None, time.time()))
    else:
        ws = workspaces.adopt_root(db, str(repo))
    write(Path(ws.path), changes)
    sh("git add -A && git commit -qm work", Path(ws.path))

    cfg = load_repo_config(ws.repo_root)
    assert all(gates.run_checked(db, ws, cmd, {}, 30)[0] for cmd in cfg.checks)
    assert commands == ran
