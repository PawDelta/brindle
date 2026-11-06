"""The machine-wide worker limit shared fairly across sessions (brindle.fair_share),
as the start gate (tasks.task_wait) applies it, from database rows alone."""

import itertools

import pytest

from brindle import config, fair_share, spawn, tasks
from brindle.db import Agent, Task, Workspace

_ids = itertools.count()


@pytest.fixture
def rows(db, tmp_path, monkeypatch):
    """A workspace, the person's limit of 4 workers, and a machine with room."""
    monkeypatch.setattr(spawn, "no_room", lambda repo_root=None: None)
    config.set_user("machine_max_workers", 4)
    db.add_workspace(Workspace("w", str(tmp_path), "w", "worktree", "main", "main", str(tmp_path), None, "s", 1.0))
    return db


def session(db, root, running=0):
    db.add_agent(Agent(root, "w", "supervisor", "claude", None, "interactive", "processing", "@0", None, 1.0))
    for _ in range(running):
        db.add_agent(Agent(f"{root}-w{next(_ids)}", "w", "developer", "claude", root, "assign", "processing",
                           "@1", None, 1.0))


def queued(db, root, at):
    t = Task(f"{root}-t{next(_ids)}", db.get_workspace("w").repo_root, None, root, "w", "developer", "do",
             "assign", 1, None, None, None, None, "pending", at)
    db.add_task(t)
    db.update_task(t.id, waiting="no room", waiting_since=at)
    return db.get_task(t.id)


def test_fair_share_session_over_its_share_queues_while_another_has_work(rows):
    session(rows, "a", running=3)
    session(rows, "b")
    mine = queued(rows, "a", at=1.0)
    theirs = queued(rows, "b", at=2.0)
    assert tasks.task_wait(rows, mine) is not None   # one slot left, and b's queued work goes first
    assert tasks.task_wait(rows, theirs) is None


def test_fair_share_queued_tasks_start_round_robin_across_sessions(rows):
    session(rows, "a", running=2)
    session(rows, "b", running=1)
    session(rows, "c")
    a1, a2 = queued(rows, "a", at=1.0), queued(rows, "a", at=2.0)
    b1, c1 = queued(rows, "b", at=3.0), queued(rows, "c", at=4.0)
    assert fair_share.order(rows) == [c1.id, b1.id, a1.id, a2.id]
    assert [tasks.task_wait(rows, t) is None for t in (a1, a2, b1, c1)] == [False, False, False, True]


def test_fair_share_lone_session_may_use_every_slot(rows):
    session(rows, "a", running=3)
    t = queued(rows, "a", at=1.0)
    assert tasks.task_wait(rows, t) is None
    rows.add_agent(Agent("a-w-last", "w", "developer", "claude", "a", "assign", "processing", "@1", None, 1.0))
    assert tasks.task_wait(rows, t) is not None   # all 4 taken: even a lone session waits at the limit
