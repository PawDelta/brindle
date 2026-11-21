"""Plans stop asking for approval when their session is closed."""

import time

import pytest

from brindle import plans, procs, tmux, visual_offers, visuals
from brindle.db import Agent, Workspace
from brindle.service import inbox, sessions
from brindle.service.errors import ServiceError


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
    "open",
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
        plans.decide(db, proposed, plan["rev"], True, person=True)
    assert db.list_tasks() == []


def test_already_closed_session_open_plan_is_not_in_inbox(db, proposed):
    sessions.close_session(db, "boss")
    # An existing stale row from before session closing also closed plans.
    db.conn.execute("UPDATE plans SET state='open' WHERE id=?", (proposed,))
    db.conn.commit()
    assert proposed not in {v["id"] for v in visuals.inbox_pending(db, None)}
    assert proposed not in {i.visual_id for i in inbox.items(db, None)}
    assert plans.to_dto(db, plans.get(db, proposed))["state"] == "closed"
