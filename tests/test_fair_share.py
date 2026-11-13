"""The machine-wide worker limit shared fairly across sessions (brindle.fair_share),
as the start gate (tasks.task_wait) and the claim (tasks._claim_fair) apply it,
from database rows alone."""

import itertools
import json

import pytest

from brindle import config, fair_share, spawn, tasks
from brindle.db import DB, Agent, Task, Workspace

_ids = itertools.count()


@pytest.fixture
def rows(db, tmp_path, monkeypatch):
    """Two repos' workspaces, the person's limit of 4 workers, and a machine with room."""
    monkeypatch.setattr(spawn, "no_room", lambda repo_root=None: None)
    config.set_user("machine_max_workers", 4)
    for name in ("w", "w2"):
        (tmp_path / name).mkdir()
        path = str(tmp_path / name)
        db.add_workspace(Workspace(name, path, name, "worktree", "main", "main", path, None, f"s-{name}", 1.0))
    return db


def session(db, root, running=0):
    db.add_agent(Agent(root, "w", "supervisor", "claude", None, "interactive", "processing", "@0", None, 1.0))
    for _ in range(running):
        db.add_agent(Agent(f"{root}-w{next(_ids)}", "w", "developer", "claude", root, "assign", "processing",
                           "@1", None, 1.0))


def queued(db, root, at, ws="w", depends_on=None):
    t = Task(f"{root}-t{next(_ids)}", db.get_workspace(ws).repo_root, None, root, ws, "developer", "do",
             "assign", 1, None, None, None, json.dumps(depends_on) if depends_on else None, "pending", at)
    db.add_task(t)
    db.update_task(t.id, waiting="no room", waiting_since=at)
    return db.get_task(t.id)


def _session_capacity(rows, monkeypatch, competing):
    config.forget_user("machine_max_workers")
    busy = [3.0]
    monkeypatch.setattr(spawn, "_cores", lambda: 4)
    monkeypatch.setattr(spawn, "_load", lambda: busy[0])
    session(rows, "a", running=3)
    mine = queued(rows, "a", at=1.0)
    if competing:
        session(rows, "b")
        theirs = queued(rows, "b", at=2.0)
        assert tasks.task_wait(rows, mine) is not None   # one slot left, and b's queued work goes first
        assert tasks.task_wait(rows, theirs) is None
    else:
        assert tasks.task_wait(rows, mine) is None
        rows.add_agent(Agent("a-w-last", "w", "developer", "claude", "a", "assign", "processing", "@1", None, 1.0))
        assert tasks.task_wait(rows, mine) is not None   # even a lone session waits at the limit
        busy[0] = 1.0
        assert tasks.task_wait(rows, mine) is None       # under half the cores busy: up to 8


@pytest.mark.variant("claim race: another repo takes the last slot after this task passes the start gate")
def test_fair_share_claim_decides_again_under_the_machine_lock(rows):
    """A's task passed the gate, then B's (another repo) took the last slot
    before A claimed: A's stale decision must not start a worker over the limit."""
    config.set_user("machine_max_workers", 2)
    session(rows, "a", running=1)
    session(rows, "b")
    late = queued(rows, "a", at=1.0)
    assert tasks.task_wait(rows, late) is None
    other = queued(rows, "b", at=2.0, ws="w2")
    assert tasks._claim_fair(rows, other)[0]
    claimed, _refused, why = tasks._claim_fair(rows, late)
    assert not claimed and why and rows.get_task(late.id).state == "pending"


@pytest.mark.variant("claim race: another connection links the worker between the worker and claim counts")
def test_fair_share_counts_a_claim_as_it_becomes_its_worker(rows, monkeypatch):
    """A's claimed task links its worker (another connection) between the
    count's read of the workers and of the claims: still one slot taken."""
    config.set_user("machine_max_workers", 1)
    session(rows, "a")
    session(rows, "b")
    first = queued(rows, "a", at=1.0)
    assert tasks._claim_fair(rows, first)[0]
    theirs = queued(rows, "b", at=2.0, ws="w2")
    read_workers, linked = rows.list_agents, []

    def workers_then_link(*args, **kwargs):
        found = read_workers(*args, **kwargs)
        if not linked:
            linked.append(True)
            other = DB()
            other.add_agent(Agent("a-worker", "w", "developer", "claude", "a", "assign", "processing", "@1",
                                  None, 1.0))
            other.update_task(first.id, agent_id="a-worker", state="started")
        return found

    monkeypatch.setattr(rows, "list_agents", workers_then_link)
    claimed, _refused, why = tasks._claim_fair(rows, theirs)
    assert linked and not claimed and why


@pytest.mark.parametrize("competing", [None,
    pytest.param(True, marks=pytest.mark.variant("a session over its fair share cannot consume a competing session's capacity")),
    pytest.param(False, marks=pytest.mark.variant("a lone session can use otherwise idle machine capacity")),
], ids=["round-robin", "over-share", "lone-session"])
def test_fair_share_queued_tasks_start_round_robin_across_sessions(rows, monkeypatch, competing):
    if competing is not None:
        _session_capacity(rows, monkeypatch, competing)
        return
    config.set_user("machine_max_workers", 1)
    session(rows, "a")
    session(rows, "b")
    session(rows, "c")
    a1, a2 = queued(rows, "a", at=1.0), queued(rows, "a", at=2.0)
    b1 = queued(rows, "b", at=3.0)
    # Older, but a dependency added since it began waiting holds it: it must not take a turn it can't use.
    queued(rows, "c", at=0.5, depends_on=[f"task:{b1.id}"])
    # Nor may one on a branch brindle doesn't track as a task, which git says isn't merged yet,
    # or can't tell (and that failure must not hold up anyone else's start).
    w = rows.get_workspace("w")
    for branch in ("ext", "broken"):
        rows.add_workspace(Workspace(branch, w.repo_root, branch, "worktree", branch, "main", f"{w.path}-{branch}",
                                     None, f"s-{branch}", 1.0))

    def ancestry(args, cwd=None):
        if args[0] == "merge-base" and "broken" in args:
            raise tasks.git.GitError("git is busy")
        return args[0] != "merge-base"

    monkeypatch.setattr(tasks.git, "ok", ancestry)
    queued(rows, "c", at=0.4, depends_on=["ext"])
    queued(rows, "c", at=0.3, depends_on=["broken"])
    assert fair_share.order(rows) == [a1.id, b1.id, a2.id]
    assert tasks._claim_fair(rows, a1)[0]
    rows.add_agent(Agent("a1-worker", "w", "developer", "claude", "a", "assign", "processing", "@1", None, 1.0))
    rows.update_task(a1.id, agent_id="a1-worker", state="started")
    rows.update_agent("a1-worker", status="done")   # a's worker finished: b's turn, not a's older backlog
    assert [tasks.task_wait(rows, t) is None for t in (a2, b1)] == [False, True]
