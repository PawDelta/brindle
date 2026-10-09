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


def environments(calls) -> list[list[str]]:
    return [c for c in calls if c[:2] == ["gh", "api"] and "/environments/" in c[2]]


def never(q, d):
    """Asks nothing but the optional model pins (which take their blank default)."""
    if "optional" in q:
        return d
    pytest.fail(f"asked {q!r}")


@pytest.mark.parametrize("cloud", sorted(BY_CLOUD))
def test_each_cloud_stores_its_variables_and_no_secret(init_run, cloud):
    calls, said = init_run(credential=cloud, cloud_vars=BY_CLOUD[cloud], ask=never)
    assert variables(calls) == BY_CLOUD[cloud]
    assert secrets(calls) == ["BRINDLE_PRO_TOKEN"], "keyless: no ANTHROPIC_API_KEY or cloud secret"
    assert f"repo:{REPO}:environment:brindle-ci" in "\n".join(said)


def test_model_pins_are_optional_outside_foundry(init_run):
    calls, _ = init_run(credential="bedrock", cloud_vars={**BEDROCK, **PINS})
    assert variables(calls) == {**BEDROCK, **PINS}
    before = len(init_run.calls)   # the fixture's call list accumulates across runs
    calls, _ = init_run(credential="bedrock", cloud_vars=BEDROCK)
    assert variables(calls[before:]) == BEDROCK


def test_foundry_needs_the_model_pins(init_run):
    with pytest.raises(CIError, match=r"ANTHROPIC_DEFAULT_OPUS_MODEL is required for --credential foundry"):
        init_run(credential="foundry", cloud_vars={k: v for k, v in FOUNDRY.items() if k not in PINS})
    assert not [c for c in init_run.calls if c[:2] in (["gh", "secret"], ["gh", "variable"])]


@pytest.mark.parametrize("name, bad, looks", [
    ("ANTHROPIC_FOUNDRY_RESOURCE", "https://acme-foundry.services.ai.azure.com", "a resource name, not a URL"),
    ("AWS_ROLE_ARN", "brindle-ci", "arn:aws:iam::"),
    ("AWS_REGION", "useast", "a region"),
    ("GCP_WORKLOAD_IDENTITY_PROVIDER", "projects/1/providers/x", "projects/N/locations/global"),
    ("GCP_SERVICE_ACCOUNT", "claude-ci", "NAME@PROJECT.iam.gserviceaccount.com"),
    ("ANTHROPIC_VERTEX_PROJECT_ID", "Not A Project", "a GCP project id"),
    ("AZURE_CLIENT_ID", "client", "a UUID"),
    ("AZURE_TENANT_ID", "tenant", "a UUID"),
    ("ANTHROPIC_DEFAULT_SONNET_MODEL", "two words", "a model or deployment name"),
])
def test_malformed_ids_stop_before_anything_runs(init_run, name, bad, looks):
    cloud = ci_client.cloud_of(name) or "bedrock"
    with pytest.raises(CIError, match=re.escape(f"{name} looks like ")) as e:
        init_run(credential=cloud, cloud_vars={**BY_CLOUD[cloud], name: bad})
    assert looks in str(e.value)
    assert not init_run.calls, "nothing ran before the bad ID was caught"


def test_a_url_as_the_foundry_resource_from_the_environment_stops_before_the_token(init_run):
    env = {**PATH, **PREVIEW, **FOUNDRY, "ANTHROPIC_FOUNDRY_RESOURCE": "https://acme.openai.azure.com/"}
    with pytest.raises(CIError, match="ANTHROPIC_FOUNDRY_RESOURCE looks like a resource name, not a URL"):
        init_run(credential="foundry", env=env)
    assert not secrets(init_run.calls) and not [s for s in init_run.said if s.startswith("3/8")]


def test_the_environment_is_created_before_the_variables(init_run):
    calls, _ = init_run(credential="vertex", cloud_vars=VERTEX)
    assert environments(calls) == [["gh", "api", f"repos/{REPO}/environments/brindle-ci", "-X", "PUT"]]
    first_variable = next(c for c in calls if c[:3] == ["gh", "variable", "set"])
    assert calls.index(environments(calls)[0]) < calls.index(first_variable)


def test_no_environment_for_key_or_federation(init_run):
    calls, _ = init_run(credential="key")
    assert not environments(calls)


def test_unattended_takes_the_environment_and_asks_nothing(init_run):
    """No terminal: ask() returns each default, which is the variable in the environment."""
    unattended = lambda q, d: "n" if q.endswith("[Y/n]") else d  # noqa: E731
    calls, _ = init_run(credential="vertex", env={**PATH, **PREVIEW, **VERTEX}, ask=unattended)
    assert variables(calls) == VERTEX


def test_unattended_without_the_ids_names_the_missing_variable(init_run):
    with pytest.raises(CIError, match=r"AWS_ROLE_ARN is required for --credential bedrock "
                                      r"\(pass --cloud-var NAME=VALUE or set \$AWS_ROLE_ARN\)"):
        init_run(credential="bedrock", ask=lambda q, d: d)
    assert not secrets(init_run.calls)


def test_a_cloud_variable_answers_the_credential_question(init_run):
    calls, _ = init_run(cloud_vars=BEDROCK, ask=never)
    assert variables(calls) == BEDROCK


def test_cloud_variables_dont_mix_with_other_credentials(init_run):
    with pytest.raises(CIError, match="configures bedrock or vertex or foundry, not --credential key"):
        init_run(credential="key", cloud_vars=BEDROCK)
    with pytest.raises(CIError, match="don't belong to --credential vertex"):
        init_run(credential="vertex", cloud_vars=BEDROCK)
    with pytest.raises(CIError, match="configure identity federation, not --credential bedrock"):
        init_run(credential="bedrock", rule_id="fdrl_1")
    assert not init_run.calls


def test_the_enterprise_policy_cloud_is_the_default_credential(init_run):
    """provider_config naming vertex makes vertex the default answer and prefills region and project."""
    asked = {}

    def answer(q, d):
        asked[q] = d
        return {"GCP workload identity provider": WIP, "GCP service account email": SA}.get(q, d)
    calls, _ = init_run(ask=answer, managed=lambda cwd: ("vertex", {
        "CLOUD_ML_REGION": "europe-west4", "ANTHROPIC_VERTEX_PROJECT_ID": "acme-proj-123"}))
    assert asked[ci_client.CREDENTIAL_QUESTION] == "vertex"
    assert asked["Vertex AI region"] == "europe-west4" and asked["GCP project id for Vertex AI"] == "acme-proj-123"
    assert variables(calls) == {**VERTEX, "CLOUD_ML_REGION": "europe-west4"}


def test_an_explicit_credential_beats_the_enterprise_default(init_run):
    calls, _ = init_run(credential="key", managed=lambda cwd: ("bedrock", {"AWS_REGION": "us-east-1"}))
    assert secrets(calls) == ["BRINDLE_PRO_TOKEN", "ANTHROPIC_API_KEY"] and not environments(calls)


@pytest.mark.parametrize("provider, cloud", [("bedrock", "bedrock"), ("vertex", "vertex"), ("azure", "foundry"),
                                             ("anthropic", None), ("openai-compatible", None)])
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


def test_managed_cloud_is_none_without_a_policy_or_when_unreadable(monkeypatch):
    from brindle.pro import managed_models

    monkeypatch.setattr(managed_models, "current", lambda root: None)
    assert ci_client.managed_cloud("/x") is None

    def boom(root):
        raise managed_models.ManagedUnavailable("down")
    monkeypatch.setattr(managed_models, "current", boom)
    assert ci_client.managed_cloud("/x") is None


def test_doctor_names_the_cloud_and_variables_but_never_values(ci_repo):
    env = {**PATH, "CLAUDE_CODE_USE_FOUNDRY": "1", "AZURE_CLIENT_ID": AZ_UUID,
           "ANTHROPIC_FOUNDRY_RESOURCE": "acme-foundry", "ANTHROPIC_DEFAULT_OPUS_MODEL": "opus-secretish"}
    out = ci_client.doctor(env, REPO, str(ci_repo), org=True)
    line = next(ln for ln in out.splitlines() if ln.startswith("cloud:"))
    assert line.startswith("cloud: foundry")
    assert "AZURE_CLIENT_ID" in line and "ANTHROPIC_FOUNDRY_RESOURCE" in line
    assert "missing: AZURE_TENANT_ID, ANTHROPIC_DEFAULT_SONNET_MODEL, ANTHROPIC_DEFAULT_HAIKU_MODEL" in line
    assert AZ_UUID not in line and "acme-foundry" not in line and "opus-secretish" not in line


def test_doctor_has_no_cloud_line_without_a_cloud(ci_repo):
    assert "cloud:" not in ci_client.doctor(PATH, REPO, str(ci_repo), org=True)


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


def test_from_stack_reads_the_role_and_the_stacks_region(monkeypatch):
    seen = fake_clis(monkeypatch)
    result, got = cli_init(monkeypatch, "--from-stack", "brindle-ci")
    assert result.exit_code == 0, result.output
    assert seen == [["aws", "cloudformation", "describe-stacks", "--stack-name", "brindle-ci", "--output", "json"]]
    assert got["credential"] == "bedrock"
    assert got["cloud_vars"] == {"AWS_ROLE_ARN": ROLE, "AWS_REGION": "eu-west-1"}
    assert ROLE not in result.output, "the IDs are not echoed"


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


def test_from_deployment_reads_client_and_tenant_not_subscription(monkeypatch):
    seen = fake_clis(monkeypatch)
    result, got = cli_init(monkeypatch, "--from-deployment", "main", "-g", "rg1")
    assert result.exit_code == 0, result.output
    assert seen == [["az", "deployment", "group", "show", "--name", "main", "--resource-group", "rg1",
                     "--output", "json"]]
    assert got["credential"] == "foundry"
    assert got["cloud_vars"] == {"AZURE_CLIENT_ID": AZ_UUID, "AZURE_TENANT_ID": AZ_UUID}


def test_an_explicit_cloud_var_beats_the_template(monkeypatch):
    fake_clis(monkeypatch)
    _, got = cli_init(monkeypatch, "--from-stack", "s", "--cloud-var", "AWS_REGION=ap-south-1")
    assert got["cloud_vars"] == {"AWS_ROLE_ARN": ROLE, "AWS_REGION": "ap-south-1"}


@pytest.mark.parametrize("args, cli, instead", [
    (["--from-stack", "s"], "aws", "--cloud-var AWS_ROLE_ARN"),
    (["--from-terraform", "d"], "terraform", "--cloud-var GCP_WORKLOAD_IDENTITY_PROVIDER"),
    (["--from-deployment", "n", "-g", "rg"], "az", "--cloud-var AZURE_CLIENT_ID"),
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


def test_a_template_value_is_still_format_checked(init_run):
    values = {"AWS_ROLE_ARN": "brindle-ci", "AWS_REGION": "us-east-1"}
    with pytest.raises(CIError, match="AWS_ROLE_ARN looks like "):
        init_run(credential="bedrock", cloud_vars=values)


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

PREVIEW_MESSAGE = ("Vertex AI and Microsoft Foundry for Brindle-CI are in preview: they haven't been verified "
                   "end to end yet. Pass --preview to set them up anyway.")
PREVIEW = {ci_client.PREVIEW_ENV: "1"}
PREVIEW_CLOUD_NAMES = ("vertex", "foundry")


@pytest.mark.parametrize("cloud", PREVIEW_CLOUD_NAMES)
def test_a_preview_cloud_is_refused_without_the_flag(init_run, cloud):
    with pytest.raises(CIError) as refused:
        init_run(credential=cloud, cloud_vars=BY_CLOUD[cloud], env={**PATH, **BY_CLOUD[cloud]}, ask=never)
    assert str(refused.value) == PREVIEW_MESSAGE
    assert not init_run.calls, "refused before anything is run, so no token is created"


@pytest.mark.parametrize("cloud", PREVIEW_CLOUD_NAMES)
def test_a_preview_cloud_is_set_up_with_the_flag(init_run, cloud):
    calls, said = init_run(credential=cloud, cloud_vars=BY_CLOUD[cloud], preview=True, ask=never)
    assert variables(calls) == BY_CLOUD[cloud]
    assert secrets(calls) == ["BRINDLE_PRO_TOKEN"]
    assert [s for s in said if "preview" in s] == [
        f"{cloud} is a preview for Brindle-CI: it isn't verified end to end yet"]


@pytest.mark.parametrize("cloud", PREVIEW_CLOUD_NAMES)
def test_a_preview_cloud_is_set_up_with_the_env_var_unattended(init_run, cloud):
    unattended = lambda q, d: "n" if q.endswith("[Y/n]") else d  # noqa: E731
    calls, _ = init_run(credential=cloud, env={**PATH, **PREVIEW, **BY_CLOUD[cloud]}, ask=unattended)
    assert variables(calls) == BY_CLOUD[cloud]


def test_the_env_var_must_be_exactly_one(init_run):
    with pytest.raises(CIError, match="in preview"):
        init_run(credential="vertex", cloud_vars=VERTEX, env={**PATH, ci_client.PREVIEW_ENV: "0"}, ask=never)


def test_bedrock_is_unaffected_without_the_flag(init_run):
    calls, said = init_run(credential="bedrock", cloud_vars=BEDROCK, env={**PATH}, ask=never)
    assert variables(calls) == BEDROCK
    assert not any("preview" in s for s in said)


def test_the_enterprise_default_of_a_preview_cloud_is_refused_without_the_flag(init_run):
    managed = lambda cwd: ("vertex", {})  # noqa: E731 - the org policy names vertex
    with pytest.raises(CIError, match="in preview"):
        init_run(managed=managed, env={**PATH, **VERTEX}, ask=lambda q, d: d)
    assert not secrets(init_run.calls), "refused before the CI token is created"


def test_the_enterprise_default_of_a_preview_cloud_is_set_up_with_the_flag(init_run):
    managed = lambda cwd: ("vertex", {})  # noqa: E731
    calls, _ = init_run(managed=managed, preview=True, env={**PATH, **VERTEX}, ask=lambda q, d: d)
    assert variables(calls) == VERTEX


def test_cli_preview_flag_reaches_init(ci_repo, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    got = {}
    monkeypatch.setattr(ci_client, "init", lambda **kw: got.update(kw))
    result = CliRunner().invoke(app, ["ci", "init", "--credential", "vertex", "--preview"], input="")
    assert result.exit_code == 0, result.output
    assert got["preview"] is True and got["credential"] == "vertex"


@pytest.mark.parametrize("cloud, expected", [("vertex", "cloud: vertex (preview) "), ("foundry", "cloud: foundry (preview) "),
                                             ("bedrock", "cloud: bedrock (keyless OIDC)")])
def test_doctor_marks_the_preview_clouds(ci_repo, cloud, expected):
    env = {**PATH, f"CLAUDE_CODE_USE_{cloud.upper()}": "1", **BY_CLOUD[cloud]}
    out = ci_client.doctor(env, REPO, str(ci_repo), org=True)
    line = next(ln for ln in out.splitlines() if ln.startswith("cloud:"))
    assert line.startswith(expected)
