import time
import pytest

from brindle import agents, autopilot, prompt_answer
from brindle.db import DB, Workspace

pytestmark = pytest.mark.usefixtures("fake_agent_cli")


def test_rate_limit_with_session_id_401_not_treated_as_auth_failure(db, repo):
    ws = db.get_workspace("main") or Workspace("ws-rl", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-rl", 0)
    db.add_workspace(ws)
    worker = agents.spawn(db, ws, "developer", mode="assign")
    # The fake CLI's pane reads as ready, so the launch didn't sit out after_launch's 30s.
    assert prompt_answer.pending(db) == []

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


def test_selected_model_stop_failure_fails_the_worker_and_tells_its_parent(db, repo, monkeypatch):
    ws = Workspace("ws-model", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-model", 0)
    db.add_workspace(ws)
    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)
    told = []
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: told.append(to))
    agents.handle_hook(db, worker.id, "stop-failure", {
        "error": "invalid_request", "last_assistant_message": "There's an issue with the selected model "
        "(us.anthropic.claude-haiku-4-5-v1:0). It may not exist or you may not have access to it."})
    assert (db.get_agent(worker.id).status, told) == ("failed", [supervisor.id])


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
    db.update_agent(worker.id, failed_why="auth")
    db.set_status(worker.id, "failed")
    worker = db.get_agent(worker.id)

    # Worker process has exited (alive=False)
    entry = view.agent_entry(db, worker, panes={}, alive=False)
    assert entry["status"] == "failed"
    assert watch.needs_you(entry, {"autopilot": False}) == "credentials need attention"


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


def test_stop_failure_payloads_with_enums_and_last_assistant_message(db, repo):
    ws = Workspace("ws-enums", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-e", 0)
    db.add_workspace(ws)
    sup = agents.spawn(db, ws, "developer", mode="interactive", autopilot=True)
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=sup.id)

    # 1. oauth_org_not_allowed error
    agents.handle_hook(db, worker.id, "stop-failure", {"error": "oauth_org_not_allowed", "error_details": "Organization not authorized"})
    w = db.get_agent(worker.id)
    assert w.status == "failed" and w.failed_why == "auth"
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


def test_launch_resolves_effective_credentials_before_signed_out_probe(db, repo, monkeypatch):
    from brindle import keystore, providers
    from brindle.db import Workspace
    ws = Workspace("ws-launch-eff", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-le", 0)
    db.add_workspace(ws)
    worker = agents.spawn(db, ws, "developer", mode="assign")

    # Set key in keystore (no key in os.environ)
    keystore.set_key("ANTHROPIC_API_KEY", "dummy-replacement-key-555")
    providers.approve_api_key("dummy-replacement-key-555")     # Claude Code's question answered
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Fake signed_out probe: if ANTHROPIC_API_KEY is not in full_env, reports loggedIn=False
    def fake_auth_probe(argv, env=None):
        if env and env.get("ANTHROPIC_API_KEY") == "dummy-replacement-key-555":
            return 0, '{"loggedIn": true}'
        return 1, '{"loggedIn": false}'

    monkeypatch.setattr(providers, "_auth_probe", fake_auth_probe)
    monkeypatch.setattr(providers, "_SIGNED_IN", {})
    opened = []
    monkeypatch.setattr(agents, "_open_window", lambda *args, **kwargs: opened.append(True))
    # On the class: undoing a patch of the shared provider instance leaves its
    # bound method behind as an instance attribute, shadowing later class patches.
    monkeypatch.setattr(type(agents.get_provider("claude")), "after_launch", lambda self, target: None)

    agents._launch(db, worker, ws, prompt="Task", resume=None, watch_pane=False)
    assert len(opened) == 1

    # With auth: subscription, API key is denied and probe fails with AgentError
    from brindle.profiles import Profile
    sub_prof = Profile("sub-worker", "developer", "claude", "Prompt", auth="subscription")
    monkeypatch.setattr(agents, "_profile_for", lambda *args, **kwargs: sub_prof)
    with pytest.raises(agents.AgentError) as exc:
        agents._launch(db, worker, ws, prompt="Task", resume=None, watch_pane=False)
    assert "signed in" in str(exc.value).lower()


def test_continue_cmd_does_not_clear_blocked_state_when_resume_fails_or_owner_block(db, repo, monkeypatch):
    from brindle import cli, procs, workspaces
    ws = Workspace("ws-cli-block", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-cb", 0)
    db.add_workspace(ws)

    sup = agents.spawn(db, ws, "developer", mode="interactive", autopilot=True)
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=sup.id)
    db.set_status(worker.id, "failed")
    db.set_status(sup.id, "idle")
    db.update_autopilot(sup.id, state="blocked", note="Credentials need attention")

    # 1. Procs.stop fails so resume cannot restart the worker
    monkeypatch.setattr(procs, "stop", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("shutdown error")))
    monkeypatch.setattr(cli, "_attach", lambda ws, win: None)
    monkeypatch.setattr(cli, "_run", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(workspaces, "adopt_root", lambda db, cwd: ws)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.chdir(repo)

    cli.continue_cmd(session_id=None, attach=False)
    # Autopilot MUST remain blocked because worker is still failed
    ap = db.get_autopilot(sup.id)
    assert ap.state == "blocked"
    assert "Credentials need attention" in (ap.note or "")

    # 2. When worker successfully restarts, if the block was an owner decision (not credentials), it is not cleared
    db.set_status(worker.id, "failed")
    db.update_autopilot(sup.id, state="blocked", note="Need user decision on database migration")
    monkeypatch.setattr(procs, "stop", lambda *args, **kwargs: 1)
    monkeypatch.setattr(agents, "_launch", lambda db_arg, a_arg, ws_arg, **kwargs: db_arg.set_status(a_arg.id, "idle"))
    cli.continue_cmd(session_id=None, attach=False)
    ap2 = db.get_autopilot(sup.id)
    assert ap2.state == "blocked"
    assert "Need user decision on database migration" in (ap2.note or "")


def test_resume_fresh_exported_key_wins_over_stale_keystore_key(db, repo, monkeypatch):
    from brindle.keystore import _key_store
    from brindle.db import Workspace
    ws = Workspace("ws-stale-ks", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-sks", 0)
    db.add_workspace(ws)

    supervisor = agents.spawn(db, ws, "developer", mode="interactive")
    worker = agents.spawn(db, ws, "developer", mode="assign", parent_id=supervisor.id)
    db.set_status(worker.id, "failed")

    # Keystore has an old stale key (from before session started)
    _key_store("ANTHROPIC_API_KEY").save({"value": "stale-revoked-key", "updated_at": 10.0})
    db.set_session_credential(supervisor.id, "ANTHROPIC_API_KEY", "stale-revoked-key")

    # Recovery process provides a fresh exported key
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fresh-replacement-key-999")
    monkeypatch.setattr(agents, "_launch", lambda *args, **kwargs: None)

    agents.resume(db, supervisor.id)

    # Session credentials must have the fresh exported replacement, NOT the stale keystore key!
    sess_creds = db.get_session_credentials(supervisor.id)
    assert sess_creds.get("ANTHROPIC_API_KEY") == "fresh-replacement-key-999"

    # Subsequent worker delegated by supervisor gets the fresh replacement
    delegated_env = {}
    def capture_launch(db_arg, agent_arg, ws_arg, **kwargs):
        delegated_env.update(agents.agent_env(ws_arg, agent_arg.id, agent_arg))

    monkeypatch.setattr(agents, "_launch", capture_launch)
    worker2, _ = agents.delegate(db, supervisor, ws, "developer", "Next task", "assign")
    assert delegated_env.get("ANTHROPIC_API_KEY") == "fresh-replacement-key-999"


def test_resume_store_rotation_wins_over_stale_inherited_shell_env(db, repo, monkeypatch):
    from brindle.db import Workspace
    from brindle import keystore, identity_lock
    ws = Workspace("ws-rot", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-rot", 0)
    db.add_workspace(ws)

    sup = agents.spawn(db, ws, "developer", mode="interactive")
    db.set_status(sup.id, "paused")
    db.set_session_credential(sup.id, "ANTHROPIC_API_KEY", "stale-inherited-value", updated_at=10.0)
    # Keystore has a freshly rotated key with recent timestamp
    monkeypatch.setattr(keystore, "known_names", lambda *args: ["ANTHROPIC_API_KEY"])
    monkeypatch.setattr(keystore, "get_key_info", lambda name: ("fresh-store-value", 100.0))
    monkeypatch.setattr(agents, "_launch", lambda db_arg, a_arg, ws_arg, **kwargs: db_arg.set_status(a_arg.id, "idle"))
    monkeypatch.setattr(identity_lock, "ensure", lambda a: None)

    # Shell retains stale inherited value
    agents.resume(db, sup.id, recovery_env={"ANTHROPIC_API_KEY": "stale-inherited-value"})
    resolved = agents.agent_env(ws, sup.id, sup, m=None)
    assert resolved.get("ANTHROPIC_API_KEY") == "fresh-store-value"


def test_unavailable_keystore_does_not_abort_launch_or_resume(db, repo, monkeypatch):
    from brindle.db import Workspace
    from brindle import keystore, identity_lock
    ws = Workspace("ws-unavail", str(repo), "main", "main", "main", "main", str(repo), 20000, "tmux-unavail", 0)
    db.add_workspace(ws)

    sup = agents.spawn(db, ws, "developer", mode="interactive")
    db.set_status(sup.id, "paused")
    # Simulate unavailable keychain / Secret Service
    monkeypatch.setattr(keystore, "known_names", lambda *args: ["ANTHROPIC_API_KEY"])
    def fail_keystore(name):
        raise keystore.CredentialError("keychain locked")
    monkeypatch.setattr(keystore, "get_key_info", fail_keystore)
    monkeypatch.setattr(agents, "_launch", lambda db_arg, a_arg, ws_arg, **kwargs: db_arg.set_status(a_arg.id, "idle"))
    monkeypatch.setattr(identity_lock, "ensure", lambda a: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "valid-exported-key")

    # Resume must succeed despite unavailable keystore
    resumed = agents.resume(db, sup.id)
    assert len(resumed) == 1

    # agent_env must succeed and select ambient valid exported key
    env = agents.agent_env(ws, sup.id, sup, m=None)
    assert env.get("ANTHROPIC_API_KEY") == "valid-exported-key"
    assert len(resumed) == 1

    # agent_env must succeed and select ambient valid exported key
    env = agents.agent_env(ws, sup.id, sup, m=None)
    assert env.get("ANTHROPIC_API_KEY") == "valid-exported-key"


