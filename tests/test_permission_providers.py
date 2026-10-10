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
from brindle.permissions import Decision, Rule, decide_all, from_agy, from_codex


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


PATCH = """*** Begin Patch
*** Add File: new.txt
+hello
*** Update File: src/app.py
@@
-a
+b
*** Delete File: old.txt
*** Update File: a.txt
*** Move to: b.txt
*** End Patch
"""


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


def test_codex_patch_is_a_request_per_file(ws):
    reqs = from_codex(codex_payload("apply_patch", {"command": PATCH}, ws.path), ws.path, ws.repo_root)
    got = [(r.kind, os.path.relpath(r.path, ws.path)) for r in reqs]
    assert got == [("write", "new.txt"), ("edit", "src/app.py"), ("write", "old.txt"),
                   ("edit", "a.txt"), ("write", "b.txt")]
    assert all(r.tool == "apply_patch" for r in reqs)
    # No file headers: one request without a path, which can only be ask.
    [r] = from_codex(codex_payload("apply_patch", {"command": "garbage"}, ws.path))
    assert r.kind == "edit" and r.path is None


@pytest.mark.parametrize("patch", [
    "*** Begin Patch\n*** Update File: a.py\n  *** Update File: /etc/hosts\n*** End Patch\n",
    "*** Begin Patch\n*** Update File: a.py\n\t*** Add File: b.py\n*** End Patch\n",
    "*** Begin Patch\n*** Update File: a.py\n*** update file: b.py\n*** End Patch\n",
    "*** Begin Patch\n*** Update File: a.py\n***Update File: b.py\n*** End Patch\n",
    "*** Begin Patch\n*** Update File: a.py\n*** Rename File: b.py\n*** End Patch\n",
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


def test_codex_output_shape():
    allow = permissions.codex_output(Decision("allow", "ok"))
    assert allow == {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                            "decision": {"behavior": "allow"}}}
    deny = permissions.codex_output(Decision("deny", "no"))
    assert deny["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert deny["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "brindle: no"}
    assert permissions.codex_output(Decision("ask", "?")) is None


# -- Codex: the hook ---------------------------------------------------------------------------


def test_codex_hook_decides_and_records(db, ws):
    a = worker(db, ws)
    turn_on(ws)
    out = agents.hook_main(db, a.id, "codex-permission-request",
                           json.dumps(codex_payload("Bash", {"command": "git push"}, ws.path)))
    assert json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    out = agents.hook_main(db, a.id, "codex-permission-request",
                           json.dumps(codex_payload("Bash", {"command": "git status"}, ws.path)))
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    # Ask: no output, and nothing kept to learn from (Codex has no tool_use_id).
    assert agents.hook_main(db, a.id, "codex-permission-request",
                            json.dumps(codex_payload("Bash", {"command": "make deploy"}, ws.path))) == ""
    assert db.latest_permission_request(a.id) is None
    rows = db.list_history(ws.repo_root, "permission")
    assert {r.result.split(":")[0] for r in rows} == {"deny", "allow", "ask"}


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


def test_a_removed_trust_stays_removed(ws, monkeypatch):
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    turn_on(ws)
    codex_hook.trust("codex", codex_hook.inspect("codex"))
    (codex_hook.codex_home() / "config.toml").write_text("")  # the person took it out
    assert codex_hook.status() == codex_hook.REMOVED
    assert codex_hook.config_flags()[1] not in launch_argv(ws)
    move_brindle(monkeypatch)  # not even after a reinstall
    assert codex_hook.launch_flags(ws.path) == []
    assert "sha256:abc" not in (codex_hook.codex_home() / "config.toml").read_text()
    assert "removed" in codex_hook.launch_warning("codex", ws.path)


def move_brindle(monkeypatch):
    """A reinstall: brindle's interpreter elsewhere, so a new hook command."""
    monkeypatch.setattr(codex_hook, "_stable_invocation",
                        lambda: ["/new/venv/bin/python", "-m", "brindle"])


def test_a_moved_brindle_carries_the_trust_over(ws, monkeypatch):
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    turn_on(ws)
    codex_hook.trust("codex", codex_hook.inspect("codex"))
    move_brindle(monkeypatch)
    assert codex_hook.status() == codex_hook.MOVED
    assert codex_hook.config_flags()[1] in codex_hook.launch_flags(ws.path)
    assert permissions.load_store().codex_hook["command"] == codex_hook.hook_command()
    assert codex_hook.status() == codex_hook.TRUSTED


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


def test_agy_mapping(ws):
    r = from_agy(agy_payload("run_command", {"CommandLine": '"git status"', "Cwd": json.dumps(ws.path)}),
                 ws.path, ws.repo_root)
    assert (r.provider, r.kind, r.command, r.cwd) == ("antigravity", "bash", "git status", ws.path)
    r = from_agy(agy_payload("view_file", {"AbsolutePath": "/etc/hosts"}))
    assert (r.kind, r.path) == ("read", "/etc/hosts")
    r = from_agy(agy_payload("write_to_file", {"TargetFile": "notes.md"}, ws.path))
    assert (r.kind, r.path) == ("write", os.path.join(ws.path, "notes.md"))
    assert from_agy(agy_payload("replace_file_content", {"TargetFile": "/x"})).kind == "edit"
    r = from_agy(agy_payload("read_url_content", {"Url": "https://example.com/a"}))
    assert (r.kind, r.url) == ("fetch", "https://example.com/a")
    r = from_agy(agy_payload("call_mcp_tool", {"ServerName": '"github"', "ToolName": '"create_issue"'}))
    assert (r.kind, r.tool) == ("mcp", "mcp__github__create_issue")
    assert from_agy(agy_payload("mcp_brindle_get_progress", {})).kind == "mcp"
    assert from_agy(agy_payload("browser_click", {})).kind == "other"
    assert from_agy({"stepIdx": 1}) is None
    assert from_agy({"toolCall": "x"}) is None


def test_agy_output_is_deny_or_ask():
    assert permissions.agy_output(Decision("deny", "no")) == {"decision": "deny", "reason": "brindle: no"}
    for d in (Decision("allow", "ok"), Decision("ask", "?"), None):
        out = permissions.agy_output(d)
        assert out["decision"] == "ask" and out["reason"]


# -- agy: the hook never answers nothing -------------------------------------------------------


def test_agy_pre_tool_denies_and_otherwise_asks(db, ws, monkeypatch):
    a = worker(db, ws, provider="antigravity", id_="g1")
    turn_on(ws)
    monkeypatch.setenv("BRINDLE_AGENT_ID", a.id)
    push = json.dumps(agy_payload("run_command", {"CommandLine": "git push", "Cwd": ws.path}, ws.path))
    out = json.loads(antigravity.pre_tool_main(push, db_factory=lambda: db))
    assert out["decision"] == "deny" and "git push" in out["reason"]
    status = json.dumps(agy_payload("run_command", {"CommandLine": "git status"}, ws.path))
    assert json.loads(antigravity.pre_tool_main(status, db_factory=lambda: db))["decision"] == "ask"
    [row] = db.list_history(ws.repo_root, "permission")
    assert row.result.startswith("deny:")


@pytest.mark.parametrize("stdin", ["", "{broken", "[]", "null", '{"toolCall": 5}', "\x00\xff"])
def test_agy_pre_tool_never_answers_nothing(db, ws, monkeypatch, stdin):
    a = worker(db, ws, provider="antigravity", id_="g1")
    turn_on(ws)
    monkeypatch.setenv("BRINDLE_AGENT_ID", a.id)
    assert json.loads(antigravity.pre_tool_main(stdin, db_factory=lambda: db))["decision"] == "ask"


def test_agy_pre_tool_failures_are_ask(db, monkeypatch):
    def broken_db():
        raise RuntimeError("no db")

    out = antigravity.pre_tool_main(json.dumps(agy_payload("run_command", {"CommandLine": "x"})),
                                    db_factory=broken_db)
    assert json.loads(out)["decision"] == "ask"
    # No agent (agy outside brindle, in a checkout brindle set up): ask, never empty.
    monkeypatch.setattr(antigravity, "agent_from_parent", lambda: None)
    assert json.loads(antigravity.pre_tool_main("{}", db_factory=lambda: db))["decision"] == "ask"
    # Even brindle's own answer-maker failing still answers ask.
    monkeypatch.setattr(permissions, "agy_output", lambda d: (_ for _ in ()).throw(RuntimeError()))
    assert json.loads(antigravity.pre_tool_main("{}", db_factory=lambda: db))["decision"] == "ask"


def test_agy_pre_tool_cli_always_prints_json(monkeypatch):
    monkeypatch.setattr(antigravity, "agent_from_parent", lambda: None)
    for stdin in ("", "garbage", json.dumps(agy_payload("run_command", {"CommandLine": "git push"}))):
        res = CliRunner().invoke(app, ["_hook", "agy-pre-tool"], input=stdin)
        assert res.exit_code == 0
        assert json.loads(res.stdout)["decision"] == "ask"


def test_agy_install_adds_the_pre_tool_hook_only_with_the_policy(ws, monkeypatch):
    monkeypatch.setattr(antigravity, "tool_names", lambda: ["report_result"])
    antigravity.install(ws.path)
    hooks = json.loads(open(os.path.join(ws.path, ".agents", "hooks.json")).read())["brindle"]
    assert "PreToolUse" not in hooks
    antigravity.install(ws.path, permission_policy=True)
    hooks = json.loads(open(os.path.join(ws.path, ".agents", "hooks.json")).read())["brindle"]
    [entry] = hooks["PreToolUse"]
    # File and web tools too, so every deny applies to them.
    for tool in ("run_command", "view_file", "write_to_file", "read_url_content", "call_mcp_tool"):
        assert tool in entry["matcher"].split("|")
    assert "agy-pre-tool" in entry["hooks"][0]["command"]


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
    out = strict_answer(db, ws, "run_command", {"CommandLine": "curl https://example.com"})
    assert out["decision"] == "deny" and "send_message" in out["reason"]
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


@pytest.mark.parametrize("stdin", ["", "{broken", "[]", "null", '{"toolCall": 5}'])
def test_agy_strict_fails_closed(db, ws, monkeypatch, stdin):
    a = worker(db, ws, provider="antigravity", id_="g1")
    turn_on(ws)
    monkeypatch.setenv("BRINDLE_AGENT_ID", a.id)
    assert json.loads(antigravity.pre_tool_main(stdin, db_factory=lambda: db, strict=True))["decision"] == "deny"


def test_agy_strict_failures_are_deny_unless_agy_plainly_prompts(db, monkeypatch):
    def broken_db():
        raise RuntimeError("no db")

    monkeypatch.setenv("BRINDLE_AGENT_ID", "g1")
    payload = json.dumps(agy_payload("run_command", {"CommandLine": "x"}))
    assert json.loads(antigravity.pre_tool_main(payload, db_factory=broken_db, strict=True))["decision"] == "deny"
    # An agent brindle doesn't know: deny.
    assert json.loads(antigravity.pre_tool_main(payload, db_factory=lambda: db, strict=True))["decision"] == "deny"
    monkeypatch.delenv("BRINDLE_AGENT_ID")
    monkeypatch.setattr(antigravity, "agent_from_parent", lambda: None)
    # No agent: the person's own agy in this checkout. Started without the
    # flag, it prompts, so ask; with the flag, or when that can't be told, deny.
    for launched, expected in ((False, "ask"), (True, "deny"), (None, "deny")):
        monkeypatch.setattr(antigravity, "_launched_with_skip_flag", lambda launched=launched: launched)
        assert json.loads(antigravity.pre_tool_main(payload, db_factory=lambda: db, strict=True))["decision"] == expected


def test_agy_strict_cli_event(monkeypatch):
    monkeypatch.setattr(antigravity, "agent_from_parent", lambda: None)
    monkeypatch.setattr(antigravity, "_launched_with_skip_flag", lambda: True)
    for stdin in ("", "garbage", json.dumps(agy_payload("run_command", {"CommandLine": "ls"}))):
        res = CliRunner().invoke(app, ["_hook", "agy-pre-tool-strict"], input=stdin)
        assert res.exit_code == 0
        assert json.loads(res.stdout)["decision"] == "deny"


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
    argv, pre = launch()
    assert antigravity.SKIP_FLAG not in argv and pre is None  # needs the policy on
    turn_on(ws)
    argv, [entry] = launch()
    assert antigravity.SKIP_FLAG in argv
    assert entry["matcher"] == "*" and "agy-pre-tool-strict" in entry["hooks"][0]["command"]
    set_local(ws.repo_root, "agy_approvals", "prompt")
    argv, [entry] = launch()
    assert antigravity.SKIP_FLAG not in argv and "agy-pre-tool-strict" not in entry["hooks"][0]["command"]


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


def test_agy_sync_creates_and_removes_its_own_file(agy_settings, ws):
    antigravity.sync_permissions(ws.repo_root, on=True, checks=[])
    assert json.loads(agy_settings.read_text())["permissions"]["allow"]
    assert not agy_settings.with_name("settings.json.brindle-backup").exists()
    antigravity.sync_permissions(ws.repo_root, on=False)
    assert not agy_settings.exists()


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
