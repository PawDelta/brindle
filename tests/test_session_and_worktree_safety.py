import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from brindle import agents, cli, git, workspaces
from brindle.db import Agent
from brindle.service import sessions as svc
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
    (lose_git_keep_hidden, "surviving files"),
    (untracked_work_in_a_gutted_tree, "uncommitted or untracked work"),
    (staged_deletion_in_a_gutted_tree, "uncommitted or untracked work"),
])
def test_a_damaged_worktree_with_work_in_it_is_never_wiped(db, repo, damage, error):
    path = Path(workspaces.create(db, str(repo), "feat/damaged", reuse_registered=True).workspace.path)
    kept = damage(path)
    with pytest.raises(workspaces.WorkspaceError, match=error):
        workspaces.create(db, str(repo), "feat/damaged", reuse_registered=True)
    for name in kept:
        assert (path / name).exists()
    if damage is staged_deletion_in_a_gutted_tree:
        assert "D  extra.py" in git.out(["status", "--porcelain"], str(path))


def test_damaged_worktree_missing_git_empty_recreated(db, repo):
    ws = workspaces.create(db, str(repo), "feat/empty-broken", reuse_registered=True).workspace
    gut(Path(ws.path))
    reused = workspaces.create(db, str(repo), "feat/empty-broken", reuse_registered=True)
    assert reused.workspace.id == ws.id
    assert (Path(reused.workspace.path) / ".git").exists()
    assert (Path(reused.workspace.path) / "app.py").exists()


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


def test_concurrent_sessions_a_third_start_leaves_both_live(db, repo, started, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))

    def launch(aid, ws_=ws, *a, **kw):
        db.add_agent(Agent(aid, ws_.id, "supervisor", "claude", None, "interactive", "idle",
                           f"%{aid}", None, float(len(db.list_agents(ws.id)))))
        return db.get_agent(aid)

    launch("sup1")
    launch("sup2")
    monkeypatch.setattr(agents, "spawn", lambda db_, ws_, profile, **kw: launch("sup3", ws_))
    # Alive until paused: a pause anywhere in start shows up as a session gone.
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: db.get_agent(a.id).status != "paused")
    monkeypatch.setattr(agents, "pane_owners", lambda db_, panes=None: {})
    # No terminal to ask in: the new one starts here, alongside both.
    cli.start(prompt=None, provider=None, attach=False, watch=False, autopilot=None,
              branch=None, worktree=None)
    assert [a.id for a in svc.live_here(db, ws)] == ["sup3", "sup2", "sup1"]


def test_bare_brindle_always_starts_a_supervisor(db, repo, started, monkeypatch):
    # default_agent is for workers; a launch is always a supervisor, autopilot on
    monkeypatch.setattr(cli, "_say_autopilot", lambda *a: None)
    cli.default(SimpleNamespace(invoked_subcommand=None), cont=False, autopilot=None,
                provider=None, show_version=False)
    assert [(p, ap) for _, p, ap in started] == [("supervisor", True)]


def test_supervisor_command_starts_explicit_supervisor_with_autopilot(db, repo, started):
    cli.supervisor(prompt=None, provider=None, attach=False, watch=False, autopilot=None, branch=None,
                   worktree=None)
    assert [(p, ap) for _, p, ap in started] == [("supervisor", True)]


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
