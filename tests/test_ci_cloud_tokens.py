"""Keyless cloud sign-in for brindle CI: a refreshed OIDC token file per cloud
(:mod:`brindle.ci_federation`), the env the agents get, and what stays out of
their panes; on GitLab (brindle Enterprise) the template's ``id_tokens`` per
cloud, the token file written once and never refreshed, and the lifetime
warning."""

from __future__ import annotations

import base64
import json
import os

import pytest

from brindle import ci_client, ci_federation as fed, ci_hosts, secrets
from brindle.ci_client import CIError
from brindle.pro import managed_models
from brindle.pro.team_policy import ProviderConfig

URL = "https://actions.example/token?x=1"
REQ = "request-token-secret"
NOW = 1_000_000.0
LIFETIME = 600.0

AWS = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/brindle", "AWS_REGION": "us-east-1"}
GCP = {"CLAUDE_CODE_USE_VERTEX": "1",
       "GCP_WORKLOAD_IDENTITY_PROVIDER": "projects/12/locations/global/workloadIdentityPools/p/providers/gh",
       "GCP_SERVICE_ACCOUNT": "brindle@proj-12345.iam.gserviceaccount.com",
       "ANTHROPIC_VERTEX_PROJECT_ID": "proj-12345", "CLOUD_ML_REGION": "us-east5"}
AZURE = {"CLAUDE_CODE_USE_FOUNDRY": "1", "AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
         "AZURE_TENANT_ID": "22222222-2222-2222-2222-222222222222", "ANTHROPIC_FOUNDRY_RESOURCE": "res"}
ACTIONS = {"ACTIONS_ID_TOKEN_REQUEST_URL": URL, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": REQ}


def jwt(exp: float, tag: str = "a") -> str:
    def b(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b({'alg': 'none'})}.{b({'exp': exp, 'jti': tag})}.sig"


class FakeActions:
    """The Actions token endpoint: records the audience of each request."""

    def __init__(self, clock, fail=0):
        self.clock, self.fail, self.audiences, self.n = clock, fail, [], 0

    def __call__(self, url, request_token, audience):
        assert (url, request_token) == (URL, REQ)
        self.audiences.append(audience)
        if self.fail:
            self.fail -= 1
            raise fed.FederationError("couldn't get the GitHub Actions OIDC token (HTTP 500)")
        self.n += 1
        return jwt(self.clock() + LIFETIME, str(self.n))


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def refresher(cloud, ids, tmp_path, **kw):
    clock = Clock()
    actions = FakeActions(clock, kw.pop("fail", 0))
    r = fed.CloudTokenRefresher(cloud, ids, URL, REQ, directory=str(tmp_path), fetch=actions, clock=clock)
    return r, actions, clock


def test_a_failed_refresh_is_retried_and_keeps_the_old_token(tmp_path, caplog):
    r, actions, clock = refresher(fed.BEDROCK, AWS, tmp_path)
    r.tick()
    old = open(r.token_file).read()
    actions.fail = 2
    clock.t += LIFETIME / 2
    d1, d2 = r.tick(), r.tick()
    assert (d1, d2) == fed.RETRY_DELAYS[:2] and d1 < LIFETIME / 2   # retried while the old one is valid
    assert open(r.token_file).read() == old
    assert r.tick() == LIFETIME / 2   # recovered
    assert open(r.token_file).read() != old
    assert REQ not in caplog.text and old not in caplog.text


def test_a_token_without_exp_gets_a_default_lifetime(tmp_path):
    clock = Clock()
    r = fed.CloudTokenRefresher(fed.BEDROCK, AWS, URL, REQ, directory=str(tmp_path),
                                fetch=lambda *a: "not.a.jwt", clock=clock)
    assert r.refresh_now() == fed.DEFAULT_CLOUD_LIFETIME_S * fed.CLOUD_REFRESH_FRACTION


def test_azure_agents_get_ids_and_token_file(tmp_path):
    r, *_ = refresher(fed.FOUNDRY, AZURE, tmp_path)
    r.refresh_now()
    assert r.agent_env() == {"AZURE_CLIENT_ID": AZURE["AZURE_CLIENT_ID"], "AZURE_TENANT_ID": AZURE["AZURE_TENANT_ID"],
                             "AZURE_FEDERATED_TOKEN_FILE": r.token_file}


# -- which clouds a job uses ----------------------------------------------------------------------


@pytest.mark.brindle_ci_guard
def test_no_cloud_without_the_actions_endpoint_ids_or_with_a_static_credential():
    assert fed.cloud_token_clouds(AWS) == []
    assert fed.cloud_token_clouds({**AWS, **ACTIONS, "AWS_ROLE_ARN": ""}) == []
    assert fed.cloud_token_clouds({**AWS, **ACTIONS, "AWS_ACCESS_KEY_ID": "AKIA"}) == []
    assert fed.cloud_token_clouds({**AWS, **ACTIONS, fed.DISABLE_VAR: "0"}) == []
    assert fed.cloud_token_clouds({**AWS, **ACTIONS, "CLAUDE_CODE_USE_BEDROCK": "0"}) == []


@pytest.mark.brindle_ci_guard
def test_a_failed_first_fetch_leaves_the_cloud_out(tmp_path):
    def boom(*a):
        raise fed.FederationError("HTTP 500")
    assert fed.start_cloud_tokens({**AWS, **ACTIONS}, fetch=boom) is None


@pytest.mark.brindle_ci_guard
def test_scrub_job_drops_the_actions_endpoint_but_gives_agents_the_cloud_env(monkeypatch):
    monkeypatch.setattr(ci_client, "_fetch_oidc", lambda *a, **k: jwt(NOW + 600))
    monkeypatch.setattr(fed.time, "time", lambda: NOW)
    env = {**AWS, **ACTIONS, "GITHUB_TOKEN": "x"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    tokens = ci_client.start_cloud_tokens(env, say=lambda m: None)
    try:
        for k in tokens.agent_env():
            monkeypatch.setenv(k, os.environ.get(k, ""))
        ci_client.scrub_job(env, None, tokens)
        for e in (env, os.environ):
            assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in e and "ACTIONS_ID_TOKEN_REQUEST_URL" not in e
            assert e["AWS_WEB_IDENTITY_TOKEN_FILE"] and e["AWS_ROLE_SESSION_NAME"]
        assert REQ not in "".join(env.values())
    finally:
        tokens.stop()


# -- panes ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"])
@pytest.mark.brindle_ci_guard
def test_the_actions_endpoint_stays_out_of_panes_whatever_is_kept(name):
    keep = secrets.provider_credentials("claude", name)
    assert name in secrets.pane_unset([name], keep=keep)
    assert name in secrets.scrub_secrets({name: "v"})


def test_managed_models_pin_the_haiku_default_not_the_deprecated_variable():
    cfg = ProviderConfig("bedrock", "us-east-1", ("opus-id", "haiku-id"), None)
    env = managed_models.provider_env(cfg, "claude")
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "haiku-id"
    assert "ANTHROPIC_SMALL_FAST_MODEL" not in env


# -- GitLab: the rendered template, the token file written once, the lifetime warning ----------


def gitlab_env(ids, var, token):
    return {**ids, "GITLAB_CI": "true", var: token}


def test_template_without_a_cloud_is_unchanged():
    assert ci_hosts.render_template() == ci_hosts.GITLAB_TEMPLATE
    assert "BRINDLE_AWS_ID_TOKEN" not in ci_hosts.GITLAB_TEMPLATE
    with pytest.raises(CIError):
        ci_hosts.render_template("nope")


def test_the_cloud_is_selected_by_the_cicd_variables():
    assert ci_hosts.cloud_from_env(AWS) == "bedrock"
    only_ids = {k: v for k, v in AWS.items() if not k.startswith("CLAUDE_CODE_USE_")}
    assert ci_hosts.cloud_from_env(only_ids) == "bedrock"
    assert ci_hosts.cloud_from_env({}) is None


def test_init_writes_the_cloud_template_and_warns(tmp_path):
    said = []
    path = ci_hosts.init_gitlab(cwd=str(tmp_path), is_entitled=lambda: True, say=said.append, env=AZURE,
                                preview=True)   # foundry is in preview
    assert path.read_text() == ci_hosts.render_template("foundry")
    assert any("lifetime must cover the job timeout" in s for s in said)


@pytest.mark.brindle_ci_guard
def test_gitlab_agents_get_the_cloud_env_and_never_the_token():
    token = jwt(NOW + 3600, "secret-jti")
    tokens = fed.start_cloud_tokens(gitlab_env(GCP, "BRINDLE_GCP_ID_TOKEN", token))
    try:
        env = tokens.agent_env()
        assert token not in "".join(env.values())
        (r,) = tokens.refreshers
        config = json.load(open(env["GOOGLE_APPLICATION_CREDENTIALS"]))
        assert config["credential_source"]["file"] == r.token_file
        assert config["audience"] == "//iam.googleapis.com/" + GCP["GCP_WORKLOAD_IDENTITY_PROVIDER"]
        assert token not in json.dumps(config)
    finally:
        tokens.stop()


def test_run_says_the_token_is_not_refreshed():
    said = []
    tokens = ci_client.start_cloud_tokens(gitlab_env(AWS, "BRINDLE_AWS_ID_TOKEN", jwt(NOW + 3600)), say=said.append)
    try:
        assert any("not refreshed" in s for s in said)
    finally:
        tokens.stop()


def test_doctor_compares_the_token_with_the_job_timeout(monkeypatch):
    lines = ci_client.cloud_status(gitlab_env(AWS, "BRINDLE_AWS_ID_TOKEN", jwt(NOW + 3600)))
    assert any(l.startswith("warning:") and "lifetime must cover the job timeout" in l for l in lines)
    assert not any(jwt(NOW + 3600) in l for l in lines)
    monkeypatch.setattr(ci_client.time, "time", lambda: NOW)
    env = {**gitlab_env(AWS, "BRINDLE_AWS_ID_TOKEN", jwt(NOW + 300)), "CI_JOB_TIMEOUT": "3600"}
    (warn,) = [l for l in ci_client.cloud_status(env) if l.startswith("warning:")]
    assert "300s left" in warn and "3600s" in warn
    env["CI_JOB_TIMEOUT"] = "200"
    (warn,) = [l for l in ci_client.cloud_status(env) if l.startswith("warning:")]
    assert "left" not in warn
