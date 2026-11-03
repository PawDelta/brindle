"""Self-serve company setup: an agent can neither be offered a setup nor remove
the person's saved route."""

import io

import pytest

from brindle import company_identity, company_login, config, providers, self_serve
from brindle.pro import team_policy
from brindle.pro.account import ProAccount
from brindle.pro.team_policy import AgentSetup, ClaudeSetup

AWS_CONFIG = """\
[profile dev]
sso_start_url = https://acme.awsapps.com/start
sso_region = us-east-1
sso_account_id = 123456789012
sso_role_name = Dev
region = us-west-2
"""


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    company_identity.reset()
    for name in ("ANTHROPIC_FOUNDRY_RESOURCE", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(providers, "claude_org_managed", lambda: None)
    cfg = tmp_path / "aws-config"
    cfg.write_text(AWS_CONFIG)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))


def test_an_agent_is_never_offered_a_setup_and_cannot_remove_the_saved_route(monkeypatch):
    calls = []
    monkeypatch.setattr(company_login, "run_cmd", lambda argv, *a, **k: calls.append(argv) or (1, ""))
    monkeypatch.setattr(company_login, "which", lambda n: f"/bin/{n}")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setenv("BRINDLE_AGENT_ID", "abc123")
    asked = []
    assert self_serve.run(lambda m: None, ask=lambda p: asked.append(p) or "y") is None
    assert asked == [] and calls == []

    monkeypatch.delenv("BRINDLE_AGENT_ID")       # the person saves a route; an agent then tries to reset it
    setup = AgentSetup(claude=ClaudeSetup(route="vertex", gcp=team_policy.GcpSetup("acme-ml-123", "us-east5")))
    self_serve.save_local(setup)
    company_login.store_env(self_serve.SELF_ORG, company_login.claude_env(self_serve.SELF_ORG, setup.claude),
                            self_serve=True)
    monkeypatch.setenv("BRINDLE_AGENT_ID", "abc123")
    assert ProAccount(out=io.StringIO(), err=io.StringIO()).run(["route", "--reset"]) == 1
    assert self_serve.route_status() == ("vertex", True)       # an agent can't remove the person's route
    monkeypatch.delenv("BRINDLE_AGENT_ID")
    assert ProAccount(out=io.StringIO()).run(["route", "--reset"]) == 0
    assert self_serve.route_status() is None
    assert not {"agent_setup", "company_claude_env"} & set(config.user_settings())
