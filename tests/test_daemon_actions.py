"""The daemon's action routes: each reaches its engine call, a long one as a
job whose end arrives on the bus, and a choice the person must make is a 409."""

import asyncio
import contextlib
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import sh

from brindle import agents, autopilot, bus, config, coordinator, pipeline, preflight, providers, workspaces
from brindle.daemon import peer, routes_actions
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace
from brindle.pro.account import ProAccount
from brindle.pro.team_policy import Access, AgentSetup, ClaudeSetup, ModelsSetup

TOKEN = "t" * 43
REAL_PREFLIGHT = preflight.check
REAL_ONBOARDED = providers.claude_onboarded


def _send(method: str, path: str, body: dict | None = None, peer_pid: int | None = None,
          token: str | None = TOKEN) -> tuple[int, dict]:
    raw = json.dumps(body or {}).encode()
    path, _, query = path.partition("?")
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "root_path": "", "server": ("127.0.0.1", 8123), "client": ("127.0.0.1", 50000),
             "headers": [*([(b"authorization", f"Bearer {token}".encode())] if token else []),
                         (b"content-type", b"application/json")]}
    if peer_pid is not None:
        scope[peer.PEER_PID] = peer_pid
    asyncio.run(build_app(TOKEN)(scope, receive, send))
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], json.loads(out)


@pytest.fixture
def world(db, repo, monkeypatch):
    """A session ``sup`` in the checkout and its plan-first worker ``wk`` in worktree ``w-1``;
    jobs run inline."""
    root = workspaces.adopt_root(db, str(repo))
    db.add_workspace(Workspace("w-1", root.repo_root, "feat", "worktree", "feat", "main", str(repo),
                               None, "brindle-x", time.time()))
    db.add_workspace(Workspace("proj/w-2", root.repo_root, "feat2", "worktree", "feat2", "main",
                               str(repo) + "-2", None, "brindle-y", time.time()))
    db.add_agent(Agent("sup", root.id, "supervisor", "claude", None, "interactive", "idle", "%1",
                       None, time.time()))
    db.add_agent(Agent("wk", "w-1", "developer", "claude", "sup", "assign", "idle", "%2", None,
                       time.time(), plan_first=1, plan_state="proposed"))
    db.add_autopilot("sup")
    monkeypatch.setattr(routes_actions, "_spawn", lambda target: target())
    monkeypatch.setattr(preflight, "check", lambda provider, repo_root=None, **kw: [])
    return root


def _flat(args):
    for x in args:
        yield from (_flat(x) if isinstance(x, (list, tuple)) else [getattr(x, "id", x)])


@pytest.mark.parametrize("method,path,body,fake,returns,status,target", [
    ("POST", "/v1/sessions/sup/pause", {}, (agents, "pause"), [], 200, "sup"),
    # The person decides a plan as the worker's supervisor would.
    ("POST", "/v1/inbox/wk/approve", {}, (agents, "approve_plan"), "approved", 200, "sup"),
    ("POST", "/v1/agents/wk/kill", {}, (agents, "kill"), None, 200, "wk"),
    ("POST", "/v1/workspaces/w-1/review", {}, (agents, "start_review"), None, 200, "w-1"),
    # Real workspace ids contain a slash, which arrives in the path as %2F decoded.
    ("POST", "/v1/workspaces/proj/w-2/merge", {}, (pipeline, "merge"), "Merged feat2", 202, "proj/w-2"),
    ("POST", "/v1/workspaces/proj/w-2/remove", {}, (workspaces, "remove"), SimpleNamespace(branch_note=None, teardown=None), 200, "proj/w-2"),
    ("POST", "/v1/goal/check", {"root": "sup"}, (routes_actions, "_run_helper"), {"exit": 0}, 202,
     "_check-milestones"),
    ("POST", "/v1/goal/enabled", {"root": "sup", "enabled": False}, (autopilot, "set_enabled"), None,
     200, "sup"),
    ("POST", "/v1/coordinator/undo", {"repo": ".", "event": "e1"}, (coordinator, "undo"), "undone",
     200, "e1"),
    ("PATCH", "/v1/config", {"values": {"delegation": "fast"}}, (config, "set_user"), "x", 200,
     "delegation"),
    ("POST", "/v1/account/logout", {}, (ProAccount, "cmd_logout"), 0, 200, None),
])
def test_an_action_reaches_its_engine_call(db, world, monkeypatch, method, path, body, fake, returns,
                                           status, target):
    calls = []
    monkeypatch.setattr(*fake, lambda *a, **k: calls.append(a) or returns)
    code, reply = _send(method, path, body)
    assert code == status, reply
    assert len(calls) == 1 and (target is None or target in _flat(calls[0]))
    if status == 202:
        done = bus.tail(db, kinds=("job.done", "job.failed"))
        assert [(e["kind"], e["payload"]["job"]) for e in done] == [("job.done", reply["job"])]


def test_closing_a_session_stops_and_hides_all_its_agents_but_keeps_their_work(db, tmp_path, monkeypatch):
    from brindle import procs, tmux
    from brindle import sessions as sessions_mod

    repo = str(tmp_path / "proj")
    for wid, name, kind, branch in [("proj/root", "root", "main", "main"), ("w-1", "feat", "worktree", "feat"),
                                    ("w-2", "feat2", "worktree", "feat2")]:
        db.add_workspace(Workspace(wid, repo, name, kind, branch, "main", f"{repo}/{name}", None,
                                   f"brindle-{name}", time.time()))
    db.add_agent(Agent("sup", "proj/root", "supervisor", "claude", None, "interactive", "idle", "%1",
                       None, time.time()))
    for n in (1, 2):
        db.add_agent(Agent(f"wk{n}", f"w-{n}", "developer", "claude", "sup", "assign", "idle", f"%{n + 1}",
                           None, time.time()))
    stopped = []
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: {})
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id not in stopped)
    monkeypatch.setattr(agents, "owns_pane", lambda *a, **k: True)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: stopped.append(a.id))
    monkeypatch.setattr(procs, "stop", lambda *a, **k: None)
    monkeypatch.setattr(sessions_mod, "live", lambda *a, **k: [])     # no tmux to ask
    for _ in range(2):         # the second close finds nothing left to do
        code, reply = _send("POST", "/v1/sessions/sup/close")
        assert code == 200, reply
    assert sorted(stopped) == ["sup", "wk1", "wk2"]
    assert all(db.get_agent(i).dismissed_at for i in stopped)
    assert reply == {"closed": [], "stopped": []}
    assert {db.get_workspace(w).branch for w in ("w-1", "w-2")} == {"feat", "feat2"}
    code, listed = _send("GET", f"/v1/sessions?repo={repo}")
    assert code == 200 and "sup" not in [s["id"] for s in listed]


@pytest.mark.parametrize("path,base,setup,status", [
    ("app.py", "shown", None, 200),
    ("app.py", "read", None, 200),              # the base from GET .../file, the app's way
    ("app.py", "e69de29", None, 409),           # the empty blob: not what the file is now
    ("app.py", "shown", "busy", 409),
    ("app.py", "shown", "hook-refuses", 409),   # the file goes back; the worker's index survives
    ("new.txt", "shown", None, 200),            # a file git doesn't track yet
    ("new.txt", "shown", "hook-refuses", 409),  # ... refused: untracked again, as it was
    ("pkg/m.py", "shown", "deleted-refused", 409),  # its deletion staged, then refused: still staged
    ("app.py", "shown", "to-head", 200),        # back to HEAD's content: no commit, the staged version goes
    pytest.param("app.py", "shown", "agent", 403, marks=pytest.mark.variant("an agent holding the token")),
    ("../proj/app.py", "shown", None, 400),
    ("out/m.py", "shown", None, 400),           # a symlinked directory leading out of the worktree
    pytest.param(".git/HEAD", "shown", None, 400, marks=pytest.mark.variant("the worktree's own git link")),
    pytest.param(":(glob)*", "shown", None, 200,
                 marks=pytest.mark.variant("a name that is pathspec magic must not commit other work")),
    pytest.param("pkg/m.py", "shown", "swap", 400,
                 marks=pytest.mark.variant("a directory swapped for an escaping symlink mid-edit")),
], ids=["edit-commits", "edit-read", "edit-stale", "edit-busy", "edit-refused", "edit-new-file", "edit-new-file-refused",
        "edit-deleted-refused", "edit-to-head", "edit-agent",
        "edit-dotdot", "edit-symlink", "edit-git", "edit-pathspec", "edit-swap"])
def test_an_edit_from_review_commits_on_the_workers_branch(db, repo, world, tmp_path, monkeypatch,
                                                           path, base, setup, status):
    from brindle import git

    wt, outside = tmp_path / "wt", tmp_path / "outside"
    sh(f"git worktree add -q -b rev {wt} main", repo)
    (wt / "pkg").mkdir()
    for name in ("pkg/m.py", ":(glob)*"):
        (wt / name).write_text("print('hi')\n")
    sh("git add -A && git commit -qm more", wt)
    # HEAD, the index and the worktree all differ for app.py; abc.txt is unrelated staged work.
    (wt / "app.py").write_text("print('staged')\n")
    (wt / "abc.txt").write_text("wip\n")
    sh("git add app.py abc.txt", wt)
    (wt / "app.py").write_text("print('working')\n")
    (wt / "new.txt").write_text("draft\n")
    outside.mkdir()
    (outside / "m.py").write_text("outside\n")
    (wt / "out").symlink_to(outside)
    db.add_workspace(Workspace("proj/w-e", world.repo_root, "rev", "worktree", "rev", "main", str(wt),
                               None, "brindle-z", time.time()))
    if setup == "busy":
        db.add_agent(Agent("ew", "proj/w-e", "developer", "claude", "sup", "assign", "processing", "%3",
                           None, time.time()))
    if setup == "deleted-refused":
        sh("git rm -q --cached pkg/m.py", wt)
    if setup in ("hook-refuses", "deleted-refused"):
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
    if setup == "swap":
        real = git.hash_blob

        def swap_then_hash(*a, **k):        # after the file is opened, before it is written
            if not (wt / "pkg.moved").exists():
                (wt / "pkg").rename(wt / "pkg.moved")
                (wt / "pkg").symlink_to(outside)
            return real(*a, **k)

        monkeypatch.setattr(git, "hash_blob", swap_then_hash)
    head, staged = sh("git rev-parse HEAD", wt), sh("git diff --cached", wt)
    shown = sh(f"git hash-object {path if path in ('app.py', 'new.txt') else 'pkg/m.py'}", wt)[:7]
    content = "print('hi')\n" if setup == "to-head" else "print('bye')\n"
    if base == "read":
        for escape in ("../proj/app.py", "out/m.py"):
            assert _send("GET", f"/v1/workspaces/proj/w-e/file?path={escape}")[0] == 400
        code, got = _send("GET", f"/v1/workspaces/proj/w-e/file?path={path}")
        assert code == 200 and got["content"] == "print('working')\n", got
        base = got["base"]
    monkeypatch.setattr(peer, "agent_caller", lambda pid: "brindle agent ew")
    code, reply = _send("PUT", "/v1/workspaces/proj/w-e/file",
                        {"path": path, "content": content, "base": shown if base == "shown" else base},
                        310 if setup == "agent" else None)
    assert code == status, reply
    edited = bus.tail(db, kinds=("workspace.edited",))
    assert (outside / "m.py").read_text() == "outside\n" and (repo / "app.py").read_text() == "print('hi')\n"
    if status != 200:
        assert sh("git rev-parse HEAD", wt) == head and sh("git diff --cached", wt) == staged and edited == []
        assert (wt / "app.py").read_text() == "print('working')\n" and (wt / "new.txt").read_text() == "draft\n"
        # The write went to the file it opened (moved away when swapped), and was put back.
        assert (wt / ("pkg.moved" if setup == "swap" else "pkg") / "m.py").read_text() == "print('hi')\n"
        return
    cached = sh("git diff --cached --name-only", wt).splitlines()
    assert "abc.txt" in cached and path not in cached and (wt / path).read_text() == content
    if setup == "to-head":
        assert reply["commit"] is None and sh("git rev-parse HEAD", wt) == head
        return
    assert sh("git log -1 --format=%s rev", wt) == f"edit from review: {path}"
    assert sh("git diff-tree --no-commit-id --name-only -r HEAD", wt) == path
    assert sh(f"git show 'rev:{path}'", wt) == "print('bye')"
    assert path in reply["stat"]
    assert reply["commit"] == sh("git rev-parse HEAD", wt)
    assert [e["workspace_id"] for e in edited] == ["proj/w-e"]


def test_a_running_session_comes_back_as_a_409_with_the_choices(db, world, monkeypatch):
    spawned = []
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "sup")
    monkeypatch.setattr(agents, "spawn", lambda *a, **k: spawned.append(a))
    code, reply = _send("POST", "/v1/sessions", {"repo": world.repo_root})
    assert code == 409 and spawned == []
    conflict = reply["conflict"]
    assert [c["key"] for c in conflict["choices"]] == ["o", "n", "p"]
    assert [s["id"] for s in conflict["sessions"]] == ["sup"]


@pytest.mark.parametrize("scope,body,peer_pid,status,written", [
    # The picker's pick for a tier is dropped too, and so is an alias of a tier, or either would still win.
    ("user", {"tiers": {"light": ["developer-codex"], "advanced": ["developer-codex"]}}, None, 200,
     {"routing": {"heavy": ["developer"], "light": ["developer-codex"], "advanced": ["developer-codex"]}}),
    ("local", {"reviewer": "reviewer"}, None, 200, {"reviewer": "reviewer"}),
    ("repo", {"tiers": {"light": ["developer"]}, "exclude_providers": ["claude"]}, None, 200,
     {"routing": {"light": ["developer"]}, "exclude_providers": ["claude"]}),
    ("repo", {"tiers": {"heavy": ["developer", "nope"]}}, None, 400, {}),
    pytest.param("repo", {"reviewer": "reviewer"}, 310, 403, {},
                 marks=pytest.mark.variant("an agent holding the token still can't change routing")),
], ids=["user", "local", "repo", "bad-profile", "agent"])
def test_routing_put_writes_its_scope(db, repo, world, monkeypatch, scope, body, peer_pid, status, written):
    monkeypatch.setattr(peer, "agent_caller", lambda pid: "brindle agent w1")
    config.set_user("routing", {"heavy": ["developer"], "advanced": ["developer-heavy"],
                                "reasoning": ["developer-fable"]})
    config.set_user("routing_light", "claude-haiku")
    before = config._read_json(config.scope_paths(str(repo))[scope])
    code, reply = _send("PUT", "/v1/routing", {"repo": str(repo), "scope": scope, **body}, peer_pid)
    assert code == status, reply
    after = config._read_json(config.scope_paths(str(repo))[scope])
    assert after == ({k: v for k, v in before.items() if k != "routing_light"} | written if written else before)
    if "exclude_providers" in written:
        # The light candidate and the default agent both run on claude: routing refuses, not falls back.
        with pytest.raises(autopilot.AutopilotError, match="excluded from routing"):
            autopilot.choose_profile(db, "sup", str(repo), weight="light")


PINNED = AgentSetup(claude=ClaudeSetup(route="bedrock", models=ModelsSetup(opus="us.anthropic.claude-opus-x-v1:0")),
                    enforce=True)


@pytest.mark.parametrize("acc,profile", [
    (Access(org_id="acme", allowed_providers=("claude",)), "developer-codex"),
    (Access(org_id="acme", error="couldn't read the policy"), "developer-codex"),
    # opus is pinned to the us. id; delegation matches a pin exactly, so the eu. one doesn't allow it.
    (Access(org_id="acme", allowed_models=("eu.anthropic.claude-opus-x-v1:0",), agent_setup=PINNED),
     "developer-heavy"),
], ids=["forbidden", "unreadable", "pin-elsewhere"])
def test_routing_put_refuses_what_the_policy_forbids(repo, world, monkeypatch, acc, profile):
    from brindle import providers
    from brindle.pro import managed_models, team_policy

    monkeypatch.setattr(team_policy, "access", lambda repo: acc)
    monkeypatch.setattr(managed_models, "current", lambda repo: None)
    monkeypatch.setattr(providers, "claude_org_managed", lambda: False)
    before = config._read_json(config.scope_paths(str(repo))["user"])
    code, reply = _send("PUT", "/v1/routing", {"repo": str(repo), "scope": "user",
                                               "tiers": {"light": [profile]}})
    assert (code, reply["error"]["code"]) == (403, "forbidden") and reply["error"]["message"]
    assert config._read_json(config.scope_paths(str(repo))["user"]) == before


SECRET = "sk-ant-daemon-test-0123456789wxyz"


@pytest.mark.parametrize("name,value,peer_pid,status", [
    ("ANTHROPIC_API_KEY", SECRET, None, 200),
    pytest.param("ANTHROPIC_API_KEY", SECRET, 310, 403, marks=pytest.mark.variant("an agent holding the token")),
    ("BRINDLE_TOKEN", SECRET, None, 400),
    # http.client refuses the header, quoting the whole value in its error.
    ("ANTHROPIC_API_KEY", SECRET + "\nsecond-line", None, 422),
], ids=["stored", "agent", "unknown-name", "bad-header"])
def test_keys_put_stores_through_keys_set_and_never_echoes_the_value(db, world, monkeypatch, provider_http, caplog,
                                                                     name, value, peer_pid, status):
    import http.client

    from brindle import key_check, keystore

    answer = key_check._get

    def checked_get(url, headers):      # the header check urllib makes before connecting
        conn = http.client.HTTPConnection("localhost")
        conn.putrequest("GET", "/")
        for k, v in headers.items():
            conn.putheader(k, v)
        return answer(url, headers)

    monkeypatch.setattr(key_check, "_get", checked_get)
    monkeypatch.setattr(peer, "agent_caller", lambda pid: "brindle agent w1")
    code, reply = _send("PUT", f"/v1/keys/{name}", {"value": value}, peer_pid)
    assert code == status, reply
    stored = keystore.get_key("ANTHROPIC_API_KEY")
    if status == 200:
        # The same check as `brindle keys set`: the provider is asked, then the key is kept.
        assert provider_http["calls"] == ["https://api.anthropic.com/v1/models"]
        assert reply == {"name": name, "last4": "wxyz", "set": True} and stored == SECRET
    else:
        assert stored is None and provider_http["calls"] == []
    assert SECRET not in json.dumps(reply) and SECRET not in json.dumps(bus.tail(db))
    assert SECRET not in caplog.text


@pytest.mark.parametrize("path,status,command", [
    ("/v1/providers/codex/login", 200, "login"),
    ("/v1/keys/OPENAI_API_KEY/set", 200, "OPENAI_API_KEY"),
    ("/v1/keys/BRINDLE_TOKEN/set", 400, None),
], ids=["login", "key", "bad-key"])
def test_login_and_secret_actions_open_one_terminal_while_it_runs(world, monkeypatch, path, status, command):
    from brindle import providers, tmux
    from brindle.daemon import terminal

    windows = {}            # terminal id -> [window name, argv, alive]
    second: list = []       # a second press, made while the first is creating its window

    def ensure_session(session, cwd, env):
        if not second:
            t = threading.Thread(target=lambda: second.append(_send("POST", path)))
            t.start()
            t.join(0.5)     # it must wait for the first window rather than make its own
            second.append(t)

    def new_window(session, name, cwd, argv, env, tag):
        windows[tag[1]] = [name, argv, True]
        return f"%{len(windows)}"

    monkeypatch.setattr(providers, "codex_binary", lambda: "codex")
    monkeypatch.setattr(tmux, "ensure_session", ensure_session)
    monkeypatch.setattr(tmux, "new_window", new_window)
    monkeypatch.setattr(tmux, "tagged_panes", lambda session, key: [
        (f"%{i + 1}", name, alive, tid) for i, (tid, (name, _, alive)) in enumerate(windows.items())])
    code, reply = _send("POST", path)
    assert code == status, reply
    if command is None:
        assert windows == {} and second == []
        return
    thread = second.pop(0)
    thread.join(5)
    assert second == [(200, {"terminal": reply["terminal"], "started": False})]
    assert reply["started"] and len(windows) == 1 and command in windows[reply["terminal"]][1]
    assert terminal._find(reply["terminal"])["target"] == "%1"
    windows[reply["terminal"]][2] = False       # it exited: the next press starts afresh
    code, again = _send("POST", path)
    assert (code, again["started"], len(windows)) == (200, True, 2)


def test_a_sign_in_takes_its_method_and_the_code_the_person_pastes(world, monkeypatch):
    from brindle import providers, tmux

    windows, typed = {}, []
    monkeypatch.setattr(providers, "claude_binary", lambda: "claude")
    monkeypatch.setattr(tmux, "ensure_session", lambda *a: None)
    monkeypatch.setattr(tmux, "new_window", lambda session, name, cwd, argv, env, tag:
                        windows.setdefault(tag[1], argv))
    monkeypatch.setattr(tmux, "tagged_panes", lambda session, key: [
        (f"%{i + 1}", "login claude", True, tid) for i, tid in enumerate(windows)])
    monkeypatch.setattr(tmux, "send_keys", lambda target, *keys, **kw: typed.append((target, *keys)))
    assert _send("POST", "/v1/providers/claude/login", {"method": "device"})[0] == 400
    code, reply = _send("POST", "/v1/providers/claude/login", {"method": "console"})
    assert code == 200 and windows[reply["terminal"]] == ["claude", "auth", "login", "--console"]
    code, sent = _send("POST", f"/v1/terminals/{reply['terminal']}/input", {"text": "ab-12"})
    assert code == 200 and typed == [("%1", "-l", "--", "ab-12"), ("%1", "Enter")]
    # One printable line only, and only into a running action terminal (never an agent's pane).
    assert _send("POST", f"/v1/terminals/{reply['terminal']}/input", {"text": "a\nb"})[0] == 400
    assert _send("POST", "/v1/terminals/wk/input", {"text": "ab-12"})[0] == 404
    assert len(typed) == 2


OPTIONS = ["Yes", "Yes, and don't ask again for npm test commands in /repo",
           "No, and tell Claude what to do differently (esc)"]
SUGGESTED = [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "npm test:*"}],
              "behavior": "allow", "destination": "localSettings"}]
# Claude Code's PermissionRequest input, as documented: no tool_use_id.
ASK = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "npm test"}}
HOLD = 2    # the repo's permission_hold_seconds in these tests


class _HookConnection:
    """A hook's connection to the daemon's Unix socket, served by the real app
    as an agent's own process reaches it: a peer pid, and no token."""
    paths: list[str] = []

    def __init__(self, path, timeout):
        _HookConnection.paths.append(path)

    def request(self, method, url, body, headers):
        self.reply = _send(method, url, json.loads(body), peer_pid=310, token=None)

    def getresponse(self):
        status, data = self.reply
        return SimpleNamespace(status=status, read=lambda: json.dumps(data).encode())

    def close(self):
        pass


@pytest.fixture
def answering(db, world, monkeypatch):
    """A daemon for the hook to hold requests in, a short hold, and the hook quick to look."""
    from brindle.daemon import holds
    from brindle.service import inbox

    config.set_local(world.repo_root, "permission_hold_seconds", HOLD)
    _HookConnection.paths = []
    monkeypatch.setattr(holds, "_UnixConnection", _HookConnection)
    monkeypatch.setattr(agents, "HOLD_LOOK_SECONDS", 0.05)
    monkeypatch.setattr(inbox, "STEP_SECONDS", 0.01)
    monkeypatch.setattr(inbox, "CONFIRM_SECONDS", 0.5)
    monkeypatch.setattr(peer, "agent_caller", lambda pid: "brindle agent wk")
    # The hook process (peer pid 310) runs under these tests' agents.
    monkeypatch.setattr(peer, "caller_agents", lambda pid: {"wk", "co", "an", "na"} if pid == 310 else set())


def _verdict(out):
    """allow, deny or None, from whichever hook answered (or the native loop)."""
    if isinstance(out, bool):
        return "allow" if out else "deny"
    if out and "hookSpecificOutput" in out:
        return out["hookSpecificOutput"]["decision"]["behavior"]
    return out and out["decision"]


def _brindle_allows_npm_test() -> bool:
    from brindle import permissions

    return any((r.kind, r.match, r.match_type, r.decision) == ("bash", "npm test", "exact", "allow")
               for r in permissions.load_store().rules)


@pytest.mark.parametrize("choice,suggested,race,status,verdict,rule", [
    ("allow_once", True, None, 200, "allow", None),
    # No suggestion from Claude Code: brindle's own rule is written.
    ("allow_always", False, None, 200, "allow", "brindle"),
    # Claude Code's suggestion goes back to it to apply, as if picked in its prompt.
    ("allow_always", True, None, 200, "allow", "claude"),
    ("deny", True, None, 200, "deny", None),
    # "Answer in the terminal instead": the hold ends now, and the CLI's own prompt asks.
    ("terminal", True, None, 200, None, None),
    # Codex's hook holds its request the same way, and so do agy's (under agy_approvals
    # "brindle") and a native worker's loop, which have no prompt of their own.
    ("allow_once", False, "codex", 200, "allow", None),
    ("allow_once", False, "antigravity", 200, "allow", None),
    ("allow_once", False, "native", 200, "allow", None),
    # A deny already in flight holds the request: this answer is refused, writes nothing,
    # and the hold runs out.
    ("allow_always", False, "in-flight", 409, None, None),
    # The hook gives up between the claim and the hand-over: the rule written meanwhile goes again.
    ("allow_always", False, "lost", 409, None, None),
    # The hold expires after the answer is handed over but before the hook takes it: not
    # delivered, so a conflict, and the rule written for it goes again.
    ("allow_once", True, "expired", 409, None, None),
    ("allow_always", False, "expired", 409, None, None),
    # Two requests for the same rule: one allowed always and consumed, the other expired.
    # The rule the first wrote stays.
    ("allow_always", False, "shared", 409, None, "brindle"),
    # The same command denied in the app, asked again, and the retry approved at the CLI's
    # own prompt: its PostToolUse ends the retry's hold, not the old denied request's.
    ("allow_once", True, "retried", None, None, None),
    # The stored request changed while the hook waits: it isn't the request the hook registered.
    ("allow_once", True, "mutated", 409, None, None),
    # An answer written into the database is no answer: the hold runs out.
    ("allow_once", True, "db-answer", None, None, None),
    # A daemon.json rewritten with another token and socket: the hook never reads it.
    ("allow_once", True, "forged-json", 200, "allow", None),
    # Another identity's session: nothing is answered or written.
    ("allow_always", False, "locked", 409, None, None),
    # The agent is closed while it waits: the hold ends, and the request with it.
    ("allow_once", True, "closed", 409, None, None),
    pytest.param("allow_once", True, "agent", 403, None, None,
                 marks=pytest.mark.variant("an agent holding the token can't answer a permission request")),
], ids=["once", "always-brindle", "always-claude", "deny", "terminal", "codex", "antigravity", "native",
        "in-flight", "lost", "expired-once", "expired-always", "shared-rule", "retried", "mutated", "db-answer", "forged-json", "locked",
        "closed", "agent"])
def test_a_permission_answer_resolves_through_the_hook(db, repo, world, answering, monkeypatch, choice,
                                                       suggested, race, status, verdict, rule):
    from brindle import antigravity, identity_lock, permissions
    from brindle.daemon import holds, lifecycle
    from brindle.db import DB
    from brindle.native import runner
    from brindle.service import inbox

    payload = {**ASK, "cwd": str(repo), "permission_suggestions": SUGGESTED if suggested else []}
    agent_id, run = "wk", lambda d, a: agents.permission_request_decision(d, a, payload)
    if race in ("codex", "antigravity", "native"):  # each its own way to the hold, for a command no rule allows
        config.set_local(world.repo_root, "permission_policy", "on")
        agent_id = race[:2]
        db.add_agent(Agent(agent_id, "w-1", "developer", race, "sup", "assign", "processing", "%4", None,
                           time.time()))
        run = {"codex": lambda d, a: agents.permission_request_decision(d, a, {
                   "tool_name": "Bash", "tool_input": {"command": "terraform apply"}, "cwd": str(repo)}, "codex"),
               "antigravity": lambda d, a: antigravity.pre_tool_decision(d, a.id, {
                   "toolCall": {"name": "run_command", "args": {"CommandLine": "terraform apply", "Cwd": str(repo)}},
                   "workspacePaths": [str(repo)]}, strict=True),
               "native": lambda d, a: runner.ask_person(d, a.id, d.get_workspace("w-1"), "Bash",
                                                        {"command": "terraform apply"})}[race]
    if race == "locked":
        monkeypatch.setattr(identity_lock, "locked_by", lambda agent_id: "another@example.com")
    if race == "lost":
        release = holds.HOLDS.release

        def hook_gives_up_first(agent_id, request_id):
            holds.HOLDS._holds.pop(request_id, None)
            return release(agent_id, request_id)

        monkeypatch.setattr(holds.HOLDS, "release", hook_gives_up_first)
    def expire_next():
        take = holds.HOLDS.take

        def too_late(request_id, nonce):    # the hook's first look after the hand-over comes too late
            hold = holds.HOLDS._holds.get(request_id)
            if hold is not None and hold.released:
                monkeypatch.setattr(holds.HOLDS, "clock", lambda: time.time() + 10 * HOLD)
            return take(request_id, nonce)

        monkeypatch.setattr(holds.HOLDS, "take", too_late)

    if race == "expired":
        expire_next()
    if race == "forged-json":
        lifecycle.info_path().write_text(json.dumps({"pid": 1, "token": "forged", "socket": "/tmp/elsewhere.sock"}))
    asked, publish = threading.Event(), bus.publish_agent
    monkeypatch.setattr(bus, "publish_agent", lambda d, kind, *a, **k: (
        kind == "permission.pending" and asked.set(), publish(d, kind, *a, **k))[1])
    if race == "shared":
        # Another request for the same rule, held too. While this one's answer is handed over
        # but not yet taken, the other is allowed always and consumed; then this one expires.
        other_agent, others, other_reply, done = db.get_agent(agent_id), [], [], []
        other = threading.Thread(target=lambda: others.append(run(DB(), other_agent)))
        other.start()
        assert asked.wait(5)
        asked.clear()
        other_id = db.hook_permission_request(agent_id)["tool_use_id"]
        other_digest = next(p.dto for p in inbox.pending_permissions(db, other_agent)
                            if p.dto.request_id == other_id).digest
        take = holds.HOLDS.take

        def other_answered_then_too_late(request_id, nonce):
            hold = holds.HOLDS._holds.get(request_id)
            if request_id != other_id and hold is not None and hold.released and not done:
                done.append(True)
                answering = threading.Thread(target=lambda: other_reply.append(_send(
                    "POST", f"/v1/inbox/{agent_id}/permission",
                    {"request": other_id, "digest": other_digest, "choice": "allow_always"})))
                answering.start()
                answering.join(10)
                monkeypatch.setattr(holds.HOLDS, "clock", lambda: time.time() + 10 * HOLD)
            return take(request_id, nonce)

        monkeypatch.setattr(holds.HOLDS, "take", other_answered_then_too_late)
    if race == "retried":   # the same command asked before, and denied in the app
        denied_agent, denied = db.get_agent(agent_id), []
        first = threading.Thread(target=lambda: denied.append(run(DB(), denied_agent)))
        first.start()
        assert asked.wait(5)
        asked.clear()
        denied_id = db.hook_permission_request(agent_id)["tool_use_id"]
        denied_digest = next(p.dto for p in inbox.pending_permissions(db, denied_agent)
                             if p.dto.request_id == denied_id).digest
        assert _send("POST", f"/v1/inbox/{agent_id}/permission",
                     {"request": denied_id, "digest": denied_digest, "choice": "deny"})[0] == 200
        first.join(5)
        assert [_verdict(o) for o in denied] == ["deny"]
    out, held, started = [], db.get_agent(agent_id), time.time()
    hook = threading.Thread(target=lambda: out.append(run(DB(), held)))
    hook.start()
    assert asked.wait(5)    # the hook holds the request for the app's answer
    request_id = db.hook_permission_request(agent_id)["tool_use_id"]
    def shown_now():    # what the app's inbox item carries
        return next(p.dto for p in inbox.pending_permissions(db, held) if p.dto.request_id == request_id)

    shown = shown_now()
    assert started + HOLD <= shown.hold_until <= time.time() + HOLD     # how long the app may answer
    if race == "in-flight":
        assert holds.HOLDS.claim(agent_id, request_id, shown.digest, "deny")
    if race == "mutated":
        swapped = permissions.Request("claude", "bash", "Bash", command="rm -rf important-data")
        db.conn.execute("UPDATE permission_requests SET request=? WHERE tool_use_id=?",
                        (json.dumps(swapped.to_dict()), request_id))
        shown = shown_now()
    if race == "db-answer":
        db.conn.execute("UPDATE permission_requests SET answer='allow_once' WHERE tool_use_id=?", (request_id,))
    if race == "closed":
        db.update_agent(agent_id, dismissed_at=time.time())
        hook.join(HOLD / 2)     # well before the hold would end
        assert not hook.is_alive()
    if race == "retried":   # approved at the CLI's own prompt: the tool ran, and its PostToolUse came
        agents.note_permission_outcome(db, held, {"tool_name": "Bash", "tool_input": {"command": "npm test"},
                                                  "tool_use_id": "toolu_retry", "cwd": str(repo)})
        hook.join(HOLD / 2)     # well before the hold would end
        assert not hook.is_alive() and holds.HOLDS.held(agent_id, request_id) is None
        assert inbox.pending_permissions(db, held) == []
    if status is not None:
        code, reply = _send("POST", f"/v1/inbox/{agent_id}/permission",
                            {"request": shown.request_id, "digest": shown.digest, "choice": choice},
                            310 if race == "agent" else None)
        assert code == status, reply
    hook.join(HOLD + 5)
    assert [_verdict(o) for o in out] == [verdict]
    if race == "shared":    # the other one was answered, allowed, and its rule is what remains
        other.join(5)
        assert other_reply[0][0] == 200 and [_verdict(o) for o in others] == ["allow"]
    if rule == "claude":
        assert out[0]["hookSpecificOutput"]["decision"]["updatedPermissions"] == SUGGESTED
    assert _brindle_allows_npm_test() == (rule == "brindle")
    assert set(_HookConnection.paths) == {str(lifecycle.socket_path())}    # never daemon.json's
    if status == 200 and verdict is not None:
        assert (reply["via"], reply["cleared"], bool(reply["rule"])) == ("hook", True, rule is not None)
        assert [e["payload"]["choice"] for e in bus.tail(db, kinds=("permission.answered",))] == [choice]


def test_every_held_request_is_offered_and_answered_by_its_own_id(db, repo, world, answering, monkeypatch):
    from brindle import permissions
    from brindle.db import DB
    from brindle.service import inbox

    asked, publish = threading.Semaphore(0), bus.publish_agent
    monkeypatch.setattr(bus, "publish_agent", lambda d, kind, *a, **k: (
        kind == "permission.pending" and asked.release(), publish(d, kind, *a, **k))[1])
    wk, out = db.get_agent("wk"), {}

    def hook(command):
        payload = {**ASK, "cwd": str(repo), "tool_input": {"command": command}}
        out[command] = agents.permission_request_decision(DB(), wk, payload)

    hooks = [threading.Thread(target=hook, args=(c,)) for c in ("npm test", "npm run lint")]
    for t in hooks:     # one after the other: the older one is held first
        t.start()
        assert asked.acquire(timeout=5)
    # The newest row says 'hook', but nothing holds it.
    stale = permissions.Request("claude", "bash", "Bash", command="rm -rf important-data").to_dict()
    db.add_permission_request("wk", "stale", json.dumps(stale), "", None, "hook")
    shown = [i.permission for i in inbox.items(db, str(repo), panes={"%1": True, "%2": True})
             if i.agent_id == "wk" and i.kind == "permission"]
    assert [p.target for p in shown] == ["npm test", "npm run lint"]
    code, reply = _send("POST", "/v1/inbox/wk/permission",
                        {"request": shown[0].request_id, "digest": shown[0].digest, "choice": "allow_once"})
    assert code == 200, reply
    hooks[0].join(5)
    assert _verdict(out["npm test"]) == "allow" and "npm run lint" not in out    # the newer one still waits
    assert [p.dto.target for p in inbox.pending_permissions(db, wk)] == ["npm run lint"]
    hooks[1].join(HOLD + 5)
    assert out["npm run lint"] is None      # its hold ran out: the CLI's own prompt asks


def test_permission_holds_take_no_token_but_only_a_hooks_own_agents(db, world, answering, monkeypatch):
    from brindle import permissions
    from brindle.daemon import holds
    from brindle.service import inbox

    now = [time.time()]
    monkeypatch.setattr(holds.HOLDS, "clock", lambda: now[0])
    monkeypatch.setattr(holds, "MAX_PER_AGENT", 1)
    hold = {"agent_id": "wk", "request_id": "r-tcp", "digest": "d", "nonce": "n", "hold_until": now[0] + 600}

    def register(**fields):
        return _send("POST", "/v1/holds", {**hold, **fields}, peer_pid=310, token=None)[0]

    def offered(request_id):
        recorded = permissions.Request("claude", "bash", "Bash", command="npm test").to_dict()
        db.add_permission_request("wk", request_id, json.dumps(recorded), "", None, "hook")
        return any(p.dto.request_id == request_id for p in inbox.pending_permissions(db, db.get_agent("wk")))

    assert _send("POST", "/v1/holds", hold, token=None)[0] == 403     # TCP: no peer to name
    # Over the Unix socket, only for an agent the calling process runs under, and so many at once.
    assert register(agent_id="sup", request_id="r-other") == 403
    assert register(request_id="r-unix") == 200
    assert register(request_id="r-more") == 409
    assert _send("POST", "/v1/holds/r-unix/end", {"nonce": "n"}, peer_pid=310, token=None)[0] == 200
    # A hook gone without ending its hold stops renewing it: the hold lapses, isn't offered
    # or answerable any more, and frees its place.
    assert register(request_id="r-gone") == 200 and offered("r-gone")
    now[0] += holds.LEASE_SECONDS + 1
    assert not offered("r-gone") and not holds.HOLDS.claim("wk", "r-gone", "d", "allow_once")
    # One whose holder keeps waiting still ends at its hold_until, whatever it claims.
    assert register(request_id="r-due", hold_until=now[0] + 10) == 200
    now[0] += 9
    assert holds.HOLDS.take("r-due", "n") == (True, None)       # renewed
    now[0] += 2
    assert not offered("r-due") and not holds.HOLDS.claim("wk", "r-due", "d", "allow_once")
    assert register(request_id="r-next") == 200


def test_a_permission_request_at_the_clis_own_prompt_gets_no_keys(db, repo, world, answering, monkeypatch):
    from brindle import tmux
    from brindle.daemon import holds
    from brindle.service import inbox

    pressed = []
    monkeypatch.setattr(tmux, "capture", lambda target, **kw: "Bash command\n  npm test\nDo you want to proceed?\n"
                        + "\n".join(f"{'❯' if i == 0 else ' '} {i + 1}. {o}" for i, o in enumerate(OPTIONS)))
    monkeypatch.setattr(tmux, "send_keys", lambda target, *keys, **kw: pressed.append(keys))
    monkeypatch.setattr(holds, "_UnixConnection", lambda path, timeout: (_ for _ in ()).throw(OSError()))
    # No daemon to hold it in: the hook records it and the CLI prompts as usual.
    assert agents.permission_request_decision(db, db.get_agent("wk"), {**ASK, "cwd": str(repo)}) is None
    [shown] = [p.dto for p in inbox.pending_permissions(db, db.get_agent("wk"))]
    assert (shown.via, shown.choices, bool(shown.terminal)) == ("prompt", [], True)    # "Go to it"
    code, reply = _send("POST", "/v1/inbox/wk/permission", {"choice": "allow_once"})
    assert code == 409 and pressed == [], reply


@pytest.fixture
def lead(db, repo, tmp_path, monkeypatch):
    """A session ``lead`` on branch ``sess`` (base main) in its own checkout, its goal reached; jobs run inline."""
    sh(f"git worktree add -q -b sess {tmp_path / 'sess'} main", repo)
    ws = workspaces.adopt_root(db, str(tmp_path / "sess"))
    db.add_agent(Agent("lead", ws.id, "supervisor", "claude", None, "interactive", "idle", "%9", None, time.time()))
    db.add_autopilot("lead")
    db.update_autopilot("lead", state="done")
    monkeypatch.setattr(routes_actions, "_spawn", lambda target: target())
    monkeypatch.setattr(routes_actions, "_PR_WORK", set())
    monkeypatch.setattr(routes_actions, "_PR_TRIED", {})
    return ws


def _commit(path, name: str, text: str, message: str = "work") -> str:
    (path / name).write_text(text)
    sh(f"git add {name} && git commit -qm '{message}'", path)
    return sh("git rev-parse HEAD", path)


def test_session_review_readiness_says_what_is_missing(db, lead, monkeypatch):
    from brindle.daemon import session_review
    from brindle.db import Task

    def missing() -> int:
        code, reply = _send("GET", "/v1/sessions/lead/pr/readiness")
        assert code == 200 and reply["ready"] == (not reply["reasons"]), reply
        return len(reply["reasons"])

    sess = Path(lead.path)
    watch = session_review.ReadinessWatch()
    watch.sweep(db)                 # the first sweep only looks
    assert missing() == 1           # nothing ahead of main
    head = _commit(sess, "a.py", "a\n")
    assert missing() == 0
    # Undone again, one condition at a time: a failing milestone, a running worker, a
    # coordinator-held task, a supervisor waiting at a prompt, and a worker's failed merge.
    autopilot.set_goal(db, "lead", "Ship it", [("tests pass", "true", None)])
    db.add_workspace(Workspace("proj/w-r", lead.repo_root, "feat-r", "worktree", "feat-r", "sess", str(sess),
                               None, "brindle-r", time.time()))
    db.add_agent(Agent("wr", "proj/w-r", "developer", "claude", "lead", "assign", "processing", "%8", None,
                       time.time()))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    db.add_task(Task("t1", lead.repo_root, None, "lead", lead.id, "developer", "later", "assign", 1, None, None,
                     None, None, "held", time.time()))
    db.set_status("lead", "waiting")
    db.add_workspace(Workspace("proj/w-f", lead.repo_root, "feat-f", "worktree", "feat-f", "sess", str(sess),
                               None, "brindle-f", time.time()))
    db.add_agent(Agent("wf", "proj/w-f", "developer", "claude", "lead", "assign", "idle", "%6", "done", time.time()))
    db.update_agent("wf", merge_failed="conflict in a.py")     # as the pipeline records it
    assert missing() == 5 and _send("GET", "/v1/sessions/lead/diff")[0] == 409
    assert watch.sweep(db) == []
    db.set_merged("proj/w-f", head)
    assert missing() == 4
    db.delete_agent("wr")
    assert missing() == 3
    db.update_task("t1", state="cancelled")
    assert missing() == 2
    db.set_status("lead", "idle")
    assert missing() == 1
    db.record_check(db.milestones("lead")[0].id, True, "", head)
    assert missing() == 1           # the completion audit hasn't approved this commit
    db.update_autopilot("lead", state="done", audit_sha=head, audit_ok=1)
    assert missing() == 0
    assert watch.sweep(db) == ["lead"] and watch.sweep(db) == []
    # The person's review edit keeps it ready; any other new commit needs a fresh audit.
    code, edit = _send("PUT", "/v1/sessions/lead/file", {"path": " c.py", "content": "c\n"})
    assert code == 200 and missing() == 0
    _commit(sess, "d.py", "d\n")
    assert missing() == 1
    assert [e["agent_id"] for e in bus.tail(db, kinds=("session.pr_ready",))] == ["lead"]


def test_session_review_diff_attributes_each_hunk_to_its_author(db, repo, lead):
    from brindle import git, history

    sess = Path(lead.path)
    _commit(repo, "shared.py", "".join(f"{n}\n" for n in range(1, 41)))
    sh("git merge -q --ff-only main", sess)
    # Two parallel workers each delete one line of the shared file and merge in turn (line 5
    # survives on feat-b until the last merge): two hunks, each its own deleter.
    for branch, name, gone in (("feat-a", "a.py", 5), ("feat-b", "b.py", 40), ("feat-c", "c.py", 0)):
        sh(f"git switch -q -c {branch} sess", sess)
        (sess / name).write_text(f"{name}\n")
        if gone:
            (sess / "shared.py").write_text("".join(f"{n}\n" for n in range(1, 41) if n != gone))
        # feat-b's commit dated last: a reverse blame then strays onto its branch and credits it with line 5.
        sh(f"git add -A && GIT_COMMITTER_DATE=@{1_700_000_000 + gone} git commit -qm work", sess)
        sh("git switch -q sess", sess)
    for branch in ("feat-a", "feat-b", "feat-c"):
        git.merge_into(lead.repo_root, sess, branch, squash=branch == "feat-c")
    _commit(sess, "app.py", "print('hi')\nprint('lead')\n")
    # feat-a's worker is still there; feat-b's workspace is gone, its report kept; feat-c has neither.
    db.add_workspace(Workspace("proj/w-a", lead.repo_root, "feat-a", "worktree", "feat-a", "sess", str(sess),
                               None, "brindle-a", time.time()))
    db.add_agent(Agent("wa", "proj/w-a", "developer", "claude", "lead", "assign", "idle", "%7", None, time.time()))
    history.record(db, lead.repo_root, "worker_result", agent_id="wb", branch="feat-b", profile="developer-codex")
    code, got = _send("GET", "/v1/sessions/lead/file?path=b.py")
    assert code == 200 and got["content"] == "b.py\n"
    code, edited = _send("PUT", "/v1/sessions/lead/file?path=b.py", {"content": "b.py\nmine\n", "base": got["base"]})
    assert code == 200 and sh("git log -1 --format=%s", sess) == "review edits: b.py"
    code, d = _send("GET", "/v1/sessions/lead/diff")
    assert code == 200, d
    assert {f["path"]: f["authors"] for f in d["files"]} == {
        "a.py": ["wa"], "app.py": ["supervisor"], "b.py": ["wb", "you"], "c.py": ["feat-c"],
        "shared.py": ["wa", "wb"]}
    assert [h["authors"] for f in d["files"] for h in f["hunks"] if f["path"] == "shared.py"] == [["wa"], ["wb"]]
    assert [(w["id"], w["kind"], w["branch"]) for w in d["workers"]] == [
        ("wa", "worker", "feat-a"), ("supervisor", "supervisor", "sess"), ("wb", "worker", "feat-b"),
        ("you", "you", None), ("feat-c", "worker", "feat-c")]


@pytest.mark.parametrize("path,body,setup,status", [
    ("app.py", {"content": "print('bye')\n"}, None, 200),
    ("app.py", {}, "delete", 200),
    ("new/deep/n.py", {"content": "n\n"}, "hook-refuses", 409),    # the file and its new directories go
    ("app.py", {"content": "x\n"}, "exists", 409),                  # a create where a file already is
    ("app.py", {"content": "print('bye')\n"}, "busy", 409),
    ("app.py", {"content": "print('bye')\n"}, "merging", 409),
], ids=["edit", "delete", "create-refused", "exists", "busy", "merging"])
def test_session_review_edits_commit_on_the_session_branch(db, repo, lead, path, body, setup, status):
    from brindle import git

    sess = Path(lead.path)
    head = sh("git rev-parse HEAD", sess)
    base = git.hash_blob(sess, (sess / "app.py").read_bytes(), "app.py")
    if setup == "busy":
        db.set_status("lead", "processing")
    if setup == "hook-refuses":
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
    body = body if setup in ("hook-refuses", "exists") else {**body, "base": base}
    with pipeline.base_lock(lead.repo_root, "sess") if setup == "merging" else contextlib.nullcontext():
        code, reply = _send("DELETE" if setup == "delete" else "PUT", f"/v1/sessions/lead/file?path={path}", body)
    assert code == status, reply
    if status != 200:
        assert sh("git rev-parse HEAD", sess) == head and not (sess / "new").exists()
        assert sh("git status --porcelain", sess) == "" and bus.tail(db, kinds=("session.edited",)) == []
        return
    assert sh("git log -1 --format=%s", sess) == "review edits: app.py" and reply["commit"] == sh(
        "git rev-parse HEAD", sess)
    assert sh("git ls-files app.py", sess) == ("" if setup == "delete" else "app.py")


def test_session_review_pr_opens_only_once_the_session_is_ready(db, lead, tmp_path, monkeypatch):
    sess, called = Path(lead.path), tmp_path / "gh-called"
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {called}\necho https://github.com/o/r/pull/7\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    code, reply = _send("POST", "/v1/sessions/lead/pr", {"title": "Ship it"})
    assert (code, reply["error"]["code"]) == (409, "not_ready") and reply["reasons"]
    assert not called.exists()
    autopilot.set_goal(db, "lead", "Ship it", [("tests pass", "true", None)])
    db.record_check(db.milestones("lead")[0].id, True, "", "sha", passed_sha="sha")
    autopilot.clear_finished_goal(db, "lead")       # reached: the goal and its milestones stay
    db.update_autopilot("lead", state="done", audit_sha=_commit(sess, "a.py", "a\n"), audit_ok=1)
    code, draft = _send("GET", "/v1/sessions/lead/pr/draft")
    assert code == 200 and draft["title"] == "Ship it" and "- tests pass" in draft["body"]
    code, reply = _send("POST", "/v1/sessions/lead/pr", {"title": draft["title"], "body": draft["body"]})
    assert code == 202, reply
    done = bus.tail(db, kinds=("job.done", "job.failed"))
    assert done[-1]["payload"]["result"]["url"] == "https://github.com/o/r/pull/7"
    assert "--base main --head sess --title Ship it --body" in called.read_text()


@pytest.mark.parametrize("auto_pr,org,opened", [
    (True, None, True),
    (False, None, False),
    (True, "requires-review", False),
], ids=["on", "off", "policy-forbids"])
def test_session_review_auto_pr_opens_once_when_ready_and_allowed(db, lead, tmp_path, monkeypatch,
                                                                   auto_pr, org, opened):
    from brindle.daemon import session_review
    from brindle.pro import team_policy

    sess, called = Path(lead.path), tmp_path / "gh-called"
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {called}\necho https://github.com/o/r/pull/7\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    if org:
        monkeypatch.setattr(team_policy.ProPolicy, "policy", lambda self: team_policy.OrgPolicy(
            "acme", 1, require_human_review=True))
    config.set_local(lead.repo_root, "auto_pr", auto_pr)
    watch = session_review.ReadinessWatch()
    watch.sweep(db)
    _commit(sess, "a.py", "a\n")
    assert watch.sweep(db) == ["lead"]
    code, ready = _send("GET", "/v1/sessions/lead/pr/readiness")
    assert code == 200 and (ready["ready"], ready["auto_pr"]) == (True, opened)
    _, cfg = _send("GET", f"/v1/config?repo={lead.repo_root}")
    assert (cfg["repo"]["auto_pr"], cfg["auto_pr_forbidden"] is None) == (auto_pr, not org)
    # Not ready (uncommitted work), then ready again with a new commit: no second PR, only a push.
    (sess / "a.py").write_text("b\n")
    assert watch.sweep(db) == []
    head = _commit(sess, "a.py", "b\n")
    assert watch.sweep(db) == ["lead"] and watch.sweep(db) == []
    creates = called.read_text().count("pr create") if called.exists() else 0
    urls = [e["payload"]["url"] for e in bus.tail(db, kinds=("session.pr_opened",))]
    pushed = sh("git ls-remote origin sess", sess).split()[:1]
    assert (creates, urls, pushed == [head]) == ((1, ["https://github.com/o/r/pull/7"], True) if opened
                                                  else (0, [], False))


def test_session_review_auto_pr_survives_gh_failing_a_restart_and_another_identity(db, lead, tmp_path, monkeypatch):
    from brindle import identity_lock

    sess, called, fail = Path(lead.path), tmp_path / "gh-called", tmp_path / "gh-fails"
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {called}\n[ -f {fail} ] && exit 1\necho https://github.com/o/r/pull/7\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    config.set_local(lead.repo_root, "auto_pr", True)
    _commit(sess, "a.py", "a\n")

    def prs() -> list[tuple]:
        return [tuple(r) for r in db.conn.execute("SELECT url, pushed_sha FROM session_prs")]

    # gh can't open it: the compare page is no PR, so nothing is recorded and the next turn to ready retries.
    fail.touch()
    routes_actions.auto_pr(db, "lead", opening=True)
    assert prs() == [] and bus.tail(db, kinds=("session.pr_opened",)) == []
    fail.unlink()
    creates = called.read_text().count("pr create")

    def remote() -> str:
        return sh("git ls-remote origin sess", sess).split()[0]

    # Its job dispatched but not yet run: a second trigger leaves that work alone, and someone
    # else signing in before it runs keeps it from pushing or opening anything under their name.
    head = _commit(sess, "a.py", "b\n")
    held = []
    monkeypatch.setattr(routes_actions, "_spawn", held.append)
    assert routes_actions.auto_pr(db, "lead", opening=True) is not None
    assert routes_actions.auto_pr(db, "lead", opening=True) is None and len(held) == 1 and prs() == [(None, None)]
    monkeypatch.setattr(identity_lock, "locked_by", lambda agent_id: "someone@other.example")
    held.pop()()
    assert prs() == [] and remote() != head and called.read_text().count("pr create") == creates
    monkeypatch.setattr(identity_lock, "locked_by", lambda agent_id: None)
    # The daemon stopped before running its job: after a restart the claim is matched to the PR gh finds open.
    routes_actions.auto_pr(db, "lead", opening=True)
    monkeypatch.setattr(routes_actions, "_PR_WORK", set())
    monkeypatch.setattr(routes_actions, "_spawn", lambda target: target())
    routes_actions.auto_pr(db, "lead", opening=False)
    assert prs() == [("https://github.com/o/r/pull/7", None)] and called.read_text().count("pr create") == creates
    routes_actions.auto_pr(db, "lead", opening=False)
    assert prs() == [("https://github.com/o/r/pull/7", head)]
    # A push that fails is tried again (here on the session's next turn to ready), with no second PR.
    origin = sh("git remote get-url origin", sess)
    sh("git remote set-url origin /nowhere", sess)
    head = _commit(sess, "a.py", "c\n")
    routes_actions.auto_pr(db, "lead", opening=False)
    sh(f"git remote set-url origin {origin}", sess)
    assert routes_actions.auto_pr(db, "lead", opening=False) is None and prs()[0][1] != head
    routes_actions.auto_pr(db, "lead", opening=True)
    assert prs()[0][1] == head == remote() and called.read_text().count("pr create") == creates
    # Signed in as someone else: the session's new commit isn't pushed under their name.
    monkeypatch.setattr(identity_lock, "locked_by", lambda agent_id: "someone@other.example")
    newer = _commit(sess, "a.py", "d\n")
    assert routes_actions.auto_pr(db, "lead", opening=False) is None and prs()[0][1] == head != newer


def _provider_setup(tmp_path, monkeypatch, onboarded: bool = True) -> tuple:
    """Every CLI installed (a stub), Claude Code's and Codex's config dirs present."""
    monkeypatch.setattr(preflight, "check", REAL_PREFLIGHT)
    monkeypatch.setattr(providers, "claude_onboarded", REAL_ONBOARDED)
    for env in ("BRINDLE_CLAUDE_BIN", "BRINDLE_CODEX_BIN", "BRINDLE_AGY_BIN"):
        (tmp_path / env).write_text("")
        monkeypatch.setenv(env, str(tmp_path / env))
    claude = tmp_path / "claude-config" / ".claude.json"
    claude.parent.mkdir()
    claude.write_text(json.dumps({"hasCompletedOnboarding": True} if onboarded else {"numStartups": 1}))
    (tmp_path / "codex-home").mkdir()
    return claude, tmp_path / "codex-home" / "config.toml"


def _codex_trust(codex, repo, level):
    import os

    # Quoted and commented as a person might: still the repo's own table.
    codex.write_text(f"model = \"x\"\n\n[projects.'{os.path.realpath(repo)}'] # mine\ntrust_level = \"{level}\"\n")


@pytest.mark.parametrize("how,provider,setup,blocked", [
    ("/v1/sessions", "claude", "signed-out", ["signin"]),
    ("/v1/sessions", "codex", None, []),            # Codex's trust is recorded silently, then it launches
    ("/v1/sessions", "codex", "untrusted", ["trust"]),   # the person's own answer to Codex is never overridden
    # Signed in by the key brindle stores for it, which Claude Code then asks about.
    ("/v1/sessions", "claude", "stored-key", ["consent"]),
    # "open" a session that has since exited: a fresh one would start, so it's checked.
    ("/v1/sessions?open", "claude", "signed-out", ["signin"]),
    ("/v1/sessions/sup/continue", "claude", "signed-out", ["signin"]),
    ("/v1/agents/wk/rewind", "claude", "signed-out", ["signin"]),
    # A worker started any way (assign, handoff, a queued task) goes through the same launch.
    ("assign", "claude", "fresh", ["consent"]),
    ("assign", "codex", "untrusted", ["trust"]),
    # The session's own key (its session_credentials), not brindle's: approving it clears the worker.
    ("assign", "claude", "session-key", ["consent"]),
    ("attached", "claude", "session-key", ["consent"]),   # the same, for a worker in a repo attached to it
    ("interactive", "claude", "fresh", []),         # a chat in a terminal: the person answers it there
], ids=["start", "start-trusts-silently", "start-untrusted", "start-stored-key", "stale-open", "continue",
        "rewind", "worker-consent", "worker-untrusted", "worker-session-key", "worker-session-key-attached",
        "terminal-chat"])
def test_preflight_holds_a_launch_its_cli_would_stop_on(db, world, tmp_path, monkeypatch, how, provider, setup,
                                                        blocked):
    from brindle import keystore

    _, codex = _provider_setup(tmp_path, monkeypatch, onboarded=setup != "fresh")
    if setup == "untrusted":
        _codex_trust(codex, world.repo_root, "untrusted")
    if setup == "stored-key":
        keystore.set_key("ANTHROPIC_API_KEY", SECRET)
    if setup == "session-key":
        db.set_session_credential("sup", "ANTHROPIC_API_KEY", SECRET)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ambient-0000000000abcd")
    before = codex.read_text() if codex.exists() else None
    monkeypatch.setattr(providers, "signed_out", lambda p, env=None: "Claude Code isn't signed in" if (
        p == "claude" and setup in ("signed-out", "stored-key") and not (env or {}).get("ANTHROPIC_API_KEY")) else None)
    opened = []

    def open_window(*a, **k):
        opened.append(a)
        raise agents.AgentError("stopped at the window")

    monkeypatch.setattr(agents, "_open_window", open_window)
    if how.startswith("/"):
        path, _, opening = how.partition("?")
        body = {"repo": world.repo_root, "provider": provider, "to": 1}
        code, reply = _send("POST", path, {**body, "choice": "o", "session_id": "sup"} if opening else body)
        assert code == 409 and SECRET not in json.dumps(reply), reply
        kinds = [i["kind"] for i in reply.get("preflight", [])]
        assert all(i["action"]["path"].startswith(f"/v1/providers/{provider}/") for i in reply.get("preflight", []))
    else:
        from brindle import repos
        from brindle.pro import license

        ws, worker = world, how in ("assign", "attached")
        for name in ("other", "unrelated") if setup == "session-key" else ():
            sh(f"git init -q -b main {tmp_path / name} && git -C {tmp_path / name} commit -q --allow-empty -m init",
               tmp_path)
        if how == "attached":
            monkeypatch.setattr(license, "has", lambda feature: feature == "multi_repo")
            ws = repos.target_workspace(db, repos.attach(db, "sup", str(tmp_path / "other")).repo_root)

        def spawn():
            with pytest.raises(agents.AgentError) as e:
                agents.spawn(db, ws, "developer" if worker else "supervisor", provider_name=provider,
                             mode="assign" if worker else how, parent_id="sup" if worker else None, prompt="go")
            return getattr(e.value, "items", [])

        items = spawn()
        kinds = [i.kind for i in items]
        if setup == "session-key":
            action = items[0].action
            assert "wxyz" in items[0].detail and action.body["session"] == "sup"
            assert action.body["repo"] == ws.repo_root
            confirm = {**action.body, "accept": True}
            assert _send("POST", action.path, {**confirm, "key": "0" * 16})[0] == 409   # some other key
            assert _send("POST", action.path, {**confirm, "session": "wk"})[0] == 409   # not a session root
            # A repo that is neither the session's own nor attached to it.
            assert _send("POST", action.path, {**confirm, "repo": str(tmp_path / "unrelated")})[0] == 409
            assert _send("POST", action.path, confirm)[0] == 200
            assert providers.api_key_approved(SECRET)
            assert not providers.api_key_approved("sk-ant-ambient-0000000000abcd")
            assert spawn() == [] and len(opened) == 1      # the same worker now launches
            opened.clear()
    assert kinds == blocked and len(opened) == (0 if blocked else 1)
    if blocked:
        assert (codex.read_text() if codex.exists() else None) == before
    elif provider == "codex":
        assert providers.codex_trust(world.repo_root) == "trusted"


@pytest.mark.parametrize("provider,setup,item", [
    ("claude", "missing", "claude.install"),
    ("antigravity", "gemini", "key.GEMINI_API_KEY"),
    ("claude", "fresh", "claude.onboarding"),
    ("claude", "key", "claude.api_key"),
    ("codex", "untrusted", "codex.trust"),
], ids=["setup", "key", "consent-onboarding", "consent-api-key", "trust"])
def test_preflight_lists_each_blocking_item_and_only_the_person_clears_it(db, world, tmp_path, monkeypatch,
                                                                          provider, setup, item):
    claude, codex = _provider_setup(tmp_path, monkeypatch, onboarded=setup != "fresh")
    probed = []
    monkeypatch.setattr(providers, "signed_out", lambda p, env=None: probed.append(env or {}))
    # The repo's own supervisor profile sets a variable that would run code in
    # the CLI's status check: asking (a GET) must never hand it over.
    agents_dir = Path(world.repo_root) / ".brindle" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    # (Declaring it as the profile's key name doesn't get it through either.)
    # The "key" setup: the profile's own key, with a different one in brindle's environment.
    (agents_dir / "supervisor.md").write_text(
        f"---\nname: supervisor\nprovider: {provider}\napi_key_env: NODE_OPTIONS\n"
        f"env.NODE_OPTIONS: --require ./x.js\n{'env.ANTHROPIC_API_KEY: ' + SECRET + chr(10) if setup == 'key' else ''}"
        "---\nYou supervise.\n")
    if setup == "missing":
        monkeypatch.setenv("BRINDLE_CLAUDE_BIN", str(tmp_path / "nowhere" / "claude"))
    if setup == "gemini":
        settings = tmp_path / "agy-home" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"modelProvider": "gemini"}')
    if setup == "key":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ambient-0000000000abcd")
    if setup == "untrusted":
        _codex_trust(codex, world.repo_root, "untrusted")
    configs = lambda: [p.read_text() if p.exists() else None for p in (claude, codex)]  # noqa: E731
    before = configs()
    query = f"/v1/providers/{provider}/preflight?repo={world.repo_root}"
    code, items = _send("GET", query)
    assert code == 200, items
    found = next(i for i in items if i["id"] == item)
    assert SECRET not in json.dumps(items) and configs() == before     # asking changes nothing
    assert all("NODE_OPTIONS" not in env for env in probed)
    action = found["action"]
    if action is None or action["name"] == "set_key":
        return
    if found["kind"] == "consent":
        assert found["link"] or found["detail"]
        # Nothing records a consent but the person's own confirmation.
        assert _send(action["method"], action["path"], action["body"])[0] == 400 and configs() == before
    if setup == "key":
        assert "wxyz" in found["detail"]        # the profile's key, not brindle's own
        # A confirmation of some other key than the one shown records nothing.
        stale = {**action["body"], "accept": True, "key": "0" * 16}
        assert _send(action["method"], action["path"], stale)[0] == 409 and configs() == before
    code, reply = _send(action["method"], action["path"], {**action["body"], "accept": True})
    assert code == 200, reply
    assert item not in [i["id"] for i in _send("GET", query)[1]]
    if setup == "key":
        assert providers.api_key_approved(SECRET) and not providers.api_key_approved("sk-ant-ambient-0000000000abcd")
    if setup == "untrusted":
        assert codex.read_text().startswith('model = "x"\n')


def test_a_scratch_session_transfers_into_a_repo_and_a_real_one_is_refused(db, world, repo, tmp_path,
                                                                           monkeypatch):
    from brindle import git, scratch

    elsewhere = tmp_path / "Downloads"
    elsewhere.mkdir()
    s = scratch.create(db, str(elsewhere))
    db.add_agent(Agent("scr", s.id, "supervisor", "claude", None, "interactive", "idle", "%3", None,
                       time.time()))
    (Path(s.path) / "notes.md").write_text("plan\n")
    git.commit_all(s.path, "Add notes")

    queued = []
    monkeypatch.setattr(routes_actions, "_spawn", queued.append)      # the first job stays pending
    code, reply = _send("POST", "/v1/sessions/scr/transfer", {"target_path": str(repo)})
    assert code == 202, reply
    assert _send("POST", "/v1/sessions/scr/transfer", {"target_path": str(repo)}) == (202, reply)
    other = _send("POST", "/v1/sessions/scr/transfer", {"target_path": str(tmp_path)})
    assert (other[0], other[1]["error"]["code"]) == (409, "conflict")
    assert len(queued) == 1
    queued[0]()
    done = bus.tail(db, kinds=("job.done", "job.failed"))
    assert [(e["kind"], e["payload"]["job"]) for e in done] == [("job.done", reply["job"])]
    moved = db.get_workspace(done[0]["payload"]["result"]["workspace_id"])
    assert (Path(moved.path) / "notes.md").read_text() == "plan\n"

    code, reply = _send("POST", "/v1/sessions/sup/transfer", {"target_path": str(repo)})
    assert (code, reply["error"]["code"]) == (400, "invalid")


@pytest.mark.parametrize("exit_code", [0, 1], ids=["done", "failed"])
def test_upgrade_runs_in_the_background_and_reports_on_the_bus(db, world, monkeypatch, exit_code):
    output = "\n".join(f"uv line {i}" for i in range(30))
    runs, again = [], []

    def run():
        runs.append(1)
        again.append(_send("POST", "/v1/engine/upgrade"))      # pressed again while it runs
        return exit_code, output

    monkeypatch.setattr(routes_actions, "_run_upgrade", run)
    code, reply = _send("POST", "/v1/engine/upgrade")
    assert code == 202 and runs == [1] and again == [(202, reply)]
    events = bus.tail(db, kinds=("job.progress", "job.done", "job.failed"))
    assert [(e["kind"], e["payload"]["job"]) for e in events] == [
        ("job.progress", reply["job"]), ("job.done" if exit_code == 0 else "job.failed", reply["job"])]
    if exit_code == 0:
        assert "uv line" not in json.dumps(events)
    else:
        error = events[-1]["payload"]["error"]
        assert error["code"] == "upgrade_failed" and error["tail"] == [f"uv line {i}" for i in range(10, 30)]


def test_user_settings_round_trip_validates_and_keeps_other_keys(db, world, monkeypatch, tmp_path):
    monkeypatch.delenv("BRINDLE_HOME", raising=False)
    monkeypatch.delenv("BRINDLE_AGENT_ID", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".brindle" / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"delegation": "fast"}))

    assert _send("GET", "/v1/user-settings") == (200, {"claude_permission_mode": "default"})
    assert _send("PATCH", "/v1/user-settings", {"claude_permission_mode": "yolo"})[0] == 400
    assert _send("PATCH", "/v1/user-settings", {})[0] == 400
    assert _send("PATCH", "/v1/user-settings", {"claude_permission_mode": "auto"})[0] == 200
    assert _send("GET", "/v1/user-settings") == (200, {"claude_permission_mode": "auto"})
    assert json.loads(path.read_text()) == {"delegation": "fast", "claude_permission_mode": "auto"}
    assert not list(path.parent.glob("*.tmp"))


def test_kb_sources_add_sync_and_remove_a_folder(db, repo, world, tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# Alpha\n\nThe first note.\n")
    q = f"?repo={repo}"

    assert _send("POST", "/v1/kb/sources" + q, {"path": str(tmp_path / "nope")})[0] == 400
    assert _send("POST", "/v1/kb/sources", {"path": str(docs)})[0] == 400      # no repo
    code, added = _send("POST", "/v1/kb/sources" + q, {"path": str(docs), "private": True})
    assert code == 202
    (source,) = _send("GET", "/v1/kb/sources" + q)[1]
    assert source["docs"] == 1 and source["visibility"] == "personal"

    (docs / "b.md").write_text("# Beta\n\nThe second note.\n")
    code, synced = _send("POST", f"/v1/kb/sources/{source['id'][:8]}/sync" + q)
    assert code == 202
    assert _send("GET", "/v1/kb/sources" + q)[1][0]["docs"] == 2
    done = {e["payload"]["job"]: e["payload"]["result"] for e in bus.tail(db, kinds=("job.done",))}
    assert done[synced["job"]]["indexed"] == 1 and added["job"] in done

    queued = []                                     # a job still running: add and sync share it
    monkeypatch.setattr(routes_actions, "_spawn", queued.append)
    again = _send("POST", f"/v1/kb/sources/{source['id']}/sync" + q)[1]
    assert _send("POST", f"/v1/kb/sources/{source['id']}/sync" + q)[1] == again
    assert _send("POST", "/v1/kb/sources" + q, {"path": str(docs)})[1] == again and len(queued) == 1
    queued[0]()
    assert _send("POST", f"/v1/kb/sources/{source['id']}/sync" + q)[1] != again

    assert _send("DELETE", "/v1/kb/sources/nope" + q)[0] == 404
    assert _send("DELETE", f"/v1/kb/sources/{source['id']}" + q)[0] == 200
    assert _send("GET", "/v1/kb/sources" + q)[1] == []


def test_set_user_overlapping_writes_keep_both_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path))
    monkeypatch.delenv("BRINDLE_AGENT_ID", raising=False)
    real_replace, second = os.replace, threading.Thread(target=config.set_user, args=("b", 2))

    def replace(src, dst):
        if not second.is_alive() and second.ident is None:
            second.start()
            second.join(0.3)        # a second writer arriving mid-write
        real_replace(src, dst)

    monkeypatch.setattr(config.os, "replace", replace)
    config.set_user("a", 1)
    second.join()
    assert json.loads((tmp_path / "config.json").read_text()) == {"a": 1, "b": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


@pytest.mark.parametrize("fails", [False, True], ids=["done", "failed"])
def test_setup_codex_hook_trusts_it_in_the_background(db, world, monkeypatch, tmp_path, fails):
    from brindle import codex_hook

    monkeypatch.setenv("HOME", str(tmp_path))

    trusted, again = [], []
    state = SimpleNamespace(command="hook")
    monkeypatch.setattr(codex_hook, "inspect", lambda binary, command=None: state)

    def trust(binary, st):
        trusted.append(st)
        again.append(_send("POST", "/v1/setup/codex-hook"))      # pressed again while it runs
        if fails:
            raise codex_hook.CodexHookError("Codex didn't answer")

    monkeypatch.setattr(codex_hook, "trust", trust)
    code, reply = _send("POST", "/v1/setup/codex-hook")
    assert code == 202 and trusted == [state] and again == [(202, reply)]
    events = bus.tail(db, kinds=("job.done", "job.failed"))
    assert [(e["kind"], e["payload"]["job"]) for e in events] == [
        ("job.failed" if fails else "job.done", reply["job"])]
    if fails:
        assert events[0]["payload"]["error"]["code"] == "codex_hook_failed"
