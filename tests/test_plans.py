"""Planning docs (brindle.plans): made only when the person asks, assign
queued behind an open plan through the real dispatch, and only the person
approves or redirects."""

import asyncio
import json
import multiprocessing
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from brindle import agents, mcp_server, plans, tasks, visual_offers
from brindle.daemon import holds
from brindle.daemon.app import build_app
from brindle.db import DB, Agent, Workspace
from brindle.service.errors import ServiceError
from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401 - fixtures
from test_pro_remote_learning import login_as

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
APP_CONNECTED = holds.app_connected
PLAN = "# Org presets\n\nOne page.\n\n## Goal\n\nPresets per org.\n\n```\n## not a section\n```\n\n## Split\n\nTwo workers."
DECISIONS = [{"id": "d1", "title": "Where presets live", "mine": "org",
              "options": [{"id": "org", "label": "Org policy"}, {"id": "repo", "label": "A file in each repo"}]}]


@pytest.fixture
def session(db, tmp_path, monkeypatch):
    """A supervisor whose workers are recorded, not launched: assign runs the
    real queue and start (tasks.*), and agents.delegate makes the worker row."""
    monkeypatch.setenv("BRINDLE_VISUALS", "1")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr(holds, "app_connected", lambda: True)
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


def _serve_connected_app(pipe):
    """A separate daemon: only its event handler sees the app connection."""
    from brindle.daemon import peer

    app = build_app(TOKEN)

    async def unix_app(scope, receive, send):
        if scope["type"] == "http":
            scope[peer.PEER_PID] = 310
        await app(scope, receive, send)

    feed = SimpleNamespace(start=lambda: [{"ready": True}], tick=lambda now: [])
    with patch("brindle.daemon.events.Feed", return_value=feed), TestClient(unix_app) as client:
        with client.websocket_connect("/v1/events") as ws:
            ws.send_text(TOKEN)
            ws.receive_json()
            pipe.send("ready")
            while (request := pipe.recv()) is not None:
                response = client.post(request)
                pipe.send((response.status_code, response.json()))
        pipe.send(client.post("/v1/holds/app").json())


@pytest.fixture
def connected_app(session, monkeypatch):
    """Keep the HTTP transport fake, but cross a real daemon process boundary."""
    ctx = multiprocessing.get_context("spawn")
    caller, daemon = ctx.Pipe()
    process = ctx.Process(target=_serve_connected_app, args=(daemon,))
    process.start()
    daemon.close()

    class Connection:
        def __init__(self, *args):
            pass

        def request(self, method, path, body, headers):
            caller.send(path)

        def getresponse(self):
            status, data = caller.recv()
            return SimpleNamespace(status=status, read=lambda: json.dumps(data).encode())

        def close(self):
            pass

    try:
        assert caller.recv() == "ready"
        monkeypatch.setattr(holds, "_UnixConnection", Connection)
        monkeypatch.setattr(holds, "app_connected", APP_CONNECTED)
        yield
        caller.send(None)
        assert caller.recv() == {"connected": False}
    finally:
        process.terminate()
        process.join()
        caller.close()
        daemon.close()


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

    monkeypatch.setattr(holds, "app_connected", lambda: bool(app_connected))
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
    pytest.param(False, marks=pytest.mark.variant(
        "No must start the task queued behind the offer; the other offer tests answer with nothing queued")),
    pytest.param("legacy", marks=pytest.mark.variant(
        "an upgraded row: the retired analysis-doc offer's 'accepted' must neither hold work nor allow a plan")),
])
def test_big_goal_offers_a_planning_doc(accept, db, session, monkeypatch, connected_app):
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
    milestones = [{"title": f"M{i}", "check": "true"} for i in range(5)]
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


@pytest.mark.parametrize("answer, goal_first, assigned_first", [
    pytest.param({"option": "build"}, True, False, marks=pytest.mark.variant(
        "the ordinary approval: goal first, then an approving option starts the plan")),
    pytest.param({"option": "build"}, False, False, marks=pytest.mark.variant(
        "the design is approved before the session has a goal: the goal that follows claims it")),
    pytest.param({"option": "build"}, True, True, marks=pytest.mark.variant(
        "review: approving a design after work was assigned must still record approval independently of offer suppression")),
    pytest.param({"option": "back"}, True, False, marks=pytest.mark.variant(
        "a redirect option ('Go back and revise') is not an approval, whatever its words: the offer must still open")),
    pytest.param({"option": "build", "note": "change the split first"}, True, False, marks=pytest.mark.variant(
        "an approving option with a redirect note is not an approval: the offer must still open")),
])
def test_approved_design_counts_as_the_plan(answer, goal_first, assigned_first, db, session, monkeypatch):
    from brindle import autopilot, visuals
    from brindle.service import inbox

    boss, started = session
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    db.add_autopilot("boss")
    with db.tx() as c:
        visual_offers._grant(c, "boss")
    if goal_first:      # goal A starts small, so it has no offer yet; its design is approved while it runs
        mcp_server.set_goal("Org presets", [{"title": "M0", "check": "true"}])
    monkeypatch.setattr(agents, "send_message", lambda *a, **k: None)
    if assigned_first:
        asyncio.run(mcp_server.assign("developer", "start the schema"))
        assert started == ["start the schema"]
    shown = visuals.publish(db, boss, "Build plan", "<p>design</p>", "Build it?",
                            [{"id": "build", "label": "Build", "approves": True},
                             {"id": "back", "label": "Go back and revise"}])
    visuals.answer(db, shown["id"], 1, **answer)

    mcp_server.set_goal("Org presets", [{"title": f"M{i}", "check": "true"} for i in range(5)])
    assert visuals.has_approved_design(db, "boss", "Org presets") is (answer == {"option": "build"})
    offers = [i for i in inbox.items(db, None) if i.kind == "plan_offer"]
    asyncio.run(mcp_server.assign("developer", "build the preset schema"))
    if answer == {"option": "build"}:
        expected = ["start the schema", "build the preset schema"] if assigned_first else ["build the preset schema"]
        assert offers == [] and started == expected and plans.holding(db, "boss") is None
        # The approval belonged to that goal: a later, unrelated goal gets the normal offer.
        mcp_server.set_goal("Billing export", [{"title": f"B{i}", "check": "true"} for i in range(5)], new_goal=True)
        assert [i for i in inbox.items(db, None) if i.kind == "plan_offer"]
    else:
        assert len(offers) == 1 and started == []


@pytest.mark.variant("review: a design approved after the session moved on belongs to the goal it was shown for")
def test_approval_stays_with_the_goal_the_design_was_shown_for(db, session, monkeypatch):
    from brindle import autopilot, visuals
    from brindle.service import inbox

    boss, started = session
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    monkeypatch.setattr(agents, "send_message", lambda *a, **k: None)
    db.add_autopilot("boss")
    with db.tx() as c:
        visual_offers._grant(c, "boss")
    mcp_server.set_goal("Org presets", [{"title": "A", "check": "true"}])
    shown = visuals.publish(db, boss, "Build plan", "<p>design</p>", "Build it?",
                            [{"id": "build", "label": "Build", "approves": True}, {"id": "back", "label": "Back"}])
    mcp_server.set_goal("Billing export", [{"title": "B", "check": "true"}])
    visuals.answer(db, shown["id"], 1, option="build")        # A's design, answered while B is the goal
    mcp_server.set_goal("Billing export", [{"title": f"B{i}", "check": "true"} for i in range(5)])
    assert [i for i in inbox.items(db, None) if i.kind == "plan_offer"]


@pytest.mark.parametrize("delivered", [
    False,
    pytest.param(True, marks=pytest.mark.variant(
        "a delivery that succeeds after the offer opened must release the held worker")),
])
@pytest.mark.variant("review: a delivery that fails after a goal claimed the approval must still undo the answer")
def test_failed_answer_delivery_leaves_no_approval_behind(delivered, db, session, monkeypatch):
    from brindle import autopilot, visuals

    boss, started = session
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    with db.tx() as c:
        visual_offers._grant(c, "boss")
    db.add_autopilot("boss")
    shown = visuals.publish(db, boss, "Build plan", "<p>design</p>", "Build it?",
                            [{"id": "build", "label": "Build", "approves": True}, {"id": "back", "label": "Back"}])

    def fail(*a, **k):      # a big goal and an assign arrive while the answer is being delivered
        monkeypatch.setattr(agents, "send_message", lambda *a, **k: None)   # starting a task also messages
        mcp_server.set_goal("Org presets", [{"title": f"M{i}", "check": "true"} for i in range(5)])
        asyncio.run(mcp_server.assign("developer", "build the preset schema"))
        if not delivered:
            raise RuntimeError("not delivered")
    monkeypatch.setattr(agents, "send_message", fail)
    if delivered:
        visuals.answer(db, shown["id"], 1, option="build")
        assert plans.holding(db, "boss") is None and started == ["build the preset schema"]
        return
    with pytest.raises(RuntimeError):
        visuals.answer(db, shown["id"], 1, option="build")
    assert visuals.get(db, shown["id"])["answer"] is None
    assert not visuals.has_approved_design(db, "boss", "Org presets")
    assert started == [] and plans.holding(db, "boss") is not None      # the undelivered answer exempted nothing


@pytest.mark.variant("review: the answer is delivered between the offer's evaluation and its insertion")
def test_approval_delivered_while_the_offer_is_being_opened(db, session, monkeypatch):
    from brindle import autopilot, visuals
    from brindle.db import DB as DBClass

    boss, started = session
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    monkeypatch.setattr(agents, "send_message", lambda *a, **k: None)
    db.add_autopilot("boss")
    with db.tx() as c:
        visual_offers._grant(c, "boss")
    shown = visuals.publish(db, boss, "Build plan", "<p>design</p>", "Build it?",
                            [{"id": "build", "label": "Build", "approves": True}, {"id": "back", "label": "Back"}])
    real = DBClass.open_plan_offer

    def late(self, *args):      # the approval lands after the evaluation saw none, before the offer is written
        visuals.answer(db, shown["id"], 1, option="build")
        return real(self, *args)
    monkeypatch.setattr(DBClass, "open_plan_offer", late)
    mcp_server.set_goal("Org presets", [{"title": f"M{i}", "check": "true"} for i in range(5)])
    assert visuals.get(db, shown["id"])["answer"] is not None
    asyncio.run(mcp_server.assign("developer", "build the preset schema"))
    assert plans.holding(db, "boss") is None and started == ["build the preset schema"]


@pytest.mark.parametrize("how", [
    pytest.param("declined", marks=pytest.mark.variant(
        "the owner's reported case: declined, then the goal grew and was asked again")),
    pytest.param("approved", marks=pytest.mark.variant("an offer accepted and its plan approved: asked once as well")),
    pytest.param("direct", marks=pytest.mark.variant(
        "a small goal never offered, then a plan the person asked for and approved, then the goal grows")),
])
def test_answered_plan_offer_is_not_asked_again_as_the_goal_grows(how, db, session, monkeypatch):
    """Owner report: every set_goal that reworded the goal and added milestones
    asked again after the person had answered or approved a plan. The same
    session's goal, however it is reworded and grown, is offered once and its
    assignments start; an unrelated goal is asked afresh."""
    from brindle import autopilot
    from brindle.service import inbox

    boss, started = session
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    db.add_autopilot("boss")
    offers = lambda: [i for i in inbox.items(db, None) if i.kind == "plan_offer"]  # noqa: E731
    items = lambda n, p="M": [{"title": f"{p}{i}", "check": "true"} for i in range(n)]  # noqa: E731
    if how == "direct":
        mcp_server.set_goal("Org presets", items(2))
        assert offers() == []
        visual_offers.person_spoke(db, boss, "Make a planning doc.")
    else:
        mcp_server.set_goal("Org presets", items(5))
        [offer] = offers()
        plans.answer_offer(db, "boss", offer.request_id, how == "approved")
    if how != "declined":
        pid = mcp_server.propose_plan(PLAN).split()[2]
        plans.decide(db, pid, 1, True, person=True)

    mcp_server.set_goal("Organization-wide presets and their admin screens", items(8))
    mcp_server.set_goal("Presets: admin screens and audit log", items(9))
    assert offers() == [] and not plans.open_offer_id(db.get_autopilot("boss").plan_offer)
    assert plans.holding(db, "boss") is None
    asyncio.run(mcp_server.assign("developer", "build the preset schema"))
    assert started == ["build the preset schema"]

    mcp_server.set_goal("Rewrite the installer", items(4, "N"), new_goal=True)
    assert len(offers()) == 1                                # an unrelated goal may be asked once


@pytest.mark.variant("reported stall: a terminal-only session's workers held by an offer nobody can answer (review 0.6.3, finding 3)")
def test_plan_offer_without_app(db, session, monkeypatch):
    from brindle import autopilot

    boss, started = session
    monkeypatch.setattr(holds, "app_connected", APP_CONNECTED)
    monkeypatch.setattr(autopilot, "_commit", lambda ws, tracked_only=False: "abc123")
    db.add_autopilot(boss.id)
    mcp_server.set_goal("Org presets", [{"title": f"M{i}", "check": "true"} for i in range(5)])
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


def test_design_doc_round_trip_comment_reply_and_next_revision(db, session):
    from brindle import plan_docs

    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    sections = [{"id": "goal", "title": "Goal", "markdown": "Presets per org."},
                {"id": "flow", "title": "Flow", "kind": "mermaid", "markdown": "graph TD; A-->B"}]
    pid = mcp_server.propose_plan("# Org presets", DECISIONS, sections=sections).split()[2]
    assert [s["kind"] for s in plan_docs.revision(db, pid)["sections"]] == ["markdown", "mermaid"]

    tid = plan_docs.comment(db, pid, 1, {"kind": "text", "section": "goal", "quote": "per org"}, "Which orgs?")
    sid = plan_docs.suggest(db, pid, 1, "goal", 0, "Presets", "Rules", "clearer")
    plans.decide(db, pid, 1, False, person=True)
    [msg] = messages(db)                                  # one message for the batch, naming the thread
    with pytest.raises(ToolError):                        # a suggestion can't be resolved ...
        mcp_server.reply_plan_thread(pid, sid, "no", resolve=True)
    assert not next(t for t in plan_docs.fields(db, plans.get(db, pid))["threads"] if t["id"] == sid)["replies"]
    assert f"[thread #{tid}]" in msg and "Which orgs?" in msg

    assert f"#{tid}" in mcp_server.reply_plan_thread(pid, tid, "All of them.", resolve=True)
    thread = next(t for t in plan_docs.fields(db, plans.get(db, pid))["threads"] if t["id"] == tid)
    assert thread["status"] == "resolved" and thread["replies"][0]["author"] == plan_docs.SUPERVISOR

    sections[0]["markdown"] = "Presets per org, all orgs."
    assert mcp_server.propose_plan("# Org presets", DECISIONS, sections=sections, plan_id=pid).split()[3] == "r2"
    assert plans.get(db, pid)["state"] == "open"


def _shared(db, pid, review, local_rev=1, remote_rev=1):
    """A plan as a sync leaves it: linked to the Library, with its last review state."""
    review = {"policy_reviewers": [], **review} if review else review   # the Library always sends the list
    with db.tx() as c:
        c.execute("INSERT INTO plan_shares (plan_id, doc_id, owner, remote_rev, local_rev, review) VALUES (?,'doc','me',?,?,?)"
                  " ON CONFLICT(plan_id) DO UPDATE SET remote_rev=excluded.remote_rev, local_rev=excluded.local_rev,"
                  " review=excluded.review", (pid, remote_rev, local_rev, json.dumps(review)))


def _signed_off(*subs, rev=1):
    return [{"sub": s, "rev": rev, "at": 5.0} for s in subs]


TWO = [{"sub": "a", "required": True}, {"sub": "b", "required": True}, {"sub": "c", "required": False}]
ONE = [{"sub": "a", "required": True}]
RULE = {"org_id": "org_1", "policy": "staff", "name": "Staff sign-off", "approvals": 1, "on_list": ["a"], "met_by": ["a"],
        "status": "met"}


@pytest.fixture
def library(monkeypatch):
    from brindle.pro import shared_docs

    monkeypatch.setattr(shared_docs, "sync", lambda db, plan_id: {})
    return shared_docs


@pytest.mark.parametrize("review, local_rev, teammate, state, signed", [
    pytest.param({"rev": 1, "reviewers": TWO, "sign_offs": _signed_off("a", "b")}, 1, None, "ready", (2, 2), id="all-signed",
                 marks=pytest.mark.variant("every required reviewer on a plain list (no rules) has signed: the gate opens at 2 of 2")),
    pytest.param({"rev": 1, "reviewers": TWO, "sign_offs": _signed_off("a", "c")}, 1, None, "waiting", (1, 2), id="optional-doesnt-count",
                 marks=pytest.mark.variant("an optional reviewer's sign-off must not stand in for a missing required one")),
    pytest.param({"rev": 1, "reviewers": ONE, "sign_offs": _signed_off("a"),
                  "policy_reviewers": [RULE, {**RULE, "policy": "security", "name": "Security"}]},
                 1, None, "ready", (1, 1), id="one-sign-off-meets-every-rule"),
    pytest.param({"rev": 1, "reviewers": ONE, "sign_offs": _signed_off("a"),
                  "policy_reviewers": [{**RULE, "on_list": [], "met_by": [], "status": "waiting"}]},
                 1, None, "waiting", (1, 2), id="a-rule-nobody-met",
                 marks=pytest.mark.variant("a rule nobody on its list has met holds the gate even though the required list is signed")),
    pytest.param({"rev": 1, "reviewers": ONE, "sign_offs": _signed_off("a", rev=0)}, 1, None, "waiting", (0, 1),
                 id="sign-off-on-an-older-revision",
                 marks=pytest.mark.variant("a stale sign-off must never open the gate on text the reviewer hasn't read")),
    pytest.param({"rev": 1, "reviewers": ONE, "sign_offs": _signed_off("a")}, 0, None, "waiting", (0, 1), id="revision-not-sent-yet"),
    pytest.param({"rev": 1, "reviewers": ONE, "sign_offs": _signed_off("a")}, 1, "a", "changes_requested", (0, 1), id="changes-after-sign-off",
                 marks=pytest.mark.variant("a reviewer who signed and then asked for changes closes the gate they had signed")),
    pytest.param({"rev": 1, "reviewers": [], "sign_offs": _signed_off("a"), "policy_reviewers": [{**RULE, "on_list": []}]},
                 1, "a", "changes_requested", (0, 1), id="changes-from-a-signer-only-a-rule-counts"),
    pytest.param({}, 1, None, "waiting", (0, 0), id="library-not-heard-from",
                 marks=pytest.mark.variant("no review data from the Library at all must wait, not read as nobody required")),
    pytest.param({"rev": 1, "reviewers": [], "policy_reviewers": ["unreadable"]}, 1, None, "waiting", (0, 0), id="unreadable-rule"),
])
def test_required_reviewers_gate(db, session, library, monkeypatch, review, local_rev, teammate, state, signed):
    from brindle import plan_docs

    boss, started = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    _shared(db, pid, review, local_rev=local_rev)
    if teammate:
        plan_docs.comment(db, pid, 1, {"kind": "section", "section": "s1"}, "Not like this.",
                          author=plan_docs.TEAMMATE + teammate)
    gate = plans.to_dto(db, plans.get(db, pid))["gate"]
    assert (gate["state"], (gate["signed"], gate["required"])) == (state, signed)
    if teammate:                                                    # the person it waits on is named
        assert any(r["sub"] == teammate or teammate in r["changes_by"] for r in gate["reviewers"])
    if state == "ready":
        # All signed, and still only the approval starts it: losing the app can't skip that step.
        monkeypatch.setattr(holds, "app_connected", lambda: False)
        asyncio.run(mcp_server.assign("developer", "build it"))
        assert started == [] and tasks.sweep(db) == 0 and started == []
        assert plans.decide(db, pid, 1, True, person=True)["state"] == "approved" and started == ["build it"]
        assert plans.gate(db, plans.get(db, pid))["state"] == "closed"
        return
    # Refused outright, even with no app connected: teammates sign off from their own machines.
    monkeypatch.setattr(holds, "app_connected", lambda: False)
    asyncio.run(mcp_server.assign("developer", "build it"))
    assert db.list_tasks() == [] and started == []
    with pytest.raises(ServiceError) as e:
        plans.decide(db, pid, 1, True, person=True)
    assert e.value.code == "gate_closed" and plans.get(db, pid)["state"] == "open" and messages(db) == []


def test_required_reviewers_request_changes_blocks_and_building_closes_the_review(db, session, library):
    boss, started = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    _shared(db, pid, {"rev": 1, "reviewers": ONE, "sign_offs": _signed_off("a")})
    asyncio.run(mcp_server.assign("developer", "queued before it was blocked"))

    # Request changes works with reviewers still out, and holds the build whatever was signed.
    assert plans.decide(db, pid, 1, False, note="Rework it.", person=True)["state"] == "redirected"
    assert plans.gate(db, plans.get(db, pid))["state"] == "changes_requested"
    assert tasks.sweep(db) == 0 and started == []

    # The new revision needs its own sign-off, which the earlier one doesn't carry over to.
    mcp_server.propose_plan(PLAN + "\n\nRevised.", plan_id=pid)
    _shared(db, pid, {"rev": 2, "reviewers": ONE, "sign_offs": _signed_off("a", rev=1)}, local_rev=2, remote_rev=2)
    assert plans.gate(db, plans.get(db, pid))["state"] == "waiting"
    _shared(db, pid, {"rev": 2, "reviewers": ONE, "sign_offs": _signed_off("a", rev=2)}, local_rev=2, remote_rev=2)
    assert plans.decide(db, pid, 2, True, person=True)["state"] == "approved"
    assert started == ["queued before it was blocked"]

    assert plans.gate(db, plans.get(db, pid))["state"] == "closed"
    for attempt in (lambda: library.set_reviewers(db, pid, ONE), lambda: library.sign_off(db, pid, 2)):
        with pytest.raises(ServiceError) as e:
            attempt()
        assert e.value.code == "review_closed"


@pytest.mark.parametrize("reviewers", [
    pytest.param(ONE, marks=pytest.mark.variant(
        "a cached required list that is fully signed still can't open the gate while the Library is unreachable")),
    pytest.param([], marks=pytest.mark.variant(
        "an empty cached list can't prove nobody was added since: a first required reviewer may be waiting remotely")),
])
def test_required_reviewers_approval_needs_the_library(db, session, library, monkeypatch, reviewers):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    _shared(db, pid, {"rev": 1, "reviewers": reviewers, "sign_offs": _signed_off("a")})

    def offline(db_, plan_id):
        raise RuntimeError("the Library can't be reached")
    monkeypatch.setattr(library, "sync", offline)
    with pytest.raises(ServiceError) as e:
        plans.decide(db, pid, 1, True, person=True)
    assert e.value.code == "gate_unverified" and plans.get(db, pid)["state"] == "open"


NO_REVIEW = {"rev": 1, "reviewers": [], "policy_reviewers": [], "sign_offs": []}


@pytest.fixture
def outage(db, session, monkeypatch):
    """A plan shared with no required reviewers, a task queued behind it, and a
    Library that is down until ``down`` is cleared and then answers ``view``."""
    from brindle.pro import analysis_docs as ad
    from brindle.pro import auth, license, shared_docs

    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    _shared(db, pid, NO_REVIEW)
    shared_docs._remember(db, pid, NO_REVIEW)
    asyncio.run(mcp_server.assign("developer", "build it"))
    line = SimpleNamespace(pid=pid, view=NO_REVIEW, signed_in="me", calls=[], down=auth.TransportError("no route to 10.0.0.1"),
                           patch=monkeypatch, real_sync=shared_docs.sync)

    def sync(db_, plan_id, **kw):
        if line.down:
            raise line.down
        shared_docs._remember(db_, plan_id, line.view)
        return {}

    def api(method, path, body=None, **kw):
        if line.down:
            raise line.down
        line.calls.append((method, path))
        return {"scope": {"kind": "user", "id": "me"}, "scopes": [{"kind": "user", "id": "me"}, {"kind": "group", "id": "t_1"}]}

    monkeypatch.setattr(shared_docs, "sync", sync)
    monkeypatch.setattr(shared_docs, "_require_team", lambda store: SimpleNamespace(sub="me", org_id="org_1"))
    monkeypatch.setattr(ad, "_session", lambda client=None, store=None: (None, None))
    monkeypatch.setattr(ad, "api", api)
    monkeypatch.setattr(license, "current", lambda **kw: SimpleNamespace(sub=line.signed_in, org_id="org_1", in_grace=False,
                                                                         features=frozenset()))
    return line


def _approve(app, line):
    return app.post(f"/v1/plans/{line.pid}/decide", json={"rev": 1, "approved": True})


def _refused_approval(app, line, reason):
    """Approve while the Library is down: refused in plain words, and nothing starts."""
    error = _approve(app, line).json()["error"]
    assert (error["code"], error["reason"]) == ("gate_unverified", reason)
    assert error["message"] and str(line.down) not in error["message"]      # the failure's own words stay out
    return app.get(f"/v1/plans/{line.pid}").json()


def _unshares(db):
    return [r["doc_id"] for r in db.conn.execute("SELECT doc_id FROM plan_unshares")]


def _approves_by_itself(db, app, line, started):
    from brindle.pro import analysis_docs as ad
    from brindle.pro import auth, shared_docs

    for down, reason in [(auth.AuthError("token rejected", code="invalid_grant", revoked=True), "sign_in"),
                         (ad.Refused("kms said no", 503, "key_unavailable"), "key_unavailable"),
                         (ad.Refused("boom", 500), "library_error"),
                         (line.down, "offline")]:
        line.down = down
        held = _refused_approval(app, line, reason)["gate"]["unconfirmed"]
        assert held["reason"] == reason and held["approval_held"] and started == []

    assert shared_docs.sync_open(db) == 0 and started == []     # still down: nothing moves
    line.down = None
    assert shared_docs.sync_open(db) == 1
    assert plans.get(db, line.pid)["state"] == "approved" and started == ["build it"]


def _not_approved_when_a_reviewer_appeared(db, app, line, started):
    from brindle.pro import shared_docs
    from brindle.service import inbox

    _refused_approval(app, line, "offline")
    line.down, line.view = None, {**NO_REVIEW, "reviewers": ONE}
    for _ in range(2):      # told once, and the next pass doesn't approve it either
        shared_docs.sync_open(db)
        gate = app.get(f"/v1/plans/{line.pid}").json()["gate"]
        assert plans.get(db, line.pid)["state"] == "open" and started == [] and messages(db) == []
        assert gate["approval_refused"] and gate["unconfirmed"] is None
    told = [i for i in inbox.items(db, None) if i.kind == "visual" and i.visual_id == line.pid]
    assert [i.reason for i in told] == [gate["approval_refused"]]


def _stops_sharing_and_builds(db, app, line, started):
    from brindle.pro import shared_docs

    assert _refused_approval(app, line, "offline")["gate"]["unconfirmed"]["can_stop_sharing"]
    now = app.post(f"/v1/plans/{line.pid}/stop-sharing", json={"rev": 1}).json()
    assert now["gate"] is None and now["team_ended"] is False
    assert _approve(app, line).json()["state"] == "approved" and started == ["build it"]

    shared_docs.sync_open(db)                                   # the Library is still down: kept for later
    assert _unshares(db) == ["doc"] and line.calls == []
    line.down = None
    shared_docs.sync_open(db)
    assert line.calls == [("GET", "/analysis-docs/doc"), ("DELETE", "/analysis-docs/doc/share/group/t_1")]
    assert _unshares(db) == []


def _access_refusal_stands_until_the_library_answers(db, app, line, started):
    from brindle.pro import analysis_docs as ad
    from brindle.pro import shared_docs

    answers = [(403, {"error": "forbidden"})]
    line.patch.setattr(shared_docs, "sync", line.real_sync)
    line.patch.setattr(ad, "_call", lambda *a, **kw: answers[-1])
    refused = _refused_approval(app, line, "library_error")["gate"]["unconfirmed"]
    assert not refused["can_stop_sharing"] and not refused["approval_held"]
    assert app.post(f"/v1/plans/{line.pid}/stop-sharing", json={"rev": 1}).status_code == 409 and _unshares(db) == []

    answers.append((200, NO_REVIEW))
    assert shared_docs.sync_open(db) == 1 and started == []
    answers.append((503, {"error": "unavailable"}))             # an outage now: nothing has refused them
    assert _refused_approval(app, line, "library_error")["gate"]["unconfirmed"]["can_stop_sharing"]


def _reviewers_once_existed(db, line):
    from brindle.pro import analysis_docs as ad
    from brindle.pro import shared_docs

    # Only the read inside set_reviewers shows the reviewer: the answer that gets stored no longer lists them.
    line.patch.setattr(ad, "_call", lambda method, path, body=None, **kw: (
        200, {**NO_REVIEW, "reviewers": ONE} if method == "GET" else NO_REVIEW))
    shared_docs.set_reviewers(db, line.pid, [])


def _another_account(db, line):
    line.signed_in = "someone-else"


@pytest.mark.parametrize("scenario", [
    _approves_by_itself, _not_approved_when_a_reviewer_appeared, _stops_sharing_and_builds,
    pytest.param(_access_refusal_stands_until_the_library_answers, marks=pytest.mark.variant(
        "bypass attempt: a 403 from the Library is a refusal, not an outage, so it can't open the way around the review")),
])
def test_held_plan_when_the_library_is_down(db, session, outage, scenario):
    _, started = session
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        scenario(db, app, outage, started)


@pytest.mark.parametrize("arrange", [
    pytest.param(_reviewers_once_existed, marks=pytest.mark.variant(
        "bypass attempt: a required reviewer removed before the outage must not reopen the way out")),
    pytest.param(_another_account, marks=pytest.mark.variant(
        "bypass attempt: only the account that shared the plan may unshare it")),
])
def test_held_plan_when_the_library_is_down_refuses_stop_sharing(db, session, outage, arrange):
    _, started = session
    arrange(db, outage)
    with TestClient(build_app(TOKEN), headers=AUTH) as app:
        assert not _refused_approval(app, outage, "offline")["gate"]["unconfirmed"]["can_stop_sharing"]
        refused = app.post(f"/v1/plans/{outage.pid}/stop-sharing", json={"rev": 1})
        assert refused.json()["error"]["code"] == "stop_sharing_refused" and refused.status_code == 409
        assert app.get(f"/v1/plans/{outage.pid}").json()["gate"] is not None and started == []
    assert _unshares(db) == []


def test_shared_doc_inbox_cards_follow_the_review_and_clear_when_acted_on(db, session, monkeypatch):
    from brindle import plan_docs
    from brindle.service import inbox

    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    me = {"sub": "me"}
    monkeypatch.setattr(inbox, "_my_sub", lambda: me["sub"])
    kinds = lambda: [(i.kind, i.doc) for i in inbox._review_items(db, None)]  # noqa: E731

    _shared(db, pid, {"rev": 1, "reviewers": ONE, "sign_offs": []})
    assert kinds() == []                                   # the author waits: no card yet
    from brindle.pro import review_queue

    asked = lambda on: review_queue._seen.__setitem__("a", [{"doc_id": "doc", "title": "t"}] if on else [])  # noqa: E731
    me["sub"] = "a"
    asked(True)
    assert kinds() == [("review_requested", "doc")]        # the Library asks the reviewer, and it opens the doc
    _shared(db, pid, {"rev": 1, "reviewers": ONE, "sign_offs": _signed_off("a")})
    asked(False)
    assert kinds() == []                                   # signing takes it off the queue
    me["sub"] = "me"
    assert kinds() == [("signed_off", "doc")]
    plan_docs.comment(db, pid, 1, {"kind": "section", "section": "s1"}, "No.", author=plan_docs.TEAMMATE + "a")
    assert kinds() == [("changes_requested", "doc")]
    _shared(db, pid, {"rev": 1, "reviewers": ONE, "sign_offs": []})
    me["sub"] = "a"
    assert kinds() == []                                   # asking for changes answers the request too
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    mcp_server.propose_plan(PLAN + "\n\nRevised.", plan_id=pid)
    _shared(db, pid, {"rev": 2, "reviewers": ONE, "sign_offs": []}, local_rev=2, remote_rev=2)
    asked(True)
    assert kinds() == [("review_requested", "doc")]        # the answer was to r1: r2 needs its own review
    with monkeypatch.context() as m:
        m.setattr(inbox.identity_lock, "locked_by", lambda _id: True)
        assert [(i.doc, i.request_id) for i in inbox._review_items(db, None)] == [("doc", None)]   # the queue's card, no session link
    asked(False)
    me["sub"] = "me"
    assert kinds() == [("changes_requested", "doc")]
    with monkeypatch.context() as m:
        m.setattr(inbox.identity_lock, "locked_by", lambda _id: True)
        assert kinds() == []                               # another identity's session shows nothing
    me["sub"] = None                                      # logged out: no cards, no error
    assert kinds() == []


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
