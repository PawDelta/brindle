"""Cloud login status readers and the expired-login classifier."""

import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from brindle import cloud_login, model_access

NOW = 1_800_000_000.0

VERTEX_ERROR = ('API Error: Could not load Google Cloud credentials · '
                '{"error":"invalid_grant","error_description":"reauth related error (invalid_rapt)",'
                '"error_subtype":"invalid_rapt"}')


@pytest.mark.parametrize("text", [
    VERTEX_ERROR,
    "An error occurred (ExpiredToken) when calling InvokeModel: The security token included "
    "in the request is expired",
    "AADSTS700082: The refresh token has expired due to inactivity.",
])
def test_expired_logins_are_their_own_cause_and_never_a_refusal(text, monkeypatch, tmp_path):
    monkeypatch.setenv("BRINDLE_HOME", str(tmp_path))
    assert model_access.expired_login(text) is model_access.EXPIRED_LOGIN
    assert model_access.classify(text) is None
    model_access.refuse("p", model_access.EXPIRED_LOGIN, fp="fp")
    assert not (tmp_path / "model_refusals.json").exists()


def _sso_token(cache, key, expires):
    (cache / (hashlib.sha1(key.encode()).hexdigest() + ".json")).write_text(
        json.dumps({"accessToken": "SECRET", "expiresAt": expires}))


def test_aws_reads_only_the_selected_profiles_sso_session(tmp_path):
    cache = tmp_path / ".aws" / "sso" / "cache"
    cache.mkdir(parents=True)
    (tmp_path / ".aws" / "config").write_text(
        "[profile dev]\nsso_session = dev-sess\n\n[profile old]\nsso_start_url = https://old.example\n")
    _sso_token(cache, "dev-sess", "2027-01-15T08:00:00Z")        # = 1_800_000_000
    _sso_token(cache, "other-sess", "2099-01-01T00:00:00Z")      # unrelated, live
    env = {"AWS_PROFILE": "dev", "AWS_CONFIG_FILE": str(tmp_path / ".aws" / "config")}
    live = cloud_login.status("bedrock", env=env, home=tmp_path, now=NOW - 10)
    assert live.ok and live.expires_at == NOW and "SECRET" not in repr(live)
    assert live.refresh_cmd == "aws sso login --profile dev"
    assert not cloud_login.status("bedrock", env=env, home=tmp_path, now=NOW + 1).ok   # other is live
    assert not cloud_login.status("bedrock", env={**env, "AWS_PROFILE": "old"}, home=tmp_path, now=NOW).ok
    # AWS_DEFAULT_PROFILE selects dev (expired) beside a live default session
    (tmp_path / ".aws" / "config").write_text(
        "[default]\nsso_session = def-sess\n\n[profile dev]\nsso_session = dev-sess\n")
    _sso_token(cache, "def-sess", "2099-01-01T00:00:00Z")
    alt = {"AWS_DEFAULT_PROFILE": "dev", "AWS_CONFIG_FILE": env["AWS_CONFIG_FILE"]}
    s = cloud_login.status("bedrock", env=alt, home=tmp_path, now=NOW + 1)
    assert not s.ok and s.expired and s.refresh_cmd == "aws sso login --profile dev"
    # refresh: any AWS CLI call renews the SSO token from the sso-session's refresh token

    def aws(stderr):
        return lambda cmd, **kw: SimpleNamespace(returncode=255, stdout="", stderr=stderr)

    # offline: the token is past its time but the session never said it lapsed
    offline = aws("Could not connect to the endpoint URL: \"https://portal.sso.us-east-1.amazonaws.com\"")
    assert not cloud_login.refresh("bedrock", env=alt, home=tmp_path, now=NOW + 1, run=offline).expired
    lapsed = aws("Error when retrieving token from sso: Token has expired and refresh failed")
    assert cloud_login.refresh("bedrock", env=alt, home=tmp_path, now=NOW + 1, run=lapsed).expired

    def renew(cmd, **kw):
        assert cmd[:4] == ["aws", "sts", "get-caller-identity", "--profile"] and cmd[4] == "dev"
        _sso_token(cache, "dev-sess", "2099-01-01T00:00:00Z")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    assert cloud_login.refresh("bedrock", env=alt, home=tmp_path, now=NOW + 1, run=renew).ok


AZ_TOKEN = json.dumps({"accessToken": "SECRET", "expiresOn": "2027-01-15 08:00:00.000000",
                       "expires_on": int(NOW), "tokenType": "Bearer"})


@pytest.mark.parametrize("proc, now, ok", [
    (SimpleNamespace(returncode=0, stdout=AZ_TOKEN, stderr=""), NOW - 5, True),
    (SimpleNamespace(returncode=0, stdout=AZ_TOKEN, stderr=""), NOW + 5, False),
    (SimpleNamespace(returncode=1, stdout="", stderr="AADSTS70043: refresh token expired"), NOW, False),
])
def test_azure_expiry_comes_from_the_cli_token(proc, now, ok):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["env"]["AZURE_CONFIG_DIR"]))
        return proc

    s = cloud_login.status("foundry", env={"AZURE_CONFIG_DIR": "/prof"}, now=now, run=fake_run)
    assert s.ok is ok and s.refresh_cmd == "az login" and "SECRET" not in repr(s)
    assert calls == [(["az", "account", "get-access-token", "--output", "json"], "/prof")]


@pytest.mark.parametrize("proc, ok", [
    (SimpleNamespace(returncode=0, stdout="ya29.SECRET\n", stderr=""), True),
    (SimpleNamespace(returncode=1, stdout="", stderr="invalid_grant: Bad Request invalid_rapt"), False),
])
def test_google_status_refreshes_through_gcloud_in_the_given_env(proc, ok, tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["env"]["GOOGLE_APPLICATION_CREDENTIALS"]))
        return proc

    seen = []

    def fake_expiry(token):
        seen.append(token)
        return 1800.0       # an impersonated token: not the usual hour

    env = {"GOOGLE_APPLICATION_CREDENTIALS": "/profile/adc.json"}
    s = cloud_login.status("vertex", env=env, home=tmp_path, now=NOW, run=fake_run, expiry=fake_expiry)
    assert s.ok is ok and calls == [(["gcloud", "auth", "application-default", "print-access-token"],
                                     "/profile/adc.json")]
    assert s.refresh_cmd == "gcloud auth application-default login" and "SECRET" not in repr(s)
    assert s.expires_at == (NOW + 1800.0 if ok else None)
    assert bool(seen) is ok


@pytest.mark.parametrize("provider, stderr", [
    # after `gcloud auth application-default revoke`
    ("vertex", "ERROR: (gcloud.auth.application-default.print-access-token) "
               "Your default credentials were not found."),
    # after `aws sso logout`: no cached token
    ("bedrock", "Error loading SSO Token: Token for dev-sess does not exist"),
    # after `az logout`
    ("foundry", "ERROR: Please run 'az login' to setup account."),
])
def test_signed_out_never_prompts(provider, stderr, tmp_path):
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "config").write_text("[profile dev]\nsso_session = dev-sess\n")
    env = {"AWS_PROFILE": "dev", "AWS_CONFIG_FILE": str(tmp_path / ".aws" / "config"),
           "CLOUDSDK_CONFIG": str(tmp_path / "gcloud"), "AZURE_CONFIG_DIR": str(tmp_path / "azure")}
    failed = SimpleNamespace(returncode=1, stdout="", stderr=stderr)

    def browser(cmd, **kw):
        raise AssertionError(f"opened a sign-in: {cmd}")

    how = cloud_login.sign_in(provider, env=env, home=tmp_path, run=lambda cmd, **kw: failed,
                              popen=browser)
    assert how == "no lapse"


def test_google_timeout_is_a_failed_login_not_an_exception(tmp_path):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    assert not cloud_login.status("vertex", home=tmp_path, run=slow).ok
