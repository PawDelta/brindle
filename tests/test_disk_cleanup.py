"""Giving disk space back (brindle.disk): merged worktrees removed whole,
abandoned ones reported, retention of brindle's own files, the free-disk
guard, and dead branches."""

import contextlib
import dataclasses
import errno
import json
import os
import subprocess
import time
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brindle import (agents, config, cull, disk, doctor, git, history, pipeline, pool, procs, sessions, spawn,
                     tasks, tmux,
                     workspaces)
from brindle.cli import app
from brindle.daemon import routes_actions
from brindle.db import Agent

from conftest import sh
from git_snapshots import fast_host_tools, repo, restore_or_build, workspaces_once  # noqa: F401  (fixtures)

DAY = 86400


@pytest.mark.parametrize("metadata", [None, {"repo_root": None}, {"branch": []},
    pytest.param({"removed_at": "yesterday"}, marks=pytest.mark.variant("corrupt trash: a timestamp written as text")),
    pytest.param({"size": -1}, marks=pytest.mark.variant("corrupt trash: a negative size")),
    pytest.param({"removed_at": float("inf")}, marks=pytest.mark.variant("corrupt trash: an infinite timestamp")),
    pytest.param({"removed_at": 10 ** 400}, marks=pytest.mark.variant(
        "corrupt JSON timestamp exceeds float range; retention must not overflow"))])
def test_corrupt_trash_does_not_block_retention(brindle_home, monkeypatch, metadata):
    from brindle import trash

    entry = {"repo_root": "/gone", "path": "/gone/tree", "branch": None,
             "sha": None, "removed_at": 0, "size": 10}
    for ident, data in (("broken", None if metadata is None else {**entry, **metadata}), ("old", entry)):
        folder = trash.directory() / ident
        (folder / "tree").mkdir(parents=True)
        (folder / "entry.json").write_text(json.dumps(None if data is None else {**data, "id": ident}))
    monkeypatch.setattr(trash, "_unpin", lambda *a: None)
    assert trash.retain(now=1, cap=9) == ["old"]
    assert (trash.directory() / "broken" / "tree").is_dir()


@pytest.mark.parametrize("action", ["restore", "expiry", "refused", "path", "cap", "empty",
                                  "registration-failure", "moved", "deleted", "restore-race",
                                  "path-race", "locked", "prune"])
def test_removed_worktree_goes_to_trash(db, repo, monkeypatch, action):
    from brindle import trash

    ws = workspaces.create(db, str(repo), "recover-me").workspace
    path = Path(ws.path)
    git.run(["commit", "--allow-empty", "-m", "unmerged work"], path)
    if action == "locked":
        for timing in ("before", "during"):
            with monkeypatch.context() as patch:
                if timing == "before":
                    git.run(["worktree", "lock", "--reason", "retained checkout", str(path)], repo)
                else:
                    replace = disk._replace
                    def lock_after_preparation(dest, write):
                        replace(dest, write)
                        if dest.name == "entry.json":
                            git.run(["worktree", "lock", "--reason", "retained checkout", str(path)], repo)
                    patch.setattr(disk, "_replace", lock_after_preparation)
                with pytest.raises(git.GitError):
                    git.remove_worktree(repo, path)
                assert path.is_dir() and git.worktree_for_branch(repo, ws.branch) == str(path)
                assert not trash.entries()
            if timing == "before":
                git.run(["worktree", "unlock", str(path)], repo)
    if action in {"restore", "deleted"}:
        module = repo.parent / "module-source"
        git.run(["clone", str(repo), str(module)], repo)
        git.run(["-c", "protocol.file.allow=always", "submodule", "add", str(repo), "nested"], module)
        git.run(["commit", "-am", "nested module"], module)
        git.run(["-c", "protocol.file.allow=always", "submodule", "add", str(module), "module"], path)
        git.run(["-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive"], path)
        git.run(["commit", "-am", "initialized modules"], path)
        for sub in (path / "module", path / "module" / "nested"):
            (sub / "app.py").write_text("staged module work")
            git.run(["add", "app.py"], sub)
            (sub / "app.py").write_text("unstaged module work")
        if action == "deleted":
            git.run(["rm", "--cached", "module"], path)
    if action == "restore":
        git.run(["worktree", "add", "--detach", str(path / "unrelated"), "HEAD"], repo)
        unrelated_pointer = (path / "unrelated" / ".git").read_bytes()
        (path / "fixtures").mkdir()
        (path / "fixtures" / ".git").write_text("fixture data")
        external = repo.parent / "external"
        external.mkdir()
        git.run(["clone", str(repo), str(external / "module")], repo)
        external_config = (external / "module" / ".git" / "config").read_bytes()
        sha = git.out(["rev-parse", "HEAD"], external / "module")
        git.run(["update-index", "--add", "--cacheinfo", "160000", sha, "escaped/module"], path)
        (path / "escaped").symlink_to(external, target_is_directory=True)
    (path / "staged.txt").write_text("staged version")
    git.run(["add", "staged.txt"], path)
    (path / "staged.txt").write_text("working version")
    (path / "uncommitted.txt").write_text("precious work")
    if action == "registration-failure":
        move = trash._move
        def fail(source, dest):
            if dest.name != "admin":
                return move(source, dest)
            raise OSError("move failed")
        monkeypatch.setattr(trash, "_move", fail)
        with pytest.raises(workspaces.WorkspaceError):
            workspaces.remove(db, ws, force=True)
        assert (path / "uncommitted.txt").read_text() == "precious work"
        assert git.worktree_for_branch(repo, ws.branch) == str(path)
        assert not trash.entries()
        return
    workspaces.remove(db, ws, force=True, delete_branch=False)
    entry = trash.entries()[0]
    assert not path.exists()
    assert git.worktree_for_branch(repo, ws.branch) is None
    assert (trash.directory() / entry["id"] / "tree" / "uncommitted.txt").read_text() == "precious work"
    assert next(c["size"] for c in disk.storage_report(db, str(repo))["categories"]
                if c["name"] == "trash") >= entry["size"]
    assert CliRunner().invoke(app, ["trash", "list"]).exit_code == 0
    saved_pointer = (trash.directory() / entry["id"] / "tree" / ".git").read_bytes()
    if action in {"refused", "path", "moved", "restore-race", "path-race"}:
        if action == "path":
            path.mkdir()
        elif action == "moved":
            # A different commit, without a process or checkout on the branch.
            git.run(["commit", "--allow-empty", "-m", "moved"], repo)
            git.run(["branch", "-f", ws.branch, "HEAD"], repo)
        elif action == "path-race":
            move = trash._move
            occupied = []
            def occupy_before_publish(source, dest):
                if dest == path:
                    if path.exists():
                        path.rename(path.with_name("reservation"))
                    path.mkdir()
                    occupied.append(path.stat().st_ino)
                return move(source, dest)
            monkeypatch.setattr(trash, "_move", occupy_before_publish)
        elif action == "restore-race":
            copy = trash.shutil.copytree
            def checkout_during_restore(source, dest, *args, **kwargs):
                result = copy(source, dest, *args, **kwargs)
                if Path(source) == trash.directory() / entry["id"] / "admin":
                    git.run(["worktree", "add", "--force", str(path.with_name("raced")), ws.branch], repo)
                return result
            monkeypatch.setattr(trash.shutil, "copytree", checkout_during_restore)
        else:
            git.add_worktree(repo, path.with_name("other"), ws.branch, "main")
        with pytest.raises(trash.TrashError):
            trash.restore(entry["id"])
        assert trash.entries()
        assert (trash.directory() / entry["id"] / "tree" / ".git").read_bytes() == saved_pointer
        assert (trash.directory() / entry["id"] / "tree" / "uncommitted.txt").read_text() == "precious work"
        if action == "path-race":
            assert path.stat().st_ino == occupied[0]
    elif action in {"expiry", "cap", "empty"}:
        if action == "empty":
            assert CliRunner().invoke(app, ["trash", "empty", "--yes"]).exit_code == 0
        else:
            walked = []
            size_of = disk.size_of
            monkeypatch.setattr(disk, "size_of", lambda p: walked.append(p) or size_of(p))
            if action == "cap":
                assert trash.retain(now=entry["removed_at"], cap=entry["size"]) == []
            trash.retain(now=entry["removed_at"] + (trash.RETENTION + 1 if action == "expiry" else 0),
                         cap=0 if action == "cap" else trash.SIZE_CAP)
            assert walked == []   # sized once, when it was trashed
        assert not trash.entries()
    else:
        if action == "deleted":
            git.delete_branch(repo, ws.branch, force=True)
            git.run(["prune", "--expire", "now"], repo)
        saved_lock = trash.directory() / entry["id"] / "admin" / "locked"
        original_lock = saved_lock.read_bytes() if saved_lock.exists() else None
        if action == "prune":
            read = git.list_worktrees
            def prune_during_restore(root):
                git.run(["worktree", "prune"], root)
                return read(root)
            monkeypatch.setattr(git, "list_worktrees", prune_during_restore)
        result = CliRunner().invoke(app, ["trash", "restore", entry["id"]])
        assert result.exit_code == 0, result.output
        assert (path / "uncommitted.txt").read_text() == "precious work"
        assert git.out(["show", ":staged.txt"], path) == "staged version"
        assert (path / "staged.txt").read_text() == "working version"
        assert "uncommitted.txt" in git.dirty_files(path)
        assert git.worktree_for_branch(repo, ws.branch) == str(path)
        assert git.out(["rev-parse", "HEAD"], path) == entry["sha"]
        restored_lock = Path(git.out(["rev-parse", "--absolute-git-dir"], path)) / "locked"
        assert (restored_lock.read_bytes() if restored_lock.exists() else None) == original_lock
        if action == "restore":
            assert (external / "module" / ".git" / "config").read_bytes() == external_config
            assert (path / "unrelated" / ".git").read_bytes() == unrelated_pointer
            assert git.out(["status", "--porcelain"], path / "unrelated") == ""
            assert (path / "fixtures" / ".git").read_text() == "fixture data"
        if action in {"restore", "deleted"}:
            for sub in (path / "module", path / "module" / "nested"):
                assert git.out(["status", "--porcelain"], sub)
                assert git.out(["show", ":app.py"], sub) == "staged module work"
                assert (sub / "app.py").read_text() == "unstaged module work"
            assert not trash.entries()
        if action == "deleted":
            assert "D\tmodule" in git.out(["diff", "--cached", "--name-status"], path).splitlines()


@pytest.mark.parametrize("why, force, dirty", [
    pytest.param("volume", True, True,    # forced: deleted in place, changes and all
                 marks=pytest.mark.variant("reported failure: a forced removal of a repo on another volume (review 0.6.3, finding 2)")),
    ("volume", False, False),    # unforced and clean: git removes it
    pytest.param("volume", False, True,   # unforced with changes: still refused
                 marks=pytest.mark.variant("fail closed: the fallback never deletes uncommitted work unforced")),
    ("no-rename", True, False),  # no exclusive rename on this platform
    pytest.param("mount", True, False,    # same device, but rename crosses a mount (EXDEV)
                 marks=pytest.mark.variant("bind mounts and dev containers: same device, rename still answers EXDEV")),
    pytest.param("no-syscall", True, False,   # libc has renameat2, the kernel or sandbox doesn't (ENOSYS)
                 marks=pytest.mark.variant("older kernels and sandboxes answer ENOSYS, not EXDEV")),
])
def test_removal_across_volumes(db, repo, monkeypatch, why, force, dirty):
    """A checkout the trash can't take is removed as 0.6.2 did, not kept forever."""
    from brindle import trash

    ws = workspaces.create(db, str(repo), "elsewhere").workspace
    path = Path(ws.path)
    if dirty:
        (path / "uncommitted.txt").write_text("work")
    if why == "volume":   # the device check, faked: no real second volume
        stat = Path.stat
        def other_volume(p, *args, **kwargs):
            values = list(stat(p, *args, **kwargs))
            if p == trash.directory():
                values[2] += 1
            return os.stat_result(values)
        monkeypatch.setattr(Path, "stat", other_volume)
    elif why == "no-rename":
        monkeypatch.setattr(trash, "sys", types.SimpleNamespace(platform="other"))
    else:
        def refuse(*args):
            return -1
        libc = types.SimpleNamespace(renamex_np=refuse, renameat2=refuse)
        error = errno.EXDEV if why == "mount" else errno.ENOSYS
        monkeypatch.setattr(trash, "ctypes", types.SimpleNamespace(
            CDLL=lambda *a, **k: libc, c_char_p=None, c_int=None, c_uint=None, get_errno=lambda: error))
        if why == "no-syscall":
            monkeypatch.setattr(trash, "sys", types.SimpleNamespace(platform="linux"))
    if dirty and not force:
        with pytest.raises(git.GitError):
            git.remove_worktree(repo, path)
        assert (path / "uncommitted.txt").read_text() == "work"
        assert git.worktree_for_branch(repo, ws.branch) == str(path)
        return
    workspaces.remove(db, ws, force=force, delete_branch=False)
    assert not path.exists()
    assert git.worktree_for_branch(repo, ws.branch) is None
    assert not trash.entries()


def _settings(values: dict) -> None:
    path = config.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values))


def _worktree(repo: Path, branch: str, merge: str | None = None, days_old: float = 1) -> Path:
    """A worktree of ``repo`` where brindle puts them, one commit on ``branch``
    made ``days_old`` days ago, merged into main (``merge``: "merge" or
    "squash") or not, with a Rust target dir."""
    wt = workspaces.new_worktree_root(str(repo)) / branch
    sh(f"git worktree add -q -b {branch} {wt}", repo)
    (wt / ".gitignore").write_text(".env\ntarget/\n")
    (wt / f"{branch}.txt").write_text("work\n")
    when = f"{int(time.time() - days_old * DAY)} +0000"
    sh(f"git add -A && GIT_AUTHOR_DATE='{when}' GIT_COMMITTER_DATE='{when}' git commit -qm {branch}", wt)
    if merge == "merge":
        sh(f"git merge -q --no-ff -m m {branch}", repo)
    elif merge == "squash":
        sh(f"git merge -q --squash {branch} && git commit -qm 'Squash merge {branch}'", repo)
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "app").write_bytes(b"x" * 4096)
    reflog = disk._gitdir(str(wt)) / "logs" / "HEAD"
    os.utime(reflog, (time.time() - days_old * DAY,) * 2)
    return wt


@pytest.fixture
def old_worktree(repo, tmp_path, tmp_path_factory):
    """``_worktree(repo, branch, days_old=30)``, built once per branch and copied."""
    return lambda branch: restore_or_build(f"disk:{branch}", tmp_path, tmp_path_factory, ("origin.git", "proj"),
                                           lambda: _worktree(repo, branch, days_old=30))


@pytest.fixture
def shipped(db, repo, tmp_path, tmp_path_factory):
    """A workspace "shipped" with one commit, merged into main; built once and copied."""
    def build():
        ws = workspaces.create(db, str(repo), "shipped").workspace
        Path(ws.path, "a.txt").write_text("a\n")
        sh(f"git add a.txt && git commit -qm a && cd {repo} && git merge -q --no-ff -m m {ws.branch}", ws.path)
        return (ws,)

    return workspaces_once("disk:shipped", db, tmp_path, tmp_path_factory, build)[0]


def _archived(repo: Path, branch: str) -> str:
    return sh(f"git rev-parse {disk.ARCHIVE_REF}{branch}", repo).strip()


def test_upgrade_sweep_removes_merged_worktrees_whole_once_and_never_a_dirty_one(db, repo):
    from brindle import trash

    done = _worktree(repo, "old-done", merge="squash")
    tip = sh("git rev-parse old-done", repo).strip()
    dirty = _worktree(repo, "old-wip", merge="merge")
    (dirty / "wip.txt").write_text("not committed\n")
    history.record(db, str(repo), "merge", branch="old-done")   # known only from its history

    assert [r["path"] for r in disk.upgrade_sweep(db)] == [str(done)]

    assert not done.exists() and _archived(repo, "old-done") == tip
    [entry] = trash.entries()
    assert (trash.directory() / entry["id"] / "tree" / "target" / "debug" / "app").is_file()
    assert sh("git branch --list old-done", repo).strip() == ""
    assert (dirty / "wip.txt").is_file() and (dirty / "target" / "debug" / "app").is_file()
    assert disk.clean(db, path=str(dirty))["kept"][0]["reason"].endswith("uncommitted changes")
    assert dirty.is_dir()
    assert disk.upgrade_sweep(db) is None   # recorded: it runs once


@pytest.mark.parametrize("state", ["dirty", "live", "process", "unknown"])
def test_dirty_or_live_abandoned_worktree_is_never_removed(db, repo, old_worktree, monkeypatch, state):
    wt = old_worktree("idea")
    if state == "dirty":
        (wt / "notes.txt").write_text("unsaved thought\n")
    elif state == "process":   # a detached dev server, or a shell on another tmux server
        monkeypatch.setattr(procs, "cwds", lambda *_: {4242: str(wt / "web")})
    elif state == "unknown":   # processes can't be inspected: fail closed
        def blind(*_):
            raise OSError("lsof: not found")
        monkeypatch.setattr(procs, "cwds", blind)
    else:
        ws = workspaces.adopt_root(db, str(wt))
        db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "processing",
                           "%1", None, time.time()))
        monkeypatch.setattr(agents, "is_alive", lambda a, *rest, **kw: True)

    out = disk.clean(db, abandoned=True)

    assert out["removed"] == [] and wt.is_dir() and (wt / "target" / "debug" / "app").is_file()
    [listed] = disk.storage_report(db, str(repo))["worktrees"]
    assert listed["removable"] is False and listed["reason"]


@pytest.mark.parametrize("cleanup", ["post-merge", "retired"])
@pytest.mark.parametrize("other", ["pane", "process", None])
def test_automatic_cleanup_keeps_a_worktree_something_else_is_live_in(db, shipped, process_scan, cleanup, other,
                                                                  monkeypatch):
    """The pipeline's post-merge removal and the sweep of retired workspaces
    go through workspaces.remove: a pane of another session or a detached
    process in the worktree (the scan, faked, shows it there) keeps it,
    while the workspace's own session (which the removal ends) doesn't."""
    ws = shipped
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "done",
                       "@999", None, time.time() - 7200))
    monkeypatch.setattr(tmux, "list_sessions", lambda: [(ws.tmux_session, False), ("someone-else", False)])
    monkeypatch.setattr(tmux, "session_panes", lambda name: [(999999, ws.path)]
                        if name == "someone-else" and other == "pane" else [])
    held = None
    if other == "process":   # a dev server started by hand, outside any pane
        held = subprocess.Popen(["/bin/sh", "-c", "read line"], cwd=ws.path, stdin=subprocess.PIPE,
                                start_new_session=True)
        process_scan[held.pid] = os.path.realpath(ws.path)
    try:
        if cleanup == "post-merge":
            pipeline._remove(db, ws)
        else:
            cull.prune_retired(db)
    finally:
        if held:
            held.kill()
            held.wait()

    assert Path(ws.path).is_dir() == (other is not None)
    assert (db.get_workspace(ws.id) is not None) == (other is not None)


@pytest.mark.parametrize("sweep", ["scratch", "pool"])
def test_scratch_prune_and_pool_sweep_keep_a_folder_a_process_is_in(db, repo, monkeypatch, sweep):
    """An old scratch session and an orphan pool folder (its row forgotten)
    are deleted by folder alone, unless a process (fake table) is in them."""
    ws = None
    if sweep == "scratch":
        ws = dataclasses.replace(workspaces.create(db, str(repo), "scratchy").workspace, created_at=0.0)
        path = ws.path
        monkeypatch.setattr(sessions.scratch, "sessions", lambda db: [ws])
        monkeypatch.setattr(sessions.scratch, "commit_count", lambda ws: 0)
        monkeypatch.setattr(tmux, "kill_session", lambda name: None)
    else:
        (repo / ".brindle").mkdir(exist_ok=True)
        (repo / ".brindle" / "config.json").write_text(json.dumps({"setup": ["true"]}))
        pool.fill(db, str(repo))
        [entry] = db.pool_entries(str(repo), ready_only=False)
        db.delete_pool_entry(entry.path)
        path = entry.path
    monkeypatch.setattr(procs, "table", lambda: {})
    monkeypatch.setattr(procs, "cwds", lambda *_: {4242: os.path.realpath(path)})
    monkeypatch.setattr(tmux, "session_panes", lambda name: [])
    monkeypatch.setattr(tmux, "list_sessions", lambda: [])

    if ws is not None:
        assert sessions.prune_scratch(db) == 0
        assert db.get_workspace(ws.id) is not None
    else:
        pool.sweep(db, str(repo))
    assert Path(path).is_dir()


@pytest.mark.parametrize("cleanup,child,onlooker", [
    ("retired", None, False),
    ("retired", "vite", False),
    ("retired", "pytest", False),
    ("retired", None, True),     # someone sits in its shell pane
    ("post-merge", None, False),
    ("post-merge", "vite", False),
    pytest.param("tool", None, False, marks=pytest.mark.variant(
        "real incident (release.153): remove_workspace refused a reported, idle worker for its own CLI and MCP")),
    ("tool", None, "viewer"),    # the app's terminal view shows the worker's pane
    pytest.param("tool", None, "other", marks=pytest.mark.variant(
        "the same pane linked into another session still keeps it, now its agents are stopped")),
    pytest.param("merged", None, False, marks=pytest.mark.variant(
        "real incident (release.153): the pipeline's cleanup with no reviewer logged 'worktree kept: in use'")),
    pytest.param("resumed", None, False, marks=pytest.mark.variant(
        "a race from review: the retired sweep's worker resumes between selection and removal")),
])
def test_a_command_in_the_workspaces_own_pane_keeps_its_worktree(db, repo, shipped, monkeypatch, cleanup, child,
                                                                 onlooker):
    """What the removal ends (the pane's own process when its session is
    killed, the agents it stops: by default every live one, with its CLI and
    MCP server) is excused, and so is the app's view of that pane; a dev
    server or test run started in the workspace's own pane is not, though
    its environment (TMUX=, a PWD in the worktree) and its paths there look
    like brindle's, and nor is another session the pane is linked into.
    ``onlooker``: True, someone attached to its own session; "viewer" or
    "other", its pane shown in an app view or in another session.
    "resumed": the sweep chose it while its worker was done, and the worker
    resumes (status and CLI) during the sweep's dirty-files check: kept,
    never stopped."""
    ws = shipped
    wt = os.path.realpath(ws.path)
    keep = None
    mcp = json.dumps({"mcpServers": {"brindle": {"env": {
        "BRINDLE_AGENT_ID": "w1", "BRINDLE_HOME": os.environ.get("BRINDLE_HOME")}}}})
    cli = []   # the worker's CLI and its brindle MCP server, under the pane's shell
    if cleanup == "post-merge":   # the worker's CLI in the pane; the approving reviewer runs this
        pane = f"claude --mcp-config {mcp}"
        db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "waiting",
                           "@998", None, time.time()))
        keep = Agent("r1", ws.id, "reviewer", "claude", None, "review", "done", "@999", None, time.time())
        db.add_agent(keep)
    elif cleanup in ("tool", "merged"):
        pane = "-bash"
        cli = [(510, 500, f"claude --mcp-config {mcp}"), (511, 510, "/u/.local/bin/brindle mcp")]
        db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "waiting",
                           "@998", None, time.time()))
    else:
        pane = "-bash" if cleanup == "resumed" else "-zsh"
        db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "done",
                           "@999", None, time.time() - 7200))
    command = {"vite": f"node {wt}/node_modules/.bin/vite",
               "pytest": f"{wt}/.venv/bin/python3 {wt}/.venv/bin/pytest -q"}.get(child)
    rows = [(500, 1, pane)] + cli + ([(501, 500, command)] if command else [])
    env = f"TMUX=/private/tmp/tmux-501/default,1,0 PWD={wt} HOME=/u"

    def ps(argv, **kw):   # the faked process table, read as procs.table reads it
        if argv[0] != "ps":
            return read(argv, **kw)
        shown = "".join(f"{pid} {ppid} {cmd}" + (f" {env}" if "e" in argv[1] else "") + "\n"
                        for pid, ppid, cmd in rows)
        return subprocess.CompletedProcess(argv, 0, shown, "")

    read = spawn.read
    monkeypatch.setattr(procs, "PROC", Path(repo, "no-proc"))
    monkeypatch.setattr(spawn, "read", ps)
    monkeypatch.setattr(procs, "cwds", lambda *_: {pid: wt for pid, _ppid, _cmd in rows}
                        | {os.getpid(): os.getcwd()})
    real_panes, real_sessions = tmux.session_panes, tmux.list_sessions
    shown = {"viewer": "brindle-view-4321-1", "other": "someone-else"}.get(onlooker)   # the app's view names
    monkeypatch.setattr(tmux, "session_panes", lambda s: [(500, wt)] if s in (ws.tmux_session, shown)
                        else real_panes(s))
    monkeypatch.setattr(tmux, "list_sessions", lambda: real_sessions() + [(ws.tmux_session, onlooker is True)]
                        + ([(shown, True)] if shown else []))
    stopped = []
    monkeypatch.setattr(agents, "is_alive", lambda a, *rest, **kw: a.id == "w1" and a.status == "waiting"
                        and "w1" not in stopped)
    monkeypatch.setattr(agents, "_stop", lambda db, a: stopped.append(a.id))
    if cleanup == "resumed":   # after the sweep chose it, at its dirty-files check
        dirty_files = git.dirty_files

        def resume(path):
            if (510, 500, f"claude --mcp-config {mcp}") not in rows:
                db.set_status("w1", "waiting")
                rows.extend([(510, 500, f"claude --mcp-config {mcp}"), (511, 510, "/u/.local/bin/brindle mcp")])
            return dirty_files(path)
        monkeypatch.setattr(git, "dirty_files", resume)

    if cleanup == "post-merge":
        pipeline._remove(db, ws, keep=keep)
    elif cleanup == "merged":
        pipeline._remove(db, ws)
    elif cleanup == "tool":
        with contextlib.suppress(workspaces.WorkspaceError):
            workspaces.remove(db, ws, delete_branch=True)
    else:
        cull.prune_retired(db)

    removed = child is None and onlooker in (False, "viewer") and cleanup != "resumed"
    assert Path(ws.path).is_dir() == (not removed)
    assert (db.get_workspace(ws.id) is None) == removed
    assert stopped == (["w1"] if removed and cleanup != "retired" else [])


def test_pipeline_cleanup_keeps_worker_messaged_after_initial_check(db, shipped, monkeypatch):
    ws = shipped
    worker = Agent("w1", ws.id, "developer", "claude", None, "assign", "waiting",
                   "@998", "Ready", 1)
    db.add_agent(worker)
    stopped = []
    monkeypatch.setattr(agents, "is_alive", lambda a, *args, **kw: a.id not in stopped)
    monkeypatch.setattr(agents, "reconcile", lambda db, a, **kw: a)
    monkeypatch.setattr(agents, "_stop", lambda db, a: stopped.append(a.id))
    monkeypatch.setattr(disk, "live_in", lambda *args, **kw: False)
    monkeypatch.setattr(pipeline, "_try_merge", lambda *args: "Merged shipped")
    notices = []

    def tell(db, parent_id, text, sender_id):
        notices.append(text)
        if len(notices) == 1:
            assert db.pending_count(worker.id) == 0
            db.enqueue(worker.id, "Please handle the follow-up", None)

    monkeypatch.setattr(pipeline, "_tell", tell)

    pipeline._finish_locked(db, worker, ws, None)

    assert Path(ws.path).is_dir()
    assert db.get_workspace(ws.id) is not None
    assert db.get_agent(worker.id) is not None
    assert stopped == []
    assert db.pending_count(worker.id) == 1
    assert len(notices) == 2


def test_abandoned_worktree_is_only_listed_until_cleaned(db, repo, old_worktree, monkeypatch):
    wt = old_worktree("stalled")
    tip = sh("git rev-parse stalled", repo).strip()
    monkeypatch.chdir(repo)

    assert CliRunner().invoke(app, ["clean"]).exit_code == 0
    [listed] = disk.storage_report(db, str(repo))["worktrees"]
    assert listed["path"] == str(wt) and listed["state"] == "abandoned" and listed["removable"]
    assert listed["size"] >= listed["build_size"] >= 4096
    assert wt.is_dir() and "stalled" in sh("git branch --list stalled", repo)

    out = routes_actions.clean_storage(db, {}, {"path": str(wt)})

    assert [r["archived"] for r in out["removed"]] == [f"{disk.ARCHIVE_REF}stalled"]
    assert not wt.exists() and _archived(repo, "stalled") == tip
    assert sh("git branch --list stalled", repo).strip() == ""


def _status(uids: tuple[int, ...] | None = None, gids: tuple[int, ...] | None = None,
            cap_prm: int = 0, name: str = "runsvc", ppid: int = 1) -> str:
    """A /proc/<pid>/status: its Name and parent, real, effective, saved and
    filesystem ids (this user's unless given) and capabilities."""
    uids = uids or (os.getuid(),) * 4
    gids = gids or (os.getgid(),) * 4
    return (f"Name:\t{name}\nState:\tR (running)\nPPid:\t{ppid}\nUid:\t{chr(9).join(map(str, uids))}\n"
            f"Gid:\t{chr(9).join(map(str, gids))}\nCapPrm:\t{cap_prm:016x}\nCapEff:\t{cap_prm:016x}\n"
            "NoNewPrivs:\t0\nSeccomp:\t0\n")


RUNNER = (2100, 2101, 2102, 2103, 2104)   # pytest and its xdist workers, in the checkout
UID, GID = os.getuid(), os.getgid()
IDS = f"Uid {UID} {UID} {UID} {UID}, Gid {GID} {GID} {GID} {GID}"
STARTED = f"started {123456 / os.sysconf('SC_CLK_TCK'):.2f}s after boot, argv[0] Runner.Listener"


@pytest.mark.parametrize("scan, refused, kept, logged", [
    ("proc-unreadable", None, True, "nor can its ids (PermissionError), so it counts as in use: pid 200, "
     "Name ?, State ?, PPid ? (?)"),
    ("lsof-partial", None, True, None),
    # GitHub's ubuntu runner: a service whose /proc entry shows this user as
    # its owner (its effective uid), but whose folder the kernel won't show
    # this user because it runs with another's ids. Not this user's, any
    # more than a root process is.
    ("runner", _status(uids=(0, UID, 0, UID)), False,
     "runs with another's ids (uid real 0, uid saved 0), so it isn't this user's: pid 1354, Name runsvc"),
    ("runner", _status(gids=(GID + 1,) * 4), False,
     f"runs with another's ids (gid real {GID + 1}, gid effective {GID + 1}"),
    # Refused with this user's own ids (capabilities, non-dumpable, a
    # security module): can't be told, so in use; logged to tell what it is.
    ("runner", _status(cap_prm=1 << 10), True, "CapEff 0000000000000400"),
    ("runner", _status(), True, "a process with this user's ids has a folder that can't be read, so it "
     "counts as in use: pid 1354, Name runsvc, State R (running), PPid 1 (systemd), "
     f"{IDS}, CapEff 0000000000000000, NoNewPrivs 0, Seccomp 0, {STARTED}"),
    ("runner", None, True, f"nor can its ids (FileNotFoundError), so it counts as in use: pid 1354, "
     f"Name ?, State ?, PPid 1 (systemd), Uid ?, Gid ?, CapEff ?, NoNewPrivs ?, Seccomp ?, {STARTED}"),
    # Whatever the service, this user's own process in the worktree keeps it.
    ("runner+shell", _status(uids=(0, UID, 0, UID)), True, "uid real 0, uid saved 0"),
    # Linux's per-user service manager (``systemd --user``), refused for its
    # CAP_WAKE_ALARM with this user's ids: it never runs in a project.
    ("systemd", _status(name="systemd", cap_prm=1 << 35), False, None),
    # Something else calling itself systemd, not started by PID 1: unknown.
    ("systemd", _status(name="systemd", ppid=2000, cap_prm=1 << 35), True, "Name systemd, State R (running), "
     "PPid 2000"),
    ("ssh-agent", _status(name="ssh-agent"), False, None),   # it chdirs to / as it daemonizes
    ("ignored", _status(name="syncd"), False, None),   # the repo's live_check_ignore names it
    # Anything else refused keeps every worktree, and a removal says what it was.
    ("unknown", _status(name="syncd"), True, "Name syncd"),
    # A worktree's own config can't excuse what holds it: only the repo's base config does.
    ("own-config", _status(name="syncd"), True, "Name syncd"),
], ids=["proc-unreadable", "lsof-partial", "runner-root-ids", "runner-other-group", "runner-capabilities",
        "runner-own-ids", "runner-no-status", "runner-and-a-shell-in-the-worktree", "systemd-user-manager",
        "systemd-not-from-pid-1", "ssh-agent", "live-check-ignore", "unknown-is-named",
        "worktree-config-ignored"])
def test_a_partial_process_scan_keeps_every_worktree(db, repo, old_worktree, tmp_path, monkeypatch, caplog,
                                                     real_process_scan, scan, refused, kept, logged):
    """One live process whose folder can't be read (or an lsof that failed
    part way) must not read as no process at all, unless the process isn't
    this user's (it runs with another's uid or gid), or it is one that never
    runs in a project: a known service manager or agent, or one the repo's
    ``live_check_ignore`` names."""
    wt = old_worktree("idea")
    if scan == "ignored":
        (repo / ".brindle").mkdir(exist_ok=True)
        (repo / ".brindle" / "config.json").write_text(json.dumps({"live_check_ignore": ["syncd"]}))
    ignore = disk.live_check_ignore(repo)
    argv = {"systemd": "/usr/lib/systemd/systemd\0--user\0", "ssh-agent": "/usr/bin/ssh-agent\0-D\0"}
    fake = tmp_path / "proc"
    (fake / "self").mkdir(parents=True)
    monkeypatch.setattr(procs, "PROC", fake)
    if scan == "lsof-partial":
        listing = subprocess.CompletedProcess([], 1, stdout=f"p{os.getpid()}\nfcwd\nn/\np200\n",
                                              stderr="lsof: can't stat a process")
        monkeypatch.setattr(spawn, "read", lambda *a, **kw: listing)
    else:
        os.symlink(str(repo), fake / "self" / "cwd")
        (fake / "self" / "status").write_text(_status())
        rows = {100: "/", 200: str(wt)} if scan == "proc-unreadable" else (
            {100: "/", **{pid: str(repo) for pid in RUNNER}, 1354: str(wt)}
            | ({2200: str(wt / "src")} if scan == "runner+shell" else {}))
        for pid, cwd in rows.items():
            (fake / str(pid)).mkdir()
            os.symlink(cwd, fake / str(pid) / "cwd")
            (fake / str(pid) / "cmdline").write_text(
                argv.get(scan, "") if pid == 1354 and scan in argv
                else "/opt/runner/bin/Runner.Listener\0--never-logged\0")
            (fake / str(pid) / "environ").write_text("MARKER=never-logged\0")
            if scan != "proc-unreadable" and (pid != 1354 or refused):
                (fake / str(pid) / "status").write_text(refused if pid == 1354 else _status())
        if scan != "proc-unreadable":
            # its stat: parent pid 1, started 123456 clock ticks after boot (field 22)
            (fake / "1354" / "stat").write_text(f"1354 (run svc) R 1 {'0 ' * 17}123456 0 0\n")
            (fake / "1").mkdir()                 # its parent, with no folder of its own to read
            (fake / "1" / "status").write_text("Name:\tsystemd\n")
        (fake / "300").mkdir()               # exited while the scan ran: no cwd left
        if scan == "proc-unreadable":
            (fake / "200").chmod(0)          # live, but its folder can't be read
        real_readlink = os.readlink

        def readlink(path, *a, **kw):   # the kernel refusing /proc/1354/cwd, its status still readable
            if Path(path) == fake / "1354" / "cwd":
                raise PermissionError(13, "Permission denied", str(path))
            return real_readlink(path, *a, **kw)

        monkeypatch.setattr(os, "readlink", readlink)
    try:
        if not kept:
            assert set(procs.cwds(ignore)) == {100, *RUNNER}
        elif scan != "runner+shell":
            with pytest.raises(OSError):
                procs.cwds(ignore)
        out = disk.clean(db, path=str(wt))
        if scan in ("unknown", "own-config"):   # it keeps any worktree, and the refusal names the process
            other = workspaces.create(db, str(repo), "other").workspace
            if scan == "own-config":   # the worker's branch lists it, committed and in its checkout
                Path(other.path, ".brindle").mkdir(exist_ok=True)
                Path(other.path, ".brindle", "config.json").write_text(json.dumps({"live_check_ignore": ["syncd"]}))
                sh("git add -f .brindle/config.json && git commit -qm ignore", other.path)
            with pytest.raises(workspaces.WorkspaceError) as refusal:
                workspaces.remove(db, other)
            assert "process syncd (pid 1354)" in str(refusal.value), refusal.value
            assert "add it to live_check_ignore if it never runs in your projects" in str(refusal.value)
    finally:
        if (fake / "200").exists():
            (fake / "200").chmod(0o755)

    assert (out["removed"] == []) == kept and wt.is_dir() == kept
    said = [r.getMessage() for r in caplog.records if r.name == "brindle.procs"]
    assert (logged is None and said == []) or any(logged in m for m in said), said
    assert "never-logged" not in caplog.text


def test_retention_caps_backups_archive_and_logs(brindle_home):
    home = config.brindle_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "brindle.db").write_text("live")
    for i in range(4):
        b = home / f"brindle.db.before-fix-{i}"
        b.write_text("old")
        os.utime(b, (1000 + i, 1000 + i))
    (home / "ollama.log").write_bytes(b"y" * (2 * disk.MB))
    (home / "hooks.log").write_text("small\n")
    old = home / "archive" / "transcripts" / "a1"
    new = home / "archive" / "transcripts" / "a2"
    for d, age in ((old, 40), (new, 1)):
        d.mkdir(parents=True)
        (d / "t.jsonl.gz").write_text("t")
        os.utime(d, (time.time() - age * DAY,) * 2)
    (home / "archive" / "brindle.jsonl").write_text("{}\n")
    _settings({"retention": {"log_mb": 1}})

    disk.retain()

    assert sorted(p.name for p in home.glob("brindle.db.before-*")) == [
        "brindle.db.before-fix-2", "brindle.db.before-fix-3"]
    assert (home / "brindle.db").read_text() == "live"
    assert (home / "ollama.log").stat().st_size == 0
    assert (home / "ollama.log.1").stat().st_size == 2 * disk.MB
    assert (home / "hooks.log").read_text() == "small\n"
    assert not old.exists() and new.is_dir() and (home / "archive" / "brindle.jsonl").is_file()


@pytest.mark.variant("security boundary (review blocker): log rotation never writes through a symlink or hard link into the database or a file outside brindle's home")
def test_log_rotation_never_follows_a_symlink_or_alias(brindle_home, tmp_path):
    home = config.brindle_home()
    home.mkdir(parents=True, exist_ok=True)
    database = home / "brindle.db"
    database.write_bytes(b"d" * (2 * disk.MB))
    (home / "hooks.log").symlink_to(database)
    os.link(database, home / "agent.log")
    outside = tmp_path / "theirs.txt"
    outside.write_text("theirs\n")
    (home / "ollama.log").write_bytes(b"y" * (2 * disk.MB))
    (home / "ollama.log.1").symlink_to(outside)
    _settings({"retention": {"log_mb": 1}})

    disk.retain()

    assert database.read_bytes() == b"d" * (2 * disk.MB)
    assert outside.read_text() == "theirs\n"
    rotated = home / "ollama.log.1"
    assert not rotated.is_symlink() and rotated.read_bytes() == b"y" * (2 * disk.MB)
    assert (home / "ollama.log").stat().st_size == 0


def test_archive_cap_trims_oldest_records_with_their_transcripts(brindle_home):
    archive = history.archive_dir()
    (archive / "transcripts").mkdir(parents=True)
    lines = []
    for i in range(4):
        tdir = archive / "transcripts" / f"a{i}"
        tdir.mkdir()
        (tdir / "t.jsonl.gz").write_bytes(b"t" * 300)
        lines.append(json.dumps({"ts": time.time() - (4 - i) * 60, "pad": "r" * 2000,
                                 "agents": [{"id": f"a{i}", "transcripts": [f"transcripts/a{i}/t.jsonl.gz"]}]}))
    (archive / "proj.jsonl").write_text("\n".join(lines) + "\n")
    os.chmod(archive / "proj.jsonl", 0o600)
    cap = 5000
    _settings({"retention": {"archive_gb": cap / disk.GB, "archive_days": 0}})

    disk.retain()

    kept = (archive / "proj.jsonl").read_text().splitlines()
    assert kept == lines[2:] and disk.size_of(archive) <= cap
    assert [d.name for d in sorted((archive / "transcripts").iterdir())] == ["a2", "a3"]
    assert (archive / "proj.jsonl").stat().st_mode & 0o777 == 0o600


def test_setup_waits_for_disk_unless_overridden(db, repo, monkeypatch):
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps({"setup": ["touch built"]}))
    sh("git add -A && git commit -qm setup", repo)
    monkeypatch.setattr(disk, "free_bytes", lambda path: 3 * disk.GB)

    created = workspaces.create(db, str(repo), "lowdisk")

    ws = created.workspace
    assert not created.setup.ok and created.setup.waiting.startswith("waiting for disk")
    assert not Path(ws.path, "built").exists()
    monkeypatch.chdir(ws.path)
    held = CliRunner().invoke(app, ["setup"])
    assert held.exit_code == 1 and "waiting for disk" in held.output and "--anyway" in held.output
    assert not Path(ws.path, "built").exists()
    forced = CliRunner().invoke(app, ["setup", "--anyway"])
    assert forced.exit_code == 0, forced.output
    assert Path(ws.path, "built").is_file()


@pytest.mark.parametrize("held", [
    lambda repo: agents.admission("developer", str(repo)),
    lambda repo: spawn.check_no_room(str(repo)),
])
def test_low_free_disk_holds_a_launch(repo, monkeypatch, held):
    monkeypatch.setattr(disk, "free_bytes", lambda path: 3 * disk.GB)

    why = held(repo)

    assert why is not None and why.startswith("waiting for disk: 3.0 GB free")
    assert tasks.room_wait(str(repo)) == why


@pytest.mark.slow("end-to-end clean/doctor CLI workflow uses real Git worktrees and verifies archived branch tips")
def test_clean_lists_then_removes_merged_and_archives_stale_branches(db, repo, monkeypatch):
    wt = _worktree(repo, "feat", merge="merge")
    when = f"{int(time.time() - 30 * DAY)} +0000"
    (repo / "idea.txt").write_text("an idea main doesn't have\n")
    sh(f"git checkout -q -b old-idea && git add idea.txt && GIT_AUTHOR_DATE='{when}' "
       f"GIT_COMMITTER_DATE='{when}' git commit -qm idea && git checkout -q main", repo)
    tip = sh("git rev-parse old-idea", repo).strip()
    assert not disk.merged(str(repo), "old-idea", "main")
    monkeypatch.chdir(repo)
    runner = CliRunner()

    dry = runner.invoke(app, ["clean", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "old-idea" in dry.output and str(wt) in dry.output
    assert wt.is_dir() and sh("git branch --list old-idea", repo).strip() == "old-idea"
    report = {c.name: c.detail for c in doctor.disk_checks(str(repo))}
    assert "old-idea" in report["dead branches"] and str(wt) in report["merged worktrees"]
    sh("git worktree add -q ../noise -b noise", repo)   # not made by brindle: never removed

    plain = runner.invoke(app, ["clean"])
    declined = runner.invoke(app, ["clean", "--branches"], input="n\n")

    assert plain.exit_code == declined.exit_code == 0, plain.output + declined.output
    assert not wt.exists() and _archived(repo, "feat")
    assert sh("git rev-parse old-idea", repo).strip() == tip
    assert git.run(["rev-parse", "--verify", "--quiet", f"{disk.ARCHIVE_REF}old-idea"], repo,
                   check=False).returncode != 0

    done = runner.invoke(app, ["clean", "--branches", "--yes"])

    assert done.exit_code == 0, done.output
    assert sh("git branch --list old-idea", repo).strip() == ""
    assert _archived(repo, "old-idea") == tip
    assert "noise" in sh("git branch --list noise", repo)


def test_a_branch_that_moves_before_deletion_is_kept(db, repo, old_worktree, monkeypatch):
    wt = old_worktree("stalled")
    removing = git.remove_worktree

    def commit_during_teardown(root, path, force=False):
        (Path(path) / "late.txt").write_text("late\n")
        sh("git add late.txt && git commit -qm late", Path(path))
        removing(root, path, force)

    monkeypatch.setattr(git, "remove_worktree", commit_during_teardown)

    [out] = disk.clean(db, path=str(wt))["removed"]

    assert not wt.exists() and out["archived"] is None
    assert sh("git log -1 --format=%s stalled", repo) == "late"
    when = f"{int(time.time() - 30 * DAY)} +0000"
    sh(f"git checkout -q -b old && echo o > o.txt && git add o.txt && GIT_AUTHOR_DATE='{when}' "
       f"GIT_COMMITTER_DATE='{when}' git commit -qm old && git checkout -q main", repo)
    [listed] = [b for b in disk.dead_branches(db, str(repo)) if b.name == "old"]
    sh("git checkout -q old && echo n > n.txt && git add n.txt && git commit -qm newer"
       " && git checkout -q main", repo)

    with pytest.raises(git.GitError):
        disk.delete_branch(db, str(repo), listed)

    assert sh("git log -1 --format=%s old", repo) == "newer"
    assert git.run(["rev-parse", "--verify", "--quiet", f"{disk.ARCHIVE_REF}old"], repo,
                   check=False).returncode != 0


@pytest.mark.variant("regression guard (review blocker): a merge record counts only for the tip it recorded, so work committed after the merge is never swept")
def test_a_branch_that_moved_on_after_its_merge_is_not_swept(db, repo, shipped):
    ws = shipped
    db.set_merged(ws.id, sh("git rev-parse HEAD", ws.path))
    Path(ws.path, "b.txt").write_text("more work\n")
    sh("git add b.txt && git commit -qm b", ws.path)
    later = time.time() + 2 * disk.FRESH

    assert disk.sweep_merged(db, later) == []

    assert Path(ws.path, "b.txt").is_file()
    assert [t.state for t in disk.trees(db, str(repo), later, sizes=False)] == [disk.ACTIVE]


def test_pushed_branch_is_deleted_on_origin_once_its_pr_merged(repo, monkeypatch):
    sh("git checkout -q -b brindle/session-1 && git commit -q --allow-empty -m s"
       " && git push -q origin brindle/session-1 && git commit -q --allow-empty -m newer"
       " && git checkout -q main", repo)
    sha = sh("git rev-parse brindle/session-1~1", repo).strip()
    newer = sh("git rev-parse brindle/session-1", repo).strip()
    ws = workspaces.Workspace(id="r/s", repo_root=str(repo), name="s", kind="main",
                              branch="brindle/session-1", base_branch="main", path=str(repo),
                              port_base=None, tmux_session="x", created_at=0.0)
    assert disk.delete_pushed_remote(ws, sha) is None   # off by default
    _settings({"delete_pushed_branches": True})
    running, raced = git.run_env, []

    def someone_pushes_first(args, *rest, **kw):
        if "push" in args and not raced:
            raced.append(sh(f"git push -q origin {newer}:refs/heads/brindle/session-1", repo))
        return running(args, *rest, **kw)

    monkeypatch.setattr(git, "run_env", someone_pushes_first)

    assert disk.delete_pushed_remote(ws, sha) is None
    assert sh("git ls-remote --heads origin brindle/session-1", repo).split()[0] == newer
    assert disk.delete_pushed_remote(ws, newer)
    assert sh("git ls-remote --heads origin brindle/session-1", repo).strip() == ""


def test_spotlight_is_kept_out_of_brindle_dirs(db, repo, monkeypatch):
    monkeypatch.setattr(disk.sys, "platform", "darwin")
    # Faked darwin makes spawn.ensure_git probe xcode-select, which a Linux host lacks: the tools are present here.
    monkeypatch.setattr(spawn, "_tools_checked", True)

    root = workspaces.new_worktree_root(str(repo))
    workspaces.new_worktree_root(str(repo))

    assert (root / disk.NEVER_INDEX).is_file()
    assert (config.brindle_home() / disk.NEVER_INDEX).is_file()
