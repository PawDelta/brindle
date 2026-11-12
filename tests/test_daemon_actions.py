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
from conftest import run_scenario, sh
from test_daemon_api import _repo_template, repo  # noqa: F401 - a copied template repo, not conftest's rebuild
from test_reliability import CLAUDE_BUSY, CLAUDE_IDLE

from brindle import agents, autopilot, bus, config, coordinator, pipeline, preflight, providers, workspaces
from brindle.daemon import holds, peer, routes_actions
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace
from brindle.pro.account import ProAccount
from brindle.pro.team_policy import Access, AgentSetup, ClaudeSetup, ModelsSetup
from brindle.service.dto import SessionDTO

TOKEN = "t" * 43
REAL_PREFLIGHT = preflight.check
REAL_ONBOARDED = providers.claude_onboarded


def test_repo_git_identity_is_set_for_that_repo_only(db, world, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from brindle import git
    from brindle.cli import app
    from brindle.service import sessions as svc

    home = tmp_path / "home"
    home.mkdir()
    global_config = home / ".gitconfig"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(key, raising=False)
    for key in ("user.name", "user.email"):
        git.run(["config", "--local", "--unset-all", key], world.repo_root, check=False)
    started = []
    real_start = svc.start
    monkeypatch.setattr(svc, "start", lambda *a, **k: started.append(True) or {"id": "new"})
    body = {"repo": world.repo_root}
    code, reply = _send("POST", "/v1/sessions", body)
    assert (code, reply["error"]["code"], started) == (409, "git_identity_missing", [])
    monkeypatch.chdir(world.repo_root)
    missing_checkout = tmp_path / "new-checkout"
    for branch in ([], ["--branch", "new-session"], ["--branch", "new-session", "--worktree", str(missing_checkout)]):
        assert CliRunner().invoke(app, ["start", "--no-attach", "--yes", *branch]).exit_code == 1
    assert not missing_checkout.exists()
    assert started == []
    for name, email in [("", "me@example.com"), ("Me", "--global")]:
        assert _send("POST", "/v1/repos/git-identity", {**body, "name": name, "email": email})[0] == 400
        assert git.run(["config", "--get", "user.name"], world.repo_root, check=False).returncode == 1
    assert _send("POST", "/v1/repos/git-identity", {**body, "name": "--Name", "email": "me@example.com"})[0] == 200
    assert git.out(["config", "--local", "user.name"], world.repo_root) == "--Name"
    assert git.out(["config", "--local", "user.email"], world.repo_root) == "me@example.com"
    checkout = tmp_path / "checkout"
    git.run(["worktree", "add", "--detach", str(checkout)], world.repo_root)
    assert git.out(["config", "user.email"], checkout) == "me@example.com"
    assert _send("POST", "/v1/repos/git-identity", {**body, "name": "Other", "email": "other@example.com"})[0] == 200
    assert git.out(["config", "user.name"], world.repo_root) == "--Name"
    assert git.out(["config", "user.email"], world.repo_root) == "me@example.com"
    assert _send("POST", "/v1/sessions", body)[0] == 200 and started == [True]
    assert not global_config.exists()
    other = tmp_path / "other"
    other.mkdir()
    git.run(["init", "-q", "-b", "main"], other)
    git.run(["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--allow-empty", "-qm", "Initial commit"], other)
    reached = []

    def stop_before_launch(repo, *args, **kwargs):
        reached.append(repo)
        raise SystemExit(0)

    with monkeypatch.context() as cli_patch:
        cli_patch.setattr("brindle.cli._resolve_supervisor", stop_before_launch)
        for cwd, target, expected in [(world.repo_root, other, 1), (other, world.repo_root, 0)]:
            cli_patch.chdir(cwd)
            result = CliRunner().invoke(app, ["start", "--no-attach", "--yes", "--worktree", str(target)])
            assert result.exit_code == expected
        assert reached == [world.repo_root]
    git.run(["config", "--local", "--unset", "user.name"], world.repo_root)
    git.run(["config", "--global", "user.name", "Global Name"], world.repo_root)
    before = global_config.read_bytes()
    assert _send("POST", "/v1/sessions", body)[0] == 200
    assert _send("POST", "/v1/repos/git-identity", {**body, "name": "Other", "email": "other@example.com"})[0] == 200
    assert git.run(["config", "--local", "--get", "user.name"], world.repo_root, check=False).returncode == 1
    assert global_config.read_bytes() == before

    # Once identity is settled, the existing running-session choice still holds the launch.
    monkeypatch.setattr(svc, "start", real_start)
    spawned = []
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "sup")
    monkeypatch.setattr(agents, "spawn", lambda *a, **k: spawned.append(a))
    code, reply = _send("POST", "/v1/sessions", body)
    assert code == 409 and spawned == []
    conflict = reply["conflict"]
    assert [c["key"] for c in conflict["choices"]] == ["o", "a", "n", "p"]
    assert [s["id"] for s in conflict["sessions"]] == ["sup"]


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
    from brindle import provider_homes

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
    monkeypatch.setattr(provider_homes, "confirm_login", lambda provider: None)
    return root


def _flat(args):
    for x in args:
        yield from (_flat(x) if isinstance(x, (list, tuple)) else [getattr(x, "id", x)])


@pytest.mark.parametrize("method,path,body,fake,returns,status,target", [
    ("POST", "/v1/sessions/sup/pause", {}, (agents, "pause"), [], 200, "sup"),
    # The person decides a plan as the worker's supervisor would.
    ("POST", "/v1/inbox/wk/approve", {}, (agents, "approve_plan"), "approved", 200, "sup"),
    ("POST", "/v1/agents/wk/kill", {}, (agents, "kill"), None, 200, "wk"),
    # Real workspace ids contain a slash, which arrives in the path as %2F decoded.
    ("POST", "/v1/workspaces/proj/w-2/merge", {}, (pipeline, "merge"), "Merged feat2", 202, "proj/w-2"),
    ("POST", "/v1/workspaces/proj/w-2/remove", {}, (workspaces, "remove"), SimpleNamespace(branch_note=None, teardown=None), 200, "proj/w-2"),
    ("POST", "/v1/goal/check", {"root": "sup"}, (routes_actions, "_run_helper"), {"exit": 0}, 202,
     "_check-milestones"),
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


def _review_lock_returns_busy_without_starting_work(db, world, monkeypatch, checks):
    import fcntl
    import hashlib

    started = []
    monkeypatch.setattr(config, "load_repo_config", lambda *a, **k: config.RepoConfig(checks=checks))
    monkeypatch.setattr(agents, "_review_profile", lambda *a: "reviewer")
    monkeypatch.setattr(agents, "_request_review", lambda *a: started.append("reviewer"))
    monkeypatch.setattr(agents, "_detach", lambda *a: started.append("checks"))
    monkeypatch.setattr(routes_actions, "REVIEW_LOCK_WAIT", 0.05, raising=False)
    name = "review-dispatch-" + hashlib.sha1(b"w-1").hexdigest()[:16]
    directory = config.brindle_home() / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{name}.lock").open("w") as holder:
        flock = fcntl.flock
        flock(holder.fileno(), fcntl.LOCK_EX)

        def guarded_flock(fd, operation):
            if os.fstat(fd).st_ino == os.fstat(holder.fileno()).st_ino:
                assert operation != fcntl.LOCK_EX, "review route attempted an unbounded lock wait"
            return flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", guarded_flock)
        before = time.monotonic()
        code, reply = _send("POST", "/v1/workspaces/w-1/review")
        assert time.monotonic() - before < 1
        assert code == 409 and reply["error"]["code"] == "busy", reply
        assert started == [] and len(db.list_agents("w-1")) == 1
        assert not agents._review_request_file("w-1", "reviewer").exists()
        flock(holder.fileno(), fcntl.LOCK_UN)
        monkeypatch.setattr(fcntl, "flock", flock)
        code, reply = _send("POST", "/v1/workspaces/w-1/review")
        assert code == 200 and len(started) == 1, reply


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
    ("app.py", "read", None, 200),              # the base from GET .../file, the app's way
    ("app.py", "e69de29", None, 409),           # the empty blob: not what the file is now
    ("app.py", "shown", "busy", 409),
    ("app.py", "shown", "hook-refuses", 409),   # the file goes back; the worker's index survives
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
], ids=["edit-read", "edit-stale", "edit-busy", "edit-refused",
        "edit-new-file-refused", "edit-deleted-refused", "edit-to-head", "edit-agent",
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


@pytest.fixture
def demo_launches(db, monkeypatch):
    """The demo's supervisor launches recorded (repo, provider) where its window would open."""
    from brindle import doctor
    from brindle.service import sessions as svc

    launched = []

    def spawn(db, ws, profile, provider_name=None, **kw):
        launched.append((ws.repo_root, provider_name))
        a = Agent(f"demo{len(launched)}", ws.id, profile, provider_name, None, "interactive", "idle", "%9", None,
                  time.time())
        db.add_agent(a)
        return a

    monkeypatch.setattr(agents, "spawn", spawn)
    monkeypatch.setattr(svc, "preflight", lambda *a, **k: [])
    monkeypatch.setattr(svc, "cull_detached", lambda root=None: None)
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["codex"])
    return launched


@pytest.mark.parametrize("case,status", [("codex-only", 200), ("overlapping", 200), ("airgap", 403)])
def test_the_demo_runs_in_a_practice_repo_on_the_cli_signed_in(db, world, brindle_home, demo_launches, monkeypatch,
                                                               case, status):
    if case == "airgap":
        monkeypatch.setenv("BRINDLE_AIRGAP", "1")
    second = []
    if case == "overlapping":
        # A second press arrives while the first is launching its supervisor: refused, nothing more made.
        launch = agents.spawn

        def spawn_while_another_arrives(*a, **k):
            second.append(_send("POST", "/v1/demo"))
            return launch(*a, **k)

        monkeypatch.setattr(agents, "spawn", spawn_while_another_arrives)
    code, reply = _send("POST", "/v1/demo")
    assert [c for c, _ in second] == ([409] if case == "overlapping" else [])
    made = sorted((brindle_home / "demo").glob("textkit-*"))
    assert code == status, reply
    if case == "airgap":
        assert demo_launches == [] and made == []
        return
    # The CLI's own practice repo (brindle.demo), outside the person's repos; with only Codex signed in,
    # the supervisor, workers and reviewers all run on it.
    assert len(made) == 1 and reply["repo"] == db.workspace_by_path(str(made[0])).repo_root
    assert demo_launches == [(reply["repo"], "codex")]
    # The flag /repos sends (the app hides it from New session's projects); a lookalike elsewhere is no demo.
    look = str(brindle_home / "textkit-1")
    db.add_workspace(Workspace("look/root", look, "root", "main", "main", "main", look, None, "brindle-look", time.time()))
    flagged = {r["repo_root"]: r["demo"] for r in _send("GET", "/v1/repos")[1]}
    assert flagged[reply["repo"]] is True and flagged[look] is False
    cfg = config.load_repo_config(reply["repo"])
    assert (cfg.routing["light"], cfg.review_profile) == (["developer-codex"], "reviewer-codex")


@pytest.mark.parametrize("then", ["settled", "cancelled"])
def test_a_demo_held_by_its_preflight_keeps_its_repo_until_settled_or_cancelled(db, world, tmp_path, demo_launches,
                                                                               brindle_home, monkeypatch, then):
    from brindle import doctor, keystore

    _provider_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: ["claude"])
    monkeypatch.setattr(providers, "signed_out", lambda p, env=None: None)
    # Signed in by the key brindle stores, which Claude Code asks about for this launch's identity.
    keystore.set_key("ANTHROPIC_API_KEY", SECRET)
    code, held = _send("POST", "/v1/demo")
    assert code == 409 and demo_launches == [], held
    action = next(i["action"] for i in held["preflight"] if i["kind"] == "consent")
    assert action["body"]["repo"] == held["repo"] and action["body"]["profile"] and Path(held["repo"]).is_dir()
    if then == "cancelled":
        assert _send("DELETE", "/v1/demo")[0] == 200
        assert not Path(held["repo"]).exists() and db.find_workspaces(held["repo"]) == []
        return
    # Settled against the repo it was asked for (its identity), then the next start launches there.
    assert _send(action["method"], action["path"], {**action["body"], "accept": True})[0] == 200
    code, reply = _send("POST", "/v1/demo")
    assert code == 200, reply
    assert demo_launches == [(held["repo"], "claude")]
    assert len(list((brindle_home / "demo").glob("textkit-*"))) == 1


@pytest.mark.parametrize("scope,body,peer_pid,status,written", [
    # The picker's pick for a tier is dropped too, and so is an alias of a tier, or either would still win.
    ("user", {"tiers": {"light": ["developer-codex"], "advanced": ["developer-codex"]}}, None, 200,
     {"routing": {"heavy": ["developer"], "light": ["developer-codex"], "advanced": ["developer-codex"]}}),
    # A kind's fallbacks alone keep the person's own pick, not the one this repo's local file overrides it with.
    ("user", {"kinds": {"medium": {"fallbacks": ["developer-codex"]}}}, None, 200,
     {"routing": {"heavy": ["developer"], "advanced": ["developer-heavy"], "reasoning": ["developer-fable"],
                  "medium": ["developer-sonnet", "developer-codex"]},
      "routing_locked": {"medium": True}, "routing_light": "claude-haiku"}),
    ("local", {"reviewer": "reviewer"}, None, 200, {"reviewer": "reviewer"}),
    ("repo", {"tiers": {"light": ["developer"]}, "exclude_providers": ["claude"]}, None, 200,
     {"routing": {"light": ["developer"]}, "exclude_providers": ["claude"]}),
    ("repo", {"tiers": {"heavy": ["developer", "nope"]}}, None, 400, {}),
    pytest.param("repo", {"reviewer": "reviewer"}, 310, 403, {},
                 marks=pytest.mark.variant("an agent holding the token still can't change routing")),
], ids=["user", "user-kind", "local", "repo", "bad-profile", "agent"])
def test_routing_put_writes_its_scope(db, repo, world, monkeypatch, scope, body, peer_pid, status, written):
    monkeypatch.setattr(peer, "agent_caller", lambda pid: "brindle agent w1")
    config.set_user("routing", {"heavy": ["developer"], "advanced": ["developer-heavy"],
                                "reasoning": ["developer-fable"]})
    config.set_user("routing_light", "claude-haiku")
    config.set_local(str(repo), "routing", {"medium": ["developer-heavy"]})
    before = config._read_json(config.scope_paths(str(repo))[scope])
    code, reply = _send("PUT", "/v1/routing", {"repo": str(repo), "scope": scope, **body}, peer_pid)
    assert code == status, reply
    after = config._read_json(config.scope_paths(str(repo))[scope])
    assert after == ({k: v for k, v in before.items() if k != "routing_light"} | written if written else before)
    if "exclude_providers" in written:
        # The light candidate and the default agent both run on claude: routing refuses, not falls back.
        with pytest.raises(autopilot.AutopilotError, match="excluded from routing"):
            autopilot.choose_profile(db, "sup", str(repo), weight="light")


def test_a_locked_kind_is_never_overridden_by_learning_and_an_unlocked_one_reports_it(db, repo, world, monkeypatch):
    from brindle import learning, plugins, quota
    from brindle.daemon import routes_read
    from brindle.pro import learning as pro_learning

    class Prefers(learning.LearningPlugin):
        def record(self, task, outcome):
            pass

        def suggest(self, task, candidates, default=None):
            return "developer-codex"

    plugins.reset()
    monkeypatch.setattr(pro_learning, "CloudLearner", lambda repo_root: Prefers())
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 100.0)
    config.set_repo(str(repo), "learning", "cloud")
    code, reply = _send("PUT", "/v1/routing", {"repo": str(repo), "scope": "repo", "kinds": {
        "medium": {"model": "developer", "fallbacks": ["developer-codex"]},      # a choice locks it
        "heavy": {"model": "developer-heavy", "fallbacks": ["developer-codex"], "locked": False}}})
    assert code == 200, reply
    cfg = config.load_repo_config(str(repo))
    assert (cfg.routing["medium"], cfg.routing_locked) == (["developer", "developer-codex"],
                                                         {"medium": True, "heavy": False})
    # Editing one row in another scope leaves the lock a lower scope set on a different row alone.
    code, reply = _send("PUT", "/v1/routing", {"repo": str(repo), "scope": "local",
                                               "kinds": {"light": {"model": "developer-light"}}})
    assert code == 200, reply
    cfg = config.load_repo_config(str(repo))
    assert cfg.routing_locked == {"medium": True, "heavy": False, "light": True}

    assert autopilot._route_by_weight(db, cfg, str(repo), "medium", None, None)[:2] == ("developer", False)
    assert autopilot._route_by_weight(db, cfg, str(repo), "heavy", None, None)[:2] == ("developer-codex", True)
    kinds = routes_read._routing_kinds(db, cfg, str(repo))
    assert (kinds["medium"].locked, kinds["medium"].learned) == (True, None)
    assert (kinds["heavy"].model, kinds["heavy"].learned) == ("developer-heavy", "developer-codex")

    # A candidate routing excludes can't be what learning runs; an explicit review_profile isn't a learned one.
    config.set_repo(str(repo), "exclude_providers", ["codex"])
    config.set_repo(str(repo), "review_profile", "reviewer-codex")
    code, reply = _send("PUT", "/v1/routing", {"repo": str(repo), "scope": "repo",
                                               "kinds": {"reviewer": {"model": "reviewer"}}})
    assert code == 200, reply
    cfg = config.load_repo_config(str(repo))
    kinds = routes_read._routing_kinds(db, cfg, str(repo))
    assert kinds["heavy"].learned is None
    assert (cfg.review_profile, kinds["reviewer"].model, kinds["reviewer"].locked) == ("reviewer", "reviewer", True)


def test_creating_a_profile_writes_the_user_agent_file_and_refuses_what_is_wrong(world, repo, monkeypatch):
    from brindle import providers
    from brindle.profiles import load_profile

    monkeypatch.setattr(providers, "unusable", lambda p, env=None: "down" if p == "codex" else None)
    body = {"repo": str(repo), "name": "my-agent", "description": "Mine", "provider": "claude",
            "model": "sonnet", "effort": "high", "instructions": "Do it well.", "rules": ["style/minimal-diff"]}
    code, reply = _send("POST", "/v1/profiles", body)
    assert code == 200, reply
    path = config.user_profiles_dir() / "my-agent.md"
    assert reply == {"name": "my-agent", "path": str(path)}
    p = load_profile("my-agent", str(repo))
    assert (p.provider, p.model, p.effort, p.rules, p.prompt, p.description) == (
        "claude", "sonnet", "high", ["style/minimal-diff"], "Do it well.", "Mine")
    before = path.read_text()
    for bad in ({"name": "../x"}, {"model": "sonnet\nprovider: codex\npermission_mode: bypassPermissions"}, {"provider": "codex"}, {"provider": "nope"}, {"rules": ["no/such"]},
                {"effort": "turbo"}):
        code, reply = _send("POST", "/v1/profiles", {**body, "name": "other", **bad})
        assert code == 400, (bad, reply)
    assert [f.name for f in config.user_profiles_dir().iterdir()] == ["my-agent.md"]
    code, _ = _send("POST", "/v1/profiles", {**body, "instructions": "Overwritten?"})
    assert (code, path.read_text()) == (409, before)


def test_creating_a_profile_refuses_a_model_the_org_does_not_allow(world, repo, monkeypatch):
    from brindle.pro import team_policy

    monkeypatch.setattr(team_policy, "access", lambda repo: Access(org_id="acme", allowed_models=("opus",)))
    body = {"repo": str(repo), "name": "my-agent", "description": "", "provider": "claude",
            "effort": None, "instructions": "x", "rules": []}
    assert _send("POST", "/v1/profiles", {**body, "model": "haiku"})[0] == 403
    assert not (config.user_profiles_dir() / "my-agent.md").exists()
    assert _send("POST", "/v1/profiles", {**body, "model": "opus"})[0] == 200


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


@pytest.mark.parametrize("action,stored,env,provider_status,expect", [
    ("put", False, False, 200, ("verified", True, False)),
    ("test", True, False, 401, ("rejected", True, False)),
    ("test", True, False, 500, ("unchecked", True, False)),
    ("delete", True, False, 200, (None, False, False)),
    ("auth", True, False, 401, ("rejected", True, False)),
    ("import", False, True, 200, ("verified", True, False)),
], ids=["save-verify", "test-rejected", "test-inconclusive", "delete", "auth-failure", "import-env"])
def test_api_key_lifecycle(db, world, monkeypatch, provider_http, action, stored, env, provider_status, expect):
    from brindle import keys_cmds, keystore

    name = "ANTHROPIC_API_KEY"
    monkeypatch.delenv(name, raising=False)
    if env:
        monkeypatch.setenv(name, SECRET)
    if stored:
        keys_cmds.store(name, SECRET, None)
    provider_http["status"] = provider_status
    provider_http["calls"].clear()

    def entry():
        code, cfg = _send("GET", "/v1/config")
        assert code == 200
        return next(k for k in cfg["keys"] if k["name"] == name)

    assert entry()["importable"] is env
    if action == "put":
        assert _send("PUT", f"/v1/keys/{name}", {"value": SECRET})[0] == 200
    elif action == "test":
        code, reply = _send("POST", f"/v1/keys/{name}/test")
        assert code == 200 and reply["status"] == expect[0] and reply["checked_at"]
    elif action == "delete":
        assert _send("DELETE", f"/v1/keys/{name}")[0] == 200
    elif action == "auth":
        agents.handle_hook(db, "wk", "stop-failure", {"error": "invalid_api_key"})
    else:
        code, reply = _send("POST", f"/v1/keys/{name}/import")
        assert code == 200 and reply["last4"] == SECRET[-4:]
    got = entry()
    assert (got["status"], got["set"], got["importable"]) == expect
    assert got["key_page"] and got["provider"] == "claude" and got["personal_keys_denied"] is False
    assert (keystore.get_key(name) is not None) is expect[1]
    assert SECRET not in json.dumps(got)


@pytest.mark.parametrize("during", ["replaced", "deleted"])
def test_api_key_lifecycle_check_result_stays_with_the_key_it_checked(db, monkeypatch, provider_http, during):
    from brindle import key_check, keys_cmds, keystore

    name, newer = "ANTHROPIC_API_KEY", "sk-ant-replacement-0123456789"
    keys_cmds.store(name, SECRET, None)

    asked = []

    def rejects_while_the_key_changes(url, headers):
        if asked:                       # the replacement's own check
            return 200, {}
        asked.append(url)
        if during == "replaced":
            keys_cmds.store(name, newer, None)
        else:
            keys_cmds.remove(name)
        return 401, {}

    monkeypatch.setattr(key_check, "_get", rejects_while_the_key_changes)
    keys_cmds.retest(name)
    rec = keystore.get_record(name)
    if during == "replaced":
        assert rec["value"] == newer and rec["status"] == "verified"
    else:
        assert rec == {}


NOFLOCK = False  # True: children skip flock, which must make the lock test below fail


@pytest.mark.parametrize("fault", [
    "lock-unavailable",
    pytest.param("delete-during-update", marks=pytest.mark.slow(
        "a cross-process lock race: two spawned interpreters, and a 2s window the delete must stay blocked in")),
])
def test_api_key_lifecycle_key_updates_hold_the_store_lock(monkeypatch, provider_http, fault):
    import fcntl
    import multiprocessing

    import key_lock_procs
    from brindle import keys_cmds, keystore

    name = "ANTHROPIC_API_KEY"
    keys_cmds.store(name, SECRET, None)
    if fault == "lock-unavailable":
        def refuse(fd, op):
            raise OSError("no lock")

        with monkeypatch.context() as m:
            m.setattr(fcntl, "flock", refuse)
            with pytest.raises(keystore.CredentialError):
                keystore.set_check(name, "rejected", SECRET)
            with pytest.raises(keystore.CredentialError):
                keystore.unset_key(name)
        assert keystore.get_key(name) == SECRET
        return
    # Another process's delete, started while the update has the record loaded, waits for it.
    ctx = multiprocessing.get_context("spawn")
    loaded, go, updated, trying, deleted = (ctx.Event() for _ in range(5))
    updater = ctx.Process(target=key_lock_procs.update, args=(name, SECRET, NOFLOCK, loaded, go, updated))
    deleter = ctx.Process(target=key_lock_procs.delete, args=(name, NOFLOCK, trying, deleted))
    updater.start()
    try:
        assert loaded.wait(60)
        deleter.start()
        assert trying.wait(60)
        assert not deleted.wait(2), "the delete got through while the update held the record"
        go.set()
        assert updated.wait(60) and deleted.wait(60)
    finally:
        go.set()
        for p in (updater, deleter):
            if p.pid is not None:
                p.join(60)
    assert keystore.get_key(name) is None


@pytest.mark.parametrize("path,status,command", [
    ("/v1/providers/codex/login", 200, "login"),
    ("/v1/keys/OPENAI_API_KEY/set", 200, "OPENAI_API_KEY"),
    ("/v1/keys/BRINDLE_TOKEN/set", 400, None),
], ids=["login", "key", "bad-key"])
def test_login_and_secret_actions_open_one_terminal_while_it_runs(world, monkeypatch, path, status, command):
    from brindle import providers, tmux

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
    monkeypatch.setattr(tmux, "kill_window", lambda pane: [w.__setitem__(2, False) for w in windows.values()])
    monkeypatch.setattr(tmux, "tagged_panes", lambda session, key: [
        (f"%{i + 1}", name, alive, tid) for i, (tid, (name, _, alive)) in enumerate(windows.items())])
    code, reply = _send("POST", path)
    assert code == status, reply
    if command is None:
        assert windows == {} and second == []
        return
    thread = second.pop(0)
    thread.join(5)
    # Where the app's own tmux client attaches: the pane, on the daemon's server.
    attach = {"target": "%1", "server": tmux.current_server()}
    assert second == [(200, {"terminal": reply["terminal"], "started": False, **attach})]
    assert reply == {"terminal": reply["terminal"], "started": True, **attach}
    assert len(windows) == 1 and command in windows[reply["terminal"]][1]
    windows[reply["terminal"]][2] = False       # it exited: the next press starts afresh
    code, again = _send("POST", path)
    assert (code, again["started"], len(windows)) == (200, True, 2)
    if command == "login":      # "Try again": ends the running sign-in and starts afresh
        code, fresh = _send("POST", path, {"restart": True})
        assert (code, fresh["started"], len(windows)) == (200, True, 3)
        assert windows[again["terminal"]][2] is False




def test_terminal_input_takes_one_printable_line_into_an_action_terminal_only(world, repo, monkeypatch):
    from brindle import providers, tmux

    windows, typed = {}, []
    monkeypatch.setattr(providers, "claude_binary", lambda: "claude")
    monkeypatch.setattr(tmux, "ensure_session", lambda *a: None)
    monkeypatch.setattr(tmux, "new_window", lambda session, name, cwd, argv, env, tag:
                        windows.setdefault(tag[1], argv) and f"%{len(windows)}")
    monkeypatch.setattr(tmux, "tagged_panes", lambda session, key: [
        (f"%{i + 1}", "shell" if argv[-1] == "-l" else "login claude", True, tid)
        for i, (tid, argv) in enumerate(windows.items())])
    monkeypatch.setattr(tmux, "send_keys", lambda target, *keys, **kw: typed.append((target, *keys)))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    code, shell = _send("POST", "/v1/terminals/shell", {"cwd": str(repo)})
    assert code == 200 and shell["started"]
    # An ordinary shell is typed into in the app's own tmux client, never through the API.
    assert _send("POST", f"/v1/terminals/{shell['terminal']}/input", {"text": "ls"})[0] == 404
    assert typed == []
    assert _send("POST", "/v1/providers/claude/login", {"method": "device"})[0] == 400
    code, reply = _send("POST", "/v1/providers/claude/login", {"method": "console"})
    assert code == 200 and windows[reply["terminal"]] == ["claude", "auth", "login", "--console"]
    code, sent = _send("POST", f"/v1/terminals/{reply['terminal']}/input", {"text": "ab-12"})
    assert code == 200 and typed == [("%2", "-l", "--", "ab-12"), ("%2", "Enter")]
    # One printable line only, and only into a running action terminal (never an agent's pane).
    assert _send("POST", f"/v1/terminals/{reply['terminal']}/input", {"text": "a\nb"})[0] == 400
    assert _send("POST", "/v1/terminals/wk/input", {"text": "ab-12"})[0] == 404
    assert len(typed) == 2


class _FakeCLI:
    """subprocess.Popen for a cloud CLI's sign-in: ``on_run(argv, env)`` gives its output lines."""
    on_run = staticmethod(lambda argv, env: [])

    def __init__(self, argv, env=None, **kwargs):
        self.stdout, self.returncode = iter(self.on_run(argv, env)), 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("case", ["bedrock", "foundry", "org_refuses", "org_route_wins", "opencode_azure",
                                  "concurrent"])
def test_provider_connection_method_is_saved_reaches_the_pane_and_respects_the_org(
        db, world, tmp_path, monkeypatch, case):
    import subprocess

    from brindle import cloud_login, connections, profiles, tmux
    from brindle.pro import managed_models
    from brindle.pro.team_policy import ProviderConfig

    path = "/v1/providers/claude/connection"
    aws = tmp_path / "no-aws" / "config"     # AWS_CONFIG_FILE (conftest)
    aws.parent.mkdir()
    ran = []
    # The sign-in job's cloud CLI (started through brindle.spawn): a fake process.
    monkeypatch.setattr(routes_actions.spawn, "start", _FakeCLI)
    if case == "bedrock":
        aws.write_text("[profile work-sso]\nsso_session = corp\n")
        settings = {"AWS_PROFILE": "work-sso", "AWS_REGION": "us-east-1"}
        code, reply = _send("PUT", path, {"method": "cloud", "cloud": "bedrock", "settings": settings})
        assert code == 200 and (reply["cloud"], reply["settings"]) == ("bedrock", settings)
        assert reply["login"] is not None and reply["clouds"] == ["bedrock", "vertex", "foundry"]
        # Another route inherited from the shell, and a personal key: the choice wins, the key goes.
        monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE_PERSONAL_KEY")
        monkeypatch.setattr(tmux, "inherited_env", lambda *a: dict(os.environ))
        env = agents._effective_window_env(db.get_agent("wk"), db.get_workspace("w-1"))
        assert {k: env.get(k) for k in settings} == settings and cloud_login.provider_from_env(env) == "bedrock"
        assert not env.get("ANTHROPIC_API_KEY") and not env.get("CLAUDE_CODE_USE_VERTEX")
        # A first sign-in (no SSO token yet) runs the route's login; a second press while it runs waits.
        def first(argv, env):
            ran.append((argv, env.get("CLAUDE_CODE_USE_VERTEX")))
            assert _send("POST", "/v1/providers/claude/cloud-sign-in")[0] == 202
            return ["Open https://device.sso.example/ABCD\n"]
        monkeypatch.setattr(_FakeCLI, "on_run", staticmethod(first))
        code, job = _send("POST", "/v1/providers/claude/cloud-sign-in")
        assert code == 202 and ran == [(["aws", "sso", "login", "--profile", "work-sso"], "")]
        done = [e["payload"] for e in bus.tail(db, kinds=("job.done",))]
        assert [d["result"]["result"] for d in done] == ["waiting", "signed in"] and done[-1]["job"] == job["job"]
        assert any(e["payload"]["text"].startswith("Open https://device") for e in bus.tail(db, kinds=("job.progress",)))
    elif case == "foundry":
        # An endpoint inherited for an older resource: the chosen resource is the only one the pane sees.
        settings = {"ANTHROPIC_FOUNDRY_RESOURCE": "new-resource", "AZURE_TENANT_ID": "tenant-1"}
        real_status = cloud_login.status     # not signed in to the Azure CLI yet
        monkeypatch.setattr(cloud_login, "status", lambda *a, run=None, **kw: real_status(
            *a, run=lambda argv, **_: subprocess.CompletedProcess(argv, 1, "", "Please run 'az login'"), **kw))
        code, reply = _send("PUT", path, {"method": "cloud", "cloud": "foundry", "settings": settings})
        assert code == 200 and reply["login"]["valid"] is False
        monkeypatch.setenv("ANTHROPIC_FOUNDRY_BASE_URL", "https://old-resource.services.ai.azure.com/anthropic")
        monkeypatch.setattr(tmux, "inherited_env", lambda *a: dict(os.environ))
        env = agents._effective_window_env(db.get_agent("wk"), db.get_workspace("w-1"))
        assert env["ANTHROPIC_FOUNDRY_RESOURCE"] == "new-resource" and not env.get("ANTHROPIC_FOUNDRY_BASE_URL")
        assert cloud_login.provider_from_env(env) == "foundry"
        monkeypatch.setattr(_FakeCLI, "on_run", staticmethod(lambda argv, env: ran.append(argv) or []))
        assert _send("POST", "/v1/providers/claude/cloud-sign-in")[0] == 202
        assert ran == [["az", "login", "--tenant", "tenant-1"]]
        assert bus.tail(db, kinds=("job.done",))[-1]["payload"]["result"]["result"] == "signed in"
    elif case == "org_refuses":
        monkeypatch.setattr(managed_models, "current", lambda repo: managed_models.Managed("acme", None, True))
        code, reply = _send("PUT", path, {"method": "api_key"})
        assert code == 403 and "acme" in reply["error"]["message"]
        assert _send("GET", path)[1]["choice"] is None
    elif case == "org_route_wins":
        # An older provider_config: the org's Bedrock route, over a choice saved before it.
        assert _send("PUT", path, {"method": "api_key"})[0] == 200
        org = managed_models.Managed("acme", ProviderConfig("bedrock", region="us-west-2"), False)
        monkeypatch.setattr(managed_models, "current", lambda repo: org)
        code, reply = _send("PUT", path, {"method": "sign_in"})
        assert code == 403 and "acme" in reply["error"]["message"]
        code, reply = _send("GET", path)
        assert (reply["method"], reply["cloud"], reply["settings"], reply["choice"]["method"]) == (
            "cloud", "bedrock", {"AWS_REGION": "us-west-2"}, "api_key") and reply["managed"]
        assert profiles.load_profile("developer", world.repo_root).auth == "auto"
    elif case == "opencode_azure":
        (Path(world.repo_root) / ".brindle" / "agents").mkdir(parents=True, exist_ok=True)
        (Path(world.repo_root) / ".brindle" / "agents" / "oc.md").write_text(
            "---\nname: oc\nprovider: opencode\nmodel: azure/gpt-5\n---\nWork.\n")
        # The Azure CLI is signed in; OpenCode's own store isn't connected yet.
        token = json.dumps({"accessToken": "FAKE", "expires_on": time.time() + 3600})
        real_status = cloud_login.status
        monkeypatch.setattr(cloud_login, "status", lambda *a, run=None, **kw: real_status(
            *a, run=lambda argv, **_: subprocess.CompletedProcess(argv, 0, token, ""), **kw))
        code, _ = _send("PUT", "/v1/providers/opencode/connection",
                        {"method": "cloud", "cloud": "azure", "settings": {"AZURE_RESOURCE_NAME": "res"}})
        assert code == 200

        def connect(argv, env):     # OpenCode's Entra connection, written to the store it was given
            store = Path(env["XDG_DATA_HOME"]) / "opencode"
            store.mkdir(parents=True)
            (store / "auth.json").write_text(json.dumps({"azure": {"type": "oauth", "accountId": "res"}}))
            return []
        monkeypatch.setattr(_FakeCLI, "on_run", staticmethod(connect))
        assert _send("POST", "/v1/providers/opencode/cloud-sign-in")[0] == 202
        assert bus.tail(db, kinds=("job.done",))[-1]["payload"]["result"]["result"] == "signed in"
        oc = profiles.load_profile("oc", world.repo_root)
        assert cloud_login.azure_connected({**oc.env, **cloud_login.profile_env(oc, oc.env)})
    elif case == "concurrent":
        # Codex's choice is saved while Claude's is being written: both are kept.
        real_read, real_refusal, others, codex = config._read_json, connections.refusal, [], []
        state = {"armed": False, "fired": False}
        # Codex's PUT got as far as it can while Claude's is between reading and writing:
        # waiting on the config writer's lock, or (unserialized) all the way through.
        progressed = threading.Event()

        class Writer:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                if not self.lock.acquire(blocking=False):
                    progressed.set()
                    self.lock.acquire()

            def __exit__(self, *exc):
                self.lock.release()
        monkeypatch.setattr(config, "_USER_WRITE", Writer())
        monkeypatch.setattr(connections, "refusal", lambda *a, **kw: state.update(armed=True) or real_refusal(*a, **kw))

        def read(p, *rest):
            # Claude's first read of the config after its policy check: Codex's PUT starts there.
            data = real_read(p, *rest)
            if state["armed"] and not state["fired"] and Path(p) == config.user_config_path():
                state["fired"] = True
                t = threading.Thread(target=lambda: codex.append(_send(
                    "PUT", "/v1/providers/codex/connection", {"method": "sign_in"})) or progressed.set())
                t.start()
                progressed.wait()
                others.append(t)
            return data
        monkeypatch.setattr(config, "_read_json", read)
        assert _send("PUT", path, {"method": "api_key"})[0] == 200
        others[0].join()
        assert codex[0][0] == 200 and set(config.user_settings()[connections.KEY]) == {"claude", "codex"}


@pytest.mark.parametrize("case", ["agy_gemini_key", "codex_login", "rejected_key", "sign_in_exported_key"])
def test_connection_readiness_says_what_an_agent_would_launch_with(db, world, monkeypatch, case):
    from brindle import company_login, keystore, pane_auth, providers_install
    from brindle.profiles import Profile

    monkeypatch.setattr(providers_install, "found", lambda p: f"/bin/{p}" if p in providers_install.PROVIDERS else None)
    for name in (*pane_auth.API_KEYS["claude"], *pane_auth.API_KEYS["codex"], "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # Claude's own login, as `claude auth status` answers it: signed in except where the case says not.
    probed = []
    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: probed.append(env) or (
        0, json.dumps({"loggedIn": case != "sign_in_exported_key"})))
    if case == "agy_gemini_key":
        settings = Path(providers.agy_settings_path())
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"modelProvider": "vertex", "theme": "dark"}))
        keystore.set_key("GEMINI_API_KEY", SECRET)
        agy = Profile(name="g", description="", provider="antigravity", prompt="", auth="api_key")
        assert pane_auth.key_problem(agy, "antigravity", (), False)      # agy would ignore the key
        code, reply = _send("PUT", "/v1/providers/antigravity/connection", {"method": "api_key"})
        assert code == 200 and reply["ready"]["ok"] and reply["ready"]["reason"]     # the person's agy changed too
        assert pane_auth.key_problem(agy, "antigravity", (), False) is None
        # Leaving the key gives the person's agy its own setting back, by choice or by removing the key.
        assert _send("PUT", "/v1/providers/antigravity/connection", {"method": "sign_in"})[0] == 200
        assert json.loads(settings.read_text()) == {"modelProvider": "vertex", "theme": "dark"}
        assert _send("PUT", "/v1/providers/antigravity/connection", {"method": "api_key"})[0] == 200
        assert _send("DELETE", "/v1/keys/GEMINI_API_KEY")[0] == 200
        assert providers.agy_model_provider() == "vertex"
        assert _send("GET", "/v1/providers/antigravity/connection")[1]["ready"]["action"] == "set_key"
    elif case == "codex_login":
        auth = Path(providers.codex_auth_path())
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text(json.dumps({"auth_mode": "chatgpt"}))
        keystore.set_key("OPENAI_API_KEY", SECRET)
        code, reply = _send("PUT", "/v1/providers/codex/connection", {"method": "api_key"})
        assert (reply["blocked_by"], reply["ready"]["action"]) == ("codex_login", "sign_out")
        ran = []
        monkeypatch.setattr(company_login, "run_cmd", lambda argv, env=None, **kw: (
            ran.append((argv, env["CODEX_HOME"])), auth.unlink(), (0, ""))[-1])
        code, job = _send("POST", "/v1/providers/codex/sign-out")
        assert code == 202 and ran == [(["/bin/codex", "logout"], str(auth.parent))]
        result = bus.tail(db, kinds=("job.done",))[-1]["payload"]["result"]
        assert result["blocked_by"] is None and result["ready"] == {"ok": True, "reason": None, "action": None}
    elif case == "rejected_key":
        # auto puts the stored key in the pane ahead of the (valid) login; api_key needs it.
        keystore.set_key("ANTHROPIC_API_KEY", SECRET, status="rejected")
        for method in ("auto", "api_key"):
            reply = _send("PUT", "/v1/providers/claude/connection", {"method": method})[1]
            assert (reply["method"], reply["ready"]["ok"], reply["ready"]["action"]) == (method, False, "set_key")
    else:
        # The exported key is taken out of a sign_in pane: only the login counts.
        monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
        reply = _send("PUT", "/v1/providers/claude/connection", {"method": "sign_in"})[1]
        assert (reply["ready"]["ok"], reply["ready"]["action"]) == (False, "sign_in")
        assert probed and not probed[-1]["ANTHROPIC_API_KEY"]


@pytest.mark.parametrize("route", ["bedrock"])
def test_cloud_profile_login_uses_launch_identity(db, world, tmp_path, monkeypatch, route):
    import hashlib

    from brindle import cloud_login, company_login, doctor, tmux

    _provider_setup(tmp_path, monkeypatch)
    cloud = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "company dev", "AWS_REGION": "us-east-1"}
    aws = tmp_path / "aws-config"
    aws.write_text("[profile company dev]\nsso_session = company\n")
    cloud["AWS_CONFIG_FILE"] = str(aws)
    if route == "vertex":
        cloud = {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "company-project",
                 "CLOUD_ML_REGION": "us-east5", "CLOUDSDK_CONFIG": str(tmp_path / "gcloud")}
    directory = Path(world.repo_root) / ".brindle" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    # Stored company env must resolve just as profile-local env does.
    local = cloud if route == "vertex" else {}
    monkeypatch.setattr(company_login, "stored_env", lambda: {} if local else cloud)
    (directory / "developer.md").write_text("---\nname: developer\nprovider: claude\nauth: cloud\n" +
        "".join(f"env.{k}: {v}\n" for k, v in local.items()) + "---\nWork.\n")
    real_status = cloud_login.status
    answer = SimpleNamespace(returncode=1, stdout="", stderr="invalid_grant: invalid_rapt")
    monkeypatch.setattr(cloud_login, "status", lambda *a, **kw: real_status(
        *a, **kw, home=tmp_path, run=lambda *a, **kw: answer, expiry=lambda _: 3600.0))
    worker, ws = db.get_agent("wk"), db.get_workspace("w-1")
    # A Claude worker can be dispatched by an OpenCode cloud supervisor.
    monkeypatch.setenv("BRINDLE_CLOUD_ROUTE", "azure")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "other-supervisor-project")
    if route == "bedrock":
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "FAKE_PERSONAL_KEY")
        monkeypatch.setattr(tmux, "inherited_env", lambda *a: dict(os.environ))
        assert not agents._effective_window_env(worker, ws).get("AWS_BEARER_TOKEN_BEDROCK")
    identity = agents.launch_identity(worker, ws)[0]
    assert cloud_login.provider_from_env(identity) == route
    assert all(identity.get(k) == v for k, v in cloud.items())
    items = agents.launch_blockers(worker, ws)
    item = next(i for i in items if i.kind == "signin")
    assert item.action.body["profile"] == "developer"
    code, pending = _send("POST", "/v1/providers/claude/preflight", item.action.body)
    assert code == 200 and any(i["id"] == "claude.cloud" for i in pending)
    windows = {}
    monkeypatch.setattr(tmux, "ensure_session", lambda *a: None)
    monkeypatch.setattr(tmux, "new_window", lambda session, name, cwd, argv, env, tag:
                        windows.setdefault(tag[1], (name, argv, env)) and f"%{len(windows)}")
    monkeypatch.setattr(tmux, "tagged_panes", lambda *a: [
        (f"%{i + 1}", name, True, tid) for i, (tid, (name, _, _)) in enumerate(windows.items())])
    code, opened = _send("POST", item.action.path, item.action.body)
    assert code == 200, opened
    _, argv, env = windows[opened["terminal"]]
    assert all(env.get(k) == v for k, v in cloud.items())
    assert cloud_login.provider_from_env(env) == route
    commands = []
    login_key = cloud_login.login_key(identity)

    def cloud_command(command, **kwargs):
        assert cloud_login.login_key(env) == login_key
        assert cloud_login.signing_in(env=env)
        assert cloud_login.sign_in(env=env, now=1e12, last_input=1e12) == "waiting"
        assert cloud_login.interactive_sign_in(env=env, run=cloud_command) == 1
        commands.append(command)
        return SimpleNamespace(returncode=0)

    # Execute the dispatched module with a fake cloud CLI, without a subprocess.
    import importlib.util
    import runpy
    import subprocess

    with monkeypatch.context() as launched:
        launched.setenv("BRINDLE_HOME", str(tmp_path / "old-tmux-home"))
        for name, value in env.items():
            launched.setenv(name, value)
        launched.setattr(subprocess, "run", cloud_command)
        with pytest.raises(SystemExit) as exited:
            runpy.run_path(importlib.util.find_spec(argv[2]).origin, run_name="__main__")
        assert exited.value.code == 0
    assert commands == ([["aws", "sso", "login", "--profile", "company dev"]] if route == "bedrock"
                        else [["gcloud", "auth", "application-default", "login", "--project", "company-project"]])
    assert not cloud_login.signing_in(env=env)
    code, again = _send("POST", item.action.path, item.action.body)
    assert code == 200 and not again["started"] and again["terminal"] == opened["terminal"]
    assert len(windows) == 1
    # "Try again" after a cloud sign-in failed and stuck: that terminal ends and a fresh one starts.
    monkeypatch.setattr(tmux, "kill_window", lambda pane: windows.pop(next(iter(windows))))
    code, opened = _send("POST", item.action.path, {**item.action.body, "restart": True})
    assert code == 200 and opened["started"] and opened["terminal"] != again["terminal"] and len(windows) == 1
    shared = {**cloud, ("AWS_REGION" if route == "bedrock" else "CLOUD_ML_REGION"): "other-region"}
    (directory / "shared.md").write_text("---\nname: shared\nprovider: claude\nauth: cloud\n" +
        "".join(f"env.{k}: {v}\n" for k, v in shared.items()) + "---\nWork.\n")
    code, same_login = _send("POST", item.action.path, {**item.action.body, "profile": "shared"})
    assert code == 200 and same_login["terminal"] == opened["terminal"] and len(windows) == 1
    checks = doctor.cloud_checks(world.repo_root)
    assert checks and all(c.level != doctor.OK for c in checks)
    assert _send("POST", item.action.path, {"repo": world.repo_root})[0] == 400
    assert _send("POST", "/v1/providers/unknown/preflight", item.action.body)[0] == 400
    assert _send("POST", item.action.path, {**item.action.body, "method": "console"})[0] == 400
    # A second profile must not inherit the in-flight login's selected identity.
    other = {**cloud, ("AWS_PROFILE" if route == "bedrock" else "CLOUDSDK_CONFIG"): "other"}
    aws.write_text(aws.read_text() + "[profile other]\nsso_session = other\n")
    (directory / "other.md").write_text("---\nname: other\nprovider: claude\nauth: cloud\n" +
        "".join(f"env.{k}: {v}\n" for k, v in other.items()) + "---\nWork.\n")
    code, separate = _send("POST", item.action.path, {**item.action.body, "profile": "other"})
    assert code == 200 and separate["terminal"] != opened["terminal"] and len(windows) == 2
    # The original identity becomes usable without changing profile or worker config.
    answer.returncode, answer.stdout = 0, "FAKE_TOKEN"
    cache = tmp_path / ".aws" / "sso" / "cache"
    cache.mkdir(parents=True)
    (cache / (hashlib.sha1(b"company").hexdigest() + ".json")).write_text(
        json.dumps({"accessToken": "FAKE_TOKEN", "expiresAt": "2099-01-01T00:00:00Z"}))
    assert not agents.launch_blockers(worker, ws)
    code, ready = _send("POST", "/v1/providers/claude/preflight", item.action.body)
    assert code == 200 and not ready
    assert any(c.level == doctor.OK for c in doctor.cloud_checks(world.repo_root))
    assert "FAKE_TOKEN" not in doctor.render(doctor.cloud_checks(world.repo_root))
    if route == "vertex":
        selected = {**cloud, "GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "selected-adc.json")}
        (directory / "developer.md").write_text("---\nname: developer\nprovider: claude\n" +
            "".join(f"env.{k}: {v}\n" for k, v in selected.items()) + "---\nWork.\n")
        answer.returncode = 1
        manual = next(i for i in agents.launch_blockers(worker, ws) if i.kind == "signin")
        code, pending = _send(manual.action.method, manual.action.path, manual.action.body)
        assert code == 200 and pending and len(windows) == 2
        assert _send("POST", item.action.path, manual.action.body)[0] == 400
        answer.returncode = 0
        assert _send(manual.action.method, manual.action.path, manual.action.body) == (200, [])


@pytest.mark.parametrize("route", ["vertex"])
def test_opencode_cloud_identity_dispatch(db, world, tmp_path, monkeypatch, route):
    from brindle import cloud_login, doctor, procs, tmux
    from brindle.profiles import ProfileError

    monkeypatch.setattr(providers, "which_cli", lambda _: None)
    _provider_setup(tmp_path, monkeypatch)
    cloud = ({"GOOGLE_CLOUD_PROJECT": "company", "VERTEX_LOCATION": "us-central1",
              "CLOUDSDK_CONFIG": str(tmp_path / "gcloud")} if route == "vertex" else
             {"AZURE_CONFIG_DIR": str(tmp_path / "azure"), "AZURE_RESOURCE_NAME": "company-models",
              "AZURE_TENANT_ID": "company-tenant", "AZURE_SUBSCRIPTION_ID": "company-subscription"})
    model = "google-vertex/gemini-2.5-pro" if route == "vertex" else "azure/gpt-4.1"
    directory = Path(world.repo_root) / ".brindle" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    profile = directory / "developer.md"
    content = (f"---\nname: developer\nprovider: opencode\nmodel: {model}\nauth: cloud\n" +
               "".join(f"env.{k}: {v}\n" for k, v in cloud.items()) + "---\nWork.\n")
    profile.write_text(content)
    db.update_agent("wk", provider="opencode")
    worker, ws = db.get_agent("wk"), db.get_workspace("w-1")
    required = "GOOGLE_CLOUD_PROJECT" if route == "vertex" else "AZURE_CONFIG_DIR"
    monkeypatch.delenv(required, raising=False)
    profile.write_text(content.replace(f"env.{required}: {cloud[required]}\n", ""))
    with pytest.raises(ProfileError):
        agents.launch_identity(worker, ws)
    profile.write_text(content)
    monkeypatch.setenv("AZURE_API_KEY", "FAKE_PERSONAL_KEY")
    monkeypatch.setenv("GOOGLE_API_KEY", "FAKE_PERSONAL_KEY")
    if route == "vertex":
        monkeypatch.setenv("HOME", str(tmp_path / "personal-home"))
        default_adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        selected_adc = Path(cloud["CLOUDSDK_CONFIG"]) / "application_default_credentials.json"
        for path, account in ((default_adc, "personal"), (selected_adc, "company")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"account": account}))
        for name in ("GOOGLE_VERTEX_PROJECT", "GOOGLE_VERTEX_LOCATION", "GOOGLE_CLOUD_LOCATION"):
            monkeypatch.setenv(name, "personal")
    monkeypatch.setattr(tmux, "inherited_env", lambda *a: dict(os.environ))
    identity = agents.launch_identity(worker, ws)[0]
    actual = agents._effective_window_env(worker, ws)
    assert all(actual[k] == v for k, v in cloud.items())
    assert not actual.get("AZURE_API_KEY") and not actual.get("GOOGLE_API_KEY")
    assert identity["XDG_DATA_HOME"] == actual["XDG_DATA_HOME"]
    if route == "vertex":
        # OpenCode's SDK uses this ADC lookup and these project/location precedences.
        account = json.loads(Path(actual.get("GOOGLE_APPLICATION_CREDENTIALS") or default_adc).read_text())["account"]
        project = actual.get("GOOGLE_VERTEX_PROJECT") or actual.get("GOOGLE_CLOUD_PROJECT")
        location = actual.get("GOOGLE_VERTEX_LOCATION") or actual.get("GOOGLE_CLOUD_LOCATION") or actual.get("VERTEX_LOCATION")
        assert (account, project, location) == ("company", "company", "us-central1")
        selected_names = ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_VERTEX_PROJECT", "GOOGLE_VERTEX_LOCATION", "GOOGLE_CLOUD_LOCATION")
        assert all(identity[k] == actual[k] for k in selected_names)
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(default_adc))
    healthy = False
    tenant = "company-tenant"

    def probe(command, **kw):
        if not healthy:
            return SimpleNamespace(returncode=1, stdout="", stderr="invalid_grant AADSTS700082")
        if route == "vertex":
            adc = Path(kw["env"].get("GOOGLE_APPLICATION_CREDENTIALS") or selected_adc)
            assert json.loads(adc.read_text())["account"] == "company"
        token = {"accessToken": "FAKE_TOKEN", "expires_on": 4070908800,
                 "tenant": tenant, "subscription": "company-subscription"}
        if route == "azure" and "https://cognitiveservices.azure.com/.default" not in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="wrong audience")
        return SimpleNamespace(returncode=0, stdout=json.dumps(token) if route == "azure" else "FAKE_TOKEN", stderr="")

    real_status = cloud_login.status
    monkeypatch.setattr(cloud_login, "status", lambda *a, **kw: real_status(
        *a, **{**kw, "run": probe, "expiry": lambda _: 3600.0}))
    if route == "azure":
        healthy = True
        assert agents.launch_blockers(worker, ws)  # A token alone is not an OpenCode Entra connection.
        healthy = False
    pending = agents.launch_blockers(worker, ws)
    item = next(i for i in pending if i.kind == "signin")
    assert any(c.level != doctor.OK for c in doctor.cloud_checks(world.repo_root))
    windows = {}
    monkeypatch.setattr(tmux, "ensure_session", lambda *a: None)
    monkeypatch.setattr(tmux, "new_window", lambda session, name, cwd, argv, env, tag:
                        windows.setdefault(tag[1], (name, argv, env)) and f"%{len(windows)}")
    monkeypatch.setattr(tmux, "tagged_panes", lambda *a: [
        (f"%{i + 1}", name, True, tid) for i, (tid, (name, _, _)) in enumerate(windows.items())])
    code, opened = _send("POST", item.action.path, item.action.body)
    assert code == 200, opened
    assert _send("POST", item.action.path, item.action.body)[1]["terminal"] == opened["terminal"]
    # Try again: the cloud sign-in still running goes, and a fresh one carries the same identity.
    monkeypatch.setattr(tmux, "kill_window", lambda pane: windows.pop(next(iter(windows))))
    code, fresh = _send("POST", item.action.path, {**item.action.body, "restart": True})
    assert code == 200 and fresh["started"] and fresh["terminal"] != opened["terminal"]
    assert list(windows) == [fresh["terminal"]]
    opened = fresh
    _, argv, env = windows[opened["terminal"]]
    assert all(env[k] == v for k, v in cloud.items()) and env["XDG_DATA_HOME"] == identity["XDG_DATA_HOME"]
    if route == "vertex":
        assert all(env[k] == identity[k] for k in selected_names)
    def login(command, **kw):
        nonlocal healthy
        if command == ["gcloud", "auth", "application-default", "login", "--project", "company"]:
            refreshed_adc = Path(kw["env"]["CLOUDSDK_CONFIG"]) / "application_default_credentials.json"
            refreshed_adc.write_text(json.dumps({"account": "company"}))
            healthy = True
        elif command == ["az", "login", "--tenant", "company-tenant", "--subscription", "company-subscription"]:
            healthy = True
        elif healthy and command[-4:] == ["--provider", "azure", "--method", "Microsoft Entra ID (Azure CLI)"]:
            auth = Path(kw["env"]["XDG_DATA_HOME"]) / "opencode" / "auth.json"
            auth.parent.mkdir(parents=True, exist_ok=True)
            auth.write_text(json.dumps({"azure": {"type": "oauth", "accountId": "company-models"}}))
        else:
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    assert cloud_login.interactive_sign_in(env=env, run=login, probe=probe) == 0
    assert _send("POST", "/v1/providers/opencode/preflight", item.action.body) == (200, [])
    assert not agents.launch_blockers(worker, ws)
    assert any(c.level == doctor.OK for c in doctor.cloud_checks(world.repo_root))
    if route == "azure":
        tenant = "another-company"
        assert agents.launch_blockers(worker, ws)
        tenant = "company-tenant"
        profile.write_text(content.replace("company-models", "another-resource"))
        other = db.get_agent("wk")
        assert agents.launch_identity(other, ws)[0]["XDG_DATA_HOME"] != identity["XDG_DATA_HOME"]
        action = next(i.action for i in agents.launch_blockers(other, ws) if i.kind == "signin")
        code, another = _send("POST", action.path, action.body)
        assert code == 200 and another["terminal"] != opened["terminal"]
        assert windows[another["terminal"]][2]["AZURE_RESOURCE_NAME"] == "another-resource"
        profile.write_text(content)
    # The real continuation dispatch checks credentials before either live
    # recovery or resuming a paused session, and clears the same block after.
    monkeypatch.setattr(agents, "is_alive", lambda a, *args: a.status not in ("paused", "failed"))
    monkeypatch.setattr(agents, "owns_pane", lambda *a: False)
    monkeypatch.setattr(procs, "stop", lambda *a, **kw: 0)
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: db.set_status(a.id, "idle"))
    for state in ("failed", "paused"):
        db.set_status("sup", "idle" if state == "failed" else "paused")
        db.set_status("wk", state)
        db.update_autopilot("sup", state="blocked", note="Credentials need attention")
        healthy = False
        cloud_login._google_expiry_cache.clear()   # the sign-in lapsed: no token left to reuse
        assert _send("POST", "/v1/sessions/sup/continue")[0] == 409
        assert db.get_autopilot("sup").state == "blocked"
        healthy = True
        code, reply = _send("POST", "/v1/sessions/sup/continue")
        assert code == 200, reply
        assert db.get_agent("wk").status == "idle" and db.get_autopilot("sup").state == "running"
    if route == "vertex":
        custom = tmp_path / "custom-adc.json"
        custom.write_text(json.dumps({"account": "company"}))
        profile.write_text(content.replace("auth: cloud\n", f"auth: cloud\nenv.GOOGLE_APPLICATION_CREDENTIALS: {custom}\n"))
        custom_identity = agents.launch_identity(db.get_agent("wk"), ws)[0]
        assert custom_identity["GOOGLE_APPLICATION_CREDENTIALS"] == str(custom)
        assert cloud_login.login_command(custom_identity) is None
    profile.write_text(content.replace("auth: cloud", "auth: auto"))
    worker = db.get_agent("wk")
    assert "XDG_DATA_HOME" not in agents.launch_identity(worker, ws)[0]
    assert agents._effective_window_env(worker, ws)["GOOGLE_API_KEY"] == "FAKE_PERSONAL_KEY"
    assert not agents.launch_blockers(worker, ws)


OPTIONS = ["Yes", "Yes, and don't ask again for npm test commands in /repo",
           "No, and tell Claude what to do differently (esc)"]
SUGGESTED = [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "npm test:*"}],
              "behavior": "allow", "destination": "localSettings"}]
# Claude Code's PermissionRequest input, as documented: no tool_use_id.
ASK = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "npm test"}}
CLAUDE_PROMPT = "Bash command\n  npm test\nDo you want to proceed?\n" + "\n".join(
    f"{'❯' if i == 0 else ' '} {i + 1}. {o}" for i, o in enumerate(OPTIONS))
HOLD = 2    # the repo's permission_hold_seconds in these tests
# A hold running out is the behaviour: the hook's real wait can't be faked away.
WAITS_OUT_THE_HOLD = pytest.mark.slow("waits out the hook's real permission hold (HOLD seconds)")


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
    from brindle.daemon import events, holds
    from brindle.service import inbox

    config.set_local(world.repo_root, "permission_hold_seconds", HOLD)
    _HookConnection.paths = []
    monkeypatch.setattr(events, "connected", 1)      # the app is open
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
    pytest.param("allow_always", False, "in-flight", 409, None, None, marks=WAITS_OUT_THE_HOLD),
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
    pytest.param("allow_once", True, "mutated", 409, None, None, marks=WAITS_OUT_THE_HOLD),
    # An answer written into the database is no answer: the hold runs out.
    pytest.param("allow_once", True, "db-answer", None, None, None, marks=WAITS_OUT_THE_HOLD),
    # A daemon.json rewritten with another token and socket: the hook never reads it.
    ("allow_once", True, "forged-json", 200, "allow", None),
    # Another identity's session: nothing is answered or written.
    pytest.param("allow_always", False, "locked", 409, None, None, marks=WAITS_OUT_THE_HOLD),
    # The agent is closed while it waits: the hold ends, and the request with it.
    ("allow_once", True, "closed", 409, None, None),
    pytest.param("allow_once", True, "agent", 403, None, None,
                 marks=[pytest.mark.variant("an agent holding the token can't answer a permission request"),
                        WAITS_OUT_THE_HOLD]),
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


@WAITS_OUT_THE_HOLD
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
    monkeypatch.setattr(tmux, "capture", lambda target, **kw: CLAUDE_PROMPT)
    monkeypatch.setattr(tmux, "send_keys", lambda target, *keys, **kw: pressed.append(keys))
    monkeypatch.setattr(holds, "_UnixConnection", lambda path, timeout: (_ for _ in ()).throw(OSError()))
    # No daemon to hold it in: the hook records it and the CLI prompts as usual.
    assert agents.permission_request_decision(db, db.get_agent("wk"), {**ASK, "cwd": str(repo)}) is None
    [shown] = [p.dto for p in inbox.pending_permissions(db, db.get_agent("wk"))]
    assert (shown.via, shown.choices, bool(shown.terminal)) == ("prompt", [], True)    # "Go to it"
    code, reply = _send("POST", "/v1/inbox/wk/permission", {"choice": "allow_once"})
    assert code == 409 and pressed == [], reply
    # Once the pane is past its prompt, nothing is shown as pending any more.
    monkeypatch.setattr(tmux, "capture", lambda target, **kw: CLAUDE_IDLE)
    assert inbox.pending_permissions(db, db.get_agent("wk")) == []


# The pane each second: the prompt, past it twice, a read it can't tell (the count starts over), past it for good.
# Or drawn after 11s busy: looked at every 5s till a look catches it, so the app's deny at 16s still reaches the hook.
@pytest.mark.parametrize("reads,looks,deny", [
    ([CLAUDE_PROMPT, CLAUDE_IDLE, CLAUDE_IDLE, "", CLAUDE_IDLE, CLAUDE_IDLE, CLAUDE_IDLE], list(range(7)), False),
    pytest.param([CLAUDE_PROMPT, CLAUDE_BUSY, CLAUDE_BUSY, "", CLAUDE_BUSY, CLAUDE_BUSY, CLAUDE_BUSY], list(range(7)),
                 False, marks=pytest.mark.variant("review: allowed in the pane, its tool still running, so no PostToolUse yet")),
    ([""] + [CLAUDE_BUSY] * 11 + [CLAUDE_PROMPT] * 5 + [CLAUDE_IDLE], list(range(17)), True)], ids=["denied", "allowed", "prompt-drawn-late"])
def test_inbox_resolved_in_pane_when_the_clis_own_prompt_is_answered(db, repo, answering, monkeypatch, reads, looks, deny):
    from brindle import tmux
    from brindle.db import DB
    from brindle.service import inbox

    clock, seen, posts, released, release = [0.0], [], [], threading.Event(), holds.HOLDS.release     # a fake clock
    config.set_local(str(repo), "permission_hold_seconds", 60)
    monkeypatch.setattr(agents, "HOLD_LOOK_SECONDS", 1.0)
    monkeypatch.setattr(agents, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(holds.HOLDS, "clock", lambda start=time.time(): start + clock[0])
    monkeypatch.setattr(holds.HOLDS, "release", lambda *a: [release(*a), released.set()][0])
    monkeypatch.setattr(tmux, "capture", lambda target, **kw: seen.append(int(clock[0])) or reads[int(clock[0])])

    def wait(holder, seconds):      # the daemon's, on the fake clock
        if deny and clock[0] == 16:
            dto = inbox.pending_permissions(db, db.get_agent("wk"))[0].dto
            posts.append(threading.Thread(target=lambda: _send("POST", "/v1/inbox/wk/permission", {
                "request": dto.request_id, "digest": dto.digest, "choice": "deny"})))
            posts[0].start()
            assert released.wait(5)     # handed over, for this wait to take
        clock[0] += seconds
        return holds.HOLDS.take(holder.request_id, holder.nonce)

    monkeypatch.setattr(holds.Holder, "wait", wait)
    out = agents.permission_request_decision(DB(), db.get_agent("wk"), {**ASK, "cwd": str(repo)})
    [post.join(5) for post in posts]
    assert _verdict(out) == ("deny" if deny else None) and seen == looks
    assert [i for i in inbox.items(db, str(repo), panes={"%2": True}) if i.agent_id == "wk"] == []
    assert bus.tail(db, kinds=("permission.changed",), repo_root=str(repo))


@pytest.fixture
def session_pr_forge(db, tmp_path, monkeypatch):
    from concurrent.futures import Future

    from brindle import git, spawn
    from brindle.daemon import session_review
    from brindle.service import inbox

    ws = Workspace("session", str(tmp_path), "session", "worktree", "sess", "main",
                   str(tmp_path), None, "session", 0)
    db.add_workspace(ws)
    db.add_agent(Agent("lead", ws.id, "supervisor", "claude", None, "interactive", "idle", "", None, 0))
    url = "https://github.com/o/r/pull/42"
    db.conn.execute("INSERT INTO session_prs (root_id, branch, url, pushed_sha, opened_at)"
                    " VALUES ('lead', 'sess', ?, 'head', 1)", (url,))
    forge = SimpleNamespace(ws=ws, url=url, local_head="head", merges=[], read_hook=None, merge_hook=None, failure=False,
                            payload={"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "head",
                                     "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]})

    class Inline:
        def submit(self, fn, *args):
            result = Future()
            result.set_result(fn(*args))
            return result

    def read(argv, **kwargs):
        if forge.read_hook:
            hook, forge.read_hook = forge.read_hook, None
            hook()
        value = forge.payload["state"] if argv[-1] == ".state" else json.dumps(forge.payload)
        return SimpleNamespace(returncode=0, stdout=value, stderr="")

    def action(argv, **kwargs):
        forge.merges.append(argv)
        if forge.merge_hook:
            forge.merge_hook()
        if forge.failure:
            return SimpleNamespace(returncode=1, stdout="", stderr="merge refused")
        forge.payload["state"] = "MERGED"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(workspaces.shutil, "which", lambda name: "/fake/gh")
    monkeypatch.setattr(spawn, "read", read)
    monkeypatch.setattr(spawn, "action", action)
    monkeypatch.setattr(session_review, "_pr_pool", Inline())
    monkeypatch.setattr(session_review, "_pr_cache", {})
    monkeypatch.setattr(session_review, "_pr_flights", {})
    monkeypatch.setattr(session_review, "_pr_changed", set())
    monkeypatch.setattr(session_review, "_head_changed", set())
    monkeypatch.setattr(session_review, "_head_cache", {
        ("head", ws.path): (time.monotonic(), {"url": url, "number": 42}),
    })
    monkeypatch.setattr(session_review, "_head_flights", set())
    monkeypatch.setattr(session_review, "_head_roots", {})
    monkeypatch.setattr(git, "out", lambda *args, **kwargs: forge.local_head)
    forge.items = lambda: inbox._pr_items(db, "lead", "sess", ws, {"root_id": "lead"})
    return forge


def test_auto_merge_session_pr_once_across_daemons_and_opt_out(db, session_pr_forge, monkeypatch):
    from brindle.daemon import session_review
    from brindle.db import DB

    forge = session_pr_forge
    settings = Path(forge.ws.repo_root) / ".brindle" / "config.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"auto_merge_session_prs": False}))
    session_review.refresh_pr_states(db)
    assert not forge.merges
    settings.write_text("{}")
    other = DB(db.path)
    try:
        forge.merge_hook = lambda: session_review.refresh_pr_states(other)
        session_review.refresh_pr_states(db)
        session_review.refresh_pr_states(other)
        assert len(forge.merges) == 1
        assert forge.merges[0][:3] == ["gh", "pr", "merge"]
        assert forge.merges[0][forge.merges[0].index("--match-head-commit") + 1] == "head"
        assert db.conn.execute("SELECT merged_at FROM session_prs").fetchone()[0] is None
        # Only the existing merge observer records the merge and starts session sync.
        landed = []
        monkeypatch.setattr(session_review, "_merge_polled", 0.0)
        monkeypatch.setattr(session_review, "_merge_checks", {})
        monkeypatch.setattr(session_review, "_sync_after_merge", lambda *args: landed.append(args[1:]))
        session_review.poll_merged(db)
        assert db.conn.execute("SELECT merged_at FROM session_prs").fetchone()[0] is not None
        assert landed == [("lead", "sess", forge.url)]
    finally:
        other.conn.close()


def test_auto_merge_session_pr_rechecks_forge_and_record(db, session_pr_forge):
    from brindle.daemon import session_review

    forge = session_pr_forge
    session_review._read_pr_state(forge.url, forge.ws.repo_root)
    forge.payload["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    session_review.refresh_pr_states(db)
    assert not forge.merges
    forge.payload["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    session_review._read_pr_state(forge.url, forge.ws.repo_root)
    forge.read_hook = lambda: db.conn.execute("UPDATE session_prs SET url = NULL")
    session_review.refresh_pr_states(db)
    assert not forge.merges
    db.conn.execute("UPDATE session_prs SET url = ?", (forge.url,))
    session_review.refresh_pr_states(db)
    assert len(forge.merges) == 1


def test_auto_merge_session_pr_failure_reaches_inbox_once(db, session_pr_forge):
    from brindle.daemon import session_review
    from brindle.db import DB

    forge = session_pr_forge
    forge.failure = True
    session_review.refresh_pr_states(db)
    other = DB(db.path)
    try:
        session_review.refresh_pr_states(other)
    finally:
        other.conn.close()
    assert len(forge.merges) == 1
    assert [item.kind for item in forge.items()] == ["pr_failed"]
    assert db.conn.execute("SELECT merged_at FROM session_prs").fetchone()[0] is None
    forge.local_head = "unpushed"
    session_review._head_cache[(forge.local_head, forge.ws.path)] = (time.monotonic(), {})
    assert [item.kind for item in forge.items()] == ["pr_failed"]
    forge.payload["state"] = "CLOSED"
    session_review._read_pr_state(forge.url, forge.ws.repo_root)
    assert not forge.items()
    forge.payload["state"] = "OPEN"
    session_review._read_pr_state(forge.url, forge.ws.repo_root)
    db.conn.execute("UPDATE session_prs SET merged_at = 1")
    assert not forge.items()
    forge.local_head = "head"  # an older HEAD lookup still says this PR is open
    assert all(item.kind != "pr_failed" for item in forge.items())


def test_auto_merge_session_pr_interrupted_owner(db, session_pr_forge):
    from brindle.daemon import session_review
    from brindle.db import DB

    forge = session_pr_forge
    # Upgrading an older database must recover its ownerless claims as well.
    db.conn.execute("DROP TABLE session_pr_merges")
    db.conn.execute("CREATE TABLE session_pr_merges (url TEXT PRIMARY KEY, state TEXT NOT NULL, error TEXT)")
    migrated = DB(db.path)
    migrated.conn.close()
    # An earlier daemon died after inserting its claim, before contacting GitHub.
    db.conn.execute("INSERT INTO session_pr_merges (url, state) VALUES (?, 'merging')", (forge.url,))
    session_review.refresh_pr_states(db)
    assert not forge.merges
    assert [item.kind for item in forge.items()] == ["pr_failed"]

    db.conn.execute("DELETE FROM session_pr_merges WHERE url = ?", (forge.url,))
    other = DB(db.path)
    try:
        def interrupted():
            # The owner is still live: a second daemon must leave its claim alone.
            session_review.refresh_pr_states(other)
            assert not session_review.pr_problem(other, "lead", forge.url)
            raise SystemExit

        forge.merge_hook = interrupted
        with pytest.raises(SystemExit):
            session_review.refresh_pr_states(db)
        forge.merge_hook = None
        assert len(forge.merges) == 1
        payload, forge.payload = forge.payload, None  # the forge cannot confirm the interrupted submission
        session_review.refresh_pr_states(other)
        forge.payload = payload
        assert len(forge.merges) == 1
        assert [item.kind for item in forge.items()] == ["pr_failed"]

        # If GitHub accepted the interrupted request, reconcile without submitting it again.
        db.conn.execute("UPDATE session_pr_merges SET state = 'merging' WHERE url = ?", (forge.url,))
        forge.payload["state"] = "MERGED"
        session_review.refresh_pr_states(other)
        assert len(forge.merges) == 1
        assert not session_review.pr_problem(other, "lead", forge.url)
        assert db.conn.execute("SELECT state FROM session_pr_merges").fetchone()[0] == "submitted"
        assert db.conn.execute("SELECT merged_at FROM session_prs").fetchone()[0] is None
    finally:
        other.conn.close()


@pytest.mark.parametrize("problem", ["conflict", "ci"])
def test_auto_merge_session_pr_unattended_problem_reaches_inbox(db, session_pr_forge, problem):
    from brindle.daemon import session_review

    forge = session_pr_forge
    if problem == "conflict":
        forge.payload["mergeable"] = "CONFLICTING"
    else:
        forge.payload["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    # A dismissed opened-PR card must not hide a later failure.
    db.conn.execute("INSERT INTO session_pr_declined (root_id, branch, sha) VALUES ('lead', 'sess', ?)",
                    (f"head|{forge.url}",))
    db.add_agent(Agent("worker", forge.ws.id, "developer", "claude", "lead", "assign", "processing", "", None, 0))
    session_review.refresh_pr_states(db)
    assert not forge.items()
    db.set_status("worker", "done")
    session_review.refresh_pr_states(db)
    assert [item.kind for item in forge.items()] == ["pr_failed"]
    assert not forge.merges
    forge.local_head = "unpushed"
    session_review._head_cache[(forge.local_head, forge.ws.path)] = (time.monotonic(), {})
    assert [item.kind for item in forge.items()] == ["pr_failed"]


@pytest.fixture
def lead(db, repo, tmp_path, monkeypatch):
    """A session ``lead`` on branch ``sess`` (base main) in its own checkout, its goal reached; jobs run inline."""
    from brindle import spawn
    monkeypatch.setattr(spawn, "room", lambda: 4)
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


@pytest.mark.slow("real git end to end: 14 readiness reads of a live checkout, ~210 git calls")
def test_session_review_readiness_says_what_is_missing(db, lead, monkeypatch):
    from brindle import gates
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
    real_head = gates.head
    monkeypatch.setattr(gates, "head", lambda ws: head if ws.id == "proj/w-f" else real_head(ws))
    monkeypatch.setattr(session_review, "_pr_open_state", lambda *a: False)    # readiness is under test, not GitHub
    assert missing() == 5 and _send("GET", "/v1/sessions/lead/pr/draft")[0] == 200
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


def _readiness_prepares_after_startup_and_recovers(db, lead, monkeypatch, obstacle):
    from brindle import gates, git, identity_lock
    from brindle.daemon import session_review
    monkeypatch.setattr(session_review.session_sync, "refresh_drift_flags", lambda db: None)
    monkeypatch.setattr(session_review.session_sync, "refresh_idle_sessions", lambda db, fetched_at: None)
    head = _commit(Path(lead.path), "a.py", "a\n")
    config.set_local(lead.repo_root, "checks", ["validate"])
    held = []
    monkeypatch.setattr(routes_actions, "_spawn", held.append)
    monkeypatch.setattr(gates, "run_checked", lambda *a, **kw: (True, ""))
    watch = session_review.ReadinessWatch()
    watch.sweep(db)
    if obstacle == "identity":
        monkeypatch.setattr(identity_lock, "locked_by", lambda *a: "other")
        watch.sweep(db)
        assert held == []
        monkeypatch.setattr(identity_lock, "locked_by", lambda *a: None)
    watch.sweep(db)
    assert len(held) == 1
    if obstacle == "interrupted":
        def fail(*a, **kw):
            raise RuntimeError("checks unavailable")
        monkeypatch.setattr(gates, "run_checked", fail)
        held.pop()()
        watch.sweep(db)
        assert held == []  # respect preparation's existing backoff
        routes_actions._PR_TRIED[("lead", "sess")] = (head, time.monotonic() - 61)
        monkeypatch.setattr(gates, "run_checked", lambda *a, **kw: (True, ""))
        watch.sweep(db)
    held.pop()()
    monkeypatch.setattr(git, "run", lambda *a, **kw: pytest.fail("settled preparation ran git"))
    watch.sweep(db)
    assert db.conn.execute("SELECT state FROM session_pr_prep WHERE root_id = 'lead'").fetchone()[0] == "passed"


@pytest.mark.parametrize("scenario, params", [
    pytest.param(_review_lock_returns_busy_without_starting_work, {"checks": []}, id="review_lock-no-checks",
                 marks=pytest.mark.variant("a repo with no checks takes the reviewer-only path; it must also report busy")),
    pytest.param(_review_lock_returns_busy_without_starting_work, {"checks": ["true"]}, id="review_lock-checks"),
    pytest.param(_readiness_prepares_after_startup_and_recovers, {"obstacle": "identity"}, id="readiness-identity"),
    pytest.param(_readiness_prepares_after_startup_and_recovers, {"obstacle": "interrupted"},
                 id="readiness-interrupted"),
])
def test_review_lock_and_readiness_preparation(request, scenario, params):
    run_scenario(request, scenario, params)


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
    ("app.py", {}, "delete", 200),
    ("new/deep/n.py", {"content": "n\n"}, "hook-refuses", 409),    # the file and its new directories go
    ("app.py", {"content": "x\n"}, "exists", 409),                  # a create where a file already is
    ("app.py", {"content": "print('bye')\n"}, "busy", 409),
    ("app.py", {"content": "print('bye')\n"}, "merging", 409),
], ids=["delete", "create-refused", "exists", "busy", "merging"])
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


@pytest.mark.slow("real git end to end: the session's checks run and its branch is pushed, ~110 git calls")
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
    db.update_autopilot("lead", state="done", audit_sha=_commit(sess, "a.py", "a\n"), audit_ok=1)
    code, draft = _send("GET", "/v1/sessions/lead/pr/draft")
    assert code == 200 and draft["title"] == "Ship it" and "- tests pass" in draft["body"]
    config.set_local(lead.repo_root, "checks", ["echo boom; false"])    # a failing check: nothing is pushed
    code, reply = _send("POST", "/v1/sessions/lead/pr", {"title": draft["title"], "body": draft["body"]})
    assert code == 202, reply
    failed = bus.tail(db, kinds=("job.done", "job.failed"))[-1]
    assert failed["kind"] == "job.failed" and "checks failed: echo boom; false" in failed["payload"]["error"]["message"]
    assert "boom" in failed["payload"]["error"]["message"] and not called.exists()
    assert sh("git ls-remote origin sess", sess) == ""
    config.set_local(lead.repo_root, "checks", ["true"])
    code, reply = _send("POST", "/v1/sessions/lead/pr", {"title": draft["title"], "body": draft["body"]})
    assert code == 202, reply
    done = bus.tail(db, kinds=("job.done", "job.failed"))
    assert done[-1]["payload"]["result"]["url"] == "https://github.com/o/r/pull/7"
    assert "--base main --head sess --title Ship it --body" in called.read_text()


@pytest.mark.parametrize("passed", [
    pytest.param(True, id="new-commits",
                 marks=pytest.mark.slow("real git end to end: ~150 git calls through readiness, checks, push and gh")),
    pytest.param(False, id="checks-fail"),
])
def test_pr_waits_for_approval(db, lead, tmp_path, monkeypatch, passed):
    """A ready session's PR is prepared (drafted, its checks run, nothing pushed) and put to the
    person in the Inbox; only their Open PR pushes the checked sha, and an open PR's new commits
    wait for their Push. (One row walks ready, open and new commits in turn.)"""
    from brindle import gates
    from brindle.daemon import session_review
    from brindle.service import inbox

    sess, called, runs, told = Path(lead.path), tmp_path / "gh-called", [], []
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    # `pr list` is the inbox's background look for a PR already open on HEAD: not an opening.
    fake.write_text(f"#!/bin/sh\n[ \"$2\" = list ] || echo \"$@\" >> {called}\necho https://github.com/o/r/pull/7\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    config.set_local(lead.repo_root, "checks", ["validate"])
    monkeypatch.setattr(gates, "run_checked", lambda db, ws, cmd, *a, **kw: (runs.append(cmd), (passed, "boom"))[1])
    monkeypatch.setattr(agents, "send_message", lambda db, to, text, **kw: told.append((to, text)))

    def remote() -> str:
        return (sh("git ls-remote origin sess", sess).split() or [""])[0]

    def asked() -> list[tuple]:
        return [(i.kind, i.reason) for i in inbox.items(db, None, panes={}) if i.kind.startswith("pr_")]

    watch, held = session_review.ReadinessWatch(), []
    watch.sweep(db)
    head = _commit(sess, "a.py", "a\n")
    # Its preparation dispatched but not yet run: the next sweep leaves that work alone.
    monkeypatch.setattr(routes_actions, "_spawn", held.append)
    assert watch.sweep(db) == ["lead"] and watch.sweep(db) == [] and len(held) == 1
    assert (runs, asked(), remote()) == ([], [], "")
    held.pop()()
    monkeypatch.setattr(routes_actions, "_spawn", lambda target: target())
    assert watch.sweep(db) == []        # checked once per HEAD
    assert (runs, remote(), called.exists()) == (["validate"], "", False)
    if not passed:      # the supervisor hears which check failed, and the PR waits
        assert asked() == [("pr_failed", "PR checks failed: validate")]
        assert [to for to, _ in told] == ["lead"] and "validate" in told[0][1] and "boom" in told[0][1]
        return
    assert asked() == [("pr_ready", "PR ready for your approval")] and told == []
    # A check required since brindle prepared it, the HEAD the same: that one runs, the passed one isn't run again.
    config.set_local(lead.repo_root, "checks", ["validate", "lint"])
    assert _send("POST", "/v1/sessions/lead/pr", {"title": "Ship it"})[0] == 202
    # The sha brindle checked; the Inbox item goes.
    # The person opened it: no "PR opened" card tells them so.
    assert (remote(), runs, called.read_text().count("pr create"), asked()) == (head, ["validate", "lint"], 1, [])
    newer = _commit(sess, "a.py", "b\n")
    watch.sweep(db)
    _, ready = _send("GET", "/v1/sessions/lead/pr/readiness")
    assert (remote(), ready["pr"]) == (head, {"url": "https://github.com/o/r/pull/7", "unpushed": 1})
    assert _send("POST", "/v1/sessions/lead/pr/push")[0] == 202
    _, ready = _send("GET", "/v1/sessions/lead/pr/readiness")
    assert (remote(), ready["pr"]["unpushed"], runs) == (newer, 0, ["validate", "lint", "validate", "lint"])
    # A PR gh opened from another branch name is adopted, but a Push to the session branch would not reach it.
    db.conn.execute("INSERT INTO session_pr_heads (root_id, branch, head_ref) SELECT root_id, branch, 'stress/other' FROM session_prs")
    code, refused = _send("POST", "/v1/sessions/lead/pr/push")
    assert code == 409 and "stress/other" in json.dumps(refused)


def test_next_session_pr_opens_from_the_same_branch_after_one_merges(db, repo, lead, tmp_path, monkeypatch):
    """A merged session PR stays the session's until its branch has changes of its own beyond it (a
    sync's clean merge from main doesn't count, a merge resolved with new content does); then the next PR opens
    from the same branch, replacing the merged one's row."""
    from brindle.daemon import session_review

    sess, first, second = Path(lead.path), "https://github.com/o/r/pull/7", "https://github.com/o/r/pull/8"
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\necho {second}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    monkeypatch.setattr(session_review, "open_pr_for_head", lambda *a, **kw: None)    # GitHub isn't under test
    pushed = _commit(sess, "a.py", "a\n")
    db.conn.execute("INSERT INTO session_prs (root_id, branch, url, pushed_sha, opened_at, merged_at)"
                    " VALUES ('lead', 'sess', ?, ?, 1, 2)", (first, pushed))     # as poll_merged leaves it

    def readiness() -> tuple:
        _, reply = _send("GET", "/v1/sessions/lead/pr/readiness")
        return reply["ready"], reply["pr"], session_review.goal_phase(db, "lead")

    _commit(repo, "main.py", "m\n")
    sh("git merge -q --no-ff main -m sync", sess)       # a sync brings main in: nothing new of its own
    ready, pr, phase = readiness()
    assert (ready, pr, phase) == (False, None, ("done", first))
    # Main changes the PR's file in a squash brindle recorded (one edited on the forge, so not replayed
    # `-s ours`); the next sync's conflict is resolved with new content in the merge alone.
    from brindle import session_sync
    assert session_sync.record_squash(db, lead, _commit(repo, "a.py", "z\n"), pushed)
    sh("git merge -q main || true", sess)
    (sess / "a.py").write_text("resolved\n")
    sh("git add a.py && git commit -q --no-edit", sess)
    assert readiness() == (True, None, ("ready_for_pr", None))
    code, reply = _send("POST", "/v1/sessions/lead/pr", {"title": "Next"})
    assert code == 202, reply
    assert readiness() == (True, {"url": second, "unpushed": 0}, ("pr_open", second))


@pytest.fixture
def phase_pr_forge(db, lead, monkeypatch):
    """``lead``'s goal in four phases (each milestone's check passes once ``land`` names it), with
    GitHub and git pushes faked in process: ``remote`` is what origin's branches hold, ``opened`` the
    PRs created; ``before_job`` runs between a landing's dispatch and its job."""
    from concurrent.futures import Future

    from brindle import gates, git, spawn
    from brindle.config import RepoConfig
    from brindle.daemon import session_review

    forge = SimpleNamespace(remote={}, opened=[], merged=[], state="OPEN", before_job=lambda: None,
                            sess=Path(lead.path))
    passing: set[str] = set()
    real_read, real_action = spawn.read, spawn.action

    def payload() -> dict:
        return {"state": forge.state, "mergeable": "MERGEABLE", "headRefOid": forge.remote.get("sess"),
                "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}

    def read(argv, **kwargs):
        if argv[:3] != ["gh", "pr", "view"]:
            return real_read(argv, **kwargs)
        return SimpleNamespace(returncode=0, stdout=forge.state if argv[-1] == ".state" else json.dumps(payload()),
                               stderr="")

    def action(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "create"]:
            forge.opened.append(argv)
            forge.state = "OPEN"
            return SimpleNamespace(returncode=0, stdout=f"https://github.com/o/r/pull/{len(forge.opened)}", stderr="")
        if argv[:3] == ["gh", "pr", "merge"]:
            forge.merged.append(argv[-1])       # --match-head-commit's
            forge.state = "MERGED"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_action(argv, **kwargs)

    class Inline:
        def submit(self, fn, *args):
            done = Future()
            done.set_result(fn(*args))
            return done

    def spawn_job(target):
        forge.before_job()
        target()

    monkeypatch.setattr(workspaces.shutil, "which", lambda name: "/fake/gh")
    monkeypatch.setattr(spawn, "read", read)
    monkeypatch.setattr(spawn, "action", action)
    monkeypatch.setattr(git, "push", lambda path, branch, sha=None, track=True: forge.remote.__setitem__(branch, sha))
    monkeypatch.setattr(routes_actions, "_spawn", spawn_job)
    monkeypatch.setattr(routes_actions, "_PHASE_WAITING", {})
    monkeypatch.setattr(session_review, "open_pr_for_head", lambda *a, **kw: None)    # GitHub's PR search isn't under test
    monkeypatch.setattr(session_review, "_pr_pool", Inline())
    monkeypatch.setattr(session_review, "_pr_cache", {})
    monkeypatch.setattr(session_review, "_pr_flights", {})
    monkeypatch.setattr(session_review, "_pr_changed", set())
    monkeypatch.setattr(session_review, "_head_changed", set())
    monkeypatch.setattr(agents, "_notify_supervisor", lambda *a: None)
    monkeypatch.setattr(gates, "run_checked", lambda db, ws, cmd, *a, **kw: (cmd in passing, ""))
    autopilot.set_goal(db, "lead", "Ship it", [(p, p, None, None, None, p) for p in ("api", "db", "docs", "ui")])

    def land(phase: str) -> str:
        """``phase``'s milestone passes on HEAD; the next phase's work is committed; the sweep lands."""
        passing.add(phase)
        autopilot.check_milestones(db, "lead", lead, cfg=RepoConfig(goal_audit=False))
        tip = _commit(forge.sess, f"after-{phase}.py", "next\n")
        routes_actions.land_phase_prs(db)
        assert sh("git rev-parse HEAD", forge.sess) == tip      # the local branch never moves
        return tip

    forge.land = land
    forge.pr = lambda: tuple(db.conn.execute("SELECT url, pushed_sha, merged_at FROM session_prs").fetchone())
    forge.states = lambda: [tuple(r) for r in db.conn.execute("SELECT phase, state FROM phase_prs ORDER BY rowid")]
    return forge


def test_phase_pr_lands_each_phase_at_its_passed_commit_mid_goal(db, phase_pr_forge):
    """While a later phase's milestone still fails, brindle lands a completed phase itself: it opens the
    session PR at the commit the phase passed on, then pushes the next phase's commit to that PR, never
    the branch's tip. A checkout dirtied between the landing's dispatch and its job refuses it."""
    from brindle.daemon import session_review

    forge, url = phase_pr_forge, "https://github.com/o/r/pull/1"
    first = _commit(forge.sess, "a.py", "a\n")
    assert session_review.reasons(db, *session_review.session(db, "lead"))    # brindle's own Open PR is refused
    tip = forge.land("api")
    assert (forge.remote, forge.pr()) == ({"sess": first}, (url, first, None))
    forge.land("db")
    assert (forge.remote, forge.pr(), len(forge.opened)) == ({"sess": tip}, (url, tip, None), 1)
    forge.before_job = lambda: (forge.sess / "a.py").write_text("uncommitted\n")
    forge.land("docs")
    assert forge.remote == {"sess": tip}
    assert forge.states() == [("api", "pushed"), ("db", "pushed"), ("docs", "refused")]


@pytest.mark.parametrize("merged_by", ["brindle", "github"])
def test_phase_pr_waits_out_a_merge_of_its_pr_then_opens_the_next(db, phase_pr_forge, merged_by):
    """The session's green PR merges (brindle's automatic merge earlier in the same sweep, or on
    GitHub before brindle saw it) while the next phase waits to land: nothing is pushed to the merged
    PR and its record keeps what it merged; once the merge is recorded the phase opens the next PR."""
    from brindle.daemon import session_review

    forge = phase_pr_forge
    first = _commit(forge.sess, "a.py", "a\n")
    pin = forge.land("api")
    if merged_by == "brindle":
        forge.before_job = lambda: session_review.refresh_pr_states(db)
    else:
        forge.before_job = lambda: setattr(forge, "state", "MERGED")
    forge.land("db")
    assert (forge.state, forge.remote, forge.pr()) == ("MERGED", {"sess": first},
                                                        ("https://github.com/o/r/pull/1", first, None))
    assert forge.states() == [("api", "pushed"), ("db", "pending")]
    db.conn.execute("UPDATE session_prs SET merged_at = 2")       # as poll_merged records it
    forge.before_job = lambda: None
    routes_actions._PHASE_WAITING.clear()
    routes_actions.land_phase_prs(db)
    assert (forge.remote, forge.pr()) == ({"sess": pin}, ("https://github.com/o/r/pull/2", pin, None))
    assert forge.states() == [("api", "pushed"), ("db", "pushed")]


@pytest.mark.parametrize("outcome", ["reached", pytest.param("lost", marks=pytest.mark.variant("a race: the push was cut short and never reached the forge")), "unknown"])
def test_phase_pr_push_cut_short_is_reconciled_before_its_pr_merges(db, phase_pr_forge, monkeypatch, outcome):
    """A phase landing's process goes away mid push, before it records the commit pushed: the automatic
    merge first reads the PR's head. The push's commit there (reached) becomes what the PR carries, so
    the merge covers it; the commit recorded before (lost) merges as recorded; anything else (unknown)
    is left for the person, unmerged."""
    from brindle import git
    from brindle.daemon import session_review

    class Gone(BaseException):
        """The daemon's process ending: nothing after the push runs."""

    forge, left = phase_pr_forge, []
    first = _commit(forge.sess, "a.py", "a\n")
    pin = forge.land("api")

    def push(path, branch, sha=None, track=True):
        forge.remote[branch] = {"reached": sha, "lost": first, "unknown": "f" * 40}[outcome]
        left.extend(tuple(r) for r in db.conn.execute("SELECT url, state, owner FROM session_pr_merges"))
        left.extend(tuple(r) for r in db.conn.execute("SELECT url, sha FROM session_pr_pushes"))
        raise Gone

    monkeypatch.setattr(git, "push", push)
    with pytest.raises(Gone):
        forge.land("db")
    # What the process left in the database: its finally never ran.
    db.conn.execute("INSERT OR REPLACE INTO session_pr_merges (url, state, owner) VALUES (?, ?, ?)", left[0])
    db.conn.execute("INSERT OR REPLACE INTO session_pr_pushes (url, sha) VALUES (?, ?)", left[1])
    session_review.refresh_pr_states(db)
    session_review.refresh_pr_states(db)
    merge = db.conn.execute("SELECT state FROM session_pr_merges").fetchone()
    expected = {"reached": ([pin], pin, "submitted"), "lost": ([first], first, "submitted"),
                "unknown": ([], first, "failed")}[outcome]
    assert (forge.merged, forge.pr()[1], merge["state"]) == expected


def _provider_setup(tmp_path, monkeypatch, onboarded: bool = True) -> tuple:
    """Every CLI installed (a stub), Claude Code's and Codex's config dirs present."""
    monkeypatch.setattr(preflight, "check", REAL_PREFLIGHT)
    monkeypatch.setattr(providers, "claude_onboarded", REAL_ONBOARDED)
    for env in ("BRINDLE_CLAUDE_BIN", "BRINDLE_CODEX_BIN", "BRINDLE_AGY_BIN", "BRINDLE_OPENCODE_BIN"):
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
    # (A start signed out, or with nothing to answer: test_a_new_session_runs_on_a_usable_cli_or_starts_nothing.)
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
], ids=["start-untrusted", "start-stored-key", "stale-open", "continue",
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
            ws = repos.target_workspace(db, "sup", repos.attach(db, "sup", str(tmp_path / "other")).repo_root)

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
    # The "key" setup: the profile's own value, with a different one in brindle's environment.
    # A fixed placeholder, not brindle's SECRET: the profile file is plain text on disk.
    profile_value = "placeholder-profile-value-0000wxyz"
    (agents_dir / "supervisor.md").write_text(
        f"---\nname: supervisor\nprovider: {provider}\napi_key_env: NODE_OPTIONS\n"
        f"env.NODE_OPTIONS: --require ./x.js\n{'env.ANTHROPIC_API_KEY: ' + profile_value + chr(10) if setup == 'key' else ''}"
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
    assert SECRET not in json.dumps(items) and profile_value not in json.dumps(items)
    assert configs() == before     # asking changes nothing
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
        assert providers.api_key_approved(profile_value) and not providers.api_key_approved("sk-ant-ambient-0000000000abcd")
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


@pytest.mark.parametrize("what,fails", [("upgrade", False), ("upgrade", True), ("hook", False), ("hook", True)])
def test_a_background_setup_job_is_shared_while_it_runs_and_reports_on_the_bus(
        db, world, monkeypatch, tmp_path, what, fails):
    from brindle import codex_hook

    monkeypatch.setenv("HOME", str(tmp_path))
    path = "/v1/engine/upgrade" if what == "upgrade" else "/v1/setup/codex-hook"
    runs, again = [], []

    def work():
        runs.append(1)
        again.append(_send("POST", path))      # pressed again while it runs
        if what == "hook" and fails:
            raise codex_hook.CodexHookError("Codex didn't answer")
        return (1 if fails else 0), "uv line"

    if what == "upgrade":
        monkeypatch.setattr(routes_actions, "_run_upgrade", work)
    else:
        monkeypatch.setattr(codex_hook, "inspect", lambda binary, command=None: SimpleNamespace(command="hook"))
        monkeypatch.setattr(codex_hook, "trust", lambda binary, st, *, turn_on: work())   # the person's own press
    code, reply = _send("POST", path)
    assert code == 202 and runs == [1] and again == [(202, reply)]
    events = bus.tail(db, kinds=("job.done", "job.failed"))
    assert [(e["kind"], e["payload"]["job"]) for e in events] == [
        ("job.failed" if fails else "job.done", reply["job"])]
    if fails:
        assert events[0]["payload"]["error"]["code"] == ("upgrade_failed" if what == "upgrade" else "codex_hook_failed")


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


@pytest.fixture
def cloning(world, repo, monkeypatch):
    from brindle import clone

    monkeypatch.setattr(clone, "ALLOW_FILE_URLS", True)       # tests only: production refuses file://
    return f"file://{repo.parent / 'origin.git'}"


def test_a_clone_streams_git_progress_and_finishes_with_the_path(db, cloning, tmp_path):
    from brindle import git

    # The picker's other path: opening a plain folder only initializes it after confirmation.
    folder = tmp_path / "new-project"
    folder.mkdir()
    (folder / "notes.txt").write_text("keep my files\n")
    code, reply = _send("POST", "/v1/repos/open", {"path": str(folder)})
    assert (code, reply["error"]["code"]) == (409, "not_a_repo")
    assert not (folder / ".git").exists()
    code, reply = _send("POST", "/v1/repos/open", {"path": str(folder), "init": True})
    assert code == 200, reply
    assert reply["initialized"] and reply["repo_root"] == str(folder)
    assert git.run(["rev-parse", "--verify", "HEAD"], folder, check=False).returncode == 0
    assert (folder / "notes.txt").read_text() == "keep my files\n"
    assert git.run(["ls-tree", "--name-only", "HEAD"], folder).stdout == ""
    head = git.run(["rev-parse", "HEAD"], folder).stdout
    assert _send("POST", "/v1/repos/open", {"path": str(folder), "init": True})[0] == 200
    assert git.run(["rev-parse", "HEAD"], folder).stdout == head
    code, reply = _send("POST", "/v1/repos/clone", {"url": cloning, "parent_dir": str(folder / "notes.txt")})
    assert (code, reply["error"]["code"]) == (400, "invalid")
    parent = tmp_path / "projects"
    code, reply = _send("POST", "/v1/repos/clone", {"url": cloning, "parent_dir": str(parent)})
    assert code == 202, reply
    done = bus.tail(db, kinds=("job.done", "job.failed"))
    assert [(e["kind"], e["payload"]["result"]) for e in done] == [
        ("job.done", {"path": str(parent / "origin")})]
    assert (parent / "origin" / "app.py").is_file()
    assert {"repo_root": str(parent / "origin"), "name": "origin", "demo": False} in _send("GET", "/v1/repos")[1]
    assert any(e["payload"]["job"] == reply["job"] for e in bus.tail(db, kinds=("job.progress",)))
    again, err = _send("POST", "/v1/repos/clone", {"url": cloning, "parent_dir": str(parent)})
    assert (again, err["error"]["code"]) == (409, "exists")


def test_a_second_clone_press_gets_the_job_already_queued(db, cloning, tmp_path, monkeypatch):
    queued = []
    monkeypatch.setattr(routes_actions, "_spawn", queued.append)
    body = {"url": cloning, "parent_dir": str(tmp_path)}
    (c1, first), (c2, second) = _send("POST", "/v1/repos/clone", body), _send("POST", "/v1/repos/clone", body)
    assert (c1, c2, second["job"], len(queued)) == (202, 202, first["job"], 1)
    queued[0]()
    assert [e["kind"] for e in bus.tail(db, kinds=("job.done", "job.failed"))] == ["job.done"]


@pytest.mark.parametrize("url", [
    "/tmp/some/repo",
    pytest.param("-u/bin/sh", marks=pytest.mark.variant("an option smuggled in as the url")),
    pytest.param("file:///tmp/x.git", marks=pytest.mark.variant("file:// stays refused in production")),
    pytest.param("git@local.git", marks=pytest.mark.variant("git@ without a host:path is a relative local path")),
    pytest.param("git@folder/repo.git", marks=pytest.mark.variant("a slash before any colon is a local path")),
    pytest.param("https://-oProxyCommand=x/a.git", marks=pytest.mark.variant("an option as the host")),
    pytest.param("https://example.com/a b.git", marks=pytest.mark.variant("whitespace in the url")),
])
def test_a_clone_refuses_what_is_not_a_remote_url(world, tmp_path, url):
    code, reply = _send("POST", "/v1/repos/clone", {"url": url, "parent_dir": str(tmp_path)})
    assert (code, reply["error"]["code"]) == (400, "invalid"), reply


@pytest.mark.variant("a race: a second settings write lands between the first's read and its write")
def test_a_local_write_made_while_another_is_reading_is_not_lost(repo, monkeypatch):
    """Forces the interleaving: a second action starts between the first's read and write, and the
    first waits until the second has either finished (unlocked writes: its update is then
    overwritten) or is blocked on the write lock (locked: it applies after the first)."""
    config.set_local(repo, "plan_first", True)
    real_read, real_lock = config._read_json, config._REPO_WRITE
    settled, started, errors = threading.Event(), threading.Event(), []

    def run_writer():
        try:
            config.set_local(repo, "max_agents", 7)
        except BaseException as e:  # noqa: BLE001 - re-raised on the test's thread below
            errors.append(e)
        settled.set()

    writer = threading.Thread(target=run_writer)

    class SpyLock:
        def __enter__(self):
            if threading.current_thread() is writer:
                settled.set()   # the second action is waiting for its turn
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    def read(path):
        data = real_read(path)
        if threading.current_thread() is not writer and not started.is_set():
            started.set()
            writer.start()      # another daemon action, mid-read
            settled.wait()
        return data

    monkeypatch.setattr(config, "_REPO_WRITE", SpyLock(), raising=False)
    monkeypatch.setattr(config, "_read_json", read)
    config.unset_local(repo, "plan_first")
    writer.join()
    assert errors == []
    assert real_read(config.scope_paths(repo)["local"]) == {"max_agents": 7}


def test_user_scope_routing_judges_the_persons_profile_not_the_repos_same_named_one(repo, world, monkeypatch):
    from brindle import doctor, providers, worker_models
    from brindle.pro import managed_models, team_policy

    for where, model in ((config.user_profiles_dir(), "user-model"), (repo / ".brindle" / "agents", "repo-model")):
        where.mkdir(parents=True, exist_ok=True)
        for name, provider in (("mine", "claude"), ("developer-codex", "codex")):
            (where / f"{name}.md").write_text(f"---\nname: {name}\nprovider: {provider}\nmodel: {model}\n---\nWork.\n")
    config.set_user("routing_light", "codex")       # the picker's pick, on the person's developer-codex
    acc = Access(org_id="acme", allowed_providers=("claude", "codex"), allowed_models=("user-model",))
    monkeypatch.setattr(team_policy, "access", lambda repo: acc)
    monkeypatch.setattr(managed_models, "current", lambda repo: None)
    monkeypatch.setattr(providers, "claude_org_managed", lambda: False)
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["claude", "codex"])
    monkeypatch.setattr(worker_models, "_signed_in", None)
    code, body = _send("GET", f"/v1/routing?repo={repo}&scope=user&learned=0")
    assert code == 200 and {p["name"]: p["allowed"] for p in body["profiles"]}["mine"] is True
    assert body["tiers"]["light"]["values"] == ["developer-codex"]
    code, reply = _send("PUT", "/v1/routing", {"repo": str(repo), "scope": "user", "tiers": {"light": ["mine"]}})
    assert code == 200, reply
    assert [m["model"] for m in _send("GET", f"/v1/routing?repo={repo}&scope=user&learned=0")[1]["tiers"]["light"]["models"]] \
        == ["user-model"]


def test_hold_skipped_without_app(db, repo, world, answering, monkeypatch):
    from brindle import permissions
    from brindle.daemon import events, holds

    monkeypatch.setattr(events, "connected", 0)
    monkeypatch.setattr(holds, "HOLDS", holds.Holds())
    monkeypatch.setattr(holds.Holder, "wait", lambda self, seconds: pytest.fail("waited for nobody"))
    # No events client: the daemon refuses the hold, so the hook returns at once and the CLI prompts.
    assert agents.permission_request_decision(db, db.get_agent("wk"), {**ASK, "cwd": str(repo)}) is None
    assert holds.HOLDS.agents() == []

    # The last client leaving while the route awaits (the caller's ancestry) is refused too.
    monkeypatch.setattr(events, "connected", 1)
    monkeypatch.setattr(peer, "caller_agents", lambda pid: (setattr(events, "connected", 0), {"wk"})[1])
    body = {"agent_id": "wk", "request_id": "r-gone", "digest": "d", "nonce": "n", "hold_until": time.time() + 60}
    assert _send("POST", "/v1/holds", body, peer_pid=310, token=None)[0] == 503
    assert holds.HOLDS.agents() == []

    # A hook reaching its deadline also returns to the CLI without waiting.
    req = permissions.Request("claude", "bash", "Bash", command="npm test")
    db.add_permission_request("wk", "expired", json.dumps(req.to_dict()), "", None, "hook")
    monkeypatch.setattr(agents, "time", SimpleNamespace(time=lambda: 100.0, monotonic=lambda: 100.0))
    assert agents._await_app_answer(db, db.get_agent("wk"), SimpleNamespace(request_id="expired"),
                                    req, 99.0) == "timeout"


def test_a_shell_opens_in_the_repo_each_call_and_closes(world, repo, tmp_path, monkeypatch):
    from brindle import tmux

    opened, killed = [], []
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(tmux, "ensure_session", lambda *a: None)
    monkeypatch.setattr(tmux, "new_window", lambda session, name, cwd, argv, env, tag:
                        opened.append((cwd, argv, tag[1])) or f"%{len(opened)}")
    monkeypatch.setattr(tmux, "tagged_panes", lambda session, key: [
        (f"%{i + 1}", "shell", True, tid) for i, (_, _, tid) in enumerate(opened)])
    monkeypatch.setattr(tmux, "pane_window", lambda pane: "@" + pane[1:])
    monkeypatch.setattr(tmux, "kill_window", killed.append)
    code, first = _send("POST", "/v1/terminals/shell", {"cwd": str(repo)})
    code2, second = _send("POST", "/v1/terminals/shell", {"cwd": str(repo)})
    assert (code, code2, first["started"], second["started"]) == (200, 200, True, True)
    assert first["terminal"] != second["terminal"] and first["target"] == "%1"
    assert [(c, a) for c, a, _ in opened] == [(os.path.realpath(repo), ["/bin/zsh", "-l"])] * 2
    # Typed into in the app's own tmux client, never through the API.
    assert _send("POST", f"/v1/terminals/{first['terminal']}/input", {"text": "ls"})[0] == 404
    assert _send("DELETE", f"/v1/terminals/{first['terminal']}")[0] == 200 and killed == ["@1"]
    assert _send("DELETE", "/v1/terminals/t-0000000000000000")[0] == 404
    # Refused outside attached repos, and without a cwd.
    count = len(opened)
    assert _send("POST", "/v1/terminals/shell", {"cwd": str(tmp_path)})[0] == 400
    assert _send("POST", "/v1/terminals/shell", {})[0] == 400
    assert len(opened) == count


def test_deprecated_disable_preserves_goal_and_open_question(db, world):
    autopilot.set_goal(db, "sup", "Ship", [("Build", "true", None)])
    autopilot.need_user(db, "sup", "Which database?", ask=True)
    code, reply = _send("POST", "/v1/goal/enabled", {"root": "sup", "enabled": False})
    assert code == 200 and reply["enabled"] is True
    ap = db.get_autopilot("sup")
    assert ap.enabled and ap.goal == "Ship" and ap.state == "blocked" and ap.question


def test_a_clone_that_git_refuses_fails_the_job_with_its_message(db, cloning, tmp_path):
    code, _ = _send("POST", "/v1/repos/clone", {"url": "file:///nonexistent/gone.git", "parent_dir": str(tmp_path)})
    assert code == 202
    failed = bus.tail(db, kinds=("job.failed",))
    assert failed and failed[0]["payload"]["error"]["code"] == "clone_failed"
    assert "gone.git" in failed[0]["payload"]["error"]["message"]


def test_user_settings_round_trip_validates_and_keeps_other_keys(db, world, monkeypatch, tmp_path):
    monkeypatch.delenv("BRINDLE_HOME", raising=False)
    monkeypatch.delenv("BRINDLE_AGENT_ID", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".brindle" / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"delegation": "fast"}))

    assert _send("GET", "/v1/user-settings") == (200, {"claude_permission_mode": "default",
                                                        "opencode_permission_mode": "ask", "visuals": "off",
                                                        "visuals_enabled": "on"})
    assert _send("PATCH", "/v1/user-settings", {"claude_permission_mode": "yolo"})[0] == 400
    assert _send("PATCH", "/v1/user-settings", {})[0] == 400
    # One bad value refuses the whole change.
    assert _send("PATCH", "/v1/user-settings", {"claude_permission_mode": "auto",
                                                "opencode_permission_mode": "default"})[0] == 400
    assert _send("PATCH", "/v1/user-settings", {"claude_permission_mode": "auto"})[0] == 200
    assert _send("PATCH", "/v1/user-settings", {"opencode_permission_mode": "autonomous"})[0] == 200
    assert _send("GET", "/v1/user-settings") == (200, {"claude_permission_mode": "auto",
                                                        "opencode_permission_mode": "autonomous", "visuals": "off",
                                                        "visuals_enabled": "on"})
    assert json.loads(path.read_text()) == {"delegation": "fast", "claude_permission_mode": "auto",
                                            "opencode_permission_mode": "autonomous"}
    assert not list(path.parent.glob("*.tmp"))


def test_session_diff_before_ready(db, repo, lead):
    code, reply = _send("GET", "/v1/sessions/lead/pr/readiness")
    assert code == 200 and not reply["ready"]        # nothing ahead of main yet
    assert _send("GET", "/v1/sessions/lead/pr/draft")[0] == 200
    code, d = _send("GET", "/v1/sessions/lead/diff")
    assert code == 200 and d["files"] == []


EVERY_CLI = ["claude", "codex", "antigravity", "opencode"]


@pytest.mark.parametrize("asked,state,answer,usable", [
    ("codex", {}, "codex", EVERY_CLI),
    # Fixable by the person: the preflight's items; else the reason. Both list what else would run.
    ("codex", {"missing": "codex"}, (409, "preflight"), ["claude", "antigravity", "opencode"]),
    ("codex", {"signed_out": ("codex",)}, (409, "preflight"), ["claude", "antigravity", "opencode"]),
    ("codex", {"allowed": ("claude", "opencode")}, (403, "forbidden"), ["claude", "opencode"]),
    ("codex", {"off": "codex"}, (409, "turned_off"), ["claude", "antigravity", "opencode"]),
    # No provider asked for: the remembered pick, else Claude, else the first usable one.
    (None, {"pick": "antigravity"}, "antigravity", ["antigravity", "claude", "codex", "opencode"]),
    (None, {"signed_out": ("claude", "antigravity", "opencode")}, "codex", ["codex"]),
    # The policy allows only a CLI that isn't installed: nothing starts, not the profile's Claude.
    (None, {"allowed": ("opencode",), "missing": "opencode"}, (409, "preflight"), []),
    # The policy allows no CLI at all: still a preflight, naming what stops each one.
    (None, {"allowed": ()}, (409, "preflight"), []),
], ids=["explicit-usable", "explicit-missing", "explicit-signed-out", "explicit-policy", "explicit-off",
        "default-pick", "default-only-codex", "default-none-by-policy", "default-none-allowed"])
def test_a_new_session_runs_on_a_usable_cli_or_starts_nothing(db, world, tmp_path, monkeypatch, asked, state,
                                                             answer, usable):
    from brindle import providers_install
    from brindle.pro import managed_models, team_policy
    from brindle.service import sessions as svc

    monkeypatch.setattr(routes_actions.git, "missing_user_identity", lambda path: [])
    _provider_setup(tmp_path, monkeypatch)
    bins = {"codex": "BRINDLE_CODEX_BIN", "opencode": "BRINDLE_OPENCODE_BIN"}
    if "missing" in state:
        monkeypatch.setenv(bins[state["missing"]], str(tmp_path / "not-installed"))
    monkeypatch.setattr(providers, "signed_out", lambda p, env=None: (
        f"{p} isn't signed in" if p in state.get("signed_out", ()) else None))
    monkeypatch.setattr(team_policy, "access", lambda repo: Access(org_id="acme", allowed_providers=state.get(
        "allowed")))
    monkeypatch.setattr(managed_models, "current", lambda repo: None)
    monkeypatch.setattr(providers_install, "turned_off", lambda p: p == state.get("off"))
    if "pick" in state:
        config.set_user(config.SUPERVISOR_KEY, state["pick"])
    started = []
    # Only the final launch (and the detached cull after it) is stubbed.
    monkeypatch.setattr(svc, "launch", lambda db_, ws, provider=None, **k: started.append(provider) or svc.Started(
        db_.get_agent("sup"), ws, True))
    monkeypatch.setattr(svc, "cull_detached", lambda root: None)
    body = {"repo": world.repo_root, **({"provider": asked} if asked else {})}
    assert _send("POST", "/v1/sessions", {**body, "name": 7})[0] == 400 and started == []  # before any launch
    got, reply = _send("POST", "/v1/sessions", {**body, "name": " Cache   work "})
    if isinstance(answer, str):
        assert (got, started, reply["name"], db.get_agent("sup").name) == (200, [answer], "Cache work", "Cache work")
    else:
        assert (got, started) == (answer[0], []), reply
        if answer[1] == "preflight":
            assert reply["preflight"] and reply["usable"] == usable, reply
        else:
            assert (reply["error"]["code"], reply["error"]["usable"]) == (answer[1], usable), reply
    _, choice = _send("GET", f"/v1/providers/usable?repo={world.repo_root}")
    assert choice == {"usable": usable, "default": usable[0] if usable else None}


def test_supervisor_and_reviewer_choices_and_ordered_fallbacks_are_used(db, repo, world, monkeypatch):
    from brindle import providers, tmux
    from brindle.daemon import routes_read
    from brindle.profiles import load_profile
    from brindle.service import sessions as svc

    # No tmux server to ask what a pane would inherit (what tmux.inherited_env returns
    # without one), and no branch tidying (a fetch) before each start.
    monkeypatch.setattr(tmux, "inherited_env", lambda session: dict(os.environ))
    monkeypatch.setattr(svc, "tidy_branch", lambda db_, ws: (ws, []))
    before = config._read_json(config.scope_paths(str(repo))["local"])
    code, reply = _send("PUT", "/v1/routing", {"repo": str(repo), "scope": "local", "kinds": {
        "supervisor": {"model": "nope"}}})
    assert code == 400 and config._read_json(config.scope_paths(str(repo))["local"]) == before   # nothing written

    agent_dir = repo / ".brindle" / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "chat-alt.md").write_text(
        "---\nname: chat-alt\nextends: developer\nprovider: codex\nmodel: gpt-test\neffort: high\n---\nHi.\n")
    code, reply = _send("PUT", "/v1/routing", {"repo": str(repo), "scope": "local", "kinds": {
        "supervisor": {"model": "chat-alt", "fallbacks": ["developer-gemini", "supervisor"]},
        "reviewer": {"model": "reviewer-opencode", "fallbacks": ["reviewer-codex", "reviewer-gemini"]}}})
    assert code == 200, reply
    cfg = config.load_repo_config(str(repo))
    kinds = routes_read._routing_kinds(db, cfg, str(repo))
    assert (kinds["supervisor"].model, kinds["supervisor"].fallbacks, kinds["supervisor"].locked) == (
        "chat-alt", ["developer-gemini", "supervisor"], True)
    assert kinds["reviewer"].fallbacks == ["reviewer-codex", "reviewer-gemini"]
    # Written to the scope asked for only.
    assert "supervisor_profile" not in config._read_json(config.scope_paths(str(repo))["repo"])

    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db_, agent, ws, **kw: launched.append(agent))
    monkeypatch.setattr(agents, "model_problem", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(svc, "cull_detached", lambda root: None)
    monkeypatch.setattr("brindle.policy.check_assign",
                        lambda cfg, root, name, *a: SimpleNamespace(allowed=name != "reviewer-codex", reason="no"))

    def unusable(dead):
        monkeypatch.setattr(providers, "unusable", lambda p, env=None: "down" if p in dead else None)
        monkeypatch.setattr(agents, "unusable", providers.unusable)

    # A new chat launches as the supervisor on the first choice that can run, with its model and effort.
    unusable([])
    svc.start(db, str(repo), autopilot=False)
    chat = launched[-1]
    got = agents._profile_for(db, chat, world)
    assert (chat.profile, chat.provider, got.provider, got.model, got.effort) == (
        "supervisor", "codex", "codex", "gpt-test", "high")
    unusable(["codex"])                       # then the next in order: its own provider's defaults
    svc.start(db, str(repo), autopilot=False)
    assert (launched[-1].provider, agents._profile_for(db, launched[-1], world).model) == (
        "antigravity", load_profile("developer-gemini", str(repo)).model)
    svc.start(db, str(repo), provider="claude", autopilot=False)       # asked for by name: kept
    assert agents._profile_for(db, launched[-1], world).model == load_profile("supervisor", str(repo)).model

    # A review walks the pick and then the fallbacks: an unavailable provider and a policy denial are skipped.
    unusable([])
    assert agents._review_profile(db, world, None, cfg) == "reviewer-opencode"
    unusable(["opencode"])
    assert agents._review_profile(db, world, None, cfg) == "reviewer-gemini"
    unusable(["opencode", "antigravity"])     # none left: the pick's own problem
    with pytest.raises(agents.AgentError, match="'reviewer-opencode' can't run: down"):
        agents._review_profile(db, world, None, cfg)
    with pytest.raises(agents.AgentError, match="can't run"):       # asked for by name: no substitute
        agents._review_profile(db, world, "reviewer-opencode", cfg)
