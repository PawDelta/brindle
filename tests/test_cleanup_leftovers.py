"""Regression coverage for stale brindle workspaces and task claims."""

import time
from pathlib import Path

import pytest

from brindle import cull, tasks, workspaces
from brindle.db import Agent


def _agent(db, ws, agent_id, *, status="paused", result=None, old=True):
    created = time.time() - 7200 if old else time.time()
    db.add_agent(Agent(agent_id, ws.id, "developer", "shell", None, "assign", status,
                       "", result, created))
    return db.get_agent(agent_id)


def _commit(path, repo, name):
    from conftest import sh

    (path / name).write_text("value = 1\n")
    sh("git add -A && git commit -qm work", path)


def _merged_workspace(db, repo, name):
    from conftest import sh

    sh("git switch -c supervisor", repo)
    ws = workspaces.create(db, str(repo), name, base="supervisor", run_setup=False).workspace
    _commit(Path(ws.path), repo, "change.py")
    workspaces.merge_back(db, ws)
    return ws


def test_prune_closes_an_idle_agent_whose_process_exited(db, repo, monkeypatch):
    ws = _merged_workspace(db, repo, "feat-dead-idle")
    agent = _agent(db, ws, "dead-idle", status="idle")
    monkeypatch.setattr("brindle.cull.tmux.list_panes", lambda: {})
    monkeypatch.setattr("brindle.cull.agents.is_alive", lambda a, panes=None: False)

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


@pytest.mark.parametrize("status, claimed", [
    pytest.param("paused", False, marks=pytest.mark.variant("A paused worker's stale file claim must not block new work")),
    pytest.param("exited", False, marks=pytest.mark.variant("An exited worker's stale file claim must not block new work")),
    pytest.param("processing", True, marks=pytest.mark.variant("An active worker's file claim must remain visible when stale claims are ignored")),
])
def test_overlap_respects_worker_state(db, repo, status, claimed):
    ws = workspaces.adopt_root(db, str(repo))
    other_ws = workspaces.create(db, str(repo), "feat-running-claim", run_setup=False).workspace
    _agent(db, other_ws, "running-claim", status=status)
    db.add_task(tasks.Task("running-task", str(repo), "running-claim", None, ws.id,
                           "developer", "work", "assign", 1, other_ws.branch, None,
                           '["src/shared.py"]', None, "started", time.time()))

    warning = tasks.overlap_warning(db, ws, ["src/shared.py"])

    if claimed:
        assert warning and "running-claim" in warning
    else:
        assert warning is None


