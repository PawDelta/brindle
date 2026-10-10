"""agy reads GEMINI_API_KEY only when its settings.json has
"modelProvider": "gemini". `brindle keys set`, `brindle doctor [--fix]` and
`auth: api_key` all deal with that. Fake keys and a fake settings file only."""
import json
import os
import stat

import pytest
from typer.testing import CliRunner

from brindle import antigravity, doctor, keystore, pane_auth, providers
from brindle.cli import app
from brindle.profiles import load_profile

KEY = "gem-test-key-5555"
runner = CliRunner()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "agy" / "settings.json"
    monkeypatch.setenv("BRINDLE_AGY_SETTINGS", str(path))
    return path


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def set_key(inp):
    return runner.invoke(app, ["keys", "set", "GEMINI_API_KEY"], input=KEY + "\n" + inp)


def test_keys_set_offers_and_accepting_creates_the_file_0600(settings):
    r = set_key("y\n")
    assert r.exit_code == 0 and KEY not in r.output
    assert "agy ignores GEMINI_API_KEY" in r.output and str(settings) in r.output
    assert json.loads(settings.read_text()) == {"modelProvider": "gemini"}
    assert stat.S_IMODE(os.stat(settings).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(settings.parent).st_mode) == 0o700
    assert keystore.get_key("GEMINI_API_KEY") == KEY


def test_keys_set_accepting_keeps_every_other_setting(settings):
    write(settings, {"theme": "dark", "nested": {"a": [1, 2]}, "modelProvider": "vertex"})
    r = set_key("y\n")
    assert r.exit_code == 0
    assert json.loads(settings.read_text()) == {"theme": "dark", "nested": {"a": [1, 2]}, "modelProvider": "gemini"}


def test_keys_set_declining_never_touches_the_file(settings):
    r = set_key("n\n")
    assert r.exit_code == 0 and "left as is" in r.output
    assert not settings.exists()
    write(settings, {"theme": "dark"})
    before = settings.read_text()
    set_key("\n")  # the default is no
    assert settings.read_text() == before


def test_keys_set_eof_is_no(settings):
    r = set_key("")
    assert r.exit_code == 0 and not settings.exists()
    assert keystore.get_key("GEMINI_API_KEY") == KEY


def test_keys_set_does_not_ask_when_already_gemini(settings):
    write(settings, {"modelProvider": "gemini"})
    r = set_key("")
    assert r.exit_code == 0 and "agy ignores" not in r.output and "Set " not in r.output


def test_other_keys_do_not_trigger_the_offer(settings):
    r = runner.invoke(app, ["keys", "set", "OPENAI_API_KEY"], input="sk-test-1\n")
    assert "agy" not in r.output and not settings.exists()


@pytest.fixture
def agy_installed(monkeypatch):
    monkeypatch.setattr(antigravity, "binary", lambda: "/bin/sh")
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["antigravity"])
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def agy_line():
    return {c.name: c for c in doctor.credential_checks()}["antigravity"]


def test_doctor_warns_when_a_key_exists_but_agy_ignores_it(agy_installed, settings, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    c = agy_line()
    assert c.level == doctor.WARN and KEY not in c.detail
    assert f'GEMINI_API_KEY is set but agy ignores it: set "modelProvider": "gemini" in {settings}' in c.detail
    assert "brindle doctor --fix" in c.detail and "brindle keys set GEMINI_API_KEY" in c.detail


def test_doctor_warns_for_a_stored_key_too(agy_installed, settings):
    keystore.set_key("GEMINI_API_KEY", KEY)
    assert agy_line().level == doctor.WARN


def test_doctor_is_fine_with_the_setting_or_without_a_key(agy_installed, settings, monkeypatch):
    assert agy_line().level == doctor.OK
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    write(settings, {"modelProvider": "gemini"})
    c = agy_line()
    assert c.level == doctor.OK and "GEMINI_API_KEY" in c.detail and KEY not in c.detail


def test_doctor_fix_sets_it_on_yes_and_leaves_it_on_no(agy_installed, settings, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    write(settings, {"theme": "dark"})
    out = doctor.fix(lambda p: False)
    assert any("left as is" in line for line in out)
    assert json.loads(settings.read_text()) == {"theme": "dark"}
    out = doctor.fix(lambda p: True)
    assert json.loads(settings.read_text()) == {"theme": "dark", "modelProvider": "gemini"}
    assert all(KEY not in line for line in out)
    assert agy_line().level == doctor.OK


def test_doctor_fix_has_nothing_to_do_without_a_key(agy_installed, settings):
    assert doctor.fix(lambda p: pytest.fail("asked")) == ["nothing to fix"]
    assert not settings.exists()


def test_doctor_fix_via_the_cli(agy_installed, settings, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    r = runner.invoke(app, ["doctor", "--fix"], input="y\n")
    assert json.loads(settings.read_text()) == {"modelProvider": "gemini"}
    assert KEY not in r.output


def agy_profile(repo):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "gem.md").write_text("---\nname: gem\nprovider: antigravity\nauth: api_key\n---\nWork.\n")
    return load_profile("gem", str(repo))


def test_api_key_refuses_when_agy_would_ignore_the_key(repo, settings, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    p = agy_profile(repo)
    msg = pane_auth.key_problem(p, "antigravity", set(), False)
    assert msg and "auth: api_key" in msg and KEY not in msg
    assert f'GEMINI_API_KEY is set but agy ignores it: set "modelProvider": "gemini" in {settings}' in msg
    write(settings, {"modelProvider": "gemini"})
    assert pane_auth.key_problem(p, "antigravity", set(), False) is None


def test_api_key_still_asks_for_a_key_first(repo, settings, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    msg = pane_auth.key_problem(agy_profile(repo), "antigravity", set(), False)
    assert msg and "no API key is available" in msg


def test_a_bad_settings_file_counts_as_not_gemini(settings):
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json")
    assert not providers.agy_uses_gemini_key()
