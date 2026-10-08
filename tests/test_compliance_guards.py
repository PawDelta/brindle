"""Compliance guards: no Claude subscription credentials stored or routed by
third parties, and CI on API keys only. All key values here are fake."""

from __future__ import annotations

import json

import pytest

from brindle import profiles
from brindle.ci_adapters import ClaudeAdapter, CodexAdapter, usable
from brindle.native.runner import endpoint_for
from brindle.pro import auth, org_profiles
from brindle.profiles import Profile, ProfileError, load_profile


@pytest.fixture
def bins(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    for name in ("claude", "codex"):
        p = d / name
        p.write_text("#!/bin/sh\necho fake\n")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", str(d))
    monkeypatch.delenv("BRINDLE_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("BRINDLE_CODEX_BIN", raising=False)
    return d


def _codex_env(tmp_path, bins, auth_json):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps(auth_json))
    return {"CODEX_HOME": str(home), "PATH": str(bins)}


# -- 1. Brindle-CI -------------------------------------------------------------------------------


@pytest.mark.parametrize("org", [False, True])
def test_claude_oauth_token_refused_api_key_passes(bins, org):
    a = ClaudeAdapter()
    ok, why = usable(a, {"CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth", "PATH": str(bins)}, org)
    assert not ok and "API key" in why
    assert usable(a, {"ANTHROPIC_API_KEY": "sk-ant-fake", "PATH": str(bins)}, org) == (True, "")


@pytest.mark.parametrize("org", [False, True])
def test_codex_chatgpt_login_refused_api_key_passes(bins, tmp_path, org):
    a = CodexAdapter()
    ok, why = usable(a, _codex_env(tmp_path, bins, {"tokens": {"access_token": "fake"}}), org)
    assert not ok and "API key" in why
    key_env = {"PATH": str(bins), "CODEX_HOME": str(tmp_path / "none"), "OPENAI_API_KEY": "sk-fake"}
    assert usable(a, key_env, org) == (True, "")


@pytest.mark.parametrize("env", [
    {"CLAUDE_CODE_USE_BEDROCK": "1"}, {"CLAUDE_CODE_USE_VERTEX": "1"}, {"CLAUDE_CODE_USE_FOUNDRY": "1"},
    {"ANTHROPIC_AUTH_TOKEN": "fake-federated"},
])
def test_cloud_and_federation_stay_accepted(bins, env):
    assert usable(ClaudeAdapter(), {**env, "PATH": str(bins)}, True) == (True, "")


# -- 2. native profiles ----------------------------------------------------------------------------


def _native(**kw):
    return Profile(name="n", description="d", prompt="p", provider="native",
                   base_url="http://localhost:1/v1", model="m", **kw)


def test_native_profile_naming_the_oauth_token_is_refused_at_runner(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-oauth")
    with pytest.raises(ValueError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        endpoint_for(_native(api_key_env="CLAUDE_CODE_OAUTH_TOKEN"))
    with pytest.raises(ValueError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        endpoint_for(_native(env={"CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth"}))


def test_native_api_key_env_still_works(monkeypatch):
    monkeypatch.setenv("MY_FAKE_KEY", "sk-fake")
    assert endpoint_for(_native(api_key_env="MY_FAKE_KEY")).api_key == "sk-fake"


@pytest.mark.parametrize("line", ["api_key_env: CLAUDE_CODE_OAUTH_TOKEN", "env.CLAUDE_CODE_OAUTH_TOKEN: fake"])
def test_profile_loading_refuses_the_oauth_token(tmp_path, line):
    d = tmp_path / ".brindle" / "agents"
    d.mkdir(parents=True)
    (d / "bad.md").write_text(f"---\nprovider: native\nbase_url: http://x/v1\nmodel: m\n{line}\n---\nhi\n")
    with pytest.raises(ProfileError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        load_profile("bad", str(tmp_path))


# -- 3. org profile library --------------------------------------------------------------------------


def _upload(tmp_path, body):
    f = tmp_path / "p.md"
    f.write_text(f"---\nname: p\n{body}\n---\nText.\n")
    return org_profiles.read_item(f, "profile")


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"])
def test_org_upload_with_a_literal_key_is_refused(tmp_path, name):
    with pytest.raises(auth.AuthError, match=name):
        _upload(tmp_path, f"env.{name}: sk-fake-not-a-real-key")


def test_org_upload_naming_the_variable_is_accepted(tmp_path):
    item = _upload(tmp_path, "api_key_env: ANTHROPIC_API_KEY")
    assert item["name"] == "p"


def test_org_push_refuses_a_literal_key_before_any_request(tmp_path):
    class NoNetwork:
        def request(self, *a, **k):
            raise AssertionError("must not be called")

    bad = {"kind": "profile", "name": "p", "text": "---\nenv.ANTHROPIC_API_KEY: sk-fake\n---\nx", "pinned": False}
    with pytest.raises(auth.AuthError):
        org_profiles.push(NoNetwork(), None, "acme", publish=[bad])
