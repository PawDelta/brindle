"""The one-click deployment templates under deploy/: each trusts only GitHub's
OIDC token for the ``brindle-ci`` environment of one repository, grants the
model-calling permission and nothing else, and outputs what ``brindle ci init``
needs. Parsed here without cfn-lint, terraform or bicep."""
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


# ---- repo hygiene ----

def test_no_account_ids_or_buckets_in_deploy_or_publish_workflow():
    root = DEPLOY.parent
    texts = [p.read_text(encoding="utf-8", errors="ignore") for p in DEPLOY.rglob("*") if p.is_file() and p.name != ".DS_Store"]
    texts.append((root / ".github/workflows/publish.yml").read_text())
    for text in texts:
        assert not re.search(r"\b\d{12}\b", text)
        assert "s3://brindle" not in text
