"""The permission policy for Codex and Antigravity (agy) workers: their hook
payloads as brindle Requests, their answers, the one-time Codex hook trust, and
the copy of brindle's rules kept in agy's own settings."""

import json
import os
import time

import pytest
from typer.testing import CliRunner

from brindle import agents, antigravity, codex_hook, permissions, workspaces
from brindle.cli import app
from brindle.config import set_local
from brindle.db import Agent
from brindle.permissions import Rule, decide_all, from_codex


@pytest.fixture
def ws(db, repo, monkeypatch):
    monkeypatch.setattr("brindle.permissions.PRESET_RULES", ())
    return workspaces.create(db, str(repo), "feature").workspace


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
    "*** Begin Patch\n*** Update File: a.py\n  *** Update File: /etc/hosts\n*** End Patch\n",
    "*** Begin Patch\n*** Update File: a.py\n*** update file: b.py\n*** End Patch\n",
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
    turn_on(ws)
    assert agents.hook_main(db, a.id, "codex-permission-request", "{not json") == ""
    assert agents.hook_main(db, a.id, "codex-permission-request", "[1, 2]") == ""

    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(permissions, "decide_all", boom)
    assert agents.hook_main(db, a.id, "codex-permission-request", payload) == ""


@pytest.mark.parametrize("command,expected", [
    ("make verify", "allow"),  # repo check
    ("task test unit", "allow"),  # profile prefix allowance
    ("task test unit && deploy", None),
    ("task test secret", "deny"),  # profile deny beats profile allow
    ("cd {ws} && task test secret", "deny"),  # ...in every part of a compound command too
    ("task test secret 2>&1", "deny"),  # ...as the allow reads the part, redirection dropped
    ('task test "" secret', "deny"),  # ...and an empty word doesn't hide it
])
def test_codex_checks_and_profile_commands(db, ws, command, expected):
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
    a = worker(db, ws)
    out = agents.hook_main(db, a.id, "codex-permission-request",
                          json.dumps(codex_payload("Bash", {"command": command.format(ws=ws.path)}, ws.path)))
    assert (json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] if out else None) == expected


@pytest.mark.parametrize("command,expected", [
    ("gh issue view 239 --repo PawDelta/brindle", "allow"),
    pytest.param("gh pr diff 5 --repo PawDelta/brindle", "allow",
                 marks=pytest.mark.variant("each read-only gh subcommand is its own allow rule")),
    ("gh pr merge 5 --repo PawDelta/brindle", "deny"),  # dontAsk: writes stay refused
    pytest.param("gh api -X POST repos/PawDelta/brindle/issues", "deny",
                 marks=pytest.mark.variant("gh api writes are a distinct bypass attempt")),
])
def test_codex_reviewer_reads_github_but_cannot_write(db, ws, command, expected):
    turn_on(ws)
    a = Agent("r1", ws.id, "reviewer-codex", "codex", "boss", "review", "processing", "%r1", None, time.time())
    db.add_agent(a)
    out = agents.hook_main(db, a.id, "codex-permission-request",
                          json.dumps(codex_payload("Bash", {"command": command}, ws.path)))
    assert json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == expected


@pytest.mark.parametrize("command,expected", [("git push", "deny")])
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
    ("cd {ws} && git status", "allow"),
    ('grep -rn "x" .', "allow"),
    pytest.param("git log |& rm -rf x", None, marks=pytest.mark.variant("shlex reads |& as one word")),
    pytest.param("ls > ~/.zshrc", None, marks=pytest.mark.variant("redirection shlex doesn't split")),
])
def test_codex_compound_command_covered_is_allowed(db, ws, command, expected):
    turn_on(ws)
    assert codex_behavior(db, ws, "developer-codex", command.format(ws=ws.path)) == expected


def test_codex_dontask_turns_ask_into_deny(db, ws):
    # A heredoc's body isn't a command: uv run:* mustn't cover it, and the
    # unwatched reviewer must be refused rather than left at a prompt.
    turn_on(ws)
    command = "uv run python - <<'PY'\nimport os; os.remove('x')\nPY"
    assert codex_behavior(db, ws, "reviewer-codex", command) == "deny"


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
                             input=json.dumps(codex_payload("Bash", {"command": "git push"}, ws.path)))
    assert res.exit_code == 0
    assert json.loads(res.output)["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    res = CliRunner().invoke(app, ["_hook", "codex-permission-request"], input="garbage")
    assert res.exit_code == 0 and res.output == ""


# -- Codex: trusting the hook once -----------------------------------------------------------


def test_codex_app_server_uses_local_protocol_and_ignores_notifications(monkeypatch):
    from io import StringIO

    # initialize + hooks/list shapes observed from local codex-cli 0.162.1.
    hook = {"key": "/<session-flags>/config.toml:permission_request:0:0",
            "eventName": "permissionRequest", "handlerType": "command", "command": "true",
            "async": False, "matcher": None, "timeoutSec": 30, "source": "sessionFlags",
            "enabled": True, "isManaged": False, "currentHash": "sha256:abc", "trustStatus": "modified"}
    messages = [
        {"id": 1, "result": {"userAgent": "brindle/0.162.1", "platformFamily": "unix"}},
        {"method": "account/updated", "params": {"authMode": "chatgpt"}},
        {"id": 2, "result": {"data": [{"cwd": "/", "hooks": [hook], "warnings": [], "errors": []}]}},
    ]
    proc = _fake_codex_process(monkeypatch, StringIO("\n".join(json.dumps(m) for m in messages)))
    state = codex_hook.inspect("codex", "true")
    assert (state.command, state.key, state.hash, state.status) == (
        "true", hook["key"], "sha256:abc", "modified")
    sent = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
    assert [m["method"] for m in sent] == ["initialize", "initialized", "hooks/list"]
    assert proc.killed and proc.waited


def _fake_codex_process(monkeypatch, stdout):
    from io import StringIO
    from types import SimpleNamespace

    proc = SimpleNamespace(stdin=StringIO(), stdout=stdout, killed=False, waited=False)

    def kill():
        proc.killed = True

    def wait(**kwargs):
        proc.waited = True
        return 0

    proc.kill, proc.wait = kill, wait
    monkeypatch.setattr(codex_hook.subprocess, "Popen", lambda *a, **kw: proc)
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


class FakeAppServer:
    """Stands in for `codex app-server`: lists brindle's hook, and stores trust
    the way Codex does (config.toml hooks.state)."""
    calls: list = []

    def __init__(self, binary, flags, timeout=30.0):
        self.flags = flags

    def call(self, method, params):
        FakeAppServer.calls.append((method, params))
        config = codex_hook.codex_home() / "config.toml"
        trusted = config.exists() and "sha256:abc" in config.read_text()
        if method == "hooks/list":
            command = json.loads(self.flags[1].split("command=", 1)[1].split(",timeout=")[0])
            return {"data": [{"cwd": "/", "errors": [], "warnings": [], "hooks": [{
                "key": "/<session-flags>/config.toml:permission_request:0:0", "source": "sessionFlags",
                "command": command, "currentHash": "sha256:abc",
                "trustStatus": "trusted" if trusted else "untrusted"}]}]}
        if method == "config/batchWrite":
            [edit] = params["edits"]
            [(key, value)] = edit["value"].items()
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(f'[hooks.state."{key}"]\ntrusted_hash = "{value["trusted_hash"]}"\n')
            return {"status": "ok"}
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
    rec = permissions.load_store().codex_hook
    assert rec == {"command": codex_hook.hook_command(),
                   "key": "/<session-flags>/config.toml:permission_request:0:0", "hash": "sha256:abc"}
    assert codex_hook.trusted()
    assert "already trusted" in r.invoke(app, ["permissions", "install-codex-hook"]).output


def launch_argv(ws):
    from brindle.profiles import load_profile
    from brindle.providers import Codex, LaunchContext

    return Codex().command(LaunchContext("c1", load_profile("developer"), None, cwd=ws.path))


def test_codex_launch_trusts_and_passes_the_hook_when_the_policy_is_on(ws, monkeypatch):
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    flag = codex_hook.config_flags()[1]
    assert flag not in launch_argv(ws)  # off: nothing trusted, nothing passed
    assert codex_hook.status() == codex_hook.NEW
    turn_on(ws)
    a = launch_argv(ws)  # on: brindle trusts its own hook the first time
    assert a[a.index(flag) - 1] == "-c"
    assert "--agent" not in flag  # one command for every agent: one trust
    assert codex_hook.status() == codex_hook.TRUSTED


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


def move_brindle(monkeypatch):
    """A reinstall: brindle's interpreter elsewhere, so a new hook command."""
    monkeypatch.setattr(codex_hook, "_stable_invocation",
                        lambda: ["/new/venv/bin/python", "-m", "brindle"])


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
    (codex_hook.codex_home() / "config.toml").write_text("")
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


@pytest.mark.parametrize("strict,expected", [(False, "ask"), (True, "deny")])
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


@pytest.mark.parametrize("strict,expected,hook_name", [(False, "ask", "agy-pre-tool"), (True, "deny", "agy-pre-tool-strict")])
def test_agy_cli_always_prints_json(monkeypatch, strict, expected, hook_name):
    monkeypatch.setattr(antigravity, "agent_from_parent", lambda: None)
    if strict:
        monkeypatch.setattr(antigravity, "_launched_with_skip_flag", lambda: True)
    for stdin in ("", "garbage", json.dumps(agy_payload("run_command", {"CommandLine": "git push"}))):
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

    assert decision("run_command", {"CommandLine": "git push"}) == "deny"
    assert decision("view_file", {"AbsolutePath": os.path.join(ws.path, ".env")}) == "deny"
    assert decision("run_command", {"CommandLine": "curl https://example.com"}) == "deny"
    assert decision("view_file", {"AbsolutePath": str(outside)}) == "deny"
    assert decision("read_url_content", {"Url": "https://example.com"}) == "deny"
    assert decision("call_mcp_tool", {"ServerName": "github", "ToolName": "create_issue"}) == "deny"
    assert decision("browser_click", {}) == "deny"  # a tool brindle doesn't know
    rows = db.list_history(ws.repo_root, "permission")
    assert len(rows) == 9 and all(r.result.startswith("deny:") for r in rows)


def test_agy_strict_closes_the_ways_around_the_hook(db, ws, monkeypatch):
    a = worker(db, ws, provider="antigravity", id_="g1")
    turn_on(ws)
    monkeypatch.setenv("BRINDLE_AGENT_ID", a.id)

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
    # for a Claude Code worker; a deny still wins, and nothing compound passes.
    assert decision("run_command", {"CommandLine": "ls -la"}) == "allow"
    assert decision("run_command", {"CommandLine": "ls"}) == "allow"
    assert decision("run_command", {"CommandLine": "uv run pytest tests/test_x.py -q"}) == "allow"
    assert decision("run_command", {"CommandLine": "lsof -i"}) == "deny"
    assert decision("run_command", {"CommandLine": "ls; curl https://example.com"}) == "deny"
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
    assert "command(git push)" in deny and f"read_file({os.path.expanduser('~/.ssh')})" in deny
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
    out = permissions.mirror_agy([*permissions.DEFAULT_RULES], ["make check", "a && b"])
    # agy's settings apply to every project, and a repo's check runs what
    # that repo defines: allowed for brindle workers in that repo, never
    # everywhere agy runs.
    assert not any("make check" in e or "a && b" in e for e in out["allow"])
    assert "command(regex:^git status$)" in out["allow"]
    assert not any("tracked" in e for e in out["allow"])
