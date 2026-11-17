"""The permission policy (brindle.permissions): brindle answers a worker's
tool-permission request from the structured request its CLI hands the hook,
allow / deny / ask, and learns only suggestions from what the person approves."""

import dataclasses
import json
import os
import time

import pytest
from typer.testing import CliRunner

from brindle import agents, codex_hook, cull, permission_grants, permissions, workspaces
from brindle.cli import app
from brindle.config import POLICY_DEFAULT_KEY, forget_user, load_repo_config, set_local, set_user
from brindle.daemon import routes_actions
from brindle.db import DB, Agent
from brindle.permissions import Request, Rule, decide
from brindle.profiles import load_profile
from brindle.providers import ClaudeCode, LaunchContext
from brindle.service import permissions as svc

from conftest import sh
from workspace_template import copy_workspace, workspace_template  # noqa: F401 (a fixture)


@pytest.fixture
def ws(db, workspace_template, tmp_path, monkeypatch):
    monkeypatch.setattr("brindle.permissions.PRESET_RULES", ())
    return copy_workspace(workspace_template, db, tmp_path)


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


def claude_payload(tool, tool_input, cwd=None):
    """Claude Code's PermissionRequest input, which has no tool_use_id."""
    return {"tool_name": tool, "tool_input": tool_input, "cwd": cwd, "hook_event_name": "PermissionRequest"}


# -- who the policy is on for -----------------------------------------------------------------


@pytest.mark.variant("the owner's decision: a home from before the default gets the rules; an explicit off or preset stays")
@pytest.mark.parametrize("before, policy, source, preset", [
    (lambda repo: None, "on", "default", "autonomous"),
    (lambda repo: DB(), "on", "default", "autonomous"),
    (lambda repo: permissions.apply_preset("secure"), "on", "default", "secure"),
    (lambda repo: set_user(POLICY_DEFAULT_KEY, "on"), "on", "default", "autonomous"),
    (lambda repo: set_user("permission_policy", "off"), "off", "user", "autonomous"),
    (lambda repo: set_local(repo, "permission_policy", "off"), "off", "repo", "autonomous"),
])
def test_existing_free_user_gets_the_policy(repo, monkeypatch, before, policy, source, preset):
    """On any plan or an unreadable license: on unless someone said off, a preset there
    stays, and it's still no one's choice, so Codex falls back rather than refuse."""
    monkeypatch.delenv("BRINDLE_POLICY_DEFAULT_OFF", raising=False)
    monkeypatch.setattr("brindle.permissions.DEFAULT_PRESET", "autonomous")
    monkeypatch.setattr("brindle.pro.license.has", lambda *_: 1 / 0)
    before(repo)
    cfg = load_repo_config(repo)
    assert (cfg.permission_policy, cfg.permission_policy_source) == (policy, source)
    assert permissions.load_store().preset == preset
    assert codex_hook.policy_chosen(str(repo)) == (source != "default")
    assert svc.policy_dto(repo_root=str(repo)).policy_on == (policy == "on")


@pytest.mark.variant("PR 371: fresh free accounts need policy enforcement for interactive supervisors")
def test_new_free_user_gets_the_policy(repo, monkeypatch):
    """Nothing of brindle's on this machine and no plan, from the app's daemon: the
    policy is on with Autonomous, and it answers a supervisor on Claude and on
    Codex, which no mode or parent_id gate drops."""
    monkeypatch.delenv("BRINDLE_POLICY_DEFAULT_OFF", raising=False)
    monkeypatch.setattr("brindle.permissions.DEFAULT_PRESET", "autonomous")
    db = DB()
    assert load_repo_config(repo).permission_policy == "on"
    assert permissions.load_store().preset == "autonomous"

    ws = workspaces.create(db, str(repo), "feature").workspace
    ask = {"tool_name": "Bash", "tool_input": {"command": "some-tool --go"}, "cwd": ws.path}
    claude = Agent("s1", ws.id, "developer", "claude", None, "interactive", "processing", "%1", None, time.time())
    db.add_agent(claude)
    answer = agents.permission_request_decision(db, claude, {**ask, "hook_event_name": "PermissionRequest"})
    assert answer["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    codex = Agent("s2", ws.id, "developer", "codex", None, "interactive", "processing", "%2", None, time.time())
    db.add_agent(codex)
    answer = agents.permission_request_decision(
        db, codex, {"tool_name": "Bash", "tool_input": {"command": "some-tool --go"}, "cwd": ws.path}, provider="codex")
    assert answer["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    monkeypatch.setattr(codex_hook, "ensure", lambda **_: codex_hook.TRUSTED)
    assert codex_hook.launch_flags(ws.path)   # a supervisor launches with the hook too


@pytest.mark.parametrize("before,repo_says,preset,policy,source", [
    pytest.param(None, None, "secure", "on", "user",
                 marks=pytest.mark.variant("explicit Secure selection enables policy despite its previous default")),
    # The default preset (the suite's is Standard) isn't choosing the switch: Codex can still fall back.
    (None, None, "standard", "on", "default"),
    ("off", None, "secure", "off", "user"),
    (None, "off", "secure", "off", "repo"),
])
def test_a_preset_turns_the_policy_on(repo, db, monkeypatch, before, repo_says, preset, policy, source):
    """In the app and with `brindle permissions preset`, each from the same start,
    unless the person's own "off" or the repo's setting says otherwise."""
    monkeypatch.chdir(repo)
    if repo_says:
        set_local(repo, "permission_policy", repo_says)

    def from_the_start():
        forget_user("permission_policy")
        forget_user(POLICY_DEFAULT_KEY)
        if before:
            set_user("permission_policy", before)

    def effective():
        cfg = load_repo_config(repo)
        return cfg.permission_policy, cfg.permission_policy_source

    from_the_start()
    res = CliRunner().invoke(app, ["permissions", "preset", preset])
    assert res.exit_code == 0 and f"permission policy: {policy}" in res.output, res.output
    assert ("aren't in force" in res.output) == (policy == "off")
    assert effective() == (policy, source)

    from_the_start()
    reply = routes_actions.set_permission_preset(db, {}, {"preset": preset})
    assert effective() == (policy, source)
    # The reply is the person's own state: a repo's off is only that repo's.
    assert reply["policy_on"] == (before != "off")
    assert [p["name"] for p in reply["presets"] if p["current"]] == ([preset] if before != "off" else [])


# -- the engine -------------------------------------------------------------------------------


def _bypass(command, why):
    return pytest.param(command, "ask", marks=pytest.mark.variant(why))


@pytest.mark.parametrize("command, decision", [
    pytest.param('tmux -L sock list-panes -a -F "#{pane_id} #{pane_pid} #{pane_start_command}"', "allow",
                 marks=pytest.mark.variant("shell syntax must preserve the decision of every part")),
    pytest.param('tmux -L sock list-panes -F "#{pane_id}"', "allow",
                 marks=pytest.mark.variant("a tmux format without spaces is still literal")),
    _bypass('tmux -L sock list-panes -F "#{pane_id}" $(id)', "substitution after a quoted format"),
    _bypass('tmux -L sock list-panes -F "#{pane_id}" `id`', "backticks after a quoted format"),
    _bypass('tmux -L sock list-panes -F "#{pane_id}" > out', "redirection after a quoted format"),
    _bypass('tmux -L sock list-panes -F {pane_id,pane_pid}', "unquoted brace expansion is not literal"),
    pytest.param("git status; rm -rf /", "ask",
                 marks=pytest.mark.variant("shell syntax must preserve the decision of every part")),
    pytest.param("git status && git push", "deny",
                 marks=pytest.mark.variant("shell syntax must preserve the decision of every part")),
    _bypass("git diff | sh", "a pipe into an uncovered command"),
    _bypass("git log $(rm x)", "command substitution"),
    _bypass("git log `id`", "backticks"),
    _bypass("git status && (rm x)", "a subshell"),
    _bypass("git diff <(rm x)", "process substitution"),
    _bypass("git show > out", "a redirection that writes a file"),
    _bypass("git status && echo x >> out", "an append after an allowed part"),
    pytest.param("git show < ~/.ssh/id_rsa", "deny",
                 marks=pytest.mark.variant("input from a credential store is a hard deny before any read rule")),
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


@pytest.mark.variant("tracked-file allowance must exclude untracked, ignored and Git metadata reads")
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
    assert decide(read(ws, os.path.expanduser("~/.ssh/id_rsa"))).decision == "deny"


@pytest.mark.variant("Git pathspec metacharacters in file names must not broaden read approval")
def test_pathspec_magic_names_are_taken_literally(ws):
    for name in ("app.py*", "[a]pp.py", "*.py"):
        with open(os.path.join(ws.path, name), "w") as f:
            f.write("x")
        assert decide(read(ws, os.path.join(ws.path, name))).decision == "ask", name


@pytest.mark.variant("even a tracked symlink cannot authorize an outside read")
def test_symlink_out_of_the_repo_is_not_allowed(ws, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    link = os.path.join(ws.path, "link.txt")
    os.symlink(outside, link)
    sh("git add link.txt && git commit -qm link", ws.path)  # even a tracked symlink
    assert decide(read(ws, link)).decision == "ask"


@pytest.mark.parametrize("match,match_type", [pytest.param("src/*", "glob",
                 marks=pytest.mark.variant("a user path rule must not allow a symlink escape")), pytest.param("src/link.txt", "exact",
                 marks=pytest.mark.variant("a user path rule must not allow a symlink escape"))])
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


def test_repo_rules_can_only_add_denies(ws):
    path = permissions.repo_rules_path(ws.repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rules": [
        {"kind": "bash", "match": "git log", "match_type": "prefix", "decision": "deny"},
        {"kind": "bash", "match": "rm", "match_type": "prefix", "decision": "allow"},
        {"kind": "fetch", "match": "https://private", "decision": "deny", "roles": ["admin"]},
    ]}))
    assert decide(bash(ws, "git log")).decision == "deny"
    assert decide(bash(ws, "rm x")).decision == "ask"
    assert decide(Request("claude", "fetch", "WebFetch", repo_root=ws.repo_root,
                          url="https://private")).decision == "deny"


# -- user rules and learning ------------------------------------------------------------------


def test_compound_commands_are_never_learned(ws):
    for _ in range(3):
        permissions.record_approval(bash(ws, "make a && make b"))
    assert permissions.suggestions() == []


@pytest.mark.variant("learned read approval must remain relative to the worktree")
def test_paths_are_learned_relative_to_the_worktree(ws):
    permissions.record_approval(read(ws, os.path.join(ws.path, "out", "log.txt")))
    permissions.record_approval(read(ws, os.path.join(ws.path, "out", "log.txt")))
    assert [s.match for s in permissions.suggestions()] == [os.path.join("out", "log.txt")]


# -- Claude Code's hook -----------------------------------------------------------------------


ENV_PART = "env | grep -E '^GIT_(AUTHOR|COMMITTER)' ; "
# Claude's prompt offered Yes for this while the Inbox card said a deny rule blocked it.
ON_FRESH_ACCOUNT = ("""echo "--- config"; git config --show-origin --get-regexp '^user\\.' ; echo "--- env"; """
                    + ENV_PART + """echo "--- log"; git log --all """
                    """--format='%h %D | a=%an <%ae> | c=%cn <%ce> | %s' -n 10; echo "--- status"; """
                    """git status --short; git branch -a; git worktree list""")


@pytest.mark.parametrize("preset, policy, chosen, profile_mode, mode", [
    # Nobody set the policy: on, whatever is in the home, and that's what stops the prompts.
    pytest.param("autonomous", None, None, None, "default",
                 marks=pytest.mark.variant("launch mode changes hook fallback and fail-closed behavior")),
    pytest.param("standard", "on", None, None, "default",
                 marks=pytest.mark.variant("launch mode changes hook fallback and fail-closed behavior")),
    ("autonomous", "off", None, None, "default"),
    ("autonomous", "on", "bypassPermissions", None, "bypassPermissions"),
    pytest.param("standard", "on", "auto", None, "auto",
                 marks=pytest.mark.variant("launch mode changes hook fallback and fail-closed behavior")),
    pytest.param("autonomous", "on", None, "auto", "auto",
                 marks=pytest.mark.variant("launch mode changes hook fallback and fail-closed behavior")),     # the built-in developer profile
    pytest.param("autonomous", "on", None, "acceptEdits", "acceptEdits",
                 marks=pytest.mark.variant("launch mode changes hook fallback and fail-closed behavior")),
])
def test_autonomous_starts_claude_without_prompts(db, ws, monkeypatch, preset, policy, chosen, profile_mode, mode):
    """On Autonomous the hooks answer what Claude Code would prompt for, a chosen mode
    stays, and a mode with no prompt to fall back on never lets a failed check through."""
    permissions.apply_preset(preset)
    if policy:
        set_local(ws.repo_root, "permission_policy", policy)
    else:
        monkeypatch.delenv("BRINDLE_POLICY_DEFAULT_OFF")
    policy = policy or "on"
    if chosen:
        set_user("claude_permission_mode", chosen)
    profile = dataclasses.replace(load_profile("supervisor"), permission_mode=profile_mode)
    argv = ClaudeCode().command(LaunchContext("abc", profile, "go", cwd=ws.path))
    assert argv[argv.index("--permission-mode") + 1] == mode

    hook = json.loads(argv[argv.index("--settings") + 1])["hooks"]["PreToolUse"][0]["hooks"][0]
    no_prompt = policy == "on" and mode in ("auto", "bypassPermissions")
    assert (hook.get("onFailure") == "block") == no_prompt
    assert hook["command"].endswith("|| exit 2") == no_prompt

    a = worker(db, ws)

    def payload(command):
        return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": ws.path, "permission_mode": mode}

    def pre_tool(command, agent=a.id):
        out = agents.hook_main(db, agent, "pre-tool", json.dumps(payload(command)))
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None

    # auto keeps its classifier, so an allow is left to it; a prompting mode saves the prompt.
    allowed = "allow" if policy == "on" and preset == "autonomous" and mode in ("default", "acceptEdits") else None
    assert pre_tool("touch x") == allowed
    if allowed:
        # What PreToolUse can't settle (a protected path, a read outside) its PermissionRequest does, unheld.
        for tool, tool_input in (("Write", {"file_path": ".git/x-note"}), ("Read", {"file_path": "/etc/hosts"})):
            answer = json.loads(agents.hook_main(db, a.id, "permission-request", json.dumps(
                claude_payload(tool, tool_input, cwd=ws.path))))
            assert answer["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    # A compound whose parts are all allowed asks nobody on Autonomous, and the Inbox's read
    # of a denied one agrees with the pane: with the rules off a prompt offers Yes, so it's an ask.
    card = permission_grants._policy(permissions.from_claude(payload(ON_FRESH_ACCOUNT), ws.path, ws.repo_root, a.id),
                                     ws.repo_root)
    assert card.decision == ("deny" if policy == "on" else "ask")
    if policy == "on":
        assert pre_tool("cat ~/.ssh/id_rsa") == pre_tool(ON_FRESH_ACCOUNT) == "deny"
        assert pre_tool("git push origin main") == "ask"
        if preset == "autonomous":
            assert pre_tool(ON_FRESH_ACCOUNT.replace(ENV_PART, "")) == allowed

    # A person's own "ask" for what brindle can't verify, which no later prompt would catch here.
    if no_prompt and preset == "autonomous":
        permissions.set_groups({"shell": {"subcategories": {"unchecked": "ask"}}})
        assert pre_tool("echo `\\whoami`") == "ask"

    # A saved approval's use is spent where the call is decided: no PermissionRequest follows here.
    if no_prompt:
        permissions.save_once(permissions.from_claude(payload("git push origin main"), ws.path, ws.repo_root, a.id))
        assert pre_tool("git push origin main") is None
        assert pre_tool("git push origin main") == "ask"

    monkeypatch.setattr(permissions, "decide", lambda *args, **kwargs: 1 / 0)
    assert pre_tool("touch x") == ("deny" if no_prompt else None)
    assert pre_tool("touch x", agent="gone") == ("deny" if mode in ("auto", "bypassPermissions") else None)
    # Claude always sends JSON: anything else fails the hook, which the launch's onFailure blocks.
    with pytest.raises(ValueError):
        agents.hook_main(db, a.id, "pre-tool", "{not json")


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
        agents.hook_main(db, a.id, "permission-request", json.dumps(claude_payload("Bash", {"command": "make docs"})))
        # PostToolUse: paired with the request by its tool and input.
        agents.hook_main(db, a.id, "tool-done", json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "make docs"}, "tool_use_id": f"t{n}"}))
    # Asked, then the turn ended without the tool running: not an approval.
    agents.hook_main(db, a.id, "permission-request", json.dumps(claude_payload("Bash", {"command": "make clean"})))
    agents.handle_hook(db, a.id, "stop", {"stop_hook_active": True})
    assert db.latest_permission_request(a.id) is None
    assert [(s.match, s.count) for s in permissions.suggestions()] == [("make docs", 2)]


@pytest.mark.parametrize("mode,age", [pytest.param("assign", 1000,
                 marks=pytest.mark.variant("an aged worker request still reaches the supervisor")), ("review", 0)])
def test_stuck_notice_names_the_pending_request(db, ws, monkeypatch, mode, age):
    a = worker(db, ws)
    turn_on(ws)
    agents.hook_main(db, a.id, "permission-request", json.dumps(
        claude_payload("Bash", {"command": "make deploy"})))
    request = db.latest_permission_request(a.id)
    db.update_permission_request(a.id, request["tool_use_id"], channel="hook")
    db.set_status(a.id, "waiting")
    db.update_agent(a.id, mode=mode, status_since=time.time() - age)
    sent = []
    monkeypatch.setattr(agents, "pane_owners", lambda db, panes: {})
    monkeypatch.setattr(agents, "owns_pane", lambda db, a, owners: True)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(cull.tmux, "capture", lambda *a, **k: "✻ Pondering…")
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


@pytest.mark.parametrize("stored, expected", [
    pytest.param({"version": 1, "rules": [{"kind": "bash", "match": "deploy", "decision": "ask", "roles": ["admin"]}],
                  "future_policy": {"team": "restricted"}}, "deny", id="store_version-unknown_fields",
                 marks=pytest.mark.variant("older store with unknown restrictions must fail closed on rewrite")),
    pytest.param({"version": 2, "rules": [{"kind": "bash", "match": "deploy", "decision": "deny", "future_scope": "team"}]},
                 "deny", id="store_version-unknown_deny_scope",
                 marks=pytest.mark.variant("unknown future scope previously broadened instead of being dropped")),
    pytest.param({"version": 2, "rules": [{"kind": "bash", "match": "deploy", "decision": "allow", "roles": ["dev"]}]},
                 "allow", id="store_version-unknown_allow_scope"),
    pytest.param({"version": 4, "rules": []}, "deny", id="store_version-future"),
])
def test_cli_rules_and_suggestions(ws, monkeypatch, stored, expected):
    monkeypatch.chdir(ws.repo_root)
    r = CliRunner()
    res = r.invoke(app, ["permissions", "allow", "bash", "make lint"])
    assert res.exit_code == 0, res.output
    rule_id = res.output.split(":")[0]
    assert r.invoke(app, ["permissions", "deny", "fetch", "https://evil.", "--prefix"]).exit_code == 0
    out = r.invoke(app, ["permissions", "list"]).output
    assert "permission_policy: off" in out and "preset: standard" in out
    assert "d-push-default" in out and "preset" in out and f"{rule_id}" in out and "user" in out
    assert r.invoke(app, ["permissions", "allow", "nonsense", "x"]).exit_code == 2

    assert r.invoke(app, ["permissions", "suggestions"]).output.strip() == "no suggestions"
    permissions.record_approval(bash(ws, "make docs"))
    permissions.record_approval(bash(ws, "make docs"))
    sid = r.invoke(app, ["permissions", "suggestions"]).output.split()[0]
    assert r.invoke(app, ["permissions", "accept", sid]).exit_code == 0
    assert "learned" in r.invoke(app, ["permissions", "list"]).output
    assert r.invoke(app, ["permissions", "accept", "nope"]).exit_code == 1

    assert r.invoke(app, ["permissions", "forget", rule_id]).exit_code == 0
    assert r.invoke(app, ["permissions", "forget", "d-push-default"]).exit_code == 0   # nothing is built in
    assert r.invoke(app, ["permissions", "forget", "d-push-default"]).exit_code == 1

    res = r.invoke(app, ["permissions", "reset"], input="n\n")
    assert res.exit_code != 0 and any(r.source == "learned" for r in permissions.load_store().rules)
    assert r.invoke(app, ["permissions", "reset", "--yes"]).exit_code == 0
    store = permissions.load_store()
    assert {r.source for r in store.rules} == {"preset"} and store.rule("d-push-default") and store.approvals == {}
    path = permissions.store_path()
    stored["remembered"] = [{"id": "preference", "uses_spent": 0}]
    path.write_text(json.dumps(stored))
    before = path.read_text()
    if stored["version"] > permissions.STORE_VERSION:
        with pytest.raises(ValueError):
            with permissions.editing() as store:
                store.rules.clear()
        assert path.read_text() == before
    else:
        with permissions.editing() as store:
            store.rules.clear()
        saved = json.loads(path.read_text())
        # Rows it can't read and keys it doesn't know survive a rewrite as they were.
        assert all(row in saved["rules"] for row in stored["rules"])
        assert all(saved[key] == value for key, value in stored.items() if key not in ("version", "rules"))
    # An unrecognized restriction cannot disappear behind a known allow; an
    # unrecognized allow just doesn't apply.
    rules = [*permissions.all_rules(), Rule("bash", "deploy", "exact", "allow")]
    assert decide(bash(ws, "deploy"), rules=rules).decision == expected
    # A request with no target is blocked too; else (a 0.4.7 store, now Autonomous) unmatched runs.
    assert decide(Request("claude", "bash", "Bash"), rules=rules).decision == expected
