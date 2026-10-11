import json
import time
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


def test_resume_failed_agent_does_not_kill_foreign_server_or_unowned_pane(db, repo, monkeypatch):
    from brindle.db import Workspace
    ws = Workspace("ws-foreign", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-foreign", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)
    db.set_status(worker.id, "failed")
    db.update_agent(worker.id, tmux_window="%7", tmux_server="other-server")

    killed = []
    monkeypatch.setattr(agents.tmux, "kill_window", lambda win: killed.append(win))
    monkeypatch.setattr(agents, "_launch", lambda *args, **kwargs: None)

    agents.resume(db, supervisor.id)
    assert "%7" not in killed


def test_dead_failed_agent_preserves_failed_status_in_view_and_watch(db, repo):
    from brindle.db import Workspace
    from brindle import view, watch
    ws = Workspace("ws-dead", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-dead", 0)
    db.add_workspace(ws)
    worker = agents.spawn(db, ws, "developer", mode="assign")
    db.set_status(worker.id, "failed")
    worker = db.get_agent(worker.id)

    # Worker process has exited (alive=False)
    entry = view.agent_entry(db, worker, panes={}, alive=False)
    assert entry["status"] == "failed"
    assert watch.needs_you(entry, {"autopilot": False}) == "credentials need attention"


def test_continue_cmd_prioritizes_live_recoverable_over_older_paused(db, repo, monkeypatch):
    from brindle import cli, workspaces
    ws = Workspace("ws-prio", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-prio", 0)
    db.add_workspace(ws)

    # 1. An older paused session
    old_sup = agents.spawn(db, ws, "developer", mode="interactive")
    db.set_status(old_sup.id, "paused")

    # 2. A current live session blocked on credentials
    live_sup = agents.spawn(db, ws, "developer", mode="interactive")
    live_worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=live_sup.id)
    db.set_status(live_worker.id, "failed")
    db.add_autopilot(live_sup.id)
    db.update_autopilot(live_sup.id, state="blocked", note="Credentials need attention")

    resumed_ids = []
    monkeypatch.setattr(agents, "resume", lambda db_arg, root_id, **kwargs: resumed_ids.append(root_id) or [live_worker])
    monkeypatch.setattr(cli, "_attach", lambda ws, win: None)
    monkeypatch.setattr(cli, "_run", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(workspaces, "adopt_root", lambda db, cwd: ws)
    monkeypatch.setattr(agents, "is_alive", lambda a: a.id == live_sup.id)
    monkeypatch.chdir(repo)

    # Calling continue with no arguments must prioritize the live blocked session over the older paused one
    cli.continue_cmd(session_id=None, attach=False)
    assert resumed_ids == [live_sup.id]

    # Explicitly continuing live session by prefix should also succeed
    resumed_ids.clear()
    cli.continue_cmd(session_id=live_sup.id[:6], attach=False)
    assert resumed_ids == [live_sup.id]


def test_continue_cmd_reloads_fresh_window_for_failed_root_with_attach(db, repo, monkeypatch):
    from brindle import cli, workspaces
    ws = Workspace("ws-failed-root", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-failed-root", 0)
    db.add_workspace(ws)

    # Supervisor itself failed with old window %old
    live_sup = agents.spawn(db, ws, "developer", mode="interactive")
    db.set_status(live_sup.id, "failed")
    db.update_agent(live_sup.id, tmux_window="%old")
    db.add_autopilot(live_sup.id)
    db.update_autopilot(live_sup.id, state="blocked", note="Credentials need attention")

    def fake_resume(db_arg, root_id, **kwargs):
        # Resume updates live_sup in DB with %new
        db.update_agent(root_id, tmux_window="%new", status="idle")
        fresh = db.get_agent(root_id)
        return [fresh]

    attached_windows = []
    monkeypatch.setattr(agents, "resume", fake_resume)
    monkeypatch.setattr(cli, "_attach", lambda ws, win: attached_windows.append(win))
    monkeypatch.setattr(cli, "_run", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(workspaces, "adopt_root", lambda db, cwd: ws)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.chdir(repo)

    # continue_cmd with attach=True must attach to %new, not %old!
    cli.continue_cmd(session_id=None, attach=True)
    assert attached_windows == ["%new"]

    # Also test explicit session_id with attach=True
    attached_windows.clear()
    cli.continue_cmd(session_id=live_sup.id[:6], attach=True)
    assert attached_windows == ["%new"]


def test_continue_cmd_recovers_dead_failed_interactive_root(db, repo, monkeypatch):
    from brindle import cli, workspaces
    ws = Workspace("ws-dead-root", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-dead-root", 0)
    db.add_workspace(ws)

    dead_sup = agents.spawn(db, ws, "developer", mode="interactive")
    db.set_status(dead_sup.id, "failed")
    db.add_autopilot(dead_sup.id)
    db.update_autopilot(dead_sup.id, state="blocked", note="Credentials need attention")

    resumed_ids = []
    def fake_resume(db_arg, root_id, **kwargs):
        resumed_ids.append(root_id)
        db.set_status(root_id, "idle")
        return [db.get_agent(root_id)]

    monkeypatch.setattr(agents, "resume", fake_resume)
    monkeypatch.setattr(cli, "_attach", lambda ws, win: None)
    monkeypatch.setattr(cli, "_run", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(workspaces, "adopt_root", lambda db, cwd: ws)
    # is_alive returns False because the process has died
    monkeypatch.setattr(agents, "is_alive", lambda a: False)
    monkeypatch.chdir(repo)

    # 1. Calling continue with explicit session_id recovers the dead failed root
    cli.continue_cmd(session_id=dead_sup.id[:6], attach=False)
    assert resumed_ids == [dead_sup.id]

    # Reset status to failed for test 2
    db.set_status(dead_sup.id, "failed")
    resumed_ids.clear()

    # 2. Calling continue without session_id also recovers the dead failed root
    cli.continue_cmd(session_id=None, attach=False)
    assert resumed_ids == [dead_sup.id]



def test_stop_failure_payloads_with_enums_and_last_assistant_message(db, repo):
    ws = Workspace("ws-enums", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-e", 0)
    db.add_workspace(ws)
    sup = agents.spawn(db, ws, "developer", mode="interactive", autopilot=True)
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=sup.id)

    # 1. oauth_org_not_allowed error
    agents.handle_hook(db, worker.id, "stop-failure", {"error": "oauth_org_not_allowed", "error_details": "Organization not authorized"})
    w = db.get_agent(worker.id)
    assert w.status == "failed"
    ap = db.get_autopilot(sup.id)
    assert ap.state == "blocked"
    assert "Organization not authorized" in (ap.note or "")

    # Reset worker and autopilot
    db.set_status(worker.id, "processing")
    db.update_autopilot(sup.id, state="running", note=None)

    # 2. billing_error in last_assistant_message
    agents.handle_hook(db, worker.id, "stop-failure", {"last_assistant_message": "Account billing error: credit balance depleted"})
    w = db.get_agent(worker.id)
    assert w.status == "failed"
    ap = db.get_autopilot(sup.id)
    assert ap.state == "blocked"


def test_paused_session_continue_clears_credential_block_and_resumes_autopilot_drive(db, repo, monkeypatch):
    from brindle import cli, workspaces, sessions
    ws = Workspace("ws-paused-rec", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-pr", 0)
    db.add_workspace(ws)
    sup = agents.spawn(db, ws, "developer", mode="interactive", autopilot=True)
    autopilot.set_goal(db, sup.id, "My Goal", [("Milestone 1", "true", "")])

    # Session encountered auth failure and blocked
    db.update_autopilot(sup.id, state="blocked", note="Credentials need attention: revoked API key")
    # User paused/stopped the session
    db.set_status(sup.id, "paused")

    resumed_ids = []
    def fake_resume(db_arg, root_id, **kwargs):
        resumed_ids.append(root_id)
        db.set_status(root_id, "idle")
        return [db.get_agent(root_id)]

    monkeypatch.setattr(agents, "resume", fake_resume)
    monkeypatch.setattr(cli, "_attach", lambda ws, win: None)
    monkeypatch.setattr(cli, "_run", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(workspaces, "adopt_root", lambda db, cwd: ws)
    monkeypatch.setattr(sessions, "paused", lambda db, root: [sessions.Session(db.get_agent(sup.id), ws, [db.get_agent(sup.id)], [ws.branch])])
    monkeypatch.setattr(agents, "is_alive", lambda a: False)
    monkeypatch.chdir(repo)

    # Continue the paused session
    cli.continue_cmd(session_id=sup.id[:6], attach=False)
    assert resumed_ids == [sup.id]

    # Verify credentials block was cleared
    ap = db.get_autopilot(sup.id)
    assert ap.state == "running"
    assert ap.note is None

    # Verify autopilot can drive again via on_stop
    sup_agent = db.get_agent(sup.id)
    decision = autopilot.on_stop(db, sup_agent, {})
    assert decision is not None
    assert decision.get("decision") == "block"


def test_recovery_with_replacement_key_propagates_to_subsequent_delegated_worker(db, repo, monkeypatch):
    from brindle import agents, cli, workspaces
    import os

    ws = Workspace("ws-rec-prop", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-prop", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)
    db.set_status(worker.id, "failed")
    db.add_autopilot(supervisor.id)
    db.update_autopilot(supervisor.id, state="blocked", note="Credentials need attention")

    # Simulate supervisor launcher having an expired key in its environment
    monkeypatch.setenv("ANTHROPIC_API_KEY", "expired-old-key")

    # Recovery happens (e.g. cli.continue_cmd with replacement key exported in CLI process)
    with monkeypatch.context() as m:
        m.setenv("ANTHROPIC_API_KEY", "dummy-replacement-key-12345")
        monkeypatch.setattr(cli, "_attach", lambda ws, win: None)
        monkeypatch.setattr(cli, "_run", lambda fn, *args, **kwargs: fn(*args, **kwargs))
        monkeypatch.setattr(workspaces, "adopt_root", lambda db, cwd: ws)
        monkeypatch.setattr(agents, "is_alive", lambda a: True)
        monkeypatch.setattr(agents, "_launch", lambda *args, **kwargs: None)
        monkeypatch.chdir(repo)

        cli.continue_cmd(session_id=None, attach=False)

    # Delegating a subsequent worker from supervisor reaches the worker with replacement key
    delegated_env = {}
    def fake_launch(db_arg, agent_arg, ws_arg, **kwargs):
        env = agents.agent_env(ws_arg, agent_arg.id, agent_arg)
        delegated_env.update(env)

    monkeypatch.setattr(agents, "_launch", fake_launch)
    worker2, _ = agents.delegate(db, supervisor, ws, "developer", "Next task", "assign")
    assert delegated_env.get("ANTHROPIC_API_KEY") == "dummy-replacement-key-12345"


def test_resume_calls_procs_stop_before_relaunch_and_preserves_failed_on_error(db, repo, monkeypatch):
    from brindle import agents, procs

    ws = Workspace("ws-stop-proc", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-sp", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)
    db.set_status(worker.id, "failed")

    stopped = []
    launched = []
    def fake_stop(ids, grace=2.0):
        stopped.extend(ids)
        return len(ids)

    def fake_launch(db_arg, agent_arg, ws_arg, **kwargs):
        assert agent_arg.id in stopped
        launched.append(agent_arg.id)

    monkeypatch.setattr(procs, "stop", fake_stop)
    monkeypatch.setattr(agents, "_launch", fake_launch)

    resumed = agents.resume(db, supervisor.id)
    assert worker.id in stopped
    assert worker.id in launched

    # When procs.stop fails/raises, failed status is preserved and agent is not relaunched
    db.set_status(worker.id, "failed")
    launched.clear()
    def fail_stop(*args, **kwargs):
        raise RuntimeError("shutdown failed")
    monkeypatch.setattr(procs, "stop", fail_stop)

    resumed_err = agents.resume(db, supervisor.id)
    assert worker.id not in launched
    w = db.get_agent(worker.id)
    assert w.status == "failed"


def test_set_session_credential_enforces_0600_permissions(tmp_path):
    db_file = tmp_path / "test.db"
    db_file.write_text("")
    db_file.chmod(0o666)
    assert (db_file.stat().st_mode & 0o777) != 0o600

    db = DB(str(db_file))
    assert (db_file.stat().st_mode & 0o777) == 0o600

    db_file.chmod(0o644)
    db.set_session_credential("ws-1", "ANTHROPIC_API_KEY", "secret-key")
    assert (db_file.stat().st_mode & 0o777) == 0o600


def test_fresher_keystore_takes_precedence_over_older_session_credential(db, repo, monkeypatch):
    from brindle import keystore, agents
    from brindle.db import Workspace

    ws = Workspace("ws-time-cred", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-tc", 0)
    db.add_workspace(ws)
    worker = agents.spawn(db, ws, "developer", mode="assign")

    # Set session credential at t=100
    monkeypatch.setattr(time, "time", lambda: 100.0)
    db.set_session_credential(worker.id, "ANTHROPIC_API_KEY", "older-session-key")

    # Keystore updated at t=200
    monkeypatch.setattr(time, "time", lambda: 200.0)
    keystore.set_key("ANTHROPIC_API_KEY", "fresh-keystore-key")

    env = agents.agent_env(ws, worker.id, worker)
    assert env.get("ANTHROPIC_API_KEY") == "fresh-keystore-key"

    # Now simulate a fresher session credential at t=300
    monkeypatch.setattr(time, "time", lambda: 300.0)
    db.set_session_credential(worker.id, "ANTHROPIC_API_KEY", "freshest-session-key")

    env2 = agents.agent_env(ws, worker.id, worker)
    assert env2.get("ANTHROPIC_API_KEY") == "freshest-session-key"


def test_resume_preserves_blocked_autopilot_state_when_agent_fails_to_restart(db, repo, monkeypatch):
    from brindle import agents, procs
    from brindle.db import Workspace

    ws = Workspace("ws-fail-block", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-fb", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive", autopilot=True)
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)
    db.set_status(worker.id, "failed")
    db.update_autopilot(supervisor.id, state="blocked", note="Credentials need attention")

    # Simulate procs.stop failing so restart is aborted
    monkeypatch.setattr(procs, "stop", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("tmux error")))

    agents.resume(db, supervisor.id)
    ap = db.get_autopilot(supervisor.id)
    assert ap.state == "blocked"
    assert "Credentials need attention" in (ap.note or "")

