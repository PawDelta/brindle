"""A worker's branch runs only the checks (and tests) its changes reach;
the supervisor's checkout always runs them in full."""
import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import autopilot, check_scope, codemap, gates, git, workspaces
from brindle.config import load_repo_config
from brindle.db import Agent

PYTEST = "uv run pytest -q"
SELECTS = "uv run pytest -q tests/"
MOD = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
TEST_F = "def helper():\n    return 1\n\n\ndef unlooked():\n    return 1\n\n\ndef test_f():\n    assert helper()\n"


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


OTHER = {"tests/test_other.py": "def test_o(): pass\n"}
CAPPED = ["tests/test_mod.py", "tests/test_mod_extra.py", "tests/test_other.py"]
CAPPED_NOTE = check_scope.CAP_NOTE.format(max=1, tests=" ".join(CAPPED))
EMPTY_NOTE = check_scope.FULL_NOTE.format(
    why="the change reaches more tests than scoped_checks_max_files and none is its own or a namesake")
LONELY_NOTE = check_scope.FULL_NOTE.format(why="no test reaches the changed files and none is named after them")
BOTH = {"src/pkg/mod.py": MOD.replace("return 1", "return 3").replace("return 2", "return 4")}
DYNAMIC = f"{PYTEST} tests/test_dynamic.py"   # an unresolvable import runs its own file
CAFE_NOTE = check_scope.FULL_NOTE.format(why="src/café.py is not in a package, so no import can name it")
DYNAMIC_IMPORTS = {
    "alias-loader": "from importlib import import_module as load\nload(name)\n",
    "fromlist": "__import__('pkg', fromlist=['new'])\n",
    "dynamic-fromlist": "__import__('pkg', fromlist=names)\n",
    "relative-level": "__import__('new', globals(), locals(), [], 1)\n",
    "dynamic-level": "__import__('new', level=level)\n",
}


@pytest.mark.parametrize("where,cmd,changes,ran", [
    ("infer", PYTEST, {"src/pkg/mod.py": MOD.split("\n\n\n", 1)[1]}, [f"{PYTEST} tests/test_f.py"]),
    ("infer", PYTEST, {"tests/test_f.py": TEST_F.replace("return 1\n\n\ndef test_f", "return 2\n\n\ndef test_f")},
     [f"{PYTEST} tests/test_f.py tests/test_g.py"]),   # graphify failed on unlooked(): importers
    ("alias-loader", PYTEST, {"src/pkg/new.py": "def h():\n    return 0\n"}, [DYNAMIC]),   # load(name) may be anything
    ("fromlist", PYTEST, {"src/pkg/new.py": "X = 1\n"}, [DYNAMIC]),
    pytest.param("dynamic-fromlist", PYTEST, {"src/pkg/new.py": "X = 1\n"}, [DYNAMIC],
                 marks=pytest.mark.variant("review requested a nonliteral fromlist bypass")),
    pytest.param("relative-level", PYTEST, {"src/pkg/new.py": "X = 1\n"}, [DYNAMIC],
                 marks=pytest.mark.variant("review requested positional relative import arguments")),
    pytest.param("dynamic-level", PYTEST, {"src/pkg/new.py": "X = 1\n"}, [DYNAMIC],
                 marks=pytest.mark.variant("review requested a nonliteral relative import level")),
    pytest.param("infer", PYTEST, {"src/café.py": "X = 1\n"}, [f"printf '%s\\n' {shlex.quote(CAFE_NOTE)}; {PYTEST}"],
                 marks=pytest.mark.variant("git C-quotes a non-ASCII name without -z; it was once skipped")),
    ("infer", SELECTS, OTHER, [SELECTS]),   # names tests itself: runs in full
    ("infer", PYTEST, {}, []),   # nothing changed: skipped
    ("infer", PYTEST, {"app/x.tsx": "x\n"}, []),   # no Python-relevant change: skipped
    ("infer", PYTEST, {"uv.lock": "x\n"}, [PYTEST]),
    ("cap", PYTEST, {"src/pkg/mod.py": MOD.replace("return 1", "return 3"), **OTHER},
     [f"printf '%s\\n' {shlex.quote(CAPPED_NOTE)}; {PYTEST} {' '.join(CAPPED)}"]),   # test_f dropped
    ("cap-empty", PYTEST, BOTH, [f"printf '%s\\n' {shlex.quote(EMPTY_NOTE)}; {PYTEST}"]),   # no own or namesake test: full
    ("infer", PYTEST, {"src/pkg/lonely.py": "X = 1\n"}, [f"printf '%s\\n' {shlex.quote(LONELY_NOTE)}; {PYTEST}"]),   # no importer
    ("cap-pr", PYTEST, {"src/pkg/mod.py": MOD.replace("return 1", "return 3"), **OTHER}, [PYTEST]),
], ids=["deleted-function", "lookup-failed", "fallback-unmapped",
        "import-fromlist", "import-dynamic-fromlist", "import-relative-level", "import-dynamic-level", "quoted-name",
        "infer-selects", "empty-diff", "non-python-only", "lockfile", "cap", "cap-empty", "no-importers", "cap-pr"])
def test_scoped_checks(db, repo, tmp_path, monkeypatch, where, cmd, changes, ran):
    config = {"checks": [cmd], "scoped_checks": "off" if where == "off" else "infer"}
    if where == "unset":
        del config["scoped_checks"]
    if where.startswith("cap"):
        config["scoped_checks_max_files"] = 0 if where == "cap-none" else 1
        if where != "cap-empty":   # the namesake tests of mod.py; one test per `_*` and plain name
            write(repo, {"tests/test_mod.py": "def test_m(): pass\n", "tests/test_mod_extra.py": "def test_e(): pass\n"})
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
    if where == "cap-pr":   # the pre-PR run on a worker's workspace never narrows
        with gates.pr_checked(db, ws):
            pass
    else:
        assert all(gates.run_checked(db, ws, cmd, {}, 30)[0] for cmd in cfg.checks)
    assert commands == ran
    full = where in ("supervisor", "cap-pr")
    assert envs == [None if full else "\n".join(sorted(changes))] * len(ran)   # the real gate dispatch sets it


def test_empty_namesake_selection_fails_and_is_not_cached(db, repo, monkeypatch):
    """The generated command, run for real through the gate, fails when its tests collect nothing."""
    cmd = "python -m pytest -q -p no:cacheprovider"   # a literal prefix, so scoping infers it
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    write(repo, {".brindle/config.json": json.dumps({"checks": [cmd], "scoped_checks": "infer"}),
                 "src/pkg/__init__.py": "", "src/pkg/lonely.py": "X = 1\n", "tests/test_lonely.py": "Y = 1\n"})
    sh("git add -A && git commit -qm base", repo)
    monkeypatch.setattr(codemap, "graph_path", lambda target: None)
    ws = workspaces.create(db, str(repo), "feat").workspace
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "done", "@0", None, time.time()))
    write(Path(ws.path), {"src/pkg/lonely.py": "X = 2\n"})
    sh("git add -A && git commit -qm work", Path(ws.path))
    ok, _ = gates.run_checked(db, ws, cmd, {}, 120)
    assert not ok
    tree = git.out(["rev-parse", "HEAD^{tree}"], ws.path)
    assert db.get_tree_check(ws.repo_root, tree, f"{cmd} tests/test_lonely.py\nsrc/pkg/lonely.py") is None
