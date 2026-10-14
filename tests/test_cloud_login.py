"""Cloud login status readers and the expired-login classifier."""

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


def test_aws_sso_cache_expiry(tmp_path):
    cache = tmp_path / ".aws" / "sso" / "cache"
    cache.mkdir(parents=True)
    (cache / "a.json").write_text(json.dumps(
        {"accessToken": "SECRET", "expiresAt": "2027-01-15T08:00:00Z"}))
    (cache / "b.json").write_text(json.dumps({"startUrl": "x"}))   # no token: ignored
    env = {"AWS_PROFILE": "dev"}
    live = cloud_login.status("bedrock", env=env, home=tmp_path, now=1_790_000_000.0)
    assert live.ok and live.expires_at == 1_800_000_000.0 and "SECRET" not in repr(live)
    assert live.refresh_cmd == "aws sso login --profile dev"
    assert not cloud_login.status("bedrock", env=env, home=tmp_path, now=NOW + 1).ok


def test_azure_cli_token_expiry(tmp_path):
    (tmp_path / ".azure").mkdir()
    (tmp_path / ".azure" / "accessTokens.json").write_text(json.dumps(
        [{"accessToken": "SECRET", "expires_on": str(int(NOW))}]))
    assert cloud_login.status("foundry", home=tmp_path, now=NOW - 5).ok
    gone = cloud_login.status("foundry", home=tmp_path, now=NOW + 5)
    assert not gone.ok and gone.refresh_cmd == "az login"
    assert not cloud_login.status("foundry", home=tmp_path / "none", now=NOW).ok


@pytest.mark.parametrize("proc, ok", [
    (SimpleNamespace(returncode=0, stdout="ya29.SECRET\n", stderr=""), True),
    (SimpleNamespace(returncode=1, stdout="", stderr="invalid_grant: Bad Request invalid_rapt"), False),
])
def test_google_status_refreshes_through_gcloud(proc, ok, tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return proc

    s = cloud_login.status("vertex", home=tmp_path, now=NOW, run=fake_run)
    assert s.ok is ok and calls == [["gcloud", "auth", "application-default", "print-access-token"]]
    assert s.refresh_cmd == "gcloud auth application-default login"
    assert (s.expires_at is not None) is ok and "SECRET" not in repr(s)


def test_google_timeout_is_a_failed_login_not_an_exception(tmp_path):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    assert not cloud_login.status("vertex", home=tmp_path, run=slow).ok
