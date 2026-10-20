"""The daemon's action routes: each reaches its engine call, a long one as a
job whose end arrives on the bus, and a choice the person must make is a 409."""

import asyncio
import json
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


def _send(method: str, path: str, body: dict | None = None, peer_pid: int | None = None) -> tuple[int, dict]:
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
             "headers": [(b"authorization", f"Bearer {TOKEN}".encode()),
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
    ("interactive", "claude", "fresh", []),         # a chat in a terminal: the person answers it there
], ids=["start", "start-trusts-silently", "start-untrusted", "start-stored-key", "stale-open", "continue",
        "rewind", "worker-consent", "worker-untrusted", "terminal-chat"])
def test_preflight_holds_a_launch_its_cli_would_stop_on(db, world, tmp_path, monkeypatch, how, provider, setup,
                                                        blocked):
    from brindle import keystore

    _, codex = _provider_setup(tmp_path, monkeypatch, onboarded=setup != "fresh")
    if setup == "untrusted":
        _codex_trust(codex, world.repo_root, "untrusted")
    if setup == "stored-key":
        keystore.set_key("ANTHROPIC_API_KEY", SECRET)
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
        with pytest.raises(agents.AgentError) as e:
            agents.spawn(db, world, "developer" if how == "assign" else "supervisor", provider_name=provider,
                         mode=how, parent_id="sup" if how == "assign" else None, prompt="go")
        kinds = [i.kind for i in getattr(e.value, "items", [])]
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
    (agents_dir / "supervisor.md").write_text(f"---\nname: supervisor\nprovider: {provider}\n"
                                              "env.NODE_OPTIONS: --require ./x.js\n---\nYou supervise.\n")
    if setup == "missing":
        monkeypatch.setenv("BRINDLE_CLAUDE_BIN", str(tmp_path / "nowhere" / "claude"))
    if setup == "gemini":
        settings = tmp_path / "agy-home" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"modelProvider": "gemini"}')
    if setup == "key":
        monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
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
    code, reply = _send(action["method"], action["path"], {**action["body"], "accept": True})
    assert code == 200, reply
    assert item not in [i["id"] for i in _send("GET", query)[1]]
    if setup == "untrusted":
        assert codex.read_text().startswith('model = "x"\n')


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
