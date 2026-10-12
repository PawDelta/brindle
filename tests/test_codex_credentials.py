"""Which credential Codex really uses (0.162.0, signed in with ChatGPT):
interactive codex ignores OPENAI_API_KEY and CODEX_API_KEY (only
`codex login --with-api-key` switches it); headless `codex exec` uses
CODEX_API_KEY over the login and ignores OPENAI_API_KEY."""

import json
import os
import stat

import pytest
from typer.testing import CliRunner

from brindle import antigravity, doctor, keystore, pane_auth, providers
from brindle.cli import app
from brindle.profiles import load_profile

runner = CliRunner()
KEY = "sk-test-codex-9999"
TOKEN = "tok-secret-chatgpt-refresh-7777"


@pytest.fixture
def codex(monkeypatch, tmp_path, brindle_home):
    monkeypatch.setattr(providers, "claude_binary", lambda: "/bin/sh")
    monkeypatch.setattr(providers, "codex_binary", lambda: "/bin/sh")
    monkeypatch.setattr(antigravity, "binary", lambda: "/bin/sh")
    for keys in providers._ENV_AUTH.values():
        for k in keys:
            monkeypatch.delenv(k, raising=False)
    path = tmp_path / "auth.json"
    monkeypatch.setenv("BRINDLE_CODEX_AUTH", str(path))
    return path


def chatgpt(path):
    path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": TOKEN}}))


def codex_check():
    return {c.name: c for c in doctor.credential_checks()}["codex"]


def test_tokens_in_auth_json_are_never_read_beyond_auth_mode(codex, monkeypatch):
    chatgpt(codex)
    seen = []
    real = json.load

    def spy(f):
        data = real(f)
        seen.append(data)
        return data

    monkeypatch.setattr(providers.json, "load", spy)
    assert providers.codex_chatgpt_login()
    # the helper returns a bool and holds no reference to the parsed tokens
    assert len(seen) == 1 and providers.codex_chatgpt_login() is True
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    text = doctor.render(doctor.credential_checks())
    assert TOKEN not in text and KEY not in text


def test_doctor_warns_openai_key_ignored_under_chatgpt(codex, monkeypatch):
    chatgpt(codex)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    c = codex_check()
    assert c.level == doctor.WARN
    assert "interactive Codex agents use your ChatGPT plan whatever key is set" in c.detail
    assert "codex login --with-api-key" in c.detail and KEY not in c.detail
    assert "codex exec" not in c.detail and "use CODEX_API_KEY" not in c.detail


def test_doctor_codex_key_points_interactive_at_login_and_exec_bills_the_key(codex, monkeypatch):
    chatgpt(codex)
    monkeypatch.setenv("CODEX_API_KEY", KEY)
    c = codex_check()
    assert c.level == doctor.WARN
    assert "`codex exec` runs (and Brindle-CI) bill CODEX_API_KEY instead" in c.detail
    assert KEY not in c.detail


def test_doctor_warns_for_a_stored_codex_key_too(codex):
    chatgpt(codex)
    keystore.set_key("CODEX_API_KEY", KEY)
    c = codex_check()
    assert c.level == doctor.WARN and "key store" in c.detail
    assert "codex login --with-api-key" in c.detail and "Brindle-CI" in c.detail


def test_keys_set_codex_key_notes_interactive_and_exec_under_chatgpt(codex):
    chatgpt(codex)
    r = runner.invoke(app, ["keys", "set", "CODEX_API_KEY"], input=KEY + "\n")
    assert r.exit_code == 0 and KEY not in r.output
    assert "`codex exec` runs (and Brindle-CI) bill CODEX_API_KEY instead" in r.output


def codex_profile(repo, auth):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cx.md").write_text(f"---\nname: cx\nprovider: codex\nauth: {auth}\n---\nWork.\n")
    return load_profile("cx", str(repo))


def test_interactive_api_key_refused_under_chatgpt_even_with_codex_key(codex, repo, monkeypatch):
    chatgpt(codex)
    p = codex_profile(repo, "api_key")
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        monkeypatch.setenv(name, KEY)
    msg = pane_auth.key_problem(p, "codex", set(), False)
    assert msg and "auth: api_key" in msg and KEY not in msg
    assert "codex login --with-api-key" in msg and "auth: subscription" in msg
    keystore.set_key("CODEX_API_KEY", KEY)
    assert pane_auth.key_problem(p, "codex", set(), False) == msg


def test_exec_api_key_accepts_codex_key_and_refuses_openai_key_under_chatgpt(codex, repo, monkeypatch):
    chatgpt(codex)
    p = codex_profile(repo, "api_key")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    msg = pane_auth.key_problem(p, "codex", set(), False, exec_mode=True)
    assert msg and "auth: api_key" in msg and "`codex exec` ignores OPENAI_API_KEY" in msg and KEY not in msg
    monkeypatch.setenv("CODEX_API_KEY", KEY)
    assert pane_auth.key_problem(p, "codex", set(), False, exec_mode=True) is None


def test_exec_api_key_accepts_a_stored_codex_key_under_chatgpt(codex, repo):
    chatgpt(codex)
    p = codex_profile(repo, "api_key")
    assert pane_auth.key_problem(p, "codex", set(), False, exec_mode=True)
    keystore.set_key("CODEX_API_KEY", KEY)
    assert pane_auth.key_problem(p, "codex", set(), False, exec_mode=True) is None


def test_set_agy_gemini_failed_write_leaves_the_original(tmp_path, monkeypatch):
    path = tmp_path / "agy" / "settings.json"
    path.parent.mkdir()
    path.write_text('{"theme": "dark"}\n')
    os.chmod(path, 0o640)
    monkeypatch.setenv("BRINDLE_AGY_SETTINGS", str(path))

    def boom(*a, **k):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(providers.json, "dump", boom)
        with pytest.raises(OSError):
            providers.set_agy_gemini()
    assert path.read_text() == '{"theme": "dark"}\n'
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]
    providers.set_agy_gemini()
    assert json.loads(path.read_text()) == {"theme": "dark", "modelProvider": "gemini"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
