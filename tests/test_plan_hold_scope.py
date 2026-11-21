"""What an open planning doc holds (brindle.tasks.plan_holds): only its own
work, tasks linked to its split or to a goal milestone. Other work the
supervisor declares with assign's outside_plan runs at once, alongside the
plan, where the person can stop it. Runs through the real assign, hold and
dispatch code."""

import asyncio

from starlette.testclient import TestClient

from brindle import mcp_server, plans, tasks, visual_offers
from brindle.daemon.app import build_app
from test_plans import AUTH, ONE, PLAN, TOKEN, _shared, library, messages, session  # noqa: F401 - fixtures

SPLIT = [{"id": "schema", "title": "Schema", "test": "uv run pytest tests/test_presets.py -q"}]
HOTFIX = "fix the failing release build"


def _open_plan(db, boss) -> str:
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    return mcp_server.propose_plan(PLAN, split=SPLIT).split()[2]


def test_unrelated_work_with_a_reason_starts_while_the_plan_is_open(db, session):
    boss, started = session
    pid = _open_plan(db, boss)

    reply = asyncio.run(mcp_server.assign("developer", "bump the release version", outside_plan=HOTFIX))

    assert started == ["bump the release version"] and pid in reply
    [row] = plans.to_dto(db, plans.get(db, pid))["alongside"]
    assert (row["title"], row["reason"], row["state"], row["can_stop"]) == (
        "bump the release version", HOTFIX, "working", True)


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


def test_an_assign_with_no_link_and_no_reason_is_refused(db, session):
    boss, started = session
    pid = _open_plan(db, boss)

    reply = asyncio.run(mcp_server.assign("developer", "bump the release version"))

    assert reply.startswith("Not started") and "outside_plan" in reply and pid in reply
    assert db.list_tasks() == [] and started == []


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
