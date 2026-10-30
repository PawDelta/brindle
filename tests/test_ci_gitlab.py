"""GitLab as a brindle CI host (brindle Enterprise, ``ci_enterprise``): job env,
ID tokens, merge requests and notes through a fake GitLab API, the init
template, the entitlement gate and secret scrubbing."""

import base64
import json

import pytest

from brindle import ci_client, ci_hosts
from brindle.ci_client import CIError
from pro_fixtures import signing_key  # noqa: F401

API = "https://gitlab.example.com/api/v4"
JOB_ENV = {"GITLAB_CI": "true", "CI_PROJECT_PATH": "acme/sub/app", "CI_API_V4_URL": API,
           "CI_JOB_TOKEN": "jobtok"}


@pytest.fixture(autouse=True)
def clean_cloud_env(monkeypatch):
    for _, (_, _, use, ids) in ci_hosts.CLOUD_ID_TOKENS.items():
        monkeypatch.delenv(use, raising=False)
        for id_var in ids:
            monkeypatch.delenv(id_var, raising=False)


def jwt(exp=None) -> str:
    def b(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b({'alg': 'none'})}.{b({'exp': exp} if exp else {})}.sig"


class FakeGitLab:
    """Records requests; answers like the GitLab REST API."""

    def __init__(self, status=201):
        self.calls = []
        self.status = status

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if self.status not in (200, 201):
            return self.status, {"message": "401 Unauthorized"}
        if url.endswith("/merge_requests"):
            return 201, {"iid": 7, "web_url": "https://gitlab.example.com/acme/sub/app/-/merge_requests/7"}
        if url.endswith("/notes"):
            return 201, {"id": 99}
        return 404, {}


def host(api=None, entitled=True):
    return ci_hosts.get_host("gitlab", {}, is_entitled=lambda: entitled, request=api or FakeGitLab())


# -- detection and the entitlement gate ---------------------------------------------------------


@pytest.mark.brindle_ci_guard
def test_gitlab_fails_closed_without_the_entitlement():
    with pytest.raises(CIError) as e:
        ci_hosts.get_host(env=JOB_ENV, is_entitled=lambda: False)
    assert e.value.code == "not_entitled"

    def boom():
        raise RuntimeError("license store unreadable")

    with pytest.raises(CIError):
        ci_hosts.get_host("gitlab", {}, is_entitled=boom)


@pytest.mark.brindle_ci_guard
def test_entitled_uses_license_has(monkeypatch):
    seen = []
    monkeypatch.setattr(ci_hosts.license, "has", lambda f: seen.append(f) or False)
    assert ci_hosts.entitled() is False
    assert seen == ["ci_enterprise"]


@pytest.mark.brindle_ci_guard
def test_unknown_host_is_refused():
    with pytest.raises(CIError):
        ci_hosts.get_host("bitbucket", {})


# -- repo, fork ---------------------------------------------------------------------------------

def test_repo_from_job_env_with_subgroups():
    h = ci_hosts.GitLabHost
    assert h.repo(JOB_ENV) == "acme/sub/app"
    assert h.repo(JOB_ENV, "acme/sub/app") == "acme/sub/app"
    with pytest.raises(CIError):
        h.repo(JOB_ENV, "other/app")
    with pytest.raises(CIError):
        h.repo({})
    with pytest.raises(CIError):
        h.repo({"CI_PROJECT_PATH": "no-slash"})


# -- OIDC ---------------------------------------------------------------------------------------


def test_no_id_token_means_no_header_and_junk_is_ignored():
    assert ci_hosts.GitLabOIDC({}).header() == {}
    assert ci_hosts.GitLabOIDC({"BRINDLE_ID_TOKEN": "notajwt"}).header() == {}


@pytest.mark.brindle_ci_guard
def test_expired_id_token_is_not_sent():
    o = ci_hosts.GitLabOIDC({"BRINDLE_ID_TOKEN": jwt(exp=1000)}, clock=lambda: 2000.0)
    with pytest.raises(CIError) as e:
        o.header()
    assert e.value.code == "oidc"


def test_client_carries_the_gitlab_token():
    h = host()
    c = h.client({"BRINDLE_ID_TOKEN": jwt()})
    assert isinstance(c.oidc, ci_hosts.GitLabOIDC)
    assert ci_client.OIDC_HEADER in c.oidc.header()


# -- merge requests and notes -------------------------------------------------------------------


def test_project_token_is_preferred_over_the_job_token():
    api = FakeGitLab()
    host(api).comment({**JOB_ENV, "BRINDLE_GITLAB_TOKEN": "glpat-x"}, 1, "hi")
    assert api.calls[0][2]["PRIVATE-TOKEN"] == "glpat-x" and "JOB-TOKEN" not in api.calls[0][2]


def test_no_token_is_an_error():
    env = {k: v for k, v in JOB_ENV.items() if k != "CI_JOB_TOKEN"}
    with pytest.raises(CIError) as e:
        host().comment(env, 1, "hi")
    assert e.value.code == "gitlab_token"


def test_api_must_be_https():
    with pytest.raises(CIError):
        host().comment({**JOB_ENV, "CI_API_V4_URL": "http://gitlab.example.com/api/v4"}, 1, "hi")


@pytest.mark.brindle_ci_guard
def test_api_failure_is_reported_without_the_token():
    with pytest.raises(CIError) as e:
        host(FakeGitLab(status=401)).comment({**JOB_ENV, "CI_JOB_TOKEN": "s3cret-job-token"}, 1, "hi")
    assert "401" in str(e.value) and "s3cret-job-token" not in str(e.value)
    assert e.value.code == "http_401"


# -- init ---------------------------------------------------------------------------------------


@pytest.mark.brindle_ci_guard
def test_init_refuses_without_entitlement_and_never_overwrites(tmp_path):
    with pytest.raises(CIError):
        ci_hosts.init_gitlab(cwd=str(tmp_path), is_entitled=lambda: False)
    assert not (tmp_path / ".gitlab-ci.yml").exists()
    (tmp_path / ".gitlab-ci.yml").write_text("mine\n")
    with pytest.raises(CIError):
        ci_hosts.init_gitlab(cwd=str(tmp_path), is_entitled=lambda: True)
    assert (tmp_path / ".gitlab-ci.yml").read_text() == "mine\n"
    ci_hosts.init_gitlab(cwd=str(tmp_path), is_entitled=lambda: True, force=True, say=lambda s: None)
    assert (tmp_path / ".gitlab-ci.yml").read_text() == ci_hosts.GITLAB_TEMPLATE


@pytest.mark.brindle_ci_guard
def test_cli_init_host_gitlab_fails_closed(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ci_hosts, "entitled", lambda: False)
    r = CliRunner().invoke(app, ["ci", "init", "--host", "gitlab"])
    assert r.exit_code != 0 and not (tmp_path / ".gitlab-ci.yml").exists()
    monkeypatch.setattr(ci_hosts, "entitled", lambda: True)
    r = CliRunner().invoke(app, ["ci", "init", "--host", "gitlab"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / ".gitlab-ci.yml").exists()


# -- a GitLab job drives start and run --------------------------------------------------------


def test_cli_init_host_is_case_insensitive_and_force_is_gitlab_only(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ci_hosts, "entitled", lambda: True)
    assert CliRunner().invoke(app, ["ci", "init", "--host", "GitLab"]).exit_code == 0
    assert CliRunner().invoke(app, ["ci", "init", "--force"]).exit_code != 0
