"""A CLI that isn't signed in stops a launch with how to sign in, instead of
opening on its login screen with brindle's prompt typed into it."""

import json

import pytest

from brindle import agents, autopilot, doctor, providers, workspaces
from brindle.config import load_repo_config

AGY_PROFILE = ("---\nname: developer-antigravity\nprovider: antigravity\n"
               "permission_mode: acceptEdits\n---\nYou are a developer agent.\n")


def agy_profile(repo, monkeypatch, tmp_path, env=""):
    """A profile on agy, with a stand-in `agy` binary that counts as installed."""
    agents_dir = repo / ".brindle" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "developer-antigravity.md").write_text(AGY_PROFILE.replace("---\nYou", f"{env}---\nYou"))
    agy = tmp_path / "agy"
    agy.write_text("#!/bin/sh\n")
    agy.chmod(0o755)
    monkeypatch.setenv("BRINDLE_AGY_BIN", str(agy))


def fake_probe(monkeypatch, replies):
    """Replies keyed by the probe's subcommand; a reply may instead be a
    function of the probe's environment. ``calls`` records each argv."""
    calls = []

    def probe(argv, env=None):
        calls.append(argv)
        reply = replies.get(argv[1])
        return reply(env if env is not None else {}) if callable(reply) else reply
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


def test_antigravity_on_the_gemini_api_without_a_key(monkeypatch):
    fake_probe(monkeypatch, {"models": AGY_NO_KEY})
    why = providers.signed_out("antigravity")
    assert "GEMINI_API_KEY isn't set" in why and "modelProvider" in why


def test_a_profile_env_key_counts_as_signed_in(db, repo, monkeypatch, tmp_path):
    """The key in a profile's env.GEMINI_API_KEY line reaches the agent, so it
    counts as signed in, the probe isn't run, and the profile stays offered."""
    from brindle import mcp_server

    calls = fake_probe(monkeypatch, {"models": AGY_OUT, "auth": CLAUDE_IN})
    agy_profile(repo, monkeypatch, tmp_path, env="env.GEMINI_API_KEY: AIza-test\n")
    assert providers.signed_out("antigravity") is not None
    assert providers.signed_out("antigravity", {"GEMINI_API_KEY": "AIza-test"}) is None
    assert providers.unusable("antigravity", {"GEMINI_API_KEY": "AIza-test"}) is None
    cfg = load_repo_config(str(repo))
    assert autopilot._unavailable("developer-antigravity", cfg, str(repo)) is None
    del calls[:]
    # A launch checks with the profile's env; stop it right after the check
    # (nothing can really launch a stand-in agy).
    seen = []
    real = providers.signed_out
    monkeypatch.setattr(agents, "signed_out", lambda p, env=None: seen.append(env) or real(p, env))

    class Stop(Exception):
        pass

    def stop(*a):
        raise Stop
    monkeypatch.setattr(agents, "_add_dirs_warning", stop)
    ws = workspaces.adopt_root(db, str(repo))
    with pytest.raises(Stop):
        agents.spawn(db, ws, "developer-antigravity", prompt="write the readme")
    assert seen == [{"GEMINI_API_KEY": "AIza-test"}] and calls == []
    monkeypatch.chdir(repo)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    assert "developer-antigravity (antigravity)" in mcp_server.list_agent_profiles()


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


