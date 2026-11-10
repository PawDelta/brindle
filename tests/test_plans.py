"""Planning docs (brindle.plans): made only when the person asks, assign
queued behind an open plan through the real dispatch, and only the person
approves or redirects."""

import asyncio
import sqlite3
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from brindle import agents, mcp_server, plans, tasks, visual_offers
from brindle.daemon.app import build_app
from brindle.db import DB, Agent, Workspace
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
    # The reader's optional data: the restated ask, each option's trade-offs and files, each worker's files and test.
    cards = [{**DECISIONS[0], "options": [{**DECISIONS[0]["options"][0], "pros": ["One place", " "],
                                           "cons": ["Admins only"], "touches": ["src/brindle/pro/policy.py"]},
                                          DECISIONS[0]["options"][1]]}]
    out = mcp_server.propose_plan(PLAN, cards, [{"id": "w1", "title": "Schema", "files": ["src/brindle/db.py"],
                                                 "test": "tests/test_presets.py::test_schema"}],
                                  ask="Admins set presets once for their whole org.")
    pid = out.split()[2]
    assert plans.sections(PLAN)[1][0]["title"] == "Goal" and len(plans.sections(PLAN)[1]) == 2

    reply = asyncio.run(mcp_server.assign("developer", "build the preset schema"))
    assert "behind plan" in reply and started == []
    assert tasks.sweep(db) == 0 and started == []      # nothing wakes a held task

    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        got = client.get(f"/v1/plans/{pid}").json()
        assert got["queued"] == 1 and [s["id"] for s in got["sections"]] == ["s1", "s2"]
        assert got["ask"] == "Admins set presets once for their whole org."
        org, repo = got["decisions"][0]["options"]
        assert (org["pros"], org["cons"], org["touches"]) == (["One place"], ["Admins only"],
                                                               ["src/brindle/pro/policy.py"])
        assert (repo["pros"], repo["touches"]) == ([], [])
        assert (got["split"][0]["files"], got["split"][0]["test"]) == (["src/brindle/db.py"],
                                                                       "tests/test_presets.py::test_schema")
        # Redirect: the person's pick and comments (on a section, the ask, a decision) reach the supervisor.
        r = client.post(f"/v1/plans/{pid}/decide", json={
            "rev": 1, "approved": False, "picks": {"d1": "repo"},
            "comments": [{"section": "s1", "text": "Repo file first; org policy later."},
                         {"section": "ask", "text": "Teams, not the whole org."},
                         {"section": "decision:d1", "text": "Policy needs Pro."}]})
        assert r.json()["state"] == "redirected", r.json()
        said = messages(db)[-1]
        assert "chose repo" in said and "Repo file first" in said
        assert 'On "What I understood (your restated ask)": Teams' in said
        assert 'On "Decision d1 (Where presets live)": Policy needs Pro.' in said
        assert started == [] and tasks.sweep(db) == 0

        # The redirect allows exactly the one revision it asked for.
        mcp_server.propose_plan(PLAN + "\n\nRevised.", DECISIONS, plan_id=pid)
        with pytest.raises(ToolError):
            mcp_server.propose_plan(PLAN, DECISIONS, plan_id=pid)

        assert client.post(f"/v1/plans/{pid}/decide", json={"rev": 1, "approved": True}).status_code == 409

        # An approval that fails before its message is queued starts nothing,
        # even with a sweep (another connection) running meanwhile, and leaves the plan open.
        queue, other, swept = plans._queue_message, DB(), []

        def fail(c, root, text):
            swept.append(tasks.sweep(other))
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(plans, "_queue_message", fail)
        with pytest.raises(sqlite3.OperationalError):
            plans.decide(db, pid, 2, True, person=True)
        assert swept == [0] and started == [] and plans.get(db, pid)["state"] == "open"
        monkeypatch.setattr(plans, "_queue_message", queue)

        assert client.post(f"/v1/plans/{pid}/decide", json={"rev": 2, "approved": True}).json()["state"] == "approved"
        assert started == ["build the preset schema"]
        # A second approval (another window, a double click) and a sweep start nothing more.
        assert client.post(f"/v1/plans/{pid}/decide", json={"rev": 2, "approved": True}).status_code == 409
        assert tasks.sweep(db) == 0 and started == ["build the preset schema"]


@pytest.mark.parametrize("accept", [
    True,
    pytest.param(False, marks=pytest.mark.variant(
        "No must start the task queued behind the offer; the other offer tests answer with nothing queued")),
    pytest.param("legacy", marks=pytest.mark.variant(
        "an upgraded row: the retired analysis-doc offer's 'accepted' must neither hold work nor allow a plan")),
])
def test_big_goal_offers_a_planning_doc(accept, db, session, monkeypatch):
    """A big goal offers to plan it first. While that is open, assign queues;
    Yes grants the plan and keeps the task queued until the plan is approved,
    No starts it. An accepted analysis-doc offer from before does neither."""
    from brindle import autopilot
    from brindle.service import inbox

    boss, started = session
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    db.add_autopilot("boss")
    grants = lambda: [r["scope"] for r in db.conn.execute(  # noqa: E731
        "SELECT scope FROM visual_grants WHERE root_id='boss' AND remaining > 0")]
    milestones = [{"title": f"M{i}", "check": "true"} for i in range(4)]
    if accept == "legacy":
        db.update_autopilot("boss", goal="Org presets")
        with db.tx() as c:
            c.execute("INSERT INTO goal_doc_requests (root_id, requested, offer) VALUES ('boss', 1, 'accepted')")
        mcp_server.set_goal("Org presets", milestones)     # the same goal: no new offer
        assert not [i for i in inbox.items(db, None) if i.kind == "plan_offer"]
        asyncio.run(mcp_server.assign("developer", "build the preset schema"))
        assert started == ["build the preset schema"] and grants() == []
        with pytest.raises(ToolError):
            mcp_server.propose_plan(PLAN)
        return

    mcp_server.set_goal("Org presets", milestones)
    [offer] = [i for i in inbox.items(db, None) if i.kind == "plan_offer"]
    assert (offer.root_id, offer.request_id) == ("boss", plans.open_offer_id(db.get_autopilot("boss").plan_offer))
    assert [m.title for m in offer.plan_offer.milestones] == [m["title"] for m in milestones]

    asyncio.run(mcp_server.assign("developer", "build the preset schema"))
    assert [t.task_text for t in db.list_tasks(state="pending")] == ["build the preset schema"]
    assert tasks.sweep(db) == 0 and started == []
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        answer = {"root": "boss", "offer": offer.request_id, "accept": accept}
        assert client.post("/v1/goal/plan-offer", json=answer).status_code == 200
        assert client.post("/v1/goal/plan-offer", json=answer).status_code == 409   # answered once
        assert not [i for i in inbox.items(db, None) if i.kind == "plan_offer"]
        assert messages(db)                                  # the supervisor is told either way
        if not accept:
            assert started == ["build the preset schema"] and grants() == []
            return
        assert grants() == ["plan"]
        assert tasks.sweep(db) == 0 and started == []       # Yes: it waits for the plan now
        pid = mcp_server.propose_plan(PLAN).split()[2]      # the Yes is the person's ask
        assert started == [] and plans.holding(db, "boss")["id"] == pid
        assert client.post(f"/v1/plans/{pid}/decide", json={"rev": 1, "approved": True}).json()["state"] == "approved"
    assert started == ["build the preset schema"]


def test_a_failed_verdict_leaves_nothing_behind_and_a_failed_push_loses_nothing(db, session, monkeypatch):
    boss, started = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN, DECISIONS).split()[2]
    asyncio.run(mcp_server.assign("developer", "build the preset schema"))
    redirect = {"comments": [{"section": "s1", "text": "Repo file first."}], "person": True}
    grants = lambda: [r["remaining"] for r in db.conn.execute("SELECT remaining FROM visual_grants")]  # noqa: E731

    # One failure inside the verdict's transaction: none of it lands, and the
    # same process can decide again at once.
    queue = plans._queue_message

    def locked(c, root, text):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(plans, "_queue_message", locked)
    with pytest.raises(sqlite3.OperationalError):
        plans.decide(db, pid, 1, False, **redirect)
    assert plans.get(db, pid)["state"] == "open" and plans.get(db, pid)["visual"]["answer"] is None
    assert grants() == [0] and messages(db) == []
    monkeypatch.setattr(plans, "_queue_message", queue)

    # The push to the supervisor's terminal failing after it doesn't undo the verdict: the message stays queued.
    def unreachable(db_, agent_id):
        raise agents.AgentError("not running")
    monkeypatch.setattr(agents, "flush", unreachable)
    assert plans.decide(db, pid, 1, False, **redirect)["state"] == "redirected"
    assert plans.get(db, pid)["visual"]["answer"]["option"] == "redirect" and grants() == [1]
    assert len(messages(db)) == 1 and "Repo file first." in messages(db)[0]
    with pytest.raises(ServiceError, match="already decided"):
        plans.decide(db, pid, 1, True, person=True)

    mcp_server.propose_plan(PLAN + "\n\nRevised.", DECISIONS, plan_id=pid)
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
