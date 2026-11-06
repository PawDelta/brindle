"""The demo on a brand new account: no git identity anywhere."""

import os
import subprocess
from pathlib import Path

from brindle import demo, workspaces


def _no_identity(monkeypatch, tmp_path):
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
              "EMAIL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "newhome"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _worker_commit(ws, name):
    """What a worker does: plain git in its worktree, no -c flags, in the
    environment brindle launches it with."""
    path = Path(ws.path)
    env = {**os.environ, **workspaces.workspace_env(ws)}
    (path / name).write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "work"], cwd=path, check=True, env=env)


def _last_author(path):
    return subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_demo_merges_with_no_git_identity(db, repo, monkeypatch, tmp_path):
    # A person's real repo with no identity: the worker's commits and
    # brindle's merge commit fall back, and no config is written.
    _no_identity(monkeypatch, tmp_path)
    real_a = workspaces.create(db, str(repo), "a", run_setup=False).workspace
    real_b = workspaces.create(db, str(repo), "b", run_setup=False).workspace
    _worker_commit(real_a, "a.py")
    workspaces.merge_back(db, real_a)
    config = subprocess.run(["git", "config", "--local", "--list"], cwd=repo, check=True,
                            capture_output=True, text=True).stdout
    assert "user.name" not in config and "user.email" not in config

    # Half an identity (a name, no email): that half is kept.
    subprocess.run(["git", "config", "user.name", "Existing Person"], cwd=repo, check=True)
    _worker_commit(real_b, "b.py")
    assert _last_author(real_b.path) == "Existing Person <brindle@localhost>"
    workspaces.merge_back(db, real_b)
    assert _last_author(repo) == "Existing Person <brindle@localhost>"

    # The demo repo carries its own identity: workers and merges both work.
    root = demo.create()
    first = workspaces.create(db, str(root), "slug", base="main", run_setup=False).workspace
    second = workspaces.create(db, str(root), "wrap", base="main", run_setup=False).workspace
    _worker_commit(first, "a.py")
    _worker_commit(second, "b.py")
    workspaces.merge_back(db, first)
    workspaces.merge_back(db, second)    # main moved on: a real merge commit
    assert _last_author(root) == "brindle demo <demo@brindle.invalid>"

    # The demo repo: workers and merges both work with no identity.
    root = demo.create()
    first = workspaces.create(db, str(root), "slug", base="main", run_setup=False).workspace
    second = workspaces.create(db, str(root), "wrap", base="main", run_setup=False).workspace
    _worker_commit(first, "a.py")
    _worker_commit(second, "b.py")
    workspaces.merge_back(db, first)
    workspaces.merge_back(db, second)    # main moved on: a real merge commit

    names = subprocess.run(["git", "ls-files"], cwd=root, check=True, capture_output=True,
                           text=True).stdout.split()
    assert "a.py" in names and "b.py" in names
