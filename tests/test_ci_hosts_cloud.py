"""Keyless cloud sign-in on GitLab (brindle Enterprise): the template's
``id_tokens`` per cloud, the token file written once and never refreshed, the
lifetime warning, and the token staying out of agent panes."""

from __future__ import annotations

import base64
import json
import os
import stat

import pytest

from brindle import ci_client, ci_federation as fed, ci_hosts, secrets
from brindle.ci_client import CIError

NOW = 1_000_000.0
AWS = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/brindle", "AWS_REGION": "us-east-1"}
PROVIDER = "projects/12/locations/global/workloadIdentityPools/p/providers/gl"
GCP = {"CLAUDE_CODE_USE_VERTEX": "1", "GCP_WORKLOAD_IDENTITY_PROVIDER": PROVIDER,
       "GCP_SERVICE_ACCOUNT": "brindle@proj-12345.iam.gserviceaccount.com",
       "ANTHROPIC_VERTEX_PROJECT_ID": "proj-12345", "CLOUD_ML_REGION": "us-east5"}
AZURE = {"CLAUDE_CODE_USE_FOUNDRY": "1", "AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
         "AZURE_TENANT_ID": "22222222-2222-2222-2222-222222222222", "ANTHROPIC_FOUNDRY_RESOURCE": "res"}
CLOUDS = [("bedrock", AWS, "sts.amazonaws.com", "BRINDLE_AWS_ID_TOKEN"),
          ("vertex", GCP, "//iam.googleapis.com/$GCP_WORKLOAD_IDENTITY_PROVIDER", "BRINDLE_GCP_ID_TOKEN"),
          ("foundry", AZURE, "api://AzureADTokenExchange", "BRINDLE_AZURE_ID_TOKEN")]


def jwt(exp: float, tag: str = "a") -> str:
    def b(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b({'alg': 'none'})}.{b({'exp': exp, 'jti': tag})}.sig"


def job_env(ids, var, token):
    return {**ids, "GITLAB_CI": "true", var: token}


# -- the rendered template ---------------------------------------------------------------------


@pytest.mark.parametrize("cloud,ids,aud,var", CLOUDS)
def test_template_declares_the_clouds_id_token_and_use_variable(cloud, ids, aud, var):
    text = ci_hosts.render_template(cloud)
    assert f"    {var}:\n      aud: {aud}\n" in text
    use = next(k for k in ids if k.startswith("CLAUDE_CODE_USE_"))
    assert f'    {use}: "1"\n' in text
    # the brindle token is still there, and only this cloud's entry is
    assert "BRINDLE_ID_TOKEN:" in text and ci_client.OIDC_AUDIENCE in text
    for other in ("BRINDLE_AWS_ID_TOKEN", "BRINDLE_GCP_ID_TOKEN", "BRINDLE_AZURE_ID_TOKEN"):
        assert (other in text) == (other == var)
    for name in ids:   # the CI/CD variables to set are named, none has a value
        if not name.startswith("CLAUDE_CODE_USE_"):
            assert name in text
    assert all(v not in text for v in ids.values() if len(v) > 8)


def test_template_without_a_cloud_is_unchanged():
    assert ci_hosts.render_template() == ci_hosts.GITLAB_TEMPLATE
    assert "BRINDLE_AWS_ID_TOKEN" not in ci_hosts.GITLAB_TEMPLATE
    with pytest.raises(CIError):
        ci_hosts.render_template("nope")


@pytest.mark.parametrize("cloud,ids,aud,var", CLOUDS)
def test_template_comment_warns_about_the_token_lifetime(cloud, ids, aud, var):
    text = ci_hosts.render_template(cloud)
    assert "never refreshes" in text and "lifetime must cover the job timeout" in text


@pytest.mark.parametrize("cloud,ids,aud,var", CLOUDS)
def test_the_cloud_is_selected_by_the_cicd_variables(cloud, ids, aud, var):
    assert ci_hosts.cloud_from_env(ids) == cloud
    only_ids = {k: v for k, v in ids.items() if not k.startswith("CLAUDE_CODE_USE_")}
    assert ci_hosts.cloud_from_env(only_ids) == cloud
    assert ci_hosts.cloud_from_env({}) is None


@pytest.mark.parametrize("cloud,ids,aud,var", CLOUDS)
def test_init_writes_the_cloud_template_and_warns(cloud, ids, aud, var, tmp_path):
    said = []
    path = ci_hosts.init_gitlab(cwd=str(tmp_path), is_entitled=lambda: True, say=said.append, env=ids,
                                preview=True)   # vertex and foundry are in preview (bedrock ignores it)
    assert path.read_text() == ci_hosts.render_template(cloud)
    assert any("lifetime must cover the job timeout" in s for s in said)


@pytest.mark.parametrize("cloud,ids", [("vertex", CLOUDS[1][1]), ("foundry", CLOUDS[2][1])])
def test_gitlab_refuses_a_preview_cloud_without_the_flag(tmp_path, cloud, ids):
    with pytest.raises(CIError, match="in preview"):
        ci_hosts.init_gitlab(cwd=str(tmp_path), is_entitled=lambda: True, say=lambda s: None, env=ids)
    assert not (tmp_path / ci_hosts.GITLAB_CI_FILE).exists(), "nothing is written when refused"


def test_init_with_no_cloud_writes_the_plain_template(tmp_path):
    path = ci_hosts.init_gitlab(cwd=str(tmp_path), is_entitled=lambda: True, say=lambda s: None, env={})
    assert path.read_text() == ci_hosts.GITLAB_TEMPLATE


# -- the token file, written once --------------------------------------------------------------


@pytest.mark.parametrize("cloud,ids,aud,var", CLOUDS)
def test_the_job_token_is_written_once_to_a_0600_file_and_never_refreshed(cloud, ids, aud, var, monkeypatch):
    token = jwt(NOW + 3600)
    fetches = []
    monkeypatch.setattr(ci_client, "_fetch_oidc", lambda *a, **k: fetches.append(a) or "x")
    tokens = fed.start_cloud_tokens(job_env(ids, var, token))
    try:
        (r,) = tokens.refreshers
        assert isinstance(r, fed.StaticCloudToken) and r.cloud == cloud
        assert open(r.token_file).read() == token
        assert stat.S_IMODE(os.stat(r.token_file).st_mode) == 0o600
        assert r._thread is None            # no refresh loop
        assert fetches == []                # nothing fetched: GitLab has no endpoint to ask
        assert r._pending is None           # the token isn't kept in memory
    finally:
        tokens.stop()


def test_a_static_token_is_not_rewritten(tmp_path):
    r = fed.StaticCloudToken("bedrock", AWS, jwt(NOW), directory=str(tmp_path))
    r.start()
    with open(r.token_file, "w") as f:
        f.write("marker")
    r.stop()
    assert open(r.token_file).read() == "marker"
    assert not hasattr(r, "tick") or r._thread is None


@pytest.mark.parametrize("cloud,ids,aud,var", CLOUDS)
def test_agents_get_the_cloud_env_and_never_the_token(cloud, ids, aud, var):
    token = jwt(NOW + 3600, "secret-jti")
    tokens = fed.start_cloud_tokens(job_env(ids, var, token))
    try:
        env = tokens.agent_env()
        assert token not in "".join(env.values())
        (r,) = tokens.refreshers
        if cloud == "bedrock":
            assert env["AWS_WEB_IDENTITY_TOKEN_FILE"] == r.token_file and env["AWS_ROLE_ARN"] == ids["AWS_ROLE_ARN"]
        elif cloud == "foundry":
            assert env["AZURE_FEDERATED_TOKEN_FILE"] == r.token_file and env["AZURE_CLIENT_ID"] == ids["AZURE_CLIENT_ID"]
        else:
            config = json.load(open(env["GOOGLE_APPLICATION_CREDENTIALS"]))
            assert config["credential_source"]["file"] == r.token_file
            assert config["audience"] == "//iam.googleapis.com/" + PROVIDER
            assert token not in json.dumps(config)
    finally:
        tokens.stop()


def test_no_cloud_without_the_token_gitlab_a_selected_cloud_or_with_a_static_credential():
    t = jwt(NOW + 3600)
    assert fed.gitlab_token_clouds({**AWS, "GITLAB_CI": "true"}) == []                       # no token
    assert fed.gitlab_token_clouds({**AWS, "BRINDLE_AWS_ID_TOKEN": t}) == []                 # not GitLab
    assert fed.gitlab_token_clouds({"GITLAB_CI": "true", "BRINDLE_AWS_ID_TOKEN": t}) == []    # no cloud selected
    assert fed.gitlab_token_clouds(job_env({**AWS, "AWS_ACCESS_KEY_ID": "AKIA"}, "BRINDLE_AWS_ID_TOKEN", t)) == []
    assert fed.gitlab_token_clouds(job_env({**AWS, fed.DISABLE_VAR: "0"}, "BRINDLE_AWS_ID_TOKEN", t)) == []
    assert fed.gitlab_token_clouds(job_env(AWS, "BRINDLE_AWS_ID_TOKEN", t)) == ["bedrock"]
    # GitHub's behaviour is untouched: it still needs the Actions endpoint
    assert fed.cloud_token_clouds(job_env(AWS, "BRINDLE_AWS_ID_TOKEN", t)) == []


def test_run_says_the_token_is_not_refreshed():
    said = []
    tokens = ci_client.start_cloud_tokens(job_env(AWS, "BRINDLE_AWS_ID_TOKEN", jwt(NOW + 3600)), say=said.append)
    try:
        assert any("not refreshed" in s for s in said)
    finally:
        tokens.stop()


# -- the lifetime warning in doctor ------------------------------------------------------------


@pytest.mark.parametrize("cloud,ids,aud,var", CLOUDS)
def test_doctor_warns_that_the_id_token_lifetime_must_cover_the_job_timeout(cloud, ids, aud, var):
    lines = ci_client.cloud_status(job_env(ids, var, jwt(NOW + 3600)))
    assert any(l.startswith("warning:") and "lifetime must cover the job timeout" in l for l in lines)
    assert not any(jwt(NOW + 3600) in l for l in lines)


def test_doctor_compares_the_token_with_the_job_timeout(monkeypatch):
    monkeypatch.setattr(ci_client.time, "time", lambda: NOW)
    env = {**job_env(AWS, "BRINDLE_AWS_ID_TOKEN", jwt(NOW + 300)), "CI_JOB_TIMEOUT": "3600"}
    (warn,) = [l for l in ci_client.cloud_status(env) if l.startswith("warning:")]
    assert "300s left" in warn and "3600s" in warn
    env["CI_JOB_TIMEOUT"] = "200"
    (warn,) = [l for l in ci_client.cloud_status(env) if l.startswith("warning:")]
    assert "left" not in warn


def test_doctor_has_no_gitlab_warning_off_gitlab_or_without_a_token():
    assert not any("warning:" in l for l in ci_client.cloud_status(AWS))
    assert not any("warning:" in l for l in ci_client.cloud_status({**AWS, "GITLAB_CI": "true"}))


# -- panes -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["BRINDLE_AWS_ID_TOKEN", "BRINDLE_GCP_ID_TOKEN", "BRINDLE_AZURE_ID_TOKEN"])
def test_the_id_token_variables_stay_out_of_panes(name):
    assert name in secrets.scrub_secrets({name: "v"})
    for provider in ("claude", "codex", "antigravity"):
        keep = secrets.provider_credentials(provider, name)
        assert name in secrets.pane_unset([name], keep=keep)


@pytest.mark.parametrize("cloud,ids,aud,var", CLOUDS)
def test_scrub_job_drops_the_token_but_keeps_the_token_file_env(cloud, ids, aud, var, monkeypatch):
    token = jwt(NOW + 3600, "pane-secret")
    env = job_env(ids, var, token)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    tokens = fed.start_cloud_tokens(env)
    try:
        ci_client.scrub_job(env, None, tokens)
        for e in (env, os.environ):
            assert var not in e
            assert token not in "".join(e.get(k, "") for k in e)
        assert any(k in env for k in ("AWS_WEB_IDENTITY_TOKEN_FILE", "GOOGLE_APPLICATION_CREDENTIALS",
                                      "AZURE_FEDERATED_TOKEN_FILE"))
    finally:
        tokens.stop()
