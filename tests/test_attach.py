"""Issue #35: `brindle attach` opens the agent that needs you, and the errors
around it say what happened."""

import time

from brindle import agents, mcp_server, workspaces
from brindle.db import Agent


def add(db, ws, aid, status, window, created):
    db.add_agent(Agent(aid, ws.id, "developer", "claude", None, "assign", status, window, None, created))


def test_attach_target_prefers_the_waiting_agent(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/x").workspace
    now = time.time()
    add(db, ws, "old", "processing", "%1", now - 30)
    add(db, ws, "stuck", "waiting", "%2", now - 20)
    add(db, ws, "new", "idle", "%3", now - 10)
    add(db, ws, "gone", "waiting", "%4", now)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id != "gone")
    assert agents.attach_target(db, ws) == "%2"
    db.update_agent("stuck", status="idle")
    assert agents.attach_target(db, ws) == "%1"        # then the busiest
    db.update_agent("old", status="idle")
    assert agents.attach_target(db, ws) == "%3"        # then the newest



