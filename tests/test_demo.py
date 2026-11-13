"""The demo on a brand new account: no git identity anywhere."""

import asyncio
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from brindle import agents, demo, workspaces
from brindle.db import Agent, Workspace


def _practice_session(db, root, name):
    (root / ".brindle").mkdir(parents=True)
    (root / ".brindle" / "goals.md").write_text("Practice")
    for suffix, parent in (("supervisor", None), ("worker", f"{name}-supervisor")):
        agent_id = f"{name}-{suffix}"
        path = root if parent is None else root / "worker"
        path.mkdir(exist_ok=True)
        db.add_workspace(Workspace(agent_id, str(root), agent_id, "root" if parent is None else "worktree",
                                   "main", None, str(path), None, "", 0))
        db.add_agent(Agent(agent_id, agent_id, suffix, "shell", parent, "supervisor" if parent is None else "assign",
                           "processing", "", None, 0))


def test_end_practice_run_stops_agents_and_removes_repo(db, brindle_home, tmp_path, monkeypatch):
    from brindle import tmux
    from brindle.daemon import routes_actions

    root = brindle_home / "demo" / "textkit-running"
    _practice_session(db, root, "practice")
    project = tmp_path / "project"
    _practice_session(db, project, "person")
    held = brindle_home / "demo" / "textkit-held"
    (held / ".brindle").mkdir(parents=True)
    (held / ".brindle" / "goals.md").write_text("Practice")
    stopped = set()

    def stop(db, agent):
        assert root.exists()
        stopped.add(agent.id)

    monkeypatch.setattr(tmux, "list_panes", lambda: {})
    monkeypatch.setattr(agents, "is_alive", lambda *a: True)
    monkeypatch.setattr(agents, "owns_pane", lambda *a: True)
    monkeypatch.setattr(agents, "_stop", stop)
    got = routes_actions.cancel_demo(db, {}, {})
    assert set(got["removed"]) == {str(root), str(held)}
    assert stopped == {"practice-supervisor", "practice-worker"}
    assert not root.exists() and not held.exists()
    assert db.find_workspaces(str(root)) == []
    assert {a.id for a in db.list_agents()} == {"person-supervisor", "person-worker"}
    assert project.exists()
    assert routes_actions.cancel_demo(db, {}, {}) == {"removed": []}


@pytest.mark.parametrize("case", ["outside", "changed_during_close", "attached_project"])
def test_end_practice_run_refuses_non_demo_repo(db, brindle_home, tmp_path, monkeypatch, case):
    from brindle import tmux

    project = tmp_path / "textkit-project"
    root = project if case == "outside" else brindle_home / "demo" / "textkit-running"
    _practice_session(db, root, "practice")
    if case == "attached_project":
        _practice_session(db, project, "person")
        db.update_agent("person-supervisor", parent_id="practice-supervisor")
    stopped = []

    def stop(db, agent):
        stopped.append(agent.id)
        if agent.parent_id is not None:
            # Swap the candidate after stopping, before its directory is removed.
            root.rename(project)
            root.symlink_to(project, target_is_directory=True)

    monkeypatch.setattr(tmux, "list_panes", lambda: {})
    monkeypatch.setattr(agents, "is_alive", lambda *a: True)
    monkeypatch.setattr(agents, "owns_pane", lambda *a: True)
    monkeypatch.setattr(agents, "_stop", stop)
    demo.discard(db, root)
    assert (project / ".brindle" / "goals.md").exists()
    assert db.get_workspace("practice-supervisor") is not None
    if case == "changed_during_close":
        assert root.is_symlink()
    else:
        assert stopped == []
        assert db.get_agent("practice-supervisor").status == "processing"


@pytest.mark.parametrize("change", [
    pytest.param("attached_after_validation", marks=pytest.mark.variant(
        "A personal subtree joining after validation must not enter the close dispatch.")),
    "moved_before_close",
])
def test_end_practice_run_rechecks_agents_after_tree_changes(db, brindle_home, tmp_path, monkeypatch, change):
    from brindle import tmux

    root = brindle_home / "demo" / "textkit-running"
    project = tmp_path / "project"
    _practice_session(db, root, "practice")
    _practice_session(db, project, "person")
    tree = agents.tree
    stopped = []

    def changed_tree(db, agent_id):
        snapshot = tree(db, agent_id)
        # A new assignment joins the tree after the validation snapshot was read.
        db.update_agent("person-supervisor", parent_id="practice-supervisor")
        return snapshot

    def stop(db, agent):
        stopped.append(agent.id)
        if change == "moved_before_close" and agent.id == "practice-supervisor":
            db.update_agent("practice-worker", workspace_id="person-worker")

    if change == "attached_after_validation":
        monkeypatch.setattr(agents, "tree", changed_tree)
    monkeypatch.setattr(tmux, "list_panes", lambda: {})
    monkeypatch.setattr(agents, "is_alive", lambda *a: True)
    monkeypatch.setattr(agents, "owns_pane", lambda *a: True)
    monkeypatch.setattr(agents, "_stop", stop)
    demo.discard(db, root)
    assert "practice-supervisor" in stopped
    for agent_id in ("person-supervisor", "person-worker"):
        agent = db.get_agent(agent_id)
        assert agent.status == "processing" and agent.dismissed_at is None
        assert agent_id not in stopped
    assert project.exists()
    if change == "moved_before_close":
        assert "practice-worker" not in stopped
        assert db.get_agent("practice-worker").status == "processing"
        assert root.exists()


def _no_identity(monkeypatch, tmp_path):
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "newhome"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _profile(env):
    return SimpleNamespace(env=env, tool_search=None, provider="shell", auth="auto")


def _worker_commit(db, ws, name):
    path = Path(ws.path)
    agent = Agent(f"w-{name}", ws.id, "developer", "shell", None, "assign", "idle", "", None, time.time())
    db.add_agent(agent)
    env = {**os.environ, **agents.agent_env(ws, agent.id, agent)}
    (path / name).write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "work"], cwd=path, check=True, env=env)


def _last(path, fmt="%an <%ae>"):
    return subprocess.run(["git", "log", "-1", f"--format={fmt}"], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


def _git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True)


def test_cli_demo_starts_workers_on_the_signed_in_provider(db, repo, monkeypatch):
    from brindle import autopilot, cli, config, providers

    monkeypatch.chdir(repo)
    monkeypatch.setattr(providers, "unusable", lambda provider, *a, **k:
                        None if provider == "codex" else "CLI not installed")
    for name in ("_preflight", "_trust_codex_hook", "_pause_transferred", "_check_updates",
                 "_cull_detached", "_local_models_detached", "_settings_sync_detached"):
        monkeypatch.setattr(cli, name, lambda *a, **k: None)
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: None)

    def launch(db, agent, ws, **kwargs):
        problem = providers.unusable(agent.provider)
        if problem:
            raise agents.AgentError(problem)
        db.update_agent(agent.id, status="processing")

    monkeypatch.setattr(agents, "_launch", launch)
    result = CliRunner().invoke(cli.app, ["demo", "--no-attach"])
    assert result.exit_code == 0, result.output
    supervisor, = db.list_agents()
    ws = db.get_workspace(supervisor.workspace_id)
    milestones = db.milestones(supervisor.id)
    assert len(milestones) == 2
    for name, milestone in zip(("slug", "wrap"), milestones):
        profile, _ = autopilot.choose_profile(db, supervisor.id, ws.repo_root,
                                              task=milestone.title, weight="light")
        agents.delegate(db, supervisor, ws, profile, milestone.title, "assign",
                        branch=name, done_when=milestone.check_cmd)
    workers = [a for a in db.list_agents() if a.parent_id == supervisor.id]
    assert len(workers) == 2
    assert all(a.status == "processing" and a.provider == supervisor.provider == "codex"
               for a in workers)
    assert len({a.workspace_id for a in workers}) == 2
    # The review at the end must also run on the only usable CLI.
    reviewer = config.load_repo_config(ws.repo_root).review_profile
    review = agents.spawn(db, db.get_workspace(workers[0].workspace_id), reviewer,
                          parent_id=supervisor.id, mode="review")
    assert db.get_agent(review.id).status == "processing"


def test_app_demo_starts_workers_and_reviewer(db, monkeypatch):
    """The app's 'Watch brindle work on a practice repo' on a fresh account
    with one CLI: POST /demo starts the supervisor, its assign for each
    milestone launches a worker, and the review runs, all on that CLI."""
    # The daemon's sign-in probe answered more than its TTL ago (or not yet):
    # demo_provider finds no CLI, and the demo must still run on the usable one.
    only = "codex"
    from brindle import config, doctor, mcp_server, providers
    from brindle.daemon import routes_actions

    monkeypatch.setattr(providers, "unusable", lambda provider, *a, **k:
                        None if provider == only else "CLI not installed")
    monkeypatch.setattr(doctor, "signed_in_providers", lambda: [])
    monkeypatch.setattr(agents, "model_problem", lambda *a, **k: None)
    # The machine's own CLI setup (is codex installed here?) is preflight's job, not this test's.
    monkeypatch.setattr(routes_actions, "session_blockers", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "preflight", lambda *a, **k: [])
    launched = []

    def launch(db, agent, ws, **kwargs):
        problem = providers.unusable(agent.provider)
        if problem:
            raise agents.AgentError(problem)
        launched.append(agent.mode)
        db.update_agent(agent.id, status="processing")

    monkeypatch.setattr(agents, "_launch", launch)
    got = routes_actions.start_demo(db, {}, {})
    assert isinstance(got, dict), got
    supervisor = db.get_agent(got["id"]) if "id" in got else db.list_agents()[0]
    assert supervisor.provider == only
    ws = db.get_workspace(supervisor.workspace_id)
    assert demo.is_demo_repo(ws.repo_root)
    milestones = db.milestones(supervisor.id)
    assert len(milestones) == 2

    # The supervisor delegates as the demo's goal tells it: each milestone to
    # its own worker, weight "light", through the real MCP tool.
    monkeypatch.setenv("BRINDLE_AGENT_ID", supervisor.id)
    for name, m in zip(("slug", "wrap"), milestones):
        asyncio.run(mcp_server.assign(task=m.title, weight="light", branch=name,
                                              done_when=m.check_cmd))
    workers = [a for a in db.list_agents() if a.parent_id == supervisor.id]
    assert len(workers) == 2
    assert all(a.status == "processing" and a.provider == only for a in workers)
    assert len({a.workspace_id for a in workers}) == 2

    reviewer = config.load_repo_config(ws.repo_root).review_profile
    review = agents.spawn(db, db.get_workspace(workers[0].workspace_id), reviewer,
                          parent_id=supervisor.id, mode="review")
    assert review.provider == only and db.get_agent(review.id).status == "processing"
    assert launched.count("review") == 1 and launched.count("assign") == 2


def test_demo_merges_with_no_git_identity(db, repo, monkeypatch, tmp_path):
    _no_identity(monkeypatch, tmp_path)
    monkeypatch.setattr(agents, "load_profile", lambda *a, **k: _profile({}))
    wss = [workspaces.create(db, str(repo), n, run_setup=False).workspace for n in "abcdef"]

    # No identity at all: the worker's commit and brindle's merge fall back,
    # and no config is written.
    _worker_commit(db, wss[0], "a.py")
    workspaces.merge_back(db, wss[0])
    config = subprocess.run(["git", "config", "--local", "--list"], cwd=repo, check=True,
                            capture_output=True, text=True).stdout
    assert "user.name" not in config and "user.email" not in config

    # Half an identity (a name, no email): that half is kept.
    _git(repo, "config", "user.name", "Existing Person")
    _worker_commit(db, wss[1], "b.py")
    assert _last(wss[1].path) == "Existing Person <brindle@localhost>"
    workspaces.merge_back(db, wss[1])
    assert _last(repo) == "Existing Person <brindle@localhost>"

    # A role-specific identity (author.*, with useConfigOnly): kept as the
    # author; the committer, which has none, falls back.
    _git(repo, "config", "--unset", "user.name")
    _git(repo, "config", "user.useConfigOnly", "true")
    _git(repo, "config", "author.name", "Role Author")
    _git(repo, "config", "author.email", "role@example.com")
    _worker_commit(db, wss[2], "c.py")
    assert _last(wss[2].path, "%an <%ae> / %cn <%ce>") == \
        "Role Author <role@example.com> / brindle <brindle@localhost>"
    workspaces.merge_back(db, wss[2])

    # An identity from the worker's profile (GIT_* env lines) is not overwritten.
    _git(repo, "config", "--unset", "author.name")
    _git(repo, "config", "--unset", "author.email")
    who = {f"GIT_{r}_{f}": v for r in ("AUTHOR", "COMMITTER")
           for f, v in (("NAME", "Profile Person"), ("EMAIL", "profile@example.com"))}
    monkeypatch.setattr(agents, "load_profile", lambda *a, **k: _profile(who))
    _worker_commit(db, wss[3], "d.py")
    assert _last(wss[3].path) == "Profile Person <profile@example.com>"

    # Git settings chosen by the profile decide what counts as set: an identity
    # passed as GIT_CONFIG_PARAMETERS is kept ...
    params = "'user.name=Config Person' 'user.email=config@example.com'"
    monkeypatch.setattr(agents, "load_profile",
                        lambda *a, **k: _profile({"GIT_CONFIG_PARAMETERS": params}))
    _worker_commit(db, wss[4], "g.py")
    assert _last(wss[4].path) == "Config Person <config@example.com>"
    # ... and an identity only in the daemon's global config is not seen by a
    # worker whose profile points git elsewhere, so that worker gets the fallback.
    theirs = tmp_path / "theirs.gitconfig"
    _git(repo, "config", "--file", str(theirs), "user.name", "Daemon Person")
    _git(repo, "config", "--file", str(theirs), "user.email", "daemon@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(theirs))
    monkeypatch.setattr(agents, "load_profile",
                        lambda *a, **k: _profile({"GIT_CONFIG_GLOBAL": "/dev/null"}))
    _worker_commit(db, wss[5], "h.py")
    assert _last(wss[5].path) == "brindle <brindle@localhost>"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")

    # The demo repo: workers and merges both work with no identity.
    monkeypatch.setattr(agents, "load_profile", lambda *a, **k: _profile({}))
    root = demo.create()
    first = workspaces.create(db, str(root), "slug", base="main", run_setup=False).workspace
    second = workspaces.create(db, str(root), "wrap", base="main", run_setup=False).workspace
    _worker_commit(db, first, "e.py")
    _worker_commit(db, second, "f.py")
    workspaces.merge_back(db, first)
    workspaces.merge_back(db, second)    # main moved on: a real merge commit
    assert _last(root) == "brindle demo <demo@brindle.invalid>"
