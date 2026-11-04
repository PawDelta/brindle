"""Faster stand-ins for conftest's per-test git setup, for modules that import
them (``from fast_repo import repo, repo_template  # noqa: F401``).

``repo`` is the same repo conftest's fixture builds (``main`` with one commit,
pushed to a bare ``origin``), but built once per module and copied per test:
a copy is a few file writes, the build is eight git processes. A module whose
tests start from worktrees too builds them into its own template (with
``scratch_db``) and copies that the same way (``copy_tree``, ``moved``)."""

import dataclasses
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import sh

IDENTITY = "-c user.name=t -c user.email=t@example.com"


def build_repo(base: Path) -> Path:
    """conftest's ``repo``, under ``base``: ``base/proj`` pushed to ``base/origin.git``."""
    origin = base / "origin.git"
    sh(f"git init -q --bare -b main {origin}", base)
    work = base / "proj"
    work.mkdir()
    sh("git init -q -b main", work)
    (work / "app.py").write_text("print('hi')\n")
    (work / ".gitignore").write_text(".env\n")
    (work / ".env").write_text("SECRET=1\n")
    sh(f"git add -A && git {IDENTITY} commit -qm init", work)
    sh(f"git remote add origin {origin} && git push -q -u origin main", work)
    sh("git remote set-head origin main", work)
    return work


@contextmanager
def scratch_db(home: Path):
    """A brindle DB under ``home``, for building a template's worktrees
    outside any test (whose own BRINDLE_HOME is per test). Git identity too,
    for commits made in them."""
    from brindle.db import DB

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("BRINDLE_HOME", str(home))
        for k in ("GIT_AUTHOR", "GIT_COMMITTER"):
            mp.setenv(f"{k}_NAME", "t")
            mp.setenv(f"{k}_EMAIL", "t@example.com")
        db = DB()
        try:
            yield db
        finally:
            db.conn.close()


def copy_tree(template: Path, dest: Path) -> Path:
    """A copy of a ``build_repo`` tree (with any worktrees added in it) at
    ``dest``: origin, and each worktree's links to the repo, point into the
    copy. Returns the copy's repo."""
    for child in template.iterdir():
        shutil.copytree(child, dest / child.name, symlinks=True)
    old, new = str(template), str(dest)
    git = dest / "proj" / ".git"
    files = [git / "config"]
    for gitdir in sorted((git / "worktrees").glob("*/gitdir")):
        files += [gitdir, Path(gitdir.read_text().strip().replace(old, new))]   # and the worktree's .git
    for f in files:
        f.write_text(f.read_text().replace(old, new))
    return dest / "proj"


def moved(row, template: Path, dest: Path):
    """``row`` (a Workspace) as it is in the copy at ``dest``."""
    old, new = str(template), str(dest)
    return dataclasses.replace(row, repo_root=row.repo_root.replace(old, new), path=row.path.replace(old, new))


@pytest.fixture(autouse=True)   # in each module that imports it
def no_screen_gap(monkeypatch):
    """``reconcile`` reads a supervisor's pane twice, 0.7s apart, on every
    message sent to it: the same two reads, back to back."""
    from brindle import agents

    real = agents.screen_status
    monkeypatch.setattr(agents, "screen_status",
                        lambda db, agent, samples=2, gap=0.7: real(db, agent, samples, 0))


@pytest.fixture(scope="module")
def repo_template(tmp_path_factory):
    base = tmp_path_factory.mktemp("repo-template")
    build_repo(base)
    return base


@pytest.fixture
def repo(repo_template, tmp_path):
    """A repo on ``main`` with one commit, pushed to a bare ``origin``."""
    return copy_tree(repo_template, tmp_path)
