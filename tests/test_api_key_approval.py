"""Approving a stored API key for Claude Code (`approve_api_key`), the opt-in
question in `brindle keys set` / `brindle doctor --fix`, and Claude Code's
org-managed settings taking over the credential. Fake keys only."""

import json
import os
import stat
import time

from typer.testing import CliRunner

from brindle import agents, doctor, keystore, providers, quota, tmux, workspaces
from brindle.cli import app
from brindle.db import Agent

KEY = "sk-ant-api03-FAKEFAKEFAKEFAKE-0123456789abcdefXYZ"
runner = CliRunner()


def _config(tmp_path):
    return tmp_path / "claude-config" / ".claude.json"


def _managed(tmp_path, monkeypatch, content):
    path = tmp_path / "managed-settings.json"
    path.write_text(content)
    monkeypatch.setenv("BRINDLE_CLAUDE_MANAGED_SETTINGS", str(path))
    return path


# -- approve_api_key -----------------------------------------------------------------------------

def test_approve_writes_only_the_last_20_chars_at_0600(tmp_path):
    path = providers.approve_api_key(KEY)
    assert path == str(_config(tmp_path).resolve())
    assert stat.S_IMODE(_config(tmp_path).stat().st_mode) == 0o600
    data = json.loads(_config(tmp_path).read_text())
    assert data == {"customApiKeyResponses": {"approved": [KEY[-20:]], "rejected": []}}
    assert KEY not in _config(tmp_path).read_text()
    assert "hasCompletedOnboarding" not in data   # none of the CI seeding


def test_approve_merges_and_unrejects(tmp_path):
    _config(tmp_path).parent.mkdir()
    _config(tmp_path).write_text(json.dumps({"numStartups": 3, "customApiKeyResponses": {
        "approved": ["other"], "rejected": [KEY[-20:]]}}))
    _config(tmp_path).chmod(0o600)
    providers.approve_api_key(KEY)
    providers.approve_api_key(KEY)   # idempotent
    data = json.loads(_config(tmp_path).read_text())
    assert data["numStartups"] == 3
    assert data["customApiKeyResponses"] == {"approved": ["other", KEY[-20:]], "rejected": []}


def test_seed_ci_config_still_seeds_onboarding_trust_and_the_key(tmp_path, repo):
    providers.seed_ci_config([str(repo)], KEY)
    data = json.loads(_config(tmp_path).read_text())
    assert data["hasCompletedOnboarding"] is True and data["theme"]
    assert data["projects"][os.path.realpath(repo)] == {"hasTrustDialogAccepted": True}
    assert data["customApiKeyResponses"]["approved"] == [KEY[-20:]]


# -- the opt-in question -------------------------------------------------------------------------

def test_keys_set_yes_approves(tmp_path):
    r = runner.invoke(app, ["keys", "set", "ANTHROPIC_API_KEY"], input=f"{KEY}\ny\n")
    assert r.exit_code == 0, r.output
    assert keystore.get_key("ANTHROPIC_API_KEY") == KEY
    assert json.loads(_config(tmp_path).read_text())["customApiKeyResponses"]["approved"] == [KEY[-20:]]
    assert KEY not in r.output


def test_keys_set_declining_leaves_the_config_untouched(tmp_path):
    r = runner.invoke(app, ["keys", "set", "ANTHROPIC_API_KEY"], input=f"{KEY}\nn\n")
    assert r.exit_code == 0, r.output
    assert keystore.get_key("ANTHROPIC_API_KEY") == KEY   # stored all the same
    assert not _config(tmp_path).exists()


def test_keys_set_without_an_answer_declines(tmp_path):
    r = runner.invoke(app, ["keys", "set", "ANTHROPIC_API_KEY"], input=f"{KEY}\n")
    assert r.exit_code == 0, r.output
    assert not _config(tmp_path).exists()


def test_keys_set_asks_only_about_the_anthropic_key(tmp_path):
    r = runner.invoke(app, ["keys", "set", "OPENAI_API_KEY"], input="sk-test-openai-0000\ny\n")
    assert r.exit_code == 0, r.output
    assert not _config(tmp_path).exists()


def test_doctor_fix_declined_leaves_the_config_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    asked = []
    out = doctor.fix(lambda p: asked.append(p) or False)
    assert len(asked) == 1 and "ANTHROPIC_API_KEY" in out[0] and KEY not in asked[0]
    assert not _config(tmp_path).exists()


def test_doctor_fix_accepted_approves_and_then_stops_asking(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    doctor.fix(lambda p: True)
    assert json.loads(_config(tmp_path).read_text())["customApiKeyResponses"]["approved"] == [KEY[-20:]]
    assert doctor.fix(lambda p: 1 / 0) == ["ANTHROPIC_API_KEY: already approved in Claude Code"]


def test_doctor_fix_cli_declines_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setattr(doctor, "checks", lambda root: [])
    r = runner.invoke(app, ["doctor", "--fix"], input="\n")
    assert r.exit_code == 0, r.output
    assert not _config(tmp_path).exists()


def test_the_first_run_message_points_at_brindle_keys_set():
    what = providers.ClaudeCode.first_run_screen("Do you want to use this API key?\n")
    assert "brindle keys set" in what


# -- org-managed settings --------------------------------------------------------------------------

def test_managed_api_key_helper_is_detected(tmp_path, monkeypatch):
    assert providers.claude_org_managed() is None
    _managed(tmp_path, monkeypatch, json.dumps({"apiKeyHelper": "/opt/bin/get-key"}))
    assert providers.claude_org_managed() == "apiKeyHelper"


def test_managed_env_with_a_base_url_or_credential_is_detected(tmp_path, monkeypatch):
    _managed(tmp_path, monkeypatch, json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gw.example"}}))
    assert providers.claude_org_managed() == "ANTHROPIC_BASE_URL"
    _managed(tmp_path, monkeypatch, json.dumps({"env": {"DISABLE_TELEMETRY": "1"}}))
    assert providers.claude_org_managed() is None


def test_unreadable_managed_settings_count_as_absent(tmp_path, monkeypatch):
    path = _managed(tmp_path, monkeypatch, "{not json")
    assert providers.claude_org_managed() is None
    path.write_text(json.dumps({"apiKeyHelper": "x"}))
    path.chmod(0)
    try:
        if os.geteuid() != 0:   # root reads it anyway
            assert providers.claude_org_managed() is None
    finally:
        path.chmod(0o600)
    monkeypatch.setenv("BRINDLE_CLAUDE_MANAGED_SETTINGS", str(tmp_path / "missing.json"))
    assert providers.claude_org_managed() is None


_branches = iter(range(1000))


def _launch(db, repo, monkeypatch, *profiles):
    ws = workspaces.create(db, str(repo), f"keys{next(_branches)}").workspace
    seen = {}

    def fake_new_window(session, name, cwd, command, env, tag=None, keep=(), **kwargs):
        seen[name] = dict(env)
        return "%1"
    monkeypatch.setattr(tmux, "new_window", fake_new_window)
    monkeypatch.setattr(tmux, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "apply_theme", lambda *a, **k: None)
    for agent_id, profile, provider in profiles:
        a = Agent(agent_id, ws.id, profile, provider, None, "assign", "starting", "", None, time.time())
        db.add_agent(a)
        agents._open_window(db, a, ws, agent_id, ["sleep", "1"], watch_pane=False)
    return seen


def test_org_managed_settings_mean_no_claude_key_injection(db, repo, tmp_path, monkeypatch):
    keystore.set_key("ANTHROPIC_API_KEY", "sk-ant-test-stored-9999")
    keystore.set_key("OPENAI_API_KEY", "sk-test-codex-8888")
    # Without managed settings the stored key reaches the pane...
    assert _launch(db, repo, monkeypatch, ("c0", "developer", "claude"))["c0"]["ANTHROPIC_API_KEY"]
    # ...with them it doesn't, though other providers still get theirs.
    _managed(tmp_path, monkeypatch, json.dumps({"apiKeyHelper": "/opt/bin/get-key"}))
    seen = _launch(db, repo, monkeypatch, ("c1", "developer", "claude"), ("x1", "developer", "codex"))
    assert "ANTHROPIC_API_KEY" not in seen["c1"]
    assert seen["x1"]["OPENAI_API_KEY"] == "sk-test-codex-8888"


def test_org_managed_settings_mean_no_provider_config_env(db, repo, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from brindle.pro import managed_models

    cfg = SimpleNamespace(provider="bedrock", region="us-east-1", endpoint=None, model_ids=(), project=None)
    m = SimpleNamespace(config=cfg, denied_keys=())
    ws = workspaces.create(db, str(repo), "pc").workspace
    a = Agent("c2", ws.id, "developer", "claude", None, "assign", "starting", "", None, time.time())
    db.add_agent(a)
    assert agents.agent_env(ws, "c2", a, m)["CLAUDE_CODE_USE_BEDROCK"] == "1"
    _managed(tmp_path, monkeypatch, json.dumps({"apiKeyHelper": "/opt/bin/get-key"}))
    assert "CLAUDE_CODE_USE_BEDROCK" not in agents.agent_env(ws, "c2", a, m)
    assert managed_models.provider_env(cfg, "claude")   # the helper itself is unchanged


# -- quota and cost ------------------------------------------------------------------------------

def test_quota_says_pay_per_token_for_a_key(monkeypatch):
    quota.record("claude", [quota.Window(95.0, time.time() + 3600, 300)], source="status line")
    assert "95%" in quota.note("claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert "pay per token" in quota.note("claude") and "%" not in quota.note("claude")
    assert quota.headroom("claude") == 100.0


def test_cost_footer_drops_the_subscription_caveat_for_a_key(monkeypatch):
    from brindle import cost

    assert "subscription" in "".join(cost._footer())
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert "subscription" not in "".join(cost._footer())
