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
    env = {**PATH, **FOUNDRY, "ANTHROPIC_FOUNDRY_RESOURCE": "https://acme.openai.azure.com/"}
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
    calls, _ = init_run(credential="vertex", env={**PATH, **VERTEX}, ask=unattended)
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
