import time

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
    assert "Bash(graphify query:*)" in allowed


def test_merges_refresh_the_graph(mapped, monkeypatch):
    started = []
    monkeypatch.setattr(codemap.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    codemap.refresh_later(str(mapped))
    assert started == [["graphify", "update", str(mapped)]]
