import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import brindle
from brindle import cli
from brindle import agents, bus, disk, gates, git, mcp_server, providers, services, workspaces
from brindle.config import load_repo_config

from conftest import sh
from git_snapshots import fast_host_tools, repo, workspaces_once  # noqa: F401  (fixtures)
from test_merge_order import add


def write_config(repo, **cfg):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps(cfg))


@pytest.fixture
def feature(db, repo, tmp_path, tmp_path_factory):
    """``workspaces.create(db, repo, "feature")``, built once and copied."""
    return workspaces_once("workspaces:feature", db, tmp_path, tmp_path_factory,
                           lambda: (workspaces.create(db, str(repo), "feature").workspace,))[0]


def test_failed_setup_keeps_workspace(db, repo):
    write_config(repo, setup=["echo before", "false", "echo never"])
    created = workspaces.create(db, str(repo), "broken")
    assert created.setup and not created.setup.ok
    assert "never" not in created.setup.log
    assert db.get_workspace(created.workspace.id)


@pytest.mark.parametrize("configured", [
    pytest.param(None, marks=pytest.mark.variant("A worktree inside the repo must stay ignored and trusted without trusting the main checkout")),
    "../elsewhere",
])
def test_new_worktrees_go_under_the_repo_and_stay_out_of_git_status(db, repo, configured):
    if configured:
        write_config(repo, worktree_root=configured)
    ws = workspaces.create(db, str(repo), "feat/x").workspace
    root = configured or ".brindle/worktrees"
    assert os.path.realpath(ws.path) == os.path.realpath(repo / root / "feat" / "x")
    assert "feat" not in sh("git status --porcelain --untracked-files=all", repo)
    # Still one of brindle's own worktrees (trusted), unlike the main checkout.
    assert providers._in_worktree(ws.path) and not providers._in_worktree(str(repo / "app.py"))


def test_session_config_setup_comes_from_base_branch(db, repo, monkeypatch):
    # The main checkout sits on another branch whose committed config differs
    # from the base the workspace is cut from: the base's config wins, for
    # setup and its services' env, check scoping and the review's checks.
    monkeypatch.setattr(services, "entitled", lambda: True)
    monkeypatch.setattr(services, "docker_path", lambda: None)
    write_config(repo, setup=["echo main:$DB_URL > marker"], checks=["pytest -q"], scoped_checks="off",
                 services=[{"name": "db", "image": "img", "port": 5432, "env": {"DB_URL": "db:{port}"}}])
    sh("git add -A && git commit -qm config && git checkout -qb import/app", repo)
    write_config(repo, setup=["echo app > marker"], checks=[], scoped_checks="infer")
    sh("git add -A && git commit -qm app-config", repo)
    ws = workspaces.create(db, str(repo), "w", base="main").workspace
    assert open(os.path.join(ws.path, "marker")).read().strip() == f"main:db:{ws.port_base + 1}"
    assert gates._scoped(ws, "pytest -q", ("main", [])) == "pytest -q"
    reviewed = []
    monkeypatch.setattr(agents, "start_review", lambda db_, caller, ws_, profile, focus, cfg: reviewed.append(cfg.checks))
    monkeypatch.chdir(repo)
    asyncio.run(mcp_server.request_review(ws.id))
    assert reviewed == [["pytest -q"]]
    # A session's own workspace reads its own branch; its worker reads the session branch as its base.
    sess = workspaces.create(db, str(repo), "brindle/session-2", base="main", apply_prefix=False).workspace
    write_config(Path(sess.path), setup=["echo $SESS_URL > marker"], checks=["echo $SESS_URL > seen"],
                 scoped_checks="off", services=[{"name": "db", "image": "img", "port": 5432, "env": {"SESS_URL": "s:{port}"}}])
    sh("git add -A && git commit -qm sess-config", sess.path)
    add(db, sess, "boss", "interactive", profile="supervisor")
    worker = workspaces.create(db, str(repo), "w2", base=sess.branch).workspace
    assert workspaces.config_for(db, sess).checks == ["echo $SESS_URL > seen"]   # not main's ["pytest -q"]
    assert workspaces.config_for(db, worker).checks == ["echo $SESS_URL > seen"]   # its base is the session branch
    cfg = workspaces.config_for(db, sess)
    want = f"s:{sess.port_base + 1}"
    reviewed.clear()
    asyncio.run(mcp_server.request_review(sess.id))
    assert reviewed == [["echo $SESS_URL > seen"]]
    # Check execution, `brindle setup` and `brindle services` all see the session branch's service env.
    gates.check_summary(db, sess, cfg)
    assert open(os.path.join(sess.path, "seen")).read().strip() == want
    assert CliRunner().invoke(cli.app, ["setup", sess.id]).exit_code == 0
    assert open(os.path.join(sess.path, "marker")).read().strip() == want
    assert CliRunner().invoke(cli.app, ["services", "ls", sess.id]).exit_code == 0
    # A base without the file committed falls back to the working tree.
    sh("git checkout -q --orphan bare && git rm -rqf --cached .brindle", repo)
    sh("git commit -qm bare --allow-empty", repo)
    assert load_repo_config(repo, branch="bare").scoped_checks == "infer"


@pytest.mark.variant("real incident: no-commit stress workers' branches were kept as 'not fully merged' after removal")
def test_remove_deletes_branch_merged_into_non_main_base(db, repo):
    # A session branch with its own commit, not in the checkout's HEAD (main); the
    # worker branch is cut from it and has no commits of its own.
    sh("git checkout -q -b session && git commit -q --allow-empty -m session && git checkout -q main", repo)
    ws = workspaces.create(db, str(repo), "worker", base="session").workspace
    removed = workspaces.remove(db, ws, delete_branch=True)
    assert removed.branch_deleted
    assert not git.branch_exists(str(repo), "worker")


@pytest.fixture
def ref_store(monkeypatch):
    """A fake branch store behind the git calls _drop_if_merged makes: ``refs``
    maps branch -> tip, ``ancestors`` holds (older, newer) pairs that are merged."""
    refs, ancestors = {"worker": "A"}, set()
    monkeypatch.setattr(disk, "tip", lambda root, branch: refs.get(branch))
    monkeypatch.setattr(git, "is_ancestor", lambda root, older, newer: (older, newer) in ancestors)

    def drop(root, branch, sha):
        if refs.get(branch) != sha:
            raise git.GitError(f"{branch} moved")
        del refs[branch]
    monkeypatch.setattr(disk, "drop_branch", drop)
    return refs, ancestors


def test_drop_keeps_branch_that_moved_after_it_was_judged(ref_store, monkeypatch):
    refs, ancestors = ref_store
    ancestors.add(("A", "session"))
    real_is_ancestor = git.is_ancestor

    def judged_then_moved(root, older, newer):
        merged = real_is_ancestor(root, older, newer)
        refs["worker"] = "B"   # the branch moves on before deletion
        return merged
    monkeypatch.setattr(git, "is_ancestor", judged_then_moved)
    ws = SimpleNamespace(repo_root="/r", branch="worker", base_branch="session")
    deleted, _ = workspaces._drop_if_merged(ws, force=False)
    assert not deleted
    assert refs == {"worker": "B"}


def test_remove_refuses_dirty_and_keeps_branch(db, repo, feature):
    ws = feature
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
    assert [(e["kind"], e["workspace_id"], e["repo_root"])
            for e in bus.tail(db, kinds=("workspace.created", "workspace.removed"))] == [
        ("workspace.created", ws.id, ws.repo_root), ("workspace.removed", ws.id, ws.repo_root)]

    # Merged, plainly or by squash: the branch goes with its workspace, its tip archived.
    plain = workspaces.create(db, str(repo), "plain").workspace
    squashed = workspaces.create(db, str(repo), "squashed").workspace
    for merged in (plain, squashed):
        Path(merged.path, f"{merged.name}.txt").write_text("work\n")
        sh(f"git add -A && git commit -qm {merged.name} && git commit -q --allow-empty -m more", merged.path)
    sh(f"git merge -q --no-ff {plain.branch}", repo)
    sh(f"git merge -q --squash {squashed.branch} && git commit -qm 'Squash merge'", repo)
    tip = sh(f"git rev-parse {squashed.branch}", repo).strip()
    for merged in (plain, squashed):
        assert workspaces.remove(db, merged).branch_deleted
    assert sh("git branch --list plain squashed", repo).strip() == ""
    assert sh(f"git rev-parse {disk.ARCHIVE_REF}squashed", repo).strip() == tip
    assert sh(f"git rev-parse {disk.ARCHIVE_REF}plain", repo).strip()


@pytest.mark.variant("input from real use: supervisors pass a worker's agent id, not a workspace name; the only test of that lookup")
def test_remove_workspace_accepts_an_agent_id(db, feature, monkeypatch):
    ws = feature
    add(db, ws, "worker", "assign", result="finished")
    monkeypatch.setattr(mcp_server, "_caller", lambda db: (None, ws))
    mcp_server.remove_workspace("worker", delete_branch=False)
    assert db.get_workspace(ws.id) is None
    assert db.get_agent("worker") is None
    assert not os.path.exists(ws.path)


@pytest.mark.variant("incident: 8 merged workspaces kept their rows after a reviewer's "
                     "`uv run` from the worktree")
def test_remove_forgets_the_row_when_brindle_ran_from_the_removed_worktree(db, feature, monkeypatch):
    # A reviewer ran brindle from the worktree under review (`uv run` there):
    # removing it took brindle's source with it, so a module not loaded yet
    # failed to import afterwards, leaving the row and its agents behind.
    ws = feature
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

