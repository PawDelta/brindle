"""Enterprise managed models: the org policy's ``provider_config`` and
``deny_personal_keys`` on the client."""
import itertools
import json
import time

import pytest

from brindle import agents, company_identity, company_login, doctor, providers, secrets, tmux, workspaces
from brindle.db import Agent
from brindle.pro import license, managed_models, team_policy
from brindle.pro.team_policy import ProviderConfig, parse_policy
from brindle.profiles import Profile

from pro_fixtures import backend, fixed_identity, pro_env, signing_key  # noqa: F401
from test_pro_team import ORG, POLICY, plugin, team, team_claims, login  # noqa: F401 - fixtures

BEDROCK = {"provider": "bedrock", "region": "us-east-1",
           "model_ids": ["anthropic.claude-sonnet-4", "anthropic.claude-haiku-4"], "endpoint": None}
VERTEX = {"provider": "vertex", "region": "europe-west1", "model_ids": ["claude-sonnet-4@2025"],
          "endpoint": "acme-project"}
AZURE ={"provider": "azure", "region": None, "model_ids": ["gpt-5"],
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
    # agent_setup is org-wide, like the rollout fields: the effective policy carries it too
    body["policy"]["agent_setup"] = ROUTED
    assert parse_policy(ORG, body).enforced.agent_setup == parse_policy(ORG, body).agent_setup is not None
    p = parse_policy(ORG, {"version": 1, "policy": {**POLICY, "agent_setup": ROUTED,
                                                    "roles": {"member": {"max_parallel_workers": 1}}}})
    assert team_policy.with_role_overrides(p, "member", None).enforced.agent_setup == p.agent_setup


def test_the_fields_survive_the_cache():
    p = parse_policy(ORG, {"version": 1, "policy": {**POLICY, "provider_config": VERTEX,
                                                    "deny_personal_keys": True}})
    again = parse_policy(ORG, json.loads(json.dumps(p.to_json())))
    assert (again.provider_config, again.deny_personal_keys) == (cfg(VERTEX), True)


@pytest.mark.parametrize("over", [
    {"provider_config": {"provider": "openai"}},
    {"provider_config": {"provider": "bedrock", "region": 3}},
    {"provider_config": {"provider": "bedrock", "model_ids": [""]}},
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


def test_a_license_that_cant_be_checked_means_not_managed(team, monkeypatch):
    def boom(feature):
        raise RuntimeError("no license")

    monkeypatch.setattr(license, "has", boom)
    assert plugin(team).managed_models() is None


# -- the provider -------------------------------------------------------------------------------


def test_azure_and_openai_compatible_env():
    assert managed_models.provider_env(cfg(AZURE), "claude")["CLAUDE_CODE_USE_FOUNDRY"] == "1"
    assert managed_models.provider_env(cfg(AZURE), "codex") == {"OPENAI_BASE_URL": AZURE["endpoint"]}
    assert managed_models.provider_env(cfg(COMPAT), "codex") == {"OPENAI_BASE_URL": COMPAT["endpoint"]}
    assert managed_models.provider_env(cfg(COMPAT), "claude") == {}
    assert managed_models.provider_env(cfg(BEDROCK), "codex") == {}


def test_nothing_changes_without_a_provider_config():
    p = profile("native", base_url="http://localhost:11434/v1", model="x")
    assert managed_models.apply_profile(managed(deny=True), p) is p
    assert managed_models.apply_profile(None, p) is p
    assert managed_models.agent_env(managed(deny=True), "claude") == {}


# -- refusals -----------------------------------------------------------------------------------


@pytest.mark.parametrize("m, p, words", [
    (managed(BEDROCK), profile("codex"), "Codex, which can't use AWS Bedrock"),
    (managed(AZURE), profile("codex", env={"OPENAI_BASE_URL": "https://x"}), "OPENAI_BASE_URL"),
    (managed(BEDROCK), profile("antigravity"), "antigravity, which can't run on AWS Bedrock"),
    (managed(deny=True), profile("native", api_key_env="OPENAI_API_KEY"), "OPENAI_API_KEY"),
])
def test_profiles_pointing_elsewhere_are_refused_with_a_reason(m, p, words):
    why = managed_models.refusal(m, p)
    assert why and words in why and ORG in why


@pytest.mark.parametrize("m, p", [
    (managed(BEDROCK), profile("shell")),
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


# -- the wiring in agents -----------------------------------------------------------------------


def agent(db, ws, provider="claude", profile_name="developer"):
    db.add_agent(Agent("w1", ws.id, profile_name, provider, None, "assign", "idle", "@0", None,
                       time.time()))
    return db.get_agent("w1")


@pytest.fixture
def ws(db, repo):
    return workspaces.adopt_root(db, str(repo))


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
    monkeypatch.setattr(providers, "signed_out", lambda *a, **k: None)
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

GATEWAY_URL = "https://llm.acme.internal/v1"
ROUTED = {"enforce": True, "exclusive": False,
          "claude": {"route": "bedrock", "aws": {"region": "us-east-1"},
                     "model_ids": BEDROCK["model_ids"], "default_model": BEDROCK["model_ids"][0]},
          "codex": {"route": "gateway", "base_url": GATEWAY_URL}}
LOGINS = {"enforce": True, "exclusive": False, "codex": {"route": "chatgpt"},
          "claude": {"route": "subscription", "org_id": "3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c"}}
OLD_SERVER = {"enforce": True, "claude": {"route": "bedrock", "aws": {"region": "eu-west-1"}}}   # no "exclusive"
VERTEX_ONLY = {"enforce": True, "exclusive": True,
               "claude": {"route": "vertex", "gcp": {"project": "acme-project", "region": "europe-west1"}}}
CODEX_ONLY = {"enforce": True, "exclusive": True, "codex": {"route": "azure", "base_url": AZURE["endpoint"]}}
ANTIGRAVITY_ONLY = {"enforce": True, "exclusive": True,
                    "antigravity": {"route": "gateway", "base_url": GATEWAY_URL}}


def from_policy(monkeypatch, entitled=True, **policy):
    """``managed_models.current`` for a policy answer."""
    monkeypatch.setattr(license, "has", lambda feature: entitled and feature == "managed_models")
    monkeypatch.setattr(team_policy, "managed_models",
                        lambda root: parse_policy(ORG, {"version": 1, "policy": {**POLICY, **policy}}))
    return managed_models.current("/repo")


def setup_managed(raw, deny=False):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(license, "has", lambda feature: True)
        return managed_models.Managed(ORG, None, deny, setup=team_policy._parse_agent_setup(raw))


@pytest.mark.parametrize("entitled, policy, source, deny", [
    (True, {"agent_setup": ROUTED, "provider_config": VERTEX}, "setup", False),      # the block beats the mirror
    (True, {"agent_setup": {**ROUTED, "enforce": False}, "provider_config": VERTEX}, "config", False),
    (True, {"agent_setup": {**ROUTED, "enforce": False}}, None, False),              # login help only
    (False, {"agent_setup": ROUTED}, None, False),
    (True, {"agent_setup": LOGINS, "provider_config": BEDROCK}, "config", False),     # nothing routed, not exclusive
    (True, {"agent_setup": {**LOGINS, "exclusive": True}, "provider_config": BEDROCK}, "setup", False),
    (True, {"agent_setup": {**ROUTED, "deny_personal_keys": False}, "deny_personal_keys": True}, "setup", False),
    (True, {"agent_setup": ROUTED, "deny_personal_keys": True}, "setup", True),      # the block doesn't say
    (True, {"agent_setup": {**LOGINS, "deny_personal_keys": True}}, "deny", True),
])
def test_which_policy_fields_are_the_managed_source(monkeypatch, entitled, policy, source, deny):
    m = from_policy(monkeypatch, entitled, **policy)
    got = None if m is None else "setup" if m.setup is not None else "config" if m.config is not None else "deny"
    assert got == source and (m.deny_personal_keys if m else False) is deny


@pytest.mark.variant("the policy shape today's production server holds: an enforced agent_setup next to an "
                     "independently chosen provider_config; a security review of the first cut found this "
                     "client flipping which one wins, so the input is pinned although its arcs are shared")
def test_without_the_exclusive_key_provider_config_stays_the_managed_source(db, ws, monkeypatch):
    """Today's production server can hold an enforced agent_setup next to an
    independently chosen provider_config, and provider_config wins; the
    agent_setup is login help plus the identity check. Unchanged until the
    server migration ships (a phase-1 server always sends ``exclusive``)."""
    m = from_policy(monkeypatch, agent_setup=OLD_SERVER, provider_config=VERTEX, deny_personal_keys=True)
    assert m.setup is None and m.config == cfg(VERTEX) and m.deny_personal_keys
    setup = team_policy._parse_agent_setup(OLD_SERVER)
    assert setup.enforce and not setup.routing
    for p in (profile("codex"), profile(env={"ANTHROPIC_BASE_URL": "https://x"}), profile("native", base_url="http://x")):
        assert managed_models.refusal(m, p) == managed_models.refusal(managed(VERTEX, deny=True), p) is not None
    assert managed_models.apply_profile(m, profile(model="sonnet")).model == VERTEX["model_ids"][0]
    # the pane env is today's layering: provider_config's, then the enforced route on top
    monkeypatch.setattr(company_identity, "_setup", lambda: (ORG, setup))
    monkeypatch.setattr(company_login, "stored_env", lambda: {})
    a = Agent("w1", ws.id, "developer", "claude", None, "assign", "idle", "@0", None, time.time())
    db.add_agent(a)
    env = {k: v for k, v in agents.agent_env(ws, "w1", a, m).items() if not k.startswith(("BRINDLE_", "ENABLE_"))}
    assert env == {**managed_models.provider_env(cfg(VERTEX), "claude"), **company_login.claude_env(ORG, setup.claude)}
    assert from_policy(monkeypatch, agent_setup=OLD_SERVER) is None      # and alone it manages nothing, as today


@pytest.mark.parametrize("raw, p, words", [
    # exclusive: an agent the block gives no route is refused as provider_config refused it
    (VERTEX_ONLY, profile("codex"), "runs Codex, which can't use Google Vertex AI"),
    (VERTEX_ONLY, profile("native", base_url="http://localhost:11434/v1"), "uses its own endpoint, which isn't Google Vertex AI"),
    (VERTEX_ONLY, profile("antigravity"), "uses antigravity, which can't run on Google Vertex AI"),
    (CODEX_ONLY, profile(), "runs Claude Code, which can't use Azure"),
    (ANTIGRAVITY_ONLY, profile(), "runs Claude Code, which can't use a Gemini-compatible gateway"),
    ({"enforce": True, "exclusive": True}, profile(), "can't use the routes"),
    # a route is a route, a login one included; shell and subagents have no model of their own
    (VERTEX_ONLY, profile(), None), (VERTEX_ONLY, profile("shell"), None),
    ({**VERTEX_ONLY, "codex": {"route": "chatgpt"}}, profile("codex"), None),
    ({**VERTEX_ONLY, "antigravity": {"route": "gemini"}}, profile("antigravity"), None),
    ({**VERTEX_ONLY, "antigravity": {"route": "gateway", "base_url": GATEWAY_URL}}, profile("antigravity"), None),
    ({**VERTEX_ONLY, "antigravity": {"route": "gateway", "base_url": GATEWAY_URL}},
     profile("antigravity", env={"GOOGLE_GEMINI_BASE_URL": "https://other"}),
     "pointing it away from a Gemini-compatible gateway"),
    ({**CODEX_ONLY, "claude": LOGINS["claude"]}, profile(), None),
    (CODEX_ONLY, profile("native"), None),
    # not exclusive: an org that set up Company agents directly keeps running Codex
    ({**VERTEX_ONLY, "exclusive": False}, profile("codex", env={"OPENAI_BASE_URL": "https://x"}), None),
    ({**VERTEX_ONLY, "exclusive": False}, profile("antigravity"), None),
    ({**CODEX_ONLY, "exclusive": False}, profile(env={"ANTHROPIC_BASE_URL": "https://proxy"}), None),
])
def test_exclusive_refuses_an_agent_the_routing_leaves_out(raw, p, words):
    why = managed_models.refusal(setup_managed(raw), p)
    assert (why is None) is (words is None) and (words is None or words in why and f"org {ORG} manages" in why)


def test_antigravity_gateway_route_yields_endpoint_and_model_provider_at_launch(
        db, ws, repo, tmp_path, monkeypatch):
    agents_dir = repo / ".brindle" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "dev-agy.md").write_text("---\nname: dev-agy\nprovider: antigravity\n---\n")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}))
    monkeypatch.setenv("BRINDLE_AGY_SETTINGS", str(settings))
    m = setup_managed({"enforce": True, "exclusive": True,
                       "antigravity": {"route": "gateway", "base_url": GATEWAY_URL}})
    monkeypatch.setattr(agents, "managed", lambda root: m)
    seen = {}

    def fake_new_window(session, name, cwd, command, env, tag=None, keep=(), **kw):
        seen.update(env)
        return "@1"

    monkeypatch.setattr(agents.tmux, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(agents.tmux, "new_window", fake_new_window)
    monkeypatch.setattr(agents, "_ensure_sidebar", lambda *a, **k: None)
    monkeypatch.setattr(agents.tmux, "apply_theme", lambda *a, **k: None)
    monkeypatch.setattr(providers, "signed_out", lambda *a, **k: None)
    monkeypatch.setattr(agents, "_detach", lambda *a, **k: None)
    monkeypatch.setattr(providers.Antigravity, "after_launch", lambda *a, **k: True)
    monkeypatch.setattr(providers.Antigravity, "command", lambda self, ctx: ["agy"])

    a = Agent("g1", ws.id, "dev-agy", "antigravity", None, "assign", "idle", "@0", None, time.time())
    db.add_agent(a)
    agents._launch(db, a, ws, prompt=None, resume=None, watch_pane=False, model_checked=True)

    assert seen.get("GOOGLE_GEMINI_BASE_URL") == GATEWAY_URL
    assert json.loads(settings.read_text()) == {"theme": "dark", "modelProvider": "gemini"}
    assert "Antigravity on a Gemini-compatible gateway" in m.describe()


# -- an old provider_config policy and its migrated agent_setup behave the same ------------------

def migrated(pc: dict) -> dict:
    """The ``agent_setup`` the server derives from ``pc`` (always exclusive);
    the server sends ``pc`` next to it as the mirror for older clients."""
    ids = list(pc["model_ids"])
    models = {"model_ids": ids, **({"default_model": ids[0]} if ids else {})}
    if pc["provider"] == "bedrock":
        setup = {"claude": {"route": "bedrock", "aws": {"region": pc["region"]}, **models}}
    elif pc["provider"] == "vertex":
        setup = {"claude": {"route": "vertex", "gcp": {"project": pc["endpoint"], "region": pc["region"]}, **models}}
    elif pc["provider"] == "azure":
        setup = {"claude": {"route": "foundry", "azure": {"base_url": pc["endpoint"]}, **models},
                 "codex": {"route": "azure", "base_url": pc["endpoint"]}}
    else:   # openai-compatible: a Codex route only; Claude Code gets no route and is refused, as today
        setup = {"codex": {"route": "gateway", "base_url": pc["endpoint"]}}
    return {**setup, "enforce": True, "exclusive": True, "deny_personal_keys": True}


PROFILES = [profile(), profile(env={"ANTHROPIC_BASE_URL": "https://proxy"}), profile(env={"ANTHROPIC_API_KEY": "sk"}),
            profile("codex"), profile("codex", env={"OPENAI_BASE_URL": "https://x"}),
            profile("native"), profile("native", base_url="http://localhost:11434/v1"),
            profile("native", api_key_env="OPENAI_API_KEY"), profile("shell"), profile("antigravity")]
_ids = itertools.count()


def pane_env(db, ws, monkeypatch, m, provider):
    """What a pane of ``provider`` gets from ``m`` through ``agents.agent_env``."""
    monkeypatch.setattr(company_identity, "_setup", lambda: (ORG, m.setup) if m.setup is not None else None)
    monkeypatch.setattr(company_login, "stored_env", lambda: {})
    a = Agent(f"w{next(_ids)}", ws.id, "developer", provider, None, "assign", "idle", "@0", None, time.time())
    db.add_agent(a)
    return {k: v for k, v in agents.agent_env(ws, a.id, a, m).items() if not k.startswith(("BRINDLE_", "ENABLE_"))}


@pytest.mark.parametrize("pc", [BEDROCK, VERTEX, AZURE, COMPAT], ids=lambda d: d["provider"])
def test_an_old_policy_and_its_migrated_form_are_equivalent(db, ws, monkeypatch, pc):
    old = from_policy(monkeypatch, provider_config=pc, deny_personal_keys=True)
    new = from_policy(monkeypatch, agent_setup=migrated(pc), provider_config=pc, deny_personal_keys=True)
    assert old.setup is None and new.setup is not None and new.exclusive
    assert old.denied_keys == new.denied_keys == secrets.PERSONAL_KEYS
    for provider in ("claude", "codex", "native"):
        assert pane_env(db, ws, monkeypatch, old, provider) == pane_env(db, ws, monkeypatch, new, provider), provider
    for p in PROFILES:
        assert managed_models.refusal(old, p) == managed_models.refusal(new, p), p
        before, after = managed_models.apply_profile(old, p), managed_models.apply_profile(new, p)
        if p.provider == "claude" and pc["model_ids"]:
            assert before.model == after.model == pc["model_ids"][0]
        if p.provider == "native" and managed_models.refusal(old, p) is None:
            assert before.base_url == after.base_url      # the known difference: native keeps its own model
    assert ("Claude on" in new.describe()) is (new.claude is not None) and "refused" in new.describe()


# -- doctor -------------------------------------------------------------------------------------


def test_doctor_fails_when_the_policy_cant_be_read(enterprise):
    from brindle.pro import auth

    enterprise.routes[f"GET /orgs/{ORG}/policy"] = [auth.TransportError("down")]
    (c,) = doctor.managed_checks("/repo")
    assert c.level == doctor.FAIL and "never been fetched" in c.detail
