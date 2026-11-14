"""Cheaper agent start (prompt_cache_prefix): agents of one profile start
with the same prefix, and short-lived agents get a small, fixed tool list."""

import json
import time
from pathlib import Path

import pytest

from brindle import agents, workspaces
from brindle.db import Agent
from brindle.providers import ClaudeCode, LaunchContext


def _start(db, agent, ws, prompt=None):
    """The Claude Code argv ``agent`` launches with: (system prompt, first
    message, argv)."""
    argv = ClaudeCode().command(LaunchContext(agent.id, agents._profile_for(db, agent, ws), prompt,
                                              cwd=ws.path, mode=agent.mode))
    return argv[argv.index("--append-system-prompt") + 1], (argv[-1] if prompt else None), argv


def _spawned(db, ws, task, monkeypatch, launched):
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws_, *, prompt, **kw: launched.append(prompt))
    agent = agents.spawn(db, ws, "developer", prompt=task, mode="assign", done_when=f"{task} works")
    return db.get_agent(agent.id), launched[-1]


@pytest.mark.parametrize("on", [True, False])
def test_same_profile_workers_share_their_start_prefix(db, repo, monkeypatch, on):
    monkeypatch.setattr("brindle.providers.trust_folder", lambda path: None)
    (Path(repo) / ".brindle" / "rules").mkdir(parents=True, exist_ok=True)
    (Path(repo) / ".brindle" / "rules" / "learned.md").write_text(
        "---\nname: learned\n---\nRULE-PACK-TEXT: keep commits small.\n")
    (Path(repo) / ".brindle" / "config.json").write_text(json.dumps({"prompt_cache_prefix": on}))
    launched = []
    starts = []
    for n, task in enumerate(("add the --json flag", "fix the login redirect")):
        ws = workspaces.create(db, str(repo), f"qx-task-{n}").workspace
        agent, prompt = _spawned(db, ws, task, monkeypatch, launched)
        starts.append((task, ws, *_start(db, agent, ws, prompt)[:2]))
    (task_a, ws_a, system_a, first_a), (task_b, ws_b, system_b, first_b) = starts
    brief = agents.worker_brief(ws_a)
    assert system_a == system_b   # role prompt, rule packs, repo brief: the shared prefix
    assert first_a != first_b and task_a in first_a and task_b in first_b
    assert task_a not in system_a and ws_a.branch not in system_a and ws_b.branch not in system_b
    if on:
        role = system_a.index("You are a developer agent")
        assert role < system_a.index("RULE-PACK-TEXT") < system_a.index(brief)
        assert brief not in first_a and brief not in first_b   # only per-task text after it
        assert "report_result" in first_a
    else:   # the repo brief stays in the task, as before the setting
        guidance = agents.worker_guidance(ws_a)
        assert guidance not in system_a and guidance in first_a


def test_short_lived_agents_get_a_fixed_subset_of_brindle_tools(db, repo):
    from brindle.mcp_server import mcp

    every = {f"mcp__brindle__{t.name}" for t in mcp._tool_manager.list_tools()}
    ws = workspaces.create(db, str(repo), "feature").workspace
    starts = []
    for rid in ("r1", "r2"):
        db.add_agent(Agent(rid, ws.id, "reviewer", "claude", None, "review", "starting", "", None, time.time()))
        _, _, argv = _start(db, db.get_agent(rid), ws)
        hidden = set(argv[argv.index("--disallowedTools") + 1].split(","))
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["env"]["FORCE_PROMPT_CACHING_5M"] == "1"
        starts.append(argv[argv.index("--disallowedTools") + 1])
    assert starts[0] == starts[1]   # the same tool list at every start
    visible = every - hidden
    assert visible == {f"mcp__brindle__{t}" for t in agents.REVIEWER_TOOLS} < every
    # A worker on a normal task keeps every tool and the default cache.
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "starting", "", None, time.time(),
                       weight="medium"))
    _, _, argv = _start(db, db.get_agent("w1"), ws)
    assert "--disallowedTools" not in argv
    assert "FORCE_PROMPT_CACHING_5M" not in json.loads(argv[argv.index("--settings") + 1])["env"]
