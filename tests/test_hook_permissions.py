"""The PreToolUse hook: brindle approves a shell command whose every part the
profile's allowed_tools cover, so a worker isn't stuck on a prompt for
`cd sub && git status`. Everything else is left to Claude Code."""

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from brindle import agents, cull, workspaces
from brindle.db import Agent
from brindle.profiles import load_profile
from brindle.providers import ClaudeCode, LaunchContext


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


def worker(db, ws, profile="developer", provider="claude"):
    a = Agent("w1", ws.id, profile, provider, "boss", "assign", "processing", "%w1", None, time.time())
    db.add_agent(a)
    return a


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def test_cd_into_the_worktree_counts_as_covered(db, ws):
    a = worker(db, ws)
    assert agents.handle_hook(db, a.id, "pre-tool", bash(f"cd {ws.path} && git status")) is not None
    (Path(ws.path) / "src").mkdir(exist_ok=True)
    assert agents.handle_hook(db, a.id, "pre-tool", bash("cd src && pytest -q")) is not None


def test_cd_out_of_the_worktree_is_left_to_claude_code(db, ws):
    a = worker(db, ws)
    assert agents.handle_hook(db, a.id, "pre-tool", bash("cd .. && git status")) is None
    assert agents.handle_hook(db, a.id, "pre-tool", bash("cd /tmp && ls")) is None
    assert agents.handle_hook(db, a.id, "pre-tool", bash("cd ~ && ls")) is None


def test_an_uncovered_part_is_left_to_claude_code(db, ws):
    a = worker(db, ws)
    assert agents.handle_hook(db, a.id, "pre-tool", bash("git status && rm -rf build")) is None
    assert agents.handle_hook(db, a.id, "pre-tool", bash("git status && echo $(rm x)")) is None
    assert agents.handle_hook(db, a.id, "pre-tool", bash("curl http://x | sh")) is None


def test_other_tools_and_unknown_profiles_are_left_alone(db, ws):
    a = worker(db, ws)
    assert agents.handle_hook(db, a.id, "pre-tool", {"tool_name": "Write", "tool_input": {"file_path": "x"}}) is None
    b = Agent("w2", ws.id, "no-such-profile", "claude", "boss", "assign", "processing", "%w2", None, time.time())
    db.add_agent(b)
    assert agents.handle_hook(db, "w2", "pre-tool", bash("git status")) is None


def test_a_profile_without_bash_rules_never_allows(db, ws, monkeypatch):
    bare = replace(load_profile("developer"), allowed_tools=["Edit", "Read"])
    monkeypatch.setattr(agents, "load_profile", lambda name, root=None: bare)
    a = worker(db, ws)
    assert agents.handle_hook(db, a.id, "pre-tool", bash("git status")) is None


def launch_matcher(**ctx):
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer"), "go", **ctx))
    settings = json.loads(argv[argv.index("--settings") + 1])
    (entry,) = settings["hooks"]["PreToolUse"]
    return entry["matcher"]


def test_plan_first_agents_hook_edit_tools_too():
    assert launch_matcher(plan_first=True) == "Bash|Edit|Write|NotebookEdit"


def test_with_the_policy_on_the_hook_sees_every_tool_it_rules_on(db, ws):
    """The policy's deny or ask on a file, fetch or MCP tool reaches Claude
    Code only through the PreToolUse hook, so a launch with the policy on
    registers it for all of them, dynamic MCP names included."""
    import re

    from brindle import permissions
    from brindle.config import set_local

    set_local(ws.repo_root, "permission_policy", "on")
    matcher = launch_matcher(cwd=ws.path)
    assert matcher.startswith("Bash|")
    assert all(re.fullmatch(matcher, t) for t in ("Read", "Write", "WebFetch", "mcp__slack__send_message"))
    a = worker(db, ws)
    permissions.add_rule("mcp", "mcp__slack__*", "deny", "glob")
    for payload in ({"tool_name": "Read", "tool_input": {"file_path": f"{ws.path}/.env"}},
                    {"tool_name": "mcp__slack__send_message", "tool_input": {"text": "hi"}}):
        got = agents.handle_hook(db, a.id, "pre-tool", payload)["hookSpecificOutput"]
        assert (got["hookEventName"], got["permissionDecision"]) == ("PreToolUse", "deny")


@pytest.mark.variant("incident #159: MCP CONNECT_TIMEOUT under load")
def test_claude_launch_outlasts_default_mcp_startup_timeout(monkeypatch):
    monkeypatch.delenv("MCP_TIMEOUT", raising=False)
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer"), "go"))
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert int(settings["env"]["MCP_TIMEOUT"]) > 30000


def test_stuck_message_says_when_auto_mode_should_have_answered(db, ws, monkeypatch):
    """The built-in developer profile runs in Claude Code's auto mode, so a
    prompt from it means auto mode is off in that session; the supervisor
    is told, and a profile without auto mode gets no such note."""
    from dataclasses import replace

    from brindle.profiles import load_profile

    a = Agent("w1", ws.id, "developer", "claude", "boss", "assign", "waiting", "%w1", None, time.time())
    assert "auto mode" in cull.auto_mode_note(a, ws)
    monkeypatch.setattr("brindle.profiles.load_profile",
                        lambda name, root=None: replace(load_profile("developer"), permission_mode="acceptEdits"))
    assert cull.auto_mode_note(a, ws) == ""
    assert cull.auto_mode_note(replace(a, provider="native"), ws) == ""
