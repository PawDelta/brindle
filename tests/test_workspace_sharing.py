"""Issue #53: one worktree path/branch must belong to one workspace row, and
removing a row must never delete a live worker's worktree."""

import dataclasses
import json
import os
import time
from pathlib import Path

import pytest

from brindle import agents, mcp_server, pipeline, workspaces
from brindle.db import Agent
from conftest import sh
from git_snapshots import fast_host_tools, repo, workspaces_once  # noqa: F401  (fixtures)


@pytest.fixture
def live(monkeypatch):
    """Agent ids that count as alive."""
    ids: set[str] = set()
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id in ids)
    return ids


def add_agent(db, ws, agent_id):
    db.add_agent(Agent(agent_id, ws.id, "developer", "claude", None, "assign", "processing",
                       "@1", None, time.time()))


def duplicate_row(db, ws, suffix="-2"):
    """The row issue #53 describes: same branch and path, another id."""
    twin = dataclasses.replace(
        ws, id=ws.id + suffix, name=ws.name + suffix,
        tmux_session=ws.tmux_session + suffix, port_base=ws.port_base + 10)
    db.add_workspace(twin)
    return twin


@pytest.fixture
def feat_x(db, repo, tmp_path, tmp_path_factory):
    """``workspaces.create(db, repo, "feat/x")``, built once and copied."""
    return workspaces_once("sharing:feat/x", db, tmp_path, tmp_path_factory,
                           lambda: (workspaces.create(db, str(repo), "feat/x").workspace,))[0]


# -- creation ---------------------------------------------------------------------


def test_assign_with_branch_of_live_worker_is_refused(db, repo, feat_x, live, monkeypatch):
    first = feat_x
    add_agent(db, first, "busy1")
    live.add("busy1")
    root = workspaces.adopt_root(db, str(repo))
    with pytest.raises(workspaces.WorkspaceError):
        agents.delegate(db, None, root, "developer", "task", "assign", branch="feat/x")


@pytest.fixture
def two_sessions(db, repo, monkeypatch):
    """supA and supB run in one checkout; a worker starts as done, with id w-<its session>."""
    root = workspaces.adopt_root(db, str(repo))
    for sup in ("supA", "supB"):
        db.add_agent(Agent(sup, root.id, "supervisor", "claude", None, "interactive", "idle",
                           f"%{sup}", None, time.time()))
    during_spawn = []

    def spawn(db_, ws_, profile, parent_id=None, **kw):
        for hook in during_spawn:
            hook()
        db_.add_agent(Agent(f"w-{parent_id}", ws_.id, profile, "claude", parent_id, "assign",
                            "done", "@2", None, time.time()))
        return db_.get_agent(f"w-{parent_id}")

    monkeypatch.setattr(agents, "spawn", spawn)
    return root, during_spawn


@pytest.mark.parametrize("prefix, branch, task, made", [
    pytest.param("", "feat/shared", "task", "feat/shared", marks=pytest.mark.variant("Branch claim precedes the first worker row")),
    # Generated names: the same hint both times, and a task long enough to truncate.
    pytest.param("", None, "a" * 120, f"brindle/developer/{'a' * 40}-abcd", marks=pytest.mark.variant("Identically truncated generated names must stay isolated")),
    pytest.param("team/", "feat/shared", "task", "team/feat/shared",
                 marks=pytest.mark.variant("bypass: the prefix create adds hid the owner")),
    pytest.param("", "x" * 95 + ".lock-more", "task", "x" * 95,
                 marks=pytest.mark.variant("bypass: a second sanitize changed the truncated name")),
])
def test_concurrent_sessions_keep_their_worker_branches_apart(db, repo, live, two_sessions, monkeypatch,
                                                              prefix, branch, task, made):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({"branch_prefix": prefix}))
    monkeypatch.setattr(agents, "new_id", lambda: "abcdef01")
    root, during_spawn = two_sessions
    refused = []

    def b_while_a_starts():   # supA's worktree exists, its worker's row doesn't yet
        during_spawn.clear()
        with pytest.raises(agents.AgentError, match="right now") as e:
            agents.delegate(db, db.get_agent("supB"), root, "developer", task, "assign", branch=branch)
        refused.append(e)

    during_spawn.append(b_while_a_starts)
    _, a_ws = agents.delegate(db, db.get_agent("supA"), root, "developer", task, "assign", branch=branch)
    assert refused and a_ws.branch == made
    # supA's worker is finished, but its worktree still holds supA's unmerged work.
    with pytest.raises(agents.AgentError, match="supA"):
        agents.delegate(db, db.get_agent("supB"), root, "developer", task, "assign", branch=branch)
    assert [w.id for w in db.list_agents(a_ws.id)] == ["w-supA"]


@pytest.mark.variant("the task's merge requirement: two sessions' workers both land in the shared branch")
def test_concurrent_sessions_merge_their_workers_into_the_checkout(db, repo, live, two_sessions):
    root, _ = two_sessions
    spaces = {}
    for sup in ("supA", "supB"):
        _, ws = agents.delegate(db, db.get_agent(sup), root, "developer", f"task {sup}", "assign")
        (Path(ws.path) / f"{sup}.py").write_text("x = 1\n")
        sh(f"git add {sup}.py && git commit -qm {sup}", Path(ws.path))
        spaces[sup] = ws
    assert spaces["supA"].branch != spaces["supB"].branch
    for sup, ws in spaces.items():
        db.add_review(ws.id, sh("git rev-parse HEAD", ws.path), None, True, "approved")
        assert pipeline.merge(db, db.get_agent(sup), ws).startswith("Merged"), sup
    assert {"supA.py", "supB.py"} <= set(sh("git ls-tree --name-only HEAD", repo).split())


# -- removal ----------------------------------------------------------------------


def test_remove_refuses_while_another_live_row_shares_the_path(db, feat_x, live):
    ws = feat_x
    twin = duplicate_row(db, ws)
    add_agent(db, twin, "busy1")
    live.add("busy1")
    with pytest.raises(workspaces.WorkspaceError, match="live worker"):
        workspaces.remove(db, ws)
    assert os.path.isdir(ws.path) and db.get_workspace(ws.id)

    live.clear()
    db.set_status("busy1", "paused")
    workspaces.remove(db, ws)
    assert db.get_workspace(ws.id) is None
    assert db.get_workspace(twin.id) is not None
    assert os.path.isdir(twin.path)


def test_mcp_remove_workspace_refuses_then_force_removes(db, feat_x, live, monkeypatch):
    ws = feat_x
    twin = duplicate_row(db, ws)
    add_agent(db, twin, "busy1")
    live.add("busy1")
    out = mcp_server.remove_workspace(ws.id)
    assert out.startswith("Not removed") and "live worker" in out
    assert os.path.isdir(ws.path) and db.get_workspace(ws.id)
    out = mcp_server.remove_workspace(ws.id, force=True)
    assert out.startswith("Removed")
    assert not os.path.isdir(ws.path)


