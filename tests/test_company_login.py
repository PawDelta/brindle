"""Company login: `brindle login` applies the org policy's agent_setup."""

import json

import pytest
from typer.testing import CliRunner

from brindle import agents, cli, company_login, workspaces
from brindle.db import Agent
from brindle.pro import account as pro_account
from brindle.pro.team_policy import (AgentSetup, AwsSetup, AzureSetup, ClaudeSetup, CodexSetup,
                                     GatewaySetup, GcpSetup, ModelsSetup)

ORG = "acme"
UUID = "3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c"
AWS = AwsSetup("https://acme.awsapps.com/start", "us-east-1", "123456789012", "BrindleWorker", "us-west-2")
GCP = GcpSetup("acme-brindle-prod", "us-east5")
AZURE = AzureSetup(UUID, "acme-foundry")
MODELS = ModelsSetup("claude-opus-x", "claude-sonnet-x", "claude-haiku-x")


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
    "bedrock": (claude("bedrock", aws=AWS), ("aws", "sts", "get-caller-identity"),
                ("aws", "sso", "login", "--profile", "brindle-acme")),
    "vertex": (claude("vertex", gcp=GCP), ("gcloud", "auth", "application-default", "print-access-token"),
               ("gcloud", "auth", "application-default", "login")),
    "foundry": (claude("foundry", azure=AZURE), ("az", "account", "show"), ("az", "login")),
    "subscription": (claude("subscription", org_id=UUID), ("claude", "auth", "status"),
                     ("claude", "auth", "login", "--sso")),
    "console": (claude("console", org_id=UUID), ("claude", "auth", "status"),
                ("claude", "auth", "login", "--console")),
    "chatgpt": (AgentSetup(codex=CodexSetup("chatgpt")), ("codex", "login", "status"), ("codex", "login")),
}


@pytest.mark.parametrize("route", ROUTES)
def test_signed_in_runs_no_login(route, monkeypatch):
    setup, check, login = ROUTES[route]
    f = Fake(monkeypatch, {check: (0, f"logged in to {UUID}")})
    ok, out = go(setup)
    assert ok
    assert f.ran(*check) and login not in [c[0] for c in f.calls]
    assert any("already signed in" in line for line in out)


@pytest.mark.parametrize("route", ROUTES)
def test_not_signed_in_runs_the_login(route, monkeypatch):
    setup, check, login = ROUTES[route]
    f = Fake(monkeypatch, {check: (1, "not logged in")})
    ok, _ = go(setup)
    assert ok
    assert login in [c[0] for c in f.calls]
    assert [c for c in f.calls if c[0] == login][0][1] is True   # interactive: keeps the terminal
    assert all(isinstance(c[2], float) for c in f.calls)           # every command has a timeout


def test_subscription_signed_in_to_another_org_logs_in(monkeypatch):
    f = Fake(monkeypatch, {("claude", "auth", "status"): (0, "org: 00000000-0000-0000-0000-000000000000")})
    go(claude("subscription", org_id=UUID))
    assert f.ran("claude", "auth", "login", "--sso")


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


def test_vertex_and_foundry_set_the_project(monkeypatch):
    f = Fake(monkeypatch)
    go(claude("vertex", gcp=GCP))
    assert f.ran("gcloud", "config", "set", "project", "acme-brindle-prod")
    go(claude("foundry", azure=AZURE))
    assert f.ran("az", "account", "set", "--subscription", UUID)


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


def test_aws_profile_created_when_there_is_no_config(aws_config, monkeypatch):
    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    assert aws_config.read_text().startswith("[profile brindle-acme]\n")


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
])
def test_env_per_route(setup, expected):
    assert company_login.claude_env(ORG, setup.claude) == expected


def test_claude_managed_settings_take_precedence(db, repo, tmp_path, monkeypatch):
    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    managed = tmp_path / "managed.json"
    managed.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://corp.example.com"}}))
    monkeypatch.setenv("BRINDLE_CLAUDE_MANAGED_SETTINGS", str(managed))
    ws = workspaces.create(db, str(repo), "feat").workspace
    env = agents.agent_env(ws, "a1", _claude_agent(ws))
    assert "CLAUDE_CODE_USE_BEDROCK" not in env and "AWS_PROFILE" not in env


def test_profile_env_beats_the_company_env(db, repo, monkeypatch):
    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    ws = workspaces.create(db, str(repo), "feat").workspace
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True)
    (d / "mine.md").write_text("---\nname: mine\nprovider: claude\nenv.AWS_REGION: eu-west-1\n---\nhi\n")
    a = Agent("a1", ws.id, "mine", "claude", None, "assign", "starting", "", None, 0.0)
    assert agents.agent_env(ws, "a1", a)["AWS_REGION"] == "eu-west-1"


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

def test_ready_line_after_the_models_answer(monkeypatch):
    f = Fake(monkeypatch)
    ok, out = go(claude("bedrock", aws=AWS, models=MODELS))
    assert ok and out[-1] == "Ready: Claude on acme's bedrock (us-west-2)"
    probes = [c[0] for c in f.calls if c[0][:2] == ("/bin/claude", "-p")]
    assert [p[3] for p in probes] == ["claude-opus-x", "claude-sonnet-x", "claude-haiku-x"]


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


def test_no_pinned_models_is_ready_without_a_probe(monkeypatch):
    f = Fake(monkeypatch)
    _, out = go(claude("foundry", azure=AZURE))
    assert out[-1] == "Ready: Claude on acme's foundry (acme-foundry)"
    assert not f.ran("/bin/claude")


def test_failed_sign_in_is_not_ready(monkeypatch):
    Fake(monkeypatch, {("az", "account", "show"): (1, ""), ("az", "login"): (1, "")})
    ok, out = go(claude("foundry", azure=AZURE))
    assert not ok and not any(line.startswith("Ready") for line in out)


# -- no agent_setup: nothing changes ------------------------------------------------------------------

def test_no_agent_setup_changes_nothing(db, repo, monkeypatch):
    f = Fake(monkeypatch)
    monkeypatch.setattr(company_login, "org_setup", lambda *a, **k: None)
    assert company_login.apply_current(print) is None
    assert not f.calls
    assert company_login.ENV_KEY not in company_login.config.user_settings()
    ws = workspaces.create(db, str(repo), "feat").workspace
    assert "AWS_PROFILE" not in agents.agent_env(ws, "a1", _claude_agent(ws))


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


def test_models_are_probed_outside_the_repo(monkeypatch, repo):
    f = Fake(monkeypatch)
    company_login.apply(ORG, claude("bedrock", aws=AWS, models=MODELS), print, str(repo))
    probe_cwds = [cwd for c, cwd in zip(f.calls, f.cwds) if c[0][:2] == ("/bin/claude", "-p")]
    assert probe_cwds and all(cwd and cwd != str(repo) for cwd in probe_cwds)


def test_not_logged_in_org_setup_is_unavailable():
    assert company_login.org_setup() is company_login.UNAVAILABLE


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


def test_entitlement_without_team_clears_the_stored_env(monkeypatch):
    from brindle.pro import license

    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    ent = type("E", (), {"org_id": ORG, "features": frozenset()})()
    monkeypatch.setattr(license, "current", lambda **k: ent)
    company_login.apply_current(print)
    assert company_login.stored_env() == {}


def test_pinned_models_without_the_claude_binary_are_not_ready(monkeypatch):
    f = Fake(monkeypatch, missing={"claude"})
    ok, out = go(claude("bedrock", aws=AWS, models=MODELS))
    assert not ok
    assert any(line.startswith("claude isn't installed; couldn't check model access") for line in out)
    assert not any(line.startswith("Ready") for line in out)
    assert not f.ran("/bin/claude")


def test_aws_profile_header_with_extra_spaces_is_replaced(aws_config, monkeypatch):
    aws_config.parent.mkdir(parents=True)
    aws_config.write_text("[profile   brindle-acme ]\nregion = old\n[default]\nregion = x\n")
    Fake(monkeypatch)
    go(claude("bedrock", aws=AWS))
    text = aws_config.read_text()
    assert text.count("brindle-acme") == 1 and "old" not in text and "[default]\nregion = x" in text


def test_account_login_applies_company_login_after_the_pawdelta_login(monkeypatch):
    class Ent:
        sub, org_id, plan, features = "me", ORG, "team", frozenset({"team"})

    seen = []
    monkeypatch.setattr(pro_account.auth, "login", lambda *a, **k: Ent())
    monkeypatch.setattr(pro_account.loopback, "can_open_browser", lambda out: False)
    monkeypatch.setattr(company_login, "apply_current", lambda say, *a, **k: seen.append(say))
    acct = pro_account.ProAccount(store=object())
    assert acct.run(["login"]) == 0
    assert len(seen) == 1


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
