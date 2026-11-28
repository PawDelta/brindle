"""Leaving a still-empty plan through the app or the supervisor's MCP tool."""

import asyncio

import pytest
from starlette.testclient import TestClient

from brindle import mcp_server, plans, tasks, visual_offers
from brindle.daemon.app import build_app
from test_plans import AUTH, TOKEN, messages, session, work  # noqa: F401 - session fixture


@pytest.mark.parametrize("person", [
    pytest.param(True, id="app-cancel", marks=pytest.mark.variant("the person cancels a writing draft from the app")),
    pytest.param(False, id="supervisor-withdraw", marks=pytest.mark.variant(
        "the supervisor can close an empty draft through its own MCP tool")),
])
def test_writing_plan_can_close_without_starting_or_dropping_its_work(db, session, person):
    boss, started = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    draft = plans.holding(db, boss.id)
    asyncio.run(mcp_server.assign("developer", "build the schema", branch="feat/schema", **work(db)))
    [held] = db.list_tasks(state="pending")
    assert started == [] and tasks.open_plan_holds(db, held)

    if person:
        with TestClient(build_app(TOKEN), headers=AUTH) as client:
            response = client.post(f"/v1/plans/{draft['id']}/cancel", json={"rev": draft["rev"]})
        assert response.status_code == 200, response.json()
    else:
        mcp_server.withdraw_plan(draft["id"])

    assert plans.get(db, draft["id"])["state"] == "cancelled"
    assert plans.holding(db, boss.id) is None
    assert db.cancelled_hold(held.id) == draft["id"]
    assert tasks.sweep(db) == 0 and started == []
    assert db.get_task(held.id).state == "pending"
    opened, called_off = messages(db)
    assert draft["id"] in opened
    assert draft["id"] in called_off and held.id in called_off
    assert tasks.start_now(db, boss, held.id).startswith("Started")
    assert started == ["build the schema"]


@pytest.mark.variant("a misclassified request opens a draft whose notice must tell the supervisor how to withdraw it")
def test_misclassified_plan_request_notifies_supervisor_of_withdrawal(db, session):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "we need a plan B for the deploy")

    draft = plans.holding(db, boss.id)
    assert draft["state"] == "writing"
    [opened] = messages(db)
    assert draft["id"] in opened
    assert "withdraw_plan" in opened
