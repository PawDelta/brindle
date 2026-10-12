import json
import os
import subprocess
import sys
import time

import pytest

from conftest import sh
from brindle import antigravity, autopilot, workspaces
from brindle.db import Agent
from brindle.profiles import load_profile
from brindle.providers import Antigravity, LaunchContext


@pytest.fixture
def ws(db, repo):
    return workspaces.adopt_root(db, str(repo))


def add_agent(db, ws, agent_id="g1", mode="interactive", status="idle", profile="supervisor"):
    db.add_agent(Agent(agent_id, ws.id, profile, "antigravity", None, mode, status, "@0", None, time.time()))


def test_command_passes_the_profiles_model_and_effort(db, ws, repo, monkeypatch):
    import dataclasses
    monkeypatch.setenv("BRINDLE_AGY_BIN", "/bin/agy")
    profile = dataclasses.replace(load_profile("developer"), model="gemini-3.1-pro-high", effort="high")
    argv = Antigravity().command(LaunchContext("g1", profile, None, cwd=str(repo)))
    assert argv[:5] == ["/bin/agy", "--model", "gemini-3.1-pro-high", "--effort", "high"]


def test_resume_uses_the_conversation(db, ws, repo):
    argv = Antigravity().command(LaunchContext("g1", load_profile("developer"), None, resume="abc", cwd=str(repo)))
    assert argv[-2:] == ["--conversation", "abc"]


def test_existing_config_is_kept_and_tracked_files_are_left_alone(repo):
    (repo / ".agents").mkdir()
    (repo / ".agents" / "mcp_config.json").write_text(json.dumps({"mcpServers": {"fs": {"command": "x"}}}))
    antigravity.install(str(repo))
    servers = json.loads((repo / ".agents" / "mcp_config.json").read_text())["mcpServers"]
    assert set(servers) == {"fs", "brindle"}
    # A file that was already there isn't hidden from git.
    assert ".agents/mcp_config.json" in sh("git status --porcelain --untracked-files=all", repo)
    (repo / ".agents" / "hooks.json").write_text("{}")
    sh("git add -f .agents/hooks.json && git commit -qm cfg", repo)
    with pytest.raises(antigravity.AntigravityError, match="committed"):
        antigravity.install(str(repo))


def test_hook_finds_its_agent_from_the_agy_process(db, ws, monkeypatch):
    add_agent(db, ws, status="processing")
    monkeypatch.delenv("BRINDLE_AGENT_ID", raising=False)
    parent = os.getppid()
    monkeypatch.setattr(antigravity, "_process_env", lambda pid: {"BRINDLE_AGENT_ID": "g1"} if pid == parent else {})
    db.add_autopilot("g1")
    autopilot.set_goal(db, "g1", "Goal", [("M", "false", None)])
    monkeypatch.setattr(autopilot, "active_workers", lambda db, rid: [])
    assert json.loads(antigravity.hook_main(db, "agy-stop", "{}"))["decision"] == "continue"
    monkeypatch.setattr(antigravity, "_process_env", lambda pid: {})
    monkeypatch.setattr(antigravity, "_parent", lambda pid: None)
    assert antigravity.hook_main(db, "agy-stop", "{}") == "{}"


def test_process_env_reads_another_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                             env={**os.environ, "BRINDLE_AGENT_ID": "abc123"})
    try:
        time.sleep(0.3)
        assert antigravity._process_env(child.pid).get("BRINDLE_AGENT_ID") == "abc123"
    finally:
        child.kill()


def test_queued_messages_arrive_as_a_new_turn(db, ws, monkeypatch):
    flushed = []
    monkeypatch.setattr(antigravity, "_flush_soon", flushed.append)
    add_agent(db, ws, status="processing")
    db.enqueue("g1", "also add tests", None)
    out = antigravity.handle_hook(db, "g1", "agy-stop", {"conversationId": "c1", "terminationReason": "NO_TOOL_CALL"})
    assert out is None and flushed == ["g1"]
    assert db.get_agent("g1").status == "idle" and db.get_agent("g1").session_ref == "c1"
    assert db.pending_count("g1") == 1   # typed in by the flush, as a new turn


def test_report_reminder_only_once_per_turn(db, ws):
    add_agent(db, ws, mode="assign", status="processing", profile="developer")
    out = antigravity.handle_hook(db, "g1", "agy-stop", {})
    assert out and "report_result" in out["reason"]
    assert antigravity.handle_hook(db, "g1", "agy-stop", {}) is None
    # A new turn resets it.
    antigravity.handle_hook(db, "g1", "agy-pre-invocation", {"invocationNum": 0})
    assert antigravity.handle_hook(db, "g1", "agy-stop", {}) is not None


def test_new_turn_is_the_user_unless_brindle_just_delivered(db, ws):
    add_agent(db, ws)
    db.add_autopilot("g1")
    autopilot.set_goal(db, "g1", "Goal", [("M", "true", None)])
    autopilot.need_user(db, "g1", "Which DB?")
    db.enqueue("g1", "Assigned task finished.", "w1")
    db.pop_pending("g1")
    antigravity.handle_hook(db, "g1", "agy-pre-invocation", {"invocationNum": 0})
    assert db.get_autopilot("g1").state == "blocked"
    assert db.get_agent("g1").status == "processing"
    with db.tx() as c:
        c.execute("UPDATE inbox SET delivered_at = delivered_at - 60")
    antigravity.handle_hook(db, "g1", "agy-pre-invocation", {"invocationNum": 0})
    assert db.get_autopilot("g1").state == "running"


def test_usage_limit_error_blocks_autopilot(db, ws):
    add_agent(db, ws, status="processing")
    db.add_autopilot("g1")
    autopilot.set_goal(db, "g1", "Goal", [("M", "true", None)])
    antigravity.handle_hook(db, "g1", "agy-stop", {"terminationReason": "error",
                                                   "error": "429 RESOURCE_EXHAUSTED: quota"})
    assert db.get_autopilot("g1").state == "blocked"


def test_paste_blocked():
    p = Antigravity()
    # Not interactive -> None
    assert p.paste_blocked("> hello", interactive=False) is None
    
    # Empty prompt -> None
    assert p.paste_blocked("> \n───\n? for shortcuts      Gemini", interactive=True) is None
    assert p.paste_blocked("   >    \n───\n? for shortcuts", interactive=True) is None
    
    # User has typed something -> typing
    assert p.paste_blocked("> hello world\n───\n? for shortcuts", interactive=True) == "typing"
    assert p.paste_blocked("  >  typing... \n───\n? for shortcuts", interactive=True) == "typing"
    
    # Text with ANSI escape codes
    ansi_empty = "\x1b[38;5;232m>\x1b[0m "
    ansi_typing = "\x1b[38;5;232m>\x1b[0m \x1b[1mhello\x1b[0m"
    assert p.paste_blocked(ansi_empty, interactive=True) is None
    assert p.paste_blocked(ansi_typing, interactive=True) == "typing"


