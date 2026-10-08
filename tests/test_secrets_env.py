"""Agents start without brindle's secrets. A local worker's pane would
otherwise inherit the tmux server's whole environment (the Pro token, GitHub's
tokens); brindle CI scrubs the same names from the job before any agent
starts. Each provider's own sign-in still reaches it."""

import os
import time

import pytest

from brindle import agents, ci_client, secrets, tmux, workspaces
from brindle.db import Agent


# -- the shared module -----------------------------------------------------------------------

def test_ci_client_uses_the_shared_names():
    assert ci_client.SECRET_ENV is secrets.SECRET_ENV
    assert ci_client.SECRET_PREFIXES is secrets.SECRET_PREFIXES
    assert ci_client.scrub_secrets is secrets.scrub_secrets
    assert ci_client.is_job_secret is secrets.is_job_secret
    assert "scrub_secrets" in ci_client.__all__


def test_scrub_secrets_is_unchanged():
    env = {"BRINDLE_PRO_TOKEN": "t", "GITHUB_TOKEN": "g", "GH_TOKEN": "h", "ACTIONS_RUNTIME_TOKEN": "a",
           "ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-x", "PATH": "/bin"}
    gone = secrets.scrub_secrets(env)
    assert gone == ["ACTIONS_RUNTIME_TOKEN", "ANTHROPIC_API_KEY", "BRINDLE_PRO_TOKEN", "GH_TOKEN",
                    "GITHUB_TOKEN"]
    assert env == {"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-x", "PATH": "/bin"}


def test_pane_unset_names_every_secret_whether_or_not_it_is_set():
    # Fixed names always; prefixed ones from what the pane would inherit.
    unset = secrets.pane_unset(["PATH", "HOME", "GH_TOKEN", "GH_HOST", "OPENAI_API_KEY"])
    assert unset == sorted({*secrets.SECRET_ENV, "GH_TOKEN", "GH_HOST"})
    assert "OPENAI_API_KEY" not in unset and "PATH" not in unset
    # A job secret always loses: ``keep`` never lets one through.
    assert "GITHUB_TOKEN" in secrets.pane_unset(["GITHUB_TOKEN"], keep=["GITHUB_TOKEN"])


@pytest.mark.parametrize("name", ["BRINDLE_PRO_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN",
                                  "GITHUB_ENTERPRISE_TOKEN", "ACTIONS_RUNTIME_TOKEN", "CI_JOB_TOKEN",
                                  "GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN"])
def test_job_secrets_lose_to_keep_allow_and_env(name):
    # keep (a profile's api_key_env, its env.* names, the provider's credentials)
    assert name in secrets.pane_unset([name], keep=[name])
    # env_allow: a glob or the exact name
    assert name in secrets.pane_unset([name], allow=[name])
    assert name in secrets.pane_unset([name], allow=["*"])
    # both together, and with deny
    assert name in secrets.pane_unset([name], keep=[name], allow=["*"], deny=["OPENAI_API_KEY"])


def test_a_profiles_api_key_env_naming_a_job_secret_is_not_a_credential():
    assert "BRINDLE_PRO_TOKEN" not in secrets.provider_credentials("native", "BRINDLE_PRO_TOKEN")
    assert "GH_TOKEN" not in secrets.provider_credentials("native", "GH_TOKEN")
    assert secrets.provider_credentials("native", "MY_MODEL_KEY") == {"MY_MODEL_KEY"}


def test_gh_and_glab_alias_tokens_are_job_secrets():
    # gh reads GITHUB_ENTERPRISE_TOKEN as GH_ENTERPRISE_TOKEN, glab GITLAB_ACCESS_TOKEN as GITLAB_TOKEN
    for name in ("GITHUB_ENTERPRISE_TOKEN", "GITLAB_ACCESS_TOKEN"):
        assert secrets.is_job_secret(name) and name in secrets.pane_unset([])
        env = {name: "x", "PATH": "/bin"}
        assert secrets.scrub_secrets(env) == [name] and env == {"PATH": "/bin"}


def test_native_ci_keys_never_allow_a_job_secret():
    from brindle.ci_adapters import NativeAdapter

    got = NativeAdapter.allowed_keys({"BRINDLE_CI_NATIVE_KEYS": ",".join(
        ["CI_JOB_TOKEN", "GITLAB_TOKEN", "ACTIONS_RUNTIME_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
         "BRINDLE_PRO_TOKEN", "MY_MODEL_KEY"])})
    assert set(got) == {"MY_MODEL_KEY"}


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


def test_the_proxy_token_survives_deny_personal_keys(proxy):
    deny = secrets.pane_deny(secrets.PERSONAL_KEYS, _proxy_env())
    assert "ANTHROPIC_AUTH_TOKEN" not in deny
    assert {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"} <= set(deny)
    assert "ANTHROPIC_AUTH_TOKEN" not in secrets.pane_unset(
        ["ANTHROPIC_AUTH_TOKEN"], keep=secrets.provider_credentials("claude"), deny=deny)


def test_the_proxy_token_from_the_process_environment_counts(proxy, monkeypatch):
    # federation.apply puts both in os.environ, which the agents inherit
    monkeypatch.setenv("ANTHROPIC_BASE_URL", PROXY[0])
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", PROXY[1])
    assert "ANTHROPIC_AUTH_TOKEN" not in secrets.pane_deny(secrets.PERSONAL_KEYS)


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


def test_claude_code_credentials_are_never_scrubbed():
    unset = _unset_for("claude")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert name not in unset
    assert set(unset) == {*secrets.SECRET_ENV, "GH_TOKEN"}
    # A profile with its own key passes it with -e, and -e wins too.
    assert "ANTHROPIC_API_KEY" not in secrets.pane_unset(["ANTHROPIC_API_KEY"], keep=["ANTHROPIC_API_KEY"])


def test_claude_cloud_backends_and_their_credentials_are_never_scrubbed():
    unset = _unset_for("claude")
    for name in ("CLAUDE_CODE_USE_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                 "CLAUDE_CODE_USE_VERTEX", "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_VERTEX_PROJECT_ID",
                 "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_FOUNDRY_API_KEY", "AZURE_CLIENT_SECRET"):
        assert name in secrets.provider_credentials("claude") and name not in unset
    assert set(unset) == {*secrets.SECRET_ENV, "GH_TOKEN"}


def test_codex_credentials_are_never_scrubbed():
    unset = _unset_for("codex")
    assert "OPENAI_API_KEY" not in unset and "CODEX_API_KEY" not in unset
    assert set(unset) == {*secrets.SECRET_ENV, "GH_TOKEN"}
    # Codex's own login is a file under its home: nothing in the environment to keep.
    assert secrets.provider_credentials("codex") == {"OPENAI_API_KEY", "CODEX_API_KEY"}


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


def test_antigravity_credentials_are_never_scrubbed():
    assert "GEMINI_API_KEY" not in _unset_for("antigravity")


def test_provider_credentials_match_what_the_sign_in_check_accepts():
    from brindle import providers

    for provider, names in providers._ENV_AUTH.items():
        assert set(names) <= secrets.provider_credentials(provider), provider


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


def test_agent_launches_scrub_and_keep_the_profiles_credentials(db, repo, monkeypatch):
    """The wiring in agents: every agent pane goes through new_window with
    its provider's credentials kept (a native profile's api_key_env too)."""
    ws = workspaces.create(db, str(repo), "feature").workspace
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True)
    (d / "ghm.md").write_text("---\nname: ghm\nprovider: native\napi: openai\n"
                              "base_url: https://models.github.ai/inference\nmodel: m\n"
                              "api_key_env: GITHUB_TOKEN\n---\nWork.\n")
    seen = {}

    def fake_new_window(session, name, cwd, command, env, tag=None, keep=()):
        seen[name] = (command, env, set(keep))
        return "%1"
    monkeypatch.setattr(tmux, "new_window", fake_new_window)
    monkeypatch.setattr(tmux, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "apply_theme", lambda *a, **k: None)

    for agent_id, profile, provider in (("c1", "developer", "claude"), ("n1", "ghm", "native")):
        a = Agent(agent_id, ws.id, profile, provider, None, "assign", "starting", "", None, time.time())
        db.add_agent(a)
        agents._open_window(db, a, ws, f"{profile}-x", ["sleep", "1"], watch_pane=False)
    _, _, keep = seen["developer-x"]
    assert {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK"} <= keep
    assert "GITHUB_TOKEN" not in keep
    _, _, keep = seen["ghm-x"]
    assert "GITHUB_TOKEN" not in keep


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


def test_a_stored_key_reaches_only_its_own_providers_pane_env(db, repo, monkeypatch):
    from brindle import keystore

    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    keystore.set_key("ANTHROPIC_API_KEY", "sk-ant-test-claude-1111")
    keystore.set_key("OPENAI_API_KEY", "sk-test-codex-2222")
    seen = _launch(db, repo, monkeypatch, ("c1", "developer", "claude"), ("x1", "developer", "codex"))
    env, _, _, command = seen["x1"]
    assert env["OPENAI_API_KEY"] == "sk-test-codex-2222" and "ANTHROPIC_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env
    assert not any("sk-" in a for a in command)
    env, _, _, command = seen["c1"]
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test-claude-1111" and "OPENAI_API_KEY" not in env
    assert not any("sk-" in a for a in command)


def test_the_exported_key_beats_the_stored_one(db, repo, monkeypatch):
    from brindle import keystore

    keystore.set_key("ANTHROPIC_API_KEY", "sk-ant-test-stored-3333")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-exported-4444")
    env, _, _, _ = _launch(db, repo, monkeypatch, ("c1", "developer", "claude"))["c1"]
    assert "ANTHROPIC_API_KEY" not in env   # the pane inherits the exported one untouched


def test_a_stored_key_is_stripped_by_deny_like_an_exported_one(db, repo, monkeypatch):
    from types import SimpleNamespace

    from brindle import keystore

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    keystore.set_key("ANTHROPIC_API_KEY", "sk-ant-test-stored-5555")
    m = SimpleNamespace(denied_keys=("ANTHROPIC_API_KEY",))
    monkeypatch.setattr(agents, "agent_env", lambda *a, **k: {"BRINDLE_AGENT_ID": "c1"})
    env, _, kwargs, _ = _launch(db, repo, monkeypatch, ("c1", "developer", "claude"), m=m)["c1"]
    assert "ANTHROPIC_API_KEY" not in env          # never even fetched for the pane
    assert kwargs["deny"] == ("ANTHROPIC_API_KEY",)   # and new_window strips it as for an exported key


def test_inherited_names_include_this_process(monkeypatch, session):
    monkeypatch.setenv("GH_FROM_LAUNCHER", "x")
    assert "GH_FROM_LAUNCHER" in tmux.inherited_names(session)
    assert "GH_FROM_LAUNCHER" in secrets.pane_unset(tmux.inherited_names(session))
    assert os.environ.get("GH_FROM_LAUNCHER") == "x"
