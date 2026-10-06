"""A CLI that isn't signed in stops a launch with how to sign in, instead of
opening on its login screen with brindle's prompt typed into it."""

import json

import pytest

from brindle import agents, antigravity, autopilot, doctor, providers, workspaces
from brindle.config import load_repo_config

AGY_PROFILE = ("---\nname: developer-antigravity\nprovider: antigravity\n"
               "permission_mode: acceptEdits\n---\nYou are a developer agent.\n")


def agy_profile(repo, monkeypatch, tmp_path):
    """A profile on agy, with a stand-in `agy` binary that counts as installed."""
    agents_dir = repo / ".brindle" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "developer-antigravity.md").write_text(AGY_PROFILE)
    agy = tmp_path / "agy"
    agy.write_text("#!/bin/sh\n")
    agy.chmod(0o755)
    monkeypatch.setenv("BRINDLE_AGY_BIN", str(agy))


def fake_probe(monkeypatch, replies):
    calls = []

    def probe(argv):
        calls.append(argv)
        return replies.get(argv[1])
    monkeypatch.setattr(providers, "_auth_probe", probe)
    for keys in providers._ENV_AUTH.values():
        for k in keys:
            monkeypatch.delenv(k, raising=False)
    return calls


CLAUDE_OUT = (1, json.dumps({"loggedIn": False, "authMethod": "none"}))
CLAUDE_IN = (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
CODEX_OUT = (1, "Not logged in\n")
# agy 1.2.16 has no status command; `agy models` says this when signed out...
AGY_OUT = (1, "Fetching available models...\nError: Please sign in to view available models. "
              "Launch the CLI without arguments to sign in.\n")
# ...this with modelProvider "gemini" in settings.json but no key...
AGY_NO_KEY = (1, 'modelProvider is set to "gemini" in settings.json, but the GEMINI_API_KEY '
                 "environment variable is not set. Set GEMINI_API_KEY to your Gemini API key, or "
                 'remove "modelProvider" from settings.json to use the default backend.\n')
# ...and lists models when signed in (or on the Gemini API with a key).
AGY_IN = (0, "Fetching available models...\ngemini-3.1-pro-high\tGemini 3.1 Pro (High)\n")


def test_signed_out_claude_and_codex(monkeypatch):
    fake_probe(monkeypatch, {"auth": CLAUDE_OUT, "login": CODEX_OUT})
    assert "claude auth login" in providers.signed_out("claude")
    assert "codex login" in providers.signed_out("codex")


def test_signed_out_antigravity(monkeypatch):
    calls = fake_probe(monkeypatch, {"models": AGY_OUT})
    why = providers.signed_out("antigravity")
    assert "run `agy`" in why and "GEMINI_API_KEY" in why
    assert calls == [[antigravity.binary(), "models"]]
    assert providers.signed_out("antigravity") == why   # a no isn't remembered
    assert len(calls) == 2


def test_antigravity_on_the_gemini_api_without_a_key(monkeypatch):
    fake_probe(monkeypatch, {"models": AGY_NO_KEY})
    why = providers.signed_out("antigravity")
    assert "GEMINI_API_KEY isn't set" in why and "modelProvider" in why


def test_signed_in_antigravity_is_remembered(monkeypatch):
    calls = fake_probe(monkeypatch, {"models": AGY_IN})
    assert providers.signed_out("antigravity") is None
    assert providers.signed_out("antigravity") is None
    assert len(calls) == 1


def test_unknown_answers_never_block(monkeypatch):
    # An older CLI without the status command, a timeout, or an odd reply.
    fake_probe(monkeypatch, {"auth": (1, "error: unknown command 'auth'"), "login": (2, "boom"),
                             "models": (1, "Error: fetching models: connection refused")})
    assert providers.signed_out("claude") is None
    assert providers.signed_out("codex") is None
    assert providers.signed_out("antigravity") is None
    assert providers.signed_out("native") is None


def test_env_credentials_skip_the_check(monkeypatch):
    calls = fake_probe(monkeypatch, {"auth": CLAUDE_OUT, "models": AGY_OUT})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert providers.signed_out("claude") is None
    assert calls == []
    # agy's only environment credential (with modelProvider "gemini" in its settings).
    assert providers._ENV_AUTH["antigravity"] == ("GEMINI_API_KEY",)
    assert "GEMINI_API_KEY" not in providers._ENV_AUTH["claude"] + providers._ENV_AUTH["codex"]
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    assert providers.signed_out("antigravity") is None
    assert calls == []


def test_signed_in_is_remembered_signed_out_is_not(monkeypatch):
    calls = fake_probe(monkeypatch, {"auth": CLAUDE_IN})
    assert providers.signed_out("claude") is None
    assert providers.signed_out("claude") is None
    assert len(calls) == 1
    providers._SIGNED_IN.clear()
    calls = fake_probe(monkeypatch, {"auth": CLAUDE_OUT})
    providers.signed_out("claude")
    providers.signed_out("claude")
    assert len(calls) == 2


def test_spawn_refuses_and_leaves_no_agent(db, repo, monkeypatch):
    fake_probe(monkeypatch, {"login": CODEX_OUT})
    ws = workspaces.adopt_root(db, str(repo))
    with pytest.raises(agents.AgentError, match="codex login"):
        agents.spawn(db, ws, "reviewer-codex", prompt="review", mode="review")
    assert db.list_agents() == []


def test_spawn_refuses_a_signed_out_antigravity_worker(db, repo, monkeypatch, tmp_path):
    fake_probe(monkeypatch, {"models": AGY_OUT})
    agy_profile(repo, monkeypatch, tmp_path)
    ws = workspaces.adopt_root(db, str(repo))
    with pytest.raises(agents.AgentError, match="Antigravity isn't signed in: run `agy`"):
        agents.spawn(db, ws, "developer-antigravity", prompt="write the readme")
    assert db.list_agents() == []


def test_routing_skips_a_signed_out_cli(repo, monkeypatch, tmp_path):
    fake_probe(monkeypatch, {"login": CODEX_OUT, "models": AGY_OUT})
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    why = autopilot._unavailable("reviewer-codex", load_repo_config(str(repo)), str(repo))
    assert why == "codex isn't signed in, skipped reviewer-codex"
    agy_profile(repo, monkeypatch, tmp_path)
    why = autopilot._unavailable("developer-antigravity", load_repo_config(str(repo)), str(repo))
    assert why == "agy isn't signed in, skipped developer-antigravity"


def test_chat_preflight_and_doctor_say_how_to_sign_in(monkeypatch):
    fake_probe(monkeypatch, {"auth": CLAUDE_OUT, "models": AGY_OUT})
    monkeypatch.setattr(providers, "claude_binary", lambda: "/bin/sh")
    monkeypatch.setenv("BRINDLE_AGY_BIN", "/bin/sh")
    assert any("claude auth login" in p for p in doctor.preflight("claude"))
    assert any("run `agy`" in p for p in doctor.preflight("antigravity"))
    checks = {c.name: c for c in doctor.signin_checks()}
    check = checks["Claude Code sign-in"]
    assert check.level == doctor.FAIL and "claude auth login" in check.detail
    check = checks["Google Antigravity sign-in"]
    assert check.level == doctor.WARN and "run `agy`" in check.detail


def test_signed_out_profiles_are_not_offered(db, repo, monkeypatch):
    from brindle import mcp_server

    fake_probe(monkeypatch, {"login": CODEX_OUT, "auth": CLAUDE_IN})
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.chdir(repo)
    workspaces.adopt_root(db, str(repo))
    listed = mcp_server.list_agent_profiles()
    assert "(claude)" in listed and "(codex)" not in listed
