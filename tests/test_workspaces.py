import json
import os
import sys

import pytest

import brindle
from brindle import git, providers, workspaces
from brindle.config import load_repo_config

from conftest import sh
from test_merge_order import add


def write_config(repo, **cfg):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(cfg))


def test_failed_setup_keeps_workspace(db, repo):
    write_config(repo, setup=["echo before", "false", "echo never"])
    created = workspaces.create(db, str(repo), "broken")
    assert created.setup and not created.setup.ok
    assert "never" not in created.setup.log
    assert db.get_workspace(created.workspace.id)


@pytest.mark.parametrize("configured", [None, "trees", "../elsewhere"])
def test_new_worktrees_go_under_the_repo_and_stay_out_of_git_status(db, repo, configured):
    if configured:
        write_config(repo, worktree_root=configured)
    ws = workspaces.create(db, str(repo), "feat/x").workspace
    root = configured or ".brindle/worktrees"
    assert os.path.realpath(ws.path) == os.path.realpath(repo / root / "feat" / "x")
    assert "feat" not in sh("git status --porcelain --untracked-files=all", repo)
    # Still one of brindle's own worktrees (trusted), unlike the main checkout.
    assert providers._in_worktree(ws.path) and not providers._in_worktree(str(repo / "app.py"))


def test_local_config_wraps_shared(repo):
    write_config(repo, setup=["b"])
    (repo / ".brindle" / "config.local.json").write_text(json.dumps({"setup": {"before": ["a"], "after": ["c"]}, "branch_prefix": "me/"}))
    cfg = load_repo_config(repo)
    assert cfg.setup == ["a", "b", "c"]
    assert cfg.branch_prefix == "me/"


def test_sync_conflict_reports_files(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "app.py"), "w").write("mine\n")
    git.commit_all(ws.path, "mine")
    (repo / "app.py").write_text("theirs\n")
    sh("git add -A && git commit -qm theirs && git push -q", repo)
    with pytest.raises(git.GitError, match="app.py"):
        git.sync(ws.path, "main")


def test_remove_refuses_dirty_and_keeps_branch(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "wip.txt"), "w").write("wip")
    with pytest.raises(workspaces.WorkspaceError, match="uncommitted"):
        workspaces.remove(db, ws)
    git.commit_all(ws.path, "wip")

    removed = workspaces.remove(db, ws, delete_branch=True)
    # Unmerged: safe delete refuses, branch survives.
    assert not removed.branch_deleted
    assert git.branch_exists(str(repo), "feature")
    assert not os.path.exists(ws.path)
    assert db.get_workspace(ws.id) is None


@pytest.mark.variant("incident: 8 merged workspaces kept their rows after a reviewer's "
                     "`uv run` from the worktree")
def test_remove_forgets_the_row_when_brindle_ran_from_the_removed_worktree(db, repo, monkeypatch):
    # A reviewer ran brindle from the worktree under review (`uv run` there):
    # removing it took brindle's source with it, so a module not loaded yet
    # failed to import afterwards, leaving the row and its agents behind.
    ws = workspaces.create(db, str(repo), "feature").workspace
    add(db, ws, "rev0", "review", "reviewer")
    real = git.remove_worktree

    def remove_with_the_source(*a, **k):
        real(*a, **k)
        monkeypatch.delattr(brindle, "rewind", raising=False)
        monkeypatch.setitem(sys.modules, "brindle.rewind", None)

    monkeypatch.setattr(git, "remove_worktree", remove_with_the_source)
    workspaces.remove(db, ws)
    assert db.get_workspace(ws.id) is None
    assert db.get_agent("rev0") is None


def test_existing_branch_is_reused_and_names_dedupe(db, repo):
    sh("git branch existing", repo)
    created = workspaces.create(db, str(repo), "existing")
    assert created.how == "existing"
    workspaces.remove(db, created.workspace)
    with pytest.raises(workspaces.WorkspaceError, match="base branch"):
        workspaces.create(db, str(repo), "main")
