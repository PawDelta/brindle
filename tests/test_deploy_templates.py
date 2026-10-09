"""The one-click deployment templates under deploy/: each trusts only GitHub's
OIDC token for the ``brindle-ci`` environment of one repository, grants the
model-calling permission and nothing else, and outputs what ``brindle ci init``
needs. Parsed here without cfn-lint, terraform or bicep."""
import json
import re
from pathlib import Path

import hcl2
import yaml

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
ISSUER = "https://token.actions.githubusercontent.com"
SUBJECT = "repo:${GitHubRepo}:environment:brindle-ci"


def cfn():
    class Loader(yaml.SafeLoader):
        pass

    def tag(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
            if suffix == "GetAtt":
                value = value.split(".", 1)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        return {("Ref" if suffix == "Ref" else f"Fn::{suffix}"): value}

    Loader.add_multi_constructor("!", tag)
    return yaml.load((DEPLOY / "aws/brindle-ci-bedrock.yaml").read_text(), Loader)


def hcl(name):
    def clean(v):
        if isinstance(v, str):
            return v[1:-1] if len(v) > 1 and v[0] == v[-1] == '"' else v
        if isinstance(v, dict):
            return {clean(k): clean(x) for k, x in v.items() if k != "__is_block__"}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v

    with open(DEPLOY / "gcp" / name) as f:
        return clean(hcl2.load(f))


def tf_resources():
    out = {}
    for block in hcl("main.tf")["resource"]:
        for rtype, named in block.items():
            for rname, body in named.items():
                out[(rtype, rname)] = body
    return out


# ---- AWS ----

def test_aws_trust_policy_pins_audience_and_subject():
    t = cfn()
    role = t["Resources"]["BrindleCiRole"]["Properties"]
    assert role["MaxSessionDuration"] == 7200
    (stmt,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == "sts:AssumeRoleWithWebIdentity"
    cond, created, existing = stmt["Principal"]["Federated"]["Fn::If"]
    assert created == {"Ref": "GitHubOidcProvider"}
    assert "oidc-provider/token.actions.githubusercontent.com" in existing["Fn::Sub"]
    assert stmt["Condition"] == {"StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": {"Fn::Sub": SUBJECT},
    }}


def test_aws_oidc_provider_is_optional_and_trusts_sts_audience():
    t = cfn()
    params = t["Parameters"]
    assert params["GitHubRepo"]["AllowedPattern"]
    assert re.fullmatch(params["GitHubRepo"]["AllowedPattern"], "acme/widgets")
    assert not re.fullmatch(params["GitHubRepo"]["AllowedPattern"], "acme")
    assert params["CreateOidcProvider"]["AllowedValues"] == ["true", "false"]
    provider = t["Resources"]["GitHubOidcProvider"]
    assert provider["Type"] == "AWS::IAM::OIDCProvider"
    assert provider["Condition"] in t["Conditions"]
    # the trust policy references the created provider through !If (an implicit dependency);
    # an explicit DependsOn on a conditional resource fails cfn-lint (E3005)
    assert "DependsOn" not in t["Resources"]["BrindleCiRole"]
    cond = t["Resources"]["BrindleCiRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]["Federated"]["Fn::If"][0]
    assert cond == provider["Condition"]
    assert provider["Properties"]["Url"] == ISSUER
    assert provider["Properties"]["ClientIdList"] == ["sts.amazonaws.com"]


def test_aws_policy_grants_only_bedrock_invoke_and_profile_reads():
    role = cfn()["Resources"]["BrindleCiRole"]["Properties"]
    (policy,) = role["Policies"]
    (stmt,) = policy["PolicyDocument"]["Statement"]
    assert stmt["Effect"] == "Allow"
    assert sorted(stmt["Action"]) == sorted([
        "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
        "bedrock:ListInferenceProfiles", "bedrock:GetInferenceProfile"])
    resources = [r["Fn::Sub"] for r in stmt["Resource"]]
    for suffix in ("inference-profile/*", "application-inference-profile/*", "foundation-model/*"):
        assert any(r.endswith(":" + suffix) for r in resources), suffix


def test_aws_outputs_role_arn():
    out = cfn()["Outputs"]
    assert set(out) == {"RoleArn"}
    assert out["RoleArn"]["Value"] == {"Fn::GetAtt": ["BrindleCiRole", "Arn"]}


# ---- GCP ----

def test_gcp_enables_apis_and_pins_subject_in_provider():
    res = tf_resources()
    apis = res[("google_project_service", "apis")]
    for api in ("aiplatform.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com"):
        assert api in apis["for_each"]
    assert len(re.findall(r"\w+\.googleapis\.com", apis["for_each"])) == 3
    assert "data" not in hcl("main.tf")   # no unused data sources needing extra permissions
    pool = res[("google_iam_workload_identity_pool", "brindle_ci")]
    assert pool["workload_identity_pool_id"] == "brindle-ci"
    provider = res[("google_iam_workload_identity_pool_provider", "github")]
    assert provider["workload_identity_pool_provider_id"] == "github"
    assert provider["oidc"][0]["issuer_uri"] == ISSUER
    assert provider["attribute_condition"] == \
        "assertion.sub == 'repo:${var.github_repo}:environment:brindle-ci'"
    assert provider["attribute_mapping"]["attribute.repository"] == "assertion.repository"
    assert "allowed_audiences" not in provider["oidc"][0]   # the default audience is the provider's own


def test_gcp_service_account_roles():
    res = tf_resources()
    sa = res[("google_service_account", "brindle_ci")]
    assert sa["account_id"] == "brindle-ci"
    user = res[("google_project_iam_member", "vertex_user")]
    assert user["role"] == "roles/aiplatform.user"
    assert user["member"] == "serviceAccount:${google_service_account.brindle_ci.email}"
    wif = res[("google_service_account_iam_member", "github_impersonation")]
    assert wif["role"] == "roles/iam.workloadIdentityUser"
    assert wif["member"].startswith("principalSet://iam.googleapis.com/")
    assert wif["member"].endswith("/attribute.repository/${var.github_repo}")
    assert "google_iam_workload_identity_pool.brindle_ci.name" in wif["member"]
    roles = {b["role"] for k, b in res.items() if k[0].endswith("_iam_member")}
    assert roles == {"roles/aiplatform.user", "roles/iam.workloadIdentityUser"}


def test_gcp_outputs_and_variables():
    outs = {k: v for block in hcl("outputs.tf")["output"] for k, v in block.items()}
    assert set(outs) == {"workload_identity_provider", "service_account_email"}
    assert outs["workload_identity_provider"]["value"] == \
        "${google_iam_workload_identity_pool_provider.github.name}"
    assert outs["service_account_email"]["value"] == "${google_service_account.brindle_ci.email}"
    variables = {k for block in hcl("variables.tf")["variable"] for k in block}
    assert variables == {"project_id", "github_repo"}
    assert (DEPLOY / "gcp/tutorial.md").read_text().strip()


# ---- Azure ----

ROLE_ID = "a97b65f3-24c7-4388-baec-2e87135dc908"


def arm():
    return json.loads((DEPLOY / "azure/azuredeploy.json").read_text())


def test_azure_template_federated_credential_and_role():
    t = arm()
    by_type = {r["type"]: r for r in t["resources"]}
    assert set(by_type) == {
        "Microsoft.ManagedIdentity/userAssignedIdentities",
        "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials",
        "Microsoft.Authorization/roleAssignments"}
    assert by_type["Microsoft.ManagedIdentity/userAssignedIdentities"]["name"] == "brindle-ci"
    cred = by_type["Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials"]["properties"]
    assert cred["issuer"] == ISSUER
    assert cred["audiences"] == ["api://AzureADTokenExchange"]
    assert cred["subject"] == "[format('repo:{0}:environment:brindle-ci', parameters('githubRepo'))]"
    assert t["variables"]["cognitiveServicesUserRoleId"] == ROLE_ID
    role = by_type["Microsoft.Authorization/roleAssignments"]
    assert "parameters('foundryAccountName')" in role["scope"]
    assert "Microsoft.CognitiveServices/accounts" in role["scope"]
    assert role["properties"]["roleDefinitionId"] == \
        "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', variables('cognitiveServicesUserRoleId'))]"
    assert role["properties"]["principalType"] == "ServicePrincipal"
    assert set(t["parameters"]) >= {"githubRepo", "foundryAccountName"}


def test_azure_outputs():
    out = arm()["outputs"]
    assert set(out) == {"clientId", "tenantId", "subscriptionId"}
    assert out["clientId"]["value"].endswith(".clientId]")
    assert out["tenantId"]["value"] == "[tenant().tenantId]"
    assert out["subscriptionId"]["value"] == "[subscription().subscriptionId]"


def test_azuredeploy_json_matches_bicep():
    bicep = (DEPLOY / "azure/main.bicep").read_text()
    t = arm()
    types = re.findall(r"^resource (\w+) '([^@']+)@", bicep, re.M)
    declared = {typ for _, typ in types if "existing" not in bicep.split(f"'{typ}@")[1].split("\n")[0]}
    assert declared == {r["type"] for r in t["resources"]}
    assert re.search(rf"var cognitiveServicesUserRoleId = '{ROLE_ID}'", bicep)
    assert ISSUER in bicep
    assert "subject: 'repo:${githubRepo}:environment:brindle-ci'" in bicep
    assert re.search(r"audiences: \[\s*'api://AzureADTokenExchange'\s*\]", bicep)
    assert "name: 'brindle-ci'" in bicep
    assert set(re.findall(r"^param (\w+)", bicep, re.M)) == set(t["parameters"])
    assert set(re.findall(r"^output (\w+)", bicep, re.M)) == set(t["outputs"])
    assert "scope: foundry" in bicep and "name: foundryAccountName" in bicep


# ---- repo hygiene ----

def test_no_account_ids_or_buckets_in_deploy_or_publish_workflow():
    root = DEPLOY.parent
    texts = [p.read_text() for p in DEPLOY.rglob("*") if p.is_file()]
    texts.append((root / ".github/workflows/publish.yml").read_text())
    for text in texts:
        assert not re.search(r"\b\d{12}\b", text)
        assert "s3://brindle" not in text


def test_publish_uploads_aws_template_only_when_bucket_variable_set():
    text = (DEPLOY.parent / ".github/workflows/publish.yml").read_text()
    assert "vars.DEPLOY_TEMPLATES_BUCKET" in text
    assert "templates/$version/brindle-ci-bedrock.yaml" in text
    assert text.count("env.BUCKET != ''") >= 2
