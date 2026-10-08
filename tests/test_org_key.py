"""Enterprise org-owned keys: ``key_env`` / ``key_helper`` in the managed-models
policy reach the pane past ``deny_personal_keys``; a personal key doesn't.
Fake keys only."""
import logging
import sys

import pytest

from brindle import agents, providers, secrets, tmux
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


@pytest.mark.parametrize("name", list(secrets.PERSONAL_KEYS))
def test_the_policy_rejects_a_key_env_naming_a_personal_key(name):
    with pytest.raises(PolicyUnavailable, match="personal"):
        parse_policy(ORG, {"version": 1, "policy": {**POLICY, "provider_config": anthropic(key_env=name)}})


def test_the_policy_rejects_a_key_env_naming_a_brindle_secret():
    with pytest.raises(PolicyUnavailable):
        parse_policy(ORG, {"version": 1, "policy": {**POLICY, "provider_config": anthropic(key_env="GITHUB_TOKEN")}})


@pytest.mark.parametrize("over", [
    {"key_env": "has space"}, {"key_env": 3}, {"key_helper": "vault read"}, {"key_helper": []},
    {"key_helper": [""]}, {"key_helper": ["a", 3]}, {"key_env": "ACME_KEY", "key_helper": ["a"]},
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


def test_key_env_maps_to_anthropic_api_key_for_claude_and_openai_api_key_for_codex(monkeypatch):
    monkeypatch.setenv("ACME_KEY", ORG_KEY)
    a = managed(ProviderConfig("anthropic", key_env="ACME_KEY"))
    assert managed_models.org_key_env(a, "claude") == {"ANTHROPIC_API_KEY": ORG_KEY}
    assert managed_models.org_key_env(a, "codex") == {}
    c = managed(ProviderConfig("openai-compatible", endpoint="https://x/v1", key_env="ACME_KEY"))
    assert managed_models.org_key_env(c, "codex") == {"OPENAI_API_KEY": ORG_KEY}
    assert managed_models.org_key_env(c, "claude") == {}


def test_a_missing_org_variable_fails_closed(monkeypatch):
    monkeypatch.delenv("ACME_KEY", raising=False)
    with pytest.raises(managed_models.ManagedUnavailable, match="ACME_KEY"):
        managed_models.org_key_env(managed(ProviderConfig("anthropic", key_env="ACME_KEY")), "claude")


@pytest.mark.parametrize("code", ["import sys; sys.exit(3)", "print('')",
                                  "import time; time.sleep(5)"])
def test_a_failing_helper_fails_closed_without_echoing_its_output(monkeypatch, code):
    monkeypatch.setattr(managed_models, "KEY_HELPER_TIMEOUT", 0.5)
    cfg = ProviderConfig("anthropic", key_helper=(sys.executable, "-c",
                                                  f"import sys; print({ORG_KEY!r}, file=sys.stderr)\n{code}"))
    with pytest.raises(managed_models.ManagedUnavailable) as e:
        managed_models.org_key_env(managed(cfg), "claude")
    assert ORG_KEY not in str(e.value)


def test_the_helper_runs_without_a_shell(monkeypatch, tmp_path):
    marker = tmp_path / "pwned"
    cfg = ProviderConfig("anthropic", key_helper=(sys.executable, "-c", "print('k')", f"; touch {marker}"))
    assert managed_models.org_key_env(managed(cfg), "claude") == {"ANTHROPIC_API_KEY": "k"}
    assert not marker.exists()


# -- the pane -----------------------------------------------------------------------------------


def test_org_key_exempt_needs_the_org_value():
    with secrets.org_key({"ANTHROPIC_API_KEY": ORG_KEY}):
        assert secrets.org_key_exempt({"ANTHROPIC_API_KEY": ORG_KEY}) == {"ANTHROPIC_API_KEY": ORG_KEY}
        assert secrets.org_key_exempt({"ANTHROPIC_API_KEY": PERSONAL}) == {}
        assert secrets.pane_deny(["ANTHROPIC_API_KEY"], {"ANTHROPIC_API_KEY": PERSONAL}) == ["ANTHROPIC_API_KEY"]
        assert secrets.pane_deny(["ANTHROPIC_API_KEY"], {"ANTHROPIC_API_KEY": ORG_KEY}) == []
    assert secrets.pane_deny(["ANTHROPIC_API_KEY"], {"ANTHROPIC_API_KEY": ORG_KEY}) == ["ANTHROPIC_API_KEY"]


def test_new_window_strips_a_personal_key_and_keeps_the_org_key(tmp_path, brindle_home, monkeypatch):
    class Done:
        stdout, stderr, returncode = "%1\n", "", 0

    monkeypatch.setattr(tmux, "_tmux", lambda *a, **k: Done())
    monkeypatch.setattr(tmux, "inherited_names", lambda s: {"ANTHROPIC_API_KEY"})
    tmux.new_window("s", "w", str(tmp_path), ["true"], {"ANTHROPIC_API_KEY": PERSONAL},
                    deny=secrets.PERSONAL_KEYS)
    [script] = (brindle_home / "launch").iterdir()
    assert "-u ANTHROPIC_API_KEY" in script.read_text()
    script.unlink()
    with secrets.org_key({"ANTHROPIC_API_KEY": ORG_KEY}):
        tmux.new_window("s", "w", str(tmp_path), ["true"], {"ANTHROPIC_API_KEY": ORG_KEY},
                        deny=secrets.PERSONAL_KEYS)
    [script] = (brindle_home / "launch").iterdir()
    text = script.read_text()
    assert "-u ANTHROPIC_API_KEY" not in text and f"export ANTHROPIC_API_KEY={ORG_KEY}" in text
    assert "-u ANTHROPIC_AUTH_TOKEN" in text and "-u OPENAI_API_KEY" in text


def test_a_key_env_org_key_reaches_the_pane_past_deny_personal_keys(db, repo, monkeypatch, brindle_home):
    monkeypatch.setenv("ACME_KEY", ORG_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", PERSONAL)     # exported by the person
    monkeypatch.setattr(tmux, "inherited_names", lambda s: {"ANTHROPIC_API_KEY"})
    m = managed(ProviderConfig("anthropic", key_env="ACME_KEY"))
    script = pane_script(db, repo, monkeypatch, brindle_home, "developer", m=m)
    assert f"export ANTHROPIC_API_KEY={ORG_KEY}" in script and PERSONAL not in script
    assert "-u ANTHROPIC_API_KEY" not in script
    assert "-u ANTHROPIC_AUTH_TOKEN" in script and "-u CLAUDE_CODE_OAUTH_TOKEN" in script


def test_a_key_helper_org_key_reaches_the_pane_past_deny_personal_keys(db, repo, monkeypatch, brindle_home):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PERSONAL)
    script = pane_script(db, repo, monkeypatch, brindle_home, "developer", m=managed(helper_cfg()))
    assert f"export ANTHROPIC_API_KEY={ORG_KEY}" in script and PERSONAL not in script
    assert "-u ANTHROPIC_API_KEY" not in script


def test_without_an_org_key_the_personal_key_is_still_stripped(db, repo, monkeypatch, brindle_home):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PERSONAL)
    script = pane_script(db, repo, monkeypatch, brindle_home, "developer",
                         m=managed(ProviderConfig("anthropic"), deny=True))
    assert PERSONAL not in script and "-u ANTHROPIC_API_KEY" in script


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


def test_claude_code_managed_settings_deny_personal_claude_credentials(db, repo, monkeypatch, brindle_home):
    monkeypatch.setattr(providers, "claude_org_managed", lambda: "apiKeyHelper")
    monkeypatch.setenv("ANTHROPIC_API_KEY", PERSONAL)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    script = pane_script(db, repo, monkeypatch, brindle_home, "developer")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert f"-u {name}" in script
    assert "-u AWS_REGION" not in script       # cloud sign-in stays


def test_without_managed_settings_nothing_is_denied(db, repo, monkeypatch, brindle_home):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PERSONAL)
    script = pane_script(db, repo, monkeypatch, brindle_home, "developer")
    assert "-u ANTHROPIC_API_KEY" not in script


def test_a_profile_with_a_personal_key_is_still_refused():
    from brindle.profiles import Profile

    p = Profile(name="dev", description="", provider="claude", prompt="",
                env={"ANTHROPIC_API_KEY": PERSONAL})
    why = managed_models.refusal(managed(ProviderConfig("anthropic", key_env="ACME_KEY")), p)
    assert why and "personal API key" in why and PERSONAL not in why
