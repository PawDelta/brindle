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


def test_start_on_merged_branch_detects_merge(repo):
    # Create a feature branch and merge it into main
    sh("git checkout -b feat/merged", repo)
    (repo / "feature.txt").write_text("done")
    sh("git add feature.txt && git commit -m 'feat done'", repo)
    sh("git checkout main && git merge feat/merged", repo)
    sh("git checkout feat/merged", repo)
    assert git.merged_into_default(str(repo), "feat/merged")
    assert not git.merged_into_default(str(repo), "main")


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
