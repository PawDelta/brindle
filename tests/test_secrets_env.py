"""Agents start without brindle's secrets. A local worker's pane would
otherwise inherit the tmux server's whole environment (the Pro token, GitHub's
tokens); brindle CI scrubs the same names from the job before any agent
starts. Each provider's own sign-in still reaches it."""

import time

import pytest

from brindle import agents, secrets, tmux, workspaces
from brindle.db import Agent


# -- the shared module -----------------------------------------------------------------------


def test_a_profiles_api_key_env_naming_a_job_secret_is_not_a_credential():
    assert "BRINDLE_PRO_TOKEN" not in secrets.provider_credentials("native", "BRINDLE_PRO_TOKEN")
    assert "GH_TOKEN" not in secrets.provider_credentials("native", "GH_TOKEN")
    assert secrets.provider_credentials("native", "MY_MODEL_KEY") == {"MY_MODEL_KEY"}


# -- the federation proxy's token under deny_personal_keys --------------------------------------

PROXY = ("http://127.0.0.1:41999", "proxy-secret")


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    secrets.register_proxy(*PROXY)
    yield
    secrets.clear_proxy()


def _proxy_env(**over):
    return {"ANTHROPIC_BASE_URL": PROXY[0], "ANTHROPIC_AUTH_TOKEN": PROXY[1], **over}


def test_a_personal_token_is_still_denied_unless_it_is_the_proxys(proxy):
    # no proxy running
    secrets.clear_proxy()
    assert "ANTHROPIC_AUTH_TOKEN" in secrets.pane_deny(secrets.PERSONAL_KEYS, _proxy_env())
    secrets.register_proxy(*PROXY)
    # loopback alone is not enough: another port, or a profile pointing elsewhere
    assert "ANTHROPIC_AUTH_TOKEN" in secrets.pane_deny(
        secrets.PERSONAL_KEYS, _proxy_env(ANTHROPIC_BASE_URL="http://127.0.0.1:9"))
    assert "ANTHROPIC_AUTH_TOKEN" in secrets.pane_deny(
        secrets.PERSONAL_KEYS, _proxy_env(ANTHROPIC_BASE_URL="https://evil.example"))
    # a different token (the person's own) next to the proxy's URL
    assert "ANTHROPIC_AUTH_TOKEN" in secrets.pane_deny(
        secrets.PERSONAL_KEYS, _proxy_env(ANTHROPIC_AUTH_TOKEN="sk-ant-oat01-mine"))
    # nothing set at all
    assert "ANTHROPIC_AUTH_TOKEN" in secrets.pane_deny(secrets.PERSONAL_KEYS, {})


def test_federation_apply_registers_the_proxy_and_stop_clears_it():
    from brindle import ci_federation

    class P:
        base_url, secret = PROXY

        def stop(self):
            pass

    class R:
        def stop(self):
            pass

    fed = ci_federation.Federation(R(), P())
    env = {}
    fed.apply(env)
    try:
        assert secrets.proxy_exempt(env) == {"ANTHROPIC_BASE_URL": PROXY[0], "ANTHROPIC_AUTH_TOKEN": PROXY[1]}
    finally:
        fed.stop()
    assert secrets.proxy_exempt(env) == {}


# -- each provider's credentials get through --------------------------------------------------

def _unset_for(provider, api_key_env=None, present=()):
    keep = secrets.provider_credentials(provider, api_key_env)
    return secrets.pane_unset([*present, *keep, "GH_TOKEN", "BRINDLE_PRO_TOKEN"], keep=keep)


def test_native_profiles_keep_their_api_key_env_but_never_a_job_secret(repo):
    # A profile can't claim a job secret (a GitHub Models profile needs its own, differently named, variable).
    assert "GITHUB_TOKEN" in _unset_for("native", "GITHUB_TOKEN")
    assert "GITHUB_TOKEN" in _unset_for("native", "MY_MODEL_KEY")
    assert "MY_MODEL_KEY" not in _unset_for("native", "MY_MODEL_KEY", present=["MY_MODEL_KEY"])
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True)
    (d / "ghm.md").write_text("---\nname: ghm\nprovider: native\napi: openai\n"
                              "base_url: https://models.github.ai/inference\nmodel: m\n"
                              "api_key_env: GITHUB_TOKEN\n---\nWork.\n")
    assert "GITHUB_TOKEN" not in secrets.agent_credentials("native", "ghm", str(repo))
    assert "GITHUB_TOKEN" not in secrets.agent_credentials("claude", "developer", str(repo))
    assert secrets.agent_credentials("native", "no-such-profile", str(repo)) == frozenset()


# -- the pane ----------------------------------------------------------------------------------

def test_scrubbed_command_runs_through_env_u():
    assert tmux.scrubbed(["claude", "--model", "x"], []) == ["claude", "--model", "x"]
    assert tmux.scrubbed(["claude"], ["GH_TOKEN", "GITHUB_TOKEN"]) == [
        "/usr/bin/env", "-u", "GH_TOKEN", "-u", "GITHUB_TOKEN", "--", "claude"]


@pytest.fixture
def session(tmp_path):
    name = "brindle_scrub"
    tmux.ensure_session(name, str(tmp_path), {})
    yield name
    tmux.kill_session(name)


def _pane_env(session, tmp_path, env, keep=(), name="w"):
    out = tmp_path / f"{name}.env"
    tmux.new_window(session, name, str(tmp_path), ["/bin/sh", "-c", f'env > "{out}"'], env, keep=keep)
    deadline = time.time() + 10
    while time.time() < deadline and not out.exists():
        time.sleep(0.05)
    time.sleep(0.2)
    return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


def test_a_pane_starts_without_the_servers_secrets_but_with_its_credentials(session, tmp_path):
    # What the tmux server holds (its global environment, where a pane
    # inherits everything from) and what the session adds.
    tmux._tmux("set-environment", "-g", "GH_TOKEN", "gh-secret")
    tmux._tmux("set-environment", "-g", "BRINDLE_PRO_TOKEN", "pro-secret")
    tmux._tmux("set-environment", "-g", "ANTHROPIC_API_KEY", "sk-ant-ok")
    tmux._tmux("set-environment", "-g", "OPENAI_API_KEY", "sk-oa-ok")
    tmux._tmux("set-environment", "-g", "AWS_SESSION_TOKEN", "aws-ok")
    tmux._tmux("set-environment", "-t", f"={session}", "GH_ENTERPRISE_TOKEN", "ghe-secret")
    try:
        assert {"GH_TOKEN", "GH_ENTERPRISE_TOKEN", "BRINDLE_PRO_TOKEN"} <= tmux.inherited_names(session)
        got = _pane_env(session, tmp_path, {"BRINDLE_AGENT_ID": "a1"}, keep=secrets.provider_credentials("claude"))
        assert not {"GH_TOKEN", "GH_ENTERPRISE_TOKEN", "BRINDLE_PRO_TOKEN"} & set(got), sorted(got)
        assert got["ANTHROPIC_API_KEY"] == "sk-ant-ok" and got["AWS_SESSION_TOKEN"] == "aws-ok"
        assert got["OPENAI_API_KEY"] == "sk-oa-ok", "other variables are left alone"
        assert got["BRINDLE_AGENT_ID"] == "a1" and "TMUX_PANE" in got and "PATH" in got
        # Neither a profile's own -e value nor keep gets a job secret through.
        got = _pane_env(session, tmp_path, {"GH_TOKEN": "from-profile"}, keep=["BRINDLE_PRO_TOKEN"], name="p")
        assert "GH_TOKEN" not in got and "BRINDLE_PRO_TOKEN" not in got
        assert "GH_ENTERPRISE_TOKEN" not in got
    finally:
        for name in ("GH_TOKEN", "BRINDLE_PRO_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SESSION_TOKEN"):
            tmux._tmux("set-environment", "-g", "-r", name, check=False)


def test_a_long_command_is_still_scrubbed(session, tmp_path):
    tmux._tmux("set-environment", "-t", f"={session}", "GH_TOKEN", "gh-secret")
    out = tmp_path / "long.env"
    tmux.new_window(session, "long", str(tmp_path),
                    ["/bin/sh", "-c", 'env > "$1"', "sh", str(out), "x" * 20_000], {})
    deadline = time.time() + 10
    while time.time() < deadline and not out.exists():
        time.sleep(0.05)
    time.sleep(0.2)
    text = out.read_text()
    assert "TMUX_PANE=" in text and "GH_TOKEN=" not in text


def _launch(db, repo, monkeypatch, *profiles, **kw):
    """Open a window per (agent_id, profile, provider); returns {name: (env, keep, kwargs)}."""
    ws = workspaces.create(db, str(repo), "keys").workspace
    seen = {}

    def fake_new_window(session, name, cwd, command, env, tag=None, keep=(), **kwargs):
        seen[name] = (dict(env), set(keep), kwargs, command)
        return "%1"
    monkeypatch.setattr(tmux, "new_window", fake_new_window)
    monkeypatch.setattr(tmux, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "apply_theme", lambda *a, **k: None)
    for agent_id, profile, provider in profiles:
        a = Agent(agent_id, ws.id, profile, provider, None, "assign", "starting", "", None, time.time())
        db.add_agent(a)
        agents._open_window(db, a, ws, agent_id, ["sleep", "1"], watch_pane=False, **kw)
    return seen
