"""A worktree workspace built once per test process and copied into each test.

``workspaces.create`` on conftest's ``repo`` costs a dozen git processes per
test, and the permission tests' large parametrize tables only need the
result: a repo on ``main`` pushed to a bare ``origin``, with a ``feature``
worktree registered in the test's database. ``workspace_template`` builds
that once (under its own temporary brindle home, never the real
one); ``copy_workspace`` copies it into the test's own ``tmp_path`` at the
paths conftest's ``repo`` uses, points git's absolute paths at the copy and
registers the same workspace row in the test's database, so every test still
gets a repo and worktree of its own to read or change.
"""

import os
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import conftest
from brindle import workspaces
from brindle.db import DB, Workspace


@dataclass(frozen=True)
class Template:
    root: Path
    workspace: Workspace


_BUILT: list[Template] = []


@pytest.fixture(scope="session")
def workspace_template(tmp_path_factory):
    # Each module that imports this fixture has its own copy of it, so the
    # build is cached here: one per test process, whichever module asks first.
    if not _BUILT:
        _BUILT.append(_build(tmp_path_factory))
    return _BUILT[0]


def _build(tmp_path_factory) -> Template:
    root = tmp_path_factory.mktemp("workspace-template")
    with pytest.MonkeyPatch.context() as mp:
        # What conftest's brindle_home gives a test: a temporary home, no
        # inherited git location, a fixed identity for the commit.
        mp.setenv("BRINDLE_HOME", str(root / "brindle-home"))
        mp.setenv("BRINDLE_CACHE_DIR", str(root / "brindle-cache"))
        for k in ("GIT_DIR", "GIT_WORK_TREE", "BRINDLE_AGENT_ID"):
            mp.delenv(k, raising=False)
        for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
            mp.setenv(k, "t")
        for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
            mp.setenv(k, "t@example.com")
        repo = conftest.repo.__wrapped__(root)
        ws = workspaces.create(DB(), str(repo), "feature").workspace
    # git init's sample hooks never run (none is enabled): half the files a
    # copy would otherwise write.
    for sample in root.glob("*/**/hooks/*.sample"):
        sample.unlink()
    return Template(root, ws)


def copy_workspace(template: Template, db: DB, tmp_path: Path) -> Workspace:
    """The template's repo, origin and worktree under ``tmp_path``, and its
    workspace row in ``db`` (the test's own database)."""
    src = template.root
    for name in ("origin.git", "proj"):
        shutil.copytree(src / name, tmp_path / name, symlinks=True)
    proj = tmp_path / "proj"
    olds = {str(src), os.path.realpath(src)}
    new = str(tmp_path)
    rel_path = os.path.relpath(template.workspace.path, src)
    # Every absolute path git keeps: origin's url, and the two links between
    # the repo and its worktree.
    for f in [proj / ".git" / "config", *(proj / ".git" / "worktrees").glob("*/gitdir"),
              tmp_path / rel_path / ".git"]:
        text = f.read_text()
        for old in olds:
            text = text.replace(old, new)
        f.write_text(text)
    ws = replace(template.workspace,
                 repo_root=str(tmp_path / os.path.relpath(template.workspace.repo_root, src)),
                 path=str(tmp_path / rel_path), created_at=time.time())
    db.add_workspace(ws)
    return ws
