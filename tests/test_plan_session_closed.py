"""Plans stop asking for approval when their session is closed."""

import asyncio
import time

import pytest

from brindle import agents, mcp_server, plans, procs, tasks, tmux, visual_offers, visuals
from brindle.approval_sources import PERSON_IN_APP
from brindle.db import DB, Agent, Workspace
from brindle.service import inbox, sessions
from brindle.service.errors import ServiceError
from test_plans import PLAN, session, work  # noqa: F401 - session is a fixture


@pytest.fixture
def proposed(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BRINDLE_VISUALS", "1")
    monkeypatch.setattr(tmux, "list_panes", lambda: tmux.PaneSnapshot())
    monkeypatch.setattr(procs, "stop", lambda *args, **kwargs: None)
    now = time.time()
    ws = Workspace("ws", str(tmp_path), "main", "main", "main", None, str(tmp_path), None, "brindle-ws", now)
    db.add_workspace(ws)
    root = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "%1", None, now)
    db.add_agent(root)
    visual_offers.person_spoke(db, root, "Make a planning doc.")
    return plans.propose(db, root, "# Test plan\n\n## Goal\n\nKeep the document readable.")["id"]


@pytest.mark.variant("regression: a closed session's plan card must leave the Inbox")
def test_closing_session_removes_plan_inbox_card(db, proposed):
    assert proposed in {i.visual_id for i in inbox.items(db, None)}
    db.set_status("boss", "paused")
    assert plans.get(db, proposed)["state"] == "open"
    assert proposed in {v["id"] for v in visuals.inbox_pending(db, None)}
    assert proposed in {i.visual_id for i in inbox.items(db, None)}
    sessions.close_session(db, "boss")
    assert proposed not in {v["id"] for v in visuals.inbox_pending(db, None)}
    assert proposed not in {i.visual_id for i in inbox.items(db, None)}


@pytest.mark.parametrize("state", [
    pytest.param("open", marks=pytest.mark.variant("the common case: an open plan closes with its session")),
    pytest.param("redirected", marks=pytest.mark.variant("a plan awaiting revision must close too")),
])
def test_closing_session_closes_plan(db, proposed, state):
    db.conn.execute("UPDATE plans SET state=? WHERE id=?", (state, proposed))
    db.conn.execute("INSERT INTO plan_shares (plan_id, doc_id, owner) VALUES (?, 'shared-doc', 'owner')", (proposed,))
    db.conn.commit()
    shared = dict(db.conn.execute("SELECT * FROM plan_shares WHERE plan_id=?", (proposed,)).fetchone())
    sessions.close_session(db, "boss")
    sessions.close_session(db, "boss")
    plan = plans.get(db, proposed)
    assert plan["state"] == "closed"
    assert db.conn.execute("SELECT state FROM plans WHERE id=?", (proposed,)).fetchone()[0] == "closed"
    assert plans.to_dto(db, plan)["can_edit"] is False
    assert plan["decision"] is None
    assert plan["markdown"] and visuals.get(db, proposed)
    assert dict(db.conn.execute("SELECT * FROM plan_shares WHERE plan_id=?", (proposed,)).fetchone()) == shared
    with pytest.raises(ServiceError):
        plans.decide(db, proposed, plan["rev"], True, approval=PERSON_IN_APP)
    assert db.list_tasks() == []


@pytest.mark.parametrize("docs_at_close", [
    pytest.param("1", marks=pytest.mark.variant("the common case: the session closes with Docs on")),
    pytest.param("0", marks=pytest.mark.variant("review: Docs turned off before the close dropped the hold")),
])
def test_closing_session_holds_its_plans_queued_work_for_the_supervisor(db, session, monkeypatch,  # noqa: F811
                                                                        docs_at_close):
    boss, started = session
    monkeypatch.setattr(tmux, "list_panes", lambda: tmux.PaneSnapshot())
    monkeypatch.setattr(procs, "stop", lambda *args, **kwargs: None)
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    assert "behind plan" in asyncio.run(mcp_server.assign("developer", "build the preset schema", **work(db)))
    [task] = db.list_tasks(state="pending")

    monkeypatch.setenv("BRINDLE_VISUALS", docs_at_close)
    sessions.close_session(db, boss.id)
    monkeypatch.setenv("BRINDLE_VISUALS", "1")                # back on before the session continues
    assert plans.get(db, pid)["state"] == "closed" and db.cancelled_hold(task.id) == pid
    tasks.on_resumed(db, boss.id)                       # continuing the session starts none of it
    assert started == [] and db.get_task(task.id).state == "pending"


@pytest.mark.variant("race (review): a sweep between the supervisor's dismissal and the holds started plan work")
def test_a_sweep_during_session_close_never_sees_dismissal_without_holds(db, session, monkeypatch):  # noqa: F811
    boss, started = session
    monkeypatch.setattr(agents, "is_alive", lambda agent, panes=None: False)   # its pane is gone: it stays idle
    monkeypatch.setattr(procs, "stop", lambda *args, **kwargs: None)
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    mcp_server.propose_plan(PLAN)
    asyncio.run(mcp_server.assign("developer", "build the preset schema", **work(db)))
    close, swept = plans.close_with_session, []

    def sweep_first(*args, **kwargs):   # another process's sweep, at the moment the plans are closed
        swept.append(tasks.sweep(DB()))
        return close(*args, **kwargs)

    monkeypatch.setattr(plans, "close_with_session", sweep_first)
    sessions.close_session(db, boss.id)
    assert swept == [0] and started == []


def test_already_closed_session_open_plan_is_not_in_inbox(db, proposed):
    sessions.close_session(db, "boss")
    # An existing stale row from before session closing also closed plans.
    db.conn.execute("UPDATE plans SET state='open' WHERE id=?", (proposed,))
    db.conn.commit()
    assert proposed not in {v["id"] for v in visuals.inbox_pending(db, None)}
    assert proposed not in {i.visual_id for i in inbox.items(db, None)}
    assert plans.to_dto(db, plans.get(db, proposed))["state"] == "closed"
