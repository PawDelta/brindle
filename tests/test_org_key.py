"""Enterprise org-owned keys: ``key_env`` / ``key_helper`` in the managed-models
policy reach the pane past ``deny_personal_keys``; a personal key doesn't.
Fake keys only."""
import logging
import sys

import pytest

from brindle import agents, secrets, tmux
from brindle.pro import managed_models
from brindle.pro.team_policy import PolicyUnavailable, ProviderConfig, parse_policy

from test_pro_team import ORG, POLICY
from test_profile_auth import pane_script

ORG_KEY = "sk-ant-test-org-owned-5555"
PERSONAL = "sk-ant-test-personal-6666"


def anthropic(**kw):
    return {"provider": "anthropic", "model_ids": [], **kw}


def managed(cfg, deny=True):
    return managed_models.Managed(ORG, cfg, deny)


def helper_cfg(value=ORG_KEY):
    return ProviderConfig("anthropic", key_helper=(sys.executable, "-c", f"print({value!r})"))


# -- the policy ---------------------------------------------------------------------------------


def test_the_policy_rejects_a_key_env_naming_a_personal_key():
    with pytest.raises(PolicyUnavailable, match="personal"):
        parse_policy(ORG, {"version": 1, "policy": {**POLICY, "provider_config": anthropic(key_env="CODEX_API_KEY")}})


def test_the_policy_rejects_a_key_env_naming_a_brindle_secret():
    with pytest.raises(PolicyUnavailable):
        parse_policy(ORG, {"version": 1, "policy": {**POLICY, "provider_config": anthropic(key_env="GITHUB_TOKEN")}})


@pytest.mark.parametrize("over", [
    {"key_env": "has space"}, {"key_helper": [""]}, {"key_env": "ACME_KEY", "key_helper": ["a"]},
])
def test_malformed_key_settings_make_the_policy_unusable(over):
    with pytest.raises(PolicyUnavailable):
        parse_policy(ORG, {"version": 1, "policy": {**POLICY, "provider_config": anthropic(**over)}})


def test_a_key_is_only_for_anthropic_and_openai_compatible():
    with pytest.raises(PolicyUnavailable):
        parse_policy(ORG, {"version": 1, "policy": {**POLICY, "provider_config": {
            "provider": "bedrock", "region": "us-east-1", "key_env": "ACME_KEY"}}})


def test_the_policy_parses_an_org_variable_and_a_helper_and_caches_them():
    p = parse_policy(ORG, {"version": 1, "policy": {**POLICY, "provider_config": anthropic(key_env="ACME_KEY")}})
    assert p.provider_config.key_env == "ACME_KEY" and p.provider_config.key_helper is None
    p = parse_policy(ORG, {"version": 1, "policy": {**POLICY, "provider_config": {
        "provider": "openai-compatible", "endpoint": "https://llm.acme.internal/v1",
        "key_helper": ["vault", "read", "-field=key", "llm"]}}})
    assert p.provider_config.key_helper == ("vault", "read", "-field=key", "llm")
    again = parse_policy(ORG, p.to_json())
    assert again.provider_config == p.provider_config


# -- the key ------------------------------------------------------------------------------------


def test_a_missing_org_variable_fails_closed(monkeypatch):
    monkeypatch.delenv("ACME_KEY", raising=False)
    with pytest.raises(managed_models.ManagedUnavailable, match="ACME_KEY"):
        managed_models.org_key_env(managed(ProviderConfig("anthropic", key_env="ACME_KEY")), "claude")


def test_a_hung_helper_fails_closed_without_echoing_its_output(monkeypatch):
    monkeypatch.setattr(managed_models, "KEY_HELPER_TIMEOUT", 0.3)
    cfg = ProviderConfig("anthropic", key_helper=(
        sys.executable, "-c", f"import sys, time; print({ORG_KEY!r}, file=sys.stderr, flush=True); time.sleep(5)"))
    with pytest.raises(managed_models.ManagedUnavailable) as e:
        managed_models.org_key_env(managed(cfg), "claude")
    assert ORG_KEY not in str(e.value)


def test_the_helper_runs_without_a_shell(monkeypatch, tmp_path):
    marker = tmp_path / "pwned"
    cfg = ProviderConfig("anthropic", key_helper=(sys.executable, "-c", "print('k')", f"; touch {marker}"))
    assert managed_models.org_key_env(managed(cfg), "claude") == {"ANTHROPIC_API_KEY": "k"}
    assert not marker.exists()


# -- the pane -----------------------------------------------------------------------------------


def test_a_key_env_org_key_reaches_the_pane_past_deny_personal_keys(db, repo, monkeypatch, brindle_home):
    monkeypatch.setenv("ACME_KEY", ORG_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", PERSONAL)     # exported by the person
    monkeypatch.setattr(tmux, "inherited_names", lambda s: {"ANTHROPIC_API_KEY"})
    m = managed(ProviderConfig("anthropic", key_env="ACME_KEY"))
    script = pane_script(db, repo, monkeypatch, brindle_home, "developer", m=m)
    assert f"export ANTHROPIC_API_KEY={ORG_KEY}" in script and PERSONAL not in script
    assert "-u ANTHROPIC_API_KEY" not in script
    assert "-u ANTHROPIC_AUTH_TOKEN" in script and "-u CLAUDE_CODE_OAUTH_TOKEN" in script


def test_the_org_key_is_not_kept_after_the_launch(db, repo, monkeypatch, brindle_home):
    pane_script(db, repo, monkeypatch, brindle_home, "developer", m=managed(helper_cfg()))
    assert secrets.org_key_exempt({"ANTHROPIC_API_KEY": ORG_KEY}) == {}


def test_key_helper_output_never_appears_in_logs(db, repo, monkeypatch, brindle_home, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(managed_models, "KEY_HELPER_TIMEOUT", 5)
    pane_script(db, repo, monkeypatch, brindle_home, "developer", m=managed(helper_cfg()))
    cfg = ProviderConfig("anthropic", key_helper=(sys.executable, "-c", f"print({ORG_KEY!r}); raise SystemExit(2)"))
    with pytest.raises(agents.AgentError):
        pane_script(db, repo, monkeypatch, brindle_home, "developer", m=managed(cfg))
    out = capsys.readouterr()
    assert ORG_KEY not in caplog.text and ORG_KEY not in out.out and ORG_KEY not in out.err
