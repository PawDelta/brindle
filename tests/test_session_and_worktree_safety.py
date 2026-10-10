import json
import os
import shutil
from pathlib import Path

import pytest

from brindle import git, workspaces
from brindle.config import load_repo_config
from conftest import sh


def test_gutted_worktree_detected_and_recreated(db, repo, tmp_path):
    created = workspaces.create(db, str(repo), "feat/husk", reuse_registered=True)
    ws = created.workspace
    assert os.path.isdir(ws.path)
    # Simulate a gutted worktree: remove most files or remove .git
    for f in list(Path(ws.path).glob("*")):
        if f.is_file():
            f.unlink()
    # Now call create with reuse_registered=True again
    reused = workspaces.create(db, str(repo), "feat/husk", reuse_registered=True)
    assert reused.workspace.id == ws.id
    # The worktree should have been recreated with files intact
    assert (Path(reused.workspace.path) / "README.md").exists() or (Path(reused.workspace.path) / ".git").exists()


def test_damaged_worktree_with_uncommitted_work_raises(db, repo):
    created = workspaces.create(db, str(repo), "feat/precious", reuse_registered=True)
    ws = created.workspace
    assert os.path.isdir(ws.path)
    # Add an untracked file and an edit
    (Path(ws.path) / "untracked_notes.txt").write_text("critical notes")
    (Path(ws.path) / "README.md").write_text("edited readme")
    # Simulate partial deletion of another tracked file to make it damaged
    tracked_files = [f for f in Path(ws.path).glob("*.py")]
    for tf in tracked_files:
        tf.unlink()

    # Reusing registered workspace must NOT silently wipe untracked_notes.txt or edited readme
    with pytest.raises(workspaces.WorkspaceError) as exc_info:
        workspaces.create(db, str(repo), "feat/precious", reuse_registered=True)
    assert "uncommitted or untracked work" in str(exc_info.value)
    assert (Path(ws.path) / "untracked_notes.txt").exists()


def test_start_on_merged_branch_detects_merge(repo):
    # Create a feature branch and merge it into main
    sh("git checkout -b feat/merged", repo)
    (repo / "feature.txt").write_text("done")
    sh("git add feature.txt && git commit -m 'feat done'", repo)
    sh("git checkout main && git merge feat/merged", repo)
    sh("git checkout feat/merged", repo)
    assert git.merged_into_default(str(repo), "feat/merged")
    assert not git.merged_into_default(str(repo), "main")


def test_start_on_squash_merged_branch_detects_squash(repo):
    # Create a feature branch with multiple commits and squash merge into main
    sh("git checkout -b feat/multi-squash", repo)
    (repo / "step1.txt").write_text("step 1")
    sh("git add step1.txt && git commit -m 'step 1'", repo)
    (repo / "step2.txt").write_text("step 2")
    sh("git add step2.txt && git commit -m 'step 2'", repo)
    sh("git checkout main", repo)
    sh("git merge --squash feat/multi-squash && git commit -m 'squashed feat'", repo)
    sh("git checkout feat/multi-squash", repo)
    assert git.merged_into_default(str(repo), "feat/multi-squash")


def test_interactive_session_does_not_default_to_supervisor(db, repo, monkeypatch):
    from brindle import cli, agents
    # When starting in an existing repo without specifying --agent, it must use default_agent (developer)
    spawned_profiles = []
    def fake_spawn(db_arg, ws_arg, agent_arg, **kwargs):
        spawned_profiles.append((agent_arg, kwargs.get("autopilot")))
        from unittest.mock import MagicMock
        m = MagicMock()
        m.id = "agent-123"
        m.profile = agent_arg
        m.tmux_window = "w1"
        return m

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(cli, "_attach", lambda ws, win: None)
    monkeypatch.setattr(cli, "_cull_detached", lambda root: None)
    monkeypatch.setattr(cli, "_local_models_detached", lambda root: None)
    monkeypatch.setattr(cli, "_settings_sync_detached", lambda: None)
    monkeypatch.setattr(cli, "_pause_running", lambda db, ws, stop_procs=False: None)
    monkeypatch.chdir(repo)

    # Calling start with agent=None (default behavior)
    cli.start(agent=None, prompt=None, provider=None, attach=False, watch=False, autopilot=None, branch=None, worktree=None)
    assert len(spawned_profiles) == 1
    profile, autopilot = spawned_profiles[0]
    assert profile == "developer"
    assert autopilot is False
