import dataclasses
import fnmatch
import json
import os
import time

import pytest

from brindle import antigravity, opencode, permissions, pipeline, workspaces
from brindle.db import Agent
from brindle.profiles import load_profile
from brindle.providers import DELIVERY_NOTE, LaunchContext, OpenCode, brindle_invocation


@pytest.fixture
def ws(db, repo):
    return workspaces.adopt_root(db, str(repo))


def _config(argv):
    assert argv[0] == "env" and argv[1].startswith("OPENCODE_CONFIG_CONTENT=")
    return json.loads(argv[1].split("=", 1)[1])


def test_launch_gives_opencode_brindles_server_plugin_instructions_and_brief(brindle_home, monkeypatch):
    monkeypatch.setenv("BRINDLE_OPENCODE_BIN", "/bin/opencode")
    profile = load_profile("reviewer-opencode")
    argv = OpenCode().command(LaunchContext("o1", profile, "review it", resume="ses_1"))
    cfg = _config(argv)
    assert argv[2:] == ["/bin/opencode", "--session", "ses_1", "--prompt", "review it"]
    server = cfg["mcp"]["brindle"]
    assert server["command"][-1] == "mcp" and server["environment"]["BRINDLE_AGENT_ID"] == "o1"
    plugin = cfg["plugin"][0].removeprefix("file://")
    assert f"const BRINDLE = {json.dumps(brindle_invocation())}" in open(plugin).read()
    instructions = open(cfg["instructions"][0]).read()
    assert profile.prompt in instructions and DELIVERY_NOTE in instructions
    # dontAsk: only the profile's commands run; the catch-all comes first (last match wins).
    bash = cfg["permission"]["bash"]
    assert list(bash)[0] == "*" and bash["*"] == "deny"
    assert bash["git diff"] == bash["git diff *"] == bash["pwd"] == "allow" and "pwd *" not in bash
    assert cfg["permission"]["edit"] == cfg["permission"]["webfetch"] == "deny"


@pytest.mark.parametrize("mode, model, perm, flags, patterns, decision", [
    ("bypassPermissions", "opencode/big-pickle", {"bash": "allow", "edit": "allow"}, ["--model", "opencode/big-pickle"], ["git push"], "allow"),
    ("plan", "claude-sonnet-4-5", {"edit": "allow", "*": "ask"}, ["--model", "anthropic/claude-sonnet-4-5", "--agent", "plan"], ["git status"], "allow"),
    (None, "gpt-5.1", {"edit": "ask", "*": "ask"}, ["--model", "openai/gpt-5.1"], ["git status --short --branch", "git branch -a", "git log --oneline -3"], "allow"),
    ("auto", "local-model", {"edit": "allow", "*": "ask"}, ["--model", "local-model"], ["git -C /worktree status --short --branch", "ls /shared/cache"], "allow"),
    pytest.param(None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
                 ["git -C . log --oneline -3", "git -C /shared/cache diff", "git -C . stash list"], "allow",
                 marks=pytest.mark.variant("relative -C and profile directory from the supervisor incident")),
    pytest.param(None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
                 ["git -C '/read only' status", 'git -C "/read only" diff'], "allow",
                 marks=pytest.mark.variant("quoted directory spellings must retain literal argument boundaries")),
    pytest.param(None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
                 ["git -C /unsafe/path status"], "ask",
                 marks=pytest.mark.variant("a glob in a configured directory must not allow arbitrary paths")),
    pytest.param(None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
                 ["git -C /worktree checkout status file"], "ask",
                 marks=pytest.mark.variant("bypass attempt against a wildcard in the -C path")),
    pytest.param("dontAsk", "local-model", {"edit": "deny", "*": "deny"}, ["--model", "local-model"],
                 ["git -C /worktree status", "git push origin main"], "deny",
                 marks=pytest.mark.variant("a compound command must not hide a write after a permitted read")),
])
def test_permission_mode_and_model_map_onto_opencode(brindle_home, mode, model, perm, flags, patterns, decision):
    profile = dataclasses.replace(load_profile("developer"), permission_mode=mode, model=model,
                                  add_dirs=["/shared/cache/"], read_dirs=["/read only", "/unsafe*"])
    argv = OpenCode().command(LaunchContext("o1", profile, None, cwd="/worktree"))
    got = _config(argv)["permission"]
    assert argv[3:] == flags
    assert got["edit"] == perm["edit"]
    if mode == "bypassPermissions":
        assert got["bash"] == got["external_directory"] == "allow"
    else:
        assert got["bash"]["*"] == perm["*"] and got["bash"]["uv run *"] == "allow"
        assert got["external_directory"] == {"*": perm["*"], "/shared/cache/*": "allow",
                                              "/read only/*": "allow", "/unsafe*/*": "allow"}
        # OpenCode 1.19.3 emits one pattern per simple command in ; / && lists,
        # retaining git's -C option. The last matching rule wins for each one.
        decisions = [next(action for pattern, action in reversed(got["bash"].items())
                          if fnmatch.fnmatchcase(command, pattern)) for command in patterns]
        assert ("deny" if "deny" in decisions else "ask" if "ask" in decisions else "allow") == decision


def add_agent(db, ws, mode="assign", status="processing", profile="developer-opencode"):
    db.add_agent(Agent("o1", ws.id, profile, "opencode", None, mode, status, "@0", None, time.time(), task="t"))


def test_plugin_events_drive_status_and_the_end_of_a_turn(db, ws, monkeypatch):
    flushed = []
    monkeypatch.setattr(antigravity, "_flush_soon", flushed.append)
    add_agent(db, ws, status="starting")
    hook = lambda event, payload: opencode.hook_main(db, "o1", event, json.dumps(payload))  # noqa: E731
    hook("opencode-prompt", {"session_id": "ses_a", "prompt": "do it"})
    assert db.get_agent("o1").status == "processing" and db.get_agent("o1").session_ref == "ses_a"
    hook("opencode-permission", {"session_id": "ses_a"})
    assert db.get_agent("o1").status == "waiting"
    hook("opencode-permission-replied", {"session_id": "ses_a"})
    assert db.get_agent("o1").status == "processing"
    # A worker that stops without reporting is reminded once, as a new turn.
    hook("opencode-stop", {"session_id": "ses_a", "error": None})
    assert flushed == ["o1"] and db.get_agent("o1").stop_blocked
    assert "report_result" in db.pop_pending("o1").body
    hook("opencode-prompt", {"session_id": "ses_a", "prompt": "reminder"})
    hook("opencode-stop", {"session_id": "ses_a"})
    assert flushed == ["o1"] and db.pending_count("o1") == 0
    # A turn that ends on the provider's limit puts OpenCode on cooldown.
    from brindle import quota

    hook("opencode-stop", {"session_id": "ses_a", "error": "APIError: 429 rate limit exceeded"})
    assert quota.get("opencode").limited_until > time.time()
    assert opencode.hook_main(db, None, "opencode-stop", "not json") == ""


def test_permission_policy_denies_through_the_plugins_pre_tool_hook(db, ws, brindle_home, monkeypatch):
    from brindle.config import set_local

    add_agent(db, ws)
    pre = lambda tool, args: opencode.hook_main(  # noqa: E731
        db, "o1", "opencode-pre-tool", json.dumps({"session_id": "ses_a", "tool": tool, "args": args}))
    denied = {"tool": "bash", "args": {"command": "git push origin main"}}
    assert pre(**denied) == ""   # policy off: OpenCode's own rules apply
    set_local(ws.repo_root, "permission_policy", "on")
    answer = json.loads(pre(**denied))
    assert answer["decision"] == "deny" and answer["reason"].startswith("brindle:")
    assert json.loads(pre("read", {"filePath": "~/.ssh/id_rsa"}))["decision"] == "deny"
    assert pre("bash", {"command": "git status"}) == ""   # allow and ask leave the call to OpenCode
    # A denied part of a compound command, and a denied file in an apply_patch (GPT-5 models).
    permissions.add_rule("bash", "make secret", "deny")
    permissions.add_rule("edit", "*/locked.txt", "deny", "glob")
    assert json.loads(pre("bash", {"command": "cd sub && make secret"}))["decision"] == "deny"
    patch = "*** Begin Patch\n*** Add File: ok.txt\n+a\n*** Update File: sub/locked.txt\n+b\n*** End Patch"
    assert json.loads(pre("apply_patch", {"patchText": patch}))["decision"] == "deny"
    # The plugin is only told to ask while the policy is on.
    monkeypatch.setenv("BRINDLE_OPENCODE_BIN", "/bin/opencode")
    argv = OpenCode().command(LaunchContext("o1", load_profile("developer-opencode"), None, cwd=ws.path))
    assert "BRINDLE_OPENCODE_POLICY=1" in argv
    # The instructions file goes when the agent's workspace does.
    path = opencode.write_instructions("o1", "x")
    pipeline.on_workspace_cleanup(db, ws)
    assert not os.path.exists(path)


def test_installed_opencode_is_ready_to_supervise_without_a_sign_in(monkeypatch):
    from brindle import doctor

    monkeypatch.setenv("BRINDLE_OPENCODE_BIN", "/nonexistent/opencode")
    assert any("npm install -g opencode-ai" in p for p in doctor.preflight("opencode"))
    monkeypatch.setenv("BRINDLE_OPENCODE_BIN", "/bin/sh")
    assert not any("OpenCode" in p for p in doctor.preflight("opencode"))
    from brindle import providers

    assert providers.seen_signed_in("opencode")   # so `brindle -s opencode` opens no login screen


@pytest.mark.parametrize("footer, state", [
    ("  △ Permission required\n   Allow once   Allow always   Reject", "waiting"),
    ("   ■⬝⬝⬝  esc interrupt        tab agents  ctrl+p commands", "busy"),
    ("   ~/proj    20.5K (10%)    ctrl+p commands    • OpenCode 1.19.3", "idle"),
    ("Loading...", None),
])
def test_screen_state_reads_opencodes_footer(footer, state):
    screen = "  ┃  esc interrupt was mentioned in the transcript\n" * 3 + "\n" * 10 + footer
    assert OpenCode().screen_state(screen) == state
    assert OpenCode().input_ready(screen) == (state in ("busy", "idle"))
