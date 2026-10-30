"""The inbox lists no plan card that its approve/reject action would refuse."""

import time

import pytest

from brindle import agents, workspaces
from brindle.db import Agent
from brindle.service import inbox


@pytest.mark.parametrize("parent, plan_first", [
    ("r1", None),   # not a plan-first worker: approve_plan refuses it
    (None, 1),      # no supervisor to decide: approve_plan refuses it
])
def test_items_lists_no_plan_the_action_refuses(db, repo, parent, plan_first):
    ws = workspaces.create(db, str(repo), "feat/stale").workspace
    now = time.time()
    db.add_agent(Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, now))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", parent, "assign", "idle", "@2", None, now))
    db.update_agent("w1", plan_state="proposed", plan_text="1. add the column", plan_first=plan_first)
    plans = [i for i in inbox.items(db, str(repo), panes={"@1": True, "@2": True}) if i.kind == "plan"]
    for item in plans:   # whatever is listed must be actionable
        a = db.get_agent(item.agent_id)
        agents.approve_plan(db, a.parent_id, a.id)
    assert plans == []
