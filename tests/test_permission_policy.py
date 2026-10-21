"""The permission policy (brindle.permissions): brindle answers a worker's
tool-permission request from the structured request its CLI hands the hook,
allow / deny / ask, and learns only suggestions from what the person approves."""

import json
import os
import time

import pytest
from typer.testing import CliRunner

from brindle import agents, cull, permissions, workspaces
from brindle.cli import app
from brindle.config import set_local
from brindle.db import Agent
from brindle.permissions import Request, Rule, decide

from conftest import sh


@pytest.fixture
def ws(db, repo, monkeypatch):
    monkeypatch.setattr("brindle.permissions.PRESET_RULES", ())
    return workspaces.create(db, str(repo), "feature").workspace


def read(ws, path):
    return Request("claude", "read", "Read", ws.path, ws.path, ws.repo_root, path=str(path))


def bash(ws, command):
    return Request("claude", "bash", "Bash", ws.path, ws.path, ws.repo_root, command=command)


def worker(db, ws):
    a = Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "%w1", None, time.time())
    db.add_agent(a)
    return a


def turn_on(ws):
    set_local(ws.repo_root, "permission_policy", "on")


def claude_payload(tool, tool_input, tool_use_id="toolu_1", cwd=None):
    return {"tool_name": tool, "tool_input": tool_input, "tool_use_id": tool_use_id,
            "cwd": cwd, "hook_event_name": "PermissionRequest"}


# -- the engine -------------------------------------------------------------------------------


def _bypass(command, why):
    return pytest.param(command, "ask", marks=pytest.mark.variant(why))


@pytest.mark.parametrize("command, decision", [
    ('tmux -L sock list-panes -a -F "#{pane_id} #{pane_pid} #{pane_start_command}"', "allow"),
    pytest.param('tmux -L sock list-panes -F "#{pane_id}"', "allow",
                 marks=pytest.mark.variant("a tmux format without spaces is still literal")),
    _bypass('tmux -L sock list-panes -F "#{pane_id}" $(id)', "substitution after a quoted format"),
    _bypass('tmux -L sock list-panes -F "#{pane_id}" `id`', "backticks after a quoted format"),
    _bypass('tmux -L sock list-panes -F "#{pane_id}" > out', "redirection after a quoted format"),
    _bypass('tmux -L sock list-panes -F {pane_id,pane_pid}', "unquoted brace expansion is not literal"),
    ('git show main:a.sql && echo "=== B ===" && git show main:b.sql; git log | git show', "allow"),
    ("git status; rm -rf /", "ask"),
    ("git status && git push", "deny"),
    _bypass("git diff | sh", "a pipe into an uncovered command"),
    _bypass("git log $(rm x)", "command substitution"),
    _bypass("git log `id`", "backticks"),
    _bypass("git status && (rm x)", "a subshell"),
    _bypass("git diff <(rm x)", "process substitution"),
    _bypass("git show > out", "a redirection that writes a file"),
    _bypass("git status && echo x >> out", "an append after an allowed part"),
    _bypass("git show < ~/.ssh/id_rsa", "input from a file no read rule saw"),
    pytest.param('echo "a; rm -rf /"', "allow",
                 marks=pytest.mark.variant("a quoted separator is a literal argument")),
    _bypass('"./check --safe" && git status', "a quoted executable with spaces is another program"),
    _bypass("./check\\ --safe; git status", "so is an escaped one"),
    _bypass("git status || rm x", "an operator other than && ; |"),
    _bypass("git status & rm x", "backgrounding"),
    _bypass("git status\nrm x", "a newline"),
])
def test_a_compound_command_is_allowed_only_when_every_part_is(ws, command, decision):
    rules = [Rule("bash", "git", "prefix", "allow"), Rule("bash", "echo", "prefix", "allow"),
             Rule("bash", "tmux", "prefix", "allow"),
             Rule("bash", "./check --safe", "exact", "allow"), Rule("bash", "git push", "prefix", "deny")]
    assert decide(bash(ws, command), rules=rules).decision == decision


# -- the defaults -----------------------------------------------------------------------------


def test_untracked_ignored_and_dot_git_reads_are_not_allowed(ws):
    root = ws.path
    with open(os.path.join(root, "new.txt"), "w") as f:
        f.write("x")
    with open(os.path.join(root, "build.log"), "w") as f:
        f.write("x")
    with open(os.path.join(root, ".gitignore"), "a") as f:
        f.write("build.log\n")
    assert decide(read(ws, os.path.join(root, "new.txt"))).decision == "ask"
    assert decide(read(ws, os.path.join(root, "build.log"))).decision == "ask"
    assert decide(read(ws, os.path.join(ws.repo_root, ".git", "config"))).decision == "ask"
    assert decide(read(ws, root)).decision == "ask"  # a directory


def test_pathspec_magic_names_are_taken_literally(ws):
    for name in ("app.py*", "[a]pp.py", "*.py"):
        with open(os.path.join(ws.path, name), "w") as f:
            f.write("x")
        assert decide(read(ws, os.path.join(ws.path, name))).decision == "ask", name


def test_symlink_out_of_the_repo_is_not_allowed(ws, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    link = os.path.join(ws.path, "link.txt")
    os.symlink(outside, link)
    sh("git add link.txt && git commit -qm link", ws.path)  # even a tracked symlink
    assert decide(read(ws, link)).decision == "ask"


@pytest.mark.parametrize("match,match_type", [("src/*", "glob"), ("src/link.txt", "exact")])
def test_a_user_allow_rule_does_not_follow_a_symlink_out(ws, tmp_path, match, match_type):
    # An allow rule matches where the path really leads, not the name the
    # worker chose: a link under an allowed folder can point anywhere.
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    os.makedirs(os.path.join(ws.path, "src"), exist_ok=True)
    link = os.path.join(ws.path, "src", "link.txt")
    os.symlink(outside, link)
    absolute = os.path.join(os.path.realpath(ws.path), match)
    for value in (match, absolute):
        rule = Rule("read", value, match_type, "allow")
        assert decide(read(ws, link), rules=[rule]).decision == "ask", value
        # A deny still matches the name as given.
        assert decide(read(ws, link), rules=[Rule("read", value, match_type, "deny")]).decision == "deny"


def test_secret_locations_are_denied(ws):
    assert decide(read(ws, os.path.expanduser("~/.ssh/id_rsa"))).decision == "deny"
    assert decide(read(ws, os.path.expanduser("~/.aws/credentials"))).decision == "deny"
    assert decide(read(ws, os.path.join(ws.repo_root, ".env"))).decision == "deny"


def test_git_push_and_force_flags_are_denied(ws):
    for cmd in ("git push", "git push origin main", "cd x && git push", "git -C x push",
                "git checkout -f main", "git clean -fd", "git reset --force"):
        assert decide(bash(ws, cmd)).decision == "deny", cmd


def test_repo_rules_can_only_add_denies(ws):
    path = permissions.repo_rules_path(ws.repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rules": [
        {"kind": "bash", "match": "git log", "match_type": "prefix", "decision": "deny"},
        {"kind": "bash", "match": "rm", "match_type": "prefix", "decision": "allow"},
    ]}))
    assert decide(bash(ws, "git log")).decision == "deny"
    assert decide(bash(ws, "rm x")).decision == "ask"


# -- user rules and learning ------------------------------------------------------------------


def test_a_user_allow_never_overrides_a_default_deny(ws):
    permissions.add_rule("bash", "git push", "allow")
    assert decide(bash(ws, "git push")).decision == "deny"


def test_compound_commands_are_never_learned(ws):
    for _ in range(3):
        permissions.record_approval(bash(ws, "make a && make b"))
    assert permissions.suggestions() == []


def test_paths_are_learned_relative_to_the_worktree(ws):
    permissions.record_approval(read(ws, os.path.join(ws.path, "out", "log.txt")))
    permissions.record_approval(read(ws, os.path.join(ws.path, "out", "log.txt")))
    assert [s.match for s in permissions.suggestions()] == [os.path.join("out", "log.txt")]


# -- Claude Code's hook -----------------------------------------------------------------------


def test_malformed_input_gives_no_output(db, ws, monkeypatch):
    a = worker(db, ws)
    turn_on(ws)
    for text in ("{not json", "[1, 2]", "", json.dumps({"tool_input": "x"})):
        assert agents.hook_main(db, a.id, "permission-request", text) == "", text

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(permissions, "decide", boom)
    assert agents.hook_main(db, a.id, "permission-request",
                            json.dumps(claude_payload("Bash", {"command": "git status"}))) == ""


def test_claude_tool_names_map_to_kinds(ws):
    def kind(tool, ti=None):
        return permissions.from_claude(claude_payload(tool, ti or {}, cwd=ws.path), ws.path, ws.repo_root)

    assert kind("Grep").kind == "read" and kind("Grep").path == ws.path
    assert kind("Write", {"file_path": "x.py"}).path == os.path.join(ws.path, "x.py")
    assert kind("MultiEdit").kind == "edit" and kind("NotebookEdit").kind == "edit"
    assert kind("WebFetch", {"url": "https://x.dev"}).url == "https://x.dev"
    assert kind("mcp__github__create_pr").kind == "mcp"
    assert kind("Task").kind == "other"


def test_approval_after_ask_is_learned_and_end_of_turn_is_not(db, ws):
    a = worker(db, ws)
    turn_on(ws)
    for n in (1, 2):
        agents.hook_main(db, a.id, "permission-request",
                         json.dumps(claude_payload("Bash", {"command": "make docs"}, f"t{n}")))
        agents.hook_main(db, a.id, "tool-done", json.dumps({"tool_name": "Bash", "tool_use_id": f"t{n}"}))
    # Asked, then the turn ended without the tool running: not an approval.
    agents.hook_main(db, a.id, "permission-request",
                     json.dumps(claude_payload("Bash", {"command": "make clean"}, "t3")))
    agents.handle_hook(db, a.id, "stop", {"stop_hook_active": True})
    assert db.latest_permission_request(a.id) is None
    assert [(s.match, s.count) for s in permissions.suggestions()] == [("make docs", 2)]


def test_stuck_notice_names_the_pending_request(db, ws, monkeypatch):
    a = worker(db, ws)
    turn_on(ws)
    agents.hook_main(db, a.id, "permission-request", json.dumps(
        claude_payload("Bash", {"command": "make deploy"})))
    db.set_status(a.id, "waiting")
    db.update_agent(a.id, status_since=time.time() - 1000)
    sent = []
    monkeypatch.setattr(agents, "pane_owners", lambda db, panes: {})
    monkeypatch.setattr(agents, "owns_pane", lambda db, a, owners: True)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(cull.tmux, "capture", lambda *a, **k: "Do you want to proceed?")
    monkeypatch.setattr(agents, "send_message", lambda db, to, body, sender_id=None: sent.append(body))
    cull.note_stuck(db, time.time(), {})
    assert "Bash: make deploy" in sent[0] and "Only the user can answer it" in sent[0]

    # The supervisor reads a message's first line: the prompt itself is in it.
    prompt = "x/results resolves through a symlink to /repo/results, outside the allowed working directories"
    monkeypatch.setattr(cull.tmux, "capture", lambda *a, **k: f"Bash command\n{prompt}\nDo you want to proceed?")
    db.update_agent(a.id, stuck_noted=None)
    cull.note_stuck(db, time.time(), {})
    assert prompt in agents.one_line(db, 1, sent[1], a.id)


# -- the CLI ----------------------------------------------------------------------------------


def test_cli_rules_and_suggestions(ws, monkeypatch):
    monkeypatch.chdir(ws.repo_root)
    r = CliRunner()
    res = r.invoke(app, ["permissions", "allow", "bash", "make lint"])
    assert res.exit_code == 0, res.output
    rule_id = res.output.split(":")[0]
    assert r.invoke(app, ["permissions", "deny", "fetch", "https://evil.", "--prefix"]).exit_code == 0
    out = r.invoke(app, ["permissions", "list"]).output
    assert "permission_policy: off" in out
    assert "d-git-push" in out and "default" in out and f"{rule_id}" in out and "user" in out
    assert r.invoke(app, ["permissions", "allow", "nonsense", "x"]).exit_code == 2

    assert r.invoke(app, ["permissions", "suggestions"]).output.strip() == "no suggestions"
    permissions.record_approval(bash(ws, "make docs"))
    permissions.record_approval(bash(ws, "make docs"))
    sid = r.invoke(app, ["permissions", "suggestions"]).output.split()[0]
    assert r.invoke(app, ["permissions", "accept", sid]).exit_code == 0
    assert "learned" in r.invoke(app, ["permissions", "list"]).output
    assert r.invoke(app, ["permissions", "accept", "nope"]).exit_code == 1

    assert r.invoke(app, ["permissions", "forget", rule_id]).exit_code == 0
    assert r.invoke(app, ["permissions", "forget", "d-git-push"]).exit_code == 1

    res = r.invoke(app, ["permissions", "reset"], input="n\n")
    assert res.exit_code != 0 and permissions.load_store().rules
    assert r.invoke(app, ["permissions", "reset", "--yes"]).exit_code == 0
    assert permissions.load_store().rules == [] and permissions.load_store().approvals == {}
