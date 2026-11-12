import json
import os
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from conftest import sh

from brindle import agents, antigravity, autopilot, permissions, workspaces
from brindle.cli import app
from brindle.config import set_local
from brindle.db import Agent
from brindle.profiles import load_profile
from brindle.providers import Antigravity, LaunchContext


@pytest.fixture
def ws(db, repo):
    return workspaces.adopt_root(db, str(repo))


def add_agent(db, ws, agent_id="g1", mode="interactive", status="idle", profile="supervisor", parent=None,
              window="@0"):
    db.add_agent(Agent(agent_id, ws.id, profile, "antigravity", parent, mode, status, window, None, time.time()))


def test_command_passes_the_profiles_model_and_effort(db, ws, repo, monkeypatch):
    import dataclasses
    monkeypatch.setenv("BRINDLE_AGY_BIN", "/bin/agy")
    profile = dataclasses.replace(load_profile("developer"), model="gemini-3.1-pro-high", effort="high")
    argv = Antigravity().command(LaunchContext("g1", profile, None, cwd=str(repo), mode="interactive"))
    assert argv[:7] == ["/bin/agy", "--model", "gemini-3.1-pro-high", "--effort", "high", "--mode", "auto"]


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


@pytest.mark.parametrize("stop_after", ["ls-files", "rev-parse"])
def test_a_config_write_is_all_or_nothing_when_its_request_stops(repo, monkeypatch, stop_after):
    # Stopped once the tracked-file check answered: nothing is written. Stopped
    # once the file is written: its exclude still lands (the request committed).
    from brindle import spawn

    real = antigravity._git
    with spawn.deadline(30) as dl:
        def git(args, cwd):
            if args[0] == "rev-parse" and stop_after == "rev-parse":
                dl.stop()
            out = real(args, cwd)
            if args[0] == "ls-files" and stop_after == "ls-files":
                dl.stop()
            return out

        monkeypatch.setattr(antigravity, "_git", git)
        rel = antigravity.AGENTS_DIR / "mcp_config.json"
        if stop_after == "ls-files":
            with pytest.raises(spawn.Busy):
                antigravity._merge_json(str(repo), rel, "mcpServers", "brindle", {"command": "b"})
        else:
            antigravity._merge_json(str(repo), rel, "mcpServers", "brindle", {"command": "b"})
    assert (repo / rel).exists() == (stop_after == "rev-parse")
    assert ("/" + rel.as_posix() in (repo / ".git" / "info" / "exclude").read_text()) == (stop_after == "rev-parse")


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


def test_process_env_reads_another_process(tmp_path, monkeypatch):
    # The real extractor, fed fake OS output: /proc bytes where they exist, `ps eww` text where not.
    environ = tmp_path / "environ"
    environ.write_bytes(b"PATH=/bin\0BRINDLE_AGENT_ID=abc123\0junk\0")
    monkeypatch.setattr(antigravity, "Path", lambda p: environ)
    assert antigravity._process_env(42).get("BRINDLE_AGENT_ID") == "abc123"
    environ.unlink()                    # no /proc entry: ask ps
    ps = subprocess.CompletedProcess([], 0, stdout="agy --x PATH=/bin BRINDLE_AGENT_ID=abc123 BRINDLE_HOME=/h\n")
    monkeypatch.setattr(antigravity.subprocess, "run", lambda *a, **k: ps)
    assert antigravity._process_env(42) == {"BRINDLE_AGENT_ID": "abc123", "BRINDLE_HOME": "/h"}


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
    assert p.paste_blocked("> hello", interactive=False) is None
    assert p.paste_blocked("> \n───\n? for shortcuts      Gemini", interactive=True) is None
    assert p.paste_blocked("> hello world\n───\n? for shortcuts", interactive=True) == "typing"
    assert p.paste_blocked(">\n\n" + "continued draft\n" * 40 + "───\n? for shortcuts", interactive=True) == "typing"
    assert p.paste_blocked("\x1b[38;5;232m>\x1b[0m ", interactive=True) is None
    assert p.paste_blocked("\x1b[38;5;232m>\x1b[0m \x1b[1mhello\x1b[0m", interactive=True) == "typing"


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
    pytest.param("git diff HEAD^..HEAD", "allow", marks=pytest.mark.variant("review 95: literal caret revision")),
    pytest.param("git diff ~/file", "deny", marks=pytest.mark.variant("leading tilde expansion")),
    pytest.param("git diff path=~/file", "deny", marks=pytest.mark.variant("assignment tilde expansion")),
    pytest.param("git diff path=one:~/file", "deny", marks=pytest.mark.variant("assignment path tilde expansion")),
    ('grep -rn "x" .', "allow"),  # quoted
    ("git diff a..b | grep x", "allow"),  # compound: each part is allowed
    ("cd sub && git status", "allow"),  # a cd inside the worktree
    pytest.param("git push --force", "deny", marks=pytest.mark.variant("a deny rule still wins")),
    # Its output may be a word the *daemon.json* deny names, so no allow vouches for it.
    ("git diff $(git merge-base main HEAD)", "deny"),
    pytest.param('git diff --no-index /dev/null "$(cat notes.txt)"', "deny", marks=pytest.mark.variant(
        "bypass: output naming ~/.brindle/daemon.json, which d-daemon-sh denies written out, prints the token")),
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
    # for an uncovered command (`touch should_not_exist.txt` here) with nobody
    # to answer it, the repo's policy off.
    monkeypatch.setenv("BRINDLE_AGY_BIN", "/bin/agy")
    monkeypatch.setattr(antigravity, "tool_names", lambda: ["report_result"])

    def launch(profile, mode="assign"):
        argv = Antigravity().command(LaunchContext("r1", load_profile(profile), None, cwd=ws.path, mode=mode))
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
    assert answer("r1", "touch should_not_exist.txt") == "deny"
    assert answer("r1", "git diff main...HEAD") == "allow"
    # GitHub #233: with agy_approvals unset, a worker on any profile runs under
    # the same gate, its profile's allowed_tools applied as for a Claude
    # worker, and an uncovered command denied rather than left at a prompt:
    # its supervisor is told once per distinct request (never about the
    # dontAsk reviewer's or a hard deny), and once the user runs the allow
    # commands it was given, the worker's retry runs.
    add_agent(db, ws, agent_id="boss", window="")  # not running: the message waits in its inbox
    add_agent(db, ws, agent_id="w1", mode="assign", profile="developer", parent="boss")
    argv, _ = launch("developer")
    assert antigravity.SKIP_FLAG in argv
    assert answer("w1", "git commit -m wip") == "allow"
    compound = "touch one.txt && touch two.txt"
    long_a, long_b = "touch " + "x" * 250 + "a", "touch " + "x" * 250 + "b"  # one summary
    permissions.add_rule("bash", "rm protected.txt", "deny")
    approval = "touch approval.txt"
    permissions.add_rule("bash", approval, "ask")
    for command in (compound, compound, long_a, long_b, approval, "git push --force", "touch one.txt || rm protected.txt"):
        assert answer("w1", command) == "deny"
    stored = db.conn.execute("SELECT request FROM permission_requests WHERE agent_id='w1'").fetchall()
    assert sorted(json.loads(r["request"])["command"] for r in stored) == sorted([compound, long_a, long_b, approval])
    told = [r["body"] for r in db.conn.execute("SELECT body FROM inbox WHERE agent_id='boss' ORDER BY id")]
    assert len(told) == 4
    monkeypatch.delenv("BRINDLE_AGENT_ID")  # the person runs them, from their own terminal
    monkeypatch.setattr("brindle.cli.DB", lambda: db)
    for line in (told[0] + "\n" + told[3]).splitlines():
        if line.startswith("brindle permissions allow "):
            assert CliRunner().invoke(app, shlex.split(line)[1:]).exit_code == 0
    assert answer("w1", compound) == "allow"
    assert answer("w1", approval) == "allow"
    assert answer("w1", approval) == "deny"
    assert antigravity.SKIP_FLAG not in launch("developer", mode="interactive")[0]  # a person answers
    # An explicit "prompt" keeps agy's own prompt, beside the reviewer's gate.
    set_local(ws.repo_root, "agy_approvals", "prompt")
    argv, hook = launch("developer")
    assert antigravity.SKIP_FLAG not in argv and antigravity.STRICT_EVENT in hook
    monkeypatch.setattr(antigravity, "_launched_with_skip_flag", lambda: False)
    assert answer("w1", "touch should_not_exist.txt") == "ask"


@pytest.mark.slow("builds a real agy launch plan with per-repo policy lookups; two launches, ~20 policy decisions and six CLI runs; 7-17s on a loaded host")
def test_interactive_supervisor_on_autonomous_needs_no_grants(db, ws, monkeypatch, tmp_path):
    # A new person: the policy on and a fresh store on the default preset. agy
    # prompted for everything its settings.json didn't allow, and reads no
    # hook's allow, so the first run was a flood of grants.
    set_local(ws.repo_root, "permission_policy", "on")
    monkeypatch.setenv("BRINDLE_AGY_BIN", "/bin/agy")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "sup")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(antigravity, "tool_names", lambda: ["assign"])
    # Model the launch flag here; parent-process discovery has its own test.
    monkeypatch.setattr(antigravity, "_launched_with_skip_flag", lambda: True)
    monkeypatch.setattr(permissions, "DEFAULT_PRESET", "autonomous")
    roots = {
        ws.path: ws.path,
        str(tmp_path / "trunk-repo"): str(tmp_path / "trunk-repo"),
        str(tmp_path / "develop-repo"): str(tmp_path / "develop-repo"),
    }
    from brindle import git

    monkeypatch.setattr(git, "main_repo_root", lambda folder: roots.get(folder, ws.repo_root))
    # Default branch discovery is exercised separately; keep this policy
    # decision test off git subprocesses while preserving per-repo branches.
    monkeypatch.setattr(permissions, "default_branch", lambda root: {
        ws.repo_root: "main", str(tmp_path / "trunk-repo"): "trunk",
        str(tmp_path / "develop-repo"): "develop",
    }.get(root))
    # No agy process runs this hook here: the real `ps` walk up the ancestors
    # finds none and says "can't tell" (None), at about 100ms a call.
    monkeypatch.setattr(antigravity, "_launched_with_skip_flag", lambda: None)
    permissions.store_path().unlink(missing_ok=True)
    add_agent(db, ws, agent_id="sup", window="")
    # The hook's ancestors as agy runs it, read through a fake ps: a shell, then
    # the agy launch() starts with the skip flag. No real process table is walked.
    agy_pid = "4242"

    def ps(argv, **_):
        pid = argv[-1]
        if "ppid=" in argv:
            return SimpleNamespace(stdout=f"{agy_pid if pid != agy_pid else 1}\n")
        command = f"/bin/agy {antigravity.SKIP_FLAG}" if pid == agy_pid else "/bin/sh -c brindle _hook"
        return SimpleNamespace(stdout=command + "\n")

    monkeypatch.setattr(antigravity, "subprocess", SimpleNamespace(run=ps))

    def launch(**ctx):
        argv = Antigravity().command(LaunchContext("sup", load_profile("supervisor"), None, cwd=ws.path,
                                                   mode="interactive", **ctx))
        hooks = json.loads(open(os.path.join(ws.path, ".agents", "hooks.json")).read())["brindle"]
        return argv, hooks["PreToolUse"][0]["hooks"][0]["command"] if "PreToolUse" in hooks else ""

    def answer(tool, args):
        stdin = json.dumps({"toolCall": {"name": tool, "args": args}, "workspacePaths": [ws.path]})
        return json.loads(antigravity.pre_tool_main(stdin, db_factory=lambda: db, strict=True))

    argv, hook = launch()
    assert antigravity.SKIP_FLAG in argv and antigravity.STRICT_EVENT in hook
    # Resuming keeps the gate, so a supervisor comes back as it left.
    argv, hook = launch(resume="abc")
    assert antigravity.SKIP_FLAG in argv and antigravity.STRICT_EVENT in hook and argv[-2:] == ["--conversation", "abc"]

    outside = str(tmp_path / "elsewhere" / "notes.md")
    for tool, args in [
        ("mcp_brindle_assign", {}),
        ("call_mcp_tool", {"ServerName": "brindle", "ToolName": "assign"}),
        ("call_mcp_tool", {"ServerName": "github", "ToolName": "list_issues"}),
        ("run_command", {"CommandLine": "npm install left-pad"}),
        ("run_command", {"CommandLine": "touch one.txt && curl https://example.com > two.txt"}),
        ("run_command", {"CommandLine": "ls", "Cwd": str(tmp_path)}),
        ("view_file", {"AbsolutePath": outside}),
        ("write_to_file", {"TargetFile": outside}),
        ("read_url_content", {"Url": "https://example.com"}),
    ]:
        assert answer(tool, args)["decision"] == "allow", (tool, args)

    # What Autonomous stops stays stopped.
    for tool, args in [
        ("run_command", {"CommandLine": "git push --force origin feat"}),
        ("run_command", {"CommandLine": "git push --force origin feat", "Cwd": str(tmp_path)}),
        ("run_command", {"CommandLine": "ls -a ~/.ssh | wc -l"}),
        ("view_file", {"AbsolutePath": str(tmp_path / ".ssh" / "id_rsa")}),
        ("view_file", {"AbsolutePath": os.path.join(ws.path, ".env")}),
    ]:
        assert answer(tool, args)["decision"] == "deny", (tool, args)
    # An ask has no prompt to wait at here. With no app to hold it for it's refused ...
    pushes = ["git push origin main", "git push origin HEAD:release/1.0"]
    for push in pushes:
        assert answer("run_command", {"CommandLine": push})["decision"] == "deny"
    # The terminal remedy must actually release this exact call once.
    refused = answer("run_command", {"CommandLine": pushes[0]})
    commands = [line for line in refused["reason"].splitlines() if line.startswith("brindle permissions allow ")]
    assert len(commands) == 1
    assert CliRunner().invoke(app, shlex.split(commands[0])[1:]).exit_code == 1
    db.add_permission_request("sup", "transient", "{}", "unanswered prompt")
    antigravity.handle_hook(db, "sup", "agy-stop", {})
    assert all(r["tool_use_id"] != "transient" for r in db.permission_requests_of("sup"))
    with monkeypatch.context() as person:
        person.delenv("BRINDLE_AGENT_ID")
        person.setattr("brindle.cli.DB", lambda: db)
        for args in (["missing-request"], ["bash", "git push origin main"]):
            assert CliRunner().invoke(app, ["permissions", "allow", "--once", *args]).exit_code == 1
            assert permissions.load_store().saved == []
        denied = permissions.add_rule("bash", pushes[0], "deny")
        assert CliRunner().invoke(app, shlex.split(commands[0])[1:]).exit_code == 1
        assert permissions.load_store().saved == []
        permissions.forget(denied.id)
        result = CliRunner().invoke(app, shlex.split(commands[0])[1:])
        assert result.exit_code == 0, result.output
        assert CliRunner().invoke(app, shlex.split(commands[0])[1:]).exit_code == 1
    denied = permissions.add_rule("bash", pushes[0], "deny")
    assert answer("run_command", {"CommandLine": pushes[0]})["decision"] == "deny"
    permissions.forget(denied.id)
    assert answer("run_command", {"CommandLine": "git push --force origin main"})["decision"] == "deny"
    assert answer("run_command", {"CommandLine": pushes[1]})["decision"] == "deny"
    assert answer("run_command", {"CommandLine": pushes[0], "Cwd": str(tmp_path)})["decision"] == "deny"
    add_agent(db, ws, agent_id="other", window="")
    with monkeypatch.context() as other:
        other.setenv("BRINDLE_AGENT_ID", "other")
        assert answer("run_command", {"CommandLine": pushes[0]})["decision"] == "deny"
    antigravity.handle_hook(db, "other", "agy-stop", {"error": "provider error"})
    assert db.permission_requests_of("other") == []
    assert answer("run_command", {"CommandLine": pushes[0]})["decision"] == "allow"
    assert answer("run_command", {"CommandLine": pushes[0]})["decision"] == "deny"
    # The default branch a push rule means is that of the repo it runs in, however it gets there.
    trunk = tmp_path / "trunk-repo"
    trunk.mkdir()
    assert answer("run_command", {"CommandLine": "git push origin trunk", "Cwd": str(trunk)})["decision"] == "deny"
    assert answer("run_command", {"CommandLine": f"cd {trunk} && git push origin trunk"})["decision"] == "deny"
    assert answer("run_command", {"CommandLine": f"git -C {trunk} push origin trunk"})["decision"] == "deny"
    assert answer("run_command", {"CommandLine": "git push origin feat", "Cwd": str(trunk)})["decision"] == "allow"
    # Each push is judged in its own repo: joined, they get what they get alone.
    develop = tmp_path / "develop-repo"
    develop.mkdir()
    each = f"git -C {trunk} push origin develop && git -C {develop} push origin trunk"
    assert answer("run_command", {"CommandLine": each})["decision"] == "allow"
    held_back = f"git -C {trunk} push origin develop && git -C {develop} push origin develop"
    assert answer("run_command", {"CommandLine": held_back})["decision"] == "deny"
    permissions.forget("d-push-default")    # the preset's git/push-protected group still asks there
    assert answer("run_command", {"CommandLine": "git push origin trunk", "Cwd": str(trunk)})["decision"] == "deny"
    # An allow covers only what it names: one push never carries what's beside it.
    permissions.apply_preset("standard")
    permissions.forget("d-push-default")
    allow = permissions.add_rule("bash", f"*{permissions.PUSH_ARROW}{permissions.DEFAULT_BRANCH}", "allow", "push")
    to_trunk, to_develop = f"git -C {trunk} push origin trunk", f"git -C {develop} push origin develop"
    for command, expected in [
        (to_trunk, "allow"),
        (f"{to_trunk} && {to_develop}", "allow"),
        (f"git -C {develop} push origin feature", "deny"),
        (f"{to_trunk} && git -C {develop} push origin feature", "deny"),
        (f"{to_trunk} feature", "deny"),
        (f"{to_trunk} && touch one.txt", "deny"),
    ]:
        assert answer("run_command", {"CommandLine": command})["decision"] == expected, command
    permissions.forget(allow.id)
    permissions.apply_preset("autonomous")
    # ... and with one, the person's answer in its Inbox decides.
    from brindle.daemon import holds
    from brindle.service import inbox

    held = []
    monkeypatch.setattr(holds.HOLDS, "_holds", {})
    monkeypatch.setattr(holds.HOLDS, "_outcomes", {})
    monkeypatch.setattr(agents, "_pane_state", lambda agent: None)
    monkeypatch.setattr(holds.Holder, "register", lambda h, until: holds.HOLDS.register(
        h.agent_id, h.request_id, h.digest, h.nonce, until))
    monkeypatch.setattr(holds.Holder, "end", lambda h: holds.HOLDS.end(h.request_id, h.nonce))

    def person_answers(holder, seconds):
        [pending] = [p for p in inbox.pending_permissions(db, db.get_agent("sup")) if p.via == "hook"]
        assert pending.dto.request_id == holder.request_id
        held.append(pending.request.command)
        assert holds.HOLDS.claim("sup", holder.request_id, pending.dto.digest, "allow_once")
        assert holds.HOLDS.release("sup", holder.request_id)
        return holds.HOLDS.take(holder.request_id, holder.nonce)

    monkeypatch.setattr(holds.Holder, "wait", person_answers)
    assert [answer("run_command", {"CommandLine": push})["decision"] for push in pushes] == ["allow", "allow"]
    assert held == pushes

    # One started before the person chose Autonomous still has agy's own prompt to ask at.
    monkeypatch.setattr(antigravity, "_launched_with_skip_flag", lambda: False)
    assert answer("run_command", {"CommandLine": pushes[0]})["decision"] == "ask"

    # The policy off, another preset, or "prompt" leaves agy asking.
    set_local(ws.repo_root, "permission_policy", "off")
    assert antigravity.SKIP_FLAG not in launch()[0]
    set_local(ws.repo_root, "permission_policy", "on")
    permissions.apply_preset("standard")
    assert antigravity.SKIP_FLAG not in launch()[0]
    permissions.apply_preset("autonomous")
    assert antigravity.SKIP_FLAG in launch()[0]
    set_local(ws.repo_root, "agy_approvals", "prompt")
    assert antigravity.SKIP_FLAG not in launch()[0]


def test_strict_allows_reading_agy_instructions(db, ws, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    instructions = tmp_path / ".gemini" / "antigravity-cli" / "mcp" / "brindle" / "instructions.md"
    instructions.parent.mkdir(parents=True)
    instructions.write_text("brindle runs other coding agents for you")
    assert strict(db, ws, "view_file", {"AbsolutePath": str(instructions)})["decision"] == "allow"
    assert strict(db, ws, "write_file", {"TargetFile": str(instructions)})["decision"] == "deny"
    other = instructions.parent.parent / "github" / "instructions.md"
    assert strict(db, ws, "view_file", {"AbsolutePath": str(other)})["decision"] == "deny"


