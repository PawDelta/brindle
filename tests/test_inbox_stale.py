"""The inbox lists no plan card that its approve/reject action would refuse."""

import json
import time

import pytest

from brindle import agents, bus, permissions, workspaces
from brindle.daemon.holds import HOLDS
from brindle.db import Agent, Workspace
from brindle.service import inbox
from brindle.service.errors import ServiceError


@pytest.mark.parametrize("handled", ["terminal", "killed"])
@pytest.mark.variant("0.2.13 stale Inbox: handling an item without an agent status transition")
def test_handling_inbox_items_notifies_connected_apps(db, tmp_path, monkeypatch, handled):
    from brindle import procs
    from brindle.daemon.routes_actions import inbox_action, kill_agent

    repo = str(tmp_path)
    ws = Workspace("w", repo, "handled", "worktree", "feat/handled", "main", repo, None, "s", time.time())
    db.add_workspace(ws)
    a = Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, time.time())
    db.add_agent(a)
    if handled == "terminal":
        req = permissions.Request("claude", "bash", "Bash", command="git status")
        db.add_permission_request(a.id, "q1", json.dumps(req.to_dict()), "", None, "prompt")
    if handled == "checks":
        db.add_autopilot(a.id)
        db.update_autopilot(a.id, proposal=json.dumps({"checks": ["true"], "at": time.time()}))
    if handled == "killed":
        db.add_agent(Agent("root", ws.id, "supervisor", "claude", None, "interactive", "idle", "@0", None, time.time()))
        db.update_agent(a.id, status="waiting", parent_id="root")
    before = db.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    if handled == "terminal":
        agents.note_permission_outcome(db, a, {"tool_name": "Bash", "tool_input": {"command": "git status"}})
        assert db.permission_requests_of(a.id) == []
    elif handled == "closed":
        monkeypatch.setattr(procs, "stop", lambda ids: None)
        agents.close(db, a.id, panes={})
        assert db.get_agent(a.id).dismissed_at is not None
    elif handled == "killed":
        monkeypatch.setattr(agents, "_stop", lambda db, agent: None)
        kill_agent(db, {"id": a.id}, {})
        assert db.get_agent(a.id) is None
    else:
        inbox_action(db, {"id": a.id, "action": "decline"}, {})
        assert db.get_autopilot(a.id).proposal is None
    events = bus.tail(db, before, repo_root=repo)
    assert events
    if handled == "killed":
        assert (events[-1]["kind"], events[-1]["agent_id"], events[-1]["workspace_id"], events[-1]["root_id"]) == (
            "agent.closed", a.id, ws.id, "root")


@pytest.mark.parametrize("claimed, registered_digest, listed", [
    pytest.param(False, None, True, marks=pytest.mark.variant("a live unclaimed permission hold must remain actionable")),
    pytest.param(True, None, False, marks=pytest.mark.variant("an already claimed permission hold cannot be listed or answered again")),
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
    pytest.param("r1", None, marks=pytest.mark.variant("a stale proposed plan on a non-plan-first worker cannot offer an unusable action")),
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
