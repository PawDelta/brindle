"""The brindle CI client (brindle Team): the thin, public half of brindle CI.

brindle CI is a paid feature whose decisions all live on PawDelta's server.
This client only does what a CI job can do on its own: it starts a run,
runs the agents, runs the checks, collects evidence and uploads it. It
holds no rules about stalls, verdicts, pull-request text or rendering;
everything it does with a run is told to it by the server, in a signed plan
and in the answer to each heartbeat.

Commands (``brindle ci ...``; see :mod:`brindle.cli`):

* ``start``: ask the server for a run (``POST /ci/runs``) and write its plan
  and run token for the run job.
* ``run``: verify the plan, scrub the job's secrets, start a supervisor with
  the plan's instructions, send heartbeats, obey ``continue`` / ``stop`` /
  ``escalate``, then upload the git bundle and the evidence.
* ``validate``: start a validation, run its checks in a scrubbed environment,
  ask each reviewer, and upload the evidence (and a second time when the
  server asks for ``more``).
* ``doctor``: which CLIs and credential *names* are here, the credential
  kind per provider, and what the credential rule makes of them.
* ``init``: the one-command setup.

Plans are JWTs signed with the brindle Pro signing key: they are verified
with the pinned keys of :mod:`brindle.pro.license`, with their own ``typ``
and ``token_use``, and checked against the repository and the checkout.
Tokens are never logged, printed or put in exception messages. In air-gap
mode (:mod:`brindle.airgap`) nothing is sent and every command refuses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import signal
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Mapping, MutableMapping

from brindle import airgap, ci_adapters, git
from brindle.pro import auth, license

log = logging.getLogger(__name__)

PLAN_TYP = "brindle-ci-plan+jwt"
PLAN_TOKEN_USE = "ci_plan"
MAX_PLAN_BYTES = 256 * 1024
LEEWAY = license.LEEWAY
PLAN_COMMON = ("iss", "aud", "sub", "org_id", "kid", "jti", "iat", "exp", "token_use", "plan_kind",
               "id", "repo")
RUN_TOKEN_RE = re.compile(r"^crt_[A-Za-z0-9_-]{16,256}$")
CI_TOKEN_RE = re.compile(r"^cpc_[A-Za-z0-9_-]{16,256}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ORIGIN_RE = re.compile(r"[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
URL_USERINFO_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/]*@")   # user:token@ in a remote URL
PROVIDERS = ("claude", "codex", "native")

# Secrets a CI job holds that no agent may see.
SECRET_ENV = ("BRINDLE_PRO_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN",
              "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL")
SECRET_PREFIXES = ("GH_",)
# What a check command must not see either: model keys and anything token-like.
MODEL_KEY_NAMES = ci_adapters.PROVIDER_KEYS
MODEL_KEY_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_AUTH_TOKEN", "_PASSWORD")

DEFAULT_HEARTBEAT_S = 60
DEFAULT_TIMEOUT_MIN = 100
TIMEOUT_MARGIN_MIN = 10          # past the plan's timeout: the job ends even if the server is gone
OUTPUT_TAIL = 4 * 1024
OUTPUT_EXCERPT = 8 * 1024
REPLY_MAX = 32 * 1024
NOTE_MAX = 500
BUNDLE_MAX = 25 * 1024 * 1024
EVIDENCE_MAX = 256 * 1024
UPLOAD_TIMEOUT = 120.0
CI_MAX_RESPONSE = 1024 * 1024    # a plan can be 256 KB, and comes inside a JSON answer
OIDC_AUDIENCE = "https://pawdelta.com/brindle"
OIDC_HEADER = "X-Brindle-OIDC"
OIDC_TTL = 240
OIDC_EXP_MARGIN = 30             # stop sending a token this long before its exp
OIDC_RETRY_DELAYS = (1.0, 3.0)   # between tries of a failed OIDC fetch
PLAN_FILE = "plan.jwt"
# A plan too big to sign inline carries instructions_sha256 in place of a
# large instructions text; the texts come beside the JWT (the answer's
# plan_texts, {sha256: text}), and the start job saves them next to the plan.
PLAN_TEXTS_FILE = "plan_texts.json"
CONCLUSIONS = ("success", "failure", "cancelled")
KILL_GRACE_S = 10.0              # wait this long for a killed check's output pipe to close
TOKEN_FILE = "run_token"
SETUP_KINDS = ("issue", "validate", "fix")
SETUP_DIR = ".github/workflows"
SETUP_BRANCH = "brindle/ci-setup"
# Labelling an issue with this hands it to the issue workflow.
TRIGGER_LABEL = "brindle"
TRIGGER_LABEL_COLOR = "2E7D32"
TRIGGER_LABEL_DESCRIPTION = "brindle CI picks this up"
ENV_TOKEN = "BRINDLE_PRO_TOKEN"
# How init sets Claude up: the ANTHROPIC_API_KEY secret, or workload identity
# federation (the IDs as Actions variables; the workflow exchanges GitHub's
# OIDC token with them once and gives the job ANTHROPIC_AUTH_TOKEN).
KEY = "key"
FEDERATION = "federation"
CREDENTIALS = (KEY, FEDERATION)
FEDERATION_VARS = (   # (variable, question, required)
    ("ANTHROPIC_FEDERATION_RULE_ID", "federation rule id (fdrl_...)", True),
    ("ANTHROPIC_ORGANIZATION_ID", "Anthropic organization id (uuid)", True),
    ("ANTHROPIC_SERVICE_ACCOUNT_ID", "service account id (svac_...)", True),
    ("ANTHROPIC_WORKSPACE_ID", "workspace id (wrkspc_..., optional)", False),
)
# An organization-level API key (not scoped to a workspace) needs the
# workspace in every request: the workflow sends it as an
# anthropic-workspace-id header (ANTHROPIC_CUSTOM_HEADERS) from this variable.
WORKSPACE_VAR = "ANTHROPIC_WORKSPACE_ID"
WORKSPACE_QUESTION = "workspace ID (only for an organization-level key; leave blank for a workspace key)"
WORKSPACE_ID_RE = re.compile(r"^wrkspc_[A-Za-z0-9_-]+$")
FEDERATION_AUDIENCE = "https://api.anthropic.com"
FEDERATION_MIN_LIFETIME_S = 7200


class CIError(Exception):
    """A failed step. ``code`` is the server's error code when it has one.
    The message never contains a token or a plan."""

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


# -- secrets -----------------------------------------------------------------------------------


def is_job_secret(name: str) -> bool:
    return name in SECRET_ENV or name.startswith(SECRET_PREFIXES)


def is_model_key(name: str) -> bool:
    return name in MODEL_KEY_NAMES or name.endswith(MODEL_KEY_SUFFIXES)


def scrub_secrets(env: MutableMapping[str, str]) -> list[str]:
    """Remove the job's secrets from ``env`` in place (before any agent
    starts), and Claude key variables set to an empty string, which Claude
    Code would take over the federation token. Returns the names removed."""
    gone = sorted(k for k in env if is_job_secret(k))
    for k in gone:
        del env[k]
    return sorted(gone + ci_adapters.drop_empty_keys(env))


def check_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment a check command runs in: no job secrets, no model
    keys, nothing token-like."""
    return {k: v for k, v in env.items() if not (is_job_secret(k) or is_model_key(k))}


def read_run_token(path: str | Path) -> str:
    """The run token in ``path``; the file is deleted once read."""
    p = Path(path)
    try:
        raw = p.read_text("utf-8")
    except OSError as e:
        raise CIError(f"can't read the run token file: {e.strerror or e}") from e
    try:
        p.unlink()
    except OSError as e:
        raise CIError(f"can't delete the run token file: {e.strerror or e}") from e
    token = raw.strip()
    if not RUN_TOKEN_RE.match(token):
        raise CIError("the run token file doesn't hold a run token")
    return token


def ci_token(env: Mapping[str, str]) -> str:
    token = (env.get(ENV_TOKEN) or "").strip()
    if not token:
        raise CIError(f"{ENV_TOKEN} isn't set (the org CI token; see `brindle ci init`)")
    if not CI_TOKEN_RE.match(token):
        raise CIError(f"{ENV_TOKEN} isn't an org CI token")
    return token


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


# -- plans ---------------------------------------------------------------------------------------


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str(v) -> bool:
    return isinstance(v, str) and bool(v)


def _limits(raw, defaults: dict) -> dict:
    out = dict(defaults)
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise CIError("plan limits are malformed")
    for k, v in raw.items():
        if k in out and not (_is_int(v) and v >= 0):
            raise CIError("plan limits are malformed")
        out[k] = v
    return out


def _plan_texts(raw) -> dict[str, str]:
    """The texts sent beside a plan ({sha256: text}); none is ``{}``."""
    if raw is None:
        return {}
    if not (isinstance(raw, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items())):
        raise CIError("plan texts are malformed", code="bad_plan")
    return raw


def _resolve_instructions(obj: dict, texts: Mapping[str, str], what: str) -> None:
    """Put the instructions of ``obj`` (a run plan or a reviewer) in
    ``obj["instructions"]``: inline, or the text in ``texts`` whose SHA-256
    is the signed ``instructions_sha256``."""
    if "instructions_sha256" not in obj:
        if not _str(obj.get("instructions")):
            raise CIError(f"{what} has no instructions")
        return
    if "instructions" in obj:
        raise CIError(f"{what} has both instructions and instructions_sha256")
    digest = obj["instructions_sha256"]
    if not (isinstance(digest, str) and SHA256_RE.match(digest)):
        raise CIError(f"{what} instructions_sha256 is malformed")
    text = texts.get(digest)
    if not _str(text):
        raise CIError(f"{what} instructions text is missing", code="bad_plan")
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        raise CIError(f"{what} instructions text isn't valid UTF-8", code="bad_plan") from None
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise CIError(f"{what} instructions text doesn't match its hash", code="bad_plan")
    obj["instructions"] = text


def _check_run_plan(c: dict, texts: Mapping[str, str]) -> None:
    goal = c.get("goal")
    if not (isinstance(goal, dict) and _str(goal.get("title"))):
        raise CIError("run plan has no goal")
    if not (_str(c.get("branch")) and _str(c.get("base_branch"))):
        raise CIError("run plan names no branch")
    if not (isinstance(c.get("base_sha"), str) and SHA_RE.match(c["base_sha"])):
        raise CIError("run plan has no base_sha")
    ms = c.get("milestones")
    if ms is not None:
        if not isinstance(ms, list):
            raise CIError("run plan milestones are malformed")
        for m in ms:
            if not (isinstance(m, dict) and _is_int(m.get("id")) and _str(m.get("title"))
                    and (m.get("check") is None or _str(m.get("check")))):
                raise CIError("run plan milestones are malformed")
    _resolve_instructions(c, texts, "run plan")
    if c.get("provider") not in PROVIDERS:
        raise CIError("run plan names an unknown provider")
    if not (c.get("profile") is None or _str(c["profile"])):
        raise CIError("run plan profile is malformed")
    c["limits"] = _limits(c.get("limits"), {"timeout_min": DEFAULT_TIMEOUT_MIN, "token_budget": 0,
                                            "heartbeat_s": DEFAULT_HEARTBEAT_S})
    if not _is_int(c.get("attempt", 1)):
        raise CIError("run plan attempt is malformed")
    cont = c.get("continuation")
    if cont is not None and not (isinstance(cont, dict) and _is_int(cont.get("pr")) and cont["pr"] > 0):
        raise CIError("run plan continuation is malformed")


def _check_validation_plan(c: dict, texts: Mapping[str, str]) -> None:
    if not _is_int(c.get("pr")):
        raise CIError("validation plan names no pull request")
    if not (isinstance(c.get("head_sha"), str) and SHA_RE.match(c["head_sha"])):
        raise CIError("validation plan has no head_sha")
    checks = c.get("checks")
    if not isinstance(checks, list):
        raise CIError("validation plan checks are malformed")
    for ch in checks:
        if not (isinstance(ch, dict) and _str(ch.get("id")) and _str(ch.get("command"))
                and _is_int(ch.get("timeout_s", 900)) and ch.get("timeout_s", 900) > 0):
            raise CIError("validation plan checks are malformed")
    reviewers = c.get("reviewers")
    if not isinstance(reviewers, list):
        raise CIError("validation plan reviewers are malformed")
    for r in reviewers:
        if not (isinstance(r, dict) and _str(r.get("id")) and r.get("provider") in PROVIDERS):
            raise CIError("validation plan reviewers are malformed")
        _resolve_instructions(r, texts, f"validation plan reviewer {auth._sanitize(r['id'], 40)}")
    c["limits"] = _limits(c.get("limits"), {"token_budget": 0})


def verify_plan(token: str, *, repo: str, now: float | None = None, texts=None) -> dict:
    """Verify a plan JWT against the pinned keys and return its claims. It
    must be a plan (``typ``, ``token_use``), for ``repo``, current, and
    well-formed for its ``plan_kind``. ``texts`` are the plan texts sent
    beside it ({sha256: text}): each signed ``instructions_sha256`` is
    replaced by its text, in ``instructions``, once the hash checks out.
    Raises :class:`CIError`; the message never includes the token."""
    now = time.time() if now is None else now
    texts = _plan_texts(texts)
    try:
        c = license.verify_signed(token, typ=PLAN_TYP, token_use=PLAN_TOKEN_USE, what="plan",
                                  max_bytes=MAX_PLAN_BYTES)
    except license.LicenseError as e:
        raise CIError(str(e), code="bad_plan") from e
    missing = [k for k in PLAN_COMMON if k not in c]
    if missing:
        raise CIError(f"plan is missing claims: {', '.join(missing)}", code="bad_plan")
    if not (_is_int(c["iat"]) and _is_int(c["exp"]) and c["exp"] > c["iat"]):
        raise CIError("plan claims are malformed", code="bad_plan")
    if c["iat"] > now + LEEWAY:
        raise CIError("plan is not valid yet", code="bad_plan")
    if now > c["exp"] + LEEWAY:
        raise CIError("plan has expired", code="bad_plan")
    if not (_str(c["id"]) and _str(c["repo"]) and _str(c["sub"]) and _str(c["org_id"])):
        raise CIError("plan claims are malformed", code="bad_plan")
    if c["repo"] != repo:
        raise CIError(f"plan is for repository {c['repo']}, not {repo}", code="bad_plan")
    kind = c["plan_kind"]
    if kind == "run":
        _check_run_plan(c, texts)
    elif kind == "validation":
        _check_validation_plan(c, texts)
    else:
        raise CIError("plan has an unknown kind", code="bad_plan")
    return c


def head_sha(cwd: str) -> str:
    try:
        return git.out(["rev-parse", "--verify", "HEAD"], cwd)
    except git.GitError as e:
        raise CIError(f"not a git checkout: {e}") from e


def _has_commit(cwd: str, sha: str) -> bool:
    return git.ok(["cat-file", "-e", f"{sha}^{{commit}}"], cwd)


def _fetch_commit(cwd: str, sha: str, pr: int | None) -> str | None:
    """Fetch ``sha`` from origin (one commit deep in a shallow clone), else,
    for a pull request, its ``refs/pull/N/head``. None once the commit is
    here, else why not."""
    shallow = git.out(["rev-parse", "--is-shallow-repository"], cwd) == "true"
    depth = ["--depth=1"] if shallow else []
    errors = []
    for ref in [sha] + ([f"refs/pull/{pr}/head"] if pr else []):
        proc = git.run(["fetch", "--no-tags", "--quiet", *depth, "origin", ref], cwd, check=False)
        if proc.returncode == 0 and _has_commit(cwd, sha):
            return None
        errors.append(f"{ref[:40]}: {(proc.stderr.strip() or proc.stdout.strip() or 'not that commit')[:200]}")
    return "can't fetch it from origin (" + "; ".join(errors) + ")"


def ensure_checkout(cwd: str, sha: str, *, what: str, pr: int | None = None,
                    say: Callable[[str], None] = print) -> None:
    """Put the checkout at ``sha``, the signed plan's commit: the workflow may
    have checked out something else (GitHub's merge commit for a pull
    request, or a branch that moved since). A worktree with uncommitted
    changes is never switched."""
    actual = head_sha(cwd)
    if actual == sha:
        return
    wrong = f"the checkout is at {actual[:12]}, not the plan's {what} {sha[:12]}"
    dirty = git.dirty_files(cwd, tracked_only=True)
    if dirty:
        raise CIError(f"{wrong}, and it has uncommitted changes ({', '.join(dirty[:5])}), so it isn't switched")
    if not _has_commit(cwd, sha):
        why = _fetch_commit(cwd, sha, pr)
        if why:
            raise CIError(f"{wrong}, and it {why}")
    try:
        git.run(["checkout", "--quiet", "--detach", sha], cwd)
    except git.GitError as e:
        raise CIError(f"{wrong}, and checking it out failed: {e}") from e
    now = head_sha(cwd)
    if now != sha:
        raise CIError(f"{wrong}, and after checking it out the checkout is at {now[:12]}")
    say(f"checked out the plan's {what} {sha[:12]} (the workflow had {actual[:12]})")


def check_run_checkout(plan: dict, cwd: str, say: Callable[[str], None] = print) -> None:
    ensure_checkout(cwd, plan["base_sha"], what="base", say=say)


def branch_tip(cwd: str, branch: str) -> str | None:
    """The commit ``branch`` points at: the local branch, else the remote's
    after a fetch. None when neither exists."""
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        proc = git.run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        if ref.startswith("refs/heads/") and git.has_remote(cwd):
            git.run(["fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                    cwd, check=False)
    return None


def prepare_branch(plan: dict, cwd: str, say: Callable[[str], None] = print) -> None:
    """Put the checkout on the plan's branch. A new run cuts the branch from
    the checkout, which must be at ``base_sha``. A continuation (the plan
    carries ``continuation``) checks the existing branch out at its tip,
    which must be ``base_sha``: the server read that tip when it planned, and
    anything else means the branch moved since."""
    branch, base_sha = plan["branch"], plan["base_sha"]
    if not plan.get("continuation"):
        check_run_checkout(plan, cwd, say=say)
        try:
            git.run(["checkout", "-B", branch], cwd)
        except git.GitError as e:
            raise CIError(f"can't create branch {branch}: {e}") from e
        return
    tip = branch_tip(cwd, branch)
    if tip is None:
        raise CIError(f"continuation: branch {branch} doesn't exist here or on origin")
    if tip != base_sha:
        raise CIError(f"continuation: branch {branch} is at {tip[:12]}, not the plan's base {base_sha[:12]}")
    try:
        git.run(["checkout", "-B", branch, base_sha], cwd)
    except git.GitError as e:
        raise CIError(f"can't check out branch {branch}: {e}") from e
    head = head_sha(cwd)
    if head != base_sha:
        raise CIError(f"continuation: the checkout is at {head[:12]}, not {base_sha[:12]}")


def check_validation_checkout(plan: dict, cwd: str, head: str | None = None,
                              say: Callable[[str], None] = print) -> None:
    ensure_checkout(cwd, plan["head_sha"], what="head", pr=plan["pr"], say=say)
    if head and head != plan["head_sha"]:
        raise CIError("the plan's head_sha isn't the one asked for")


def github_repo(env: Mapping[str, str], given: str | None = None) -> str:
    repo = env.get("GITHUB_REPOSITORY") or given
    if not repo:
        raise CIError("no repository: pass --repo or set GITHUB_REPOSITORY")
    if given and repo != given:
        raise CIError(f"--repo {given} isn't this job's repository ({repo})")
    if not REPO_RE.match(repo):
        raise CIError("repository must be owner/name")
    return repo


# -- HTTP -------------------------------------------------------------------------------------------


def _error(status: int, body: dict) -> CIError:
    code = body.get("error") if isinstance(body.get("error"), str) else f"http_{status}"
    code = re.sub(r"[^a-z0-9_]", "", code.lower())[:64] or f"http_{status}"
    msg = body.get("message")
    text = auth._sanitize(msg) if isinstance(msg, str) and msg else code
    return CIError(f"{code}: {text}" if text != code else code, code=code)


def multipart(fields: list[tuple[str, str, bytes, str]]) -> auth.RawBody:
    """A ``multipart/form-data`` body of (field, filename, data, content type)."""
    boundary = "brindle-" + secrets.token_hex(16)
    out = bytearray()
    for name, filename, data, ctype in fields:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
        out += data
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return auth.RawBody(bytes(out), f"multipart/form-data; boundary={boundary}")


def _fetch_oidc(url: str, request_token: str, timeout: float = 15.0) -> str:
    """Ask the Actions runtime for an OIDC token for brindle's audience."""
    import urllib.error
    import urllib.request

    full = url + ("&" if "?" in url else "?") + "audience=" + urllib.parse.quote(OIDC_AUDIENCE, safe="")
    if urllib.parse.urlsplit(full).scheme != "https":
        raise CIError("the OIDC token request URL isn't https", code="oidc")
    req = urllib.request.Request(full, headers={"Authorization": f"bearer {request_token}",
                                                "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(64 * 1024)
    except urllib.error.HTTPError as e:
        raise CIError(f"couldn't get the GitHub Actions OIDC token (HTTP {e.code})", code="oidc") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        why = type(getattr(e, "reason", None) or e).__name__
        raise CIError(f"couldn't get the GitHub Actions OIDC token ({why})", code="oidc") from None
    try:
        value = json.loads(body.decode("utf-8")).get("value")
    except (ValueError, AttributeError, UnicodeDecodeError):
        value = None
    if not isinstance(value, str) or value.count(".") != 2:
        raise CIError("the GitHub Actions OIDC answer held no token", code="oidc")
    return value


class OIDC:
    """The ``X-Brindle-OIDC`` header: a GitHub Actions OIDC token for brindle's
    audience, when this job can mint one (``ACTIONS_ID_TOKEN_REQUEST_URL`` and
    ``ACTIONS_ID_TOKEN_REQUEST_TOKEN``, read once at construction, before the
    job's secrets are scrubbed). Outside Actions there is no header. Tokens
    are cached a few minutes (never past their own ``exp``) and never logged.

    GitHub's token endpoint fails now and then; a run job asks it once per
    heartbeat for hours, so a failed fetch is retried, and when it still
    fails a cached token that hasn't expired yet is sent instead."""

    def __init__(self, env: Mapping[str, str] | None = None, fetch: Callable[[str, str], str] = _fetch_oidc,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        env = os.environ if env is None else env
        self.url = env.get("ACTIONS_ID_TOKEN_REQUEST_URL") or None
        self.request_token = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN") or None
        self._fetch, self._clock, self._sleep = fetch, clock, sleep
        self._cached: tuple[float, float, str] | None = None   # (refresh at, expires at, token)

    @property
    def available(self) -> bool:
        return bool(self.url and self.request_token)

    def header(self) -> dict[str, str]:
        """The header to send, or nothing outside Actions. The token goes to
        whatever ``BRINDLE_PRO_BASE_URL`` names (https is enforced, see
        :func:`brindle.pro.auth.check_url`), so that variable must come from
        trusted workflow environment, never from the repository: whoever
        receives the token can act as this job toward the server."""
        if not self.available:
            return {}
        now = self._clock()
        if self._cached is None or self._cached[0] <= now:
            try:
                token = self._fetch_retrying()
            except CIError:
                if self._cached is not None and self._cached[1] > self._clock():
                    return {OIDC_HEADER: self._cached[2]}
                raise
            now = self._clock()
            expires = _jwt_exp(token)
            expires = now + OIDC_TTL + OIDC_EXP_MARGIN if expires is None else expires - OIDC_EXP_MARGIN
            self._cached = (min(now + OIDC_TTL, expires), expires, token)
        return {OIDC_HEADER: self._cached[2]}

    def _fetch_retrying(self) -> str:
        for delay in (*OIDC_RETRY_DELAYS, None):
            try:
                return self._fetch(self.url, self.request_token)
            except CIError:
                if delay is None:
                    raise
                self._sleep(delay)
        raise AssertionError("unreachable")


def _jwt_exp(token: str) -> float | None:
    """A JWT's ``exp``, read without verifying it (the server verifies it;
    this only says when to stop sending it)."""
    import base64

    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = claims.get("exp")
    except (IndexError, ValueError, AttributeError, TypeError):
        return None
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None
    try:
        value = float(exp)   # an int too big for a float overflows
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


class Client:
    """The ``/ci`` endpoints. Reuses :class:`brindle.pro.auth.Client`'s URL
    rules, transport (https only, timeouts, same-origin redirects) and
    air-gap guard. Every call carries the job's OIDC token when there is
    one (:class:`OIDC`)."""

    def __init__(self, base: str | None = None, transport: auth.Transport | None = None,
                 oidc: OIDC | None = None, env: Mapping[str, str] | None = None) -> None:
        self.base = auth.base_url(base)
        self.transport = transport or auth.UrllibTransport(max_bytes=CI_MAX_RESPONSE)
        self._client = auth.Client(self.base, self.transport)
        self._uploads = auth.Client(self.base, transport or auth.UrllibTransport(
            timeout=UPLOAD_TIMEOUT, max_bytes=CI_MAX_RESPONSE))
        self.oidc = oidc or OIDC(env)

    def _call(self, method: str, path: str, body, token: str, *, client=None) -> tuple[int, dict]:
        try:
            return (client or self._client).call(method, path, body, token, headers=self.oidc.header())
        except auth.AirGapped as e:
            raise CIError(str(e), code="airgap") from e
        except auth.TransportError as e:
            raise CIError(str(e), code=e.code) from e
        except auth.AuthError as e:
            raise CIError(str(e), code=e.code) from e

    def start_run(self, token: str, repo: str, trigger: dict, providers: list[str]) -> tuple[int, dict]:
        from brindle import __version__

        return self._call("POST", "/ci/runs", auth.JSONBody(
            repo=repo, trigger=trigger, providers_available=providers, client=__version__), token)

    def events(self, run_token: str, run_id: str, event: dict) -> dict:
        status, body = self._call("POST", f"/ci/runs/{urllib.parse.quote(run_id, safe='')}/events",
                                  auth.JSONBody(event), run_token)
        if status != 200:
            raise _error(status, body)
        return body

    def put_result(self, run_token: str, run_id: str, evidence: dict, bundle: bytes | None) -> dict:
        fields = []
        if bundle is not None:
            fields.append(("bundle", "run.bundle", bundle, "application/octet-stream"))
        fields.append(("evidence", "evidence.json", json.dumps(evidence, separators=(",", ":")).encode(),
                       "application/json"))
        status, body = self._call("PUT", f"/ci/runs/{urllib.parse.quote(run_id, safe='')}/result",
                                  multipart(fields), run_token, client=self._uploads)
        if status != 200:
            raise _error(status, body)
        return body

    def job_status(self, token: str, the_id: str, repo: str, job: str, conclusion: str,
                   kind: str = "run") -> None:
        """``POST /ci/runs/{id}/job-status`` (``kind="run"``) or
        ``POST /ci/validations/{id}/job-status`` (``kind="validation"``)."""
        collection = "validations" if kind == "validation" else "runs"
        status, body = self._call("POST", f"/ci/{collection}/{urllib.parse.quote(the_id, safe='')}/job-status",
                                  auth.JSONBody(repo=repo, job=job, conclusion=conclusion), token)
        if status != 200:
            raise _error(status, body)

    def start_validation(self, token: str, repo: str, pr: int, head: str, fork: bool,
                         providers: list[str]) -> tuple[int, dict]:
        return self._call("POST", "/ci/validations", auth.JSONBody(
            repo=repo, pr=pr, head_sha=head, fork=fork, providers_available=providers), token)

    def put_evidence(self, run_token: str, validation_id: str, evidence: dict) -> dict:
        status, body = self._call("PUT", f"/ci/validations/{urllib.parse.quote(validation_id, safe='')}/evidence",
                                  auth.JSONBody(evidence), run_token, client=self._uploads)
        if status != 200:
            raise _error(status, body)
        return body

    def workflow(self, token: str, kind: str) -> str:
        status, body = self._call("GET", f"/ci/workflow?kind={urllib.parse.quote(kind, safe='')}", None, token)
        if status != 200:
            raise _error(status, body)
        text = body.get(auth.TEXT_KEY)
        if not isinstance(text, str) or not text.strip():
            raise CIError("the server sent no workflow", code="bad_response")
        return text


def refuse_airgap() -> None:
    if airgap.enabled():
        raise CIError(f"air-gap mode is on ({airgap.source()}): brindle CI talks to the brindle Pro "
                      "backend and can't run here", code="airgap")


# -- start ------------------------------------------------------------------------------------------


def _write_plan_files(out_dir: str | Path, body: dict, id_key: str) -> str:
    the_id, plan, run_token = body.get(id_key), body.get("plan"), body.get("run_token")
    if not (_str(the_id) and _str(plan) and isinstance(run_token, str) and RUN_TOKEN_RE.match(run_token)):
        raise CIError("the server's answer is malformed", code="bad_response")
    try:
        texts = _plan_texts(body.get("plan_texts"))
    except CIError:
        raise CIError("the server's answer is malformed", code="bad_response") from None
    out = Path(out_dir)
    write_private(out / PLAN_FILE, plan)
    if texts:
        write_private(out / PLAN_TEXTS_FILE, json.dumps(texts))
    else:
        (out / PLAN_TEXTS_FILE).unlink(missing_ok=True)
    write_private(out / TOKEN_FILE, run_token + "\n")
    return auth._sanitize(the_id, 80)


def read_plan_texts(plan_path: str | Path):
    """The plan texts the start job saved beside the plan file at
    ``plan_path``: None when there are none. They are checked against the
    plan's signed hashes by :func:`verify_plan`. ``run`` calls this (as its
    ``texts`` loader) only after the run token is read and deleted."""
    try:
        raw = (Path(plan_path).parent / PLAN_TEXTS_FILE).read_text("utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise CIError(f"can't read the plan texts: {e.strerror or e}") from e
    except ValueError:
        raise CIError("plan texts are malformed", code="bad_plan") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise CIError("plan texts are malformed", code="bad_plan") from None


def start(repo: str, trigger: dict, out_dir: str | Path, *, client: Client, token: str,
          providers: list[str], say: Callable[[str], None] = print) -> int:
    """``brindle ci start``: ask for a run (or, for a ``validate`` trigger, a
    validation); write its plan and run token to ``out_dir`` for the run
    job. Exit code: 0 when started, when a duplicate already covers it, or
    when the server skipped it (nothing written); 1 otherwise."""
    if trigger["kind"] == "validate":
        status, body = client.start_validation(token, repo, trigger["pr"], trigger["head_sha"], trigger["fork"],
                                               providers)
        if status == 200 and _str(body.get("skipped")):
            say(f"skipped: {auth._sanitize(body['skipped'], 40)} (the server posts the check itself)")
            return 0
        if status == 201:
            vid = _write_plan_files(out_dir, body, "validation_id")
            say(f"validation {vid} started; plan and run token written to {out_dir}")
            return 0
        raise _error(status, body)
    status, body = client.start_run(token, repo, trigger, providers)
    if status == 201:
        run_id = _write_plan_files(out_dir, body, "run_id")
        say(f"run {run_id} started; plan and run token written to {out_dir}")
        return 0
    if status == 409 and body.get("error") == "duplicate":
        existing = body.get("existing") if isinstance(body.get("existing"), dict) else {}
        what = (f"pull request #{existing['pr']}" if _is_int(existing.get("pr"))
                else f"run {auth._sanitize(existing.get('run_id', '?'), 80)}")
        say(f"not started: {what} already covers this (the server has commented)")
        return 0
    if status == 409 and body.get("error") == "branch_conflict":
        msg = body.get("message")
        say("not started: " + (auth._sanitize(msg, 300) if _str(msg) else "the branch has changes brindle didn't make")
            + " (the server has commented)")
        return 0
    if status == 410 and body.get("error") == "jira_text_gone":
        say("not started: the Jira ticket's text has expired on the server; move the ticket to the trigger status again")
        return 0
    raise _error(status, body)


def trigger_for(issue: int | None = None, goal_text: str | None = None, dispatch: str | None = None,
                validate: bool = False, pr: int | None = None, head: str | None = None,
                fork: bool | None = None) -> dict:
    """What ``brindle ci start`` was asked to start."""
    given = [x for x in (issue, goal_text, dispatch) if x is not None] + ([True] if validate else [])
    if len(given) != 1:
        raise CIError("give exactly one of --issue, --goal-text, --dispatch or --validate")
    if validate:
        if pr is None or not SHA_RE.match(head or ""):
            raise CIError("--validate needs --pr N and --head <40-hex sha>")
        return {"kind": "validate", "pr": int(pr), "head_sha": head, "fork": bool(fork)}
    if issue is not None:
        return {"kind": "issue", "issue": int(issue)}
    if dispatch is not None:
        if not re.fullmatch(r"run_[A-Za-z0-9_-]{4,80}", dispatch):
            raise CIError("--dispatch takes a run id (run_...)")
        return {"kind": "dispatch", "run_id": dispatch}
    text = (goal_text or "").strip()
    title, _, detail = text.partition("\n")
    return {"kind": "text", "title": title.strip(), "detail": detail.strip()}


def parse_bool(text: str | None) -> bool | None:
    if text is None:
        return None
    t = text.strip().lower()
    if t in ("true", "1", "yes"):
        return True
    if t in ("false", "0", "no", ""):
        return False
    raise CIError("--fork takes true or false")


def plan_id(plan_token: str) -> tuple[str, str, str]:
    """(plan_kind, id, repo) of a plan whose signature checks out, without
    the time or repository checks: for the report job, which may run after
    the plan expired and only names the run to the server."""
    try:
        c = license.verify_signed(plan_token, typ=PLAN_TYP, token_use=PLAN_TOKEN_USE, what="plan",
                                  max_bytes=MAX_PLAN_BYTES)
    except license.LicenseError as e:
        raise CIError(str(e), code="bad_plan") from e
    if not (_str(c.get("id")) and c.get("plan_kind") in ("run", "validation") and _str(c.get("repo"))
            and REPO_RE.match(c["repo"])):
        raise CIError("plan claims are malformed", code="bad_plan")
    return c["plan_kind"], c["id"], c["repo"]


def report(plan_dir: str | Path, *, client: Client, token: str, start: str | None, run: str | None,
           say: Callable[[str], None] = print) -> None:
    """``brindle ci report``: tell the server how the start and run jobs
    ended, so it can comment (a run) or post the check (a validation) when
    the run job died without uploading. A run reports every conclusion
    given to ``POST /ci/runs/{id}/job-status``; a validation reports only a
    failed or cancelled run job, to ``POST /ci/validations/{id}/job-status``,
    with the run token when the run job left it behind, else the CI token.
    Nothing to report when the start job wrote no plan (a duplicate, a
    conflict or a skip)."""
    plan_dir = Path(plan_dir)
    try:
        plan_token = (plan_dir / PLAN_FILE).read_text("utf-8").strip()
    except OSError:
        say("nothing to report: the start job wrote no plan")
        return
    for job, conclusion in (("start", start), ("run", run)):
        if conclusion is not None and conclusion not in CONCLUSIONS:
            raise CIError(f"--{job} must be one of {', '.join(CONCLUSIONS)}")
    kind, the_id, repo = plan_id(plan_token)
    if kind == "validation":
        if run not in ("failure", "cancelled"):
            say("nothing to report: the validation's run job uploaded its evidence")
            return
        bearer = token
        try:
            left = (plan_dir / TOKEN_FILE).read_text("utf-8").strip()
            if RUN_TOKEN_RE.match(left):
                bearer = left        # the run job never read it: it is still good for this one call
        except OSError:
            pass
        client.job_status(bearer, the_id, repo, "run", run, kind="validation")
        say(f"reported: run job {run}")
        return
    for job, conclusion in (("start", start), ("run", run)):
        if conclusion is None:
            continue
        client.job_status(token, the_id, repo, job, conclusion)
        say(f"reported: {job} job {conclusion}")


# -- the run loop -----------------------------------------------------------------------------------


def tail(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text.encode("utf-8")) <= limit else text.encode("utf-8")[-limit:].decode("utf-8", "ignore")


def milestone_rows(db, root_id: str, ids: list[int] | None = None) -> list[dict]:
    """The session's milestones as the server wants them, under the PLAN's
    milestone ids, never brindle's positions. The mapping is by order: the
    i-th milestone brindle stores (``set_goal`` keeps the plan's order) is
    reported as ``ids[i]``. A row past the end of ``ids`` (which only a
    goal brindle didn't get from the plan could produce) keeps its position."""
    rows = []
    for i, m in enumerate(db.milestones(root_id)):
        rows.append({"id": ids[i] if ids and i < len(ids) else m.position, "status": m.status,
                     "exit": 0 if m.status == "passed" else 1 if m.status == "failed" else None,
                     "output_tail": tail(m.output, OUTPUT_TAIL)})
    return rows


def count_commits(cwd: str, base_sha: str, branch: str) -> int:
    try:
        return int(git.out(["rev-list", "--count", f"{base_sha}..{branch}"], cwd) or "0")
    except (git.GitError, ValueError) as e:
        raise CIError(f"can't count the commits: {e}") from e


def session_event(db, root_id: str, adapter: ci_adapters.Adapter, *, cwd: str | None = None,
                  base_sha: str | None = None, branch: str | None = None,
                  milestone_ids: list[int] | None = None) -> dict:
    """What the session looks like right now, as the server wants it: the
    state brindle's autopilot records, the milestones and the usage. No
    interpretation beyond naming the autopilot's own states."""
    root = db.get_agent(root_id)
    ap = db.get_autopilot(root_id)
    question = None
    if root is None:
        state, note = "failed", "the supervisor is gone"
    elif ap and ap.state == "done":
        state, note = "finished", None
    elif ap and ap.state in ("blocked", "usage_paused"):
        state, note = "needs_user", None
        question = ap.note or "the supervisor needs the person"
    elif ap and ap.state == "stalled":
        state, note = "stalled", ap.note
    elif root.status in ("paused", "done") or not adapter.alive(db, root):
        state, note = "failed", f"the supervisor is {root.status}"
    elif stuck := adapter.stuck_screen(db, root):
        state, note = "failed", stuck
    else:
        state, note = "working", f"supervisor {root.status}; autopilot {ap.state if ap else 'off'}"
    event = {"state": state, "milestones": milestone_rows(db, root_id, milestone_ids),
             "usage": adapter.usage(db, root_id),
             "commits": count_commits(cwd, base_sha, branch) if cwd and base_sha and branch else 0,
             "provider_error": adapter.provider_error(db, root_id)}
    if question:
        event["question"] = tail(question, NOTE_MAX)
    if note:
        event["note"] = tail(note, NOTE_MAX)
    return event


def _set_goal(db, root_id: str, plan: dict) -> None:
    from brindle import autopilot

    goal = plan["goal"]
    ms = plan.get("milestones") or []
    if not ms:
        return
    autopilot.set_goal(db, root_id, goal["title"], [(m["title"], m["check"], None) for m in ms],
                       goal.get("detail") if isinstance(goal.get("detail"), str) else None)


def make_bundle(cwd: str, base_sha: str, branch: str) -> tuple[int, bytes | None, str | None]:
    """(commits on the branch past base, the bundle bytes, why there is none)."""
    n = count_commits(cwd, base_sha, branch)
    if n == 0:
        return 0, None, "no commits"
    with tempfile.TemporaryDirectory(prefix="brindle-ci-") as tmp:
        path = Path(tmp) / "run.bundle"
        try:
            git.run(["bundle", "create", str(path), f"{base_sha}..{branch}"], cwd)
            data = path.read_bytes()
        except (git.GitError, OSError) as e:
            raise CIError(f"can't write the bundle: {e}") from e
    if len(data) > BUNDLE_MAX:
        return n, None, f"bundle is {len(data)} bytes, over the {BUNDLE_MAX} limit"
    return n, data, None


def run(plan_token: str, token_path: str | Path, *, cwd: str, env: MutableMapping[str, str],
        client: Client, db=None, adapters: Mapping[str, ci_adapters.Adapter] | None = None,
        clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
        org: bool | None = None, texts=None, say: Callable[[str], None] = print) -> dict:
    """``brindle ci run``: the run job. A run plan runs the supervisor and
    uploads the result; a validation plan runs the checks and reviewers and
    uploads the evidence. ``texts`` are the plan texts saved beside the plan,
    or a function that reads them (:func:`read_plan_texts`), called once the
    run token is gone from disk. Returns the server's answer to the upload."""
    from brindle import workspaces
    from brindle.db import DB

    repo = github_repo(env)
    # The token file goes first, whatever happens to the plan: a rejected
    # plan must not leave a run token on the runner's disk.
    run_token = read_run_token(token_path)
    if callable(texts):
        texts = texts()
    plan = verify_plan(plan_token, repo=repo, now=clock(), texts=texts)
    if plan["plan_kind"] == "validation":
        check_validation_checkout(plan, cwd, say=say)
        gone = scrub_secrets(env)
        for k in gone:
            os.environ.pop(k, None)
        return run_validation(plan, run_token, cwd=cwd, env=env, client=client, adapters=adapters,
                              org=org, clock=clock, say=say)
    prepare_branch(plan, cwd, say=say)
    gone = scrub_secrets(env)
    for k in gone:
        os.environ.pop(k, None)
    run_id, base_sha, branch = plan["id"], plan["base_sha"], plan["branch"]
    db = db or DB()
    ws = workspaces.adopt_root(db, cwd)
    adapters = adapters or ci_adapters.default_adapters(ws.repo_root)
    providers_used: list[str] = []
    started = clock()

    def attempt(p: dict):
        adapter = adapters.get(p["provider"])
        if adapter is None:
            raise CIError(f"no adapter for provider {p['provider']}")
        try:
            root = adapter.launch(db, ws, p["instructions"], p.get("profile"))
        except ci_adapters.AdapterError as e:
            raise CIError(f"the {p['provider']} supervisor couldn't start: {e}", code="launch") from None
        _set_goal(db, root.id, p)
        providers_used.append(p["provider"])
        say(f"supervisor {root.id} started ({p['provider']}, attempt {p.get('attempt', 1)})")
        return adapter, root

    adapter, root = attempt(plan)
    deadline = started + (plan["limits"]["timeout_min"] + TIMEOUT_MARGIN_MIN) * 60
    final: str | None = None
    event: dict = {"state": "working", "milestones": [], "usage": {}}
    while final is None:
        sleep(plan["limits"]["heartbeat_s"])
        event = session_event(db, root.id, adapter, cwd=cwd, base_sha=base_sha, branch=branch,
                              milestone_ids=[m["id"] for m in plan.get("milestones") or []])
        try:
            answer = client.events(run_token, run_id, event)
        except CIError as e:
            if e.code in ("airgap", "bad_plan"):
                raise
            log.warning("brindle ci: heartbeat failed (%s): %s", e.code, e)
            if clock() > deadline:
                final = "timeout"
            continue
        action = answer.get("action")
        if action == "continue":
            # A session that is over, or that needs the person, ends the job
            # at once: the result carries the state (and the question) and
            # the server takes it from there. A stalled session keeps
            # heartbeating until the server says escalate or stop.
            if event["state"] in ("finished", "failed", "needs_user"):
                final = event["state"]
        elif action == "stop":
            reason = answer.get("reason")
            final = reason if reason in ("budget", "timeout") else event["state"] if event["state"] != "working" else "failed"
            say(f"the server stopped the run ({auth._sanitize(reason or '?', 40)})")
        elif action == "escalate":
            new = verify_plan(answer.get("plan") or "", repo=repo, now=clock(), texts=answer.get("plan_texts"))
            if new["plan_kind"] != "run" or new["id"] != run_id or new["base_sha"] != base_sha \
                    or new["branch"] != branch:
                raise CIError("the escalation plan is for another run", code="bad_plan")
            adapter.stop(db, root.id)
            plan = new
            adapter, root = attempt(plan)
            deadline = started + (plan["limits"]["timeout_min"] + TIMEOUT_MARGIN_MIN) * 60
        else:
            raise CIError("the server sent an unknown action", code="bad_response")
    adapter.stop(db, root.id)
    event = session_event(db, root.id, adapter, cwd=cwd, base_sha=base_sha, branch=branch,
                          milestone_ids=[m["id"] for m in plan.get("milestones") or []])
    commits, bundle, why = make_bundle(cwd, base_sha, branch)
    evidence = {"final_state": final, "milestones": event["milestones"], "usage": event["usage"],
                "providers": providers_used, "commits": commits}
    if event.get("question"):
        evidence["question"] = event["question"]
    if why and commits:
        log.warning("brindle ci: uploading without a bundle: %s", why)
    result = client.put_result(run_token, run_id, evidence, bundle)
    status = result.get("status")
    if status == "published" and _str(result.get("url")):
        say(f"published: {auth._sanitize(result['url'], 200)}")
    elif status == "reported" and _str(result.get("comment_url")):
        say(f"reported: {auth._sanitize(result['comment_url'], 200)}")
    else:
        say(f"uploaded ({auth._sanitize(status or '?', 40)})")
    return result


# -- validate -----------------------------------------------------------------------------------------


def run_check(check: dict, cwd: str, env: Mapping[str, str], *, popen=subprocess.Popen) -> dict:
    """Run one check command in ``env`` with its timeout; the raw outcome.
    The command gets its own process group, so a timeout kills everything it
    started (a grandchild holding the output pipes would otherwise keep the
    job waiting past the timeout)."""
    timeout = int(check.get("timeout_s", 900))
    t0 = time.monotonic()
    try:
        proc = popen(check["command"], shell=True, cwd=cwd, env=dict(env), stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        proc = None
        code, output = 127, str(e)
    if proc is not None:
        try:
            out, _ = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            try:
                out, _ = proc.communicate(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                out = b""     # something still holds the pipe after SIGKILL: don't wait on it
            code = 124
        output = (out or b"").decode("utf-8", "replace")
        if code == 124:
            output += f"\n(timed out after {timeout}s)"
    return {"id": check["id"], "exit": code, "output_excerpt": tail(output, OUTPUT_EXCERPT),
            "duration_s": round(time.monotonic() - t0, 1)}


CHECK_RESULTS_SLOT = "{{brindle.check_results}}"


def render_check_results(checks: list[dict]) -> str:
    """The check results as text for the one slot a reviewer's instructions
    may hold (``{{brindle.check_results}}``): id, exit code and output."""
    if not checks:
        return "(no checks were run)"
    parts = []
    for c in checks:
        parts.append(f"check {c['id']}: exit {c['exit']} ({c.get('duration_s', 0)}s)\n"
                     f"{(c.get('output_excerpt') or '').rstrip() or '(no output)'}")
    return "\n\n".join(parts)


def ask_reviewer(reviewer: dict, cwd: str, env: Mapping[str, str],
                 adapters: Mapping[str, ci_adapters.Adapter], org: bool | None,
                 checks: list[dict] | None = None) -> tuple[dict, dict]:
    """One reviewer's raw reply (and its usage) as the server wants it. The
    instructions' check-results slot, if any, is filled first."""
    instructions = reviewer["instructions"]
    if CHECK_RESULTS_SLOT in instructions:
        instructions = instructions.replace(CHECK_RESULTS_SLOT, render_check_results(checks or []))
    adapter = adapters.get(reviewer["provider"])
    # Only the protocol's fields: a reviewer that couldn't run or whose CLI
    # failed is sent with an empty reply (and the model, when known); what
    # that means for the verdict is the server's call.
    row = {"id": reviewer["id"], "provider": reviewer["provider"], "model": None, "reply": ""}
    if adapter is None:
        log.warning("brindle ci: reviewer %s: no adapter for %s", reviewer["id"], reviewer["provider"])
        return row, {}
    ok, why = ci_adapters.usable(adapter, env, org)
    if not ok:
        log.warning("brindle ci: reviewer %s not usable: %s", reviewer["id"], why)
        return row, {}
    try:
        rev = adapter.review(instructions, cwd, env)
    except ci_adapters.AdapterError as e:
        log.warning("brindle ci: reviewer %s failed: %s", reviewer["id"], e)
        return row, {}
    row["model"] = rev.model
    if rev.exit in (None, 0):
        row["reply"] = tail(rev.reply, REPLY_MAX)
    else:
        log.warning("brindle ci: reviewer %s exited %s", reviewer["id"], rev.exit)
    return row, rev.usage


def pr_is_fork(env: Mapping[str, str], repo: str) -> bool:
    payload = ci_adapters._event_payload(env) or {}
    pr = payload.get("pull_request")
    head = (pr or {}).get("head") if isinstance(pr, dict) else None
    head_repo = (head or {}).get("repo") if isinstance(head, dict) else None
    if isinstance(head_repo, dict):
        if isinstance(head_repo.get("fork"), bool) and head_repo["fork"]:
            return True
        full = head_repo.get("full_name")
        if isinstance(full, str) and full:
            return full.lower() != repo.lower()
    return False


def run_validation(plan: dict, run_token: str, *, cwd: str, env: MutableMapping[str, str], client: Client,
                   adapters: Mapping[str, ci_adapters.Adapter] | None = None, org: bool | None = None,
                   clock: Callable[[], float] = time.time, check_runner: Callable[..., dict] = run_check,
                   say: Callable[[str], None] = print) -> dict:
    """The run job of a validation (``brindle ci run`` with a validation
    plan): the checks in a scrubbed environment, each reviewer through its
    adapter, the evidence upload, and a second upload of the complete
    evidence when the server answers ``more``. The job's secrets are
    already scrubbed by the caller."""
    repo = plan["repo"]
    check_validation_checkout(plan, cwd, say=say)
    adapters =adapters or ci_adapters.default_adapters(cwd)
    if org is None:
        org = ci_adapters.repo_is_org(env, repo)
    validation_id = plan["id"]
    scrubbed = check_env(env)
    checks = [check_runner(c, cwd, scrubbed) for c in plan["checks"]]
    usage: dict = {}
    reviews: list[dict] = []
    done: set[str] = set()

    def review_all(p: dict) -> None:
        for r in p["reviewers"]:
            if r["id"] in done:
                continue
            row, u = ask_reviewer(r, cwd, env, adapters, org, checks)
            reviews.append(row)
            ci_adapters.merge_usage(usage, u)
            done.add(r["id"])

    review_all(plan)
    result = client.put_evidence(run_token, validation_id, {"checks": checks, "reviews": reviews, "usage": usage})
    if result.get("status") == "more":
        more = verify_plan(result.get("plan") or "", repo=repo, now=clock(), texts=result.get("plan_texts"))
        if more["plan_kind"] != "validation" or more["id"] != validation_id:
            raise CIError("the follow-up plan is for another validation", code="bad_plan")
        review_all(more)
        result = client.put_evidence(run_token, validation_id, {"checks": checks, "reviews": reviews, "usage": usage})
    status = result.get("status")
    if status == "posted":
        say(f"posted: {auth._sanitize(result.get('conclusion') or '?', 20)}"
            + (f" {auth._sanitize(result['check_url'], 200)}" if _str(result.get("check_url")) else ""))
    else:
        say(f"uploaded ({auth._sanitize(status or '?', 40)})")
    return result


# -- doctor ---------------------------------------------------------------------------------------------


def doctor(env: Mapping[str, str], repo: str | None, cwd: str,
           adapters: Mapping[str, ci_adapters.Adapter] | None = None, org: bool | None = None) -> str:
    adapters = adapters or ci_adapters.default_adapters(cwd)
    if org is None:
        org = ci_adapters.repo_is_org(env, repo)
    rows = ci_adapters.doctor(adapters, env, org)
    return ci_adapters.format_doctor(rows, repo, org)


# -- init -------------------------------------------------------------------------------------------------


def _gh(args: list[str], *, run=subprocess.run, input: str | None = None, interactive: bool = False) -> str:
    try:
        if interactive:
            proc = run(["gh", *args], text=True, timeout=600)
            out = ""
        else:
            proc = run(["gh", *args], capture_output=True, text=True, timeout=120, input=input,
                       stdin=None if input is not None else subprocess.DEVNULL)
            out = proc.stdout or ""
    except FileNotFoundError:
        raise CIError("the GitHub CLI (gh) isn't installed") from None
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CIError(f"gh {args[0]} failed: {e}") from e
    if proc.returncode != 0:
        err = auth._sanitize((getattr(proc, "stderr", "") or "").strip() or f"exit {proc.returncode}", 300)
        raise CIError(f"gh {' '.join(args[:2])} failed: {err}")
    return out


def _gh_api(path: str, *, run=subprocess.run, method: str | None = None, body: dict | None = None,
            jq: str | None = None) -> str | None:
    """``gh api path``: its output, or None when GitHub answers 404."""
    args = ["api", path]
    if method:
        args += ["-X", method]
    if jq:
        args += ["--jq", jq]
    if body is not None:
        args += ["--input", "-"]
    try:
        return _gh(args, run=run, input=json.dumps(body) if body is not None else None)
    except CIError as e:
        if "HTTP 404" in str(e):
            return None
        raise


def create_label(repo: str, *, run=subprocess.run, say: Callable[[str], None] = print) -> None:
    """The label that hands an issue to brindle CI (``--force``: running init
    again updates it rather than failing)."""
    _gh(["label", "create", TRIGGER_LABEL, "--repo", repo, "--color", TRIGGER_LABEL_COLOR,
         "--description", TRIGGER_LABEL_DESCRIPTION, "--force"], run=run)
    say(f"   label '{TRIGGER_LABEL}' ready: label an issue with it and brindle CI picks the issue up")


def _yaml_scalar(text: str) -> str:
    text = re.split(r"\s+#", text, maxsplit=1)[0].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return text


def workflow_jobs(text: str) -> list[str]:
    """The check names a workflow's jobs report as: each job's ``name``, or
    its id. A line scan of the top-level ``jobs:`` mapping (brindle has no
    YAML parser); a name computed by an expression is left out, since its
    check name is only known when it runs."""
    names: list[str] = []
    in_jobs = False
    job_indent = body_indent = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            in_jobs = re.match(r"jobs\s*:\s*(#.*)?$", line) is not None
            continue
        if not in_jobs:
            continue
        if job_indent is None:
            job_indent = indent
        if indent == job_indent:
            m = re.match(r"([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(#.*)?$", line)
            names.append(m.group(1) if m else "")
            body_indent = None
        elif indent > job_indent and names:
            if body_indent is None:
                body_indent = indent
            m = re.match(r"name\s*:\s*(.+)$", line)
            if indent == body_indent and m:
                names[-1] = _yaml_scalar(m.group(1))
    return [n for n in names if n and "${{" not in n]


def repo_jobs(cwd: str) -> list[str]:
    """The check names of the jobs in the checkout's workflows, brindle's own left out."""
    found: list[str] = []
    folder = Path(cwd) / SETUP_DIR
    files = sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")]) if folder.is_dir() else []
    for path in files:
        if path.name.startswith("brindle-ci-"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found += [j for j in workflow_jobs(text) if j not in found]
    return found


def required_checks(repo: str, branch: str, *, run=subprocess.run) -> list[str]:
    """The status checks ``branch`` requires (none when it isn't protected or
    requires none)."""
    out = _gh_api(f"repos/{repo}/branches/{branch}/protection/required_status_checks", run=run)
    if out is None:
        return []
    try:
        got = json.loads(out)
    except ValueError:
        return []
    if not isinstance(got, dict):
        return []
    names = [n for n in got.get("contexts") or [] if isinstance(n, str)]
    names += [c["context"] for c in got.get("checks") or []
              if isinstance(c, dict) and isinstance(c.get("context"), str) and c["context"] not in names]
    return [n for n in names if n]


def _require_checks(repo: str, branch: str, names: list[str], *, run=subprocess.run) -> None:
    """Make ``names`` required on ``branch``: on top of its protection when it
    has some (only the status checks change), else a protection of just them.
    Raises :class:`CIError` with what to tell the person when GitHub refuses."""
    plan = "private repositories need a paid GitHub plan for branch protection"
    protection = f"repos/{repo}/branches/{branch}/protection"
    try:
        if _gh_api(protection, run=run) is None:
            body = {"required_status_checks": {"strict": False, "contexts": names}, "enforce_admins": False,
                    "required_pull_request_reviews": None, "restrictions": None}
            if _gh_api(protection, run=run, method="PUT", body=body) is None:   # GitHub's 404 for "not allowed"
                raise CIError(f"GitHub refused to protect {branch} (404: no admin rights, or {plan})", "refused")
        elif _gh_api(f"{protection}/required_status_checks", run=run, method="PATCH",
                     body={"contexts": names}) is None:
            raise CIError(f"this branch is protected but doesn't require status checks; add "
                          f"{', '.join(names)} under Settings > Branches", "refused")
    except CIError as e:
        if e.code == "refused":
            raise
        raise CIError(f"GitHub didn't let brindle require {', '.join(names)}: {e} ({plan})", "refused") from e


def setup_required_checks(repo: str, *, cwd: str, run=subprocess.run,
                          ask: Callable[[str, str], str] | None = None, required_check: str | None = None,
                          no_required_check: bool = False, say: Callable[[str], None] = print) -> None:
    """Fix builds only fix the default branch's required checks, and a new
    repository has none: offer to make its workflow jobs required.
    ``required_check`` (--required-check) names the one to require without
    asking; ``no_required_check`` (--no-required-check) only warns."""
    branch = _gh(["api", f"repos/{repo}", "--jq", ".default_branch"], run=run).strip() or "main"
    have = required_checks(repo, branch, run=run)
    if have:
        say(f"   {branch} requires {', '.join(have)}: brindle fixes builds where one of them fails")
        return
    settings = (f"   to do it yourself: Settings > Branches on github.com/{repo}, add a rule for {branch}, "
                f"turn on 'Require status checks to pass' and pick the check")
    say(f"   warning: {branch} requires no status checks, and brindle only fixes builds where a required "
        "check fails, so fix builds won't run")
    if no_required_check:
        say(settings)
        return
    if required_check:
        names = [required_check]
    else:
        jobs = repo_jobs(cwd)
        if not jobs:
            say(f"   no workflow jobs in {SETUP_DIR} to require; once you have CI, mark its job as required")
            say(settings)
            return
        asker = ask or (lambda q, d: d)
        names = [job for job in jobs
                 if not asker(f"Mark '{job}' as required so brindle can fix it when it fails? [Y/n]", "y")
                 .strip().lower().startswith("n")]
        if not names:
            say(settings)
            return
    try:
        _require_checks(repo, branch, names, run=run)
    except CIError as e:
        say(f"   {e}")
        say(settings)
        return
    say(f"   {branch} now requires {', '.join(names)}")


def origin_url(cwd: str) -> str | None:
    """``cwd``'s ``origin`` remote URL, or None when there is none (or git
    can't tell: not installed, timed out)."""
    try:
        proc = git.run(["remote", "get-url", "origin"], cwd, check=False)
    except git.GitError:
        return None
    url = (proc.stdout or "").strip()
    return url if proc.returncode == 0 and url else None


def origin_repo(url: str) -> str | None:
    """The ``owner/name`` an https, ssh or scp-style remote URL names, or None."""
    m = ORIGIN_RE.search(url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def check_checkout(repo: str, cwd: str) -> None:
    """init builds the setup pull request from the checkout in ``cwd``: it
    must be a checkout of ``repo``, or the workflows would be committed and
    pushed to another repository."""
    url = origin_url(cwd)
    if url is None:
        raise CIError(f"run this inside a checkout of {repo} (no origin remote here)")
    origin = origin_repo(url)
    if origin is None:
        shown = auth._sanitize(URL_USERINFO_RE.sub(r"\1", url), 200)
        raise CIError(f"run this inside a checkout of {repo} (origin here ({shown}) isn't a GitHub owner/name)")
    if origin.lower() != repo.lower():
        raise CIError(f"run this inside a checkout of {repo} (origin here is {origin})")


def check_clean(cwd: str) -> None:
    """init commits the workflows on a branch of its own and comes back:
    with uncommitted changes it stops before doing anything, so they're
    never carried along or lost."""
    try:
        dirty = git.dirty_files(cwd, tracked_only=True)
    except git.GitError as e:
        raise CIError(f"not a git checkout: {e}") from e
    if dirty:
        raise CIError(f"commit or stash your changes first ({len(dirty)} uncommitted, e.g. "
                      f"{auth._sanitize(dirty[0], 100)}): init commits the workflows on a branch of its own")
    if git.current_branch(cwd) == SETUP_BRANCH:
        raise CIError(f"switch off {SETUP_BRANCH} first: init recreates that branch")
    for kind in SETUP_KINDS:   # a file there that git doesn't track would be overwritten, then removed
        rel = f"{SETUP_DIR}/brindle-ci-{kind}.yml"
        if (Path(cwd) / rel).exists() and not git.ok(["ls-files", "--error-unmatch", rel], cwd):
            raise CIError(f"move {rel} away first: init writes the workflow there, and git doesn't track it")


def push_setup_branch(cwd: str, files: Mapping[str, str]) -> None:
    """Commit ``files`` on :data:`SETUP_BRANCH` and push it, then put the
    checkout back where it was (also when a step fails) and delete the local
    setup branch, so the next ``git pull`` after the setup pull request is
    squash-merged doesn't diverge."""
    start = git.current_branch(cwd)
    back = ["checkout", "--quiet", start] if start else ["checkout", "--quiet", "--detach", head_sha(cwd)]
    git.run(["checkout", "-B", SETUP_BRANCH], cwd)
    committed = False
    new: list[Path] = []   # files and folders init creates, which git can't restore
    try:
        for rel, text in files.items():
            for part in [*reversed(Path(rel).parents[:-1]), Path(rel)]:   # outermost folder first
                full = Path(cwd) / part
                if not full.exists() and full not in new:
                    new.append(full)
            path = Path(cwd) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            git.run(["add", rel], cwd)
        git.run(["commit", "-q", "-m", "Add brindle CI workflows"], cwd)
        committed = True
        git.run(["push", "-u", "origin", SETUP_BRANCH], cwd)
    finally:
        if not committed:   # the tree was clean, so this only drops what was written here
            git.run(["reset", "-q", "--hard"], cwd, check=False)
            for p in reversed(new):
                try:
                    p.rmdir() if p.is_dir() else p.unlink(missing_ok=True)
                except OSError:
                    pass
        git.run(back, cwd)
        git.run(["branch", "-D", SETUP_BRANCH], cwd, check=False)


def _claude_credential(given: str | None, ask: Callable[[str, str], str] | None) -> str:
    choice = given or (ask or (lambda q, d: d))("Claude: API key or identity federation? (key, federation)", KEY)
    choice = choice.strip().lower()
    if choice not in CREDENTIALS:
        raise CIError(f"credential must be {' or '.join(CREDENTIALS)}, not {auth._sanitize(choice, 40)!r}")
    return choice


def _workspace_id(value: str) -> str:
    value = value.strip()
    if value and not WORKSPACE_ID_RE.match(value):
        raise CIError(f"a workspace ID looks like wrkspc_..., not {auth._sanitize(value, 40)!r}")
    return value


def _set_key_workspace(repo: str, env: Mapping[str, str], ask: Callable[[str, str], str] | None,
                       given: str | None, *, run=subprocess.run, say: Callable[[str], None] = print) -> None:
    """For an organization-level API key, store its workspace as the
    ``ANTHROPIC_WORKSPACE_ID`` Actions variable (not a secret), which the
    workflow turns into the anthropic-workspace-id header. ``given`` (from
    --workspace-id) skips the question, which defaults to the variable in ``env``."""
    if given is None:
        given = (ask or (lambda q, d: d))(WORKSPACE_QUESTION, env.get(WORKSPACE_VAR) or "")
    value = _workspace_id(given)
    if not value:
        return
    _gh(["variable", "set", WORKSPACE_VAR, "--repo", repo, "--body", value], run=run)
    say(f"   {WORKSPACE_VAR} set: requests carry the anthropic-workspace-id header")


def _set_federation(repo: str, env: Mapping[str, str], ask: Callable[[str, str], str] | None, *,
                    run=subprocess.run, say: Callable[[str], None] = print) -> None:
    """Store Claude's workload identity federation IDs as the repository's
    Actions variables (they aren't secrets). Each question defaults to the
    variable in ``env``, so a scripted init can pass them that way."""
    asker = ask or (lambda q, d: d)
    say("   claude: identity federation; the IDs are stored as Actions variables")
    for name, question, required in FEDERATION_VARS:
        value = asker(question, env.get(name) or "").strip()
        if name == WORKSPACE_VAR:
            value = _workspace_id(value)
        if not value:
            if required:
                raise CIError(f"{name} is required for identity federation")
            continue
        _gh(["variable", "set", name, "--repo", repo, "--body", value], run=run)
        say(f"   {name} set")
    say(f"   create the federation rule in the Claude Console: subject prefix repo:{repo}:*, "
        f"condition {federation_condition(repo)}, audience {FEDERATION_AUDIENCE}, "
        f"token lifetime at least {FEDERATION_MIN_LIFETIME_S} s")
    say("   this lets only brindle's workflows mint tokens; on pull requests the PR's copy of the validate "
        "workflow runs, so only give write access to people you trust (forks never get a token)")


def federation_condition(repo: str) -> str:
    """The CEL condition init recommends for the federation rule: the
    repository's own brindle CI workflows, not any workflow in it."""
    return (f'claims.repository == "{repo}" && '
            f'claims.workflow_ref.startsWith("{repo}/{SETUP_DIR}/brindle-ci-")')


def init(*, repo: str | None, org: str | None, providers: list[str] | None, cwd: str, env: Mapping[str, str],
         base: str | None = None, run=subprocess.run, open_url: Callable[[str], None] | None = None,
         ask: Callable[[str, str], str] | None = None, account=None, client: Client | None = None,
         credential: str | None = None, workspace_id: str | None = None, required_check: str | None = None,
         no_required_check: bool = False, say: Callable[[str], None] = print) -> None:
    """``brindle ci init``: the one-command setup. ``account`` is a
    :class:`brindle.pro.account.ProAccount` (the person's brindle Pro
    login; built with ``make`` when not given), ``ask(question, default)`` asks the person, ``open_url`` opens
    the browser. ``credential`` is how Claude signs in (``key`` or
    ``federation``; asked when not given). ``workspace_id`` is the workspace
    of an organization-level API key (asked after the key when not given).
    ``required_check`` / ``no_required_check`` answer the required-check
    question (see :func:`setup_required_checks`)."""
    import webbrowser

    from brindle.pro import account as account_mod

    refuse_airgap()
    if credential is not None:
        credential = _claude_credential(credential, None)
    if workspace_id is not None:
        workspace_id = _workspace_id(workspace_id)
    if repo:
        if not REPO_RE.match(repo):
            raise CIError("repository must be owner/name (pass --repo)")
        check_checkout(repo, cwd)
    check_clean(cwd)
    say("1/8 checking the GitHub CLI and your rights on the repository")
    _gh(["auth", "status"], run=run)
    if not repo:
        repo = _gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], run=run).strip()
        if not REPO_RE.match(repo or ""):
            raise CIError("repository must be owner/name (pass --repo)")
        check_checkout(repo, cwd)
    admin = _gh(["api", f"repos/{repo}", "--jq", ".permissions.admin"], run=run).strip().lower()
    if admin != "true":
        raise CIError(f"you need admin rights on {repo} to set its secrets and workflows")

    plugin = account or account_mod.make(cwd)
    api = client or Client(base)
    org_id = plugin._team_org(org)
    install_url = api.base + "/github/install?" + urllib.parse.urlencode({"org_id": org_id})
    say(f"2/8 install the brindle GitHub App for {repo}: {install_url}")
    (open_url or webbrowser.open)(install_url)

    say("3/8 creating the org CI token and storing it as the BRINDLE_PRO_TOKEN secret")
    got = auth.create_ci_token(plugin._client(base), plugin.store, org_id, f"ci:{repo}")
    cpc = got["token"]
    _gh(["secret", "set", ENV_TOKEN, "--repo", repo], run=run, input=cpc)
    say(f"   token {got['token_id']} created for org {got['org_id']} (the value is only in the secret)")

    say("4/8 model keys: each one goes into gh's own prompt; brindle never sees it")
    if providers is None:
        installed = [n for n, a in ci_adapters.default_adapters(cwd).items() if a.cli and a.installed()]
        answer = (ask or (lambda q, d: d))("which providers? (claude, codex)", ",".join(installed) or "claude")
        providers = [p.strip() for p in answer.split(",") if p.strip()]
    secret_names = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY"}
    for p in providers:
        if p == "claude" and _claude_credential(credential, ask) == FEDERATION:
            _set_federation(repo, env, ask, run=run, say=say)
            continue
        name = secret_names.get(p)
        if not name:
            say(f"   {p}: no secret to set here (configure a native profile in the repo)")
            continue
        say(f"   {p}: paste the key for {name}")
        _gh(["secret", "set", name, "--repo", repo], run=run, interactive=True)
        if p == "claude":
            _set_key_workspace(repo, env, ask, workspace_id, run=run, say=say)

    say(f"5/8 creating the '{TRIGGER_LABEL}' issue label")
    create_label(repo, run=run, say=say)

    say("6/8 checking the default branch's required status checks")
    setup_required_checks(repo, cwd=cwd, run=run, ask=ask, required_check=required_check,
                          no_required_check=no_required_check, say=say)

    say("7/8 fetching the workflows and opening a pull request with them")
    files = {}
    for kind in SETUP_KINDS:
        try:
            files[f"{SETUP_DIR}/brindle-ci-{kind}.yml"] = api.workflow(cpc, kind)
        except CIError as e:
            if e.code in ("not_found", "http_404", "bad_request"):
                continue
            raise
    if not files:
        raise CIError("the server offered no workflow for this org")
    push_setup_branch(cwd, files)
    url = _gh(["pr", "create", "--repo", repo, "--head", SETUP_BRANCH, "--title", "Add brindle CI",
               "--body", "Workflows from `brindle ci init`."], run=run).strip()
    say(f"   pull request: {auth._sanitize(url, 200)}")
    say(f"   you're back on {git.current_branch(cwd) or 'the commit you started on'}; "
        f"merge the pull request, then pull")

    say("8/8 doctor")
    say(doctor(env, repo, cwd, org=ci_adapters.repo_is_org(env, repo, run=run)))


__all__ = ["CIError", "Client", "check_env", "doctor", "init", "read_run_token", "run", "run_check",
           "scrub_secrets", "session_event", "start", "trigger_for", "run_validation", "report", "plan_id",
           "OIDC", "verify_plan"]
