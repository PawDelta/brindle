"""Cancelling a planning doc (brindle.plans.cancel): the plan closes without
approval, and the tasks it held stay queued until the supervisor decides each
one (start_now or cancel_task). Runs through the real hold and dispatch code."""

import asyncio

import pytest
from starlette.testclient import TestClient

from brindle import mcp_server, plans, tasks, visual_offers
from brindle.daemon.app import build_app
from test_plans import AUTH, ONE, PLAN, TOKEN, _shared, messages, session, library  # noqa: F401 - fixtures


@pytest.mark.parametrize("how", [
    pytest.param("person", marks=pytest.mark.variant(
        "the person's Cancel from the app, through the daemon route")),
    pytest.param("supervisor", marks=pytest.mark.variant(
        "the supervisor's own withdraw, through the MCP tool: the same effect")),
])
def test_cancelled_plan_keeps_its_held_tasks_queued_and_names_them(how, db, session, monkeypatch):
    boss, started = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    asyncio.run(mcp_server.assign("developer", "build the schema", branch="feat/schema"))
    [held] = db.list_tasks(state="pending")
    assert started == [] and tasks.plan_holds(db, held)

    if how == "person":
        with TestClient(build_app(TOKEN), headers=AUTH) as client:
            r = client.post(f"/v1/plans/{pid}/cancel", json={"rev": 1})
        assert r.status_code == 200 and r.json()["state"] == "cancelled", r.json()
    else:
        assert "Withdrew" in mcp_server.withdraw_plan(pid)
    assert plans.get(db, pid)["state"] == "cancelled"
    assert plans.holding(db, boss.id) is None            # a cancelled plan is no longer a hold

    # The held task neither starts nor is dropped on its own, however the sweep runs.
    assert tasks.sweep(db) == 0 and started == []
    assert [t.id for t in db.list_tasks(state="pending")] == [held.id]
    assert tasks.plan_holds(db, db.get_task(held.id))

    said = messages(db)[-1]
    assert held.id in said and "feat/schema" in said and "build the schema" in said


def test_start_now_starts_the_held_task_and_cancel_task_gives_up_the_other(db, session, monkeypatch):
    boss, started = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    asyncio.run(mcp_server.assign("developer", "build the schema"))
    asyncio.run(mcp_server.assign("developer", "build the UI"))
    keep, drop = sorted(db.list_tasks(state="pending"), key=lambda t: t.created_at)
    plans.cancel(db, pid, 1, person=True)

    assert tasks.start_now(db, boss, keep.id).startswith("Started")
    assert started == ["build the schema"]
    assert tasks.cancel(db, boss, drop.id).startswith("Cancelled")
    assert db.get_task(drop.id).state == "cancelled"
    assert tasks.sweep(db) == 0 and started == ["build the schema"]


@pytest.mark.parametrize("review, refused", [
    pytest.param({"rev": 1, "reviewers": ONE, "sign_offs": []}, None, id="required-reviewer-unsigned",
                 marks=pytest.mark.variant("a required reviewer who hasn't signed off doesn't stop a cancel")),
    pytest.param({"rev": 1, "reviewers": [], "sign_offs": []}, plans.ACCESS_REFUSED, id="library-refused-access",
                 marks=pytest.mark.variant("the Library refusing access to the plan's doc doesn't stop a cancel")),
])
def test_shared_plan_cancels_without_its_review_and_is_unshared(review, refused, db, session, library, monkeypatch):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    _shared(db, pid, review)
    if refused:
        with db.tx() as c:
            c.execute("UPDATE plan_shares SET access_refused=? WHERE plan_id=?", (refused, pid))

    def never_to_library(*args, **kwargs):
        raise AssertionError("a cancelled plan isn't sent to the Library")
    monkeypatch.setattr(plans, "_to_library", never_to_library)

    assert plans.cancel(db, pid, 1, person=True)["state"] == "cancelled"
    assert db.conn.execute("SELECT 1 FROM plan_shares WHERE plan_id=?", (pid,)).fetchone() is None
    assert [r["doc_id"] for r in db.conn.execute("SELECT doc_id FROM plan_unshares")] == ["doc"]
