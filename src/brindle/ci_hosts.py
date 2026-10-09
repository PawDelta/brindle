"""Where brindle CI runs: a small host layer over the CI platform.

Today's behavior is GitHub Actions (:class:`GitHubHost` wraps it unchanged:
``GITHUB_REPOSITORY``, the Actions OIDC token, ``gh``). :class:`GitLabHost`
runs the same client in a GitLab CI job (brindle Enterprise, the
``ci_enterprise`` feature): the job's ``CI_*`` variables, an ID token for
OIDC, and merge requests and comments through the GitLab REST API.

A GitLab host is never built without the entitlement: :func:`get_host` checks
it with :func:`brindle.pro.license.has`, which is false on anything it can't
verify (fail closed). Tokens are read once from the environment, before the
job's secrets are scrubbed (:mod:`brindle.secrets`), and never logged or put
in exception messages.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Callable, Mapping

from brindle import ci_client, ci_federation
from brindle.ci_client import CIError
from brindle.pro import auth, license

GITHUB = "github"
GITLAB = "gitlab"
HOSTS = (GITHUB, GITLAB)
FEATURE = "ci_enterprise"
GITLAB_PATH_RE = ci_client.PROJECT_RE   # group[/subgroup...]/project
GITLAB_CI_FILE = ".gitlab-ci.yml"
ID_TOKEN_ENV = "BRINDLE_ID_TOKEN"        # the id_tokens: variable of the template
PROJECT_TOKEN_ENV = "BRINDLE_GITLAB_TOKEN"   # a project access token (api scope), preferred over CI_JOB_TOKEN
API_MAX_RESPONSE = 1024 * 1024
API_TIMEOUT = 30.0

# (method, url, headers, json body or None) -> (status, parsed JSON object)
Request = Callable[[str, str, dict, "dict | None"], "tuple[int, dict]"]


def _urllib_request(method: str, url: str, headers: dict, body: dict | None) -> tuple[int, dict]:
    import urllib.error
    import urllib.request

    data = None if body is None else json.dumps(body).encode()
    hdrs = dict(headers)
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            status, raw = resp.status, resp.read(API_MAX_RESPONSE)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(64 * 1024)
    except (urllib.error.URLError, OSError, ValueError) as e:
        why = type(getattr(e, "reason", None) or e).__name__
        raise CIError(f"couldn't reach the GitLab API ({why})", code="gitlab_api") from None
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {"items": parsed}


class GitHubHost:
    """GitHub Actions: today's behavior, unchanged."""

    name = GITHUB
    setup_dir = ci_client.SETUP_DIR
    org_hint = None   # look the owner up (event payload, gh)

    @staticmethod
    def detect(env: Mapping[str, str]) -> bool:
        return env.get("GITHUB_ACTIONS") == "true"

    @staticmethod
    def repo(env: Mapping[str, str], given: str | None = None) -> str:
        return ci_client.github_repo(env, given)

    @staticmethod
    def oidc(env: Mapping[str, str] | None = None) -> ci_client.OIDC:
        return ci_client.OIDC(env)

    @staticmethod
    def is_fork(env: Mapping[str, str], repo: str) -> bool:
        return ci_client.pr_is_fork(env, repo)

    def client(self, env: Mapping[str, str] | None = None, **kw) -> ci_client.Client:
        return ci_client.Client(oidc=self.oidc(env), env=env, **kw)


class GitLabOIDC:
    """The ``X-Brindle-OIDC`` header on GitLab: the job's ID token. GitLab
    hands the token to the job as a variable (``id_tokens:`` in the template
    names it ``BRINDLE_ID_TOKEN`` with brindle's audience; the older
    ``CI_JOB_JWT_V2`` is the fallback), so nothing is fetched. Read once at
    construction, before the secrets are scrubbed. A token past its ``exp``
    isn't sent."""

    def __init__(self, env: Mapping[str, str] | None = None, clock=None) -> None:
        import os
        import time

        env = os.environ if env is None else env
        self._token = env.get(ID_TOKEN_ENV) or env.get("CI_JOB_JWT_V2") or None
        self._clock = clock or time.time

    @property
    def available(self) -> bool:
        return bool(self._token and self._token.count(".") == 2)

    def header(self) -> dict[str, str]:
        if not self.available:
            return {}
        exp = ci_client._jwt_exp(self._token)
        if exp is not None and exp - ci_client.OIDC_EXP_MARGIN <= self._clock():
            raise CIError("the GitLab ID token has expired", code="oidc")
        return {ci_client.OIDC_HEADER: self._token}


class GitLabHost:
    """A GitLab CI job (brindle Enterprise). ``request`` is the REST
    transport (replaced in tests)."""

    name = GITLAB
    setup_dir = "."

    org_hint = True   # no `gh` lookup of the owner; a personal subscription credential stays refused

    def __init__(self, request: Request | None = None, env: Mapping[str, str] | None = None) -> None:
        self._request = request or _urllib_request
        # Read once, before ci_client.run scrubs the job's secrets from the environment.
        self._env = dict(env or {})
        self._secrets = {k: self._env.get(k) or "" for k in (PROJECT_TOKEN_ENV, "CI_JOB_TOKEN")}

    def _merged(self, env: Mapping[str, str]) -> dict:
        """``env`` with the secrets captured at construction filled back in."""
        out = {**self._env, **env}
        for k, v in self._secrets.items():
            if v and not out.get(k):
                out[k] = v
        return out

    @staticmethod
    def detect(env: Mapping[str, str]) -> bool:
        return env.get("GITLAB_CI") == "true"

    @staticmethod
    def repo(env: Mapping[str, str], given: str | None = None) -> str:
        repo = env.get("CI_PROJECT_PATH") or given
        if not repo:
            raise CIError("no repository: pass --repo or set CI_PROJECT_PATH")
        if given and repo != given:
            raise CIError(f"--repo {given} isn't this job's project ({repo})")
        if not GITLAB_PATH_RE.match(repo):
            raise CIError("project must be group/name")
        return repo

    @staticmethod
    def oidc(env: Mapping[str, str] | None = None) -> GitLabOIDC:
        return GitLabOIDC(env)

    @staticmethod
    def is_fork(env: Mapping[str, str], repo: str) -> bool:
        """A merge request whose source project isn't this one."""
        source = env.get("CI_MERGE_REQUEST_SOURCE_PROJECT_PATH")
        return bool(source) and source.lower() != repo.lower()

    def client(self, env: Mapping[str, str] | None = None, **kw) -> ci_client.Client:
        return ci_client.Client(oidc=self.oidc(env), env=env, **kw)

    # -- the REST API ------------------------------------------------------------------------

    @staticmethod
    def _auth(env: Mapping[str, str]) -> dict[str, str]:
        project = (env.get(PROJECT_TOKEN_ENV) or "").strip()
        if project:
            return {"PRIVATE-TOKEN": project}
        job = (env.get("CI_JOB_TOKEN") or "").strip()
        if job:
            return {"JOB-TOKEN": job}
        raise CIError(f"no GitLab token: set {PROJECT_TOKEN_ENV} (a project access token) or run in a job "
                      "(CI_JOB_TOKEN)", code="gitlab_token")

    @staticmethod
    def api_url(env: Mapping[str, str]) -> str:
        url = (env.get("CI_API_V4_URL") or "").rstrip("/")
        if not url:
            raise CIError("CI_API_V4_URL isn't set (not a GitLab job?)", code="gitlab_api")
        if urllib.parse.urlsplit(url).scheme != "https":
            raise CIError("CI_API_V4_URL isn't https", code="gitlab_api")
        return url

    def _api(self, env: Mapping[str, str], method: str, path: str, body: dict | None = None) -> dict:
        env = self._merged(env)
        project = urllib.parse.quote(self.repo(env), safe="")
        url = f"{self.api_url(env)}/projects/{project}{path}"
        status, out = self._request(method, url, {**self._auth(env), "Accept": "application/json"}, body)
        if status not in (200, 201):
            msg = out.get("message") or out.get("error")
            text = auth._sanitize(msg if isinstance(msg, str) else json.dumps(msg), 200) if msg else ""
            raise CIError(f"GitLab API {method} {path.split('?')[0]} failed (HTTP {status})"
                          + (f": {text}" if text else ""), code=f"http_{status}")
        return out

    def create_merge_request(self, env: Mapping[str, str], *, source: str, target: str, title: str,
                             description: str = "") -> dict:
        """Open a merge request; returns ``{"iid": int, "url": str}``."""
        out = self._api(env, "POST", "/merge_requests", {
            "source_branch": source, "target_branch": target, "title": title, "description": description,
            "remove_source_branch": True})
        iid, url = out.get("iid"), out.get("web_url")
        if not isinstance(iid, int) or isinstance(iid, bool) or not isinstance(url, str):
            raise CIError("the GitLab API answered without a merge request", code="bad_response")
        return {"iid": iid, "url": url}

    def comment(self, env: Mapping[str, str], mr_iid: int, body: str) -> str:
        """Add a note to merge request ``mr_iid``; returns the note id."""
        out = self._api(env, "POST", f"/merge_requests/{int(mr_iid)}/notes", {"body": body})
        return str(out.get("id", ""))


def entitled() -> bool:
    return license.has(FEATURE)


def get_host(name: str | None = None, env: Mapping[str, str] | None = None, *,
             is_entitled: Callable[[], bool] | None = None, request: Request | None = None):
    """The host for ``name`` (``github`` or ``gitlab``), else the one ``env``
    shows (a GitLab job, otherwise GitHub). GitLab needs the ``ci_enterprise``
    entitlement and is refused without it, including when the check fails."""
    import os

    env = os.environ if env is None else env
    if name is not None and name not in HOSTS:
        raise CIError(f"host must be {' or '.join(HOSTS)}, not {auth._sanitize(name, 40)!r}")
    chosen = name or (GITLAB if GitLabHost.detect(env) and not GitHubHost.detect(env) else GITHUB)
    if chosen == GITHUB:
        return GitHubHost()
    try:
        ok = bool((is_entitled or entitled)())
    except Exception:  # noqa: BLE001 - fail closed on anything
        ok = False
    if not ok:
        raise CIError("brindle CI on GitLab needs brindle Enterprise; see `brindle account`",
                      code="not_entitled")
    return GitLabHost(request, env)


GITLAB_TEMPLATE = """\
# brindle CI on GitLab (written by `brindle ci init --host gitlab`).
# Set BRINDLE_PRO_TOKEN (the org CI token) as a masked, protected CI/CD variable, and
# BRINDLE_GITLAB_TOKEN (a project access token with the api scope) so brindle can open
# merge requests and comment. Add your model key (for example ANTHROPIC_API_KEY) the same way.

stages: [brindle]

brindle-ci:
  stage: brindle
  image: python:3.12
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  id_tokens:
    BRINDLE_ID_TOKEN:
      aud: https://pawdelta.com/brindle
  variables:
    GIT_DEPTH: "0"
  script:
    - pip install brindle
    - brindle ci doctor
    - brindle ci start --validate --pr "$CI_MERGE_REQUEST_IID" --head "$CI_COMMIT_SHA" --out brindle-plan
    - brindle ci run --plan brindle-plan/plan.jwt --run-token-file brindle-plan/run_token
  after_script:
    # CI_JOB_STATUS (success, failed, canceled) -> report's vocabulary
    - |
      case "$CI_JOB_STATUS" in
        success) c=success ;;
        canceled) c=cancelled ;;
        *) c=failure ;;
      esac
      brindle ci report --plan-dir brindle-plan --run "$c" || true
  artifacts:
    when: always
    expire_in: 1 week
    paths: [brindle-plan/plan.jwt]
"""


# Keyless cloud sign-in: one id_tokens entry per cloud (the audience that cloud's
# identity provider trusts), read by ci_federation.StaticCloudToken. The variables
# are the CI/CD variables `ci init` documents for each cloud.
CLOUD_ID_TOKENS = {   # cloud: (id_tokens variable, aud, CLAUDE_CODE_USE_* variable, CI/CD variables to set)
    "bedrock": (ci_federation.GITLAB_TOKEN_VARS["bedrock"], ci_federation.AWS_AUDIENCE,
                "CLAUDE_CODE_USE_BEDROCK", ("AWS_ROLE_ARN", "AWS_REGION")),
    "vertex": (ci_federation.GITLAB_TOKEN_VARS["vertex"],
               ci_federation.GCP_AUDIENCE_PREFIX + "$GCP_WORKLOAD_IDENTITY_PROVIDER",
               "CLAUDE_CODE_USE_VERTEX", ("GCP_WORKLOAD_IDENTITY_PROVIDER", "GCP_SERVICE_ACCOUNT",
                                          "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION")),
    "foundry": (ci_federation.GITLAB_TOKEN_VARS["foundry"], ci_federation.AZURE_AUDIENCE,
                "CLAUDE_CODE_USE_FOUNDRY", ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "ANTHROPIC_FOUNDRY_RESOURCE")),
}


def render_template(cloud: str | None = None) -> str:
    """The brindle job template; with ``cloud`` (bedrock, vertex or foundry),
    also the ``id_tokens`` entry and ``CLAUDE_CODE_USE_*`` variable that make
    the job sign in to Claude on that cloud with no stored cloud secret."""
    if cloud is None:
        return GITLAB_TEMPLATE
    if cloud not in CLOUD_ID_TOKENS:
        raise CIError(f"cloud must be one of {', '.join(CLOUD_ID_TOKENS)}")
    var, aud, use, ids = CLOUD_ID_TOKENS[cloud]
    comment = (
        f"# Keyless {cloud} sign-in: set {', '.join(ids)} (and your model pins, "
        f"{', '.join(ci_client.MODEL_PIN_VARS)}) as CI/CD variables; no cloud secret is stored.\n"
        f"# WARNING: GitLab can't mint a new ID token mid-job, so brindle writes this one once and\n"
        f"# never refreshes it. Its lifetime must cover the job timeout: set the {cloud} identity\n"
        f"# provider's maximum token lifetime, and this job's `timeout:`, to match.\n")
    text = GITLAB_TEMPLATE.replace("\nstages:", "\n" + comment + "\nstages:", 1)
    text = text.replace("      aud: https://pawdelta.com/brindle\n",
                        f"      aud: https://pawdelta.com/brindle\n    {var}:\n      aud: {aud}\n", 1)
    return text.replace('    GIT_DEPTH: "0"\n', f'    GIT_DEPTH: "0"\n    {use}: "1"\n', 1)


def cloud_from_env(env: Mapping[str, str]) -> str | None:
    """The cloud ``env`` (the CI/CD variables) selects: by ``CLAUDE_CODE_USE_*``,
    else by which cloud's ID variable is set."""
    for cloud, (_, _, use, ids) in CLOUD_ID_TOKENS.items():
        if (env.get(use) or "").strip().lower() not in ("", "0", "false", "no"):
            return cloud
    for cloud, (_, _, _, ids) in CLOUD_ID_TOKENS.items():
        if env.get(ids[0]):
            return cloud
    return None


def init_gitlab(*, cwd: str, is_entitled: Callable[[], bool] | None = None,
                say: Callable[[str], None] = print, force: bool = False,
                env: Mapping[str, str] | None = None, cloud: str | None = None,
                preview: bool = False) -> Path:
    """``brindle ci init --host gitlab``: write ``.gitlab-ci.yml`` (the
    brindle job template) in ``cwd``. Refuses without the ``ci_enterprise``
    entitlement, and never overwrites an existing file unless ``force``.
    ``cloud`` adds that cloud's keyless sign-in (see :func:`render_template`);
    it defaults to the cloud ``env`` selects. A preview cloud needs ``preview``
    (see :func:`ci_client.preview_check`)."""
    ci_client.refuse_airgap()
    get_host(GITLAB, {}, is_entitled=is_entitled)
    path = Path(cwd) / GITLAB_CI_FILE
    if path.exists() and not force:
        raise CIError(f"{GITLAB_CI_FILE} already exists; merge the brindle-ci job from the template "
                      "into it, or pass --force to replace it")
    import os

    env = os.environ if env is None else env
    cloud = cloud or cloud_from_env(env)
    if warning := ci_client.preview_check(cloud, preview=preview, env=env):
        say(warning)
    path.write_text(render_template(cloud), encoding="utf-8")
    say(f"wrote {GITLAB_CI_FILE}")
    say(f"set {ci_client.ENV_TOKEN} (the org CI token), {PROJECT_TOKEN_ENV} and your model keys as masked "
        "CI/CD variables, then commit the file")
    if cloud:
        say(f"{cloud}: the job's ID token is not refreshed; " + ci_federation.GITLAB_LIFETIME_WARNING)

    say(ci_client.doctor(env, None, cwd, org=True))
    return path


__all__ = ["GitHubHost", "GitLabHost", "GitLabOIDC", "get_host", "init_gitlab", "GITLAB_TEMPLATE", "HOSTS"]
