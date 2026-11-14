"""Cheaper agent start (prompt_cache_prefix): agents of one profile start
with the same prefix, and short-lived agents get a small, fixed tool list."""

import json
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, opencode, workspaces
from brindle.db import Agent
from brindle.providers import Antigravity, ClaudeCode, Codex, LaunchContext, get_provider


def _start(db, agent, ws, prompt=None):
    """The Claude Code argv ``agent`` launches with: (system prompt, first
    message, argv)."""
    argv = ClaudeCode().command(LaunchContext(agent.id, agents._profile_for(db, agent, ws), prompt,
                                              cwd=ws.path, mode=agent.mode))
    return argv[argv.index("--append-system-prompt") + 1], (argv[-1] if prompt else None), argv


def _launched(db, monkeypatch):
    """Each agent the real dispatch starts, with the first message it is given."""
    seen = []
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws_, *, prompt, **kw: seen.append((a.id, prompt)))
    monkeypatch.setattr("brindle.providers.trust_folder", lambda path: None)
    return seen


def _two_starts(db, repo, monkeypatch, kind):
    """Two agents of one profile on different tasks: [(task, ws, system prompt, first message)]."""
    seen = _launched(db, monkeypatch)
    out = []
    for n, task in enumerate(("add the --json flag", "fix the login redirect")):
        ws = workspaces.create(db, str(repo), f"qx-task-{n}").workspace
        if kind == "reviewer":   # a branch review, through request_review's own brief
            (Path(ws.path) / f"f{n}.py").write_text("x = 1\n")
            sh("git add -A && git commit -qm work", Path(ws.path))
            agents.request_review(db, None, ws, "reviewer", focus=task, check_summary=f"{task}: passed")
        elif kind == "opencode":   # OpenCode's instructions are the profile's prompt
            agent = Agent(f"oc{n}", ws.id, "developer", "opencode", None, "assign", "starting", "", None,
                          time.time())
            db.add_agent(agent)
            first = agents.decorate_worker_prompt(task, agent.id, ws, None, get_provider("opencode"), False)
            out.append((task, ws, agents._profile_for(db, agent, ws).prompt, first))
            continue
        else:
            agents.spawn(db, ws, "developer", prompt=task, mode="assign", done_when=f"{task} works")
        agent_id, prompt = seen[-1]
        out.append((task, ws, *_start(db, db.get_agent(agent_id), ws, prompt)[:2]))
    return out


@pytest.mark.parametrize("kind,on", [
    pytest.param("worker", True, marks=pytest.mark.variant(
        "compares two workers' real launch argv: per-task text leaking into the system prompt "
        "breaks the shared prefix while every line still runs")),
    pytest.param("worker", False, marks=pytest.mark.variant(
        "the setting turned off keeps the repo brief in the task and out of the system prompt")),
    pytest.param("reviewer", True, marks=pytest.mark.variant(
        "two branch reviews through request_review: its fixed classification text must lead, "
        "in the shared brief, not follow each review's checks results")),
    pytest.param("opencode", True, marks=pytest.mark.variant(
        "a provider other than Claude: the brief must reach OpenCode's instructions, not stay after the task")),
])
def test_same_profile_agents_share_their_start_prefix(db, repo, monkeypatch, kind, on):
    (Path(repo) / ".brindle" / "rules").mkdir(parents=True, exist_ok=True)
    (Path(repo) / ".brindle" / "rules" / "learned.md").write_text(
        "---\nname: learned\n---\nRULE-PACK-TEXT: keep commits small.\n")
    (Path(repo) / ".brindle" / "config.json").write_text(json.dumps({"prompt_cache_prefix": on}))
    (task_a, ws_a, system_a, first_a), (task_b, ws_b, system_b, first_b) = _two_starts(db, repo, monkeypatch, kind)
    brief = (agents.review_brief(ws_a, branch_review=True) if kind == "reviewer"
             else agents.worker_brief(ws_a))
    assert system_a == system_b   # role prompt, rule packs, repo brief: the shared prefix
    assert first_a != first_b and task_a in first_a and task_b in first_b
    assert task_a not in system_a and ws_a.branch not in system_a and ws_b.branch not in system_b
    if on:
        role = system_a.index("You are a code reviewer" if kind == "reviewer" else "You are a developer agent")
        assert role < system_a.index("RULE-PACK-TEXT") < system_a.index(brief)
        # Only per-task text after it: none of the brief's paragraphs repeats in the task.
        assert [part for part in brief.split("\n\n")[1:] if part in first_a] == []
        assert ("submit_review" if kind == "reviewer" else "report_result") in first_a
    else:   # the repo brief stays in the task, as before the setting
        guidance = agents.worker_guidance(ws_a)
        assert guidance not in system_a and guidance in first_a


def _hidden(db, ws, agent, profile=None):
    """The brindle tools ``agent``'s launch (with ``profile``, else the one
    built now) takes out of its context, as its provider is told, by bare name."""
    profile = profile or agents._profile_for(db, agent, ws)
    ctx = LaunchContext(agent.id, profile, None, cwd=ws.path, mode=agent.mode)
    if agent.provider == "codex":
        flag = next(a for a in Codex().command(ctx) if a.startswith("mcp_servers.brindle.disabled_tools="))
        return json.loads(flag.split("=", 1)[1])
    if agent.provider == "antigravity":   # agy reads the checkout's .agents/mcp_config.json
        Antigravity().command(ctx)
        server = json.loads((Path(ws.path) / ".agents" / "mcp_config.json").read_text())["mcpServers"]["brindle"]
        assert not set(server.get("disabledTools", [])) & set(server["tools"])   # none still eager
        return server.get("disabledTools", [])
    if agent.provider == "opencode":
        perms = opencode.config(ctx, opencode.launch_for(profile, ws.path))["permission"]
        return sorted(k.removeprefix("brindle_") for k, v in perms.items() if k.startswith("brindle_") and v == "deny")
    argv = ClaudeCode().command(ctx)
    assert json.loads(argv[argv.index("--settings") + 1])["env"]["FORCE_PROMPT_CACHING_5M"] == "1"
    return [t.removeprefix("mcp__brindle__") for t in argv[argv.index("--disallowedTools") + 1].split(",")]


@pytest.mark.parametrize("provider,profile,mode,weight,keep", [
    pytest.param("claude", "reviewer", "review", None, agents.REVIEWER_TOOLS, marks=pytest.mark.variant(
        "Claude Code's own rendering (--disallowedTools, the 5-minute cache) and a medium worker kept whole")),
    ("codex", "reviewer-codex", "review", None, agents.REVIEWER_TOOLS),
    ("opencode", "developer-opencode", "assign", "light", agents.LIGHT_WORKER_TOOLS),
    ("antigravity", "reviewer-gemini", "review", None, agents.REVIEWER_TOOLS),
])
def test_short_lived_agents_get_a_fixed_subset_of_brindle_tools(db, repo, provider, profile, mode, weight, keep):
    from brindle.mcp_server import mcp

    every = {t.name for t in mcp._tool_manager.list_tools()}
    ws = workspaces.create(db, str(repo), "feature").workspace
    starts = []
    for aid in ("a1", "a2"):
        db.add_agent(Agent(aid, ws.id, profile, provider, None, mode, "starting", "", None, time.time()))
        db.update_agent(aid, weight=weight)   # as spawn records it
        starts.append(_hidden(db, ws, db.get_agent(aid)))
    assert starts[0] == starts[1]   # the same tool list at every start
    assert every - set(starts[0]) == set(keep) and set(keep) < every
    if provider == "antigravity":
        # The tool list is the checkout's. The reviewer's profile is built
        # while it is alone; an agy worker then registers and writes its
        # config before the reviewer's launch writes: the worker keeps its tools.
        stale = agents._profile_for(db, db.get_agent("a1"), ws)
        assert stale.hidden_brindle_tools
        db.add_agent(Agent("w1", ws.id, "developer-gemini", provider, None, "assign", "starting", "", None,
                           time.time()))
        assert _hidden(db, ws, db.get_agent("w1")) == []
        assert _hidden(db, ws, db.get_agent("a1"), stale) == []
    if provider == "claude":   # a worker on a normal task keeps every tool and the default cache
        db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "starting", "", None,
                           time.time()))
        db.update_agent("w1", weight="medium")
        _, _, argv = _start(db, db.get_agent("w1"), ws)
        assert "--disallowedTools" not in argv
        assert "FORCE_PROMPT_CACHING_5M" not in json.loads(argv[argv.index("--settings") + 1])["env"]
