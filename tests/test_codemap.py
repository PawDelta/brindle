import json
import os
import time
from pathlib import Path

import pytest

from brindle import agents, codemap, workspaces
from brindle.db import Agent
from brindle.profiles import load_profile
from brindle.providers import ClaudeCode, LaunchContext, get_provider


@pytest.fixture
def mapped(repo, monkeypatch):
    """A repo with a graphify graph, and graphify on PATH."""
    (repo / "graphify-out").mkdir()
    (repo / "graphify-out" / "graph.json").write_text("{}")
    monkeypatch.setattr(codemap.shutil, "which", lambda cmd: f"/bin/{cmd}")
    return repo


def test_workers_get_the_code_map(db, mapped):
    ws = workspaces.create(db, str(mapped), "feat").workspace
    prompt = agents.decorate_worker_prompt("add a flag", "w1", ws, None, get_provider("claude"), headless=False)
    assert "Code map:" in prompt and str(mapped / "graphify-out" / "graph.json") in prompt


def test_the_supervisor_gets_the_code_map(db, mapped):
    ws = workspaces.adopt_root(db, str(mapped))
    db.add_agent(Agent("s1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@0", None, time.time()))
    assert "Code map:" in agents._profile_for(db, db.get_agent("s1"), ws).prompt


def test_graphify_queries_are_pre_approved():
    argv = ClaudeCode().command(LaunchContext("a1", load_profile("developer"), None))
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "Bash(graphify query:*)" in allowed and "Bash(graphify affected:*)" in allowed


def test_graph_query_log_records_read_only_graphify_commands_per_agent(db, mapped):
    ws = workspaces.create(db, str(mapped), "feat").workspace
    for aid in ("w1", "w2"):
        db.add_agent(Agent(aid, ws.id, "developer", "claude", None, "headless", "idle", "@0", None, time.time()))
    for aid, cmd in [("w1", "graphify query 'auth'"), ("w1", "git status"), ("w1", "graphify update ."),
                     ("w2", "graphify path a b")]:
        agents.pre_tool_decision(db, db.get_agent(aid), {"tool_name": "Bash", "tool_input": {"command": cmd}})
    rows = codemap.graph_queries(db, ws.repo_root, "w1")
    assert [(r.agent_id, r.branch, r.task) for r in rows] == [("w1", "feat", "graphify query 'auth'")]
    assert len(codemap.graph_queries(db, ws.repo_root)) == 2


def _fake_graph(path: Path, hub: str) -> None:
    nodes = [{"id": f"n{i}", "label": hub if i == 0 else f"f{i}", "community": i % 2,
              "community_name": "Alpha" if i % 2 == 0 else "Beta",
              "source_file": "src/a.py", "source_location": f"L{10 + i}"} for i in range(5)]
    links = [{"source": "n0", "target": f"n{i}"} for i in range(1, 5)]
    path.write_text(json.dumps({"nodes": nodes, "links": links}))


def test_supervisor_repo_map_is_in_the_supervisor_prompt_and_cached_per_graph_mtime(db, mapped):
    graph = mapped / "graphify-out" / "graph.json"
    _fake_graph(graph, "Hub")
    stamp = graph.stat().st_mtime
    ws = workspaces.adopt_root(db, str(mapped))
    db.add_agent(Agent("s1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@0", None, time.time()))
    prompt = agents._profile_for(db, db.get_agent("s1"), ws).prompt
    assert "Alpha (3)" in prompt and "Beta (2)" in prompt and "Hub src/a.py:10" in prompt
    assert "graphify affected" in prompt
    worker = agents.decorate_worker_prompt("add a flag", "w1", ws, None, get_provider("claude"), headless=False)
    assert "Alpha (3)" not in worker
    assert "Hub src" in codemap.supervisor_map(mapped)
    _fake_graph(graph, "Renamed")
    os.utime(graph, (stamp, stamp))
    assert "Hub src" in codemap.supervisor_map(mapped)   # same mtime: not re-read
    os.utime(graph, (stamp + 5, stamp + 5))
    assert "Renamed src" in codemap.supervisor_map(mapped)


def test_merges_refresh_the_graph(mapped, monkeypatch):
    started = []
    runners = []
    real_popen = codemap.subprocess.Popen

    def fake_popen(argv, **kw):
        if "-c" in argv:
            runners.append((argv[-3], argv[-2] or None, os.dup(kw["pass_fds"][0])))
            return None
        if "graphify" in argv:
            started.append(argv)
            return None
        return real_popen(argv, **kw)

    monkeypatch.setattr(codemap.subprocess, "Popen", fake_popen)
    codemap.refresh_later(str(mapped))
    codemap._run_refresh(*runners.pop())
    assert started == [["nice", "-n", "10", "graphify", "update", str(mapped), "--force"]]


def test_graphify_single_flight(mapped, monkeypatch):
    import importlib.util

    monkeypatch.setattr("brindle.git.paths", lambda *a, **kw: [])
    codemap.copy_to_store(mapped / codemap.GRAPH, str(mapped))
    checkout = mapped.parent / "session"
    checkout.mkdir()
    (checkout / "brindle.py").write_text("raise SystemExit('checkout decoy imported')")
    completions, started = [], []
    real_popen = codemap.subprocess.Popen
    spec = importlib.util.spec_from_file_location("independent_codemap", codemap.__file__)
    caller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(caller)

    class Process:
        def wait(self):
            if len(started) == 1:
                for _ in range(30):
                    caller.refresh_later(str(checkout) + "/.", str(mapped))
                caller.refresh(str(checkout), str(mapped))
                assert len(started) == 1

    def launch(argv, **kw):
        if "graphify" not in argv:
            # Exercise the child bootstrap, replacing only its graph update work.
            probe = argv.copy()
            code = probe.index("-c") + 1
            probe[code] = (
                "import brindle.codemap as c; "
                "c._run_refresh = lambda *a: print('runner reached'); " + probe[code]
            )
            child = real_popen(probe, cwd=kw["cwd"], stdout=codemap.subprocess.PIPE,
                               stderr=codemap.subprocess.PIPE, text=True)
            stdout, stderr = child.communicate()
            assert child.returncode == 0, stderr
            assert stdout.strip() == "runner reached"
            # Model inherited descriptors surviving the launching caller's exit.
            fd = os.dup(kw["pass_fds"][0])
            completions.append(lambda: codemap._run_refresh(argv[-3], argv[-2], fd))
            return Process()
        started.append(argv)
        return Process()

    monkeypatch.setattr(codemap.subprocess, "Popen", launch)
    assert caller.seed_session(checkout, str(mapped)) is True
    assert codemap.seed_session(checkout, str(mapped)) is False
    codemap.refresh_later(str(checkout), str(mapped))
    assert len(completions) == 1
    assert not started
    del caller  # Its detached runner and lock must remain alive.
    spec.loader.exec_module(caller := importlib.util.module_from_spec(spec))
    caller.refresh_later(str(checkout), str(mapped))
    assert len(completions) == 1
    completions.pop(0)()
    assert len(started) == 2
    assert all(argv[:4] == ["nice", "-n", "10", "graphify"] for argv in started)
    assert not completions
    codemap.refresh(str(checkout), str(mapped))
    assert len(started) == 3


@pytest.mark.parametrize("tracked", [True, False])
def test_automatic_rebuild_preserves_tracked_graph_or_can_be_disabled(mapped, monkeypatch, tracked):
    from brindle import git

    graph = mapped / codemap.GRAPH
    _fake_graph(graph, "Semantic hub")
    for name in ("graph.html", "GRAPH_REPORT.md", "manifest.json"):
        (graph.parent / name).write_text("semantic graph artifact")
    original = {p: p.read_bytes() for p in graph.parent.iterdir()}
    # Model the git index without spawning git; graphify would downgrade all artifacts.
    monkeypatch.setattr(git, "paths", lambda *a, **kw: [str(p.relative_to(mapped)) for p in original] if tracked else [])
    if not tracked:
        config = mapped / ".brindle"
        config.mkdir(exist_ok=True)
        (config / "config.json").write_text(json.dumps({"graphify_auto_refresh": False}))

    started = []

    def rebuild(argv, **kw):
        started.append(argv)
        for path in original:
            path.write_text('{"nodes": []}')

    monkeypatch.setattr(codemap.subprocess, "Popen", rebuild)
    for refresh in (codemap.refresh, codemap.refresh_later):
        refresh(str(mapped), repo_root=str(mapped))
        assert {p: p.read_bytes() for p in original} == original
    assert codemap.graph_path(mapped) == graph  # Queries remain available.
    graph.unlink()
    assert codemap.seed_session(mapped, repo_root=str(mapped)) is False
    assert not graph.exists()
    assert not started


def test_prune_stores_removes_old_graph_store_and_keeps_fresh_one(tmp_path, monkeypatch):
    home = tmp_path / "brindle"
    monkeypatch.setattr("brindle.config.brindle_home", lambda: home)
    old_store = home / "graphs" / "old-repo-a1b2"
    fresh_store = home / "graphs" / "fresh-repo-c3d4"
    old_store.mkdir(parents=True)
    fresh_store.mkdir(parents=True)
    (old_store / "graph.json").write_text("{}")
    (fresh_store / "graph.json").write_text("{}")
    now = time.time()
    old = now - 31 * 24 * 60 * 60
    os.utime(old_store / "graph.json", (old, old))
    os.utime(old_store, (old, old))

    assert codemap.prune_stores(now) == 1
    assert not old_store.exists()
    assert (fresh_store / "graph.json").is_file()


def test_session_graph_resolution_and_seeding(db, mapped, monkeypatch):
    from brindle.config import brindle_home

    store = brindle_home() / "graphs" / mapped.name / "graph.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text('{"nodes": [{"id": "seed-node"}]}')
    cache = mapped / "graphify-out" / "cache" / "ast" / "v1"
    cache.mkdir(parents=True)
    (cache / "entry.json").write_text('{"nodes": []}')

    started, runners = [], []
    real_popen = codemap.subprocess.Popen

    def fake_popen(argv, **kw):
        if "-c" in argv:
            runners.append((argv[-3], argv[-2] or None, os.dup(kw["pass_fds"][0])))
            return None
        if "graphify" in argv:
            seeded_cache = Path(kw["cwd"]) / "graphify-out" / "cache" / "ast" / "v1" / "entry.json"
            assert seeded_cache.read_bytes() == (cache / "entry.json").read_bytes()
            started.append(argv)
            return None
        return real_popen(argv, **kw)

    monkeypatch.setattr(codemap.subprocess, "Popen", fake_popen)

    session_ws = workspaces.create(db, str(mapped), "session-branch").workspace
    worker_ws = workspaces.create(db, str(mapped), "worker-task", base="session-branch").workspace

    prompt = agents.decorate_worker_prompt("do task", "w1", worker_ws, None, get_provider("claude"), headless=False)
    codemap._run_refresh(*runners.pop())
    session_graph = Path(session_ws.path) / "graphify-out" / "graph.json"
    assert "Code map:" in prompt and str(session_graph) in prompt
    assert session_graph.is_file()
    assert json.loads(session_graph.read_text()) == {"nodes": [{"id": "seed-node"}]}
    assert started == [["nice", "-n", "10", "graphify", "update", session_ws.path, "--force"]]
    (mapped / codemap.GRAPH).unlink()
    assert codemap.seed_session(mapped, str(mapped)) is True
    codemap._run_refresh(*runners.pop())

    # No stored graph: the repo hasn't opted in, so nothing is seeded or started
    (mapped / codemap.GRAPH).unlink(missing_ok=True)
    store.unlink()
    runs = len(started)
    assert codemap.seed_session(mapped, str(mapped)) is False
    assert not runners and len(started) == runs and not (mapped / codemap.GRAPH).exists()

    # Config switch disables graph
    monkeypatch.setattr("brindle.config.load_repo_config", lambda root: type("Cfg", (), {"graphify": False})())
    assert codemap.graph_path(worker_ws) is None
    assert codemap.seed_session(session_ws.path) is False


def test_session_graph_refresh_and_community_carryover(mapped, monkeypatch):
    from brindle.config import brindle_home

    old_data = {
        "nodes": [
            {"id": "n1", "community": 1, "community_name": "Auth"},
            {"id": "n2", "community": 1, "community_name": "Auth"},
        ]
    }
    graph_file = mapped / "graphify-out" / "graph.json"
    graph_file.write_text(json.dumps(old_data))

    new_data = {
        "nodes": [
            {"id": "n1", "community": 10},
            {"id": "n3", "community": 20, "source_file": "src/core/helper.py"},
        ]
    }

    real_popen = codemap.subprocess.Popen

    def fake_popen(argv, **kw):
        if "graphify" in argv:
            graph_file.write_text(json.dumps(new_data))
            return None
        return real_popen(argv, **kw)

    monkeypatch.setattr(codemap.subprocess, "Popen", fake_popen)
    codemap.refresh(str(mapped))

    updated = json.loads(graph_file.read_text())
    nodes_by_id = {n["id"]: n for n in updated["nodes"]}
    assert nodes_by_id["n1"]["community_name"] == "Auth"
    assert nodes_by_id["n3"]["community_name"] == "helper"

    store = brindle_home() / "graphs" / mapped.name / "graph.json"
    assert store.is_file()
    assert json.loads(store.read_text()) == updated


def test_session_graph_removed_with_worktree(db, mapped):
    ws = workspaces.create(db, str(mapped), "remove-session").workspace
    out_dir = Path(ws.path) / "graphify-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "graph.json").write_text("{}")
    assert out_dir.is_dir()

    workspaces.remove(db, ws, force=True)
    assert not out_dir.exists()

