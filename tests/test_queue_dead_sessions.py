"""A queued task is never held behind, or cancelled by, another session that
can't make progress (paused, locked to another sign-in, closed, long idle)."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from brindle import config, gates, git, identity_lock, tasks
from brindle.db import Agent, Task, Workspace


@pytest.fixture
def sessions(db, monkeypatch):
    """Session a's pending task on src/shared.py, which session b's branch
    has changed; b's earlier worker ``bw`` merged. Started through the real
    dispatch path (``_start_if_ready``) with git, sign-ins and spawn faked."""
    def session(name, branch, status="processing"):
        db.add_workspace(Workspace(name, "/repo", name, "worktree", branch, "main", f"/repo/{name}", None, "", 0))
        db.add_agent(Agent(name, name, "supervisor", "claude", None, "interactive", status, "", None, 0))

    session("a", "session-a")
    session("b", "session")
    db.add_workspace(Workspace("bw", "/repo", "bw", "worktree", "b-feature", "session", "/repo/bw", None, "", 0))
    db.add_agent(Agent("bw", "bw", "developer", "claude", "b", "assign", "done", "", "done", 0))
    db.add_task(Task("bw-task", "/repo", "bw", "b", "b", "developer", "b's work", "assign", 1,
                     "b-feature", None, None, None, "merged", 0))
    # An old in-place worker of b's, closed long ago: b's own state decides.
    db.add_agent(Agent("old", "b", "developer", "claude", "b", "assign", "done", "", "done", 0))
    db.update_agent("old", dismissed_at=1.0)
    refs ={"refs/heads/main": "m", "refs/heads/session-a": "m", "refs/heads/session": "b-head"}
    monkeypatch.setattr(git, "ref_shas", lambda root: refs)
    monkeypatch.setattr(git, "default_branch", lambda root: "main")
    monkeypatch.setattr(git, "merged_into", lambda *args, **kwargs: False)
    monkeypatch.setattr(git, "ok", lambda args, root: False)
    monkeypatch.setattr(git, "paths", lambda args, root: ["src/shared.py"]
                        if args[0] == "diff" and args[-1].endswith("...b-head") else [])
    monkeypatch.setattr(tasks, "_changed_files", lambda ws: [])
    monkeypatch.setattr(config, "load_repo_config",
                        lambda root: SimpleNamespace(overlap=None, max_weight=None, merge_into=None))
    locked = set()
    monkeypatch.setattr(identity_lock, "locked_by",
                        lambda agent_id: "claude org other" if agent_id in locked else None)
    monkeypatch.setattr(tasks, "task_wait", lambda db, t: None)
    monkeypatch.setattr(tasks, "_claim_fair", lambda db, t: (db.claim_task(t.id), None, None))
    monkeypatch.setattr(tasks, "_launch_warnings", lambda *args: [])
    monkeypatch.setattr(tasks.agents, "send_message", lambda *args, **kwargs: None)
    told = []
    monkeypatch.setattr(gates, "tell", lambda db, to, text: told.append(to))

    def delegate(db, caller, ws, *args, **kwargs):
        worker = replace(db.get_agent("bw"), id="started", parent_id="a", workspace_id="a", result=None)
        db.add_agent(worker)
        return worker, db.get_workspace("a")

    monkeypatch.setattr(tasks.agents, "delegate", delegate)

    def pending(text="new work", depends_on=()):
        db.add_task(Task("new", "/repo", None, "a", "a", "developer", text, "assign", 1, None, None,
                         tasks._dumps(["src/shared.py"]), tasks._dumps(list(depends_on)), "pending", 1))
        return lambda: tasks._start_if_ready(db, db.get_task("new"), "its dependencies", check_room=False)

    return SimpleNamespace(locked=locked, told=told, pending=pending, session=session, refs=refs)


# The incident: paused and locked to another account; then each stall alone,
# and a session holding the files on the branch it attached this repo with.
@pytest.mark.parametrize("stall", [
    pytest.param("paused-locked", marks=pytest.mark.variant("real incident: held 4 times behind a paused session locked to another account")),
    "locked", "closed", "idle-no-process",
    pytest.param("attached-paused", marks=pytest.mark.variant("input variant: a paused session someone is still attached to")),
])
def test_overlap_with_stalled_session_starts_with_warning(db, sessions, stall):
    holder = "workspace:b"
    if stall.startswith("attached"):
        db.conn.execute("UPDATE workspaces SET repo_root='/elsewhere' WHERE id='b'")
        db.add_session_repo("b", "attached", "/repo", "session")
        holder = "session"
    start = sessions.pending(depends_on=["bw"])
    # b is live: the task waits for b's branch to land.
    assert not start()
    assert tasks._loads(db.get_task("new").depends_on) == ["bw", holder]
    # b stalls: it neither holds a's task nor cancels it with its sign-in
    # (nor does its merged worker bw's), and the task starts while both
    # supervisors hear of the overlap.
    sessions.locked.add("bw")
    if "paused" in stall:
        db.set_status("b", "paused")
    if "locked" in stall:
        sessions.locked.add("b")
    if stall == "closed":
        db.update_agent("b", dismissed_at=tasks.time.time())
    if stall.startswith("idle"):
        db.update_agent("b", status="idle", status_since=tasks.time.time() - tasks.STALLED_AFTER)
    sessions.told.clear()
    assert start()
    assert db.get_task("new").state == "started"
    assert set(sessions.told) == {"a", "b"}


def test_stalled_session_never_hides_a_live_holder(db, sessions):
    # c is live, its reported but unmerged worker names issue #420.
    sessions.session("c", "session-c")
    db.add_workspace(Workspace("cw", "/repo", "cw", "worktree", "c-fix", "session-c", "/repo/cw", None, "", 0))
    db.add_agent(Agent("cw", "cw", "developer", "claude", "c", "assign", "done", "", "done", 0))
    db.add_task(Task("cw-task", "/repo", "cw", "c", "c", "developer", "fix #416 and #420", "assign", 1,
                     "c-fix", None, None, None, "started", 0))
    db.set_status("b", "paused")
    text = "After PR #416 merged (#417), fix #420"
    assert tasks._issue_numbers(text) == {"420"}   # history citations hold nothing
    start = sessions.pending(text)
    assert not start()
    assert tasks._loads(db.get_task("new").depends_on) == ["cw"]


def test_worker_stalling_after_its_hold_releases_the_task(db, sessions):
    # b's branch is off the file; its running worker bw2 holds it instead.
    sessions.refs["refs/heads/session"] = "b-clean"
    db.add_workspace(Workspace("bw2", "/repo", "bw2", "worktree", "b-next", "session", "/repo/bw2", None, "", 0))
    db.add_agent(Agent("bw2", "bw2", "developer", "claude", "b", "assign", "processing", "", None, 0))
    db.add_task(Task("bw2-task", "/repo", "bw2", "b", "b", "developer", "b's next", "assign", 1,
                     "b-next", None, tasks._dumps(["src/shared.py"]), None, "started", 0))
    start = sessions.pending()
    assert not start()
    assert tasks._loads(db.get_task("new").depends_on) == ["bw2"]
    # The worker pauses while b stays live: it won't merge, so a's task
    # starts, and both supervisors hear of the overlap though no scan finds it now.
    db.set_status("bw2", "paused")
    sessions.told.clear()
    assert start()
    assert db.get_task("new").state == "started"
    assert set(sessions.told) == {"a", "b"}
