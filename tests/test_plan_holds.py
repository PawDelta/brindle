"""A plan's work stays queued while no app can answer (brindle.plans.holding):
only an approval source (brindle.approval_sources) releases it."""

import asyncio

import pytest

from brindle import mcp_server, plans, tasks, visual_offers
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

    plans.decide(db, pid, 1, True, person=True)                # the person's approval releases it
    assert started == ["build the preset schema"]
