"""GitLab as a brindle CI host (brindle Enterprise, ``ci_enterprise``): job env,
ID tokens, merge requests and notes through a fake GitLab API, the init
template, the entitlement gate and secret scrubbing."""

import base64
import json
import os
import urllib.parse

import pytest

from brindle import ci_client, ci_hosts, secrets
from brindle.ci_client import CIError
from pro_fixtures import signing_key  # noqa: F401
from test_ci_client import CI_TOKEN, FakeAdapter, Server, ci_repo, head, plan  # noqa: F401 - fixtures

API = "https://gitlab.example.com/api/v4"
JOB_ENV = {"GITLAB_CI": "true", "CI_PROJECT_PATH": "acme/sub/app", "CI_API_V4_URL": API,
           "CI_JOB_TOKEN": "jobtok"}


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

def test_detect_gitlab_job():
    assert ci_hosts.GitLabHost.detect(JOB_ENV)
    assert not ci_hosts.GitLabHost.detect({"GITHUB_ACTIONS": "true"})
    assert ci_hosts.GitHubHost.detect({"GITHUB_ACTIONS": "true"})


def test_get_host_picks_by_env_and_gates_gitlab(monkeypatch):
    assert ci_hosts.get_host(env={"GITHUB_ACTIONS": "true"}).name == "github"
    assert ci_hosts.get_host(env={}).name == "github"
    monkeypatch.setattr(ci_hosts, "entitled", lambda: True)
    assert ci_hosts.get_host(env=JOB_ENV, is_entitled=lambda: True).name == "gitlab"


def test_gitlab_fails_closed_without_the_entitlement():
    with pytest.raises(CIError) as e:
        ci_hosts.get_host(env=JOB_ENV, is_entitled=lambda: False)
    assert e.value.code == "not_entitled"

    def boom():
        raise RuntimeError("license store unreadable")

    with pytest.raises(CIError):
        ci_hosts.get_host("gitlab", {}, is_entitled=boom)


def test_entitled_uses_license_has(monkeypatch):
    seen = []
    monkeypatch.setattr(ci_hosts.license, "has", lambda f: seen.append(f) or False)
    assert ci_hosts.entitled() is False
    assert seen == ["ci_enterprise"]


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


def test_fork_is_a_merge_request_from_another_project():
    h = ci_hosts.GitLabHost
    assert h.is_fork({"CI_MERGE_REQUEST_SOURCE_PROJECT_PATH": "me/app"}, "acme/app")
    assert not h.is_fork({"CI_MERGE_REQUEST_SOURCE_PROJECT_PATH": "ACME/app"}, "acme/app")
    assert not h.is_fork({}, "acme/app")


# -- OIDC ---------------------------------------------------------------------------------------

def test_id_token_header_prefers_the_id_tokens_variable():
    a, b = jwt(), jwt()
    o = ci_hosts.GitLabOIDC({"BRINDLE_ID_TOKEN": a, "CI_JOB_JWT_V2": b})
    assert o.available and o.header() == {ci_client.OIDC_HEADER: a}
    assert ci_hosts.GitLabOIDC({"CI_JOB_JWT_V2": b}).header() == {ci_client.OIDC_HEADER: b}


def test_no_id_token_means_no_header_and_junk_is_ignored():
    assert ci_hosts.GitLabOIDC({}).header() == {}
    assert ci_hosts.GitLabOIDC({"BRINDLE_ID_TOKEN": "notajwt"}).header() == {}


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

def test_create_merge_request():
    api = FakeGitLab()
    mr = host(api).create_merge_request(JOB_ENV, source="brindle/fix", target="main", title="Fix",
                                        description="body")
    assert mr == {"iid": 7, "url": "https://gitlab.example.com/acme/sub/app/-/merge_requests/7"}
    method, url, headers, body = api.calls[0]
    assert method == "POST"
    assert url == f"{API}/projects/{urllib.parse.quote('acme/sub/app', safe='')}/merge_requests"
    assert headers["JOB-TOKEN"] == "jobtok" and "PRIVATE-TOKEN" not in headers
    assert body["source_branch"] == "brindle/fix" and body["target_branch"] == "main"


def test_comment_on_a_merge_request():
    api = FakeGitLab()
    assert host(api).comment(JOB_ENV, 7, "looks good") == "99"
    method, url, _, body = api.calls[0]
    assert url.endswith("/acme%2Fsub%2Fapp/merge_requests/7/notes") and body == {"body": "looks good"}


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


def test_api_failure_is_reported_without_the_token():
    with pytest.raises(CIError) as e:
        host(FakeGitLab(status=401)).comment({**JOB_ENV, "CI_JOB_TOKEN": "s3cret-job-token"}, 1, "hi")
    assert "401" in str(e.value) and "s3cret-job-token" not in str(e.value)
    assert e.value.code == "http_401"


# -- init ---------------------------------------------------------------------------------------

def test_init_writes_the_template(tmp_path):
    said = []
    path = ci_hosts.init_gitlab(cwd=str(tmp_path), is_entitled=lambda: True, say=said.append)
    text = path.read_text()
    assert path.name == ".gitlab-ci.yml" and text == ci_hosts.GITLAB_TEMPLATE
    assert "id_tokens:" in text and "BRINDLE_ID_TOKEN" in text and ci_client.OIDC_AUDIENCE in text
    assert "brindle ci run" in text
    assert any("BRINDLE_PRO_TOKEN" in s for s in said)


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

def test_gitlab_job_runs_a_validation_end_to_end(plan, ci_repo, tmp_path, monkeypatch):
    nested = "acme/sub/widgets"
    sha = head(ci_repo)
    vplan = plan("validation", head_sha=sha, repo=nested)
    server = Server(plan(repo=nested), validation_plan=vplan)
    env = {"GITLAB_CI": "true", "CI_PROJECT_PATH": nested, "CI_API_V4_URL": API, "CI_JOB_TOKEN": "jobtok",
           "BRINDLE_PRO_TOKEN": CI_TOKEN, "PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}
    h = host(FakeGitLab())
    assert h.repo(env) == nested
    out = tmp_path / "out"
    assert ci_client.start(nested, {"kind": "validate", "pr": 7, "head_sha": sha, "fork": False}, out,
                           client=server.client, token=CI_TOKEN, providers=["claude"], say=lambda s: None) == 0
    result = ci_client.run((out / "plan.jwt").read_text(), out / "run_token", cwd=str(ci_repo), env=env,
                           client=server.client, adapters={"claude": FakeAdapter("claude")}, org=h.org_hint,
                           repo=h.repo(env), say=lambda s: None)
    assert result["status"] == "posted"
    assert "CI_JOB_TOKEN" not in env and "BRINDLE_PRO_TOKEN" not in env   # scrubbed before the checks
    assert ci_client.plan_id((out / "plan.jwt").read_text())[2] == nested   # report accepts a nested path


def test_tokens_are_captured_before_the_scrub():
    api = FakeGitLab()
    env = dict(JOB_ENV)
    h = ci_hosts.get_host("gitlab", env, is_entitled=lambda: True, request=api)
    secrets.scrub_secrets(env)
    assert "CI_JOB_TOKEN" not in env
    h.comment(env, 3, "after the scrub")
    assert api.calls[0][2]["JOB-TOKEN"] == "jobtok"


def test_template_reports_the_job_status(tmp_path):
    assert "brindle ci report" in ci_hosts.GITLAB_TEMPLATE and "CI_JOB_STATUS" in ci_hosts.GITLAB_TEMPLATE


def test_cli_init_host_is_case_insensitive_and_force_is_gitlab_only(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ci_hosts, "entitled", lambda: True)
    assert CliRunner().invoke(app, ["ci", "init", "--host", "GitLab"]).exit_code == 0
    assert CliRunner().invoke(app, ["ci", "init", "--force"]).exit_code != 0


# -- secrets ------------------------------------------------------------------------------------

def test_gitlab_job_secrets_are_scrubbed():
    env = {"CI_JOB_TOKEN": "a", "CI_JOB_JWT_V2": "b", "BRINDLE_ID_TOKEN": "c", "BRINDLE_GITLAB_TOKEN": "d",
           "CI_REGISTRY_PASSWORD": "e", "CI_PROJECT_PATH": "acme/app", "PATH": "/bin"}
    secrets.scrub_secrets(env)
    assert env == {"CI_PROJECT_PATH": "acme/app", "PATH": "/bin"}
    assert all(not ci_client.check_env({n: "x"}) == {n: "x"} for n in ("CI_JOB_TOKEN", "BRINDLE_ID_TOKEN"))
    assert "CI_JOB_TOKEN" in secrets.pane_unset(["PATH"])
    extra = {"CI_DEPENDENCY_PROXY_PASSWORD": "x", "CI_BUILD_TOKEN": "y"}
    secrets.scrub_secrets(extra)
    assert extra == {}
