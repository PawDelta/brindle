"""`brindle doctor` shows, per installed provider CLI, how it's signed in
(its own login, an environment key, or signed out) and whether a quota limit
is in effect."""

import json
import time

import pytest

from brindle import antigravity, doctor, providers, quota


@pytest.fixture
def installed(monkeypatch, tmp_path):
    """claude, codex and agy all installed; no environment keys set, and no
    Codex rollouts to read quota from."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(providers, "claude_binary", lambda: "/bin/sh")
    monkeypatch.setattr(providers, "codex_binary", lambda: "/bin/sh")
    monkeypatch.setattr(antigravity, "binary", lambda: "/bin/sh")
    for keys in providers._ENV_AUTH.values():
        for k in keys:
            monkeypatch.delenv(k, raising=False)


def lines(repo_root=None):
    return {c.name: c for c in doctor.signin_checks(repo_root)}


SIGNED_IN = {"auth": (0, json.dumps({"loggedIn": True})), "login": (0, "Logged in using ChatGPT\n"),
             "models": (0, "Fetching available models...\ngemini-3.1-pro-high\tGemini 3.1 Pro (High)\n")}


def test_doctor_names_profile_env_keys(installed, repo, monkeypatch):
    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: SIGNED_IN.get(argv[1]))
    agents = repo / ".brindle" / "agents"
    agents.mkdir(parents=True)
    (agents / "keyed.md").write_text(
        "---\nname: keyed\nprovider: codex\nenv.CODEX_API_KEY: sk-profile-secret\n---\nWork.\n")
    (agents / "cleared.md").write_text(
        "---\nname: cleared\nprovider: claude\nenv.ANTHROPIC_API_KEY:\n---\nWork.\n")
    got = lines(str(repo))
    assert got["Codex sign-in"].detail.startswith(
        "its own login; profile keyed: environment key CODEX_API_KEY;")
    assert "profile" not in got["Claude Code sign-in"].detail   # an empty value clears the key
    assert "sk-profile-secret" not in doctor.render(doctor.signin_checks(str(repo)))


def test_wsl_ignores_a_windows_shim_and_doctor_asks_for_a_linux_install(tmp_path, monkeypatch):
    shim = tmp_path / "mnt" / "c" / "bin"
    shim.mkdir(parents=True)
    exe = shim / "codex"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(providers, "CODEX_BUNDLED", str(tmp_path / "none"))
    monkeypatch.delenv("BRINDLE_CODEX_BIN", raising=False)
    monkeypatch.setenv("PATH", str(shim))
    monkeypatch.setattr("shutil.which", lambda name, **kw: f"/mnt/c/bin/{name}")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert providers.codex_binary() == "codex"
    c = doctor._tool("codex", False, "why", install="npm i")
    assert "Windows" in c.detail and "Linux" in c.detail
    monkeypatch.delenv("WSL_DISTRO_NAME")
    monkeypatch.setattr(providers, "is_wsl", lambda: False)
    assert providers.codex_binary() == "/mnt/c/bin/codex"


def test_doctor_names_the_env_key_never_its_value(installed, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    c = lines()["Codex sign-in"]
    assert c.level == doctor.OK
    assert "environment key OPENAI_API_KEY" in c.detail
    assert "sk-secret-value" not in doctor.render(doctor.signin_checks())


def test_doctor_shows_signed_out(installed, monkeypatch):
    replies = {"auth": (1, json.dumps({"loggedIn": False})), "login": (1, "Not logged in\n"),
               "models": (1, "Error: Please sign in to view available models. Launch the CLI "
                             "without arguments to sign in.\n")}
    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: replies.get(argv[1]))
    got = lines()
    assert got["Claude Code sign-in"].level == doctor.FAIL
    assert got["Claude Code sign-in"].detail.startswith("signed out: ")
    assert "claude auth login" in got["Claude Code sign-in"].detail
    assert got["Codex sign-in"].level == doctor.WARN
    assert "codex login" in got["Codex sign-in"].detail
    assert got["Google Antigravity sign-in"].level == doctor.WARN
    assert "run `agy`" in got["Google Antigravity sign-in"].detail


def test_doctor_shows_a_quota_limit_in_effect(installed):
    quota.record_limit("antigravity", now=time.time())
    c = lines()["Google Antigravity sign-in"]
    assert c.level == doctor.WARN
    assert "quota: Antigravity limit reached, available again" in c.detail


def test_doctor_shows_quota_usage_and_not_twice(installed):
    quota.record("codex", [quota.Window(42, time.time() + 3600, 10080)])
    c = lines()["Codex sign-in"]
    assert c.level == doctor.OK
    assert "quota: Codex at 42% of its weekly limit" in c.detail
    # An installed CLI's quota is on its sign-in line, not a second one.
    assert not any(q.name == "codex quota" for q in doctor.quota_checks(None))


def test_doctor_keeps_quota_line_for_cli_not_installed(installed, monkeypatch):
    monkeypatch.setattr(providers, "codex_binary", lambda: "/nonexistent/codex")
    quota.record("codex", [quota.Window(42, time.time() + 3600, 10080)])
    assert any(q.name == "codex quota" for q in doctor.quota_checks(None))


# -- which credential wins -----------------------------------------------------------------------

FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKE-0123456789abcdefXYZ"


def credentials():
    return {c.name: c for c in doctor.credential_checks()}


def test_doctor_names_an_exported_key_and_warns_next_to_a_subscription_login(installed, monkeypatch):
    monkeypatch.setattr(providers, "_auth_probe", lambda argv, env=None: SIGNED_IN.get(argv[1]))
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    c = credentials()["claude"]
    assert c.level == doctor.WARN
    assert c.detail.startswith("API key (ANTHROPIC_API_KEY, from environment)")
    assert "bills the key" in c.detail
    assert FAKE_KEY not in doctor.render(doctor.credential_checks())


def test_doctor_names_a_stored_key_by_where_it_is_kept(installed, monkeypatch):
    from brindle import keystore

    keystore.set_key("ANTHROPIC_API_KEY", FAKE_KEY)
    assert credentials()["claude"].detail == "API key (ANTHROPIC_API_KEY, from key file)"
    assert providers.backend_label("keychain") == "keychain"
    assert providers.backend_label("secret-service") == "secret service"


def test_doctor_says_org_managed(installed, tmp_path, monkeypatch):
    path = tmp_path / "managed-settings.json"
    path.write_text(json.dumps({"apiKeyHelper": "/opt/bin/get-key"}))
    monkeypatch.setenv("BRINDLE_CLAUDE_MANAGED_SETTINGS", str(path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)   # the org's settings win over it
    c = credentials()["claude"]
    assert (c.level, c.detail) == (doctor.OK, "org-managed (apiKeyHelper)")
