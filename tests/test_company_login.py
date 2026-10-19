"""Company login: `brindle login` applies the org policy's agent_setup."""

import dataclasses
import json
import os
import shlex
import sys
import time
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from brindle import agents, cli, cloud_credential, company_login, workspaces
from brindle.db import Agent
from brindle.pro import account as pro_account
from brindle.pro.license import current as REAL_CURRENT
from pro_fixtures import BASE, backend, claims, pro_env, sign, signing_key  # noqa: F401 - fixtures
from brindle.pro.team_policy import (AgentSetup, AwsFederation, AwsSetup, AzureFederation, AzureSetup,
                                     ClaudeSetup, CodexSetup, GatewaySetup, GcpFederation, GcpSetup, ModelsSetup)

ORG = "acme"
UUID = "3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c"
AWS = AwsSetup("us-west-2", "https://acme.awsapps.com/start", "us-east-1", "123456789012", "BrindleWorker")
OWN_AWS = AwsSetup("us-west-2")          # no SSO group: the machine's own AWS sign-in
GCP = GcpSetup("acme-brindle-prod", "us-east5")
AZURE = AzureSetup(UUID, "acme-foundry")
FOUNDRY_URL = AzureSetup(base_url="https://acme.services.ai.azure.com/anthropic")
MODELS = ModelsSetup("claude-opus-x", "claude-sonnet-x", "claude-haiku-x")
IDS = ("us.anthropic.claude-opus-4-5-v1:0", "us.anthropic.claude-sonnet-4-5-v1:0", "us.anthropic.claude-haiku-4-5-v1:0")


class Fake:
    """Replaces company_login.run_cmd/which: ``rc`` maps an argv prefix to
    (returncode, output); anything unlisted succeeds. Every call is recorded."""

    def __init__(self, monkeypatch, rc=None, missing=()):
        self.calls, self.rc, self.missing = [], rc or {}, set(missing)
        monkeypatch.setattr(company_login, "run_cmd", self.run)
        monkeypatch.setattr(company_login, "which",
                            lambda n: None if n in self.missing else f"/bin/{n}")

    def run(self, argv, timeout=30.0, interactive=False, cwd=None, env=None, stdin=None):
        self.calls.append((tuple(argv), interactive, timeout))
        self.cwds = getattr(self, "cwds", []) + [cwd]
        for prefix, result in self.rc.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return result
        return 0, ""

    def ran(self, *prefix):
        return any(c[0][:len(prefix)] == prefix for c in self.calls)


def go(setup, say=None, org=ORG):
    out = []
    ok = company_login.apply(org, setup, say or out.append)
    return ok, out


def claude(route, **kw):
    return AgentSetup(claude=ClaudeSetup(route=route, **kw))


@pytest.mark.parametrize("model, ids, expected", [
    ("claude-sonnet-4-6", (IDS[-1],), IDS[-1]),
    pytest.param("claude-haiku-5-5", (IDS[-1],), IDS[-1],
                 marks=pytest.mark.variant("Haiku profile from the live Bedrock failure")),
    (IDS[0], IDS, IDS[0]),
    (None, ("claude-sonnet-4-6", IDS[-1]), IDS[-1]),
    pytest.param("claude-sonnet-4-6", IDS, IDS[-1],
                 marks=pytest.mark.variant("route default differs from its first served id")),
])
def test_route_model_is_used_by_launch_and_policy(repo, monkeypatch, model, ids, expected):
    from brindle import policy, providers
    from brindle.config import RepoConfig
    from brindle.pro.team_policy import OrgPolicy, ProPolicy
    from brindle.profiles import Profile

    setup = claude("bedrock", aws=OWN_AWS, model_ids=ids, default_model=IDS[-1])
    monkeypatch.setattr(company_login, "org_setup", lambda *a, **k: (ORG, setup))
    company_login.store_env(ORG, company_login.claude_env(ORG, setup.claude))
    monkeypatch.setattr(providers, "claude_org_managed", lambda: False)
    p = Profile("routed", "", "claude", "", model=model)
    monkeypatch.setattr("brindle.profiles.load_profile", lambda *a: p)
    org = OrgPolicy(org_id=ORG, version=1, allowed_models=(expected,))
    plugin = ProPolicy(str(repo))
    monkeypatch.setattr(plugin, "policy", lambda: org)
    monkeypatch.setattr(policy, "plugin", lambda *a: plugin)
    argv = providers.ClaudeCode().command(providers.LaunchContext("a1", p, None, cwd=str(repo)))
    decision = policy.check_assign(RepoConfig(), str(repo), p.name, "task", "assign")
    assert (argv[argv.index("--model") + 1], policy._profile_fields(p.name, str(repo)),
            decision.allowed) == (expected, ("claude", expected), True)


def test_route_model_only_when_the_route_env_reaches_the_launch(monkeypatch):
    from brindle import providers

    setup = claude("bedrock", aws=OWN_AWS, model_ids=(IDS[-1],), default_model=IDS[-1])
    monkeypatch.setattr(company_login, "org_setup", lambda *a, **k: (ORG, setup))
    monkeypatch.setattr(providers, "claude_org_managed", lambda: False)
    off = [company_login.claude_model(m) for m in (None, "sonnet")]
    company_login.store_env(ORG, company_login.claude_env(ORG, setup.claude))
    assert (off, company_login.claude_model("sonnet")) == ([None, "sonnet"], IDS[-1])


def test_a_pinned_alias_wins_over_the_route_default(repo, monkeypatch):
    from brindle import policy, providers
    from brindle.config import RepoConfig
    from brindle.pro import managed_models
    from brindle.pro.team_policy import OrgPolicy, ProPolicy
    from brindle.profiles import Profile

    setup = AgentSetup(claude=ClaudeSetup(route="bedrock", aws=OWN_AWS, models=ModelsSetup(opus=IDS[0]),
                                          model_ids=(IDS[0], IDS[-1]), default_model=IDS[-1]), enforce=True)
    monkeypatch.setattr(company_login, "org_setup", lambda *a, **k: (ORG, setup))
    monkeypatch.setattr(providers, "claude_org_managed", lambda: False)
    p = Profile("developer-opus", "", "claude", "", model="opus")
    monkeypatch.setattr("brindle.profiles.load_profile", lambda *a: p)
    plugin = ProPolicy(str(repo))
    org = {"setup": setup}
    monkeypatch.setattr(plugin, "policy", lambda: OrgPolicy(org_id=ORG, version=1, allowed_models=(IDS[0],),
                                                             agent_setup=org["setup"]))
    monkeypatch.setattr(policy, "plugin", lambda *a: plugin)
    m = managed_models.Managed(ORG, None, False, setup=setup)
    decision = policy.check_assign(RepoConfig(), str(repo), p.name, "task", "assign")
    org["setup"] = dataclasses.replace(setup, enforce=False)     # the pane doesn't get this route
    unused = policy.check_assign(RepoConfig(), str(repo), p.name, "task", "assign")
    assert (company_login.claude_model("opus"), company_login.claude_model("sonnet"),
            managed_models.apply_profile(m, p).model, decision.allowed, unused.allowed) == \
        ("opus", IDS[-1], "opus", True, False)


class _Ent:
    def __init__(self, org_id):
        self.org_id = org_id


@pytest.fixture(autouse=True)
def current_org(monkeypatch):
    """The locally cached entitlement's org (tests change ``.org``)."""
    from brindle.pro import license

    state = type("S", (), {"org": ORG})()

    def current(**kw):
        assert kw.get("refresh") is False        # never the network
        return _Ent(state.org)
    monkeypatch.setattr(license, "current", current)
    return state


def test_switching_orgs_stops_applying_the_stored_env(db, repo, current_org, monkeypatch, caplog):
    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    ws = workspaces.create(db, str(repo), "feat").workspace
    assert agents.agent_env(ws, "a1", _claude_agent(ws))["AWS_PROFILE"] == "brindle-acme"
    current_org.org = "other-org"
    with caplog.at_level("WARNING", logger="brindle.company_login"):
        env = agents.agent_env(ws, "a1", _claude_agent(ws))
    assert "AWS_PROFILE" not in env and "CLAUDE_CODE_USE_BEDROCK" not in env
    assert [r.getMessage() for r in caplog.records if "brindle login" in r.getMessage()]
    current_org.org = ORG               # back to the org: applied again
    assert agents.agent_env(ws, "a1", _claude_agent(ws))["AWS_PROFILE"] == "brindle-acme"


def test_no_cached_entitlement_applies_no_stored_env(monkeypatch):
    from brindle.pro import license

    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    monkeypatch.setattr(license, "current", lambda **k: (_ for _ in ()).throw(license.LicenseError("not logged in")))
    assert company_login.stored_env() == {}


@pytest.fixture(autouse=True)
def aws_config(tmp_path, monkeypatch):
    path = tmp_path / "aws" / "config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
    return path


# -- each route: signed in -> no login, not signed in -> login ------------------------------

ROUTES = {
    "foundry": (claude("foundry", azure=AZURE), ("az", "account", "show"), ("az", "login")),
    "subscription": (claude("subscription", org_id=UUID), ("claude", "auth", "status"),
                     ("claude", "auth", "login", "--sso")),
    "console": (claude("console", org_id=UUID), ("claude", "auth", "status"),
                ("claude", "auth", "login", "--console")),
    "chatgpt": (AgentSetup(codex=CodexSetup("chatgpt")), ("codex", "login", "status"), ("codex", "login")),
}


def test_signed_in_runs_no_login(monkeypatch):
    setup, check, login = ROUTES["subscription"]
    f = Fake(monkeypatch, {check: (0, f"logged in to {UUID}")})
    ok, out = go(setup)
    assert ok
    assert f.ran(*check) and login not in [c[0] for c in f.calls]
    assert any("already signed in" in line for line in out)


@pytest.mark.parametrize("route", ["foundry", "console", "chatgpt"])
def test_not_signed_in_runs_the_login(route, monkeypatch):
    setup, check, login = ROUTES[route]
    f = Fake(monkeypatch, {check: (1, "not logged in")})
    ok, _ = go(setup)
    assert ok
    assert login in [c[0] for c in f.calls]
    assert [c for c in f.calls if c[0] == login][0][1] is True   # interactive: keeps the terminal
    assert all(isinstance(c[2], float) for c in f.calls)           # every command has a timeout


def test_gateway_signs_in_nowhere(monkeypatch):
    f = Fake(monkeypatch)
    ok, out = go(claude("gateway", gateway=GatewaySetup("https://gw.example.com/v1")))
    assert ok and not f.calls
    assert out == ["Ready: Claude on acme's gateway"]


def test_codex_api_key_and_azure(monkeypatch):
    f = Fake(monkeypatch)
    _, out = go(AgentSetup(codex=CodexSetup("api_key")))
    assert any("codex login --with-api-key" in line for line in out) and not f.calls
    _, out = go(AgentSetup(codex=CodexSetup("azure")))
    assert not f.calls


# -- the AWS profile --------------------------------------------------------------------------

def test_aws_profile_is_written_and_other_profiles_kept(aws_config, monkeypatch):
    aws_config.parent.mkdir(parents=True)
    aws_config.write_text("# mine\n[default]\nregion = eu-west-1\n\n"
                          "[profile brindle-acme]\nsso_start_url = https://old\nregion = old\n\n"
                          "[profile other]\nrole_arn = arn:x\n")
    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    text = aws_config.read_text()
    assert "# mine\n[default]\nregion = eu-west-1\n" in text
    assert "[profile other]\nrole_arn = arn:x\n" in text
    assert "old" not in text and text.count("[profile brindle-acme]") == 1
    for line in ("sso_start_url = https://acme.awsapps.com/start", "sso_region = us-east-1",
                 "sso_account_id = 123456789012", "sso_role_name = BrindleWorker", "region = us-west-2"):
        assert line in text
    assert not [p for p in aws_config.parent.iterdir() if p.name != "config"]   # no temp file left


# -- the env for panes -------------------------------------------------------------------------

def _claude_agent(ws, provider="claude"):
    return Agent("a1", ws.id, "developer", provider, None, "assign", "starting", "", None, 0.0)


def test_env_is_stored_and_reaches_a_claude_pane(db, repo, monkeypatch):
    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS, models=MODELS))
    stored = json.loads(company_login.config.user_config_path().read_text())[company_login.ENV_KEY]
    assert stored["env"] == {
        "CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "brindle-acme", "AWS_REGION": "us-west-2",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-x", "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-x",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-x"}
    ws = workspaces.create(db, str(repo), "feat").workspace
    env = agents.agent_env(ws, "a1", _claude_agent(ws))
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1" and env["AWS_PROFILE"] == "brindle-acme"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-x"
    other = agents.agent_env(ws, "a1", _claude_agent(ws, "codex"))
    assert "AWS_PROFILE" not in other


@pytest.mark.parametrize("setup,expected", [
    (claude("vertex", gcp=GCP), {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "acme-brindle-prod",
                                 "CLOUD_ML_REGION": "us-east5"}),
    (claude("foundry", azure=AZURE), {"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_RESOURCE": "acme-foundry"}),
    (claude("gateway", gateway=GatewaySetup("https://gw.example.com/v1")),
     {"ANTHROPIC_BASE_URL": "https://gw.example.com/v1"}),
    (claude("subscription", org_id=UUID), {}),
    # no SSO group: no AWS_PROFILE; default_model is ANTHROPIC_MODEL; with several ids and no
    # aliases the last stands in for Haiku, as provider_config had it
    (claude("bedrock", aws=OWN_AWS, model_ids=IDS, default_model=IDS[1]),
     {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-west-2", "ANTHROPIC_MODEL": IDS[1],
      "ANTHROPIC_DEFAULT_HAIKU_MODEL": IDS[2]}),
    (claude("vertex", gcp=GCP, model_ids=IDS, models=ModelsSetup(opus="claude-opus-x")),   # aliases say what Haiku is
     {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "acme-brindle-prod", "CLOUD_ML_REGION": "us-east5",
      "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-x"}),
    (claude("foundry", azure=AzureSetup(UUID, "acme-foundry", FOUNDRY_URL.base_url)),
     {"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_RESOURCE": "acme-foundry",
      "ANTHROPIC_FOUNDRY_BASE_URL": FOUNDRY_URL.base_url}),
    (claude("console", org_id=UUID, default_model="claude-sonnet-4-5"), {"ANTHROPIC_MODEL": "claude-sonnet-4-5"}),
])
def test_env_per_route(setup, expected):
    assert company_login.claude_env(ORG, setup.claude) == expected


# -- routes that leave the cloud sign-in to the person --------------------------------------------

def test_a_route_that_leaves_the_cloud_sign_in_to_the_person(aws_config, monkeypatch):
    f = Fake(monkeypatch)
    ok, out = go(claude("bedrock", aws=OWN_AWS, models=MODELS, model_ids=IDS, default_model=IDS[1]))
    assert ok and out[-1] == "Ready: Claude on acme's bedrock (us-west-2)"
    assert f.ran("aws", "sts", "get-caller-identity", "--region", "us-west-2")
    assert not f.ran("aws", "sso", "login") and not aws_config.exists()        # no profile to write or log in to
    stored = company_login.stored_env()
    assert stored["AWS_REGION"] == "us-west-2" and stored["ANTHROPIC_MODEL"] == IDS[1] and "AWS_PROFILE" not in stored
    probes = [c[0][3] for c in f.calls if c[0][:2] == ("/bin/claude", "-p")]
    assert probes == ["claude-opus-x", "claude-sonnet-x", "claude-haiku-x", IDS[1]]   # the default model too
    f = Fake(monkeypatch, {("aws", "sts"): (255, "Unable to locate credentials")})
    ok, out = go(claude("bedrock", aws=OWN_AWS))
    assert not ok and not any(line.startswith("Ready") for line in out)
    assert not f.ran("aws", "sso", "login") and not any(c[1] for c in f.calls)   # nothing interactive: it's theirs to do
    assert any("brindle login" in line for line in out) and company_login.stored_env() == {}
    # Foundry by its endpoint, with no subscription named: az signs in, nothing picks a subscription
    f = Fake(monkeypatch, {("az", "account", "show"): (1, "")})
    ok, out = go(claude("foundry", azure=FOUNDRY_URL))
    assert ok and f.ran("az", "login") and not f.ran("az", "account", "set")
    assert out[-1] == f"Ready: Claude on acme's foundry ({FOUNDRY_URL.base_url})"
    go(claude("foundry", azure=AzureSetup(UUID, None, FOUNDRY_URL.base_url)))
    assert f.ran("az", "account", "set", "--subscription", UUID)


def test_claude_managed_settings_take_precedence(db, repo, tmp_path, monkeypatch):
    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    managed = tmp_path / "managed.json"
    managed.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://corp.example.com"}}))
    monkeypatch.setenv("BRINDLE_CLAUDE_MANAGED_SETTINGS", str(managed))
    ws = workspaces.create(db, str(repo), "feat").workspace
    env = agents.agent_env(ws, "a1", _claude_agent(ws))
    assert "CLAUDE_CODE_USE_BEDROCK" not in env and "AWS_PROFILE" not in env


# -- a missing CLI -------------------------------------------------------------------------------

@pytest.mark.parametrize("setup,tool", [
    (claude("bedrock", aws=AWS), "aws"), (claude("vertex", gcp=GCP), "gcloud"),
    (claude("foundry", azure=AZURE), "az"), (claude("subscription", org_id=UUID), "claude"),
    (AgentSetup(codex=CodexSetup("chatgpt")), "codex")])
def test_missing_cli_prints_the_install_command_and_stops(setup, tool, monkeypatch):
    f = Fake(monkeypatch, missing={tool})
    ok, out = go(setup)
    assert not ok and not f.calls
    assert any(line.startswith(f"{tool} isn't installed") and company_login.INSTALL[tool] in line
               for line in out)
    assert not any(line.startswith("Ready") for line in out)


# -- the model check -----------------------------------------------------------------------------------


def test_refused_model_prints_the_exact_fix(monkeypatch):
    from brindle import model_access

    Fake(monkeypatch, {("/bin/claude", "-p", "--model", "claude-opus-x"):
                       (1, "The model is not available for this account")})
    ok, out = go(claude("bedrock", aws=AWS, models=MODELS))
    assert not ok
    assert f"Claude model claude-opus-x: {model_access.BEDROCK_QUOTA.line()}" in out
    assert not any(line.startswith("Ready") for line in out)


def test_unclassified_model_error_is_shown(monkeypatch):
    Fake(monkeypatch, {("/bin/claude", "-p"): (1, "boom")})
    ok, out = go(claude("vertex", gcp=GCP, models=ModelsSetup(opus="claude-opus-x")))
    assert not ok and "Claude model claude-opus-x: boom" in out


def test_stale_env_is_cleared_when_the_org_has_no_setup_or_on_logout(db, repo, monkeypatch):
    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    monkeypatch.setattr(company_login, "org_setup", lambda *a, **k: None)
    company_login.apply_current(print)
    assert company_login.stored_env() == {}
    go(claude("bedrock", aws=AWS))
    assert company_login.stored_env()
    pro_account.ProAccount(store=type("S", (), {"load": lambda s: None, "clear": lambda s: None,
                                                "delete": lambda s: None})()).cmd_logout(None)
    assert company_login.stored_env() == {}


def test_clear_federation_on_removed_setup(current_org, monkeypatch, tmp_path):
    from brindle.pro import auth

    Fake(monkeypatch)
    d = cloud_credential.org_dir(ORG, create=True)
    (d / cloud_credential.AZURE_TOKEN).write_text("tok")
    monkeypatch.setattr(company_login, "org_setup", lambda *a, **k: None)
    company_login.apply_current(print)
    assert not d.exists()

    # Signed in to the personal org (no setup) after a team org asked for re-authentication:
    # the team org's files stay, an org the person has left goes.
    team, gone = cloud_credential.org_dir(ORG, create=True), cloud_credential.org_dir("left", create=True)
    (team / cloud_credential.AZURE_TOKEN).write_text("tok")
    aws = tmp_path / "aws-config"
    company_login.write_aws_profile(aws, company_login.profile_name(ORG), FED_AWS)
    monkeypatch.setattr(company_login, "aws_config_path", lambda: aws)
    current_org.org = "me-personal"
    monkeypatch.setattr(auth, "member_orgs", lambda *a: {ORG, "me-personal"})
    company_login.apply_current(print)
    assert team.exists() and company_login.profile_name(ORG) in aws.read_text()
    assert not gone.exists()




def test_azure_refresh_at_launch(db, repo, monkeypatch):
    token_file = str(cloud_credential.azure_token_path(ORG))
    company_login.config.set_user(company_login.ENV_KEY, {"org": ORG, "env": {
        "AZURE_FEDERATED_TOKEN_FILE": token_file}})
    freshened = []
    monkeypatch.setattr(cloud_credential, "freshen_azure", freshened.append)
    ws = workspaces.create(db, str(repo), "feat").workspace
    agents.agent_env(ws, "a1", _claude_agent(ws))
    agents.agent_env(ws, "a1", _claude_agent(ws, "codex"))      # only Claude Code reads the file
    assert freshened == [token_file]


def test_failed_sign_in_leaves_no_env(monkeypatch):
    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    assert company_login.stored_env()
    Fake(monkeypatch, {("az", "account", "show"): (1, ""), ("az", "login"): (1, "")})
    go(claude("foundry", azure=AZURE))
    assert company_login.stored_env() == {}


def test_only_known_names_reach_panes(monkeypatch):
    company_login.config.set_user(company_login.ENV_KEY, {"org": ORG, "env": {
        "PATH": "/evil", "LD_PRELOAD": "x", "AWS_PROFILE": "brindle-acme"}})
    assert company_login.stored_env() == {"AWS_PROFILE": "brindle-acme"}


def test_a_failing_policy_keeps_the_stored_env(monkeypatch):
    from brindle.pro import license, team_policy

    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    stored = company_login.stored_env()
    assert stored
    ent = type("E", (), {"org_id": ORG, "features": frozenset({"team"})})()
    monkeypatch.setattr(license, "current", lambda **k: ent)

    def down(*a, **k):
        raise team_policy.PolicyUnavailable("offline")
    monkeypatch.setattr(team_policy, "current_policy", down)
    assert company_login.apply_current(print) is None
    assert company_login.stored_env() == stored
    # The policy was had and has no agent_setup: now it is cleared.
    monkeypatch.setattr(team_policy, "current_policy",
                        lambda *a, **k: type("P", (), {"agent_setup": None})())
    assert company_login.apply_current(print) is None
    assert company_login.stored_env() == {}
    # An agent_setup that no longer has a Claude route (the owner removed it) clears it too.
    go(claude("bedrock", aws=AWS))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")      # `brindle doctor` from a session
    monkeypatch.setattr(team_policy, "current_policy", lambda *a, **k: type(
        "P", (), {"agent_setup": AgentSetup(codex=CodexSetup(route="api_key"))})())
    company_login.apply_current(print)
    assert company_login.stored_env() == {}


def test_the_sweep_refetches_the_entitlement_and_drops_a_removed_claude_route(backend, signing_key, monkeypatch):
    # Live incident: the org owner removed the Claude route, but only an
    # entitlement near expiry was refetched, so its old policy version kept
    # the route, and its env kept reaching Claude panes, even after a
    # `brindle doctor` from a session (where settings writes are refused).
    from brindle.pro import auth, credentials, license, team_policy

    monkeypatch.setattr(license, "current", REAL_CURRENT)
    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    now = time.time()
    store = credentials.default_store()
    store.save({**backend.issue(), "access_expires_at": now + 900, "base_url": BASE,
                "entitlement": sign(signing_key, claims(org_id=ORG, features=["team"], policy_version=1,
                                                        iat=int(now - 1200), exp=int(now + 3 * 3600)))})
    backend.routes["GET /entitlement"] = [(200, {"entitlement": sign(signing_key, claims(
        org_id=ORG, features=["team"], policy_version=2))})]
    monkeypatch.setattr(team_policy, "current_policy", lambda ent, *a, **k: SimpleNamespace(
        agent_setup=claude("bedrock", aws=AWS) if ent.policy_version < 2 else AgentSetup()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    assert company_login.stored_env()
    assert company_login.sync(store=store, client=auth.Client(BASE, backend))
    assert company_login.stored_env() == {}
    assert company_login.ENV_KEY not in company_login.config.user_settings()


def test_pinned_models_without_the_claude_binary_are_not_ready(monkeypatch):
    f = Fake(monkeypatch, missing={"claude"})
    ok, out = go(claude("bedrock", aws=AWS, models=MODELS))
    assert not ok
    assert any(line.startswith("claude isn't installed; couldn't check model access") for line in out)
    assert not any(line.startswith("Ready") for line in out)
    assert not f.ran("/bin/claude")


def test_doctor_fix_applies_company_login(monkeypatch):
    from brindle import doctor

    monkeypatch.setattr(company_login, "apply_current",
                        lambda say, *a, **k: say("Ready: Claude on acme's gateway"))
    assert "Ready: Claude on acme's gateway" in doctor.fix(lambda p: False)


# -- brindle login == brindle account login -------------------------------------------------------------

def test_brindle_login_is_brindle_account_login(monkeypatch):
    from brindle import account as account_mod

    seen = []
    monkeypatch.setattr(account_mod, "run", lambda cfg, root, args, echo=print: seen.append(args) or 0)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["login"]).exit_code == 0
    assert runner.invoke(cli.app, ["account", "login"]).exit_code == 0
    assert runner.invoke(cli.app, ["login", "--device"]).exit_code == 0
    assert runner.invoke(cli.app, ["account", "login", "--device"]).exit_code == 0
    assert seen == [["login"], ["login"], ["login", "--device"], ["login", "--device"]]


def test_run_cmd_never_hands_a_probe_the_terminal():
    # A probe that inherits the tty (claude exits by resetting its modes) shifts the sidebar.
    probe = "import os; s = os.fstat(0); print(s.st_rdev == os.stat(os.devnull).st_rdev)"
    read, write = os.pipe()
    saved = os.dup(0)
    os.dup2(read, 0)  # a stdin the child would otherwise inherit
    try:
        assert company_login.run_cmd([sys.executable, "-c", probe]) == (0, "True\n")
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read, write):
            os.close(fd)


# -- federated orgs: the cloud's own hook, no cloud CLI, no cloud login --------------------------------

FED_AWS = AwsSetup(region="us-west-2", account_id="123456789012", role_name="brindle-members-acme",
                   federation=AwsFederation("arn:aws:iam::123456789012:role/brindle-members-acme"))
FED_GCP = GcpSetup("acme-brindle-prod", "us-east5", GcpFederation(
    "123456789", "brindle-members", "pawdelta", "bm@acme-brindle-prod.iam.gserviceaccount.com"))
FED_AZURE = AzureSetup(resource="acme-foundry",
                       federation=AzureFederation(UUID, "7c9e6679-7425-40de-944b-e07fc1f90ae7"))
FEDERATED = {"bedrock": ("aws", FED_AWS), "vertex": ("gcp", FED_GCP), "foundry": ("azure", FED_AZURE)}


@pytest.fixture
def hook(tmp_path, monkeypatch):
    """A brindle script to name in the hooks, and the exchange login proves them with."""
    d = tmp_path / "bin"
    d.mkdir()
    exe = d / "brindle"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(exe)])
    checked = []
    monkeypatch.setattr(cloud_credential, "check", lambda cloud, org, **kw: checked.append(cloud))
    return exe, checked


def federated(route):
    cloud, block = FEDERATED[route]
    return claude(route, auth="federated", **{cloud: block})     # the cloud's name is its block's


@pytest.mark.parametrize("route,expected", [
    ("bedrock", {"AWS_PROFILE": "brindle-acme"}),
    ("vertex", {"GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES": "1"}),
    ("foundry", {"AZURE_CLIENT_ID": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "AZURE_TENANT_ID": UUID}),
])
def test_federated_routes_set_their_hook_and_run_no_cloud_cli(route, expected, aws_config, hook, monkeypatch):
    exe, checked = hook
    f = Fake(monkeypatch, missing={"aws", "gcloud", "az"})
    ok, _ = go(federated(route))
    assert ok and checked == [FEDERATED[route][0]] and not f.calls
    d = cloud_credential.org_dir(ORG)
    if route == "bedrock":
        text = aws_config.read_text()
        assert "sso_" not in text and "region = us-west-2" in text
        line = next(ln for ln in text.splitlines() if ln.startswith("credential_process = "))
        argv = shlex.split(line.removeprefix("credential_process = "))
        assert argv == [str(exe), "cloud-credential", "aws", "--org", ORG]
        assert os.access(argv[0], os.X_OK)                   # names an executable that exists
    else:
        expected["GOOGLE_APPLICATION_CREDENTIALS" if route == "vertex" else "AZURE_FEDERATED_TOKEN_FILE"] = \
            str(d / ("gcp-credentials.json" if route == "vertex" else "azure-token"))
    stored = company_login.stored_env()
    assert expected.items() <= stored.items()                # ENV_NAMES admits them
    if route == "vertex":
        source = json.loads((d / "gcp-credentials.json").read_text())["credential_source"]["executable"]
        assert shlex.split(source["command"]) == [str(exe), "cloud-credential", "gcp", "--org", ORG]
        assert source["output_file"] == str(d / "gcp-subject-token.json")


def test_a_failed_exchange_is_not_ready_and_keeps_no_env(hook, monkeypatch):
    Fake(monkeypatch)
    monkeypatch.setattr(cloud_credential, "check", lambda *a, **k: "AWS refused the role (AccessDenied)")
    ok, out = go(federated("bedrock"))
    assert not ok and any("AccessDenied" in line for line in out)
    assert company_login.stored_env() == {}


def test_logout_and_a_signed_out_doctor_fix_remove_the_federation_directory(aws_config, hook, monkeypatch, tmp_path):
    from brindle import doctor
    from brindle.pro import credentials, license

    Fake(monkeypatch)
    aws_config.parent.mkdir(parents=True, exist_ok=True)
    aws_config.write_text("[profile other]\nregion = x\n")

    def sign_in():
        go(federated("bedrock"))
        go(federated("vertex"))
        assert cloud_credential.org_ids() == [ORG] and "brindle-acme" in aws_config.read_text()

    sign_in()
    assert pro_account.ProAccount(store=credentials.FileStore(tmp_path / "pro")).run(["logout"]) == 0
    assert cloud_credential.org_ids() == [] and aws_config.read_text() == "[profile other]\nregion = x\n"
    sign_in()
    monkeypatch.setattr(license, "current", lambda **k: (_ for _ in ()).throw(license.LicenseError("signed out")))
    doctor.fix(lambda p: False)
    assert cloud_credential.org_ids() == [] and "brindle-acme" not in aws_config.read_text()


@pytest.mark.parametrize("tool, platform, wanted", [
    ("codex", "linux", "EACCES"),
    ("az", "linux", "InstallAzureCLIDeb"),
    ("az", "darwin", None),
])
def test_install_hint_gives_linux_alternatives_only_on_linux(monkeypatch, tool, platform, wanted):
    monkeypatch.setattr(sys, "platform", platform)
    hint = company_login.install_hint(tool)
    assert company_login.INSTALL[tool] in hint
    assert (wanted in hint) if wanted else hint == company_login.INSTALL[tool]


def test_wsl_browser_interactive_login_gets_its_env(monkeypatch):
    seen = {}
    monkeypatch.setattr(company_login.subprocess, "run",
                        lambda argv, **kw: seen.update(kw) or type("P", (), {"returncode": 0})())
    assert company_login.run_cmd(["codex", "login"], interactive=True, env={"BROWSER": "wslview"}) == (0, "")
    assert seen["env"] == {"BROWSER": "wslview"}


# -- agents launched while signed out of brindle --------------------------------------------------


def test_signing_in_adopts_the_agents_launched_while_signed_out(monkeypatch):
    from brindle import identity_lock, signin_pause

    now = {"ident": "claude org Me (org-9)", "in": False}
    monkeypatch.setattr(signin_pause, "check", lambda provider, cache=False: (
        signin_pause.Check("ok", ident=now["ident"]), False))
    monkeypatch.setattr(company_login, "_logged_in", lambda store=None, client=None: now["in"])
    signin_pause.record_launch(Agent("c1", "ws", "developer", "claude", "chat", "assign", "processing",
                                     "", None, 0.0))
    now["in"] = True                   # `brindle login`: Claude moves to the org's route
    stored = []
    monkeypatch.setattr(company_login, "store_env", lambda *a: (
        stored.append(a), now.update(ident="claude org Acme (org-1)")))
    setup, check, _ = ROUTES["subscription"]
    Fake(monkeypatch, {check: (0, f"logged in to {UUID}")})
    assert go(setup)[0] and stored
    assert signin_pause.recorded_identity("c1") == "claude org Acme (org-1)"
    assert identity_lock.locked_by("c1") is None
    now["ident"] = "claude org Other (org-2)"     # a different account signs in: locked
    assert identity_lock.locked_by("c1") == "claude org Acme (org-1)"
    # A failing check never fails the sign-in over adopting.
    monkeypatch.setattr(signin_pause, "check", lambda provider, cache=False: 1 / 0)
    assert go(setup)[0]
