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
    monkeypatch.setattr(antigravity, "_flush_soon", lambda agent_id: None)
    assert antigravity.hook_main(db, "agy-stop", "{}") == "{}"
    assert db.pending_count("g1") == 1   # the autopilot nudge, for g1
    db.set_status("g1", "processing")
    monkeypatch.setattr(antigravity, "_process_env", lambda pid: {})
    monkeypatch.setattr(antigravity, "_parent", lambda pid: None)
    assert antigravity.hook_main(db, "agy-stop", "{}") == "{}"
    assert db.get_agent("g1").status == "processing"


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


def test_agy_unreported_reminder_is_new_turn(db, ws, monkeypatch):
    # A turn agy's Stop hook continues has no MCP tools: the reminder must be typed in.
    flushed = []
    monkeypatch.setattr(antigravity, "_flush_soon", flushed.append)
    add_agent(db, ws, mode="review", status="processing", profile="reviewer")
    assert antigravity.handle_hook(db, "g1", "agy-stop", {}) is None
    assert flushed == ["g1"] and db.get_agent("g1").status == "idle"
    assert "submit_review" in db.pop_pending("g1").body   # what the flush types in
    # The reminder's own turn doesn't reset the cap: no second reminder.
    antigravity.handle_hook(db, "g1", "agy-pre-invocation", {"invocationNum": 0})
    assert antigravity.handle_hook(db, "g1", "agy-stop", {}) is None
    assert flushed == ["g1"] and db.pending_count("g1") == 0
    # A turn the user starts later does.
    with db.tx() as c:
        c.execute("UPDATE inbox SET delivered_at = delivered_at - 60")
    antigravity.handle_hook(db, "g1", "agy-pre-invocation", {"invocationNum": 0})
    antigravity.handle_hook(db, "g1", "agy-stop", {})
    assert flushed == ["g1", "g1"]


@pytest.mark.parametrize("error, flushes, told", [
    ("500 INTERNAL: backend error", True, "provider error (error) before it called report_result"),
    ("401 unauthorized", False, "authentication failure"),
])
def test_agy_error_stop_flushes_and_tells_the_parent(db, ws, monkeypatch, error, flushes, told):
    from brindle import agents

    flushed = []
    monkeypatch.setattr(antigravity, "_flush_soon", flushed.append)
    monkeypatch.setattr(agents, "is_alive", lambda agent: False)
    add_agent(db, ws, agent_id="boss", status="processing")
    db.add_agent(Agent("g1", ws.id, "developer", "antigravity", "boss", "assign", "processing", "@1",
                       None, time.time()))
    db.enqueue("g1", "also add tests", "boss")
    antigravity.handle_hook(db, "g1", "agy-stop", {"terminationReason": "error", "error": error})
    assert flushed == (["g1"] if flushes else [])
    note, = [m.body for m in iter(lambda: db.pop_pending("boss"), None)]
    assert told in note and "backend error" not in note   # never the provider's own text


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
    assert p.paste_blocked(">\n" + "continued draft\n" * 40 + "───\n? for shortcuts", interactive=True) == "typing"
    
    # Text with ANSI escape codes
    ansi_empty = "\x1b[38;5;232m>\x1b[0m "
    ansi_typing = "\x1b[38;5;232m>\x1b[0m \x1b[1mhello\x1b[0m"
    assert p.paste_blocked(ansi_empty, interactive=True) is None
    assert p.paste_blocked(ansi_typing, interactive=True) == "typing"


@pytest.mark.variant("live incident: agy's accept-edits hint held a new supervisor's first messages")
def test_new_supervisor_gets_its_queued_first_messages_over_the_mode_hint(db, ws, monkeypatch):
    # agy 1.3.5 started with --mode shows its mode as a hint in the empty
    # input box; the flush after its start must still type in the queue.
    from brindle import agents, tmux

    add_agent(db, ws, status="idle")
    db.enqueue("g1", "brindle instructions", None)
    db.enqueue("g1", "Say hi", None)
    screen = ("\x1b[38;5;111m>\x1b[39m \x1b[38;5;110mAccept-edits mode: file edits auto-approved "
              "(shift+tab to cycle)\x1b[39m\n" + "─" * 40 + "\n? for shortcuts   accept-edits · Gemini")
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: screen)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    assert agents.flush(db, "g1")
    assert pasted == ["brindle instructions"]


# -- agy_approvals "brindle": the strict PreToolUse hook --------------------------------------


def strict(db, ws, tool, args):
    """The strict hook's answer for one call by an agy reviewer-gemini worker."""
    if db.get_agent("r1") is None:
        db.add_agent(Agent("r1", ws.id, "reviewer-gemini", "antigravity", "boss", "assign", "processing",
                           "@0", None, time.time()))
    payload = {"toolCall": {"name": tool, "args": args}, "conversationId": "c", "workspacePaths": [ws.path]}
    return antigravity.pre_tool_decision(db, "r1", payload, strict=True)


@pytest.mark.parametrize("command,expected", [
    ("git diff HEAD~1..HEAD", "allow"),  # ~ makes it no simple command
    ('grep -rn "x" .', "allow"),  # quoted
    ("git diff a..b | grep x", "allow"),  # compound: each part is allowed
    ("cd sub && git status", "allow"),  # a cd inside the worktree
    pytest.param("git push", "deny", marks=pytest.mark.variant("a deny rule still wins")),
    ("git diff $(git merge-base main HEAD)", "deny"),  # what $(...) runs can't be seen
    ("git diff > notes.txt", "deny"),  # a redirection writes where no rule says
    ("git status |& curl https://example.com", "deny"),  # |& pipes like |
    ("git status\ncurl https://example.com", "deny"),  # a second line
    ("git status x#; curl https://example.com", "deny"),  # a # mid-word is no comment to the shell
])
def test_strict_allows_compound_commands_covered_by_allowlist(db, ws, command, expected):
    os.makedirs(os.path.join(ws.path, "sub"), exist_ok=True)
    assert strict(db, ws, "run_command", {"CommandLine": command})["decision"] == expected


def test_dont_ask_refuses_an_uncovered_command_instead_of_prompting(db, ws, monkeypatch):
    # GitHub #225: agy has no dontAsk mode, so reviewer-gemini sat at a prompt
    # for `git ls-tree` with nobody to answer it, the repo's policy off.
    monkeypatch.setenv("BRINDLE_AGY_BIN", "/bin/agy")
    monkeypatch.setattr(antigravity, "tool_names", lambda: ["report_result"])

    def launch(profile):
        argv = Antigravity().command(LaunchContext("r1", load_profile(profile), None, cwd=ws.path))
        hooks = json.loads(open(os.path.join(ws.path, ".agents", "hooks.json")).read())["brindle"]
        return argv, hooks["PreToolUse"][0]["hooks"][0]["command"]

    argv, hook = launch("reviewer-gemini")
    assert antigravity.SKIP_FLAG in argv and antigravity.STRICT_EVENT in hook

    def answer(agent_id, command):
        monkeypatch.setenv("BRINDLE_AGENT_ID", agent_id)
        stdin = json.dumps({"toolCall": {"name": "run_command", "args": {"CommandLine": command}},
                            "workspacePaths": [ws.path]})
        return json.loads(antigravity.pre_tool_main(stdin, db_factory=lambda: db, strict=True))["decision"]

    strict(db, ws, "list_dir", {})   # adds r1, the reviewer
    assert answer("r1", "git ls-tree -r --name-only main") == "deny"
    assert answer("r1", "git diff main...HEAD") == "allow"
    # Its worker relaunching in the same checkout keeps the reviewer's gate,
    # and, started without the flag, still gets agy's own prompt.
    argv, hook = launch("developer")
    assert antigravity.SKIP_FLAG not in argv and antigravity.STRICT_EVENT in hook
    add_agent(db, ws, agent_id="w1", mode="assign", profile="developer")
    monkeypatch.setattr(antigravity, "_launched_with_skip_flag", lambda: False)
    assert answer("w1", "git ls-tree -r --name-only main") == "ask"


def test_strict_allows_reading_agy_instructions(db, ws, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    instructions = tmp_path / ".gemini" / "antigravity-cli" / "mcp" / "brindle" / "instructions.md"
    instructions.parent.mkdir(parents=True)
    instructions.write_text("brindle runs other coding agents for you")
    assert strict(db, ws, "view_file", {"AbsolutePath": str(instructions)})["decision"] == "allow"
    assert strict(db, ws, "write_file", {"TargetFile": str(instructions)})["decision"] == "deny"
    other = instructions.parent.parent / "github" / "instructions.md"
    assert strict(db, ws, "view_file", {"AbsolutePath": str(other)})["decision"] == "deny"


