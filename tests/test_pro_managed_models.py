"""Enterprise managed models: the org policy's ``provider_config`` and
``deny_personal_keys`` on the client."""
import itertools
import json
import time

import pytest

from brindle import agents, company_identity, company_login, doctor, secrets, tmux, workspaces
from brindle.db import Agent
from brindle.pro import license, managed_models, team_policy
from brindle.pro._files import private_dir
from brindle.pro.team_policy import OrgPolicy, ProviderConfig, parse_policy
from brindle.profiles import Profile

from pro_fixtures import backend, claims, fixed_identity, pro_env, sign, signing_key  # noqa: F401
from test_pro_team import ORG, POLICY, plugin, team, team_claims, login  # noqa: F401 - fixtures

BEDROCK = {"provider": "bedrock", "region": "us-east-1",
           "model_ids": ["anthropic.claude-sonnet-4", "anthropic.claude-haiku-4"], "endpoint": None}
VERTEX = {"provider": "vertex", "region": "europe-west1", "model_ids": ["claude-sonnet-4@2025"],
          "endpoint": "acme-project"}
AZURE = {"provider": "azure", "region": None, "model_ids": ["gpt-5"],
         "endpoint": "https://acme.openai.azure.com/v1"}
COMPAT = {"provider": "openai-compatible", "region": None, "model_ids": [],
          "endpoint": "https://llm.acme.internal/v1"}


def cfg(d):
    return ProviderConfig(d["provider"], d["region"], tuple(d["model_ids"]), d["endpoint"])


def managed(d=None, deny=False):
    return managed_models.Managed(ORG, cfg(d) if d else None, deny)


def profile(provider="claude", **kw):
    return Profile(name="dev", description="", provider=provider, prompt="", **kw)


# -- the policy ---------------------------------------------------------------------------------


def test_the_policy_fields_are_parsed():
    p = parse_policy(ORG, {"version": 1, "policy": {**POLICY, "provider_config": BEDROCK,
                                                    "deny_personal_keys": True}})
    assert p.provider_config == cfg(BEDROCK) and p.deny_personal_keys is True


def test_a_policy_without_them_manages_nothing():
    p = parse_policy(ORG, {"version": 1, "policy": POLICY})
    assert p.provider_config is None and p.deny_personal_keys is False


def test_the_effective_policy_inherits_them_unless_it_sets_its_own():
    body = {"version": 1, "policy": {**POLICY, "provider_config": BEDROCK, "deny_personal_keys": True},
            "effective": dict(POLICY)}
    assert parse_policy(ORG, body).enforced.provider_config == cfg(BEDROCK)
    body["effective"] = {**POLICY, "provider_config": VERTEX, "deny_personal_keys": False}
    e = parse_policy(ORG, body).enforced
    assert e.provider_config == cfg(VERTEX) and e.deny_personal_keys is False


def test_the_fields_survive_the_cache():
    p = parse_policy(ORG, {"version": 1, "policy": {**POLICY, "provider_config": VERTEX,
                                                    "deny_personal_keys": True}})
    again = parse_policy(ORG, json.loads(json.dumps(p.to_json())))
    assert (again.provider_config, again.deny_personal_keys) == (cfg(VERTEX), True)


@pytest.mark.parametrize("over", [
    {"provider_config": "bedrock"}, {"provider_config": {"provider": "openai"}},
    {"provider_config": {"provider": "bedrock", "region": 3}},
    {"provider_config": {"provider": "bedrock", "model_ids": "x"}},
    {"provider_config": {"provider": "bedrock", "model_ids": [""]}},
    {"provider_config": {"provider": "azure", "endpoint": ""}},
    {"deny_personal_keys": "yes"},
])
def test_malformed_fields_make_the_policy_unusable(over):
    with pytest.raises(team_policy.PolicyUnavailable):
        parse_policy(ORG, {"version": 1, "policy": {**POLICY, **over}})


@pytest.fixture
def enterprise(team, monkeypatch):
    """A member of a team org with managed_models and a policy that sets both fields."""
    login(team, team_claims(features=["learning", "team", "managed_models"]))
    team.policy_body = {**team.policy_body, "policy": {**POLICY, "provider_config": BEDROCK,
                                                       "deny_personal_keys": True}}
    monkeypatch.setattr(license, "has", lambda feature: feature == "managed_models")
    monkeypatch.setattr(team_policy, "managed_models", lambda root: plugin(team).managed_models())
    return team


def test_managed_settings_come_from_the_org_policy(enterprise):
    m = managed_models.current("/repo")
    assert m.org_id == ORG and m.config == cfg(BEDROCK) and m.deny_personal_keys
    assert m.denied_keys == secrets.PERSONAL_KEYS


def test_without_the_feature_nothing_is_managed(team, monkeypatch):
    team.policy_body = {**team.policy_body, "policy": {**POLICY, "provider_config": BEDROCK,
                                                       "deny_personal_keys": True}}
    monkeypatch.setattr(license, "has", lambda feature: False)
    monkeypatch.setattr(team_policy, "managed_models", lambda root: plugin(team).managed_models())
    assert managed_models.current("/repo") is None
    assert f"GET /orgs/{ORG}/policy" not in team.paths()


def test_an_org_that_sets_neither_field_manages_nothing(enterprise):
    enterprise.policy_body = {**enterprise.policy_body, "policy": dict(POLICY)}
    assert managed_models.current("/repo") is None


def test_an_unreadable_policy_fails_closed(enterprise):
    from brindle.pro import auth

    enterprise.routes[f"GET /orgs/{ORG}/policy"] = [auth.TransportError("down")]
    with pytest.raises(managed_models.ManagedUnavailable, match="never been fetched"):
        managed_models.current("/repo")


def test_a_license_that_cant_be_checked_means_not_managed(team, monkeypatch):
    def boom(feature):
        raise RuntimeError("no license")

    monkeypatch.setattr(license, "has", boom)
    assert plugin(team).managed_models() is None


# -- the provider -------------------------------------------------------------------------------


def test_bedrock_env():
    assert managed_models.provider_env(cfg(BEDROCK), "claude") == {
        "CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1",
        "ANTHROPIC_MODEL": "anthropic.claude-sonnet-4",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "anthropic.claude-haiku-4"}


def test_vertex_env():
    assert managed_models.provider_env(cfg(VERTEX), "claude") == {
        "CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": "europe-west1",
        "ANTHROPIC_VERTEX_PROJECT_ID": "acme-project", "ANTHROPIC_MODEL": "claude-sonnet-4@2025"}


def test_vertex_project_wins_over_endpoint():
    both = ProviderConfig("vertex", "europe-west1", (), "legacy-endpoint", "acme-project")
    assert managed_models.provider_env(both, "claude")["ANTHROPIC_VERTEX_PROJECT_ID"] == "acme-project"
    only = ProviderConfig("vertex", "europe-west1", (), None, "acme-project")
    assert managed_models.provider_env(only, "claude")["ANTHROPIC_VERTEX_PROJECT_ID"] == "acme-project"
    old = ProviderConfig("vertex", "europe-west1", (), "legacy-endpoint")
    assert managed_models.provider_env(old, "claude")["ANTHROPIC_VERTEX_PROJECT_ID"] == "legacy-endpoint"


def test_provider_config_project_is_parsed():
    from brindle.pro import team_policy

    got = team_policy._managed({"provider_config": {**VERTEX, "project": "acme-project"}})
    assert got["provider_config"].project == "acme-project"
    assert team_policy._managed({"provider_config": VERTEX})["provider_config"].project is None


def test_personal_key_refusal_says_how_to_fix_it():
    why = managed_models.refusal(managed(COMPAT, deny=True),
                                 profile("native", api_key_env="OPENAI_API_KEY"))
    assert "OPENAI_API_KEY" in why and "api_key_env" in why


def test_azure_and_openai_compatible_env():
    assert managed_models.provider_env(cfg(AZURE), "claude")["CLAUDE_CODE_USE_FOUNDRY"] == "1"
    assert managed_models.provider_env(cfg(AZURE), "codex") == {"OPENAI_BASE_URL": AZURE["endpoint"]}
    assert managed_models.provider_env(cfg(COMPAT), "codex") == {"OPENAI_BASE_URL": COMPAT["endpoint"]}
    assert managed_models.provider_env(cfg(COMPAT), "claude") == {}
    assert managed_models.provider_env(cfg(BEDROCK), "codex") == {}


def test_native_profiles_get_the_managed_endpoint_and_model():
    p = managed_models.apply_profile(managed(AZURE), profile("native", model="llama3"))
    assert (p.base_url, p.model) == (AZURE["endpoint"], "gpt-5")
    same = managed_models.apply_profile(managed(AZURE), profile("native", model="gpt-5"))
    assert same.model == "gpt-5"


def test_claude_profiles_get_a_model_the_provider_serves():
    p = managed_models.apply_profile(managed(BEDROCK), profile(model="sonnet"))
    assert p.model == "anthropic.claude-sonnet-4"
    kept = managed_models.apply_profile(managed(BEDROCK), profile(model="anthropic.claude-haiku-4"))
    assert kept.model == "anthropic.claude-haiku-4"


def test_nothing_changes_without_a_provider_config():
    p = profile("native", base_url="http://localhost:11434/v1", model="x")
    assert managed_models.apply_profile(managed(deny=True), p) is p
    assert managed_models.apply_profile(None, p) is p
    assert managed_models.agent_env(managed(deny=True), "claude") == {}


# -- refusals -----------------------------------------------------------------------------------


@pytest.mark.parametrize("m, p, words", [
    (managed(BEDROCK), profile(env={"ANTHROPIC_BASE_URL": "https://proxy"}), "ANTHROPIC_BASE_URL"),
    (managed(BEDROCK), profile(env={"CLAUDE_CODE_USE_VERTEX": "1"}), "CLAUDE_CODE_USE_VERTEX"),
    (managed(COMPAT), profile(), "can't use an OpenAI-compatible endpoint"),
    (managed(BEDROCK), profile("codex"), "Codex, which can't use AWS Bedrock"),
    (managed(AZURE), profile("codex", env={"OPENAI_BASE_URL": "https://x"}), "OPENAI_BASE_URL"),
    (managed(BEDROCK), profile("native", base_url="http://localhost:11434/v1"), "its own endpoint"),
    (managed(AZURE), profile("native", base_url="http://localhost:11434/v1"), "not " + AZURE["endpoint"]),
    (managed(BEDROCK), profile("antigravity"), "antigravity, which can't run on AWS Bedrock"),
    (managed(deny=True), profile(env={"ANTHROPIC_API_KEY": "sk"}), "ANTHROPIC_API_KEY"),
    (managed(deny=True), profile("native", api_key_env="OPENAI_API_KEY"), "OPENAI_API_KEY"),
])
def test_profiles_pointing_elsewhere_are_refused_with_a_reason(m, p, words):
    why = managed_models.refusal(m, p)
    assert why and words in why and ORG in why


@pytest.mark.parametrize("m, p", [
    (managed(BEDROCK), profile(env={"EDITOR": "vim"})),
    (managed(AZURE), profile("codex")),
    (managed(AZURE), profile("native", base_url=AZURE["endpoint"] + "/")),
    (managed(AZURE), profile("native")),
    (managed(BEDROCK), profile("shell")),
    (managed(deny=True), profile("native", api_key_env="ACME_LLM_KEY")),
    (managed(deny=True), profile("antigravity")),
])
def test_compatible_profiles_run(m, p):
    assert managed_models.refusal(m, p) is None


# -- personal keys ------------------------------------------------------------------------------


def test_pane_unset_always_takes_denied_names_out():
    names = ["PATH", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_PROFILE"]
    keep = secrets.provider_credentials("claude") | {"OPENAI_API_KEY"}
    plain = secrets.pane_unset(names, keep=keep)
    assert "ANTHROPIC_API_KEY" not in plain and "AWS_PROFILE" not in plain
    gone = secrets.pane_unset(names, keep=keep, deny=secrets.PERSONAL_KEYS)
    assert {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"} <= set(gone)
    assert "AWS_PROFILE" not in gone and "PATH" not in gone
    # whatever env_allow lists
    assert "ANTHROPIC_API_KEY" in secrets.pane_unset(names, keep=keep, allow=["ANTHROPIC_API_KEY"],
                                                    deny=secrets.PERSONAL_KEYS)


def test_personal_keys_cover_every_provider_sign_in_key():
    for p in ("claude", "codex", "antigravity"):
        keys = {k for k in secrets.provider_credentials(p)
                if k.endswith(("_API_KEY", "_AUTH_TOKEN", "OAUTH_TOKEN")) and "FOUNDRY" not in k
                and not k.startswith(("AWS_", "AZURE_"))}
        assert keys <= set(secrets.PERSONAL_KEYS), p


# -- the wiring in agents -----------------------------------------------------------------------


def agent(db, ws, provider="claude", profile_name="developer"):
    db.add_agent(Agent("w1", ws.id, profile_name, provider, None, "assign", "idle", "@0", None,
                       time.time()))
    return db.get_agent("w1")


@pytest.fixture
def ws(db, repo):
    return workspaces.adopt_root(db, str(repo))


def test_agent_env_points_workers_at_the_managed_provider(db, ws, monkeypatch):
    a = agent(db, ws)
    monkeypatch.setattr(agents, "managed", lambda root: managed(BEDROCK, deny=True))
    env = agents.agent_env(ws, "w1", a)
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1" and env["AWS_REGION"] == "us-east-1"
    assert env["BRINDLE_AGENT_ID"] == "w1"


def test_agent_env_is_untouched_without_managed_models(db, ws, monkeypatch):
    a = agent(db, ws)
    monkeypatch.setattr(agents, "managed", lambda root: None)
    assert "CLAUDE_CODE_USE_BEDROCK" not in agents.agent_env(ws, "w1", a)


def test_an_unreadable_policy_stops_every_launch(db, ws, monkeypatch):
    a = agent(db, ws)

    def down(root):
        raise managed_models.ManagedUnavailable("no policy")

    monkeypatch.setattr(managed_models, "current", down)
    with pytest.raises(agents.AgentError, match="managed models: no policy"):
        agents.agent_env(ws, "w1", a)
    with pytest.raises(agents.AgentError, match="managed models"):
        agents._profile_for(db, a, ws)


def test_a_launch_reads_the_policy_once(db, ws, monkeypatch):
    a = agent(db, ws)
    calls = []

    def once(root):
        calls.append(root)
        return managed(BEDROCK, deny=True)

    monkeypatch.setattr(agents, "managed", once)
    monkeypatch.setattr(agents.tmux, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(agents.tmux, "new_window", lambda *a, **k: "@1")
    monkeypatch.setattr(agents, "_ensure_sidebar", lambda *a, **k: None)
    monkeypatch.setattr(agents.tmux, "apply_theme", lambda *a, **k: None)
    monkeypatch.setattr(agents, "signed_out", lambda *a, **k: None)
    agents._launch(db, a, ws, prompt=None, resume=None, watch_pane=False)
    assert len(calls) == 1


def test_a_refused_profile_does_not_launch(db, ws, repo, monkeypatch):
    (repo / ".brindle" / "agents").mkdir(parents=True)
    (repo / ".brindle" / "agents" / "developer.md").write_text(
        "---\nname: developer\nprovider: claude\nenv.ANTHROPIC_BASE_URL: https://proxy.example\n---\nGo.\n")
    a = agent(db, ws)
    monkeypatch.setattr(agents, "managed", lambda root: managed(BEDROCK))
    with pytest.raises(agents.AgentError, match="managed models: .*ANTHROPIC_BASE_URL"):
        agents._profile_for(db, a, ws)


def test_the_launched_profile_has_the_managed_model(db, ws, monkeypatch):
    a = agent(db, ws)
    monkeypatch.setattr(agents, "managed", lambda root: managed(BEDROCK))
    assert agents._profile_for(db, a, ws).model == "anthropic.claude-sonnet-4"


def test_the_pane_is_opened_without_personal_keys(db, ws, monkeypatch):
    a = agent(db, ws)
    seen = {}

    def fake_new_window(session, name, cwd, command, env, tag=None, keep=(), **kw):
        seen.update(env=env, keep=set(keep), kw=kw)
        return "%1"

    monkeypatch.setattr(tmux, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "new_window", fake_new_window)
    monkeypatch.setattr(tmux, "current_server", lambda: "s")
    monkeypatch.setattr(tmux, "apply_theme", lambda *a: None)
    monkeypatch.setattr(agents, "_ensure_sidebar", lambda *a, **k: None)
    monkeypatch.setattr(agents, "managed", lambda root: managed(BEDROCK, deny=True))
    agents._open_window(db, a, ws, "w", ["claude"], False)
    assert seen["kw"]["deny"] == secrets.PERSONAL_KEYS
    assert seen["env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"

    seen.clear()
    monkeypatch.setattr(agents, "managed", lambda root: managed(BEDROCK))
    agents._open_window(db, a, ws, "w", ["claude"], False)
    assert "deny" not in seen["kw"]


def test_real_panes_start_without_personal_keys(tmp_path, monkeypatch):
    """The unset really happens: a pane started with ``deny`` doesn't see the key."""
    import shutil
    import subprocess

    if not shutil.which("tmux"):
        pytest.skip("tmux isn't installed")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-personal")
    monkeypatch.setenv("AWS_PROFILE", "company")
    session = f"managed-{time.time_ns()}"
    tmux.ensure_session(session, str(tmp_path), {"ANTHROPIC_API_KEY": "sk-personal", "AWS_PROFILE": "company"})
    out = tmp_path / "env.txt"
    try:
        tmux.new_window(session, "w", str(tmp_path), ["/bin/sh", "-c", f'env > "{out}"'], {},
                        keep=secrets.provider_credentials("claude"), deny=secrets.PERSONAL_KEYS)
        for _ in range(50):
            if out.exists() and out.stat().st_size:
                break
            time.sleep(0.1)
        text = out.read_text()
    finally:
        subprocess.run(["tmux", *(["-L", tmux.socket()] if hasattr(tmux, "socket") else []),
                        "kill-session", "-t", f"={session}"], capture_output=True)
    assert "ANTHROPIC_API_KEY" not in text and "sk-personal" not in text
    assert "AWS_PROFILE=company" in text


def test_real_panes_keep_the_federation_proxys_token_under_deny(tmp_path, monkeypatch):
    """In federated CI the pane's ANTHROPIC_AUTH_TOKEN is the credential proxy's own
    secret: deny_personal_keys strips a person's token but not that one."""
    import shutil

    if not shutil.which("tmux"):
        pytest.skip("tmux isn't installed")
    url, token = "http://127.0.0.1:41999", "proxy-secret"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", token)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-personal")
    secrets.register_proxy(url, token)
    session = f"managed-proxy-{time.time_ns()}"
    tmux.ensure_session(session, str(tmp_path), {})
    out = tmp_path / "env.txt"
    try:
        tmux.new_window(session, "w", str(tmp_path), ["/bin/sh", "-c", f'env > "{out}"'], {},
                        keep=secrets.provider_credentials("claude"), deny=secrets.PERSONAL_KEYS)
        for _ in range(50):
            if out.exists() and out.stat().st_size:
                break
            time.sleep(0.1)
        text = out.read_text()
        # a personal token in the same slot, with the proxy's URL, is still stripped
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-mine")
        out2 = tmp_path / "env2.txt"
        tmux.new_window(session, "w2", str(tmp_path), ["/bin/sh", "-c", f'env > "{out2}"'], {},
                        keep=secrets.provider_credentials("claude"), deny=secrets.PERSONAL_KEYS)
        for _ in range(50):
            if out2.exists() and out2.stat().st_size:
                break
            time.sleep(0.1)
        text2 = out2.read_text()
    finally:
        secrets.clear_proxy()
        tmux.kill_session(session)
    assert f"ANTHROPIC_AUTH_TOKEN={token}" in text and f"ANTHROPIC_BASE_URL={url}" in text
    assert "ANTHROPIC_API_KEY" not in text and "sk-personal" not in text
    assert "ANTHROPIC_AUTH_TOKEN" not in text2 and "sk-ant-oat01-mine" not in text2


# -- the routes of an enforced agent_setup (model-routing phase 1) ------------------------------
#
# A phase-1 server writes routing to ``agent_setup`` (always with ``exclusive``,
# true when it converted a ``provider_config``) and keeps ``provider_config`` as
# a mirror for older clients. This client takes ``agent_setup`` as the managed
# source when it carries ``exclusive`` and is enforced and routes an agent
# somewhere (or is exclusive); else ``provider_config``, exactly as before.

FOUNDRY_URL = "https://acme.services.ai.azure.com/anthropic"
GATEWAY_URL = "https://llm.acme.internal/v1"
ROUTED = {"enforce": True, "exclusive": False,
          "claude": {"route": "bedrock", "aws": {"region": "us-east-1"},
                     "model_ids": BEDROCK["model_ids"], "default_model": BEDROCK["model_ids"][0]},
          "codex": {"route": "gateway", "base_url": GATEWAY_URL}}


def from_policy(monkeypatch, **policy):
    """``managed_models.current`` for a policy answer, with managed_models entitled."""
    monkeypatch.setattr(license, "has", lambda feature: feature == "managed_models")
    monkeypatch.setattr(team_policy, "managed_models",
                        lambda root: parse_policy(ORG, {"version": 1, "policy": {**POLICY, **policy}}))
    return managed_models.current("/repo")


def test_an_enforced_routed_agent_setup_is_what_is_managed(monkeypatch):
    m = from_policy(monkeypatch, agent_setup=ROUTED, provider_config=VERTEX)
    assert m.setup is not None and m.config is None           # agent_setup wins over the mirror
    assert m.claude.route == "bedrock" and m.codex.base_url == GATEWAY_URL
    assert m.deny_personal_keys is False and m.denied_keys == ()


def test_without_enforce_the_block_is_login_help_and_provider_config_applies(monkeypatch):
    m = from_policy(monkeypatch, agent_setup={**ROUTED, "enforce": False}, provider_config=VERTEX)
    assert m.setup is None and m.config == cfg(VERTEX)
    assert from_policy(monkeypatch, agent_setup={**ROUTED, "enforce": False}) is None


def test_enforce_without_a_cloud_route_manages_nothing_by_itself(monkeypatch):
    setup = {"enforce": True, "exclusive": False,
             "claude": {"route": "subscription", "org_id": "3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c"},
             "codex": {"route": "chatgpt"}}
    assert from_policy(monkeypatch, agent_setup=setup) is None
    m = from_policy(monkeypatch, agent_setup=setup, provider_config=BEDROCK)
    assert m.setup is None and m.config == cfg(BEDROCK)
    # exclusive: the login routes are the routes, and every other agent is refused
    m = from_policy(monkeypatch, agent_setup={**setup, "exclusive": True}, provider_config=BEDROCK)
    assert m.setup is not None and m.config is None and m.exclusive
    assert managed_models.refusal(m, profile()) is None and managed_models.refusal(m, profile("codex")) is None
    assert "native" in managed_models.refusal(m, profile("native"))
    assert "antigravity" in managed_models.refusal(m, profile("antigravity"))


OLD_SERVER = {"enforce": True, "claude": {"route": "bedrock", "aws": {"region": "eu-west-1"}}}   # no "exclusive"


def test_without_the_exclusive_key_provider_config_stays_the_managed_source(db, ws, monkeypatch):
    """Today's production server can hold an enforced agent_setup next to an
    independently chosen provider_config, and provider_config wins; the
    agent_setup is login help plus the identity check. Unchanged until the
    server migration ships (a phase-1 server always sends ``exclusive``)."""
    m = from_policy(monkeypatch, agent_setup=OLD_SERVER, provider_config=VERTEX, deny_personal_keys=True)
    assert m.setup is None and m.config == cfg(VERTEX) and m.deny_personal_keys
    setup = team_policy._parse_agent_setup(OLD_SERVER)
    assert setup.enforce and setup.routing is False
    # refusals are provider_config's
    why = managed_models.refusal(m, profile("codex"))
    assert why == managed_models.refusal(managed(VERTEX, deny=True), profile("codex"))
    assert "Codex, which can't use Google Vertex AI" in why
    assert "away from Google Vertex AI" in managed_models.refusal(m, profile(env={"ANTHROPIC_BASE_URL": "https://x"}))
    assert "isn't Google Vertex AI" in managed_models.refusal(m, profile("native", base_url="http://localhost:11434/v1"))
    assert managed_models.apply_profile(m, profile(model="sonnet")).model == VERTEX["model_ids"][0]
    # the pane env is today's layering: provider_config's, then the enforced route on top
    monkeypatch.setattr(company_identity, "_setup", lambda: (ORG, setup))
    monkeypatch.setattr(company_login, "stored_env", lambda: {})
    a = Agent("w1", ws.id, "developer", "claude", None, "assign", "idle", "@0", None, time.time())
    db.add_agent(a)
    env = {k: v for k, v in agents.agent_env(ws, "w1", a, m).items()
           if not k.startswith("BRINDLE_") and k != "ENABLE_TOOL_SEARCH"}
    assert env == {**managed_models.provider_env(cfg(VERTEX), "claude"), **company_login.claude_env(ORG, setup.claude)}
    # with no provider_config, an old-server block manages nothing by itself (as today)
    assert from_policy(monkeypatch, agent_setup=OLD_SERVER) is None
    m = from_policy(monkeypatch, agent_setup=OLD_SERVER, deny_personal_keys=True)
    assert m.setup is None and m.config is None and m.deny_personal_keys


def test_enforce_needs_the_entitlement(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: False)
    p = parse_policy(ORG, {"version": 1, "policy": {**POLICY, "agent_setup": ROUTED}})
    assert p.agent_setup.enforce is False
    monkeypatch.setattr(team_policy, "managed_models", lambda root: p)
    assert managed_models.current("/repo") is None


@pytest.mark.parametrize("in_block, top, want", [
    (True, False, True), (False, True, False), (None, True, True), (None, False, False), (True, True, True)])
def test_deny_personal_keys_from_the_block_else_the_top_level_field(monkeypatch, in_block, top, want):
    setup = {**ROUTED, **({"deny_personal_keys": in_block} if in_block is not None else {})}
    m = from_policy(monkeypatch, agent_setup=setup, deny_personal_keys=top)
    assert m.deny_personal_keys is want
    plain = {k: v for k, v in setup.items() if k not in ("claude", "codex")}     # nothing routed
    m = from_policy(monkeypatch, agent_setup=plain, deny_personal_keys=top)
    assert (m.deny_personal_keys if m else False) is want and (m is None or m.config is None)


def setup_managed(raw, deny=False, org=ORG):
    monkeypatch_has = license.has
    try:
        license.has = lambda feature: True
        s = team_policy._parse_agent_setup(raw)
    finally:
        license.has = monkeypatch_has
    return managed_models.Managed(org, None, deny, setup=s)


def test_route_env_delegates_to_the_one_env_builder():
    m = setup_managed(ROUTED)
    assert managed_models.agent_env(m, "claude") == company_login.claude_env(ORG, m.setup.claude)
    assert managed_models.agent_env(m, "claude") == {
        "CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1",
        "ANTHROPIC_MODEL": "anthropic.claude-sonnet-4", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "anthropic.claude-haiku-4"}
    assert managed_models.agent_env(m, "codex") == {"OPENAI_BASE_URL": GATEWAY_URL}
    assert managed_models.agent_env(m, "native") == {}
    assert managed_models.agent_env(m, "antigravity") == {}


VERTEX_ONLY = {"enforce": True, "exclusive": False,
               "claude": {"route": "vertex", "gcp": {"project": "acme-project", "region": "europe-west1"}}}
CODEX_ONLY = {"enforce": True, "exclusive": False, "codex": {"route": "azure", "base_url": AZURE["endpoint"]}}


def test_without_exclusive_an_agent_with_no_route_runs_unmanaged():
    """An org that set up Company agents directly keeps running Codex."""
    m = setup_managed(VERTEX_ONLY)
    assert not m.exclusive
    assert managed_models.agent_env(m, "codex") == {} and m.codex is None
    assert managed_models.refusal(m, profile("codex")) is None
    assert managed_models.refusal(m, profile("codex", env={"OPENAI_BASE_URL": "https://x"})) is None
    assert managed_models.refusal(m, profile("native", base_url="http://localhost:11434/v1")) is None
    assert managed_models.refusal(m, profile("antigravity")) is None
    m = setup_managed(CODEX_ONLY)
    assert m.claude is None and managed_models.agent_env(m, "claude") == {}
    assert managed_models.refusal(m, profile(env={"ANTHROPIC_BASE_URL": "https://proxy"})) is None
    assert managed_models.refusal(m, profile("codex", env={"OPENAI_BASE_URL": "https://x"}))


@pytest.mark.parametrize("raw, p, words", [
    (VERTEX_ONLY, profile("codex"), "runs Codex, which can't use Google Vertex AI"),
    (VERTEX_ONLY, profile("codex", env={"OPENAI_BASE_URL": "https://x"}), "runs Codex, which can't use"),
    (VERTEX_ONLY, profile("native"), "uses its own endpoint, which isn't Google Vertex AI"),
    (VERTEX_ONLY, profile("native", base_url="http://localhost:11434/v1"), "uses its own endpoint"),
    (VERTEX_ONLY, profile("antigravity"), "uses antigravity, which can't run on Google Vertex AI"),
    (CODEX_ONLY, profile(), "runs Claude Code, which can't use Azure"),
    (CODEX_ONLY, profile(env={"EDITOR": "vim"}), "runs Claude Code, which can't use Azure"),
    (CODEX_ONLY, profile("antigravity"), "can't run on Azure"),
    ({"enforce": True, "exclusive": True}, profile(), "can't use the routes"),
])
def test_with_exclusive_an_agent_with_no_route_is_refused_as_provider_config_refused_it(raw, p, words):
    why = managed_models.refusal(setup_managed({**raw, "exclusive": True}), p)
    assert why and words in why and f"org {ORG} manages" in why


@pytest.mark.parametrize("raw, p", [
    (VERTEX_ONLY, profile()), (VERTEX_ONLY, profile("shell")), (VERTEX_ONLY, profile("subagent")),
    (CODEX_ONLY, profile("codex")), (CODEX_ONLY, profile("native")),
    ({**VERTEX_ONLY, "codex": {"route": "chatgpt"}}, profile("codex")),    # a login route is a route
    ({**CODEX_ONLY, "claude": {"route": "subscription", "org_id": "3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c"}}, profile()),
])
def test_with_exclusive_an_agent_with_a_route_runs(raw, p):
    assert managed_models.refusal(setup_managed({**raw, "exclusive": True}), p) is None


def test_exclusive_shows_in_doctor():
    assert "agents without a route refused" in setup_managed({**VERTEX_ONLY, "exclusive": True}).describe()
    assert "refused" not in setup_managed(VERTEX_ONLY).describe()


@pytest.mark.parametrize("p, words", [
    (profile(env={"ANTHROPIC_BASE_URL": "https://proxy"}), "ANTHROPIC_BASE_URL"),
    (profile(env={"CLAUDE_CODE_USE_VERTEX": "1"}), "away from AWS Bedrock"),
    (profile("codex", env={"OPENAI_BASE_URL": "https://x"}), "away from an OpenAI-compatible endpoint"),
    (profile("native", base_url="http://localhost:11434/v1"), "not " + GATEWAY_URL),
])
def test_routed_profiles_pointing_elsewhere_are_refused(p, words):
    why = managed_models.refusal(setup_managed(ROUTED), p)
    assert why and words in why and ORG in why


@pytest.mark.parametrize("p", [
    profile(env={"EDITOR": "vim"}), profile("codex"), profile("native", base_url=GATEWAY_URL + "/"),
    profile("native"), profile("shell"), profile("antigravity")])
def test_routed_compatible_profiles_run(p):
    assert managed_models.refusal(setup_managed(ROUTED), p) is None


def test_routed_personal_keys_are_denied_and_refused():
    m = setup_managed(ROUTED, deny=True)
    assert m.denied_keys == secrets.PERSONAL_KEYS
    assert "ANTHROPIC_API_KEY" in managed_models.refusal(m, profile(env={"ANTHROPIC_API_KEY": "sk"}))


def test_routed_profiles_get_the_default_model_and_the_codex_endpoint():
    m = setup_managed(ROUTED)
    assert managed_models.apply_profile(m, profile(model="sonnet")).model == "anthropic.claude-sonnet-4"
    assert managed_models.apply_profile(m, profile(model="anthropic.claude-haiku-4")).model == "anthropic.claude-haiku-4"
    n = managed_models.apply_profile(m, profile("native", base_url="http://localhost:11434/v1", model="llama3"))
    assert (n.base_url, n.model) == (GATEWAY_URL, "llama3")
    unrouted = setup_managed({"enforce": True, "exclusive": False,
                              "claude": {"route": "gateway", "gateway": {"base_url": GATEWAY_URL}}})
    assert managed_models.apply_profile(unrouted, profile(model="sonnet")).model == "sonnet"    # no model_ids
    p = profile("native", base_url="http://localhost:11434/v1")
    assert managed_models.apply_profile(unrouted, p) is p


def test_describe_names_the_routes():
    text = setup_managed(ROUTED, deny=True).describe()
    assert text.startswith(f"org {ORG}: ")
    assert "Claude on AWS Bedrock (region us-east-1, your own AWS sign-in, models" in text
    assert f"Codex on an OpenAI-compatible endpoint ({GATEWAY_URL})" in text
    assert "personal API keys denied" in text


# -- an old provider_config policy and its migrated agent_setup behave the same ------------------

def migrated(pc: dict, deny: bool) -> dict:
    """The ``agent_setup`` the server derives from ``pc`` (contract section 4);
    the server sends ``pc`` next to it as the mirror for older clients."""
    ids = list(pc["model_ids"])
    models = {"model_ids": ids, **({"default_model": ids[0]} if ids else {})}
    if pc["provider"] == "bedrock":
        setup = {"claude": {"route": "bedrock", "aws": {"region": pc["region"]}, **models}}
    elif pc["provider"] == "vertex":
        setup = {"claude": {"route": "vertex", "gcp": {"project": pc.get("project") or pc["endpoint"],
                                                       "region": pc["region"]}, **models}}
    elif pc["provider"] == "azure":
        setup = {"claude": {"route": "foundry", "azure": {"base_url": pc["endpoint"]}, **models},
                 "codex": {"route": "azure", "base_url": pc["endpoint"]}}
    else:
        # openai-compatible: a Codex route only; Claude Code gets no route (refused, as today)
        setup = {"codex": {"route": "gateway", "base_url": pc["endpoint"]}}
    return {**setup, "enforce": True, "exclusive": True, "deny_personal_keys": deny}


PROFILES = {
    "claude": profile(), "claude-proxy": profile(env={"ANTHROPIC_BASE_URL": "https://proxy"}),
    "claude-vertex": profile(env={"CLAUDE_CODE_USE_VERTEX": "1"}), "claude-editor": profile(env={"EDITOR": "vim"}),
    "claude-key": profile(env={"ANTHROPIC_API_KEY": "sk"}),
    "codex": profile("codex"), "codex-elsewhere": profile("codex", env={"OPENAI_BASE_URL": "https://x"}),
    "native-elsewhere": profile("native", base_url="http://localhost:11434/v1"), "native": profile("native"),
    "native-key": profile("native", api_key_env="OPENAI_API_KEY"),
    "shell": profile("shell"), "antigravity": profile("antigravity"),
}
_ids = itertools.count()


def pane_env(db, ws, monkeypatch, m, provider):
    """What a pane of ``provider`` gets from ``m`` through ``agents.agent_env``."""
    monkeypatch.setattr(company_identity, "_setup", lambda: (ORG, m.setup) if m.setup is not None else None)
    monkeypatch.setattr(company_login, "stored_env", lambda: {})
    a = Agent(f"w{next(_ids)}", ws.id, "developer", provider, None, "assign", "idle", "@0", None, time.time())
    db.add_agent(a)
    env = agents.agent_env(ws, a.id, a, m)
    return {k: v for k, v in env.items() if not k.startswith("BRINDLE_") and k != "ENABLE_TOOL_SEARCH"}


@pytest.mark.parametrize("pc", [BEDROCK, VERTEX, AZURE, COMPAT], ids=lambda d: d["provider"])
@pytest.mark.parametrize("deny", [True, False])
def test_an_old_policy_and_its_migrated_form_are_equivalent(db, ws, monkeypatch, pc, deny):
    old = from_policy(monkeypatch, provider_config=pc, deny_personal_keys=deny)
    new = from_policy(monkeypatch, agent_setup=migrated(pc, deny), provider_config=pc, deny_personal_keys=deny)
    assert old.setup is None and new.setup is not None and new.exclusive
    assert old.denied_keys == new.denied_keys
    for provider in ("claude", "codex", "native"):
        assert pane_env(db, ws, monkeypatch, old, provider) == pane_env(db, ws, monkeypatch, new, provider), provider
    for name, p in PROFILES.items():
        before, after = managed_models.refusal(old, p), managed_models.refusal(new, p)
        assert (before is None) == (after is None), (name, before, after)
        if before is not None:
            assert before == after, name
        launched = managed_models.apply_profile(new, p)
        if p.provider == "claude" and pc["model_ids"]:
            assert launched.model == managed_models.apply_profile(old, p).model == pc["model_ids"][0]
        if p.provider == "native" and before is None:
            assert launched.base_url == managed_models.apply_profile(old, p).base_url


def test_the_migrated_openai_compatible_policy_still_refuses_claude_code(db, ws, monkeypatch):
    old = from_policy(monkeypatch, provider_config=COMPAT)
    new = from_policy(monkeypatch, agent_setup=migrated(COMPAT, False), provider_config=COMPAT)
    assert new.setup.claude is None and new.codex.route == "gateway"
    why = managed_models.refusal(new, PROFILES["claude"])
    assert why == managed_models.refusal(old, PROFILES["claude"])
    assert "runs Claude Code, which can't use an OpenAI-compatible endpoint" in why
    assert pane_env(db, ws, monkeypatch, new, "claude") == pane_env(db, ws, monkeypatch, old, "claude")
    assert "ANTHROPIC_BASE_URL" not in pane_env(db, ws, monkeypatch, new, "claude")
    assert pane_env(db, ws, monkeypatch, new, "codex") == {"OPENAI_BASE_URL": COMPAT["endpoint"]}


def test_a_native_profile_keeps_its_model_under_the_migrated_form(monkeypatch):
    """The one known difference: Codex routes carry no model ids, so a native
    profile follows the endpoint but isn't moved onto provider_config's first id."""
    old = from_policy(monkeypatch, provider_config=AZURE)
    new = from_policy(monkeypatch, agent_setup=migrated(AZURE, False), provider_config=AZURE)
    before, after = (managed_models.apply_profile(m, profile("native", model="llama3")) for m in (old, new))
    assert before.base_url == after.base_url == AZURE["endpoint"]
    assert (before.model, after.model) == ("gpt-5", "llama3")


def test_the_enforced_route_is_the_top_env_layer_and_login_help_the_lowest(db, ws, monkeypatch):
    from types import SimpleNamespace

    raw = {"exclusive": False, "claude": {"route": "bedrock", "aws": {"region": "eu-west-1"}}}
    monkeypatch.setattr(agents, "load_profile",
                        lambda name, root=None: SimpleNamespace(env={"AWS_REGION": "us-east-1", "EDITOR": "vi"},
                                                                tool_search=None))
    monkeypatch.setattr(company_login, "stored_env", lambda: {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "eu-west-1"})
    a = Agent("w1", ws.id, "developer", "claude", None, "assign", "idle", "@0", None, time.time())
    db.add_agent(a)
    # without enforce: the stored login env is the lowest layer, the profile's line wins
    monkeypatch.setattr(company_identity, "_setup", lambda: (ORG, team_policy._parse_agent_setup(raw)))
    env = agents.agent_env(ws, "w1", a, None)
    assert env["AWS_REGION"] == "us-east-1" and env["CLAUDE_CODE_USE_BEDROCK"] == "1" and env["EDITOR"] == "vi"
    # with enforce: the route is the top layer, whatever the profile says
    m = setup_managed({**raw, "enforce": True})
    monkeypatch.setattr(company_identity, "_setup", lambda: (ORG, m.setup))
    env = agents.agent_env(ws, "w1", a, m)
    assert env["AWS_REGION"] == "eu-west-1" and env["CLAUDE_CODE_USE_BEDROCK"] == "1" and env["EDITOR"] == "vi"


def test_doctor_shows_a_routed_agent_setup(monkeypatch):
    monkeypatch.setattr(managed_models, "current", lambda root: setup_managed(ROUTED, deny=True))
    (c,) = doctor.managed_checks("/repo")
    assert c.level == doctor.OK and "AWS Bedrock" in c.detail and "us-east-1" in c.detail
    assert "personal API keys denied" in c.detail


# -- doctor -------------------------------------------------------------------------------------


def test_doctor_shows_the_managed_provider(enterprise):
    (c,) = doctor.managed_checks("/repo")
    assert c.level == doctor.OK and c.name == "managed models"
    assert "AWS Bedrock" in c.detail and "us-east-1" in c.detail and "personal API keys denied" in c.detail


def test_doctor_says_nothing_without_managed_models(team, monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: False)
    monkeypatch.setattr(team_policy, "managed_models", lambda root: plugin(team).managed_models())
    assert doctor.managed_checks("/repo") == []


def test_doctor_fails_when_the_policy_cant_be_read(enterprise):
    from brindle.pro import auth

    enterprise.routes[f"GET /orgs/{ORG}/policy"] = [auth.TransportError("down")]
    (c,) = doctor.managed_checks("/repo")
    assert c.level == doctor.FAIL and "never been fetched" in c.detail
