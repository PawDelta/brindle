"""The daemon's action routes: each reaches its engine call, a long one as a
job whose end arrives on the bus, and a choice the person must make is a 409."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import sh

from brindle import agents, autopilot, bus, config, coordinator, pipeline, workspaces
from brindle.daemon import peer, routes_actions
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace
from brindle.pro.account import ProAccount
from brindle.pro.team_policy import Access, AgentSetup, ClaudeSetup, ModelsSetup

TOKEN = "t" * 43


def _send(method: str, path: str, body: dict | None = None, peer_pid: int | None = None) -> tuple[int, dict]:
    raw = json.dumps(body or {}).encode()
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
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
    ("app.py", "e69de29", None, 409),           # the empty blob: not what the file is now
    ("app.py", "shown", "busy", 409),
    ("app.py", "shown", "locked", 409),         # the commit is refused: the worker's index survives
    ("../proj/app.py", "shown", None, 400),
    ("out/m.py", "shown", None, 400),           # a symlinked directory leading out of the worktree
    pytest.param(".git/HEAD", "shown", None, 400, marks=pytest.mark.variant("the worktree's own git link")),
    pytest.param(":(glob)*", "shown", None, 200,
                 marks=pytest.mark.variant("a name that is pathspec magic must not commit other work")),
    pytest.param("pkg/m.py", "shown", "swap", 200,
                 marks=pytest.mark.variant("a directory swapped for an escaping symlink mid-edit")),
], ids=["edit-commits", "edit-stale", "edit-busy", "edit-refused", "edit-dotdot", "edit-symlink", "edit-git",
        "edit-pathspec", "edit-swap"])
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
    outside.mkdir()
    (outside / "m.py").write_text("outside\n")
    (wt / "out").symlink_to(outside)
    db.add_workspace(Workspace("proj/w-e", world.repo_root, "rev", "worktree", "rev", "main", str(wt),
                               None, "brindle-z", time.time()))
    if setup == "busy":
        db.add_agent(Agent("ew", "proj/w-e", "developer", "claude", "sup", "assign", "processing", "%3",
                           None, time.time()))
    if setup == "locked":
        (repo / ".git" / "refs" / "heads" / "rev.lock").write_text("")
    if setup == "swap":
        real = git.hash_blob

        def swap_then_hash(*a, **k):        # after the file is opened, before it is written
            if not (wt / "pkg.moved").exists():
                (wt / "pkg").rename(wt / "pkg.moved")
                (wt / "pkg").symlink_to(outside)
            return real(*a, **k)

        monkeypatch.setattr(git, "hash_blob", swap_then_hash)
    head, staged = sh("git rev-parse HEAD", wt), sh("git diff --cached", wt)
    shown = sh(f"git hash-object {'app.py' if path == 'app.py' else 'pkg/m.py'}", wt)[:7]
    code, reply = _send("PUT", "/v1/workspaces/proj/w-e/file",
                        {"path": path, "content": "print('bye')\n", "base": shown if base == "shown" else base})
    assert code == status, reply
    edited = bus.tail(db, kinds=("workspace.edited",))
    assert (outside / "m.py").read_text() == "outside\n" and (repo / "app.py").read_text() == "print('hi')\n"
    if status != 200:
        assert sh("git rev-parse HEAD", wt) == head and sh("git diff --cached", wt) == staged and edited == []
        assert (wt / "app.py").read_text() == "print('working')\n"
        return
    assert sh("git log -1 --format=%s rev", wt) == f"edit from review: {path}"
    assert sh("git diff-tree --no-commit-id --name-only -r HEAD", wt) == path
    assert sh(f"git show 'rev:{path}'", wt) == "print('bye')"
    cached = sh("git diff --cached --name-only", wt).splitlines()
    assert "abc.txt" in cached and path not in cached
    if setup != "swap":     # swapped, the worktree's pkg is the symlink and the write went to pkg.moved
        assert (wt / path).read_text() == "print('bye')\n" and path in reply["stat"]
    else:
        assert (wt / "pkg.moved" / "m.py").read_text() == "print('bye')\n"
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
    ("/v1/engine/upgrade", 200, "upgrade"),
    ("/v1/keys/BRINDLE_TOKEN/set", 400, None),
], ids=["login", "key", "upgrade", "bad-key"])
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
