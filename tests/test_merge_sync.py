"""merge_workspace's pre-gate sync: bring a stale branch's local base in
before checks run, and turn conflicts and merge failures into readable
"Not merged: ..." replies instead of raising."""

import asyncio
import json
import os
import time

import pytest

from conftest import sh
from brindle import agents, git, mcp_server, tmux, workspaces
from brindle.db import Agent


def write_config(repo, **cfg):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(cfg))


@pytest.fixture
def boss(db, repo, monkeypatch):
    """A supervisor caller adopted on the main checkout, so mcp_server tools
    that need `_caller` resolve without touching the real process cwd."""
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                        "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return ws


def _advance_base(repo, filename="base.txt", content="b"):
    """Add a commit to the local base branch, without touching origin."""
    (repo / filename).write_text(content)
    sh(f"git add -A && git commit -qm {filename}", repo)


# -- workspaces.sync_with_base -----------------------------------------------


def test_sync_with_base_skips_a_dirty_worktree(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    _advance_base(repo)
    open(os.path.join(ws.path, "scratch.txt"), "w").write("wip")  # uncommitted

    result = workspaces.sync_with_base(ws)

    assert result.status == "skipped"
    assert not os.path.exists(os.path.join(ws.path, "base.txt"))


# -- merge_workspace tool -----------------------------------------------------


def test_merge_workspace_syncs_then_asks_for_a_new_review(db, repo, boss):
    write_config(repo, review=True)
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "f.txt"), "w").write("f")
    git.commit_all(ws.path, "feature work")
    _advance_base(repo)

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert "Not merged: synced" in out
    assert "feature" in out and "main" in out
    assert "request_review" in out
    # The branch itself was brought up to date even though nothing merged.
    assert os.path.exists(os.path.join(ws.path, "base.txt"))
    assert git.dirty_files(ws.path) == []
    assert not (repo / "f.txt").exists()  # main untouched


def test_merge_workspace_reports_non_conflict_sync_failure(db, repo, boss):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "f.txt"), "w").write("f")
    git.commit_all(ws.path, "feature work")
    _advance_base(repo)
    sh("git config merge.ff only", ws.path)

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Not merged: merging main failed")
    assert git.dirty_files(ws.path) == []
    assert not git.ok(["rev-parse", "-q", "--verify", "MERGE_HEAD"], ws.path)
    assert not (repo / "f.txt").exists()  # main untouched


CLAUDE_IDLE = "⏺ Done.\n\n────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"


def _stale_branch_with_worker(db, repo, mode="assign", result=None):
    """A branch behind main, with worker w1 marked processing in it."""
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "f.txt"), "w").write("f")
    git.commit_all(ws.path, "feature work")
    _advance_base(repo)
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", mode, "processing",
                        "@1", result, time.time()))
    return ws


def test_merge_workspace_ignores_worker_whose_window_is_dead(db, repo, boss):
    ws = _stale_branch_with_worker(db, repo)  # "@1" isn't a live window

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Merged feature into main")
    assert (repo / "f.txt").exists()


def test_merge_workspace_reconciles_a_stale_processing_worker(db, repo, boss, monkeypatch):
    ws = _stale_branch_with_worker(db, repo)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_IDLE)

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Merged feature into main")
    assert db.get_agent("w1").status == "idle"


def test_merge_workspace_up_to_date_branch_merges_as_before(db, repo, boss):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "new.py"), "w").write("x = 1\n")
    git.commit_all(ws.path, "work")

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Merged feature into main")
    assert (repo / "new.py").read_text() == "x = 1\n"


def test_merge_workspace_reports_giterror_from_merge_back(db, repo, boss, monkeypatch):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "new.py"), "w").write("x = 1\n")
    git.commit_all(ws.path, "work")

    def boom(*a, **k):
        raise git.GitError("boom")

    monkeypatch.setattr(workspaces, "merge_back", boom)
    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out == "Not merged: boom"


def test_merge_after_approval_dirty(db, repo, boss):
    """An untracked file left in the worktree after approval (check output,
    the reviewer's scratch) isn't a change to the branch: the gates ignore it
    and merge_back must too, instead of failing "uncommitted changes"."""
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "f.txt"), "w").write("f")
    git.commit_all(ws.path, "feature work")
    open(os.path.join(ws.path, "check-output.log"), "w").write("scratch")  # untracked

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Merged feature"), out
    assert (repo / "f.txt").exists()
