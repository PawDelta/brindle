"""A plan's work stays queued while no app can answer (brindle.plans.holding):
only an approval source (brindle.approval_sources) releases it."""

import asyncio

import pytest

from brindle import agents, mcp_server, plans, tasks, visual_offers
from brindle.approval_sources import PERSON_IN_APP
from brindle.daemon import holds
from test_plans import APP_CONNECTED, PLAN, session, work  # noqa: F401 - session is a fixture


@pytest.mark.variant("regression (review F3): plan work started unapproved whenever the app was closed")
def test_plan_work_stays_queued_while_the_app_is_absent(db, session, monkeypatch):  # noqa: F811
    boss, started = session
    monkeypatch.setattr(holds, "app_connected", APP_CONNECTED)   # the real check: no daemon runs here
    assert holds.app_connected() is False
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]

    reply = asyncio.run(mcp_server.assign("developer", "build the preset schema", **work(db)))
    assert "behind plan" in reply and started == []
    assert tasks.sweep(db) == 0 and started == []              # nothing wakes a held task
    tasks.on_resumed(db, boss.id)                              # nor does continuing the session
    assert started == [] and plans.holding(db, boss.id)["id"] == pid

    plans.decide(db, pid, 1, True, approval=PERSON_IN_APP)                # the person's approval releases it
    assert started == ["build the preset schema"]


def _policy_refuses(monkeypatch):
    monkeypatch.setattr(tasks, "_launch_refusal", lambda db, t: "the repo's policy changed meanwhile")


def _dependency_breaks(monkeypatch):
    def unmet(*args, **kwargs):
        raise agents.AgentError("its dependency can never merge")
    monkeypatch.setattr(tasks, "unmet_dependencies", unmet)


@pytest.mark.parametrize("step, refusal", [
    pytest.param("_launch_overlap", None, id="claim",
                 marks=pytest.mark.variant("review: a stale eligibility read let the claim start a held task")),
    pytest.param("_launch_overlap", _policy_refuses, id="policy-refusal",
                 marks=pytest.mark.variant("review: the policy refusal cancelled a task the close had held")),
    pytest.param("_release_cross_waits", _dependency_breaks, id="dependency-refusal",
                 marks=pytest.mark.variant("review: the dependency refusal cancelled a task the close had held")),
])
def test_a_hold_landing_after_the_eligibility_check_still_wins(db, session, monkeypatch, step, refusal):  # noqa: F811
    boss, started = session
    db.set_status(boss.id, "paused")
    asyncio.run(mcp_server.assign("developer", "build the preset schema"))
    [task] = db.list_tasks(state="pending")
    db.set_status(boss.id, "idle")
    original = getattr(tasks, step)

    def hold_lands(db_, t, ws):      # after the sweep read the task as free, before it claims or refuses it
        db.set_cancelled_hold(t.id, "v_000000000000")
        return original(db_, t, ws)

    monkeypatch.setattr(tasks, step, hold_lands)
    if refusal:
        refusal(monkeypatch)
    assert tasks.sweep(db) == 0 and started == [] and db.get_task(task.id).state == "pending"
