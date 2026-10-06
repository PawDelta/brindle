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


def lines():
    return {c.name: c for c in doctor.signin_checks()}


def test_doctor_lists_each_installed_cli(installed):
    got = lines()
    assert set(got) == {"Claude Code sign-in", "Codex sign-in", "Google Antigravity sign-in"}
    for c in got.values():
        assert c.level == doctor.OK
        assert c.detail == "its own login (no sign-in problem found); quota: no limit in effect"


def test_doctor_skips_clis_not_installed(installed, monkeypatch):
    monkeypatch.setattr(providers, "codex_binary", lambda: "/nonexistent/codex")
    monkeypatch.setattr(antigravity, "binary", lambda: "/nonexistent/agy")
    assert set(lines()) == {"Claude Code sign-in"}


def test_doctor_names_the_env_key_never_its_value(installed, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    c = lines()["Codex sign-in"]
    assert c.level == doctor.OK
    assert "environment key OPENAI_API_KEY" in c.detail
    assert "sk-secret-value" not in doctor.render(doctor.signin_checks())


def test_doctor_reads_env_keys_from_providers_generically(installed, monkeypatch):
    # A provider added to _ENV_AUTH later shows its key without doctor changes.
    monkeypatch.setitem(providers._ENV_AUTH, "antigravity", ("SOME_AGY_KEY",))
    monkeypatch.setenv("SOME_AGY_KEY", "x")
    assert "environment key SOME_AGY_KEY" in lines()["Google Antigravity sign-in"].detail


def test_doctor_shows_signed_out(installed, monkeypatch):
    replies = {"auth": (1, json.dumps({"loggedIn": False})), "login": (1, "Not logged in\n")}
    monkeypatch.setattr(providers, "_auth_probe", lambda argv: replies.get(argv[1]))
    got = lines()
    assert got["Claude Code sign-in"].level == doctor.FAIL
    assert got["Claude Code sign-in"].detail.startswith("signed out: ")
    assert "claude auth login" in got["Claude Code sign-in"].detail
    assert got["Codex sign-in"].level == doctor.WARN
    assert "codex login" in got["Codex sign-in"].detail


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
