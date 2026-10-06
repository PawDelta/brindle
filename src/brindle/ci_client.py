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

import json
import logging
import os
import re
import secrets
import subprocess
import time
import urllib.parse
import uuid
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
RUN_TOKEN_RE = re.compile(r"^cpr_[A-Za-z0-9_-]{16,256}$")
CI_TOKEN_RE = re.compile(r"^cpc_[A-Za-z0-9_-]{16,256}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PROVIDERS = ("claude", "codex", "native")

# Secrets a CI job holds that no agent may see.
SECRET_ENV = ("BRINDLE_PRO_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN",
              "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL")
SECRET_PREFIXES = ("GH_",)
# What a check command must not see either: model keys and anything token-like.
MODEL_KEY_NAMES = (*ci_adapters.CLAUDE_API_KEYS, *ci_adapters.CLAUDE_SUBSCRIPTION,
                   *ci_adapters.CODEX_API_KEYS, "GEMINI_API_KEY", "GOOGLE_API_KEY")
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
UPLOAD_MAX_RESPONSE = auth.MAX_RESPONSE
PLAN_FILE = "plan.jwt"
TOKEN_FILE = "run_token"
WORKFLOW_KINDS = ("issue", "validate", "fix")
WORKFLOW_DIR = ".github/workflows"
SETUP_BRANCH = "brindle/ci-setup"
ENV_TOKEN = "BRINDLE_PRO_TOKEN"


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
    starts). Returns the names removed."""
    gone = sorted(k for k in env if is_job_secret(k))
    for k in gone:
        del env[k]
    return gone


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


def _check_run_plan(c: dict) -> None:
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
                    and _str(m.get("check"))):
                raise CIError("run plan milestones are malformed")
    if not _str(c.get("instructions")):
        raise CIError("run plan has no instructions")
    if c.get("provider") not in PROVIDERS:
        raise CIError("run plan names an unknown provider")
    if not (c.get("profile") is None or _str(c["profile"])):
        raise CIError("run plan profile is malformed")
    c["limits"] = _limits(c.get("limits"), {"timeout_min": DEFAULT_TIMEOUT_MIN, "token_budget": 0,
                                            "heartbeat_s": DEFAULT_HEARTBEAT_S})
    if not _is_int(c.get("attempt", 1)):
        raise CIError("run plan attempt is malformed")


def _check_validation_plan(c: dict) -> None:
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
        if not (isinstance(r, dict) and _str(r.get("id")) and r.get("provider") in PROVIDERS
                and _str(r.get("instructions"))):
            raise CIError("validation plan reviewers are malformed")
    c["limits"] = _limits(c.get("limits"), {"token_budget": 0})


def verify_plan(token: str, *, repo: str, now: float | None = None) -> dict:
    """Verify a plan JWT against the pinned keys and return its claims. It
    must be a plan (``typ``, ``token_use``), for ``repo``, current, and
    well-formed for its ``plan_kind``. Raises :class:`CIError`; the
    message never includes the token."""
    now = time.time() if now is None else now
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
        _check_run_plan(c)
    elif kind == "validation":
        _check_validation_plan(c)
    else:
        raise CIError("plan has an unknown kind", code="bad_plan")
    return c


def head_sha(cwd: str) -> str:
    try:
        return git.out(["rev-parse", "--verify", "HEAD"], cwd)
    except git.GitError as e:
        raise CIError(f"not a git checkout: {e}") from e


def check_run_checkout(plan: dict, cwd: str) -> None:
    head = head_sha(cwd)
    if head != plan["base_sha"]:
        raise CIError(f"the checkout is at {head[:12]}, not the plan's base {plan['base_sha'][:12]}")


def check_validation_checkout(plan: dict, cwd: str, head: str | None = None) -> None:
    actual = head_sha(cwd)
    if actual != plan["head_sha"]:
        raise CIError(f"the checkout is at {actual[:12]}, not the plan's head {plan['head_sha'][:12]}")
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


class Client:
    """The ``/ci`` endpoints. Reuses :class:`brindle.pro.auth.Client`'s URL
    rules, transport (https only, timeouts, same-origin redirects) and
    air-gap guard."""

    def __init__(self, base: str | None = None, transport: auth.Transport | None = None) -> None:
        self.base = auth.base_url(base)
        self.transport = transport or auth.UrllibTransport()
        self._client = auth.Client(self.base, self.transport)
        self._uploads = auth.Client(self.base, transport or auth.UrllibTransport(timeout=UPLOAD_TIMEOUT))

    def _call(self, method: str, path: str, body, token: str, *, client=None) -> tuple[int, dict]:
        try:
            return (client or self._client).call(method, path, body, token)
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

    def job_status(self, token: str, run_id: str, job: str, conclusion: str) -> None:
        status, body = self._call("POST", f"/ci/runs/{urllib.parse.quote(run_id, safe='')}/job-status",
                                  auth.JSONBody(job=job, conclusion=conclusion), token)
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


def start(repo: str, trigger: dict, out_dir: str | Path, *, client: Client, token: str,
          providers: list[str], say: Callable[[str], None] = print) -> int:
    """``brindle ci start``: ask for a run; write its plan and run token to
    ``out_dir``. Exit code: 0 on a run or a duplicate, 1 otherwise."""
    status, body = client.start_run(token, repo, trigger, providers)
    if status == 201:
        run_id, plan, run_token = body.get("run_id"), body.get("plan"), body.get("run_token")
        if not (_str(run_id) and _str(plan) and isinstance(run_token, str) and RUN_TOKEN_RE.match(run_token)):
            raise CIError("the server's run answer is malformed", code="bad_response")
        out = Path(out_dir)
        write_private(out / PLAN_FILE, plan)
        write_private(out / TOKEN_FILE, run_token + "\n")
        say(f"run {auth._sanitize(run_id, 80)} started; plan and run token written to {out}")
        return 0
    if status == 409 and body.get("error") == "duplicate":
        existing = body.get("existing") if isinstance(body.get("existing"), dict) else {}
        what = (f"pull request #{existing['pr']}" if _is_int(existing.get("pr"))
                else f"run {auth._sanitize(existing.get('run_id', '?'), 80)}")
        say(f"not started: {what} already covers this (the server has commented)")
        return 0
    raise _error(status, body)


def trigger_for(issue: int | None, goal_text: str | None, dispatch: str | None) -> dict:
    given = [x for x in (issue, goal_text, dispatch) if x is not None]
    if len(given) != 1:
        raise CIError("give exactly one of --issue, --goal-text or --dispatch")
    if issue is not None:
        return {"kind": "issue", "issue": int(issue)}
    if dispatch is not None:
        if not re.fullmatch(r"run_[A-Za-z0-9_-]{4,80}", dispatch):
            raise CIError("--dispatch takes a run id (run_...)")
        return {"kind": "dispatch", "run_id": dispatch}
    text = (goal_text or "").strip()
    title, _, detail = text.partition("\n")
    return {"kind": "text", "title": title.strip(), "detail": detail.strip()}


# -- the run loop -----------------------------------------------------------------------------------


def tail(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text.encode("utf-8")) <= limit else text.encode("utf-8")[-limit:].decode("utf-8", "ignore")


def milestone_rows(db, root_id: str) -> list[dict]:
    rows = []
    for m in db.milestones(root_id):
        rows.append({"id": m.position, "status": m.status,
                     "exit": 0 if m.status == "passed" else 1 if m.status == "failed" else None,
                     "output_tail": tail(m.output, OUTPUT_TAIL)})
    return rows


def count_commits(cwd: str, base_sha: str, branch: str) -> int:
    try:
        return int(git.out(["rev-list", "--count", f"{base_sha}..{branch}"], cwd) or "0")
    except (git.GitError, ValueError) as e:
        raise CIError(f"can't count the commits: {e}") from e


def session_event(db, root_id: str, adapter: ci_adapters.Adapter, *, cwd: str | None = None,
                  base_sha: str | None = None, branch: str | None = None) -> dict:
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
    else:
        state, note = "working", f"supervisor {root.status}; autopilot {ap.state if ap else 'off'}"
    event = {"state": state, "milestones": milestone_rows(db, root_id), "usage": adapter.usage(db, root_id),
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
    path = Path(cwd) / ".git" / f"brindle-ci-{uuid.uuid4().hex}.bundle"
    try:
        git.run(["bundle", "create", str(path), f"{base_sha}..{branch}"], cwd)
        data = path.read_bytes()
    except (git.GitError, OSError) as e:
        raise CIError(f"can't write the bundle: {e}") from e
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if len(data) > BUNDLE_MAX:
        return n, None, f"bundle is {len(data)} bytes, over the {BUNDLE_MAX} limit"
    return n, data, None


def run(plan_token: str, token_path: str | Path, *, cwd: str, env: MutableMapping[str, str],
        client: Client, db=None, adapters: Mapping[str, ci_adapters.Adapter] | None = None,
        clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
        say: Callable[[str], None] = print) -> dict:
    """``brindle ci run``. Returns the server's answer to the result upload."""
    from brindle import workspaces
    from brindle.db import DB

    repo = github_repo(env)
    plan = verify_plan(plan_token, repo=repo, now=clock())
    if plan["plan_kind"] != "run":
        raise CIError("this plan isn't a run plan")
    check_run_checkout(plan, cwd)
    run_token = read_run_token(token_path)
    gone = scrub_secrets(env)
    for k in gone:
        os.environ.pop(k, None)
    run_id, base_sha, branch = plan["id"], plan["base_sha"], plan["branch"]
    try:
        git.run(["checkout", "-B", branch], cwd)
    except git.GitError as e:
        raise CIError(f"can't create branch {branch}: {e}") from e
    db = db or DB()
    ws = workspaces.adopt_root(db, cwd)
    adapters = adapters or ci_adapters.default_adapters(ws.repo_root)
    providers_used: list[str] = []
    started = clock()

    def attempt(p: dict):
        adapter = adapters.get(p["provider"])
        if adapter is None:
            raise CIError(f"no adapter for provider {p['provider']}")
        root = adapter.launch(db, ws, p["instructions"], p.get("profile"))
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
        event = session_event(db, root.id, adapter, cwd=cwd, base_sha=base_sha, branch=branch)
        try:
            answer = client.events(run_token, run_id, event)
        except CIError as e:
            if e.code in ("airgap", "bad_plan"):
                raise
            log.warning("brindle ci: heartbeat failed (%s)", e.code)
            if clock() > deadline:
                final = "timeout"
            continue
        action = answer.get("action")
        if action == "continue":
            if event["state"] in ("finished", "failed"):
                final = event["state"]
            elif clock() > deadline:
                final = "timeout"
        elif action == "stop":
            reason = answer.get("reason")
            final = reason if reason in ("budget", "timeout") else event["state"] if event["state"] != "working" else "failed"
            say(f"the server stopped the run ({auth._sanitize(reason or '?', 40)})")
        elif action == "escalate":
            new = verify_plan(answer.get("plan") or "", repo=repo, now=clock())
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
    event = session_event(db, root.id, adapter, cwd=cwd, base_sha=base_sha, branch=branch)
    commits, bundle, why = make_bundle(cwd, base_sha, branch)
    evidence = {"final_state": final, "milestones": event["milestones"], "usage": event["usage"],
                "providers": providers_used, "commits": commits}
    if event.get("question"):
        evidence["question"] = event["question"]
    if why and commits:
        evidence["note"] = why
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


def run_check(check: dict, cwd: str, env: Mapping[str, str], *, run=subprocess.run) -> dict:
    """Run one check command in ``env`` with its timeout; the raw outcome."""
    timeout = int(check.get("timeout_s", 900))
    t0 = time.monotonic()
    try:
        proc = run(check["command"], shell=True, cwd=cwd, env=dict(env), capture_output=True, text=True,
                   timeout=timeout, stdin=subprocess.DEVNULL)
        code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        code = 124
        output = ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        output += f"\n(timed out after {timeout}s)"
    except OSError as e:
        code, output = 127, str(e)
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
    row = {"id": reviewer["id"], "provider": reviewer["provider"], "model": None, "reply": ""}
    if adapter is None:
        row["error"] = "no adapter"
        return row, {}
    ok, why = ci_adapters.usable(adapter, env, org)
    if not ok:
        row["error"] = why
        return row, {}
    try:
        rev = adapter.review(instructions, cwd, env)
    except ci_adapters.AdapterError as e:
        row["error"] = str(e)
        return row, {}
    row["model"] = rev.model
    row["reply"] = tail(rev.reply, REPLY_MAX)
    if rev.exit not in (None, 0):
        row["error"] = f"exit {rev.exit}"
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


def validate(repo: str, pr: int, head: str, *, cwd: str, env: MutableMapping[str, str], client: Client,
             token: str, adapters: Mapping[str, ci_adapters.Adapter] | None = None,
             org: bool | None = None, clock: Callable[[], float] = time.time,
             check_runner: Callable[..., dict] = run_check, say: Callable[[str], None] = print) -> dict:
    """``brindle ci validate``: start and run a validation in one process."""
    if not SHA_RE.match(head or ""):
        raise CIError("--head must be a full 40-hex commit sha")
    adapters = adapters or ci_adapters.default_adapters(cwd)
    providers = ci_adapters.providers_available(adapters, env, org)
    status, body = client.start_validation(token, repo, pr, head, pr_is_fork(env, repo), providers)
    if status == 200 and body.get("skipped"):
        say(f"skipped: {auth._sanitize(body['skipped'], 40)} (the server posts the check itself)")
        return body
    if status != 201:
        raise _error(status, body)
    plan = verify_plan(body.get("plan") or "", repo=repo, now=clock())
    if plan["plan_kind"] != "validation":
        raise CIError("this plan isn't a validation plan", code="bad_plan")
    if plan["pr"] != pr:
        raise CIError("the plan is for another pull request", code="bad_plan")
    check_validation_checkout(plan, cwd, head)
    run_token = body.get("run_token")
    if not (isinstance(run_token, str) and RUN_TOKEN_RE.match(run_token)):
        raise CIError("the server's validation answer is malformed", code="bad_response")
    validation_id = plan["id"]
    gone = scrub_secrets(env)
    for k in gone:
        os.environ.pop(k, None)
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
        more = verify_plan(result.get("plan") or "", repo=repo, now=clock())
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


def init(*, repo: str | None, org: str | None, providers: list[str] | None, cwd: str, env: Mapping[str, str],
         base: str | None = None, run=subprocess.run, open_url: Callable[[str], None] | None = None,
         ask: Callable[[str, str], str] | None = None, account=None, client: Client | None = None,
         say: Callable[[str], None] = print) -> None:
    """``brindle ci init``: the one-command setup. ``account`` is a
    :class:`brindle.pro.account.AccountPlugin` (the person's brindle Pro
    login), ``ask(question, default)`` asks the person, ``open_url`` opens
    the browser."""
    import webbrowser

    from brindle.pro import account as account_mod

    refuse_airgap()
    say("1/6 checking the GitHub CLI and your rights on the repository")
    _gh(["auth", "status"], run=run)
    if not repo:
        repo = _gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], run=run).strip()
    if not REPO_RE.match(repo or ""):
        raise CIError("repository must be owner/name (pass --repo)")
    admin = _gh(["api", f"repos/{repo}", "--jq", ".permissions.admin"], run=run).strip().lower()
    if admin != "true":
        raise CIError(f"you need admin rights on {repo} to set its secrets and workflows")

    plugin = account or account_mod.AccountPlugin(cwd)
    api = client or Client(base)
    install_url = api.base + "/github/install"
    say(f"2/6 install the brindle GitHub App for {repo}: {install_url}")
    (open_url or webbrowser.open)(install_url)

    say("3/6 creating the org CI token and storing it as the BRINDLE_PRO_TOKEN secret")
    org_id = plugin._team_org(org)
    got = auth.create_ci_token(plugin._client(base), plugin.store, org_id, f"ci:{repo}")
    cpc = got["token"]
    _gh(["secret", "set", ENV_TOKEN, "--repo", repo], run=run, input=cpc)
    say(f"   token {got['token_id']} created for org {got['org_id']} (the value is only in the secret)")

    say("4/6 model keys: each one goes into gh's own prompt; brindle never sees it")
    if providers is None:
        installed = [n for n, a in ci_adapters.default_adapters(cwd).items() if a.cli and a.installed()]
        answer = (ask or (lambda q, d: d))("which providers? (claude, codex)", ",".join(installed) or "claude")
        providers = [p.strip() for p in answer.split(",") if p.strip()]
    secret_names = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY"}
    for p in providers:
        name = secret_names.get(p)
        if not name:
            say(f"   {p}: no secret to set here (configure a native profile in the repo)")
            continue
        say(f"   {p}: paste the key for {name}")
        _gh(["secret", "set", name, "--repo", repo], run=run, interactive=True)

    say("5/6 fetching the workflows and opening a pull request with them")
    files = {}
    for kind in WORKFLOW_KINDS:
        try:
            files[f"{WORKFLOW_DIR}/brindle-ci-{kind}.yml"] = api.workflow(cpc, kind)
        except CIError as e:
            if e.code in ("not_found", "http_404", "bad_request"):
                continue
            raise
    del cpc
    if not files:
        raise CIError("the server offered no workflow for this org")
    git.run(["checkout", "-B", SETUP_BRANCH], cwd)
    for rel, text in files.items():
        path = Path(cwd) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git.run(["add", rel], cwd)
    git.run(["commit", "-q", "-m", "Add brindle CI workflows"], cwd)
    git.run(["push", "-u", "origin", SETUP_BRANCH], cwd)
    url = _gh(["pr", "create", "--repo", repo, "--head", SETUP_BRANCH, "--title", "Add brindle CI",
               "--body", "Workflows from `brindle ci init`."], run=run).strip()
    say(f"   pull request: {auth._sanitize(url, 200)}")

    say("6/6 doctor")
    say(doctor(env, repo, cwd, org=ci_adapters.repo_is_org(env, repo, run=run)))


__all__ = ["CIError", "Client", "check_env", "doctor", "init", "read_run_token", "run", "run_check",
           "scrub_secrets", "session_event", "start", "trigger_for", "validate", "verify_plan"]
