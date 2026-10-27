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


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


@pytest.mark.parametrize("where,changes,ran", [
    ("worker", {"docs/guide.md": "more\n"}, []),
    ("worker", {"app/src/view.ts": "export const v = 2\n"}, ["cd app && npx vitest related --run src/view.ts"]),
    ("worker", {"src/pkg/mod.py": MOD.replace("return 2", "return 3"), "tests/test_other.py": "def test_o(): pass\n"},
     [f"{PYTEST} tests/test_g.py tests/test_other.py || [ $? -eq 5 ]"]),
    ("worker", {"src/pkg/new.py": "def h():\n    return 0\n"}, [PYTEST]),   # not in the graph
    ("worker", {"tests/conftest.py": "X = 2\n"}, [PYTEST]),
    ("supervisor", {"docs/guide.md": "more\n"}, [PYTEST, APP]),
], ids=["docs-only", "app-only", "python-affected", "fallback-unmapped", "fallback-conftest", "supervisor"])
def test_scoped_checks(db, repo, tmp_path, monkeypatch, where, changes, ran):
    write(repo, {".brindle/config.json": json.dumps(CONFIG), "src/pkg/mod.py": MOD, "docs/guide.md": "doc\n",
                 "app/src/view.ts": "export const v = 1\n", "tests/conftest.py": "X = 1\n",
                 "tests/test_f.py": "def test_f(): pass\n", "tests/test_g.py": "def test_g(): pass\n"})
    sh("git add -A && git commit -qm base", repo)
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"nodes": [
        {"id": "mod_f", "label": "f()", "source_file": "src/pkg/mod.py"},
        {"id": "mod_g", "label": "g()", "source_file": "src/pkg/mod.py"}]}))
    monkeypatch.setattr(codemap, "graph_path", lambda target: graph)
    reach = {"mod_f": {"tests/test_f.py"}, "mod_g": {"tests/test_g.py"}}
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
