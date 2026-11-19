"""A session's goal outlives the session's rows: closing a paused supervisor
and then losing its rows (the leftover-worktree sweep removing its
worktree, or a kill) used to cascade its goal, milestones and their results
away for good (2026-11-18, a 40-milestone goal). They are archived instead,
and a session started again on the branch takes them back."""

import contextlib
import json
import threading
import time
from pathlib import Path

import pytest

from brindle import agents, autopilot as pilot, disk, git, pipeline, workspaces, worktree_cleanup
from brindle.db import DB, Agent, Workspace
from brindle.service import sessions as service

BRANCH = "brindle/session-18"
MILESTONES = [("Archive the goal", "true", None), ("Restore it on reopen", "false", "on the same branch")]
EDITED = "on the same branch, as edited by hand"


def _supervisor(db, ws, root_id: str) -> None:
    db.add_agent(Agent(root_id, ws.id, agents.SUPERVISOR, "claude", None, "interactive", "idle", "",
                       None, time.time()))
    pilot.enable(db, root_id, ws)


def _session_with_goal(db, repo, *, on_main: bool = False, commit: bool = True):
    if on_main:
        ws = workspaces.adopt_root(db, str(repo))
    else:
        ws = workspaces.create(db, str(repo), BRANCH, apply_prefix=False).workspace
        if commit:
            git.run(["commit", "-q", "--allow-empty", "-m", "session work"], ws.path)
    (Path(ws.path) / ".brindle").mkdir(exist_ok=True)   # the checkout's own config: its goal file sits in it
    _supervisor(db, ws, "c497329d")
    pilot.set_goal(db, "c497329d", "Keep goals when sessions close", MILESTONES, "the detail")
    first = db.milestones("c497329d")[0]
    db.record_check(first.id, True, "ok (exit 0)", "abc123")
    path = pilot.session_goals_path(db, "c497329d")
    assert path.is_relative_to(ws.path)
    # The person edits the file; the session takes that up on its next resume.
    path.write_text(path.read_text().replace("on the same branch", EDITED))
    return ws


@pytest.mark.parametrize("case", ["sweep", "kill", "sweep-no-commits", "reflog-expired", "main", "config-locked"])
def test_a_closed_sessions_goal_comes_back_when_its_branch_is_reopened(db, repo, monkeypatch, case):
    """Pause, then close a supervisor with milestones; its rows then go (the
    hourly sweep removes the closed session's worktree, or it is killed).
    Started again on the same branch (one with no commits of its own, one
    whose reflog has since expired, the default branch in the person's own
    checkout), the session has its goal back: the same milestones, checks
    and results, and its goal file with the person's edit, though that file
    was in the removed worktree. A sweep that can't mark the branch (its git
    config locked) keeps the session for a later one."""
    monkeypatch.setattr(agents, "is_alive", lambda *a, **kw: False)
    ws = _session_with_goal(db, repo, on_main=case == "main", commit=case != "sweep-no-commits")
    agents.pause(db, "c497329d", stop_procs=False, stop_local_models=False)
    service.close_session(db, "c497329d")
    if case == "config-locked":
        lock = Path(repo) / ".git" / "config.lock"
        lock.write_text("")
        worktree_cleanup.sweep_workspaces(db, time.time() + 2 * disk.FRESH)
        assert Path(ws.path).is_dir() and db.get_autopilot("c497329d").goal
        lock.unlink()
    if case in ("kill", "main"):
        agents.kill(db, "c497329d")
    else:
        worktree_cleanup.sweep_workspaces(db, time.time() + 2 * disk.FRESH)
        assert db.get_workspace(ws.id) is None and not Path(ws.path).exists()
    assert db.get_agent("c497329d") is None and git.branch_exists(str(repo), ws.branch)
    if case == "reflog-expired":
        git.run(["reflog", "expire", "--expire=all", f"refs/heads/{BRANCH}"], repo)

    reopened = workspaces.checkout_for(db, str(repo), branch=ws.branch)
    _supervisor(db, reopened, "7760dd04")

    ap = db.get_autopilot("7760dd04")
    assert ap.goal == "Keep goals when sessions close" and ap.detail == "the detail"
    ms = db.milestones("7760dd04")
    assert [(m.title, m.check_cmd, m.detail) for m in ms] == [MILESTONES[0], (*MILESTONES[1][:2], EDITED)]
    assert [(m.status, m.output, m.checked_sha) for m in ms] == [("passed", "ok (exit 0)", "abc123"),
                                                                ("pending", None, None)]
    assert pilot.parse_goals(pilot.session_goals_path(db, "7760dd04").read_text()) == pilot._db_plan(db, "7760dd04")
    # Taken back once: a second session on the branch starts without it.
    _supervisor(db, reopened, "second00")
    assert db.get_autopilot("second00").goal is None


def test_a_new_branch_that_only_reuses_the_name_starts_without_the_goal(db, repo, monkeypatch):
    """A session branch merged and deleted frees its name; a new session cut
    from the default branch under that name is not the old one reopened,
    even once nothing in its reflog says it was made since."""
    monkeypatch.setattr(agents, "is_alive", lambda *a, **kw: False)
    ws = _session_with_goal(db, repo)
    git.run(["merge", "-q", "--no-edit", BRANCH], repo)
    agents.kill(db, "c497329d")
    workspaces.remove(db, ws, force=True, delete_branch=True)
    assert not git.branch_exists(str(repo), BRANCH)

    fresh = workspaces.checkout_for(db, str(repo), branch=BRANCH)
    git.run(["reset", "-q", "--hard", "main"], fresh.path)
    git.run(["reflog", "expire", "--expire=all", f"refs/heads/{BRANCH}"], repo)
    _supervisor(db, fresh, "7760dd04")

    assert db.get_autopilot("7760dd04").goal is None and not db.milestones("7760dd04")


def test_two_sessions_of_a_branch_deleted_at_once_get_its_one_mark(db, repo, monkeypatch):
    """Two sessions of one branch deleted at the same time: the second's
    archive happens while the first is between reading that the branch has
    no mark and writing one. Both archives end up with the mark the branch
    keeps, so either can be taken back."""
    ws = workspaces.create(db, str(repo), BRANCH, apply_prefix=False).workspace
    for root in ("c497329d", "other000"):
        _supervisor(db, ws, root)
        pilot.set_goal(db, root, f"goal of {root}", MILESTONES)
    real_run, real_lock = git.run, pipeline._reentrant_lock
    other_started = threading.Event()
    first_read: list[bool] = []

    def delete_other() -> None:
        try:
            DB(db.path).delete_agent("other000")
        finally:
            other_started.set()

    other = threading.Thread(target=delete_other)

    def run(args, *a, **kw):
        out = real_run(args, *a, **kw)
        if args[:2] == ["config", "--get"] and threading.current_thread() is not other and not first_read:
            first_read.append(True)   # the first archive has read "no mark yet": the second starts now
            other.start()
            other_started.wait()
        return out

    @contextlib.contextmanager
    def lock(name, *a, **kw):
        if threading.current_thread() is other:
            other_started.set()   # it waits for the lock from here
        with real_lock(name, *a, **kw):
            yield

    monkeypatch.setattr(git, "run", run)
    monkeypatch.setattr(pipeline, "_reentrant_lock", lock)
    db.delete_agent("c497329d")
    other.join()

    mark = pilot._branch_mark(str(repo), BRANCH)
    marks = dict(db.conn.execute("SELECT root_id, branch_mark FROM goal_archives").fetchall())
    assert mark and marks == {"c497329d": mark, "other000": mark}


def test_archived_goals_are_never_evicted_by_later_ones(monkeypatch):
    """However many sessions of a repo are deleted after it, the first one's
    archived goal stays whole and can be taken back."""
    monkeypatch.setattr(pilot, "archive_extras", lambda db, root: {
        "repo_root": "/r", "branch": f"b-{root}", "goals_text": f"# goal of {root}\n"})
    db = DB(":memory:")
    db.add_workspace(Workspace("ws", "/r", "proj", "main", "main", None, "/r", None, "", 0))
    for n in range(205):
        root = f"s{n}"
        db.add_agent(Agent(root, "ws", agents.SUPERVISOR, "claude", None, "interactive", "paused", "",
                           None, time.time()))
        db.add_autopilot(root)
        db.update_autopilot(root, goal=f"goal {n}")
        db.set_milestones(root, MILESTONES)
        db.record_check(db.milestones(root)[0].id, True, "ok (exit 0)", "abc123")
        db.delete_agent(root)

    first = db.goal_archive_for("/r", "b-s0")
    assert first is not None and first.goals_text == "# goal of s0\n"
    assert [m["status"] for m in json.loads(first.goal)["milestones"]] == ["passed", "pending"]
    db.add_agent(Agent("new", "ws", agents.SUPERVISOR, "claude", None, "interactive", "idle", "", None, time.time()))
    db.add_autopilot("new")
    assert db.restore_goal_archive("s0", "new") and db.get_autopilot("new").goal == "goal 0"
