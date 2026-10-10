import pytest

from brindle import agents, antigravity, autopilot, providers
from brindle.db import DB, Workspace


def test_auth_failure_classified_and_pauses_autopilot(db, repo, monkeypatch):
    ws = db.get_workspace("main") or Workspace("ws-1", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-1", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive", autopilot=True)
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)

    # Put a mock entry in _SIGNED_IN
    providers._SIGNED_IN[("claude", (("ANTHROPIC_API_KEY", "dummy"),))] = 9999999999.0

    # Call stop-failure with 401 unauthorized
    payload = {"error": "401 Client Error: Unauthorized for url https://api.anthropic.com/v1/messages"}
    agents.handle_hook(db, worker.id, "stop-failure", payload)

    # 1. Worker status must be "failed", not "idle"
    w = db.get_agent(worker.id)
    assert w.status == "failed"

    # 2. Supervisor received notification message
    rows = db.conn.execute("SELECT body FROM inbox WHERE agent_id=?", (supervisor.id,)).fetchall()
    assert any("authentication failure" in r[0].lower() for r in rows)

    # 3. Autopilot was paused with need_user
    ap = db.get_autopilot(supervisor.id)
    assert ap.state == "blocked"
    assert "Credentials need attention" in (ap.note or "")

    # 4. _SIGNED_IN cache for claude was invalidated
    assert not any(k[0] == "claude" for k in providers._SIGNED_IN.keys())


def test_antigravity_stop_classifies_auth_failure(db, repo, monkeypatch):
    ws = Workspace("ws-agy", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-agy", 0)
    db.add_workspace(ws)
    agent = agents.spawn(db, ws, "developer", mode="assign")

    hook_calls = []
    def fake_handle_hook(db_arg, agent_id, event, payload):
        hook_calls.append((event, payload))

    monkeypatch.setattr(agents, "handle_hook", fake_handle_hook)

    antigravity.handle_hook(db, agent.id, "agy-stop", {"error": "Invalid API key: GEMINI_API_KEY expired (403)"})
    assert len(hook_calls) == 1
    event, payload = hook_calls[0]
    assert event == "stop-failure"
    assert payload.get("error_type") == "auth"
