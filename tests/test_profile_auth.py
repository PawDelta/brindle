"""A profile's ``auth: auto | subscription | api_key``. Fake keys only."""
import itertools
import time

import pytest
from typer.testing import CliRunner

from brindle import agents, keystore, providers, tmux, workspaces
from brindle.cli import app
from brindle.db import Agent
from brindle.profiles import ProfileError, load_profile

_ids = itertools.count(1)
STORED = "sk-ant-test-stored-1111"
EXPORTED = "sk-ant-test-exported-2222"
runner = CliRunner()


def write_profile(repo, name, auth=None, provider="claude", extra=""):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    line = f"auth: {auth}\n" if auth else ""
    (d / f"{name}.md").write_text(f"---\nname: {name}\nprovider: {provider}\n{line}{extra}---\nWork.\n")


def pane_script(db, repo, monkeypatch, brindle_home, profile, m=None, provider="claude"):
    """Open a window for an agent on ``profile`` with tmux faked; returns the
    private launch script's text (its exports and ``env -u`` list)."""
    class Done:
        stdout, stderr, returncode = "%1\n", "", 0

    monkeypatch.setattr(tmux, "_tmux", lambda *a, **k: Done())
    monkeypatch.setattr(tmux, "inherited_names", lambda s: set())
    monkeypatch.setattr(tmux, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "apply_theme", lambda *a, **k: None)
    for old in (brindle_home / "launch").glob("*") if (brindle_home / "launch").exists() else ():
        old.unlink()
    n = next(_ids)
    ws = workspaces.create(db, str(repo), f"auth{n}").workspace
    a = Agent(f"c{n}", ws.id, profile, provider, None, "assign", "starting", "", None, time.time())
    db.add_agent(a)
    agents._open_window(db, a, ws, f"c{n}", ["true"], watch_pane=False, m=m)
    [script] = (brindle_home / "launch").iterdir()
    return script.read_text()


@pytest.fixture
def keyed(monkeypatch):
    """A key stored with `brindle keys` and another exported."""
    keystore.set_key("ANTHROPIC_API_KEY", STORED)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", EXPORTED)


def test_auth_defaults_to_auto_and_parses(repo):
    write_profile(repo, "plain")
    write_profile(repo, "sub", "subscription")
    assert load_profile("plain", str(repo)).auth == "auto"
    assert load_profile("sub", str(repo)).auth == "subscription"


def test_an_unknown_auth_is_an_error(repo):
    write_profile(repo, "odd", "oauth")
    with pytest.raises(ProfileError, match="auth"):
        load_profile("odd", str(repo))


def test_auto_passes_a_stored_key_as_before(db, repo, monkeypatch, brindle_home, keyed):
    write_profile(repo, "plain")
    script = pane_script(db, repo, monkeypatch, brindle_home, "plain")
    assert f"export ANTHROPIC_API_KEY={STORED}" in script


def test_subscription_puts_no_stored_key_in_the_pane_and_strips_exported_ones(
        db, repo, monkeypatch, brindle_home, keyed):
    write_profile(repo, "sub", "subscription")
    script = pane_script(db, repo, monkeypatch, brindle_home, "sub")
    assert STORED not in script and EXPORTED not in script
    assert "export ANTHROPIC_API_KEY" not in script
    assert "-u ANTHROPIC_API_KEY" in script and "-u ANTHROPIC_AUTH_TOKEN" in script
    assert "-u CLAUDE_CODE_OAUTH_TOKEN" not in script    # the login's own token stays


def test_subscription_strips_a_key_the_profile_sets_itself(db, repo, monkeypatch, brindle_home):
    write_profile(repo, "sub", "subscription", extra="env.ANTHROPIC_API_KEY: sk-ant-test-profile-3333\n")
    script = pane_script(db, repo, monkeypatch, brindle_home, "sub")
    assert "-u ANTHROPIC_API_KEY" in script


def test_subscription_strips_codex_keys_too(db, repo, monkeypatch, brindle_home):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-4444")
    write_profile(repo, "csub", "subscription", provider="codex")
    script = pane_script(db, repo, monkeypatch, brindle_home, "csub", provider="codex")
    assert "-u OPENAI_API_KEY" in script


def _launch(db, repo, profile, m=None):
    ws = workspaces.create(db, str(repo), "launch").workspace
    a = Agent("c2", ws.id, profile, "claude", None, "assign", "starting", "", None, time.time())
    db.add_agent(a)
    agents._launch(db, a, ws, prompt=None, resume=None, watch_pane=False)


def test_api_key_refuses_to_start_without_a_key(db, repo, monkeypatch):
    write_profile(repo, "keyed", "api_key")
    with pytest.raises(agents.AgentError, match=r"auth: api_key.*ANTHROPIC_API_KEY"):
        _launch(db, repo, "keyed")


def test_api_key_accepts_an_exported_a_stored_or_a_profile_key(repo, monkeypatch):
    from brindle import pane_auth

    write_profile(repo, "keyed", "api_key")
    p = load_profile("keyed", str(repo))
    assert pane_auth.key_problem(p, "claude", set(), False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", EXPORTED)
    assert pane_auth.key_problem(p, "claude", set(), False) is None
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    keystore.set_key("ANTHROPIC_API_KEY", STORED)
    assert pane_auth.key_problem(p, "claude", set(), False) is None


def test_api_key_is_refused_when_the_org_denies_personal_keys(repo, monkeypatch):
    from brindle import pane_auth, secrets

    monkeypatch.setenv("ANTHROPIC_API_KEY", EXPORTED)
    write_profile(repo, "keyed", "api_key")
    p = load_profile("keyed", str(repo))
    msg = pane_auth.key_problem(p, "claude", set(secrets.PERSONAL_KEYS), False)
    assert msg and "doesn't allow personal API keys" in msg and EXPORTED not in msg
    # ... unless the org supplies the key
    assert pane_auth.key_problem(p, "claude", set(secrets.PERSONAL_KEYS), True) is None


def test_keys_set_warns_when_claude_has_a_login(brindle_home, monkeypatch):
    monkeypatch.setattr(providers, "subscription_login", lambda provider="claude": True)
    monkeypatch.setattr(providers, "claude_org_managed", lambda: None)
    monkeypatch.setattr(providers, "api_key_approved", lambda k: True)
    r = runner.invoke(app, ["keys", "set", "ANTHROPIC_API_KEY"], input=STORED + "\n")
    assert r.exit_code == 0 and STORED not in r.output
    assert "instead of your subscription" in r.output and "auth: subscription" in r.output


def test_keys_set_is_quiet_without_a_login(brindle_home, monkeypatch):
    monkeypatch.setattr(providers, "subscription_login", lambda provider="claude": False)
    monkeypatch.setattr(providers, "claude_org_managed", lambda: None)
    monkeypatch.setattr(providers, "api_key_approved", lambda k: True)
    r = runner.invoke(app, ["keys", "set", "ANTHROPIC_API_KEY"], input=STORED + "\n")
    assert r.exit_code == 0 and "instead of your subscription" not in r.output
