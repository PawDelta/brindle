import json
import pytest

from brindle import agents, antigravity, autopilot, providers, watch
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

    # 5. watch.py needs_you flags the failed agent
    agent_dict = {"id": worker.id, "status": "failed", "profile": "developer", "provider": "claude"}
    ws_dict = {"id": ws.id, "branch": "feat/x"}
    assert watch.needs_you(agent_dict, ws_dict) == "credentials need attention"


def test_rate_limit_with_session_id_401_not_treated_as_auth_failure(db, repo):
    ws = db.get_workspace("main") or Workspace("ws-rl", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-rl", 0)
    db.add_workspace(ws)
    worker = agents.spawn(db, ws, "developer", mode="assign")

    # A rate limit payload containing '401' only in session_id or transcript path
    payload = {
        "error": "rate_limit_error: Rate limit exceeded (429)",
        "session_id": "session-401abc",
        "path": "/logs/transcript-401.jsonl",
    }
    agents.handle_hook(db, worker.id, "stop-failure", payload)
    w = db.get_agent(worker.id)
    # Must NOT become failed
    assert w.status != "failed"


def test_resume_restarts_failed_worker(db, repo, monkeypatch):
    ws = db.get_workspace("main") or Workspace("ws-res", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-res", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)
    db.set_status(worker.id, "failed")

    restarted = []
    def fake_launch(db_arg, agent_arg, ws_arg, **kwargs):
        restarted.append(agent_arg.id)

    monkeypatch.setattr(agents, "_launch", fake_launch)
    resumed = agents.resume(db, supervisor.id)
    assert any(a.id == worker.id for a in resumed)
    assert worker.id in restarted


def test_cross_process_invalidation_timestamp(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    monkeypatch.setattr(providers, "brindle_home", lambda: tmp_path)
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path))
    providers._SIGNED_IN[("claude", ())] = 100.0

    providers.invalidate_signed_in("claude")
    # seen_signed_in in this process must return False
    assert not providers.seen_signed_in("claude")

    # Now verify in a separate subprocess that the invalidation file is recognized across processes
    code = (
        "import sys, os, time\n"
        "from brindle import providers\n"
        "providers._SIGNED_IN[('claude', ())] = time.time() - 30\n"
        "time.sleep(0.05)\n"
        "providers.invalidate_signed_in('claude')\n"
        "assert not providers.seen_signed_in('claude'), 'failed in parent'\n"
        "sys.exit(0)\n"
    )
    res = subprocess.run([sys.executable, "-c", code], env={**os.environ, "BRINDLE_HOME": str(tmp_path)}, capture_output=True, text=True)
    assert res.returncode == 0, f"Subprocess failed:\n{res.stderr}"


def test_continue_cmd_recovers_live_session_with_failed_workers(db, repo, monkeypatch):
    from brindle import cli, workspaces

    ws = Workspace("ws-live", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-live", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)
    db.set_status(worker.id, "failed")
    db.add_autopilot(supervisor.id)
    db.update_autopilot(supervisor.id, state="blocked", note="Credentials need attention")

    resumed_ids = []
    def fake_resume(db_arg, root_id, **kwargs):
        resumed_ids.append(root_id)
        return [worker]

    monkeypatch.setattr(agents, "resume", fake_resume)
    monkeypatch.setattr(cli, "_attach", lambda ws, win: None)
    monkeypatch.setattr(cli, "_run", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(workspaces, "adopt_root", lambda db, cwd: ws)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.chdir(repo)

    cli.continue_cmd(session_id=None, attach=False)
    assert supervisor.id in resumed_ids
    ap = db.get_autopilot(supervisor.id)
    assert ap.state == "running"
    assert ap.note is None


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


def test_fresh_exported_key_included_in_agent_env(db, repo):
    from brindle.db import Workspace, Agent
    from brindle import agents, keystore
    import os
    from unittest.mock import patch

    ws = Workspace("ws-auth-rot", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-rot", 0)
    db.add_workspace(ws)
    a = Agent(id="a", workspace_id=ws.id, profile="developer", provider="claude",
              parent_id=None, mode="assign", status="failed", tmux_window="", result=None, created_at=0)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "new-rotated-key"}), \
         patch.object(agents.company_login, "stored_env", return_value={}), \
         patch.object(agents.company_identity, "enforced_route_env", return_value=None), \
         patch.object(agents.providers, "claude_org_managed", return_value=False):
        env = agents.agent_env(ws, "a", a, m=None)
        env = {**keystore.pane_keys({"ANTHROPIC_API_KEY"}, env), **env}
        assert env.get("ANTHROPIC_API_KEY") == "new-rotated-key"
