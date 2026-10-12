import json
import os

import pytest

from brindle import git, workspaces
from brindle.config import load_repo_config

from conftest import sh


def write_config(repo, **cfg):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(cfg))


def test_failed_setup_keeps_workspace(db, repo):
    write_config(repo, setup=["echo before", "false", "echo never"])
    created = workspaces.create(db, str(repo), "broken")
    assert created.setup and not created.setup.ok
    assert "never" not in created.setup.log
    assert db.get_workspace(created.workspace.id)


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


def test_existing_branch_is_reused_and_names_dedupe(db, repo):
    sh("git branch existing", repo)
    created = workspaces.create(db, str(repo), "existing")
    assert created.how == "existing"
    workspaces.remove(db, created.workspace)
    with pytest.raises(workspaces.WorkspaceError, match="base branch"):
        workspaces.create(db, str(repo), "main")
