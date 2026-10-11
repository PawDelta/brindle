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
    # The worktree should have been restored with tracked files intact
    assert (Path(reused.workspace.path) / "app.py").exists()


def test_damaged_worktree_missing_git_with_surviving_files_raises(db, repo):
    created = workspaces.create(db, str(repo), "feat/missing-git", reuse_registered=True)
    ws = created.workspace
    assert os.path.isdir(ws.path)
    # Remove .git file but leave surviving user notes
    git_file = Path(ws.path) / ".git"
    if git_file.is_file():
        git_file.unlink()
    elif git_file.is_dir():
        shutil.rmtree(git_file)
    (Path(ws.path) / "surviving_notes.txt").write_text("critical notes that must not be deleted")

    # Reusing registered workspace must fail closed and preserve surviving files
    with pytest.raises(workspaces.WorkspaceError) as exc_info:
        workspaces.create(db, str(repo), "feat/missing-git", reuse_registered=True)
    assert "surviving files" in str(exc_info.value)
    assert (Path(ws.path) / "surviving_notes.txt").exists()


def test_damaged_worktree_missing_git_with_hidden_files_and_dirs_raises(db, repo):
    created = workspaces.create(db, str(repo), "feat/hidden-git-loss", reuse_registered=True)
    ws = created.workspace
    assert os.path.isdir(ws.path)
    # Remove .git and all visible files, leaving only .env and .claude/
    git_file = Path(ws.path) / ".git"
    if git_file.is_file():
        git_file.unlink()
    elif git_file.is_dir():
        shutil.rmtree(git_file)
    for f in list(Path(ws.path).glob("*")):
        if f.is_file():
            f.unlink()
        elif f.is_dir():
            shutil.rmtree(f)
    (Path(ws.path) / ".env").write_text("SECRET_KEY=keep_me\n")
    hidden_dir = Path(ws.path) / ".claude"
    hidden_dir.mkdir()
    (hidden_dir / "config.json").write_text("{}")

    # Reusing registered workspace must refuse destructive recovery and preserve .env and .claude/
    with pytest.raises(workspaces.WorkspaceError) as exc_info:
        workspaces.create(db, str(repo), "feat/hidden-git-loss", reuse_registered=True)
    assert "surviving files" in str(exc_info.value)
    assert (Path(ws.path) / ".env").exists()
    assert (Path(ws.path) / ".claude" / "config.json").exists()


def test_damaged_worktree_missing_git_empty_recreated(db, repo):
    created = workspaces.create(db, str(repo), "feat/empty-broken", reuse_registered=True)
    ws = created.workspace
    assert os.path.isdir(ws.path)
    # Remove everything in ws.path so directory is empty
    for f in list(Path(ws.path).glob("*")):
        if f.is_file():
            f.unlink()
        elif f.is_dir():
            shutil.rmtree(f)
    git_file = Path(ws.path) / ".git"
    if git_file.exists():
        if git_file.is_file():
            git_file.unlink()
        else:
            shutil.rmtree(git_file)

    # Empty directory with broken/missing git should be safely pruned and recreated
    reused = workspaces.create(db, str(repo), "feat/empty-broken", reuse_registered=True)
    assert reused.workspace.id == ws.id
    assert (Path(reused.workspace.path) / ".git").exists()
    assert (Path(reused.workspace.path) / "app.py").exists()


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


def test_damaged_worktree_with_staged_deletion_raises(db, repo):
    created = workspaces.create(db, str(repo), "feat/staged-del", reuse_registered=True)
    ws = created.workspace
    assert os.path.isdir(ws.path)
    # Add a second tracked file
    (Path(ws.path) / "extra.py").write_text("extra")
    sh("git add extra.py && git commit -m 'add extra'", ws.path)
    # Stage a deletion of extra.py
    sh("git rm extra.py", ws.path)
    # Delete app.py without staging it so it appears damaged/gutted
    (Path(ws.path) / "app.py").unlink()

    # Reusing registered workspace must NOT silently discard the staged deletion
    with pytest.raises(workspaces.WorkspaceError) as exc_info:
        workspaces.create(db, str(repo), "feat/staged-del", reuse_registered=True)
    assert "uncommitted or untracked work" in str(exc_info.value)
    status_out = git.out(["status", "--porcelain"], ws.path)
    assert "D  extra.py" in status_out


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


def test_remote_only_squash_merge_detected(tmp_path):
    # Set up a bare remote with default branch main and clone it
    remote = tmp_path / "remote.git"
    remote.mkdir()
    sh("git init --bare -b main", remote)

    local = tmp_path / "local"
    sh(f"git clone {remote} {local}", tmp_path)
    sh("git checkout -b main", local)
    (local / "init.txt").write_text("initial")
    sh("git add init.txt && git commit -m 'initial' && git push origin main", local)

    # Create feature branch locally
    sh("git checkout -b feat/remote-squash", local)
    (local / "feature.txt").write_text("feature work")
    sh("git add feature.txt && git commit -m 'feat commit' && git push origin feat/remote-squash", local)

    # In another clone, squash merge feat/remote-squash into main and push to remote
    other = tmp_path / "other"
    sh(f"git clone {remote} {other}", tmp_path)
    sh("git checkout main && git merge --squash origin/feat/remote-squash", other)
    sh("git commit -m 'squashed feature' && git push origin main", other)

    # Now in local, main and origin/main are stale; merged_into_default must fetch origin
    # and detect the squash merge
    sh("git checkout feat/remote-squash", local)
    assert git.merged_into_default(str(local), "feat/remote-squash")


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
def test_scratch_implicit_start_defaults_to_developer(db, tmp_path, monkeypatch):
    from brindle import cli, agents
    # When starting outside a git repo (scratch directory) without --agent,
    # it must use default_agent (developer) and autopilot=False
    spawned_profiles = []
    def fake_spawn(db_arg, ws_arg, agent_arg, **kwargs):
        spawned_profiles.append((agent_arg, kwargs.get("autopilot")))
        from unittest.mock import MagicMock
        m = MagicMock()
        m.id = "agent-scratch-123"
        m.profile = agent_arg
        m.tmux_window = "w1"
        return m

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(cli, "_attach", lambda ws, win: None)
    monkeypatch.setattr(cli, "_cull_detached", lambda root: None)
    monkeypatch.setattr(cli, "_local_models_detached", lambda root: None)
    monkeypatch.setattr(cli, "_settings_sync_detached", lambda: None)
    monkeypatch.setattr(cli, "_pause_running", lambda db, ws, stop_procs=False: None)
    monkeypatch.setattr(cli, "_preflight", lambda agent, provider, root: None)
    monkeypatch.setattr(cli, "_trust_codex_hook", lambda root: None)
    monkeypatch.chdir(tmp_path)

    cli.start(agent=None, prompt=None, provider=None, attach=False, watch=False, autopilot=None, branch=None, worktree=None)
    assert len(spawned_profiles) == 1
    profile, autopilot = spawned_profiles[0]
    assert profile == "developer"
    assert autopilot is False
