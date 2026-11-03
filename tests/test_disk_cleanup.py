"""Giving disk space back (brindle.disk): merged worktrees removed whole,
abandoned ones reported, retention of brindle's own files, the free-disk
guard, and dead branches."""

import dataclasses
import json
import os
import subprocess
import time
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

DAY = 86400


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


def _archived(repo: Path, branch: str) -> str:
    return sh(f"git rev-parse {disk.ARCHIVE_REF}{branch}", repo).strip()


def test_upgrade_sweep_removes_merged_worktrees_whole_once_and_never_a_dirty_one(db, repo):
    done = _worktree(repo, "old-done", merge="squash")
    tip = sh("git rev-parse old-done", repo).strip()
    dirty = _worktree(repo, "old-wip", merge="merge")
    (dirty / "wip.txt").write_text("not committed\n")
    history.record(db, str(repo), "merge", branch="old-done")   # known only from its history

    assert [r["path"] for r in disk.upgrade_sweep(db)] == [str(done)]

    assert not done.exists() and _archived(repo, "old-done") == tip
    assert sh("git branch --list old-done", repo).strip() == ""
    assert (dirty / "wip.txt").is_file() and (dirty / "target" / "debug" / "app").is_file()
    assert disk.clean(db, path=str(dirty))["kept"][0]["reason"].endswith("uncommitted changes")
    assert dirty.is_dir()
    assert disk.upgrade_sweep(db) is None   # recorded: it runs once


@pytest.mark.parametrize("state", ["dirty", "live", "process", "unknown"])
def test_dirty_or_live_abandoned_worktree_is_never_removed(db, repo, monkeypatch, state):
    wt = _worktree(repo, "idea", days_old=30)
    if state == "dirty":
        (wt / "notes.txt").write_text("unsaved thought\n")
    elif state == "process":   # a detached dev server, or a shell on another tmux server
        monkeypatch.setattr(procs, "cwds", lambda: {4242: str(wt / "web")})
    elif state == "unknown":   # processes can't be inspected: fail closed
        def blind():
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
def test_automatic_cleanup_keeps_a_worktree_something_else_is_live_in(db, repo, cleanup, other):
    """The pipeline's post-merge removal and the sweep of retired workspaces
    go through workspaces.remove: a pane of another session or a detached
    process in the worktree keeps it, while the workspace's own session
    (which the removal ends) doesn't."""
    ws = workspaces.create(db, str(repo), "shipped").workspace
    Path(ws.path, "a.txt").write_text("a\n")
    sh("git add a.txt && git commit -qm a", ws.path)
    sh(f"git merge -q --no-ff -m m {ws.branch}", repo)
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "done",
                       "@999", None, time.time() - 7200))
    tmux.ensure_session(ws.tmux_session, ws.path, {})
    held = None
    if other == "pane":
        tmux._tmux("new-session", "-d", "-s", "someone-else", "-c", ws.path, "/bin/sh")
    elif other == "process":   # a dev server started by hand, outside any pane
        held = subprocess.Popen(["/bin/sh", "-c", "read line"], cwd=ws.path, stdin=subprocess.PIPE,
                                start_new_session=True)
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
    monkeypatch.setattr(procs, "cwds", lambda: {4242: os.path.realpath(path)})
    monkeypatch.setattr(tmux, "session_panes", lambda name: [])
    monkeypatch.setattr(tmux, "list_sessions", lambda: [])

    if ws is not None:
        assert sessions.prune_scratch(db) == 0
        assert db.get_workspace(ws.id) is not None
    else:
        pool.sweep(db, str(repo))
    assert Path(path).is_dir()


@pytest.mark.parametrize("cleanup,child,attached", [
    ("retired", None, False),
    ("retired", "vite", False),
    ("retired", "pytest", False),
    ("retired", None, True),     # someone sits in its shell pane
    ("post-merge", None, False),
    ("post-merge", "vite", False),
])
def test_a_command_in_the_workspaces_own_pane_keeps_its_worktree(db, repo, monkeypatch, cleanup, child,
                                                                 attached):
    """What the removal ends (the pane's own process when its session is
    killed, the agents the pipeline stops) is excused; a dev server or test
    run started in the workspace's own pane is not, though its environment
    (TMUX=, a PWD in the worktree) and its paths there look like brindle's."""
    ws = workspaces.create(db, str(repo), "shipped").workspace
    Path(ws.path, "a.txt").write_text("a\n")
    sh("git add a.txt && git commit -qm a", ws.path)
    sh(f"git merge -q --no-ff -m m {ws.branch}", repo)
    wt = os.path.realpath(ws.path)
    keep = None
    if cleanup == "post-merge":   # the worker's CLI in the pane; the approving reviewer runs this
        mcp = json.dumps({"mcpServers": {"brindle": {"env": {
            "BRINDLE_AGENT_ID": "w1", "BRINDLE_HOME": os.environ.get("BRINDLE_HOME")}}}})
        pane = f"claude --mcp-config {mcp}"
        db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "waiting",
                           "@998", None, time.time()))
        keep = Agent("r1", ws.id, "reviewer", "claude", None, "review", "done", "@999", None, time.time())
        db.add_agent(keep)
    else:
        pane = "-zsh"
        db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "done",
                           "@999", None, time.time() - 7200))
    command = {"vite": f"node {wt}/node_modules/.bin/vite",
               "pytest": f"{wt}/.venv/bin/python3 {wt}/.venv/bin/pytest -q"}.get(child)
    rows = [(500, 1, pane)] + ([(501, 500, command)] if command else [])
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
    monkeypatch.setattr(procs, "cwds", lambda: {pid: wt for pid, _ppid, _cmd in rows}
                        | {os.getpid(): os.getcwd()})
    real_panes, real_sessions = tmux.session_panes, tmux.list_sessions
    monkeypatch.setattr(tmux, "session_panes", lambda s: [(500, wt)] if s == ws.tmux_session else real_panes(s))
    monkeypatch.setattr(tmux, "list_sessions", lambda: real_sessions() + [(ws.tmux_session, attached)])
    stopped = []
    monkeypatch.setattr(agents, "is_alive", lambda a, *rest, **kw: a.id == "w1" and a.status == "waiting"
                        and "w1" not in stopped)
    monkeypatch.setattr(agents, "_stop", lambda db, a: stopped.append(a.id))

    if cleanup == "post-merge":
        pipeline._remove(db, ws, keep=keep)
    else:
        cull.prune_retired(db)

    removed = child is None and not attached
    assert Path(ws.path).is_dir() == (not removed)
    assert (db.get_workspace(ws.id) is None) == removed
    assert stopped == (["w1"] if removed and keep else [])


def test_abandoned_worktree_is_only_listed_until_cleaned(db, repo, monkeypatch):
    wt = _worktree(repo, "stalled", days_old=30)
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


@pytest.mark.parametrize("failure", ["proc-unreadable", "lsof-partial"])
def test_a_partial_process_scan_keeps_every_worktree(db, repo, tmp_path, monkeypatch, failure):
    """One live process whose folder can't be read (or an lsof that failed
    part way) must not read as no process at all."""
    wt = _worktree(repo, "idea", days_old=30)
    fake = tmp_path / "proc"
    (fake / "self").mkdir(parents=True)
    monkeypatch.setattr(procs, "PROC", fake)
    if failure == "proc-unreadable":
        os.symlink("/", fake / "self" / "cwd")
        for pid, cwd in (("100", "/"), ("200", str(wt))):
            (fake / pid).mkdir()
            os.symlink(cwd, fake / pid / "cwd")
        (fake / "300").mkdir()               # exited while the scan ran: no cwd left
        (fake / "200").chmod(0)              # live, but its folder can't be read
    else:
        listing = subprocess.CompletedProcess([], 1, stdout=f"p{os.getpid()}\nfcwd\nn/\np200\n",
                                              stderr="lsof: can't stat a process")
        monkeypatch.setattr(spawn, "read", lambda *a, **kw: listing)
    try:
        with pytest.raises(OSError):
            procs.cwds()
        out = disk.clean(db, path=str(wt))
    finally:
        if (fake / "200").exists():
            (fake / "200").chmod(0o755)

    assert out["removed"] == [] and wt.is_dir()


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


def test_merged_and_squash_merged_branches_go_with_their_workspace(db, repo):
    plain = workspaces.create(db, str(repo), "plain").workspace
    squashed = workspaces.create(db, str(repo), "squashed").workspace
    for ws in (plain, squashed):
        Path(ws.path, f"{ws.name}.txt").write_text("work\n")
        sh(f"git add -A && git commit -qm {ws.name} && git commit -q --allow-empty -m more", ws.path)
    sh(f"git merge -q --no-ff {plain.branch}", repo)
    sh(f"git merge -q --squash {squashed.branch} && git commit -qm 'Squash merge'", repo)
    tip = sh(f"git rev-parse {squashed.branch}", repo).strip()

    for ws in (plain, squashed):
        assert workspaces.remove(db, ws).branch_deleted

    assert sh("git branch --list plain squashed", repo).strip() == ""
    assert _archived(repo, "squashed") == tip and _archived(repo, "plain")


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


def test_a_branch_that_moves_before_deletion_is_kept(db, repo, monkeypatch):
    wt = _worktree(repo, "stalled", days_old=30)
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


def test_a_branch_that_moved_on_after_its_merge_is_not_swept(db, repo):
    ws = workspaces.create(db, str(repo), "again").workspace
    Path(ws.path, "a.txt").write_text("a\n")
    sh("git add a.txt && git commit -qm a", ws.path)
    sh(f"git merge -q --no-ff -m m {ws.branch}", repo)
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
    running, raced = git.run, []

    def someone_pushes_first(args, *rest, **kw):
        if args[0] == "push" and not raced:
            raced.append(sh(f"git push -q origin {newer}:refs/heads/brindle/session-1", repo))
        return running(args, *rest, **kw)

    monkeypatch.setattr(git, "run", someone_pushes_first)

    assert disk.delete_pushed_remote(ws, sha) is None
    assert sh("git ls-remote --heads origin brindle/session-1", repo).split()[0] == newer
    assert disk.delete_pushed_remote(ws, newer)
    assert sh("git ls-remote --heads origin brindle/session-1", repo).strip() == ""


def test_spotlight_is_kept_out_of_brindle_dirs(db, repo, monkeypatch):
    monkeypatch.setattr(disk.sys, "platform", "darwin")

    root = workspaces.new_worktree_root(str(repo))
    workspaces.new_worktree_root(str(repo))

    assert (root / disk.NEVER_INDEX).is_file()
    assert (config.brindle_home() / disk.NEVER_INDEX).is_file()
