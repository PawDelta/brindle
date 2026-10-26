"""Permission presets (brindle.permissions): named, editable rule sets with
nothing built in, the Autonomous default, migration of older stores, the
read-only pipeline allowance, prompt counts and the daemon's policy API."""

import asyncio
import json
import os
import time

import pytest

from brindle import permissions, workspaces
from brindle.db import Agent
from brindle.permissions import Request, decide

TOKEN = "t" * 43


@pytest.fixture
def ws(db, repo, monkeypatch):
    monkeypatch.setattr("brindle.permissions.PRESET_RULES", ())
    return workspaces.create(db, str(repo), "feature").workspace


def bash(ws, command):
    return Request("claude", "bash", "Bash", ws.path, ws.path, ws.repo_root, command=command)


def read(ws, path):
    return Request("claude", "read", "Read", ws.path, ws.path, ws.repo_root, path=str(path))


def write(ws, path):
    return Request("claude", "write", "Write", ws.path, ws.path, ws.repo_root, path=os.path.join(ws.path, path))


def _send(method, path, body=None):
    """One request through the daemon's ASGI app, with the person's token."""
    from brindle.daemon.app import build_app

    raw = json.dumps(body or {}).encode()
    path, _, query = path.partition("?")
    sent = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "root_path": "", "server": ("127.0.0.1", 8123), "client": ("127.0.0.1", 50000),
             "headers": [(b"authorization", f"Bearer {TOKEN}".encode()), (b"content-type", b"application/json")]}
    asyncio.run(build_app(TOKEN)(scope, receive, send))
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], json.loads(out)


def test_a_new_store_is_autonomous_and_an_old_one_migrates_to_standard_keeping_its_rules(monkeypatch):
    monkeypatch.setattr(permissions, "DEFAULT_PRESET", "autonomous")   # conftest pins the tests to Standard
    store = permissions.load_store()
    assert store.preset == "autonomous" and store.unmatched == "allow"
    assert {r.source for r in store.rules} == {"preset"}
    assert store.rule("d-push-default").decision == "ask" and store.rule("d-ssh").decision == "deny"
    # A store from before presets: the person's own and learned rules, and the
    # command allows the first run wrote as theirs.
    old = {"version": 1, "approvals": {"x": {"kind": "bash", "match": "make docs", "count": 2}},
           "rules": [{"kind": "bash", "match": "make lint", "match_type": "exact", "decision": "allow", "source": "user"},
                     {"kind": "fetch", "match": "https://evil.", "match_type": "prefix", "decision": "deny",
                      "source": "learned"},
                     {"kind": "bash", "match": "npm ", "match_type": "prefix", "decision": "allow", "source": "user"}],
           "codex_hook": {"/h": {"command": "c", "key": "k", "hash": "h"}}}
    permissions.store_path().write_text(json.dumps(old))
    store = permissions.load_store()
    assert store.preset == "standard" and store.unmatched == "ask"
    mine = [(r.kind, r.match, r.source) for r in store.rules if r.source != "preset"]
    assert mine == [("bash", "make lint", "user"), ("fetch", "https://evil.", "learned")]
    assert [r for r in store.rules if r.match == "npm "] and store.rule("d-push-default")
    assert store.approvals["x"]["count"] == 2 and store.codex_hook == {"/h": {"command": "c", "key": "k", "hash": "h"}}
    assert json.loads(permissions.store_path().read_text())["preset"] == "standard"   # migrated once, on disk


@pytest.mark.parametrize("request_of, decision, rule", [
    (lambda ws: bash(ws, "terraform apply"), "allow", None),                  # nothing covers it: it runs
    (lambda ws: bash(ws, "python build.py > out.txt"), "allow", None),
    (lambda ws: write(ws, "src/new.py"), "allow", None),
    (lambda ws: bash(ws, "git push origin feature"), "allow", None),
    (lambda ws: bash(ws, "git push origin main"), "ask", "d-push-default"),
    (lambda ws: bash(ws, "git push"), "ask", "d-push-default"),               # its upstream may be main
    (lambda ws: bash(ws, "cd sub && git push origin HEAD:main"), "ask", "d-push-default"),
    (lambda ws: bash(ws, "git push origin HEAD"), "ask", "d-push-default"),     # HEAD may be the default branch
    (lambda ws: bash(ws, "git push origin refs/heads/*:refs/heads/*"), "ask", "d-push-default"),   # so may a wildcard
    (lambda ws: bash(ws, "git push --force origin feature"), "deny", "d-git-force"),
    (lambda ws: bash(ws, "git push origin +topic:feature"), "deny", "d-git-force"),   # a forced refspec
    (lambda ws: bash(ws, "git push origin :feature"), "deny", "d-git-force"),         # deletes the branch
    (lambda ws: bash(ws, "rm -rf / --no-preserve-root"), "deny", "d-rm"),
    (lambda ws: bash(ws, "rm -fr /"), "deny", "d-rm"),
    (lambda ws: bash(ws, "rm -Rf ~/"), "deny", "d-rm"),
    (lambda ws: bash(ws, "rm -rf build/"), "allow", None),
    (lambda ws: read(ws, os.path.expanduser("~/.aws/credentials")), "deny", "d-aws"),
    (lambda ws: read(ws, os.path.join(ws.repo_root, ".env")), "deny", "d-env"),
])
def test_autonomous_runs_everything_but_default_branch_pushes_and_destructive_actions(ws, request_of, decision, rule):
    permissions.apply_preset("autonomous")
    got = decide(request_of(ws))
    assert got.decision == decision, got.reason
    assert (got.rule.id if got.rule else None) == rule


def test_every_rule_can_be_edited_or_removed_and_a_preset_change_keeps_the_persons_rules(ws):
    assert permissions.forget("d-push-default")    # a seeded rule: not built in
    assert decide(bash(ws, "git push origin main")).rule is None
    edited = permissions.update_rule("d-git-force", decision="allow", label="we rebase")
    assert edited.source == "user" and edited.label == "we rebase" and edited.id == "d-git-force"
    assert decide(bash(ws, "git push --force origin x")).decision == "allow"
    store = permissions.apply_preset("secure")
    assert store.preset == "secure" and store.unmatched == "deny"
    assert store.rule("d-git-force").decision == "allow"           # the person's version stays
    assert store.rule("d-push-default").limits == {"max_scope": "branch", "life": "time", "minutes": 15}
    assert decide(Request("claude", "mcp", "mcp__slack__send_message", ws.path, ws.path, ws.repo_root)).decision == "deny"
    permissions.set_unmatched("ask")
    assert decide(Request("claude", "mcp", "mcp__slack__send_message", ws.path, ws.path, ws.repo_root)).decision == "ask"
    with pytest.raises(ValueError):
        permissions.apply_preset("yolo")
    timed = permissions.add_rule("bash", "deploy", "allow", expires=time.time() + 60)
    assert decide(bash(ws, "deploy")).rule.id == "d-bash-ask"       # Secure's ask rule stands over an allow
    assert permissions.forget("d-bash-ask")
    assert decide(bash(ws, "deploy")).decision == "allow"
    permissions.update_rule(timed.id, expires=None)
    assert permissions.load_store().rule(timed.id).expires is None


@pytest.mark.parametrize("command, decision", [
    ('grep -rn "a\\|b" src -l', "allow"),                          # the stall this allowance is for
    ("cat README.md | head -20", "allow"),
    ("git log --oneline -5 | grep fix", "allow"),
    ("find . -name '*.py' | wc -l", "allow"),
    ("sed -n '12,40p' src/x.py", "allow"),
    ("ls -la && pwd", "allow"),
    ("grep -rn a src > hits.txt", "ask"),                          # writes a file
    ("cat $(find . -name x)", "ask"),                              # what $(...) runs can't be seen
    ("ls | xargs rm", "ask"),                                      # a part that isn't read-only
    ("find . -name '*.pyc' -delete", "ask"),
    ("sed -i s/a/b/ src/x.py", "ask"),
    ("sed -n -e p -e w/tmp/output input", "ask"),                  # a second script that writes
    ("rg --pre=evil needle input", "ask"),                         # runs a program on each file
    ("xxd -r input output", "ask"),                                # not an audited program
    ("uniq in out", "ask"),                                        # its second operand is an output file
    ("fd -x rm", "ask"),
    ("sort -o out.txt in.txt", "ask"),
    ("sort -ro out.txt in.txt", "ask"),                            # the writing flag inside a cluster
    ("file -bC -m magic input", "ask"),
    ("tree -fo out.txt", "ask"),
    ("cat x | tee copy.txt", "ask"),
])
def test_read_only_pipelines_run_but_writes_substitutions_and_other_programs_do_not(ws, command, decision):
    got = decide(bash(ws, command))
    assert got.decision == decision, got.reason
    if decision == "allow":
        assert got.rule.id == "d-readonly"


@pytest.mark.parametrize("provider", ["claude", "codex", "opencode", "antigravity"])
def test_an_explicit_ask_survives_every_providers_fallbacks(db, ws, provider, monkeypatch):
    """A rule that asks is the person's to decide: not the profile's allowed_tools
    (Claude Code's PreToolUse approval, Codex), not OpenCode's own config (the
    plugin fails closed), not agy's in-workspace default (the strict hook
    denies and escalates)."""
    from pathlib import Path

    from brindle import agents, antigravity, opencode
    from brindle.config import set_local

    set_local(ws.repo_root, "permission_policy", "on")
    agents_dir = Path(ws.repo_root) / ".brindle" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "developer.md").write_text("---\nname: developer\nallowed_tools: [Bash(git push:*)]\n---\n")
    a = Agent("p1", ws.id, "developer", provider, "boss", "assign", "processing", "%p1", None, time.time())
    db.add_agent(a)
    monkeypatch.setattr(agents, "_hold_for_app", lambda *args, **kw: None)     # no daemon: the CLI's prompt asks
    if provider == "claude":
        pre = lambda command: agents.pre_tool_decision(  # noqa: E731
            db, a, {"tool_name": "Bash", "tool_input": {"command": command}})["hookSpecificOutput"]
        assert pre("git push origin main")["permissionDecision"] == "ask"      # forces the prompt the hook holds
        assert pre("git push origin feature")["permissionDecision"] == "allow"  # no rule asks: allowed_tools cover it
        assert pre("git push --force origin feature")["permissionDecision"] == "deny"
        assert agents.pre_tool_decision(db, a, {"tool_name": "Read", "tool_input": {
            "file_path": os.path.join(ws.path, ".env")}})["hookSpecificOutput"]["permissionDecision"] == "deny"
        return                                                                  # PermissionRequest counts the ask
    if provider == "codex":
        out = agents.permission_request_decision(
            db, a, {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}, "cwd": ws.path}, "codex")
        assert out is None                                                     # ask, not the profile's allow
        out = agents.permission_request_decision(
            db, a, {"tool_name": "Bash", "tool_input": {"command": "git push origin feature"}, "cwd": ws.path}, "codex")
        assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"    # no rule asks: allowed_tools cover it
    elif provider == "opencode":
        out = json.loads(opencode.hook_main(db, a.id, "opencode-pre-tool", json.dumps(
            {"session_id": "s", "tool": "bash", "args": {"command": "git push origin main"}})))
        assert out["decision"] == "deny"                                       # fails closed: it can't hold the ask
        assert opencode.hook_main(db, a.id, "opencode-pre-tool", json.dumps(
            {"session_id": "s", "tool": "bash", "args": {"command": "git push origin feature"}})) == ""
    else:
        permissions.apply_preset("secure")                                     # writes ask explicitly
        payload = {"toolCall": {"name": "write_to_file", "args": {"TargetFile": os.path.join(ws.path, "x.py")}},
                   "conversationId": "c", "workspacePaths": [ws.path]}
        assert antigravity.pre_tool_decision(db, a.id, payload, strict=True)["decision"] == "deny"
        permissions.apply_preset("standard")                                   # unmatched: agy's own default runs it
        assert antigravity.pre_tool_decision(db, a.id, payload, strict=True)["decision"] == "allow"
    assert permissions.prompt_counts().get("d-push-default" if provider != "antigravity" else "d-write-ask") == 1


def test_prompt_counts_count_the_asks_of_the_last_day_per_rule(ws, monkeypatch):
    now = time.time()
    permissions.note_prompt(decide(bash(ws, "git push origin main")))
    permissions.note_prompt(decide(bash(ws, "git push origin main")))
    permissions.note_prompt(decide(bash(ws, "terraform apply")))
    permissions.note_prompt(decide(bash(ws, "git status")))          # allowed: not an ask
    assert permissions.prompt_counts() == {"d-push-default": 2, "unmatched": 1}
    monkeypatch.setattr(permissions.time, "time", lambda: now + permissions.PROMPT_WINDOW + 1)
    assert permissions.prompt_counts() == {}


def test_the_daemon_serves_and_edits_the_policy(ws):
    code, policy = _send("GET", "/v1/permissions")
    assert code == 200 and policy["preset"] == "standard" and policy["unmatched"] == "ask"
    assert [p["name"] for p in policy["presets"]] == ["autonomous", "permissive", "standard", "secure"]
    push = next(r for r in policy["rules"] if r["id"] == "d-push-default")
    assert push["decision"] == "ask" and push["limits"] == {"max_scope": "destination", "life": "time", "minutes": 30}

    code, policy = _send("PUT", "/v1/permissions/preset", {"preset": "autonomous"})
    assert code == 200 and policy["preset"] == "autonomous" and policy["unmatched"] == "allow"
    assert _send("PUT", "/v1/permissions/preset", {"preset": "nope"})[0] == 400

    code, rule = _send("POST", "/v1/permissions/rules", {
        "kind": "bash", "match": "terraform *", "match_type": "glob", "decision": "ask",
        "limits": {"max_scope": "*", "life": "time", "minutes": 10}, "minutes": 60})
    assert code == 200 and rule["source"] == "user" and rule["limits"]["minutes"] == 10 and rule["expires"]
    assert decide(bash(ws, "terraform plan")).decision == "ask"
    code, rule = _send("PUT", f"/v1/permissions/rules/{rule['id']}", {"decision": "deny", "label": "hands off"})
    assert code == 200 and rule["decision"] == "deny" and rule["label"] == "hands off"
    assert decide(bash(ws, "terraform plan")).decision == "deny"
    code, rule = _send("POST", f"/v1/permissions/rules/{rule['id']}/exceptions",
                       {"kind": "bash", "match": "terraform plan"})
    assert code == 200 and len(rule["exclusions"]) == 1
    assert decide(bash(ws, "terraform plan")).decision == "allow"    # excepted; autonomous runs the rest
    assert decide(bash(ws, "terraform apply")).decision == "deny"
    assert _send("DELETE", f"/v1/permissions/rules/{rule['id']}")[0] == 200
    assert _send("DELETE", f"/v1/permissions/rules/{rule['id']}")[0] == 404
    assert _send("POST", "/v1/permissions/rules", {"kind": "bash", "match": "x", "decision": "maybe"})[0] == 400

    code, policy = _send("PATCH", "/v1/permissions", {"unmatched": "deny"})
    assert code == 200 and policy["unmatched"] == "deny"
    assert decide(bash(ws, "terraform apply")).decision == "deny"
    assert _send("DELETE", "/v1/permissions/rules/d-ssh")[0] == 200     # the preset's rules are the person's too
    assert _send("GET", "/v1/permissions")[1]["prompt_counts"] == {}
