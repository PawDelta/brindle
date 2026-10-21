import dataclasses
import fnmatch
import json
import os
import shutil
import subprocess
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
    (None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
     ["git -C /otherworktree status --short", "ls /worktree"], "allow"),
    (None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
     ["git branch --list 'fix/*'", "git branch -v", "git branch -vv", "git branch"], "allow"),
    pytest.param(None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
                 ["git -C /otherworktree branch --list --no-list -D victim"], "ask",
                 marks=pytest.mark.variant("branch listing options must not be cancelled into a write")),
    pytest.param(None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
                 ["git -C /otherworktree diff --output=/worktree/out"], "ask",
                 marks=pytest.mark.variant("a read-only subcommand can still write through its options")),
    pytest.param(None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
                 ["git -C /otherworktree status > /worktree/out"], "ask",
                 marks=pytest.mark.variant("a shell redirection must not turn an approved read into a write")),
    pytest.param(None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
                 ["git -C /escaped status"], "ask",
                 marks=pytest.mark.variant("a symlink under worktrees must not approve a path outside the scope")),
    pytest.param(None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
                 ["git branch --list `pwd`"], "ask",
                 marks=pytest.mark.variant("branch patterns must not hide command substitution")),
    pytest.param(None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
                 ["git branch --list fix/*"], "ask",
                 marks=pytest.mark.variant("unquoted patterns could expand into option-like filenames")),
    ("dontAsk", "local-model", {"edit": "deny", "*": "deny"}, ["--model", "local-model"],
     ["git -C /otherworktree status"], "allow"),
    ("dontAsk", "local-model", {"edit": "deny", "*": "deny"}, ["--model", "local-model"],
     ["git  -C /otherworktree\t status"], "allow"),
    pytest.param(None, "local-model", {"edit": "ask", "*": "ask"}, ["--model", "local-model"],
                 ("git -C /otherworktree status", "git -C /otherworktree checkout status file"), "ask",
                 marks=pytest.mark.variant("same callID with changed command must still prompt; approvals expire and are single-use")),
    pytest.param("dontAsk", "local-model", {"edit": "deny", "*": "deny"}, ["--model", "local-model"],
                 ["git -C /worktree status", "git push origin main"], "deny",
                 marks=pytest.mark.variant("a compound command must not hide a write after a permitted read")),
])
def test_permission_mode_and_model_map_onto_opencode(brindle_home, tmp_path, mode, model, perm, flags, patterns, decision):
    plugin_case = isinstance(patterns, tuple)
    requested = patterns[1] if plugin_case else ""
    patterns = [patterns[0]] if plugin_case else patterns
    cwd = tmp_path / "repo" / "supervisor"
    target = tmp_path / "repo" / ".brindle" / "worktrees" / "created-later"
    for directory in (cwd, target, tmp_path / "cache", tmp_path / "read only", tmp_path / "outside"):
        directory.mkdir(parents=True, exist_ok=True)
    escaped = target.parent / "escaped"
    escaped.symlink_to(tmp_path / "outside", target_is_directory=True)
    replacements = {"/worktree": str(cwd), "/otherworktree": str(target),
                    "/shared/cache": str(tmp_path / "cache"), "/read only": str(tmp_path / "read only"),
                    "/escaped": str(escaped)}
    for original, replacement in replacements.items():
        patterns = [command.replace(original, replacement) for command in patterns]
        requested = requested.replace(original, replacement)
    profile = dataclasses.replace(load_profile("supervisor"), permission_mode=mode, model=model,
                                  add_dirs=[str(tmp_path / "cache"), str(tmp_path / "read only"), "/unsafe*"])
    argv = OpenCode().command(LaunchContext("o1", profile, None, cwd=str(cwd)))
    got = _config(argv)["permission"]
    assert argv[3:] == flags
    assert got["edit"] == perm["edit"]
    if mode == "bypassPermissions":
        assert got["bash"] == got["external_directory"] == "allow"
    else:
        assert got["bash"]["*"] == perm["*"] and got["bash"]["uv run *"] == "allow"
        # OpenCode 1.19.3 emits one pattern per simple command in ; / && lists,
        # retaining git's -C option. The last matching rule wins for each one.
        # A brindle approval runs the command respelled, and the plugin answers OpenCode's ask once.
        approved = opencode.readonly_git(profile, "; ".join(patterns), str(cwd), str(tmp_path / "repo"))
        seen = [opencode.squeezed(command) for command in patterns] if approved else patterns
        decisions = [next(action for pattern, action in reversed(got["bash"].items())
                          if fnmatch.fnmatchcase(command, pattern)) for command in seen]
        if approved:
            decisions = ["allow" if action == "ask" else action for action in decisions]
        if plugin_case:
            # Execute the real JS hooks with in-memory IPC/client fakes; no
            # OpenCode process, tmux, shell commands, or timing sleeps.
            node = shutil.which("node")
            if not node:
                pytest.skip("Node is required to execute the OpenCode plugin")
            program = """
import { EventEmitter } from 'node:events';
let input = ''; for await (const chunk of process.stdin) input += chunk;
const {source, command, requested} = JSON.parse(input);
globalThis.fakeSpawn = () => {
  const p = new EventEmitter(); p.stdout = new EventEmitter();
  p.stdin = {end() {queueMicrotask(() => {
    p.stdout.emit('data', JSON.stringify({decision: 'allow'})); p.emit('close');
  })}}; return p;
};
process.env.BRINDLE_AGENT_ID = 'test'; process.env.BRINDLE_OPENCODE_POLICY = '';
process.env.OPENCODE_CONFIG_CONTENT = '{}';
let clock = 1000; Date.now = () => clock;
const {Brindle} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const replies = [], counts = [];
const hooks = await Brindle({client: {postSessionIdPermissionsPermissionId: async r => {replies.push(r); return {}}}});
const before = () => hooks['tool.execute.before']({sessionID:'s', callID:'c', tool:'bash'}, {args:{command}});
const asked = async (changes = {}) => {
  const {sessionID = 's', callID = 'c', permission = 'bash', text = command} = changes;
  await hooks.event({event:{type:'permission.asked', properties:{id:'p', sessionID,
    permission, tool:{callID}, metadata:{command:text}}}}); counts.push(replies.length);
};
await before();
await asked({text:requested}); await asked({sessionID:'other'});
await asked({callID:'other'}); await asked({permission:'edit'});
await asked(); await asked();
await before(); clock += 60000; await asked();
console.log(JSON.stringify([counts, replies.map(r => r.body.response)]));
"""
            source = opencode.PLUGIN.replace('import { spawn } from "node:child_process"',
                                            'const spawn = globalThis.fakeSpawn').replace("__BRINDLE__", '["fake"]')
            result = subprocess.run([node, "--input-type=module", "-e", program],
                                    input=json.dumps({"source": source, "command": patterns[0], "requested": requested}),
                                    text=True, capture_output=True, check=True, timeout=10)
            assert json.loads(result.stdout) == [[0, 0, 0, 0, 1, 1, 1], ["once"]]
            decisions = ["ask"]  # the changed command received no approval
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

    add_agent(db, ws, profile="supervisor")
    pre = lambda tool, args: opencode.hook_main(  # noqa: E731
        db, "o1", "opencode-pre-tool", json.dumps({"session_id": "ses_a", "tool": tool, "args": args}))
    denied = {"tool": "bash", "args": {"command": "git push origin main"}}
    assert pre(**denied) == ""   # policy off: OpenCode's own rules apply
    set_local(ws.repo_root, "permission_policy", "on")
    answer = json.loads(pre(**denied))
    assert answer["decision"] == "deny" and answer["reason"].startswith("brindle:")
    assert json.loads(pre("read", {"filePath": "~/.ssh/id_rsa"}))["decision"] == "deny"
    assert pre("bash", {"command": "git status"}) == ""   # allow and ask leave the call to OpenCode
    target = os.path.join(ws.repo_root, ".brindle", "worktrees", "created-after-launch")
    os.makedirs(target)
    assert json.loads(pre("bash", {"command": f"git -C {target} status; ls"}))["decision"] == "allow"
    assert pre("bash", {"command": f"git -C {target} branch -D victim"}) == ""
    db.update_agent("o1", profile="reviewer-opencode")
    assert json.loads(pre("bash", {"command": f"git -C {target} status"}))["decision"] == "allow"
    # Respelled as OpenCode's rules match it; quoted spacing is an argument and stays.
    answer = json.loads(pre("bash", {"command": f"git  -C {target} log --grep='a  b' --grep=\"c  \\\" d\""}))
    assert answer["command"] == f"git -C {target} log --grep='a  b' --grep=\"c  \\\" d\""
    assert json.loads(pre("bash", {"command": f"ls; git  -C {target} checkout status file"}))["decision"] == "deny"
    permissions.add_rule("bash", "git status", "deny")
    assert json.loads(pre("bash", {"command": f"git -C {target} status"}))["decision"] == "deny"
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
