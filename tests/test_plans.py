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
from brindle.daemon import events
from brindle.daemon.app import build_app
from brindle.db import DB, Agent, Workspace
from brindle.service.errors import ServiceError
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as

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
    monkeypatch.setattr(events, "connected", 1)
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


def test_plan_board_links_dispatch_and_reads_recorded_checks(db, session, monkeypatch):
    from dataclasses import replace
    from brindle import gates

    boss, started = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    check = "uv run pytest tests/test_presets.py -q"
    pid = mcp_server.propose_plan(PLAN, split=[
        {"id": "schema", "title": "Schema", "test": check},
        {"id": "ui", "title": "UI", "test": "npx vitest run presets"},
    ]).split()[2]
    with monkeypatch.context() as transport:
        transport.setattr(mcp_server._Request, "answer", mcp_server._Request.cancel)
        asyncio.run(mcp_server.assign("developer", "schema", plan_worker=f"{pid}:schema"))
    cancelled = db.list_tasks()[0]
    assert cancelled.state == "cancelled"
    link_plan = mcp_server._Request.link_plan

    def competing_assignment(req, *args):
        result = link_plan(req, *args)
        # Another request claims the cancelled card after our precheck,
        # before our enqueue's atomic claim. Both dispatch through the real queue.
        with monkeypatch.context() as inner:
            inner.setattr(mcp_server._Request, "link_plan", link_plan)
            asyncio.run(mcp_server.assign("developer", "schema", plan_worker=f"{pid}:schema"))
        return result

    with monkeypatch.context() as race:
        race.setattr(mcp_server._Request, "link_plan", competing_assignment)
        asyncio.run(mcp_server.assign("developer", "competing schema request", plan_worker=f"{pid}:schema"))
    assert plans.to_dto(db, plans.get(db, pid))["split"][0]["task_id"] != cancelled.id
    asyncio.run(mcp_server.handoff("developer", "UI", plan_worker=f"{pid}:ui"))
    board = lambda: plans.to_dto(db, plans.get(db, pid))["split"]  # noqa: E731
    first, second = board()
    assert first["task_id"] != second["task_id"]
    assert [(w["state"], w["test_state"]) for w in board()] == [("queued", "not run")] * 2
    asyncio.run(mcp_server.assign("developer", "a second trigger", plan_worker=f"{pid}:schema"))
    assert len(db.list_tasks()) == 4 and started == []
    # A different session cannot attach its task to this board.
    with monkeypatch.context() as identity:
        identity.setattr("brindle.autopilot.root_of", lambda db_, aid: "other")
        asyncio.run(mcp_server.assign("developer", "foreign", plan_worker=f"{pid}:schema"))
    assert len(db.list_tasks()) == 4
    plans.decide(db, pid, 1, True, person=True)
    first, second = board()
    assert len(started) == 2
    assert first["agent_id"] == db.get_task(first["task_id"]).agent_id
    assert first["state"] == second["state"] == "working"
    asyncio.run(mcp_server.assign("developer", "duplicate while working", plan_worker=f"{pid}:schema"))
    assert len(db.list_tasks()) == 4 and len(started) == 2
    ws = replace(db.get_workspace(boss.workspace_id), id="worker-ws", name="worker")
    db.add_workspace(ws)
    db.update_agent(first["agent_id"], workspace_id=ws.id)
    sha, outcome = ["pass"], [True]
    monkeypatch.setattr(gates, "head", lambda ws_: sha[0])
    monkeypatch.setattr(gates.git, "out", lambda *a, **kw: f"tree-{sha[0]}")
    monkeypatch.setattr(gates.git, "dirty_files", lambda path: [])
    monkeypatch.setattr(gates, "_scoped", lambda ws_, cmd, diff: cmd)
    run_queued = gates._run_queued
    monkeypatch.setattr(gates, "_run_queued", lambda *a: (outcome[0], "recorded output"))

    def inconclusive():
        with monkeypatch.context() as execution:
            execution.setattr(gates, "_run_queued", run_queued)
            execution.setattr(gates, "_run_once", lambda *a: (False, gates.autopilot.TIMED_OUT))
            execution.setattr(gates, "_concurrency", lambda ws_: 1)
            assert gates.timed_out(gates.run_checked(db, ws, check, {}, 30, full=True)[1])

    sha[0] = "inconclusive-first"
    inconclusive()
    assert board()[0]["test_state"] == "not run"
    sha[0] = "pass"
    gates.run_checked(db, ws, "unrelated", {}, 30, full=True)
    assert board()[0]["test_state"] == "not run"
    gates.run_checked(db, ws, check, {}, 30, full=True)
    assert board()[0]["test_state"] == "passing"
    sha[0] = "inconclusive-after-pass"
    inconclusive()
    assert board()[0]["test_state"] == "passing"
    sha[0], outcome[0] = "fail", False
    gates.run_checked(db, ws, check, {}, 30, full=True)
    assert board()[0]["test_state"] == "failing"
    db.update_agent(first["agent_id"], pipeline="reviewing", result="previous report")
    assert board()[0]["state"] == "in review"
    db.update_agent(first["agent_id"], pipeline="fixing")
    assert board()[0]["state"] == "working"
    sha[0], outcome[0] = "fixed", True
    gates.run_checked(db, ws, check, {}, 30, full=True)
    assert board()[0]["test_state"] == "passing"
    db.set_milestones(boss.id, [("UI", second["test"], None)])
    db.set_task_milestone(second["task_id"], "UI")
    db.record_check(db.milestones(boss.id)[0].id, True, "passed", "sha")
    db.update_task(first["task_id"], state="merged")
    assert [(w["state"], w["test_state"]) for w in board()] == [
        ("merged", "passing"), ("working", "passing")]
    db.delete_workspace(ws.id)
    assert (board()[0]["state"], board()[0]["test_state"]) == ("merged", "passing")


def test_approved_plan_goes_to_the_library(db, session, tmp_path, monkeypatch, backend, fixed_identity):
    """The approved plan is sent to the library, and a failed upload never undoes the approval."""
    from brindle.pro import analysis_docs as ad
    from brindle.pro import auth, credentials

    boss, _ = session
    backend.docs, backend.switches = [], {"store": "on", "assist": "on"}
    backend.routes["POST /analysis-docs"] = lambda form, headers: (
        (backend.docs.append(dict(form)), (500, {"id": "d1"}))[1])
    backend.routes["GET /me/analysis-docs/settings"] = lambda f, h: (200, {"switches": backend.switches})
    store = credentials.FileStore(tmp_path / "pro")
    login_as(backend, store, features=("analysis_docs",))
    up = ad.AnalysisDocs(str(tmp_path), client=auth.Client(None, backend), store=store,
                         key_store=credentials.FileStore(tmp_path / "pro", account="learning-keys"),
                         identity=lambda: "a" * 40, notice=lambda t: None, start_thread=False)
    monkeypatch.setattr(ad, "_instances", {str(tmp_path): up})

    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    split = [{"id": "w1", "title": "Schema", "files": ["src/brindle/db.py"], "test": "tests/test_x.py::test_a"},
             {"id": "w2", "title": "UI", "after": ["w1"]}]
    pid = mcp_server.propose_plan(PLAN, DECISIONS, split).split()[2]
    out = plans.decide(db, pid, 1, True, picks={"d1": "repo"},
                       comments=[{"section": "s1", "text": "Keep it small."}], person=True)
    assert up.flush()
    [sent] = backend.docs                         # exactly one attempt, whatever the answer
    assert plans.get(db, pid)["state"] == "approved" and out["state"] == "approved"
    assert sent["source"] == {"kind": "goal", "ref": pid, "session": "boss"}
    md = sent["files"][0]["md"]
    assert PLAN in md and "A file in each repo" in md and "Keep it small." in md
    assert "after: w1" in md and "src/brindle/db.py" in md and "tests/test_x.py::test_a" in md


@pytest.mark.parametrize("app_connected", [0, 1])
def test_assign_queues_behind_open_plan_until_the_person_approves(db, session, monkeypatch, app_connected):
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

    monkeypatch.setattr(events, "connected", app_connected)
    reply = asyncio.run(mcp_server.assign("developer", "build the preset schema"))
    if not app_connected:
        plans.decide(db, pid, 1, False, note="Revise the split.", person=True)
        asyncio.run(mcp_server.assign("developer", "build the UI"))
        assert started == ["build the preset schema", "build the UI"]
        return
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


def test_plan_offer_without_app(db, session, monkeypatch):
    from brindle import autopilot

    boss, started = session
    monkeypatch.setattr(events, "connected", 0)
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    db.add_autopilot(boss.id)
    mcp_server.set_goal("Org presets", [{"title": f"M{i}", "check": "true"} for i in range(4)])
    asyncio.run(mcp_server.assign("developer", "build the preset schema"))
    assert started == ["build the preset schema"]
    assert db.get_autopilot(boss.id).plan_offer is None
    assert plans.holding(db, boss.id) is None


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
