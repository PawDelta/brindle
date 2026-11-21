import pytest
"""What an open planning doc holds (brindle.tasks.plan_holds): only its own
work, tasks linked to its split or to a goal milestone. Other work the
supervisor declares with assign's outside_plan runs at once, alongside the
plan, where the person can stop it. Runs through the real assign, hold and
dispatch code."""

import asyncio
import time

from starlette.testclient import TestClient

from brindle import mcp_server, plans, tasks, visual_offers
from brindle.daemon.app import build_app
from test_plans import AUTH, ONE, PLAN, TOKEN, _shared, library, messages, session  # noqa: F401 - fixtures

SPLIT = [{"id": "schema", "title": "Schema", "test": "uv run pytest tests/test_presets.py -q"}]
HOTFIX = "fix the failing release build"


def _open_plan(db, boss) -> str:
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    return mcp_server.propose_plan(PLAN, split=SPLIT).split()[2]


@pytest.mark.variant("unrelated work held behind an open plan: the owner's reported stall")
def test_unrelated_work_with_a_reason_starts_while_the_plan_is_open(db, session):
    boss, started = session
    pid = _open_plan(db, boss)

    reply = asyncio.run(mcp_server.assign("developer", "bump the release version", outside_plan=HOTFIX))

    assert started == ["bump the release version"] and pid in reply
    [row] = plans.to_dto(db, plans.get(db, pid))["alongside"]
    assert (row["title"], row["reason"], row["state"], row["can_stop"]) == (
        "bump the release version", HOTFIX, "working", True)


@pytest.mark.variant("the plan's own task released early alongside unrelated work")
def test_plan_worker_task_stays_held_while_alongside_work_runs(db, session):
    boss, started = session
    pid = _open_plan(db, boss)

    asyncio.run(mcp_server.assign("developer", "build the schema", plan_worker=f"{pid}:schema"))
    asyncio.run(mcp_server.assign("developer", "bump the release version", outside_plan=HOTFIX))

    [held] = db.list_tasks(state="pending")
    assert held.task_text == "build the schema" and tasks.plan_holds(db, held)
    assert tasks.sweep(db) == 0 and started == ["bump the release version"]
    # "Holding N tasks" counts only the plan's own work.
    assert plans.holding_count(db, boss.id, pid) == 1
    plans.decide(db, pid, 1, True, person=True)
    assert started == ["bump the release version", "build the schema"]


@pytest.mark.variant("plan work slipping past the hold with no link and no reason")
def test_an_assign_with_no_link_and_no_reason_is_refused(db, session):
    boss, started = session
    pid = _open_plan(db, boss)

    reply = asyncio.run(mcp_server.assign("developer", "bump the release version"))

    assert reply.startswith("Not started") and "outside_plan" in reply and pid in reply
    assert db.list_tasks() == [] and started == []


@pytest.mark.variant("a pending reviewer freezing the whole session, the owner's second stall")
def test_a_pending_reviewer_holds_only_the_shared_plans_work(db, session, library):
    boss, started = session
    pid = _open_plan(db, boss)
    _shared(db, pid, {"rev": 1, "reviewers": ONE, "sign_offs": []})
    assert plans.gate_shut(db, boss.id)

    asyncio.run(mcp_server.assign("developer", "bump the release version", outside_plan=HOTFIX))
    assert started == ["bump the release version"]
    assert "build gate is closed" in asyncio.run(
        mcp_server.assign("developer", "build the schema", plan_worker=f"{pid}:schema"))
    assert [t.task_text for t in db.list_tasks()] == ["bump the release version"]


@pytest.mark.variant("work for an older approved plan wrongly held by a newer open one")
def test_work_linked_to_an_older_plan_runs_alongside_the_open_one(db, session):
    boss, started = session
    old = _open_plan(db, boss)
    plans.decide(db, old, 1, True, person=True)
    pid = _open_plan(db, boss)

    # Linked to the approved plan, not the open one: it needs the reason like any other work.
    assert asyncio.run(mcp_server.assign("developer", "build the schema", plan_worker=f"{old}:schema")).startswith(
        "Not started")
    reply = asyncio.run(mcp_server.assign("developer", "build the schema", plan_worker=f"{old}:schema",
                                          outside_plan="finishes the approved presets plan"))

    assert started == ["build the schema"] and "behind plan" not in reply
    [task] = db.list_tasks()
    assert [r["task_id"] for r in plans.to_dto(db, plans.get(db, pid))["alongside"]] == [task.id]
    assert plans._board(db, plans.get(db, old))[0]["task_id"] == task.id     # its own board keeps the link


def test_stop_racing_a_start_removes_the_started_workspace_instead_of_cancelling(db, session, monkeypatch, tmp_path):
    """Dispatch claims the task between Stop's read of it as queued and its cancel: the start wins,
    and Stop removes the started worker's workspace rather than marking a live worker cancelled."""
    from brindle import agents, workspaces
    from brindle.db import Agent, Workspace

    boss, started = session
    pid = _open_plan(db, boss)

    def delegate(db_, caller, caller_ws, profile, task, mode, **kw):
        ws = Workspace("ws2", caller_ws.repo_root, "w1", "worktree", "feat/bump", "main", str(tmp_path / "w1"), None,
                       "brindle-ws2", time.time())
        db_.add_workspace(ws)
        db_.add_agent(Agent("w1", ws.id, profile, "claude", caller.id, mode, "processing", "%9", None, time.time()))
        started.append(task)
        return db_.get_agent("w1"), ws
    monkeypatch.setattr(agents, "delegate", delegate)
    removed: list[str] = []
    monkeypatch.setattr(workspaces, "remove", lambda db_, ws, **kw: removed.append(ws.id))
    monkeypatch.setattr(tasks, "start_in_background", lambda task_id: None)   # it waits for room
    asyncio.run(mcp_server.assign("developer", "bump the release version", outside_plan=HOTFIX))
    [queued] = db.list_tasks(state="pending")

    read, raced = db.get_task, []

    def read_then_start(task_id):
        t = read(task_id)
        if task_id == queued.id and t.state == "pending" and not raced:
            raced.append(task_id)
            assert tasks._start_if_ready(db, t, "room on the machine", check_room=False)
        return t                                    # Stop goes on with the row it read: queued
    monkeypatch.setattr(db, "get_task", read_then_start)
    plans.stop_alongside(db, pid, queued.id)

    assert started == ["bump the release version"] and read(queued.id).state == "started"
    assert removed == ["ws2"]


def test_stop_cancels_a_queued_task_running_alongside_the_plan(db, session, monkeypatch):
    boss, started = session
    pid = _open_plan(db, boss)
    monkeypatch.setattr(tasks, "start_in_background", lambda task_id: None)   # it waits for room
    asyncio.run(mcp_server.assign("developer", "bump the release version", outside_plan=HOTFIX))
    [queued] = db.list_tasks(state="pending")

    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        r = client.post(f"/v1/plans/{pid}/alongside/{queued.id}/stop", json={})
    assert r.status_code == 200, r.json()
    assert db.get_task(queued.id).state == "cancelled" and started == []
    [row] = r.json()["alongside"]
    assert (row["state"], row["can_stop"]) == ("cancelled", False)
    assert queued.id in messages(db)[-1]          # its supervisor is told, as for cancel_task
