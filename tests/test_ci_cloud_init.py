"""``brindle ci init --credential bedrock|vertex|foundry``: keyless GitHub OIDC
sign-in, the IDs stored as Actions variables. All IDs here are fake."""

from __future__ import annotations

import os
import re

import pytest

from brindle import ci_client
from brindle.ci_client import CIError
from test_ci_client import REPO, ci_entitled, ci_repo, init_run  # noqa: F401 - fixtures

PATH = {"PATH": os.environ["PATH"]}
AZ_UUID = "11111111-2222-3333-4444-555555555555"
ROLE = "arn:aws:iam::123456789012:role/brindle-ci"
WIP = "projects/123456789/locations/global/workloadIdentityPools/gh-pool/providers/gh-provider"
SA = "claude-ci@acme-proj-123.iam.gserviceaccount.com"
PINS = {"ANTHROPIC_DEFAULT_OPUS_MODEL": "opus-x", "ANTHROPIC_DEFAULT_SONNET_MODEL": "sonnet-x",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "haiku-x"}
BEDROCK = {"AWS_ROLE_ARN": ROLE, "AWS_REGION": "us-east-1"}
VERTEX = {"GCP_WORKLOAD_IDENTITY_PROVIDER": WIP, "GCP_SERVICE_ACCOUNT": SA,
          "ANTHROPIC_VERTEX_PROJECT_ID": "acme-proj-123", "CLOUD_ML_REGION": "us-east5"}
FOUNDRY = {"AZURE_CLIENT_ID": AZ_UUID, "AZURE_TENANT_ID": AZ_UUID, "ANTHROPIC_FOUNDRY_RESOURCE": "acme-foundry",
           **PINS}
BY_CLOUD = {"bedrock": BEDROCK, "vertex": VERTEX, "foundry": FOUNDRY}


def variables(calls) -> dict[str, str]:
    return {c[3]: c[-1] for c in calls if c[:3] == ["gh", "variable", "set"]}


def secrets(calls) -> list[str]:
    return [c[3] for c in calls if c[:3] == ["gh", "secret", "set"]]


def never(q, d):
    """Asks nothing but the optional model pins (which take their blank default)."""
    if "optional" in q:
        return d
    pytest.fail(f"asked {q!r}")


@pytest.mark.parametrize("name, bad, looks", [
    ("GCP_WORKLOAD_IDENTITY_PROVIDER", "projects/1/providers/x", "projects/N/locations/global"),
])
@pytest.mark.brindle_ci_guard
def test_malformed_ids_stop_before_anything_runs(init_run, name, bad, looks):
    cloud = ci_client.cloud_of(name) or "bedrock"
    with pytest.raises(CIError, match=re.escape(f"{name} looks like ")) as e:
        init_run(credential=cloud, cloud_vars={**BY_CLOUD[cloud], name: bad})
    assert looks in str(e.value)
    assert not init_run.calls, "nothing ran before the bad ID was caught"


@pytest.mark.brindle_ci_guard
def test_unattended_without_the_ids_names_the_missing_variable(init_run):
    with pytest.raises(CIError, match=r"AWS_ROLE_ARN is required for --credential bedrock "
                                      r"\(pass --cloud-var NAME=VALUE or set \$AWS_ROLE_ARN\)"):
        init_run(credential="bedrock", ask=lambda q, d: d)
    assert not secrets(init_run.calls)


def test_a_cloud_variable_answers_the_credential_question(init_run):
    calls, _ = init_run(cloud_vars=BEDROCK, ask=never)
    assert variables(calls) == BEDROCK


@pytest.mark.brindle_ci_guard
def test_cloud_variables_dont_mix_with_other_credentials(init_run):
    with pytest.raises(CIError, match="configures bedrock or vertex or foundry, not --credential key"):
        init_run(credential="key", cloud_vars=BEDROCK)
    with pytest.raises(CIError, match="don't belong to --credential vertex"):
        init_run(credential="vertex", cloud_vars=BEDROCK)
    with pytest.raises(CIError, match="configure identity federation, not --credential bedrock"):
        init_run(credential="bedrock", rule_id="fdrl_1")
    assert not init_run.calls


@pytest.mark.parametrize("provider, cloud", [("bedrock", "bedrock"), ("vertex", "vertex")])
def test_managed_cloud_reads_the_org_policy(monkeypatch, provider, cloud):
    from brindle.pro import managed_models
    from brindle.pro.team_policy import ProviderConfig

    cfg = ProviderConfig(provider=provider, region="us-east-1", project="acme-proj-123")
    monkeypatch.setattr(managed_models, "current", lambda root: managed_models.Managed("org_1", cfg, False))
    got = ci_client.managed_cloud("/x")
    assert (got[0] if got else None) == cloud
    if cloud == "bedrock":
        assert got[1] == {"AWS_REGION": "us-east-1"}
    if cloud == "vertex":
        assert got[1] == {"CLOUD_ML_REGION": "us-east-1", "ANTHROPIC_VERTEX_PROJECT_ID": "acme-proj-123"}


@pytest.mark.parametrize("claude, cloud, prefill", [
    ({"route": "bedrock", "aws": {"region": "us-east-1"}}, "bedrock", {"AWS_REGION": "us-east-1"}),
    ({"route": "vertex", "gcp": {"project": "acme-proj-123", "region": "us-east5"}}, "vertex",
     {"CLOUD_ML_REGION": "us-east5", "ANTHROPIC_VERTEX_PROJECT_ID": "acme-proj-123"}),
    ({"route": "foundry", "azure": {"base_url": "https://acme.services.ai.azure.com/anthropic"}}, "foundry", {}),
    ({"route": "gateway", "gateway": {"base_url": "https://llm.acme.internal/v1"}}, None, None),
])
def test_managed_cloud_reads_an_enforced_agent_setup_route(monkeypatch, claude, cloud, prefill):
    from brindle.pro import license, managed_models, team_policy

    monkeypatch.setattr(license, "has", lambda feature: True)
    setup = team_policy._parse_agent_setup({"enforce": True, "claude": claude})
    monkeypatch.setattr(managed_models, "current",
                        lambda root: managed_models.Managed("org_1", None, False, setup=setup))
    got = ci_client.managed_cloud("/x")
    assert got == ((cloud, prefill) if cloud else None)


def test_managed_cloud_is_none_without_a_policy_or_when_unreadable(monkeypatch):
    from brindle.pro import managed_models

    monkeypatch.setattr(managed_models, "current", lambda root: None)
    assert ci_client.managed_cloud("/x") is None

    def boom(root):
        raise managed_models.ManagedUnavailable("down")
    monkeypatch.setattr(managed_models, "current", boom)
    assert ci_client.managed_cloud("/x") is None


STACK_ID = "arn:aws:cloudformation:eu-west-1:123456789012:stack/brindle-ci/abc"
CLI_JSON = {
    "aws": {"Stacks": [{"StackId": STACK_ID, "Outputs": [{"OutputKey": "RoleArn", "OutputValue": ROLE}]}]},
    "terraform": {"workload_identity_provider": {"value": WIP, "sensitive": False},
                  "service_account_email": {"value": SA, "sensitive": False}},
    "az": {"properties": {"outputs": {"clientId": {"value": AZ_UUID}, "tenantId": {"value": AZ_UUID},
                                      "subscriptionId": {"value": AZ_UUID}}}},
}


def fake_clis(monkeypatch, payloads=None, missing=()):
    """Fake aws, terraform and az: their JSON, or not installed."""
    import json
    import subprocess

    seen = []

    def run(argv, **kw):
        seen.append(argv)
        if argv[0] in missing:
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(argv, 0, json.dumps((payloads or CLI_JSON)[argv[0]]), "")
    monkeypatch.setattr(subprocess, "run", run)
    return seen


def cli_init(monkeypatch, *args):
    from typer.testing import CliRunner

    from brindle.cli import app

    got = {}
    monkeypatch.setattr(ci_client, "init", lambda **kw: got.update(kw))
    return CliRunner().invoke(app, ["ci", "init", *args], input=""), got


def test_from_stack_region_option_is_passed_on(monkeypatch):
    seen = fake_clis(monkeypatch)
    _, got = cli_init(monkeypatch, "--from-stack", "s", "--region", "us-east-1")
    assert seen[0][-2:] == ["--region", "us-east-1"] and got["cloud_vars"]["AWS_REGION"] == "us-east-1"


def test_from_terraform_reads_the_provider_and_service_account(monkeypatch):
    seen = fake_clis(monkeypatch)
    result, got = cli_init(monkeypatch, "--from-terraform", "deploy/gcp")
    assert result.exit_code == 0, result.output
    assert seen == [["terraform", "-chdir=deploy/gcp", "output", "-json"]]
    assert got["credential"] == "vertex"
    assert got["cloud_vars"] == {"GCP_WORKLOAD_IDENTITY_PROVIDER": WIP, "GCP_SERVICE_ACCOUNT": SA,
                                 "ANTHROPIC_VERTEX_PROJECT_ID": "acme-proj-123"}


def test_an_explicit_cloud_var_beats_the_template(monkeypatch):
    fake_clis(monkeypatch)
    _, got = cli_init(monkeypatch, "--from-stack", "s", "--cloud-var", "AWS_REGION=ap-south-1")
    assert got["cloud_vars"] == {"AWS_ROLE_ARN": ROLE, "AWS_REGION": "ap-south-1"}


@pytest.mark.parametrize("args, cli, instead", [
    (["--from-stack", "s"], "aws", "--cloud-var AWS_ROLE_ARN"),
])
def test_a_missing_cli_names_what_to_pass_instead(monkeypatch, args, cli, instead):
    fake_clis(monkeypatch, missing=(cli,))
    result, got = cli_init(monkeypatch, *args)
    assert result.exit_code != 0 and not got
    assert f"{cli} isn't installed" in result.output and instead in result.output


def test_a_missing_output_names_what_to_pass_instead(monkeypatch):
    fake_clis(monkeypatch, {"az": {"properties": {"outputs": {"clientId": {"value": AZ_UUID}}}}})
    result, got = cli_init(monkeypatch, "--from-deployment", "n", "-g", "rg")
    assert result.exit_code != 0 and not got
    assert "no tenantId output" in result.output and "--cloud-var AZURE_CLIENT_ID" in result.output


@pytest.mark.brindle_ci_guard
def test_from_options_need_their_own_cloud(monkeypatch):
    fake_clis(monkeypatch)
    result, got = cli_init(monkeypatch, "--from-stack", "s", "--credential", "vertex")
    assert result.exit_code != 0 and "configures bedrock" in result.output and not got
    result, _ = cli_init(monkeypatch, "--from-deployment", "n")
    assert result.exit_code != 0 and "--resource-group" in result.output


def test_cli_cloud_var_options_reach_init(ci_repo, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    got = {}
    monkeypatch.setattr(ci_client, "init", lambda **kw: got.update(kw))
    result = CliRunner().invoke(app, ["ci", "init", "--credential", "bedrock", "--cloud-var", f"AWS_ROLE_ARN={ROLE}",
                                      "--cloud-var", "AWS_REGION=us-east-1"], input="")
    assert result.exit_code == 0, result.output
    assert got["credential"] == "bedrock" and got["cloud_vars"] == BEDROCK
    bad = CliRunner().invoke(app, ["ci", "init", "--cloud-var", "AWS_REGION"], input="")
    assert bad.exit_code != 0 and "NAME=VALUE" in bad.output


# -- preview clouds: vertex and foundry are refused without --preview or BRINDLE_CI_PREVIEW_CLOUDS=1 ----


@pytest.mark.parametrize("cloud", ["vertex"])
def test_a_preview_cloud_is_set_up_with_the_flag(init_run, cloud):
    calls, said = init_run(credential=cloud, cloud_vars=BY_CLOUD[cloud], preview=True, ask=never)
    assert variables(calls) == BY_CLOUD[cloud]
    assert secrets(calls) == ["BRINDLE_PRO_TOKEN"]
    assert [s for s in said if "preview" in s] == [
        f"{cloud} is a preview for Brindle-CI: it isn't verified end to end yet"]


@pytest.mark.brindle_ci_guard
def test_the_enterprise_default_of_a_preview_cloud_is_refused_without_the_flag(init_run):
    managed = lambda cwd: ("vertex", {})  # noqa: E731 - the org policy names vertex
    with pytest.raises(CIError, match="in preview"):
        init_run(managed=managed, env={**PATH, **VERTEX}, ask=lambda q, d: d)
    assert not secrets(init_run.calls), "refused before the CI token is created"
