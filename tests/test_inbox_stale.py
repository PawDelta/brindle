"""The inbox lists no plan card that its approve/reject action would refuse."""

import json
import time

import pytest

from brindle import agents, permissions, workspaces
from brindle.daemon.holds import HOLDS
from brindle.db import Agent
from brindle.service import inbox
from brindle.service.errors import ServiceError


@pytest.mark.parametrize("claimed, registered_digest, listed", [
    (False, None, True),     # a live hold nobody answered: listed
    (True, None, False),     # its answer is already claimed: answer_permission refuses it
    (False, "other", False),  # the hook registered a different request: refused
])
def test_items_lists_no_hook_request_the_answer_refuses(db, repo, claimed, registered_digest, listed):
    ws = workspaces.create(db, str(repo), "feat/hold").workspace
    db.add_agent(Agent("w1", ws.id, "developer", "codex", None, "interactive", "idle", "@2", None, time.time()))
    request = json.dumps(permissions.Request("codex", "bash", "Bash").to_dict())
    digest = permissions.request_digest(request, None)
    until = time.time() + 600
    db.add_permission_request("w1", "r1", request, "asking", None, "hook", hold_until=until)
    try:
        assert HOLDS.register("w1", "r1", registered_digest or digest, "n", until)
        if claimed:
            assert HOLDS.claim("w1", "r1", digest, "allow_once")
        shown = [i for i in inbox.items(db, str(repo), panes={"@2": True}) if i.kind == "permission"]
        assert [i.permission.request_id for i in shown] == (["r1"] if listed else [])
        if listed:   # the claim is the step that refuses
            assert HOLDS.claim("w1", "r1", shown[0].permission.digest, "allow_once")
        else:
            with pytest.raises(ServiceError):
                inbox.answer_permission(db, "w1", "r1", digest, "allow_once")
    finally:
        HOLDS.drop_agent("w1")


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
