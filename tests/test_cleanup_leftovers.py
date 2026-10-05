"""Regression coverage for stale copse workspaces and task claims."""

import time
from pathlib import Path

from copse import cull, tasks, workspaces
from copse.db import Agent


def _agent(db, ws, agent_id, *, status="paused", result=None, old=True):
    created = time.time() - 7200 if old else time.time()
    db.add_agent(Agent(agent_id, ws.id, "developer", "shell", None, "assign", status,
                       "", result, created))
    return db.get_agent(agent_id)


def _commit(path, repo, name):
    from conftest import sh

    (path / name).write_text("value = 1\n")
    sh("git add -A && git commit -qm work", path)


def test_prune_drops_missing_workspace_rows(db, repo):
    ws = workspaces.create(db, str(repo), "feat-missing", run_setup=False).workspace
    import shutil

    shutil.rmtree(ws.path)

    lines = cull.prune_retired(db)

    assert any("missing workspace" in line for line in lines)
    assert db.get_workspace(ws.id) is None


def test_prune_closes_old_paused_agent_on_merged_workspace(db, repo, monkeypatch):
    base = "supervisor"
    from conftest import sh

    sh(f"git switch -c {base}", repo)
    ws = workspaces.create(db, str(repo), "feat-supervisor-base", base=base,
                           run_setup=False).workspace
    _commit(Path(ws.path), repo, "change.py")
    workspaces.merge_back(db, ws)
    agent = _agent(db, ws, "old-paused")
    monkeypatch.setattr("copse.cull.tmux.list_panes", lambda: {})

    lines = cull.prune_stale_agents(db, now=time.time())

    assert any(agent.id in line and "merged" in line for line in lines)
    assert db.get_agent(agent.id).dismissed_at is not None


def _merged_workspace(db, repo, name):
    from conftest import sh

    sh("git switch -c supervisor", repo)
    ws = workspaces.create(db, str(repo), name, base="supervisor", run_setup=False).workspace
    _commit(Path(ws.path), repo, "change.py")
    workspaces.merge_back(db, ws)
    return ws


def test_prune_leaves_an_idle_agent_with_a_live_pane(db, repo, monkeypatch):
    """Idle at its prompt is not stopped: a supervisor between requests on an
    old merged worktree must not be closed (and its process stopped)."""
    ws = _merged_workspace(db, repo, "feat-live-idle")
    agent = _agent(db, ws, "live-idle", status="idle")
    monkeypatch.setattr("copse.cull.tmux.list_panes", lambda: {})
    monkeypatch.setattr("copse.cull.agents.is_alive", lambda a, panes=None: True)
    closed = []
    monkeypatch.setattr("copse.cull.agents.close", lambda db, aid, panes=None: closed.append(aid))

    lines = cull.prune_stale_agents(db, now=time.time())

    assert closed == [] and not any(agent.id in line for line in lines)


def test_prune_closes_an_idle_agent_whose_process_exited(db, repo, monkeypatch):
    ws = _merged_workspace(db, repo, "feat-dead-idle")
    agent = _agent(db, ws, "dead-idle", status="idle")
    monkeypatch.setattr("copse.cull.tmux.list_panes", lambda: {})
    monkeypatch.setattr("copse.cull.agents.is_alive", lambda a, panes=None: False)

    lines = cull.prune_stale_agents(db, now=time.time())

    assert any(agent.id in line and "exited" in line for line in lines)
    assert db.get_agent(agent.id).dismissed_at is not None


def test_prune_removes_worktree_merged_into_supervisor_branch(db, repo):
    from conftest import sh

    sh("git switch -c supervisor", repo)
    ws = workspaces.create(db, str(repo), "feat-supervisor-merged", base="supervisor",
                           run_setup=False).workspace
    _commit(Path(ws.path), repo, "change.py")
    workspaces.merge_back(db, ws)
    _agent(db, ws, "finished", status="done", result="done")

    lines = cull.prune_retired(db)

    assert any("removed merged worktree" in line for line in lines)
    assert db.get_workspace(ws.id) is None


def test_overlap_ignores_paused_and_exited_workers(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    other_ws = workspaces.create(db, str(repo), "feat-claim", run_setup=False).workspace
    _agent(db, other_ws, "paused-claim", status="paused")
    exited_ws = workspaces.create(db, str(repo), "feat-exited-claim", run_setup=False).workspace
    _agent(db, exited_ws, "exited-claim", status="exited")
    db.add_task(tasks.Task("paused-task", str(repo), "paused-claim", None, ws.id,
                           "developer", "work", "assign", 1, other_ws.branch, None,
                           '["src/shared.py"]', None, "started", time.time()))
    db.add_task(tasks.Task("exited-task", str(repo), "exited-claim", None, ws.id,
                           "developer", "work", "assign", 1, exited_ws.branch, None,
                           '["src/shared.py"]', None, "started", time.time()))

    assert tasks.overlap_warning(db, ws, ["src/shared.py"]) is None


def test_overlap_keeps_running_workers_claims(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    other_ws = workspaces.create(db, str(repo), "feat-running-claim", run_setup=False).workspace
    _agent(db, other_ws, "running-claim", status="processing")
    db.add_task(tasks.Task("running-task", str(repo), "running-claim", None, ws.id,
                           "developer", "work", "assign", 1, other_ws.branch, None,
                           '["src/shared.py"]', None, "started", time.time()))

    warning = tasks.overlap_warning(db, ws, ["src/shared.py"])

    assert warning and "running-claim" in warning
