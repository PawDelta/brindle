"""A task queued behind another session's work cuts its branch from its own
session's branch when it starts, never from the other session's: waiting
only delays the start (2026-11-17: a queued task's branch carried another
session's private commits to public main)."""

import asyncio
import time
from pathlib import Path

import pytest

from conftest import sh
from fast_repo import build_repo, copy_tree, moved, no_screen_gap, scratch_db  # noqa: F401
from brindle import agents, mcp_server, workspaces
from brindle.db import Agent
from test_merge_order import fake_spawn


@pytest.fixture(autouse=True)
def no_real_spawn(monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)


@pytest.fixture(scope="module")
def two_session_template(tmp_path_factory):
    """The repo with two session worktrees, ``session-a`` and ``session-b``, made once."""
    base = tmp_path_factory.mktemp("two-session-template")
    repo = build_repo(base)
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text('{"pipeline": false, "review": false}')
    with scratch_db(tmp_path_factory.mktemp("two-session-template-home")) as db_:
        made = [workspaces.create(db_, str(repo), name, run_setup=False).workspace
                for name in ("session-a", "session-b")]
    return base, made


@pytest.mark.variant("end to end through real git: the started branch shares no commits with the other session's branch")
def test_dependent_task_branch_is_cut_from_own_session(db, tmp_path, two_session_template, monkeypatch):
    template, made = two_session_template
    repo = copy_tree(template, tmp_path)
    ws_a, ws_b = (moved(ws, template, tmp_path) for ws in made)
    for root, ws in (("rootA", ws_a), ("rootB", ws_b)):
        db.add_workspace(ws)
        db.add_agent(Agent(root, ws.id, "supervisor", "claude", None, "interactive", "processing",
                           "@0", None, time.time()))
    b_start = sh("git rev-parse session-b", repo)

    # Session A's worker commits work of its own.
    monkeypatch.setenv("BRINDLE_AGENT_ID", "rootA")
    out = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    a_worker = out.split("Started worker ", 1)[1].split()[0]
    a_ws = db.get_workspace(db.get_agent(a_worker).workspace_id)
    (Path(a_ws.path) / "private.py").write_text("research\n")
    sh("git add -A && git commit -qm private", Path(a_ws.path))
    a_commit = sh("git rev-parse HEAD", Path(a_ws.path))

    # Session B's task waits on it.
    monkeypatch.setenv("BRINDLE_AGENT_ID", "rootB")
    out = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=[a_worker]))
    assert out.startswith("Queued task"), out

    # A's work merges into A's session branch: B's task starts.
    monkeypatch.setenv("BRINDLE_AGENT_ID", "rootA")
    out = asyncio.run(mcp_server.merge_workspace(a_ws.id))
    assert out.startswith("Merged"), out
    [b_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-b"]
    assert b_task.state == "started"
    b_ws = db.get_workspace(db.get_agent(b_task.agent_id).workspace_id)
    assert b_ws.branch == "feat-b"

    # Its branch carries none of A's commits and starts from B's session branch.
    assert sh("git rev-list feat-b ^session-b", repo) == ""
    assert sh(f"git branch --contains {a_commit} --list feat-b", repo) == ""
    assert sh("git merge-base feat-b session-b", repo) == b_start
