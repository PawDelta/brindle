"""Keyless cloud sign-in for brindle CI: a refreshed OIDC token file per cloud
(:mod:`brindle.ci_federation`), the env the agents get, and what stays out of
their panes."""

from __future__ import annotations

import base64
import json
import os
import stat

import pytest

from brindle import ci_adapters, ci_client, ci_federation as fed, secrets
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


@pytest.mark.parametrize("cloud,ids,audience", [
    (fed.BEDROCK, AWS, "sts.amazonaws.com"),
    (fed.VERTEX, GCP, "//iam.googleapis.com/" + GCP["GCP_WORKLOAD_IDENTITY_PROVIDER"]),
    (fed.FOUNDRY, AZURE, "api://AzureADTokenExchange"),
])
def test_token_file_has_the_clouds_audience_and_mode_0600(cloud, ids, audience, tmp_path):
    r, actions, clock = refresher(cloud, ids, tmp_path)
    delay = r.refresh_now()
    assert actions.audiences == [audience]
    assert stat.S_IMODE(os.stat(r.token_file).st_mode) == 0o600
    assert open(r.token_file).read() == jwt(NOW + LIFETIME, "1")
    assert delay == LIFETIME / 2   # half the token's lifetime, from its exp


def test_refresh_rewrites_the_file_in_place_at_half_life(tmp_path):
    r, actions, clock = refresher(fed.BEDROCK, AWS, tmp_path)
    assert r.tick() == LIFETIME / 2
    clock.t += LIFETIME / 2
    assert r.tick() == LIFETIME / 2
    assert open(r.token_file).read() == jwt(clock.t + LIFETIME, "2")
    assert stat.S_IMODE(os.stat(r.token_file).st_mode) == 0o600


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


def test_aws_agents_get_the_role_and_token_file_only(tmp_path):
    r, *_ = refresher(fed.BEDROCK, AWS, tmp_path)
    r.refresh_now()
    env = r.agent_env()
    assert env == {"AWS_ROLE_ARN": AWS["AWS_ROLE_ARN"], "AWS_WEB_IDENTITY_TOKEN_FILE": r.token_file,
                   "AWS_ROLE_SESSION_NAME": "brindle-ci"}
    assert not any(open(r.token_file).read() in v for v in env.values())


def test_azure_agents_get_ids_and_token_file(tmp_path):
    r, *_ = refresher(fed.FOUNDRY, AZURE, tmp_path)
    r.refresh_now()
    assert r.agent_env() == {"AZURE_CLIENT_ID": AZURE["AZURE_CLIENT_ID"], "AZURE_TENANT_ID": AZURE["AZURE_TENANT_ID"],
                             "AZURE_FEDERATED_TOKEN_FILE": r.token_file}


def test_google_agents_get_an_external_account_config(tmp_path):
    r, *_ = refresher(fed.VERTEX, GCP, tmp_path)
    r.refresh_now()
    env = r.agent_env()
    assert list(env) == ["GOOGLE_APPLICATION_CREDENTIALS"]
    path = env["GOOGLE_APPLICATION_CREDENTIALS"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    cfg = json.load(open(path))
    assert cfg == {
        "type": "external_account",
        "audience": "//iam.googleapis.com/" + GCP["GCP_WORKLOAD_IDENTITY_PROVIDER"],
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": "https://sts.googleapis.com/v1/token",
        "service_account_impersonation_url": "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                                             "brindle@proj-12345.iam.gserviceaccount.com:generateAccessToken",
        "credential_source": {"file": r.token_file, "format": {"type": "text"}},
    }
    assert open(r.token_file).read() not in open(path).read()


def test_the_token_dir_is_outside_the_checkout_and_removed_on_stop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = fed.CloudTokenRefresher(fed.BEDROCK, AWS, URL, REQ, fetch=lambda *a: jwt(NOW + 600), clock=lambda: NOW)
    try:
        r.refresh_now()
        assert not os.path.realpath(r.token_file).startswith(str(tmp_path.resolve()))
        assert stat.S_IMODE(os.stat(r.directory).st_mode) == 0o700
    finally:
        r.stop()
    assert not os.path.exists(r.directory)


# -- which clouds a job uses ----------------------------------------------------------------------


@pytest.mark.parametrize("ids,cloud", [(AWS, "bedrock"), (GCP, "vertex"), (AZURE, "foundry")])
def test_the_cloud_is_chosen_by_claude_code_use_and_its_ids(ids, cloud):
    assert fed.cloud_token_clouds({**ids, **ACTIONS}) == [cloud]


def test_no_cloud_without_the_actions_endpoint_ids_or_with_a_static_credential():
    assert fed.cloud_token_clouds(AWS) == []
    assert fed.cloud_token_clouds({**AWS, **ACTIONS, "AWS_ROLE_ARN": ""}) == []
    assert fed.cloud_token_clouds({**AWS, **ACTIONS, "AWS_ACCESS_KEY_ID": "AKIA"}) == []
    assert fed.cloud_token_clouds({**AWS, **ACTIONS, fed.DISABLE_VAR: "0"}) == []
    assert fed.cloud_token_clouds({**AWS, **ACTIONS, "CLAUDE_CODE_USE_BEDROCK": "0"}) == []


def test_start_uses_the_default_fetch_and_applies_to_every_env(monkeypatch):
    seen = []

    def fake(url, request_token, timeout=15.0, audience=None):
        seen.append(audience)
        return jwt(NOW + 600)

    monkeypatch.setattr(ci_client, "_fetch_oidc", fake)
    monkeypatch.setattr(fed.time, "time", lambda: NOW)
    tokens = ci_client.start_cloud_tokens({**AWS, **ACTIONS}, say=lambda m: None)
    try:
        assert seen == ["sts.amazonaws.com"]
        env, other = {}, {}
        tokens.apply(env, other)
        assert env == other and env["AWS_WEB_IDENTITY_TOKEN_FILE"].endswith("bedrock-token")
    finally:
        tokens.stop()


def test_a_failed_first_fetch_leaves_the_cloud_out(tmp_path):
    def boom(*a):
        raise fed.FederationError("HTTP 500")
    assert fed.start_cloud_tokens({**AWS, **ACTIONS}, fetch=boom) is None


def test_scrub_job_drops_the_actions_endpoint_but_gives_agents_the_cloud_env(monkeypatch):
    monkeypatch.setattr(ci_client, "_fetch_oidc", lambda *a, **k: jwt(NOW + 600))
    monkeypatch.setattr(fed.time, "time", lambda: NOW)
    env = {**AWS, **ACTIONS, "GITHUB_TOKEN": "x"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    tokens = ci_client.start_cloud_tokens(env, say=lambda m: None)
    try:
        ci_client.scrub_job(env, None, tokens)
        for e in (env, os.environ):
            assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in e and "ACTIONS_ID_TOKEN_REQUEST_URL" not in e
            assert e["AWS_WEB_IDENTITY_TOKEN_FILE"] and e["AWS_ROLE_SESSION_NAME"]
        assert REQ not in "".join(env.values())
    finally:
        tokens.stop()


# -- panes ----------------------------------------------------------------------------------------


KEPT = ("ANTHROPIC_FOUNDRY_AUTH_TOKEN", "CLAUDE_CODE_USE_MANTLE", "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "VERTEX_REGION_CLAUDE_4_5_SONNET",
        "ANTHROPIC_BEDROCK_REGION_PREFIX", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
        "AZURE_FEDERATED_TOKEN_FILE", "GOOGLE_APPLICATION_CREDENTIALS")


@pytest.mark.parametrize("name", KEPT)
def test_claude_panes_keep_the_newer_variables(name):
    assert name in secrets.provider_credentials("claude")
    assert name not in secrets.provider_credentials("codex")
    assert name not in secrets.pane_unset([name], keep=secrets.provider_credentials("claude"))


@pytest.mark.parametrize("name", ["ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"])
def test_the_actions_endpoint_stays_out_of_panes_whatever_is_kept(name):
    keep = secrets.provider_credentials("claude", name)
    assert name not in keep or name in secrets.pane_unset([name], keep=keep)
    assert name in secrets.pane_unset([name], keep=keep)
    assert name in secrets.scrub_secrets({name: "v"})


def test_mantle_counts_as_a_cloud_credential():
    assert "CLAUDE_CODE_USE_MANTLE" in ci_adapters.CLAUDE_CLOUD
    cred = ci_adapters.ClaudeAdapter().credential({"CLAUDE_CODE_USE_MANTLE": "1"})
    assert cred.kind == ci_adapters.CLOUD and cred.names == ("CLAUDE_CODE_USE_MANTLE",)


def test_managed_models_pin_the_haiku_default_not_the_deprecated_variable():
    cfg = ProviderConfig("bedrock", "us-east-1", ("opus-id", "haiku-id"), None)
    env = managed_models.provider_env(cfg, "claude")
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "haiku-id"
    assert "ANTHROPIC_SMALL_FAST_MODEL" not in env
