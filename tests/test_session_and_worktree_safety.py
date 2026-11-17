import shutil
from pathlib import Path

import pytest

from brindle import agents, cli, git, procs, tmux, workspaces
from brindle.db import Agent
from conftest import sh


def gut(path: Path, keep_git=False):
    """Delete everything in a worktree (its .git link too unless ``keep_git``)."""
    for f in path.iterdir():
        if keep_git and f.name == ".git":
            continue
        shutil.rmtree(f) if f.is_dir() else f.unlink()


def lose_git_keep_notes(path):
    gut(path)
    (path / "surviving_notes.txt").write_text("critical notes that must not be deleted")
    return ["surviving_notes.txt"]


def lose_git_keep_hidden(path):
    gut(path)
    (path / ".env").write_text("SECRET_KEY=keep_me\n")
    (path / ".claude").mkdir()
    (path / ".claude" / "config.json").write_text("{}")
    return [".env", ".claude/config.json"]


def untracked_work_in_a_gutted_tree(path):
    (path / "untracked_notes.txt").write_text("critical notes")
    (path / "README.md").write_text("edited readme")
    for f in path.glob("*.py"):
        f.unlink()
    return ["untracked_notes.txt"]


def staged_deletion_in_a_gutted_tree(path):
    (path / "extra.py").write_text("extra")
    sh("git add extra.py && git commit -m 'add extra' && git rm extra.py", path)
    (path / "app.py").unlink()
    return []


@pytest.mark.parametrize("damage, error", [
    (lose_git_keep_notes, "surviving files"),
    (untracked_work_in_a_gutted_tree, "uncommitted or untracked work"),
    (staged_deletion_in_a_gutted_tree, "uncommitted or untracked work"),
])
def test_a_damaged_worktree_with_work_in_it_is_never_wiped(db, repo, damage, error):
    path = Path(workspaces.create(db, str(repo), "feat/damaged", reuse_registered=True).workspace.path)
    kept = damage(path)
    with pytest.raises(workspaces.WorkspaceError, match=error) as refused:
        workspaces.create(db, str(repo), "feat/damaged", reuse_registered=True)
    assert "force" not in str(refused.value)
    for name in kept:
        assert (path / name).exists()
    if damage is staged_deletion_in_a_gutted_tree:
        assert "D  extra.py" in git.out(["status", "--porcelain"], str(path))


def broken_git_link(path):
    gut(path)
    (path / ".git").write_text("gitdir: /nowhere\n")


@pytest.fixture
def ended(monkeypatch):
    """Sessions the reuse ended (tmux faked)."""
    killed = []
    monkeypatch.setattr(workspaces, "_rescue_sidebar", lambda db, ws: None)
    monkeypatch.setattr(tmux, "kill_session", killed.append)
    return killed


@pytest.mark.parametrize("damage", [
    pytest.param(gut, marks=pytest.mark.variant("Repairing a fully wiped worktree must preserve its directory inode and live tmux session")),
    broken_git_link, shutil.rmtree,
])
def test_a_wiped_worktree_is_repaired_in_the_same_folder(db, repo, ended, damage):
    """Panes and the tmux server may sit in a wiped worktree: it is filled
    again in place (same inode, session kept); only a folder that is gone
    is added afresh."""
    ws = workspaces.create(db, str(repo), "feat/wiped", reuse_registered=True).workspace
    inode = Path(ws.path).stat().st_ino
    damage(Path(ws.path))
    reused = workspaces.create(db, str(repo), "feat/wiped", reuse_registered=True)
    assert reused.workspace.id == ws.id
    assert (Path(ws.path) / ".git").exists() and (Path(ws.path) / "app.py").exists()
    if damage is not shutil.rmtree:
        assert Path(ws.path).stat().st_ino == inode and ended == []
    else:
        assert ended == [ws.tmux_session]   # a folder added afresh ends the session that sat in the old one


def test_a_wiped_worktree_something_lives_in_is_never_replaced(db, repo, ended, monkeypatch):
    """When it can't be repaired in place the folder would be replaced:
    refused while a process (fake table) sits in it."""
    ws = workspaces.create(db, str(repo), "feat/held", reuse_registered=True).workspace
    gut(Path(ws.path))
    real_add = git.add_worktree

    def add(root, path, branch, start):
        if Path(path).exists():
            raise git.GitError(f"'{path}' already exists")
        return real_add(root, path, branch, start)

    monkeypatch.setattr(git, "add_worktree", add)
    monkeypatch.setattr(procs, "table", lambda: {})
    monkeypatch.setattr(procs, "cwds", lambda *_: {4242: str(Path(ws.path).resolve())})
    monkeypatch.setattr(tmux, "session_panes", lambda name: [])
    monkeypatch.setattr(tmux, "list_sessions", lambda: [])
    with pytest.raises(workspaces.WorkspaceError, match="in use") as refused:
        workspaces.create(db, str(repo), "feat/held", reuse_registered=True)
    assert "force" not in str(refused.value)
    assert Path(ws.path).is_dir() and ended == []


@pytest.mark.variant("Failed git and filesystem removal must preserve the workspace's database record")
def test_remove_workspace_refuses_to_forget_when_directory_remains(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/failed-rm", reuse_registered=True).workspace

    def fail_remove(*args, **kwargs):
        raise git.GitError("git worktree remove failed")

    monkeypatch.setattr(git, "remove_worktree", fail_remove)
    monkeypatch.setattr(shutil, "rmtree", lambda *args, **kwargs: None)
    with pytest.raises(workspaces.WorkspaceError, match="could not remove worktree directory"):
        workspaces.remove(db, ws, force=True, delete_branch=False, keep_session=True)
    assert db.get_workspace(ws.id) is not None


@pytest.fixture
def started(repo, monkeypatch):
    """`brindle start` with nothing launched: records (branch, profile, autopilot) per spawn."""
    calls = []

    def fake_spawn(db_, ws_, profile, **kwargs):
        calls.append((ws_.branch, profile, kwargs.get("autopilot")))
        return Agent("a1", ws_.id, profile, "claude", None, "interactive", "starting", "w1", None, 0.0)

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(git, "require_user_identity", lambda path: None)
    for name in ("_attach", "_cull_detached", "_local_models_detached", "_settings_sync_detached",
                 "_preflight", "_trust_codex_hook"):
        monkeypatch.setattr(cli, name, lambda *a, **kw: None)
    monkeypatch.chdir(repo)
    return calls


@pytest.mark.parametrize("dirty", [False, True])
def test_start_on_a_merged_branch_switches_to_main_only_when_clean(db, repo, started, dirty):
    sh("git checkout -b feat/merged && echo x > feature.txt && git add feature.txt && git commit -qm feat"
       " && git checkout -q main && git merge -q feat/merged && git checkout -q feat/merged", repo)
    if dirty:
        (repo / "uncommitted.txt").write_text("uncommitted work")
    cli.start(prompt=None, provider=None, attach=False, watch=False, autopilot=None,
              branch=None, worktree=None)
    expected = "feat/merged" if dirty else "main"
    assert git.current_branch(str(repo)) == expected
    assert [branch for branch, _, _ in started] == [expected]


@pytest.mark.parametrize("local_commit", [False, True])
def test_start_on_main_fast_forwards_to_origin_and_never_drops_local_work(
        db, repo, started, tmp_path, local_commit):
    other = tmp_path / "other"
    sh(f"git clone -q {tmp_path / 'origin.git'} other && cd other && echo y > pushed.txt"
       " && git add pushed.txt && git commit -qm pushed && git push -q origin main", tmp_path)
    pushed = git.out(["rev-parse", "HEAD"], other)
    (repo / "notes.txt").write_text("my notes")
    if local_commit:
        sh("echo z > local.txt && git add local.txt && git commit -qm local", repo)
    local = git.out(["rev-parse", "HEAD"], repo)
    cli.start(prompt=None, provider=None, attach=False, watch=False, autopilot=None,
              branch=None, worktree=None)
    assert git.out(["rev-parse", "HEAD"], repo) == (local if local_commit else pushed)
    assert (repo / "notes.txt").read_text() == "my notes"


def test_squash_merged_branch_detected_after_main_advances(tmp_path):
    repo = tmp_path / "no-origin"   # no origin/HEAD: the default branch is found locally
    repo.mkdir()
    sh("git init -q -b main && git config user.name T && git config user.email t@test.invalid"
       " && echo base > a.txt && git add . && git commit -qm base", repo)
    sh("git checkout -q -b feature && echo feature > a.txt && git add . && git commit -qm one"
       " && echo two > b.txt && git add . && git commit -qm two && git checkout -q main"
       " && git merge -q --squash feature && git commit -qm squash", repo)
    assert git.merged_into_default(str(repo), "feature") is True
    sh("echo improved > a.txt && git add . && git commit -qm 'improvement on main'", repo)
    assert git.merged_into_default(str(repo), "feature") is True
