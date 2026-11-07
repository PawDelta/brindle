"""Planning docs (brindle.plans): made only when the person asks, assign
queued behind an open plan through the real dispatch, and only the person
approves or redirects."""

import asyncio
import os
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from brindle import agents, mcp_server, plans, tasks, visual_offers
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace
from brindle.service.errors import ServiceError

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PLAN = "# Org presets\n\nOne page.\n\n## Goal\n\nPresets per org.\n\n```\n## not a section\n```\n\n## Split\n\nTwo workers."
DECISIONS = [{"id": "d1", "title": "Where presets live", "mine": "org",
              "options": [{"id": "org", "label": "Org policy"}, {"id": "repo", "label": "A file in each repo"}]}]


@pytest.fixture
def session(db, tmp_path, monkeypatch):
    """A supervisor whose workers are recorded, not launched: assign runs the
    real queue and start (tasks.*), and agents.delegate makes the worker row."""
    monkeypatch.setenv("BRINDLE_VISUALS", "1")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr(agents, "is_alive", lambda agent, panes=None: True)
    ws = Workspace("ws1", str(tmp_path), "main", "main", "main", None, str(tmp_path), None, "brindle-ws1", time.time())
    db.add_workspace(ws)
    boss = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "%1", None, time.time(),
                 headless=1)
    db.add_agent(boss)
    started: list[str] = []

    def delegate(db_, caller, caller_ws, profile, task, mode, **kw):
        worker = Agent(f"w{len(started) + 1}", caller_ws.id, profile, "claude", caller.id, mode, "processing",
                       "%9", None, time.time())
        db_.add_agent(worker)
        started.append(task)
        return worker, caller_ws

    monkeypatch.setattr(agents, "delegate", delegate)
    return boss, started


def messages(db) -> list[str]:
    return [r["body"] for r in db.conn.execute("SELECT body FROM inbox WHERE agent_id='boss' ORDER BY id")]


def test_assign_queues_behind_open_plan_until_the_person_approves(db, session, monkeypatch):
    boss, started = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    out = mcp_server.propose_plan(PLAN, DECISIONS, [{"id": "w1", "title": "Schema"}])
    pid = out.split()[2]
    assert plans.sections(PLAN)[1][0]["title"] == "Goal" and len(plans.sections(PLAN)[1]) == 2

    reply = asyncio.run(mcp_server.assign("developer", "build the preset schema"))
    assert "behind plan" in reply and started == []
    assert tasks.sweep(db) == 0 and started == []      # nothing wakes a held task

    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        got = client.get(f"/v1/plans/{pid}").json()
        assert got["queued"] == 1 and [s["id"] for s in got["sections"]] == ["s1", "s2"]
        # Redirect: the person's pick and comment reach the supervisor; nothing starts.
        r = client.post(f"/v1/plans/{pid}/decide", json={
            "rev": 1, "approved": False, "picks": {"d1": "repo"},
            "comments": [{"section": "s1", "text": "Repo file first; org policy later."}]})
        assert r.json()["state"] == "redirected", r.json()
        assert "chose repo" in messages(db)[-1] and "Repo file first" in messages(db)[-1]
        assert started == [] and tasks.sweep(db) == 0

        # The redirect allows exactly the one revision it asked for.
        mcp_server.propose_plan(PLAN + "\n\nRevised.", DECISIONS, plan_id=pid)
        with pytest.raises(ToolError):
            mcp_server.propose_plan(PLAN, DECISIONS, plan_id=pid)

        assert client.post(f"/v1/plans/{pid}/decide", json={"rev": 1, "approved": True}).status_code == 409

        # An approval whose message can't be delivered starts nothing, even
        # with a sweep running while it's on its way, and leaves the plan open.
        deliver = agents.send_message

        def fail(*a, **k):
            assert tasks.sweep(db) == 0
            raise agents.AgentError("the supervisor's inbox is gone")
        monkeypatch.setattr(agents, "send_message", fail)
        assert client.post(f"/v1/plans/{pid}/decide", json={"rev": 2, "approved": True}).status_code >= 400
        assert started == [] and plans.get(db, pid)["state"] == "open"
        monkeypatch.setattr(agents, "send_message", deliver)

        assert client.post(f"/v1/plans/{pid}/decide", json={"rev": 2, "approved": True}).json()["state"] == "approved"
        assert started == ["build the preset schema"]
        # A second approval (another window, a double click) and a sweep start nothing more.
        assert client.post(f"/v1/plans/{pid}/decide", json={"rev": 2, "approved": True}).status_code == 409
        assert tasks.sweep(db) == 0 and started == ["build the preset schema"]


def test_a_verdict_in_flight_fences_out_others_until_its_process_is_gone(db, session, monkeypatch):
    boss, started = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN, DECISIONS).split()[2]
    asyncio.run(mcp_server.assign("developer", "build the preset schema"))
    deliver, clock = agents.send_message, time.time

    def slow(*a, **k):
        # An hour into this redirect's delivery, an approval comes in: refused, nothing starts.
        monkeypatch.setattr(plans.time, "time", lambda: clock() + 3600)
        with pytest.raises(ServiceError, match="already decided"):
            plans.decide(db, pid, 1, True, person=True)
        assert started == [] and tasks.sweep(db) == 0
        return deliver(*a, **k)
    monkeypatch.setattr(agents, "send_message", slow)
    plans.decide(db, pid, 1, False, comments=[{"section": "s1", "text": "Repo file first."}], person=True)
    monkeypatch.setattr(agents, "send_message", deliver)
    assert plans.get(db, pid)["state"] == "redirected" and plans.get(db, pid)["visual"]["answer"]["option"] == "redirect"
    assert [r["remaining"] for r in db.conn.execute("SELECT remaining FROM visual_grants")] == [1]

    # A claim left by a process that died mid-delivery is taken over; a live one never is.
    mcp_server.propose_plan(PLAN + "\n\nRevised.", DECISIONS, plan_id=pid)
    db.conn.execute("UPDATE plans SET state='deciding', claim=? WHERE id=?", (f"{os.getpid()}:x", pid))
    with pytest.raises(ServiceError, match="already decided"):
        plans.decide(db, pid, 2, True, person=True)
    db.conn.execute("UPDATE plans SET claim='999999:x' WHERE id=?", (pid,))
    monkeypatch.setattr(plans.procs, "alive", lambda p: p != 999999)
    assert plans.decide(db, pid, 2, True, person=True)["state"] == "approved"
    assert started == ["build the preset schema"]


def test_only_the_person_decides_and_only_when_they_asked(db, session):
    boss, started = session
    with pytest.raises(ToolError, match="ask for one"):
        mcp_server.propose_plan(PLAN)                    # nobody asked for a plan
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]

    assert mcp_server.approve_plan(pid).startswith("Not decided")
    with pytest.raises(ServiceError) as e:
        plans.decide(db, pid, 1, True, person=False)
    assert e.value.code == "forbidden"
    assert plans.get(db, pid)["state"] == "open" and messages(db) == []
    assert "behind plan" in asyncio.run(mcp_server.assign("developer", "anything")) and started == []
