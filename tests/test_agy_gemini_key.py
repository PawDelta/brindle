"""agy reads GEMINI_API_KEY only when its settings.json has
"modelProvider": "gemini". `brindle keys set`, `brindle doctor [--fix]` and
`auth: api_key` all deal with that. Fake keys and a fake settings file only."""
import json

import pytest
from typer.testing import CliRunner

from brindle import antigravity, doctor, keystore, pane_auth
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


def test_keys_set_eof_is_no(settings):
    r = set_key("")
    assert r.exit_code == 0 and not settings.exists()
    assert keystore.get_key("GEMINI_API_KEY") == KEY


def test_keys_set_does_not_ask_when_already_gemini(settings):
    write(settings, {"modelProvider": "gemini"})
    r = set_key("")
    assert r.exit_code == 0 and "agy ignores" not in r.output and "Set " not in r.output


@pytest.fixture
def agy_installed(monkeypatch):
    monkeypatch.setattr(antigravity, "binary", lambda: "/bin/sh")
    monkeypatch.setattr(doctor, "signin_providers", lambda: ["antigravity"])
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def agy_line():
    return {c.name: c for c in doctor.credential_checks()}["antigravity"]


def test_doctor_warns_for_a_stored_key_too(agy_installed, settings):
    keystore.set_key("GEMINI_API_KEY", KEY)
    assert agy_line().level == doctor.WARN


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
