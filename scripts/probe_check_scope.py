"""Show what scoped checks would run on this repo for representative change sets.

    uv run python scripts/probe_check_scope.py [--graph graphify-out/graph.json]

Each change set names files and the graph symbols a branch changed in them
(none: the graph can't map the change, so the test imports decide). The
probe runs ``check_scope.command`` as a worker branch's gate would, with the
real graph, ``graphify affected`` and the repo's tests, and prints one row per
set: how many test files the change reaches, what the check runs, and whether
the module's own tests are among them. It exits 1 when a set falls back to
the full suite, runs more than ``scoped_checks_max_files`` files, or leaves
out the module's own tests. "new module" adds an untracked module and its
test for the run and removes them afterwards.
"""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

from brindle import check_scope, codemap
from brindle.config import load_repo_config

ROOT = Path(__file__).resolve().parent.parent
CMD = "uv run pytest -q"
NEW = {"src/brindle/scope_probe_new.py": "X = 1\n", "tests/test_scope_probe_new.py": "from brindle import scope_probe_new\n"}
# name: ({changed file: changed graph labels, or None when the graph can't map it}, the module's own test)
SETS = {
    "db.py": ({"src/brindle/db.py": ["._migrate()", ".add_session_repo()"]}, "tests/test_db_schema_compat.py"),
    "agents.py": ({"src/brindle/agents.py": None}, "tests/test_agents.py"),
    "tasks.py": ({"src/brindle/tasks.py": None}, "tests/test_task_deps.py"),
    "kb.py": ({"src/brindle/kb.py": None}, "tests/test_kb.py"),
    "daemon/routes_read.py": ({"src/brindle/daemon/routes_read.py": None}, "tests/test_daemon_api.py"),
    "new module": ({"src/brindle/scope_probe_new.py": None, "tests/test_scope_probe_new.py": None},
                   "tests/test_scope_probe_new.py"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--graph", type=Path, default=ROOT / codemap.GRAPH)
    args = parser.parse_args()
    if not args.graph.is_file():
        parser.error(f"no graph at {args.graph}: pass --graph")
    nodes = check_scope._nodes_by_file(args.graph)
    max_files = load_repo_config(str(ROOT)).scoped_checks_max_files
    changes: dict[str, list[str] | None] = {}

    def changed_nodes(ws, base, path, file_nodes, skip_tests=False):
        labels = changes[path]
        return None if labels is None else {n["id"] for n in nodes.get(path, []) if n.get("label") in labels}

    codemap.graph_path = lambda ws: args.graph
    check_scope._changed_nodes = changed_nodes
    ws = SimpleNamespace(path=str(ROOT))
    failed = False
    created: list[Path] = []
    try:
        for name, text in NEW.items():
            try:
                with open(ROOT / name, "x", encoding="utf-8") as f:   # never overwrite a real file
                    f.write(text)
            except FileExistsError:
                parser.error(f"{name} already exists; move it away to run the probe")
            created.append(ROOT / name)
        print(f"{'change set':<24}{'reached':>8}{'runs':>6}  {'own test included':<40}command")
        for name, (files, own) in SETS.items():
            changes.clear()
            changes.update(files)
            try:
                reached = len(check_scope.pytest_targets(ws, "HEAD", list(files)))
            except check_scope.Unscoped as why:
                reached = f"full: {why}"
            run = check_scope.command(ws, CMD, check_scope.CheckScope(), ("HEAD", list(files)), max_files)
            tests = [t for t in shlex.split(run.split("; ")[-1]) if t.startswith("tests/")] if run else []
            ok = run not in (None, CMD) and not run.startswith("printf '%s\\n' 'brindle: ran this check in full") \
                and 0 < len(tests) <= max_files and own in tests
            failed |= not ok
            print(f"{name:<24}{reached!s:>8}{len(tests):>6}  {own if own in tests else 'MISSING ' + own:<40}"
                  f"{'' if ok else 'FAIL  '}{run if len(run or '') < 160 else run[:157] + '...'}")
    finally:
        for path in created:   # only what this run made
            path.unlink(missing_ok=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
