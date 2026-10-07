"""The brindle CI client against a fake server: start, run (continue / stop /
escalate), the result upload, validate (including ``more``), plan
verification failures, secret scrubbing, air-gap mode and doctor."""

import hashlib
import json
import os
import re
import subprocess
import time
import uuid

import pytest

from brindle import ci_adapters, ci_client
from brindle.ci_client import CIError
from brindle.db import DB, Agent
from conftest import sh
from pro_fixtures import BASE, TEST_KID, FakeTransport, b64, pro_env, sign, signing_key  # noqa: F401

REPO = "acme/widgets"
ORG_UUID = "0f8e2b1c-6a4d-4e3b-9c7a-1d2e3f4a5b6c"
RUN_TOKEN = "crt_" + "r" * 32
CI_TOKEN = "cpc_" + "c" * 32
PLAN_HEADER = {"typ": "brindle-ci-plan+jwt"}


def plan_claims(kind: str, **over) -> dict:
    now = int(time.time())
    c = {"iss": BASE, "aud": "brindle-pro", "sub": "ci:ct_1", "org_id": "org_1", "kid": TEST_KID,
         "jti": "p1", "iat": now, "exp": now + 3600, "token_use": "ci_plan", "plan_kind": kind,
         "repo": REPO}
    if kind == "run":
        c.update({"id": "run_1", "goal": {"title": "Fix it", "detail": "d", "source": "issue", "ref": "#1"},
                  "branch": "brindle/ci-1", "base_branch": "main", "base_sha": "0" * 40,
                  "milestones": [{"id": 1, "title": "tests pass", "check": "true"}],
                  "instructions": "do the thing", "provider": "claude", "profile": None,
                  "limits": {"timeout_min": 10, "token_budget": 1000, "heartbeat_s": 1}, "attempt": 1})
    else:
        c.update({"id": "val_1", "pr": 7, "head_sha": "0" * 40,
                  "checks": [{"id": "c1", "command": "echo ok", "timeout_s": 30}],
                  "reviewers": [{"id": "r1", "provider": "claude", "instructions": "review it"}],
                  "criteria": [{"id": "k1", "text": "works"}], "mode": "advisory",
                  "limits": {"token_budget": 1000}})
    c.update(over)
    return {k: v for k, v in c.items() if v is not ...}


@pytest.fixture
def plan(signing_key):  # noqa: F811 - the fixture
    def make(kind="run", header=None, **over):
        return sign(signing_key, plan_claims(kind, **over), {**PLAN_HEADER, **(header or {})})
    return make


@pytest.fixture
def ci_repo(repo, monkeypatch):
    monkeypatch.chdir(repo)
    return repo


def head(repo) -> str:
    return sh("git rev-parse HEAD", repo)


class FakeAdapter(ci_adapters.Adapter):
    """Launches record a root agent in the DB (no process); the test moves
    the autopilot's state through ``on_launch``."""

    def __init__(self, name, *, commit=None, on_launch=None, reply="LGTM", available=True, kind="api_key"):
        self.name, self.commit, self.on_launch = name, commit, on_launch
        self.reply, self._available, self._kind = reply, available, kind
        self.launched, self.stopped, self.env_at_launch, self.reviews = [], [], [], []

    def installed(self, env=None):
        return self._available

    def credential(self, env):
        return ci_adapters.Credential(self._kind if self._available else None, ("FAKE_KEY",))

    def launch(self, db, ws, instructions, profile):
        aid = f"{self.name[:2]}{len(self.launched)}{uuid.uuid4().hex[:5]}"
        db.add_agent(Agent(id=aid, workspace_id=ws.id, profile=profile or "supervisor", provider=self.name,
                           parent_id=None, mode="interactive", status="idle", tmux_window="", result=None,
                           created_at=time.time(), task=instructions))
        db.add_autopilot(aid)
        self.launched.append((aid, instructions, profile))
        self.env_at_launch.append(dict(os.environ))
        if self.commit:
            self.commit(ws)
        if self.on_launch:
            self.on_launch(db, aid)
        return db.get_agent(aid)

    def stop(self, db, root_id):
        self.stopped.append(root_id)

    def alive(self, db, root):
        return True

    def usage(self, db, root_id):
        return {"fake-model": {"input": 10, "output": 5, "cache_read": 0}}

    def review(self, instructions, cwd, env, *, timeout=0, profile=None):
        self.reviews.append((instructions, dict(env)))
        return ci_adapters.Review(self.reply, model="fake-model", exit=0,
                                  usage={"fake-model": {"input": 3, "output": 2, "cache_read": 1}})


def commit_file(ws):
    path = os.path.join(ws.path, "fix.txt")
    with open(path, "w") as f:
        f.write("fixed\n")
    sh("git add fix.txt && git commit -qm fix", ws.path)


class Server:
    """A fake brindle CI server: records every request, scripts the answers."""

    def __init__(self, plan_token, *, actions=None, result=None, validation_plan=None, evidence=None,
                 plan_texts=None):
        self.plan_token = plan_token
        self.plan_texts = plan_texts
        self.actions = list(actions or [])
        self.events = []
        self.results = []
        self.evidence = list(evidence or [{"status": "posted", "conclusion": "success",
                                           "check_url": "https://gh.test/check"}])
        self.evidence_calls = []
        self.validation_plan = validation_plan
        self.result = result or {"status": "published", "pr": 9, "url": "https://gh.test/pr/9", "draft": False}
        self.transport = FakeTransport({
            "POST /ci/runs": self._start, "POST /ci/runs/run_1/events": self._events,
            "PUT /ci/runs/run_1/result": self._result, "POST /ci/validations": self._validation,
            "PUT /ci/validations/val_1/evidence": self._evidence,
        })
        self.client = ci_client.Client(BASE, self.transport)

    def _start(self, form, headers):
        assert headers["Authorization"] == f"Bearer {CI_TOKEN}"
        return 201, {"run_id": "run_1", "plan": self.plan_token, "run_token": RUN_TOKEN}

    def _events(self, form, headers):
        assert headers["Authorization"] == f"Bearer {RUN_TOKEN}"
        self.events.append(form)
        if self.actions:
            return 200, self.actions.pop(0)
        return 200, {"action": "continue"}

    def _result(self, form, headers):
        assert headers["Authorization"] == f"Bearer {RUN_TOKEN}"
        body = form.data
        self.results.append(body)
        return 200, self.result

    def _validation(self, form, headers):
        assert headers["Authorization"] == f"Bearer {CI_TOKEN}"
        body = {"validation_id": "val_1", "plan": self.validation_plan, "run_token": RUN_TOKEN}
        if self.plan_texts is not None:
            body["plan_texts"] = self.plan_texts
        return 201, body

    def _evidence(self, form, headers):
        assert headers["Authorization"] == f"Bearer {RUN_TOKEN}"
        self.evidence_calls.append(json.loads(json.dumps(form)))   # a copy: the client reuses its lists
        return 200, self.evidence.pop(0) if len(self.evidence) > 1 else self.evidence[0]


def evidence_of(body: bytes) -> dict:
    """The evidence JSON part of a multipart result upload."""
    marker = b'name="evidence"'
    i = body.index(marker)
    start = body.index(b"\r\n\r\n", i) + 4
    end = body.index(b"\r\n--", start)
    return json.loads(body[start:end])


def run_env(**extra):
    env = {"GITHUB_REPOSITORY": REPO, "PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"),
           "BRINDLE_PRO_TOKEN": CI_TOKEN, "GH_TOKEN": "ghp_secret", "GITHUB_TOKEN": "ghs_secret"}
    env.update(extra)
    return env


def token_file(tmp_path):
    p = tmp_path / "run_token"
    p.write_text(RUN_TOKEN + "\n")
    return p


# -- start -----------------------------------------------------------------------------------


def test_start_writes_plan_and_token(plan, tmp_path):
    token = plan()
    server = Server(token)
    said = []
    code = ci_client.start(REPO, {"kind": "issue", "issue": 1}, tmp_path / "out", client=server.client,
                           token=CI_TOKEN, providers=["claude"], say=said.append)
    assert code == 0
    assert (tmp_path / "out" / "plan.jwt").read_text() == token
    assert (tmp_path / "out" / "run_token").read_text().strip() == RUN_TOKEN
    assert oct((tmp_path / "out" / "run_token").stat().st_mode)[-3:] == "600"
    assert RUN_TOKEN not in "\n".join(said) and token not in "\n".join(said)
    key, form, _ = server.transport.calls[0]
    assert form["repo"] == REPO and form["providers_available"] == ["claude"] and "client" in form


def test_start_duplicate_exits_zero_and_errors_raise(plan, tmp_path):
    t = FakeTransport({"POST /ci/runs": [(409, {"error": "duplicate", "existing": {"pr": 5}}),
                                         (403, {"error": "repo_not_linked", "message": "install the app"})]})
    client = ci_client.Client(BASE, t)
    said = []
    assert ci_client.start(REPO, {"kind": "issue", "issue": 1}, tmp_path, client=client, token=CI_TOKEN,
                           providers=[], say=said.append) == 0
    assert "#5" in said[0]
    with pytest.raises(CIError, match="repo_not_linked") as e:
        ci_client.start(REPO, {"kind": "issue", "issue": 1}, tmp_path, client=client, token=CI_TOKEN,
                        providers=[], say=said.append)
    assert e.value.code == "repo_not_linked"
    assert not (tmp_path / "plan.jwt").exists()


def test_trigger_for():
    assert ci_client.trigger_for(3, None, None) == {"kind": "issue", "issue": 3}
    assert ci_client.trigger_for(None, "Title\nmore", None) == {"kind": "text", "title": "Title", "detail": "more"}
    assert ci_client.trigger_for(None, None, "run_abc1") == {"kind": "dispatch", "run_id": "run_abc1"}
    assert ci_client.trigger_for(validate=True, pr=7, head="0" * 40, fork=True) == {
        "kind": "validate", "pr": 7, "head_sha": "0" * 40, "fork": True}
    assert ci_client.trigger_for(validate=True, pr=7, head="0" * 40)["fork"] is False
    with pytest.raises(CIError, match="exactly one"):
        ci_client.trigger_for(1, "x", None)
    with pytest.raises(CIError, match="exactly one"):
        ci_client.trigger_for(1, validate=True, pr=7, head="0" * 40)
    with pytest.raises(CIError, match="--validate needs"):
        ci_client.trigger_for(validate=True, pr=7, head="short")
    assert ci_client.parse_bool("true") is True and ci_client.parse_bool("False") is False
    assert ci_client.parse_bool(None) is None
    with pytest.raises(CIError, match="true or false"):
        ci_client.parse_bool("maybe")


def test_oidc_header_on_every_call_inside_actions(plan, tmp_path):
    fetched = []

    def fetch(url, request_token):
        fetched.append((url, request_token))
        return "h.p.s"
    actions = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.test/x?api-version=2",
               "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req-secret"}
    clock = {"t": 1000.0}
    oidc = ci_client.OIDC(actions, fetch=fetch, clock=lambda: clock["t"])
    server = Server(plan())
    client = ci_client.Client(BASE, server.transport, oidc=oidc)
    assert ci_client.start(REPO, {"kind": "issue", "issue": 1}, tmp_path, client=client, token=CI_TOKEN,
                           providers=[], say=lambda s: None) == 0
    client.events(RUN_TOKEN, "run_1", {"state": "working"})
    for _, _, hdrs in server.transport.calls:
        assert hdrs["X-Brindle-OIDC"] == "h.p.s"
    assert fetched == [(actions["ACTIONS_ID_TOKEN_REQUEST_URL"], "req-secret")], "cached within its ttl"
    clock["t"] += 600
    client.events(RUN_TOKEN, "run_1", {"state": "working"})
    assert len(fetched) == 2
    # Outside Actions: no header at all.
    bare = ci_client.Client(BASE, Server(plan()).transport, env={})
    bare.events(RUN_TOKEN, "run_1", {"state": "working"})
    assert "X-Brindle-OIDC" not in bare.transport.calls[-1][2]
    # The source is captured at construction, so scrubbing the job's secrets later doesn't lose it.
    env = run_env(**actions)
    captured = ci_client.Client(BASE, server.transport, env=env)
    ci_client.scrub_secrets(env)
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in env and captured.oidc.available


def _jwt(exp) -> str:
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


ACTIONS = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.test/x",
           "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req-secret"}


def test_oidc_retries_a_failed_fetch():
    answers = [CIError("down", code="oidc"), CIError("down", code="oidc"), _jwt(2000)]
    slept = []

    def fetch(url, request_token):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    oidc = ci_client.OIDC(ACTIONS, fetch=fetch, clock=lambda: 1000.0, sleep=slept.append)
    assert oidc.header() == {"X-Brindle-OIDC": _jwt(2000)}
    assert slept == list(ci_client.OIDC_RETRY_DELAYS)


def test_oidc_falls_back_to_an_unexpired_token_then_gives_up():
    clock = {"t": 1000.0}
    fail = {"on": False}

    def fetch(url, request_token):
        if fail["on"]:
            raise CIError("couldn't get the GitHub Actions OIDC token (HTTP 503)", code="oidc")
        return _jwt(clock["t"] + 600)
    oidc = ci_client.OIDC(ACTIONS, fetch=fetch, clock=lambda: clock["t"], sleep=lambda s: None)
    token = oidc.header()["X-Brindle-OIDC"]
    fail["on"] = True
    clock["t"] += ci_client.OIDC_TTL + 1          # due for a refresh, but the old one is still good
    assert oidc.header() == {"X-Brindle-OIDC": token}
    clock["t"] = 1000.0 + 600                     # past its exp: no stale token is ever sent
    with pytest.raises(CIError, match="HTTP 503"):
        oidc.header()


def test_oidc_refreshes_before_the_token_expires():
    fetched = []

    def fetch(url, request_token):
        fetched.append(1)
        return _jwt(1000.0 + 60)                  # shorter-lived than OIDC_TTL
    clock = {"t": 1000.0}
    oidc = ci_client.OIDC(ACTIONS, fetch=fetch, clock=lambda: clock["t"], sleep=lambda s: None)
    oidc.header()
    clock["t"] += 60 - ci_client.OIDC_EXP_MARGIN
    oidc.header()
    assert len(fetched) == 2


@pytest.mark.parametrize("exp", ["NaN", "Infinity", "-Infinity", "1e999", "10" + "0" * 400, '"soon"', "true",
                                 "null"])
def test_jwt_exp_ignores_a_non_finite_or_non_numeric_exp(exp):
    import base64

    payload = base64.urlsafe_b64encode(('{"exp": %s}' % exp).encode()).decode().rstrip("=")
    assert ci_client._jwt_exp(f"h.{payload}.s") is None
    assert ci_client._jwt_exp(_jwt(2000)) == 2000.0
    assert ci_client._jwt_exp("not-a-jwt") is None


def test_oidc_fetch_says_why_it_failed(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    def http_error(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b""))
    monkeypatch.setattr(urllib.request, "urlopen", http_error)
    with pytest.raises(CIError, match=r"\(HTTP 503\)") as e:
        ci_client._fetch_oidc("https://token.actions.test/x", "req-secret")
    assert "req-secret" not in str(e.value) and e.value.code == "oidc"

    def timeout(req, timeout=0):
        raise urllib.error.URLError(TimeoutError("timed out"))
    monkeypatch.setattr(urllib.request, "urlopen", timeout)
    with pytest.raises(CIError, match=r"\(TimeoutError\)"):
        ci_client._fetch_oidc("https://token.actions.test/x", "req-secret")


def test_oidc_fetch_parses_the_actions_answer(monkeypatch):
    import urllib.request

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return self.body
    seen = {}

    def urlopen(req, timeout=0):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        return Resp(b'{"value": "a.b.c"}')
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert ci_client._fetch_oidc("https://token.actions.test/x?api-version=2", "req") == "a.b.c"
    assert seen["url"] == "https://token.actions.test/x?api-version=2&audience=https%3A%2F%2Fpawdelta.com%2Fbrindle"
    assert seen["auth"] == "bearer req"
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: Resp(b'{"nope": 1}'))
    with pytest.raises(CIError, match="held no token") as e:
        ci_client._fetch_oidc("https://token.actions.test/x", "req")
    assert "req" not in str(e.value) and e.value.code == "oidc"
    with pytest.raises(CIError, match="isn't https"):
        ci_client._fetch_oidc("http://token.actions.test/x", "req")


def test_plans_up_to_256kb_verify(plan, tmp_path):
    big = plan(instructions="x" * (180 * 1024))
    assert 200 * 1024 < len(big) <= 256 * 1024
    assert len(ci_client.verify_plan(big, repo=REPO)["instructions"]) == 180 * 1024
    server = Server(big)
    assert ci_client.start(REPO, {"kind": "issue", "issue": 1}, tmp_path, client=server.client, token=CI_TOKEN,
                           providers=[], say=lambda s: None) == 0
    assert (tmp_path / "plan.jwt").read_text() == big
    too_big = plan(instructions="x" * (200 * 1024))
    with pytest.raises(CIError, match="malformed plan"):
        ci_client.verify_plan(too_big, repo=REPO)
    assert ci_client.CI_MAX_RESPONSE >= 2 * ci_client.MAX_PLAN_BYTES


def test_report_posts_job_status(plan, tmp_path):
    t = FakeTransport({"POST /ci/runs/run_1/job-status": [(200, {})]})
    oidc = ci_client.OIDC({"ACTIONS_ID_TOKEN_REQUEST_URL": "https://t.actions.test/x",
                           "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req"}, fetch=lambda url, tok: "h.p.s")
    client = ci_client.Client(BASE, t, oidc=oidc)
    said = []
    ci_client.report(tmp_path, client=client, token=CI_TOKEN, start="success", run="failure", say=said.append)
    assert said == ["nothing to report: the start job wrote no plan"] and not t.calls
    (tmp_path / "plan.jwt").write_text(plan(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600))
    ci_client.report(tmp_path, client=client, token=CI_TOKEN, start="success", run="failure", say=said.append)
    assert [(c[0], c[1]) for c in t.calls] == [
        ("POST /ci/runs/run_1/job-status", {"repo": REPO, "job": "start", "conclusion": "success"}),
        ("POST /ci/runs/run_1/job-status", {"repo": REPO, "job": "run", "conclusion": "failure"})]
    assert all(c[2]["Authorization"] == f"Bearer {CI_TOKEN}" and c[2]["X-Brindle-OIDC"] == "h.p.s" for c in t.calls)
    plain = ci_client.Client(BASE, t, env={})
    ci_client.report(tmp_path, client=plain, token=CI_TOKEN, start=None, run="success", say=said.append)
    assert "X-Brindle-OIDC" not in t.calls[-1][2] and t.calls[-1][1]["repo"] == REPO
    t.calls.clear()
    assert said[-1] == "reported: run job success" and said[-2] == "reported: run job failure"
    ci_client.report(tmp_path, client=client, token=CI_TOKEN, start="cancelled", run=None, say=said.append)
    assert len(t.calls) == 1
    with pytest.raises(CIError, match="--run must be one of"):
        ci_client.report(tmp_path, client=client, token=CI_TOKEN, start=None, run="exploded", say=said.append)
    (tmp_path / "plan.jwt").write_text(plan("validation"))
    ci_client.report(tmp_path, client=client, token=CI_TOKEN, start="success", run="success", say=said.append)
    assert said[-1].startswith("nothing to report: the validation") and len(t.calls) == 1
    (tmp_path / "plan.jwt").write_text("garbage")
    with pytest.raises(CIError, match="malformed plan"):
        ci_client.report(tmp_path, client=client, token=CI_TOKEN, start="success", run=None, say=said.append)


# -- run ---------------------------------------------------------------------------------------


def finish_after(db, adapter, n):
    """A ``sleep`` for the run loop: ``adapter``'s latest supervisor reaches
    the goal (autopilot ``done``) on the ``n``-th heartbeat after its launch."""
    state = {"root": None, "beats": 0}

    def sleep(seconds):
        root = adapter.launched[-1][0] if adapter.launched else None
        if root != state["root"]:
            state.update(root=root, beats=0)
        if root is None:
            return
        state["beats"] += 1
        if state["beats"] >= n:
            db.update_autopilot(root, state="done")
    return sleep


def test_run_heartbeats_until_finished_and_uploads_bundle(plan, ci_repo, tmp_path, monkeypatch):
    base = head(ci_repo)
    token = plan(base_sha=base)
    server = Server(token)
    db = DB()
    adapter = FakeAdapter("claude", commit=commit_file)
    env = run_env()
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", CI_TOKEN)
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    tf = token_file(tmp_path)
    said = []
    result = ci_client.run(token, tf, cwd=str(ci_repo), env=env, client=server.client, db=db,
                           adapters={"claude": adapter}, sleep=finish_after(db, adapter, 2), say=said.append)
    assert result["status"] == "published"
    assert not tf.exists(), "the run token file is deleted once read"
    assert "BRINDLE_PRO_TOKEN" not in env and "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert env["GITHUB_REPOSITORY"] == REPO
    snap = adapter.env_at_launch[0]
    assert "BRINDLE_PRO_TOKEN" not in snap and "GH_TOKEN" not in snap
    assert adapter.launched[0][1] == "do the thing" and adapter.launched[0][2] is None
    assert sh("git rev-parse --abbrev-ref HEAD", ci_repo) == "brindle/ci-1"
    # the goal came from the plan
    ms = db.milestones(adapter.launched[0][0])
    assert [(m.title, m.check_cmd) for m in ms] == [("tests pass", "true")]
    # heartbeats carry state, milestones and usage; the last one says finished
    assert server.events[0]["state"] == "working"
    assert server.events[0]["milestones"][0] == {"id": 1, "status": "pending", "exit": None, "output_tail": ""}
    assert server.events[0]["usage"] == {"fake-model": {"input": 10, "output": 5, "cache_read": 0}}
    assert server.events[-1]["state"] == "finished"
    assert all(e["commits"] == 1 and e["provider_error"] is None for e in server.events)
    assert adapter.stopped == [adapter.launched[0][0]]
    body = server.results[0]
    ev = evidence_of(body)
    assert ev["final_state"] == "finished" and ev["commits"] == 1 and ev["providers"] == ["claude"]
    assert b'name="bundle"' in body and b"# v2 git bundle" in body
    assert "https://gh.test/pr/9" in said[-1]
    for _, form, hdrs in server.transport.calls:
        assert CI_TOKEN not in json.dumps(form, default=str) and "ghp_secret" not in json.dumps(form, default=str)


def test_run_obeys_stop(plan, ci_repo, tmp_path):
    base = head(ci_repo)
    token = plan(base_sha=base)
    server = Server(token, actions=[{"action": "continue"}, {"action": "stop", "reason": "budget"}])
    adapter = FakeAdapter("claude")
    result = ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                           db=DB(), adapters={"claude": adapter}, sleep=lambda s: None, say=lambda s: None)
    assert len(server.events) == 2
    assert adapter.stopped == [adapter.launched[0][0]]
    ev = evidence_of(server.results[0])
    assert ev["final_state"] == "budget" and ev["commits"] == 0
    assert b'name="bundle"' not in server.results[0], "no commits: no bundle"
    assert result["status"] == "published"


def test_run_escalates_to_a_new_provider_in_the_same_worktree(plan, ci_repo, tmp_path):
    base = head(ci_repo)
    token = plan(base_sha=base)
    second = plan(base_sha=base, provider="codex", attempt=2, instructions="try again", jti="p2",
                  limits={"timeout_min": 10, "token_budget": 1000, "heartbeat_s": 1})
    server = Server(token, actions=[{"action": "escalate", "plan": second}])
    claude = FakeAdapter("claude")
    codex = FakeAdapter("codex", commit=commit_file)
    db = DB()
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                  db=db, adapters={"claude": claude, "codex": codex}, sleep=finish_after(db, codex, 1),
                  say=lambda s: None)
    assert claude.stopped == [claude.launched[0][0]]
    assert codex.launched[0][1] == "try again"
    assert codex.launched[0][0] != claude.launched[0][0]
    assert sh("git rev-parse --abbrev-ref HEAD", ci_repo) == "brindle/ci-1"
    ev = evidence_of(server.results[0])
    assert ev["providers"] == ["claude", "codex"] and ev["final_state"] == "finished" and ev["commits"] == 1


def test_escalation_plan_must_be_the_same_run(plan, ci_repo, tmp_path):
    base = head(ci_repo)
    token = plan(base_sha=base)
    other = plan(base_sha=base, provider="codex", id="run_2", jti="p2")
    server = Server(token, actions=[{"action": "escalate", "plan": other}])
    with pytest.raises(CIError, match="another run"):
        ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                      db=DB(), adapters={"claude": FakeAdapter("claude"), "codex": FakeAdapter("codex")},
                      sleep=lambda s: None, say=lambda s: None)


def test_run_reports_needs_user(plan, ci_repo, tmp_path):
    base = head(ci_repo)
    token = plan(base_sha=base)

    db, adapter = DB(), FakeAdapter("claude")

    def ask(seconds):
        db.update_autopilot(adapter.launched[0][0], state="blocked", note="which database?")
    server = Server(token)   # the server only ever says continue
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                  db=db, adapters={"claude": adapter}, sleep=ask, say=lambda s: None)
    assert len(server.events) == 1, "needs_user ends the job at once"
    assert server.events[0]["state"] == "needs_user" and server.events[0]["question"] == "which database?"
    assert adapter.stopped == [adapter.launched[0][0]]
    ev = evidence_of(server.results[0])
    assert ev["final_state"] == "needs_user" and ev["question"] == "which database?"


def test_run_fails_fast_when_the_supervisor_cant_start(plan, ci_repo, tmp_path):
    base = head(ci_repo)
    token = plan(base_sha=base)

    class Stuck(FakeAdapter):
        def launch(self, db, ws, instructions, profile):
            raise ci_adapters.AdapterError("Claude Code stopped on its first-run theme picker, "
                                           "which nobody in CI can answer")
    server = Server(token)
    with pytest.raises(CIError, match="claude supervisor couldn't start: .*theme picker") as e:
        ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                      db=DB(), adapters={"claude": Stuck("claude")}, sleep=lambda s: None, say=lambda s: None)
    assert e.value.code == "launch" and server.events == []


def test_run_ends_when_the_supervisor_sits_on_a_first_run_screen(plan, ci_repo, tmp_path):
    base = head(ci_repo)
    token = plan(base_sha=base)

    class Stuck(FakeAdapter):
        def stuck_screen(self, db, root):
            return 'Claude Code is on its "use this API key?" question, which nobody in CI can answer'
    adapter = Stuck("claude")
    server = Server(token)   # the server only ever says continue
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                  db=DB(), adapters={"claude": adapter}, sleep=lambda s: None, say=lambda s: None)
    assert len(server.events) == 1
    assert server.events[0]["state"] == "failed" and "API key" in server.events[0]["note"]
    assert evidence_of(server.results[0])["final_state"] == "failed"


def test_run_keeps_heartbeating_while_stalled(plan, ci_repo, tmp_path):
    base = head(ci_repo)
    token = plan(base_sha=base)
    db, adapter = DB(), FakeAdapter("claude")

    def stall(seconds):
        db.update_autopilot(adapter.launched[0][0], state="stalled", note="no progress after 3 reminders")
    server = Server(token, actions=[{"action": "continue"}, {"action": "continue"},
                                    {"action": "stop", "reason": "timeout"}])
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                  db=db, adapters={"claude": adapter}, sleep=stall, say=lambda s: None)
    assert [e["state"] for e in server.events] == ["stalled"] * 3, "the server decides what a stall means"
    assert evidence_of(server.results[0])["final_state"] == "timeout"


def test_milestones_carry_the_plans_ids_and_may_lack_a_check(plan, ci_repo, tmp_path):
    base = head(ci_repo)
    token = plan(base_sha=base, milestones=[{"id": 7, "title": "design", "check": None},
                                           {"id": 9, "title": "tests pass", "check": "true"}])
    db, adapter = DB(), FakeAdapter("claude")

    def pass_second(seconds):
        root = adapter.launched[0][0]
        db.record_check(db.milestones(root)[1].id, True, "ok\n")
        db.update_autopilot(root, state="done")
    server = Server(token)
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                  db=db, adapters={"claude": adapter}, sleep=pass_second, say=lambda s: None)
    rows = server.events[0]["milestones"]
    assert [(r["id"], r["status"], r["exit"]) for r in rows] == [(7, "pending", None), (9, "passed", 0)]
    assert rows[1]["output_tail"] == "ok\n"
    assert [r["id"] for r in evidence_of(server.results[0])["milestones"]] == [7, 9]
    with pytest.raises(CIError, match="milestones are malformed"):
        ci_client.verify_plan(plan(milestones=[{"id": 1, "title": "x", "check": ""}]), repo=REPO)


def test_result_has_only_protocol_fields_without_a_bundle(plan, ci_repo, tmp_path, monkeypatch):
    base = head(ci_repo)
    token = plan(base_sha=base)
    server = Server(token, actions=[{"action": "stop", "reason": "budget"}])
    monkeypatch.setattr(ci_client, "BUNDLE_MAX", 10)
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                  db=DB(), adapters={"claude": FakeAdapter("claude", commit=commit_file)}, sleep=lambda s: None,
                  say=lambda s: None)
    body = server.results[0]
    ev = evidence_of(body)
    assert b'name="bundle"' not in body, "over the limit: no bundle"
    assert ev["final_state"] == "budget" and ev["commits"] == 1
    assert set(ev) <= {"final_state", "milestones", "usage", "providers", "question", "commits"}


def test_run_reports_provider_errors(plan, ci_repo, tmp_path, monkeypatch):
    from brindle import providers

    base = head(ci_repo)
    token = plan(base_sha=base)
    db, adapter = DB(), FakeAdapter("claude")
    adapter.provider_error = lambda db_, root: ci_adapters.Adapter.provider_error(adapter, db_, root)
    beats = []

    from brindle import quota

    def sleep(seconds):
        beats.append(1)
        if len(beats) == 1:
            monkeypatch.setattr(quota, "get", lambda provider: quota.Quota(provider, [], time.time() + 600, 0.0, "t"))
        elif len(beats) == 2:
            monkeypatch.setattr(quota, "get", lambda provider: None)
            monkeypatch.setattr(providers, "signed_out", lambda provider, env=None: "signed out")
    server = Server(token, actions=[{"action": "continue"}, {"action": "continue"},
                                    {"action": "stop", "reason": "cancelled"}])
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                  db=db, adapters={"claude": adapter}, sleep=sleep, say=lambda s: None)
    assert [(e["state"], e["provider_error"]) for e in server.events] == [
        ("working", "rate_limit"), ("working", "auth"), ("working", "auth")]
    assert all(e["commits"] == 0 for e in server.events)
    assert evidence_of(server.results[0])["final_state"] == "failed", "cancelled while working"


def test_report_posts_a_failed_validation_run_job(plan, tmp_path):
    t = FakeTransport({"POST /ci/validations/val_1/job-status": [(200, {})]})
    oidc = ci_client.OIDC({"ACTIONS_ID_TOKEN_REQUEST_URL": "https://t.actions.test/x",
                           "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req"}, fetch=lambda url, tok: "h.p.s")
    client = ci_client.Client(BASE, t, oidc=oidc)
    said = []
    (tmp_path / "plan.jwt").write_text(plan("validation"))
    # The run job uploaded: nothing to report, whatever the start job said.
    ci_client.report(tmp_path, client=client, token=CI_TOKEN, start="failure", run="success", say=said.append)
    ci_client.report(tmp_path, client=client, token=CI_TOKEN, start="success", run=None, say=said.append)
    assert not t.calls
    # The run job died: the server posts the check, told with the CI token when the run token is gone.
    ci_client.report(tmp_path, client=client, token=CI_TOKEN, start="success", run="failure", say=said.append)
    assert t.calls[-1][:2] == ("POST /ci/validations/val_1/job-status", {"repo": REPO, "job": "run", "conclusion": "failure"})
    assert t.calls[-1][2]["Authorization"] == f"Bearer {CI_TOKEN}" and t.calls[-1][2]["X-Brindle-OIDC"] == "h.p.s"
    assert said[-1] == "reported: run job failure"
    # The run job never ran (cancelled): its token is still in the artifact and is used for this one call.
    token_file(tmp_path)
    ci_client.report(tmp_path, client=client, token=CI_TOKEN, start="success", run="cancelled", say=said.append)
    assert t.calls[-1][1]["conclusion"] == "cancelled" and t.calls[-1][2]["Authorization"] == f"Bearer {RUN_TOKEN}"
    assert len(t.calls) == 2
    with pytest.raises(CIError, match="--run must be one of"):
        ci_client.report(tmp_path, client=client, token=CI_TOKEN, start=None, run="nope", say=said.append)


def test_start_branch_conflict_exits_zero(plan, tmp_path):
    t = FakeTransport({"POST /ci/runs": [(409, {"error": "branch_conflict",
                                                "message": "brindle/ci-123 has commits brindle didn't make"})]})
    said = []
    code = ci_client.start(REPO, {"kind": "issue", "issue": 123}, tmp_path / "out", client=ci_client.Client(BASE, t),
                           token=CI_TOKEN, providers=[], say=said.append)
    assert code == 0 and said == ["not started: brindle/ci-123 has commits brindle didn't make (the server has commented)"]
    assert not (tmp_path / "out").exists()


def test_start_dispatch_jira_text_gone_exits_zero(plan, tmp_path):
    t = FakeTransport({"POST /ci/runs": [(410, {"error": "jira_text_gone"})]})
    said = []
    code = ci_client.start(REPO, {"kind": "dispatch", "run_id": "run_abc"}, tmp_path / "out",
                           client=ci_client.Client(BASE, t), token=CI_TOKEN, providers=[], say=said.append)
    assert code == 0 and said[0].startswith("not started: the Jira ticket's text has expired")
    assert not (tmp_path / "out").exists()
    # any other 410 is still an error
    t = FakeTransport({"POST /ci/runs": [(410, {"error": "gone"})]})
    with pytest.raises(ci_client.CIError):
        ci_client.start(REPO, {"kind": "dispatch", "run_id": "run_abc"}, tmp_path / "out",
                        client=ci_client.Client(BASE, t), token=CI_TOKEN, providers=[], say=said.append)

def push_branch(repo, branch: str) -> str:
    """A branch with one commit past main, pushed to origin and deleted
    locally (as a fresh CI checkout would see it). Returns its tip."""
    sh(f"git checkout -q -b {branch}", repo)
    (repo / "earlier.txt").write_text("from the first run\n")
    sh("git add earlier.txt && git commit -qm earlier", repo)
    tip = sh("git rev-parse HEAD", repo)
    sh(f"git push -q origin {branch} && git checkout -q main && git branch -q -D {branch}", repo)
    return tip


def test_run_continuation_checks_out_the_existing_branch(plan, ci_repo, tmp_path):
    tip = push_branch(ci_repo, "brindle/ci-1")
    main = head(ci_repo)
    token = plan(base_sha=tip, continuation={"pr": 12})
    server = Server(token)
    db, adapter = DB(), FakeAdapter("claude", commit=commit_file)
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client, db=db,
                  adapters={"claude": adapter}, sleep=finish_after(db, adapter, 1), say=lambda s: None)
    assert sh("git rev-parse --abbrev-ref HEAD", ci_repo) == "brindle/ci-1"
    assert sh("git rev-parse HEAD~1", ci_repo) == tip, "the new commit sits on the branch tip"
    assert sh("git merge-base --is-ancestor " + main + " HEAD && echo yes", ci_repo) == "yes"
    ev = evidence_of(server.results[0])
    assert ev["commits"] == 1 and ev["final_state"] == "finished"
    assert b'name="bundle"' in server.results[0]
    assert server.events[0]["commits"] == 1


def test_run_continuation_refuses_a_moved_branch(plan, ci_repo, tmp_path):
    tip = push_branch(ci_repo, "brindle/ci-1")
    token = plan(base_sha="3" * 40, continuation={"pr": 12})
    tf = token_file(tmp_path)
    with pytest.raises(CIError, match=f"brindle/ci-1 is at {tip[:12]}, not the plan's base 333333333333"):
        ci_client.run(token, tf, cwd=str(ci_repo), env=run_env(), client=Server(token).client, db=DB(),
                      adapters={"claude": FakeAdapter("claude")}, sleep=lambda s: None)
    assert not tf.exists()
    missing = plan(base_sha=tip, branch="brindle/ci-9", continuation={"pr": 12})
    with pytest.raises(CIError, match="brindle/ci-9 doesn't exist"):
        ci_client.run(missing, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=Server(missing).client,
                      db=DB(), adapters={"claude": FakeAdapter("claude")}, sleep=lambda s: None)
    with pytest.raises(CIError, match="continuation is malformed"):
        ci_client.verify_plan(plan(continuation={"pr": "12"}), repo=REPO)
    with pytest.raises(CIError, match="continuation is malformed"):
        ci_client.verify_plan(plan(continuation="yes"), repo=REPO)


def test_run_rejects_a_checkout_not_at_base(plan, ci_repo, tmp_path):
    token = plan(base_sha="1" * 40)
    tf = token_file(tmp_path)
    with pytest.raises(CIError, match="not the plan's base"):
        ci_client.run(token, tf, cwd=str(ci_repo), env=run_env(), client=Server(token).client, db=DB(),
                      adapters={"claude": FakeAdapter("claude")}, sleep=lambda s: None)
    assert not tf.exists(), "the token file is deleted whatever happens to the plan"


def test_run_keeps_going_when_a_heartbeat_fails(plan, ci_repo, tmp_path):
    from brindle.pro.auth import TransportError

    base = head(ci_repo)
    token = plan(base_sha=base)
    server = Server(token, actions=[{"action": "continue"}, {"action": "stop", "reason": "timeout"}])
    flaky = [TransportError("down")]
    real = server._events

    def events(form, headers):
        if flaky:
            raise flaky.pop()
        return real(form, headers)
    server.transport.routes["POST /ci/runs/run_1/events"] = events
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                  db=DB(), adapters={"claude": FakeAdapter("claude")}, sleep=lambda s: None, say=lambda s: None)
    assert evidence_of(server.results[0])["final_state"] == "timeout"


def test_run_ends_past_the_plan_timeout_only_when_the_server_is_unreachable(plan, ci_repo, tmp_path):
    from brindle.pro.auth import TransportError

    base = head(ci_repo)
    token = plan(base_sha=base)
    server = Server(token)
    clock = {"t": time.time()}
    attempts = []

    def tick(s):
        clock["t"] += 15 * 60

    def down(form, headers):
        attempts.append(1)
        raise TransportError("down")
    server.transport.routes["POST /ci/runs/run_1/events"] = down
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                  db=DB(), adapters={"claude": FakeAdapter("claude")}, clock=lambda: clock["t"], sleep=tick,
                  say=lambda s: None)
    assert evidence_of(server.results[0])["final_state"] == "timeout" and len(attempts) <= 3
    # A server that answers keeps the run alive past the plan's timeout: timing out is its call.
    server2 = Server(token, actions=[{"action": "continue"}] * 5 + [{"action": "stop", "reason": "timeout"}])
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server2.client,
                  db=DB(), adapters={"claude": FakeAdapter("claude")}, clock=lambda: clock["t"], sleep=tick,
                  say=lambda s: None)
    assert len(server2.events) == 6


# -- plan verification ---------------------------------------------------------------------------


def test_plan_verification_failures(plan, signing_key):  # noqa: F811
    now = time.time()
    good = plan()
    assert ci_client.verify_plan(good, repo=REPO)["id"] == "run_1"
    with pytest.raises(CIError, match="not a plan"):
        ci_client.verify_plan(plan(header={"typ": "brindle-entitlement+jwt"}), repo=REPO)
    with pytest.raises(CIError, match="not a plan"):
        ci_client.verify_plan(plan(token_use="entitlement"), repo=REPO)
    with pytest.raises(CIError, match="repository other/repo, not"):
        ci_client.verify_plan(plan(repo="other/repo"), repo=REPO)
    h, p, s = good.split(".")
    with pytest.raises(CIError, match="signature is invalid"):
        ci_client.verify_plan(f"{h}.{p}.{s[:-4]}AAAA", repo=REPO)
    forged = b64(json.dumps(plan_claims("run", goal={"title": "evil"})).encode())
    with pytest.raises(CIError, match="signature is invalid"):
        ci_client.verify_plan(f"{h}.{forged}.{s}", repo=REPO)
    with pytest.raises(CIError, match="expired"):
        ci_client.verify_plan(plan(iat=int(now) - 7200, exp=int(now) - 3600), repo=REPO)
    with pytest.raises(CIError, match="not valid yet"):
        ci_client.verify_plan(plan(iat=int(now) + 7200, exp=int(now) + 9000), repo=REPO)
    with pytest.raises(CIError, match="unknown key"):
        ci_client.verify_plan(plan(header={"kid": "nope"}, kid="nope"), repo=REPO)
    with pytest.raises(CIError, match="unknown provider"):
        ci_client.verify_plan(plan(provider="gemini"), repo=REPO)
    with pytest.raises(CIError, match="no instructions"):
        ci_client.verify_plan(plan(instructions=""), repo=REPO)
    with pytest.raises(CIError, match="missing claims: id"):
        ci_client.verify_plan(plan(id=...), repo=REPO)
    with pytest.raises(CIError, match="unknown kind"):
        ci_client.verify_plan(plan(plan_kind="other"), repo=REPO)
    with pytest.raises(CIError, match="malformed"):
        ci_client.verify_plan("not.a.jwt", repo=REPO)


def test_plan_errors_never_quote_the_token(plan):
    bad = plan(repo="other/repo")
    with pytest.raises(CIError) as e:
        ci_client.verify_plan(bad, repo=REPO)
    assert bad[:20] not in str(e.value)


def test_plan_defaults_limits(plan):
    c = ci_client.verify_plan(plan(limits=...), repo=REPO)
    assert c["limits"] == {"timeout_min": 100, "token_budget": 0, "heartbeat_s": 60}


# -- the org's protected paths and dollar budget (Team org_budgets) --------------------------------


@pytest.mark.parametrize("over", [{"protected_paths": "infra/"}, {"protected_paths": [""]},
                                  {"protected_paths": [1]}, {"protected_paths": ["x"] * 65},
                                  {"budget_usd": "5"}, {"budget_usd": -1}, {"budget_usd": True}])
def test_plan_org_limits_must_be_well_formed(plan, over):
    with pytest.raises(CIError, match="malformed"):
        ci_client.verify_plan(plan(**over), repo=REPO)


def test_plan_org_limits_are_optional_and_kept(plan):
    c = ci_client.verify_plan(plan(protected_paths=["infra/"], budget_usd=5), repo=REPO)
    assert c["protected_paths"] == ["infra/"] and c["budget_usd"] == 5
    assert "protected_paths" not in ci_client.verify_plan(plan(), repo=REPO)


def test_the_token_budget_is_priced_in_dollars(plan, monkeypatch):
    from brindle import pricing

    monkeypatch.setattr(pricing, "profile_price", lambda name, root, extra=None: pricing.Price(10, 50, 12.5, 1))
    c = ci_client.verify_plan(plan(profile="big"), repo=REPO)
    # a typical task (200k in, 40k out, 600k cache read) is $4.60 for 840k tokens
    assert ci_client.token_budget_usd(c, ".") == pytest.approx(4.6 * 1000 / 840_000)
    monkeypatch.setattr(pricing, "profile_price", lambda name, root, extra=None: None)
    assert ci_client.token_budget_usd(c, ".") is None


def test_a_run_that_changes_a_protected_path_publishes_nothing(plan, ci_repo, tmp_path):
    base = head(ci_repo)
    token = plan(base_sha=base, protected_paths=["fix.txt"])
    server = Server(token)
    db = DB()
    adapter = FakeAdapter("claude", commit=commit_file)
    said = []
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client, db=db,
                  adapters={"claude": adapter}, sleep=finish_after(db, adapter, 1), say=said.append)
    ev = evidence_of(server.results[0])
    assert ev["final_state"] == "failed" and ev["commits"] == 0
    assert "fix.txt" in ev["question"] and "protected" in ev["question"]
    assert b'name="bundle"' not in server.results[0]


def test_a_run_outside_the_protected_paths_is_published(plan, ci_repo, tmp_path):
    base = head(ci_repo)
    token = plan(base_sha=base, protected_paths=["infra/", ".github/workflows"])
    server = Server(token)
    db = DB()
    adapter = FakeAdapter("claude", commit=commit_file)
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client, db=db,
                  adapters={"claude": adapter}, sleep=finish_after(db, adapter, 1), say=lambda s: None)
    ev = evidence_of(server.results[0])
    assert ev["final_state"] == "finished" and ev["commits"] == 1
    assert b'name="bundle"' in server.results[0]


def test_an_escalation_plan_cannot_drop_the_orgs_limits(plan, ci_repo, tmp_path):
    base = head(ci_repo)
    first = plan(base_sha=base, protected_paths=["fix.txt"], budget_usd=5)
    esc = plan(base_sha=base, provider="codex", attempt=2, budget_usd=50)    # no protected_paths
    server = Server(first, actions=[{"action": "escalate", "plan": esc}])
    db = DB()
    claude = FakeAdapter("claude")
    codex = FakeAdapter("codex", commit=commit_file)
    ci_client.run(first, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client, db=db,
                  adapters={"claude": claude, "codex": codex}, sleep=finish_after(db, codex, 1),
                  say=lambda s: None)
    ev = evidence_of(server.results[0])
    assert ev["final_state"] == "failed" and "fix.txt" in ev["question"]


def test_a_run_stops_at_the_orgs_dollar_budget(plan, ci_repo, tmp_path, monkeypatch):
    from brindle import pricing

    monkeypatch.setitem(pricing.PRICES, "fake-model", pricing.Price(1_000_000, 1_000_000, 0, 0))
    base = head(ci_repo)
    token = plan(base_sha=base, budget_usd=1)
    server = Server(token)
    adapter = FakeAdapter("claude")
    said = []
    ci_client.run(token, token_file(tmp_path), cwd=str(ci_repo), env=run_env(), client=server.client,
                  db=DB(), adapters={"claude": adapter}, sleep=lambda s: None, say=said.append)
    assert len(server.events) == 1
    assert evidence_of(server.results[0])["final_state"] == "budget"
    assert any("org's $1.00 limit" in s for s in said)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_plan_instructions_inline_or_by_hash(plan):
    assert ci_client.verify_plan(plan(), repo=REPO)["instructions"] == "do the thing"
    text = "review this diff: ✓\n" + "x" * 8000
    h = sha256(text)
    c = ci_client.verify_plan(plan(instructions=..., instructions_sha256=h), repo=REPO, texts={h: text})
    assert c["instructions"] == text
    reviewers = [{"id": "r1", "provider": "claude", "instructions_sha256": h},
                 {"id": "r2", "provider": "codex", "instructions": "inline"}]
    v = ci_client.verify_plan(plan("validation", reviewers=reviewers), repo=REPO, texts={h: text, "f" * 64: "spare"})
    assert [r["instructions"] for r in v["reviewers"]] == [text, "inline"]


def test_plan_instructions_by_hash_are_checked(plan):
    text = "a long issue body"
    h = sha256(text)
    by_hash = plan(instructions=..., instructions_sha256=h)
    with pytest.raises(CIError, match="instructions text is missing"):
        ci_client.verify_plan(by_hash, repo=REPO)
    with pytest.raises(CIError, match="instructions text is missing"):
        ci_client.verify_plan(by_hash, repo=REPO, texts={sha256("other"): "other"})
    with pytest.raises(CIError, match="doesn't match its hash") as e:
        ci_client.verify_plan(by_hash, repo=REPO, texts={h: text + " and more"})
    assert e.value.code == "bad_plan"
    for bad in (h.upper(), h[:-1], 12, ""):
        with pytest.raises(CIError, match="instructions_sha256 is malformed"):
            ci_client.verify_plan(plan(instructions=..., instructions_sha256=bad), repo=REPO, texts={h: text})
    with pytest.raises(CIError, match="both instructions and instructions_sha256"):
        ci_client.verify_plan(plan(instructions_sha256=h), repo=REPO, texts={h: text})
    with pytest.raises(CIError, match="plan texts are malformed"):
        ci_client.verify_plan(by_hash, repo=REPO, texts=[text])
    with pytest.raises(CIError, match="plan texts are malformed"):
        ci_client.verify_plan(by_hash, repo=REPO, texts="{not json")
    with pytest.raises(CIError, match="isn't valid UTF-8") as e:
        ci_client.verify_plan(by_hash, repo=REPO, texts={h: "lone \ud800 surrogate"})
    assert e.value.code == "bad_plan"
    tampered = [{"id": "r1", "provider": "claude", "instructions_sha256": h}]
    with pytest.raises(CIError, match="reviewer r1 instructions text doesn't match"):
        ci_client.verify_plan(plan("validation", reviewers=tampered), repo=REPO, texts={h: "evil"})
    both = [{"id": "r1", "provider": "claude", "instructions_sha256": h, "instructions": text}]
    with pytest.raises(CIError, match="reviewer r1 has both"):
        ci_client.verify_plan(plan("validation", reviewers=both), repo=REPO, texts={h: text})


# -- secrets ---------------------------------------------------------------------------------------


def test_scrub_secrets_and_check_env():
    env = {"BRINDLE_PRO_TOKEN": "x", "GH_TOKEN": "y", "GH_HOST": "github.com", "GITHUB_TOKEN": "z",
           "GITHUB_REPOSITORY": REPO, "ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "o", "PATH": "/bin",
           "MY_SERVICE_TOKEN": "t"}
    gone = ci_client.scrub_secrets(env)
    assert gone == ["BRINDLE_PRO_TOKEN", "GH_HOST", "GH_TOKEN", "GITHUB_TOKEN"]
    assert set(env) == {"GITHUB_REPOSITORY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "PATH",
                        "MY_SERVICE_TOKEN"}
    assert set(ci_client.check_env(env)) == {"GITHUB_REPOSITORY", "PATH"}


def test_scrub_secrets_drops_empty_keys():
    """The workflow sets ANTHROPIC_API_KEY from a secret that may not exist
    (an empty string), which Claude Code would take over the federation
    token in ANTHROPIC_AUTH_TOKEN."""
    env = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-x", "BRINDLE_PRO_TOKEN": "x",
           "PATH": "/bin"}
    assert ci_client.scrub_secrets(env) == ["ANTHROPIC_API_KEY", "BRINDLE_PRO_TOKEN"]
    assert env == {"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-x", "PATH": "/bin"}


def test_custom_headers_are_not_scrubbed():
    """An organization-level key's anthropic-workspace-id header reaches
    every claude process; it is not a secret."""
    env = {"ANTHROPIC_CUSTOM_HEADERS": "anthropic-workspace-id: wrkspc_1", "BRINDLE_PRO_TOKEN": "x"}
    ci_client.scrub_secrets(env)
    assert env == {"ANTHROPIC_CUSTOM_HEADERS": "anthropic-workspace-id: wrkspc_1"}
    assert ci_adapters.claude_env(env) == env


FEDERATION = {"ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1", "ANTHROPIC_ORGANIZATION_ID": "org-uuid",
              "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_1", "ANTHROPIC_WORKSPACE_ID": "wrkspc_1",
              "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.test/x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req-secret",
              "ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-from-the-workflow"}
FRESH = "sk-ant-oat01-minted-by-the-run"


@pytest.fixture
def federation(monkeypatch):
    """A job set up for identity federation: a fake exchange, and the
    variables the run sets on this process restored afterwards."""
    from brindle import ci_federation

    calls = {"fetch": [], "exchange": [], "refuse": None}

    def fetch(url, request_token):
        calls["fetch"].append((url, request_token))
        return "h.p.s"

    def exchange(assertion, ids):
        calls["exchange"].append((assertion, dict(ids)))
        if calls["refuse"]:
            raise ci_federation.FederationError(calls["refuse"])
        return FRESH, 598.0
    monkeypatch.setattr(ci_federation, "fetch_identity_token", fetch)
    monkeypatch.setattr(ci_federation, "exchange_token", exchange)
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.setenv(k, "placeholder")   # so monkeypatch restores their absence
        monkeypatch.delenv(k)
    return calls


def _port_closed(base_url: str) -> bool:
    import socket

    host, port = base_url.split("://", 1)[1].split(":")
    try:
        socket.create_connection((host, int(port)), timeout=2).close()
    except OSError:
        return True
    return False


def test_run_lends_a_refreshed_federated_token_through_its_proxy(plan, ci_repo, tmp_path, federation):
    base = head(ci_repo)
    server = Server(plan(base_sha=base))
    db = DB()
    adapter = FakeAdapter("claude", commit=commit_file)
    env = run_env(**FEDERATION)
    said = []
    result = ci_client.run(plan(base_sha=base), token_file(tmp_path), cwd=str(ci_repo), env=env, client=server.client,
                           db=db, adapters={"claude": adapter}, sleep=finish_after(db, adapter, 2), say=said.append)
    assert result["status"] == "published"
    assert federation["fetch"] == [("https://token.actions.test/x", "req-secret")]
    assert federation["exchange"][0][1] == {k: FEDERATION[k] for k in ("ANTHROPIC_FEDERATION_RULE_ID",
                                                                        "ANTHROPIC_ORGANIZATION_ID",
                                                                        "ANTHROPIC_SERVICE_ACCOUNT_ID",
                                                                        "ANTHROPIC_WORKSPACE_ID")}
    snap = adapter.env_at_launch[0]
    assert snap["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    secret = snap["ANTHROPIC_AUTH_TOKEN"]
    assert secret not in (FRESH, FEDERATION["ANTHROPIC_AUTH_TOKEN"]) and len(secret) >= 32
    assert env["ANTHROPIC_BASE_URL"] == snap["ANTHROPIC_BASE_URL"] and env["ANTHROPIC_AUTH_TOKEN"] == secret
    for e in (snap, env):
        assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in e and "ACTIONS_ID_TOKEN_REQUEST_URL" not in e
        assert FRESH not in e.values() and "req-secret" not in e.values()
    assert "ANTHROPIC_BASE_URL" not in os.environ and "ANTHROPIC_AUTH_TOKEN" not in os.environ \
        or _port_closed(snap["ANTHROPIC_BASE_URL"])
    assert _port_closed(snap["ANTHROPIC_BASE_URL"]), "the proxy is gone with the run"
    assert any("refreshes its Anthropic token itself" in s for s in said)
    everything = "\n".join(said) + json.dumps(server.transport.calls, default=str)
    assert FRESH not in everything and secret not in everything and "req-secret" not in everything


def test_run_keeps_the_workflow_token_when_the_exchange_fails(plan, ci_repo, tmp_path, federation):
    federation["refuse"] = "the Anthropic token exchange failed (HTTP 401: Authentication failed)"
    base = head(ci_repo)
    server = Server(plan(base_sha=base))
    db = DB()
    adapter = FakeAdapter("claude", commit=commit_file)
    env = run_env(**FEDERATION)
    said = []
    result = ci_client.run(plan(base_sha=base), token_file(tmp_path), cwd=str(ci_repo), env=env, client=server.client,
                           db=db, adapters={"claude": adapter}, sleep=finish_after(db, adapter, 2), say=said.append)
    assert result["status"] == "published"
    snap = adapter.env_at_launch[0]   # this process's environment, which the agents inherit
    assert "ANTHROPIC_AUTH_TOKEN" not in snap and "ANTHROPIC_BASE_URL" not in snap
    assert env["ANTHROPIC_AUTH_TOKEN"] == FEDERATION["ANTHROPIC_AUTH_TOKEN"] and "ANTHROPIC_BASE_URL" not in env
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in snap
    assert any("HTTP 401" in s and "using the workflow's token" in s for s in said)


def test_run_without_federation_leaves_the_credential_alone(plan, ci_repo, tmp_path, federation):
    base = head(ci_repo)
    server = Server(plan(base_sha=base))
    db = DB()
    adapter = FakeAdapter("claude", commit=commit_file)
    env = run_env(ANTHROPIC_API_KEY="sk-ant-api03-key", **{k: v for k, v in FEDERATION.items()
                                                           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")})
    ci_client.run(plan(base_sha=base), token_file(tmp_path), cwd=str(ci_repo), env=env, client=server.client,
                  db=db, adapters={"claude": adapter}, sleep=finish_after(db, adapter, 2), say=lambda s: None)
    assert federation["exchange"] == [], "a key wins in Claude Code, so there is nothing to refresh"
    snap = adapter.env_at_launch[0]
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-key" and "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_BASE_URL" not in snap and "ANTHROPIC_AUTH_TOKEN" not in snap


def test_validate_reviewers_use_the_proxy_and_checks_see_no_secret(plan, ci_repo, tmp_path, federation):
    sha = head(ci_repo)
    vplan = plan("validation", head_sha=sha,
                 checks=[{"id": "c1", "command": "printenv ANTHROPIC_AUTH_TOKEN ACTIONS_ID_TOKEN_REQUEST_TOKEN "
                                                 "ANTHROPIC_BASE_URL; exit 0", "timeout_s": 30}])
    server = Server(plan(), validation_plan=vplan)
    adapter = FakeAdapter("claude", reply="Looks fine.")
    env = run_env(**FEDERATION)
    result = validate(server, ci_repo, tmp_path, adapters={"claude": adapter}, env=env)
    assert result["status"] == "posted"
    _, review_env = adapter.reviews[0]
    assert review_env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    secret = review_env["ANTHROPIC_AUTH_TOKEN"]
    assert secret not in (FRESH, FEDERATION["ANTHROPIC_AUTH_TOKEN"])
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in review_env
    out = server.evidence_calls[0]["checks"][0]["output_excerpt"]
    assert secret not in out and "req-secret" not in out and FRESH not in out
    assert _port_closed(review_env["ANTHROPIC_BASE_URL"])


def test_read_run_token_deletes_the_file(tmp_path):
    p = token_file(tmp_path)
    assert ci_client.read_run_token(p) == RUN_TOKEN
    assert not p.exists()
    with pytest.raises(CIError, match="can't read"):
        ci_client.read_run_token(p)
    p.write_text("garbage")
    with pytest.raises(CIError, match="doesn't hold a run token"):
        ci_client.read_run_token(p)
    assert not p.exists()


def test_ci_token_from_env():
    assert ci_client.ci_token({"BRINDLE_PRO_TOKEN": CI_TOKEN}) == CI_TOKEN
    with pytest.raises(CIError, match="isn't set"):
        ci_client.ci_token({})
    with pytest.raises(CIError, match="isn't an org CI token"):
        ci_client.ci_token({"BRINDLE_PRO_TOKEN": "nope"})


# -- validate ----------------------------------------------------------------------------------------




def validate(server, ci_repo, tmp_path, *, adapters, env=None, org=False, say=None, pr=7):
    """A validation the way the workflow runs it: `ci start --validate` in
    one job, then `ci run` with the plan and run token it wrote."""
    env = run_env() if env is None else env
    said = [] if say is None else say
    providers = ci_adapters.providers_available(adapters, env, org)
    out = tmp_path / "out"
    code = ci_client.start(REPO, {"kind": "validate", "pr": pr, "head_sha": head(ci_repo), "fork": False}, out,
                           client=server.client, token=CI_TOKEN, providers=providers, say=said.append)
    assert code == 0
    if not (out / "plan.jwt").exists():
        return None
    plan_token = (out / "plan.jwt").read_text()
    return ci_client.run(plan_token, out / "run_token", cwd=str(ci_repo), env=env, client=server.client,
                         adapters=adapters, org=org, texts=lambda: ci_client.read_plan_texts(out / "plan.jwt"),
                         say=said.append)


def test_plan_texts_are_saved_beside_the_plan_and_rechecked(plan, ci_repo, tmp_path):
    text = "Review this pull request.\n" + "+ a diff line\n" * 2000
    h = sha256(text)
    vplan = plan("validation", head_sha=head(ci_repo),
                 reviewers=[{"id": "r1", "provider": "claude", "instructions_sha256": h}])
    server = Server(plan(), validation_plan=vplan, plan_texts={h: text})
    adapter = FakeAdapter("claude")
    assert validate(server, ci_repo, tmp_path, adapters={"claude": adapter})["status"] == "posted"
    assert adapter.reviews[0][0] == text
    out = tmp_path / "out"
    assert json.loads((out / "plan_texts.json").read_text()) == {h: text}
    assert oct((out / "plan_texts.json").stat().st_mode)[-3:] == "600"

    trigger = {"kind": "validate", "pr": 7, "head_sha": head(ci_repo), "fork": False}
    assert ci_client.start(REPO, trigger, out, client=server.client, token=CI_TOKEN, providers=["claude"],
                           say=lambda s: None) == 0
    (out / "plan_texts.json").write_text(json.dumps({h: text + "tampered"}))
    with pytest.raises(CIError, match="doesn't match its hash"):
        ci_client.run((out / "plan.jwt").read_text(), out / "run_token", cwd=str(ci_repo), env=run_env(),
                      client=server.client, adapters={"claude": adapter},
                      texts=ci_client.read_plan_texts(out / "plan.jwt"), say=lambda s: None)
    assert not (out / "run_token").exists(), "the run token never stays on disk"

    server.plan_texts = None    # a plan that fits inline: no texts, and none left over
    assert ci_client.start(REPO, trigger, out, client=server.client, token=CI_TOKEN, providers=["claude"],
                           say=lambda s: None) == 0
    assert not (out / "plan_texts.json").exists() and ci_client.read_plan_texts(out / "plan.jwt") is None

    server.plan_texts = {h: 5}
    with pytest.raises(CIError, match="malformed") as e:
        ci_client.start(REPO, trigger, tmp_path / "other", client=server.client, token=CI_TOKEN,
                        providers=["claude"], say=lambda s: None)
    assert e.value.code == "bad_response" and not (tmp_path / "other" / "plan.jwt").exists()


def test_validate_runs_checks_scrubbed_and_asks_reviewers(plan, ci_repo, tmp_path, monkeypatch):
    sha = head(ci_repo)
    vplan = plan("validation", head_sha=sha,
                 checks=[{"id": "c1", "command": "echo ok; printenv ANTHROPIC_API_KEY GH_TOKEN; exit 0", "timeout_s": 30},
                         {"id": "c2", "command": "echo nope >&2; exit 3", "timeout_s": 30}])
    server = Server(plan(), validation_plan=vplan)
    adapter = FakeAdapter("claude", reply="Looks fine.")
    env = run_env(ANTHROPIC_API_KEY="sk-secret-value")
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", CI_TOKEN)
    said = []
    result = validate(server, ci_repo, tmp_path, adapters={"claude": adapter}, env=env, say=said)
    assert result["status"] == "posted" and "success" in said[-1]
    assert "validation val_1 started" in said[0]
    start = server.transport.calls[0][1]
    assert start == {"repo": REPO, "pr": 7, "head_sha": sha, "fork": False, "providers_available": ["claude"]}
    assert not (tmp_path / "out" / "run_token").exists()
    assert "BRINDLE_PRO_TOKEN" not in env and "GH_TOKEN" not in env
    assert "BRINDLE_PRO_TOKEN" not in os.environ
    assert env["ANTHROPIC_API_KEY"] == "sk-secret-value", "model keys stay for the reviewers"
    ev = server.evidence_calls[0]
    c1, c2 = ev["checks"]
    assert c1["id"] == "c1" and c1["exit"] == 0 and "sk-secret-value" not in c1["output_excerpt"] \
        and "ghp_secret" not in c1["output_excerpt"] and "ok" in c1["output_excerpt"]
    assert c2["exit"] == 3 and "nope" in c2["output_excerpt"] and isinstance(c2["duration_s"], float)
    assert ev["reviews"] == [{"id": "r1", "provider": "claude", "model": "fake-model", "reply": "Looks fine."}]
    assert ev["usage"] == {"fake-model": {"input": 3, "output": 2, "cache_read": 1}}
    assert adapter.reviews[0][0] == "review it"
    assert "sk-secret-value" not in json.dumps(server.transport.calls, default=str)


def test_validate_fills_the_check_results_slot(plan, ci_repo, tmp_path):
    sha = head(ci_repo)
    vplan = plan("validation", head_sha=sha,
                 checks=[{"id": "c1", "command": "echo all good", "timeout_s": 30},
                         {"id": "c2", "command": "echo broken >&2; exit 2", "timeout_s": 30}],
                 reviewers=[{"id": "r1", "provider": "claude",
                             "instructions": "Review.\n\n{{brindle.check_results}}\n\nBe brief. {{other.slot}}"},
                            {"id": "r2", "provider": "claude", "instructions": "No slot here"}])
    server = Server(plan(), validation_plan=vplan)
    adapter = FakeAdapter("claude")
    validate(server, ci_repo, tmp_path, adapters={"claude": adapter})
    filled = adapter.reviews[0][0]
    assert filled.startswith("Review.\n\ncheck c1: exit 0 (") and "all good" in filled
    assert "check c2: exit 2 (" in filled and "broken" in filled
    assert "{{brindle.check_results}}" not in filled and filled.endswith("Be brief. {{other.slot}}")
    assert adapter.reviews[1][0] == "No slot here"
    assert ci_client.render_check_results([]) == "(no checks were run)"


def test_validate_handles_more_with_the_complete_evidence(plan, ci_repo, tmp_path):
    sha = head(ci_repo)
    first = plan("validation", head_sha=sha)
    second = plan("validation", head_sha=sha, jti="p2",
                  reviewers=[{"id": "r1", "provider": "claude", "instructions": "review it"},
                             {"id": "r2", "provider": "codex", "instructions": "second opinion"}])
    server = Server(plan(), validation_plan=first,
                    evidence=[{"status": "more", "plan": second},
                              {"status": "posted", "conclusion": "failure", "check_url": "https://gh.test/c"}])
    claude, codex = FakeAdapter("claude", reply="A"), FakeAdapter("codex", reply="B")
    result = validate(server, ci_repo, tmp_path, adapters={"claude": claude, "codex": codex})
    assert result["conclusion"] == "failure"
    assert len(server.evidence_calls) == 2
    first_call, second_call = server.evidence_calls
    assert [r["id"] for r in first_call["reviews"]] == ["r1"]
    assert [r["id"] for r in second_call["reviews"]] == ["r1", "r2"], "the follow-up carries every review"
    assert second_call["checks"] == first_call["checks"], "and every check"
    assert len(claude.reviews) == 1 and len(codex.reviews) == 1, "each reviewer is asked once"
    assert second_call["usage"]["fake-model"]["input"] == 6


def test_validate_records_an_unusable_reviewer(plan, ci_repo, tmp_path):
    sha = head(ci_repo)
    server = Server(plan(), validation_plan=plan("validation", head_sha=sha))
    sub = FakeAdapter("claude", kind="subscription")
    validate(server, ci_repo, tmp_path, adapters={"claude": sub}, org=True)
    assert server.transport.calls[0][1]["providers_available"] == []
    row = server.evidence_calls[0]["reviews"][0]
    assert row == {"id": "r1", "provider": "claude", "model": None, "reply": ""} and not sub.reviews
    assert server.evidence_calls[0]["checks"], "the checks are posted whatever the reviewers did"


def test_validate_sends_an_empty_reply_for_a_failed_reviewer_cli(plan, ci_repo, tmp_path):
    sha = head(ci_repo)
    server = Server(plan(), validation_plan=plan("validation", head_sha=sha))
    failing = FakeAdapter("claude", reply="partial words")
    failing.review = lambda *a, **k: ci_adapters.Review("partial words", model="fake-model", exit=1)
    validate(server, ci_repo, tmp_path, adapters={"claude": failing})
    row = server.evidence_calls[0]["reviews"][0]
    assert row == {"id": "r1", "provider": "claude", "model": "fake-model", "reply": ""}
    assert set(row) == {"id", "provider", "model", "reply"}


@pytest.mark.parametrize("why", ["fork", "stale_head"])
def test_start_validate_skipped(plan, ci_repo, tmp_path, why):
    t = FakeTransport({"POST /ci/validations": [(200, {"skipped": why})]})
    said = []
    code = ci_client.start(REPO, {"kind": "validate", "pr": 7, "head_sha": head(ci_repo), "fork": why == "fork"},
                           tmp_path / "out", client=ci_client.Client(BASE, t), token=CI_TOKEN, providers=[],
                           say=said.append)
    assert code == 0 and f"skipped: {why}" in said[0]
    assert not (tmp_path / "out").exists(), "nothing for the run job"
    assert t.calls[0][1]["fork"] is (why == "fork")


def test_pr_is_fork_from_the_event_payload(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"repo": {"full_name": "someone/widgets", "fork": True}}}}))
    assert ci_client.pr_is_fork({"GITHUB_EVENT_PATH": str(event)}, REPO) is True
    event.write_text(json.dumps({"pull_request": {"head": {"repo": {"full_name": REPO, "fork": False}}}}))
    assert ci_client.pr_is_fork({"GITHUB_EVENT_PATH": str(event)}, REPO) is False
    assert ci_client.pr_is_fork({}, REPO) is False


def pr_checkout(repo, tmp_path, pr=7) -> tuple:
    """A PR (head pushed as refs/pull/N/head, GitHub's merge commit as
    refs/pull/N/merge) and a shallow clone that checked out the merge commit,
    the way an older workflow does. Returns (clone, head sha, merge sha)."""
    origin = tmp_path / "origin.git"
    sh("git checkout -q -b feature", repo)
    (repo / "feature.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm feature", repo)
    pr_head = head(repo)
    sh("git checkout -q main", repo)
    (repo / "other.py").write_text("y = 2\n")
    sh("git add -A && git commit -qm other && git push -q origin main", repo)
    sh("git merge -q --no-ff --no-edit feature", repo)
    merge = head(repo)
    sh(f"git push -q origin {pr_head}:refs/pull/{pr}/head {merge}:refs/pull/{pr}/merge", repo)
    clone = tmp_path / "runner"
    sh(f"git clone -q --depth 1 --no-local file://{origin} {clone}", tmp_path)
    sh(f"git fetch -q --depth 1 origin refs/pull/{pr}/merge && git checkout -q --detach FETCH_HEAD", clone)
    return clone, pr_head, merge


def test_checkout_switches_from_the_merge_commit_to_the_plans_head(repo, tmp_path):
    clone, pr_head, merge = pr_checkout(repo, tmp_path)
    assert head(clone) == merge
    said = []
    ci_client.check_validation_checkout({"head_sha": pr_head, "pr": 7}, str(clone), say=said.append)
    assert head(clone) == pr_head
    assert said == [f"checked out the plan's head {pr_head[:12]} (the workflow had {merge[:12]})"]
    said.clear()
    ci_client.check_validation_checkout({"head_sha": pr_head, "pr": 7}, str(clone), say=said.append)
    assert head(clone) == pr_head and said == [], "already at the head: nothing to do"


def test_checkout_switches_a_run_to_the_plans_base(repo, tmp_path):
    clone, _, merge = pr_checkout(repo, tmp_path)
    base = sh("git rev-parse origin/main", repo)
    said = []
    ci_client.check_run_checkout({"base_sha": base}, str(clone), say=said.append)
    assert head(clone) == base and said == [f"checked out the plan's base {base[:12]} (the workflow had {merge[:12]})"]


def test_checkout_refuses_an_unreachable_head(repo, tmp_path):
    clone, _, merge = pr_checkout(repo, tmp_path)
    with pytest.raises(CIError, match=r"not the plan's head 222222222222, and it can't fetch it from origin \("):
        ci_client.check_validation_checkout({"head_sha": "2" * 40, "pr": 7}, str(clone), say=lambda s: None)
    assert head(clone) == merge


def test_checkout_never_switches_a_dirty_worktree(repo, tmp_path):
    clone, pr_head, merge = pr_checkout(repo, tmp_path)
    (clone / "app.py").write_text("changed\n")
    with pytest.raises(CIError, match=r"not the plan's head .*uncommitted changes \(app.py\), so it isn't switched"):
        ci_client.check_validation_checkout({"head_sha": pr_head, "pr": 7}, str(clone), say=lambda s: None)
    assert head(clone) == merge and (clone / "app.py").read_text() == "changed\n"


def test_validate_rejects_the_wrong_head(plan, ci_repo, tmp_path):
    server = Server(plan(), validation_plan=plan("validation", head_sha="2" * 40))
    with pytest.raises(CIError, match="not the plan's head"):
        validate(server, ci_repo, tmp_path, adapters={})
    assert not (tmp_path / "out" / "run_token").exists()


def test_run_check_times_out(ci_repo):
    row = ci_client.run_check({"id": "slow", "command": "sleep 5", "timeout_s": 1}, str(ci_repo), {"PATH": os.environ["PATH"]})
    assert row["exit"] == 124 and "timed out after 1s" in row["output_excerpt"]


def test_run_check_timeout_kills_the_whole_process_group(ci_repo):
    """A grandchild that keeps the output pipe open must not outlive the timeout."""
    t0 = time.monotonic()
    row = ci_client.run_check({"id": "fork", "command": "echo started; sh -c 'sleep 30' & sleep 30", "timeout_s": 1},
                              str(ci_repo), {"PATH": os.environ["PATH"]})
    assert row["exit"] == 124 and "started" in row["output_excerpt"]
    assert time.monotonic() - t0 < 10


def test_run_check_missing_command(ci_repo):
    row = ci_client.run_check({"id": "x", "command": "definitely-not-a-command-xyz", "timeout_s": 5},
                              str(ci_repo), {"PATH": "/nonexistent"})
    assert row["exit"] == 127


# -- air-gap, doctor, the CLI ------------------------------------------------------------------------


def test_airgap_refuses(plan, monkeypatch):
    monkeypatch.setenv("BRINDLE_AIRGAP", "1")
    with pytest.raises(CIError, match="air-gap mode") as e:
        ci_client.refuse_airgap()
    assert e.value.code == "airgap"
    server = Server(plan())
    with pytest.raises(CIError) as e:
        server.client.events(RUN_TOKEN, "run_1", {"state": "working"})
    assert e.value.code == "airgap" and not server.events


def test_doctor_names_credentials_but_never_values(ci_repo):
    env = {"ANTHROPIC_API_KEY": "sk-ant-very-secret", "OPENAI_API_KEY": "sk-oa-very-secret", "PATH": "/nonexistent"}
    text = ci_client.doctor(env, REPO, str(ci_repo), org=True)
    assert "ANTHROPIC_API_KEY" in text and "OPENAI_API_KEY" in text
    assert "very-secret" not in text
    assert "owner is an organization" in text and "claude: cli claude missing" in text


def test_cli_doctor_and_help(ci_repo, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret")
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(ci_adapters, "repo_is_org", lambda env, repo=None, run=None: False)
    result = CliRunner().invoke(app, ["ci", "doctor", "--repo", REPO])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" in result.output and "very-secret" not in result.output
    assert "personal account" in result.output
    for cmd in ("start", "run", "report", "doctor", "init"):
        assert CliRunner().invoke(app, ["ci", cmd, "--help"]).exit_code == 0


def test_cli_run_fails_cleanly_on_a_bad_plan(ci_repo, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    (tmp_path / "plan.jwt").write_text("not.a.plan")
    tf = token_file(tmp_path)
    result = CliRunner().invoke(app, ["ci", "run", "--plan", str(tmp_path / "plan.jwt"), "--run-token-file", str(tf)])
    assert result.exit_code == 1 and "brindle ci: malformed plan" in result.output
    assert not tf.exists(), "the run token never stays on disk"


@pytest.mark.parametrize("make, error", [
    (lambda p: p.mkdir(), "can't read the plan texts"),
    (lambda p: p.write_text("{not json"), "plan texts are malformed"),
    (lambda p: p.write_bytes(b'{"\xff": 1}'), "plan texts are malformed"),
])
def test_cli_run_reads_plan_texts_after_the_run_token(plan, ci_repo, tmp_path, monkeypatch, make, error):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    (tmp_path / "plan.jwt").write_text(plan())
    make(tmp_path / "plan_texts.json")
    tf = token_file(tmp_path)
    result = CliRunner().invoke(app, ["ci", "run", "--plan", str(tmp_path / "plan.jwt"), "--run-token-file", str(tf)])
    assert result.exit_code == 1 and error in result.output
    assert not tf.exists(), "the run token never stays on disk"


def test_cli_start_needs_the_token(ci_repo, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.delenv("BRINDLE_PRO_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)   # on GitHub Actions it names the real repo
    result = CliRunner().invoke(app, ["ci", "start", "--repo", REPO, "--issue", "1", "--out", str(tmp_path)])
    assert result.exit_code == 1 and "BRINDLE_PRO_TOKEN isn't set" in result.output


# -- init ---------------------------------------------------------------------------------------------


@pytest.fixture
def ci_entitled(monkeypatch):
    """The person's verified entitlement includes ``ci`` (org_1); returns the
    keyword arguments each ``license.current`` call got."""
    from brindle.pro import license

    seen = []

    def current(**kw):
        seen.append(kw)
        return license.Entitlement(sub="user_1", org_id="org_1", plan="team", status="active",
                                   features=frozenset({"ci"}), seats=5, iat=0, exp=0, kid=TEST_KID)
    monkeypatch.setattr(license, "current", current)
    return seen


class GhProc:
    def __init__(self, out="", code=0, err=""):
        self.stdout, self.stderr, self.returncode = out, err, code


NOT_FOUND = "gh: Not Found (HTTP 404)"
PLAN_REFUSAL = "gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)"


def gh_repo_api(argv, *, required=(), protected=False, refuse=None, branch="main"):
    """A fake GitHub's answer to the repository API calls init makes (None
    for any other call). ``required``: the default branch's required checks;
    ``protected``: whether it has protection; ``refuse``: the error a
    protection change gets."""
    if argv[:2] != ["gh", "api"]:
        return None
    path, rest = argv[2], argv[3:]
    if path == f"repos/{REPO}":
        return GhProc("true\n" if rest == ["--jq", ".permissions.admin"] else branch + "\n")
    if not path.startswith(f"repos/{REPO}/branches/{branch}/protection"):
        return None
    if "-X" in rest:
        return GhProc(code=1, err=refuse) if refuse else GhProc("{}")
    if path.endswith("/required_status_checks"):
        return GhProc(json.dumps({"strict": False, "contexts": list(required)})) if required \
            else GhProc(code=1, err=NOT_FOUND)
    return GhProc("{}") if protected else GhProc(code=1, err=NOT_FOUND)


def test_init_never_shows_the_token(plan, ci_repo, tmp_path, monkeypatch, ci_entitled):
    calls = []
    Proc = GhProc

    def run(argv, **kw):
        calls.append((argv, kw.get("input")))
        if argv[:2] == ["gh", "repo"]:
            return Proc(REPO + "\n")
        if got := gh_repo_api(argv):
            return got
        if argv[:2] == ["gh", "api"]:
            return Proc("Organization\n")
        if argv[:3] == ["gh", "pr", "create"]:
            return Proc("https://gh.test/pr/1\n")
        return Proc()

    class Plugin:
        store = object()

        def _team_org(self, org):
            return org or "org_1"

        def _client(self, base):
            return object()

    from brindle.pro import auth

    monkeypatch.setattr(auth, "create_ci_token", lambda client, store, org, name: {
        "token": CI_TOKEN, "token_id": "ct_1", "org_id": org, "name": name})
    t = FakeTransport({"GET /ci/workflow?kind=issue": [(200, {"text": "name: issue\n"})],
                       "GET /ci/workflow?kind=validate": [(200, {"text": "name: validate\n"})],
                       "GET /ci/workflow?kind=fix": [(404, {"error": "not_found"})]})
    opened, said = [], []
    pushed = []
    from brindle import git as git_mod

    real_run = git_mod.run

    def git_run(args, cwd, check=True):
        if args[0] == "push":
            pushed.append({rel: sh(f"git show HEAD:{rel}", cwd) for rel in (
                ".github/workflows/brindle-ci-issue.yml", ".github/workflows/brindle-ci-validate.yml")})
            pushed.append(sh("git rev-parse --abbrev-ref HEAD", cwd))
            assert sh("git ls-tree --name-only HEAD .github/workflows/", cwd).split() == [
                ".github/workflows/brindle-ci-issue.yml", ".github/workflows/brindle-ci-validate.yml"]
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, cwd, check)
    monkeypatch.setattr(git_mod, "run", git_run)
    sh(f"git remote set-url origin git@github.com:{REPO}.git", ci_repo)
    ci_client.init(repo=None, org=None, providers=["claude"], cwd=str(ci_repo), env={"PATH": os.environ["PATH"]},
                   run=run, open_url=opened.append, account=Plugin(), client=ci_client.Client(BASE, t),
                   say=said.append)
    text = "\n".join(said)
    assert CI_TOKEN not in text and "ct_1" in text
    assert opened == [BASE + "/github/install?org_id=org_1"]
    secret_calls = [c for c in calls if c[0][:3] == ["gh", "secret", "set"]]
    assert secret_calls[0][0][3] == "BRINDLE_PRO_TOKEN" and secret_calls[0][1] == CI_TOKEN
    assert secret_calls[1][0][3] == "ANTHROPIC_API_KEY" and secret_calls[1][1] is None, "the person pastes it into gh"
    assert pushed == [{".github/workflows/brindle-ci-issue.yml": "name: issue",
                       ".github/workflows/brindle-ci-validate.yml": "name: validate"}, "brindle/ci-setup"]
    # Back where the person started, the setup branch gone: the next pull after the squash merge is clean.
    assert sh("git rev-parse --abbrev-ref HEAD", ci_repo) == "main"
    assert sh("git branch --list brindle/ci-setup", ci_repo) == ""
    assert not (ci_repo / ".github/workflows").exists() and sh("git status --porcelain", ci_repo) == ""
    assert ["gh", "label", "create", "brindle", "--repo", REPO, "--color", "2E7D32",
            "--description", "brindle CI picks this up", "--force"] in [c[0] for c in calls]
    assert "https://gh.test/pr/1" in text and "owner is an organization" in text
    assert "back on main" in text and "8/8 doctor" in text


@pytest.fixture
def init_run(ci_repo, monkeypatch, ci_entitled):
    """Runs ``ci_client.init`` against a fake gh, server and Pro account;
    returns (gh calls, said lines). ``gh={...}`` sets :func:`gh_repo_api`'s
    answers; ``go.inputs`` holds what each gh call got on stdin."""
    from brindle import git as git_mod
    from brindle.pro import auth

    Proc = GhProc
    calls, said, inputs, github = [], [], [], {}

    def run(argv, **kw):
        calls.append(argv)
        inputs.append(kw.get("input"))
        if got := gh_repo_api(argv, **github):
            return got
        if argv[:2] == ["gh", "api"]:
            return Proc("Organization\n")
        if argv[:3] == ["gh", "pr", "create"]:
            return Proc("https://gh.test/pr/1\n")
        return Proc()

    class Plugin:
        store = object()

        def _team_org(self, org):
            return "org_1"

        def _client(self, base):
            return object()

    monkeypatch.setattr(auth, "create_ci_token", lambda client, store, org, name: {
        "token": CI_TOKEN, "token_id": "ct_1", "org_id": org, "name": name})
    real_run = git_mod.run
    monkeypatch.setattr(git_mod, "run", lambda args, cwd, check=True: (
        subprocess.CompletedProcess(args, 0, "", "") if args[0] == "push" else real_run(args, cwd, check)))
    sh(f"git remote set-url origin https://github.com/{REPO}.git", ci_repo)

    def go(gh=None, **kw):
        github.update(gh or {})
        t = FakeTransport({"GET /ci/workflow?kind=issue": [(200, {"text": "name: issue\n"})],
                           "GET /ci/workflow?kind=validate": [(404, {"error": "not_found"})],
                           "GET /ci/workflow?kind=fix": [(404, {"error": "not_found"})]})
        kw.setdefault("env", {"PATH": os.environ["PATH"]})
        ci_client.init(repo=REPO, org=None, providers=["claude"], cwd=str(ci_repo), run=run,
                       open_url=lambda url: None, account=Plugin(), client=ci_client.Client(BASE, t),
                       say=said.append, **kw)
        return calls, said
    go.calls, go.inputs, go.said = calls, inputs, said
    return go


def test_init_federation_sets_the_variables(init_run):
    answers = {"Claude: API key or identity federation? (key, federation)": "federation",
               "federation rule id (fdrl_...)": "fdrl_1", "Anthropic organization id (uuid)": ORG_UUID,
               "service account id (svac_...)": "svac_1", "workspace id (wrkspc_..., optional)": ""}
    asked = []

    def ask(q, default):
        asked.append(q)
        return answers.get(q, default)
    calls, said = init_run(ask=ask)
    assert asked[0] == "Claude: API key or identity federation? (key, federation)"
    variables = [c[3:] for c in calls if c[:3] == ["gh", "variable", "set"]]
    assert variables == [["ANTHROPIC_FEDERATION_RULE_ID", "--repo", REPO, "--body", "fdrl_1"],
                         ["ANTHROPIC_ORGANIZATION_ID", "--repo", REPO, "--body", ORG_UUID],
                         ["ANTHROPIC_SERVICE_ACCOUNT_ID", "--repo", REPO, "--body", "svac_1"]]
    secrets = [c[3] for c in calls if c[:3] == ["gh", "secret", "set"]]
    assert secrets == ["BRINDLE_PRO_TOKEN"], "no ANTHROPIC_API_KEY secret with federation"
    text = "\n".join(said)
    assert f"subject prefix repo:{REPO}:*" in text and "https://api.anthropic.com" in text and "600 s" in text
    assert (f'condition claims.repository == "{REPO}" && '
            f'claims.workflow_ref.startsWith("{REPO}/.github/workflows/brindle-ci-")') in text
    assert "forks never get a token" in text


def test_init_federation_non_interactive(init_run):
    """--credential federation with the IDs in the environment asks nothing."""
    env = {"PATH": os.environ["PATH"], "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1",
           "ANTHROPIC_ORGANIZATION_ID": ORG_UUID, "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_1",
           "ANTHROPIC_WORKSPACE_ID": "wrkspc_1"}
    calls, _ = init_run(credential="federation", env=env)
    variables = {c[3]: c[-1] for c in calls if c[:3] == ["gh", "variable", "set"]}
    assert variables == {"ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1", "ANTHROPIC_ORGANIZATION_ID": ORG_UUID,
                         "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_1", "ANTHROPIC_WORKSPACE_ID": "wrkspc_1"}


def test_init_federation_needs_the_ids(init_run):
    """A missing ID stops init before step 3 creates the CI token, so none is left orphaned."""
    with pytest.raises(CIError, match=r"ANTHROPIC_FEDERATION_RULE_ID is required for identity federation "
                                      r"\(pass --rule-id or set \$ANTHROPIC_FEDERATION_RULE_ID\)"):
        init_run(credential="federation")
    assert not [c for c in init_run.calls if c[:2] in (["gh", "secret"], ["gh", "variable"])]
    assert not [s for s in init_run.said if s.startswith("3/8")]


def test_init_bad_federation_workspace_stops_before_the_token(init_run):
    env = {"PATH": os.environ["PATH"], "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1",
           "ANTHROPIC_ORGANIZATION_ID": ORG_UUID, "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_1",
           "ANTHROPIC_WORKSPACE_ID": "nope"}
    with pytest.raises(CIError, match="workspace ID looks like wrkspc_"):
        init_run(credential="federation", env=env)
    assert not [c for c in init_run.calls if c[:2] == ["gh", "secret"]]


def test_init_federation_from_options_asks_nothing(init_run):
    """--rule-id, --organization-id, --service-account-id (and --workspace-id)
    give the IDs, and imply federation when --credential isn't given."""
    def never(q, d):
        if "required" in q:
            return d
        pytest.fail(f"asked {q!r}")
    calls, _ = init_run(ask=never, rule_id="fdrl_2", organization_id=ORG_UUID, service_account_id="svac_2",
                        workspace_id="wrkspc_2",
                        env={"PATH": os.environ["PATH"], "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_env"})
    assert {c[3]: c[-1] for c in calls if c[:3] == ["gh", "variable", "set"]} == {
        "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_2", "ANTHROPIC_ORGANIZATION_ID": ORG_UUID,
        "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_2", "ANTHROPIC_WORKSPACE_ID": "wrkspc_2"}
    assert [c[3] for c in calls if c[:3] == ["gh", "secret", "set"]] == ["BRINDLE_PRO_TOKEN"]


def test_cli_init_without_a_terminal_never_prompts(ci_repo, monkeypatch):
    """Unattended (stdin not a terminal), a question takes its default
    instead of aborting, except that a [Y/n] one, which changes the
    repository's settings, is no; the federation options reach init."""
    from typer.testing import CliRunner

    from brindle.cli import app

    got = {}

    def init(**kw):
        got.update(kw)
        got["answers"] = [kw["ask"]("federation rule id (fdrl_...)", "fdrl_env"),
                          kw["ask"]("Mark 'Tests' as required so brindle can fix it when it fails? [Y/n]", "y")]
    monkeypatch.setattr(ci_client, "init", init)
    result = CliRunner().invoke(app, ["ci", "init", "--credential", "federation", "--rule-id", "fdrl_1",
                                      "--organization-id", ORG_UUID, "--service-account-id", "svac_1",
                                      "--workspace-id", "wrkspc_1"], input="")
    assert result.exit_code == 0, result.output
    assert "Aborted" not in result.output and got["answers"] == ["fdrl_env", "n"]
    assert (got["credential"], got["rule_id"], got["organization_id"], got["service_account_id"],
            got["workspace_id"]) == ("federation", "fdrl_1", ORG_UUID, "svac_1", "wrkspc_1")


def test_init_unattended_requires_no_check_without_the_option(init_run, ci_repo):
    """With the CLI's unattended answers, the branch is only protected when
    --required-check names the check."""
    commit_workflow(ci_repo)
    unattended = lambda q, d: "n" if q.endswith("[Y/n]") else d  # noqa: E731
    _, said = init_run(credential="key", ask=unattended)
    assert not protection_writes(init_run) and "Require status checks to pass" in "\n".join(said)
    init_run(credential="key", ask=unattended, required_check="Tests")
    assert [w[2]["required_status_checks"]["contexts"] for w in protection_writes(init_run)] == [["Tests"]]


@pytest.mark.parametrize("kw, error", [
    ({"rule_id": "rule-1"}, "ANTHROPIC_FEDERATION_RULE_ID looks like fdrl_..., not 'rule-1'"),
    ({"organization_id": "org-1"}, "ANTHROPIC_ORGANIZATION_ID looks like a UUID, not 'org-1'"),
    ({"service_account_id": "sa_1"}, "ANTHROPIC_SERVICE_ACCOUNT_ID looks like svac_..., not 'sa_1'"),
    ({"credential": "key", "rule_id": "fdrl_1", "service_account_id": "svac_1"},
     "--rule-id, --service-account-id configure identity federation, not --credential key"),
])
def test_init_rejects_bad_federation_options_first(init_run, kw, error):
    with pytest.raises(CIError, match=re.escape(error)):
        init_run(**kw)
    assert not init_run.calls, "nothing ran before the bad option was caught"


def test_init_checks_federation_ids_from_the_environment(init_run):
    env = {"PATH": os.environ["PATH"], "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1",
           "ANTHROPIC_ORGANIZATION_ID": "not-a-uuid", "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_1"}
    with pytest.raises(CIError, match="ANTHROPIC_ORGANIZATION_ID looks like a UUID"):
        init_run(credential="federation", env=env)
    assert not [c for c in init_run.calls if c[:2] == ["gh", "secret"]]


def test_init_key_credential_sets_the_secret(init_run):
    calls, _ = init_run(credential="key")
    assert [c[3] for c in calls if c[:3] == ["gh", "secret", "set"]] == ["BRINDLE_PRO_TOKEN", "ANTHROPIC_API_KEY"]
    assert not [c for c in calls if c[:2] == ["gh", "variable"]]


def test_init_key_asks_for_an_org_keys_workspace_after_the_key(init_run):
    asked = []

    def ask(q, default):
        asked.append(q)
        return "wrkspc_01AbC-d" if q.startswith("workspace ID") else default
    calls, said = init_run(credential="key", ask=ask)
    assert asked[-1] == ci_client.WORKSPACE_QUESTION
    gh = [c for c in calls if c[:3] in (["gh", "secret", "set"], ["gh", "variable", "set"])]
    assert gh[-2][3] == "ANTHROPIC_API_KEY", "asked after the key"
    assert gh[-1][1:] == ["variable", "set", "ANTHROPIC_WORKSPACE_ID", "--repo", REPO, "--body", "wrkspc_01AbC-d"]
    assert any("anthropic-workspace-id" in s for s in said)


@pytest.mark.parametrize("kw", [
    {"workspace_id": "wrkspc_1",
     "ask": lambda q, d: pytest.fail(f"asked {q!r}") if q.startswith("workspace") else d},
    {"env": {"PATH": os.environ["PATH"], "ANTHROPIC_WORKSPACE_ID": "wrkspc_1"}},
], ids=["option", "environment"])
def test_init_key_workspace_from_the_option_or_the_environment(init_run, kw):
    calls, _ = init_run(credential="key", **kw)
    assert [c[-1] for c in calls if c[:3] == ["gh", "variable", "set"]] == ["wrkspc_1"]


def test_init_rejects_a_bad_workspace_id(init_run):
    with pytest.raises(CIError, match="workspace ID looks like wrkspc_"):
        init_run(credential="key", workspace_id="ws 1; rm")
    assert not init_run.calls, "nothing ran before the bad option was caught"
    with pytest.raises(CIError, match="workspace ID looks like wrkspc_"):
        init_run(credential="key", ask=lambda q, d: "1234" if q.startswith("workspace") else d)
    assert not [c for c in init_run.calls if c[:2] == ["gh", "variable"]]


def test_init_rejects_an_unknown_credential_first(init_run):
    with pytest.raises(CIError, match="credential must be key or federation"):
        init_run(credential="password")
    assert not init_run.calls, "nothing ran before the bad option was caught"


def test_init_refuses_uncommitted_work_before_doing_anything(init_run, ci_repo):
    (ci_repo / "app.py").write_text("print('mine')\n")
    with pytest.raises(CIError, match=r"commit or stash your changes first \(1 uncommitted, e.g. app.py\)"):
        init_run(credential="key")
    assert not init_run.calls
    assert (ci_repo / "app.py").read_text() == "print('mine')\n"
    assert sh("git rev-parse --abbrev-ref HEAD", ci_repo) == "main"


def test_init_refuses_to_start_on_the_setup_branch(init_run, ci_repo):
    sh("git checkout -q -b brindle/ci-setup", ci_repo)
    with pytest.raises(CIError, match="switch off brindle/ci-setup first"):
        init_run(credential="key")
    assert not init_run.calls


def test_init_refuses_a_symlink_where_it_writes(init_run, ci_repo, tmp_path):
    (ci_repo / ".github").mkdir()
    (ci_repo / ".github/workflows").symlink_to(tmp_path)
    sh("git add -A && git commit -qm link", ci_repo)   # tracked, but still a symlink
    with pytest.raises(CIError, match=".github/workflows is a symlink"):
        init_run(credential="key")
    assert not init_run.calls and not list(tmp_path.glob("brindle-ci-*"))


@pytest.mark.parametrize("kind, error", [("file", "move .github/workflows/brindle-ci-issue.yml away first"),
                                         ("dangling symlink", "brindle-ci-issue.yml is a symlink")])
def test_push_setup_branch_checks_the_paths_again(ci_repo, kind, error):
    """A file that appeared after init's first check (even a dangling
    symlink, which exists() misses) still stops the write."""
    (ci_repo / ".github/workflows").mkdir(parents=True)
    target = ci_repo / ".github/workflows/brindle-ci-issue.yml"
    if kind == "file":
        target.write_text("mine\n")
    else:
        target.symlink_to(ci_repo / "nowhere")
    with pytest.raises(CIError, match=error):
        ci_client.push_setup_branch(str(ci_repo), {".github/workflows/brindle-ci-issue.yml": "name: issue\n"})
    assert sh("git rev-parse --abbrev-ref HEAD", ci_repo) == "main"
    assert sh("git branch --list brindle/ci-setup", ci_repo) == "" and not (ci_repo / "nowhere").exists()


def test_init_refuses_an_untracked_file_where_it_writes(init_run, ci_repo):
    (ci_repo / ".github/workflows").mkdir(parents=True)
    (ci_repo / ".github/workflows/brindle-ci-fix.yml").write_text("mine\n")
    with pytest.raises(CIError, match="move .github/workflows/brindle-ci-fix.yml away first"):
        init_run(credential="key")
    assert not init_run.calls
    assert (ci_repo / ".github/workflows/brindle-ci-fix.yml").read_text() == "mine\n"


@pytest.mark.parametrize("step", ["add", "commit", "push"])
def test_init_goes_back_to_the_start_branch_when_a_step_fails(init_run, ci_repo, monkeypatch, step):
    from brindle import git as git_mod

    sh("git checkout -q -b work", ci_repo)
    patched = git_mod.run

    def git_run(args, cwd, check=True):
        if args[0] == step:
            raise git_mod.GitError(f"git {step}: boom")
        return patched(args, cwd, check)
    monkeypatch.setattr(git_mod, "run", git_run)
    with pytest.raises(git_mod.GitError, match="boom"):
        init_run(credential="key")
    assert sh("git rev-parse --abbrev-ref HEAD", ci_repo) == "work"
    assert sh("git branch --list brindle/ci-setup", ci_repo) == ""
    assert sh("git status --porcelain", ci_repo) == "" and not (ci_repo / ".github").exists()


def test_init_goes_back_to_a_detached_head(init_run, ci_repo):
    start = head(ci_repo)
    sh("git checkout -q --detach", ci_repo)
    _, said = init_run(credential="key")
    assert head(ci_repo) == start and sh("git rev-parse --abbrev-ref HEAD", ci_repo) == "HEAD"
    assert "back on the commit you started on" in "\n".join(said)


CI_WORKFLOW = """\
name: CI
on: [push, pull_request]
jobs:
  test:
    name: "Tests"   # the check name
    runs-on: ubuntu-latest
    steps:
      - name: not a job name
        run: make test
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: make lint
  matrix:
    name: build ${{ matrix.os }}
    runs-on: ${{ matrix.os }}
"""


def commit_workflow(repo, name="ci.yml", text=CI_WORKFLOW):
    (repo / ".github/workflows").mkdir(parents=True, exist_ok=True)
    (repo / ".github/workflows" / name).write_text(text)
    sh("git add -A && git commit -qm ci", repo)


def test_workflow_jobs_reads_the_check_names():
    assert ci_client.workflow_jobs(CI_WORKFLOW) == ["Tests", "lint"]
    assert ci_client.workflow_jobs("on: push\n") == []
    assert ci_client.workflow_jobs("jobs:\n    a:\n        name: 'A'\n    b: # c\n        x: 1\n") == ["A", "b"]


def test_repo_jobs_leaves_brindles_own_workflows_out(ci_repo):
    commit_workflow(ci_repo)
    commit_workflow(ci_repo, "brindle-ci-fix.yml", "jobs:\n  fix:\n    runs-on: x\n")
    commit_workflow(ci_repo, "more.yaml", "jobs:\n  lint:\n    runs-on: x\n  e2e:\n    runs-on: x\n")
    assert ci_client.repo_jobs(str(ci_repo)) == ["Tests", "lint", "e2e"]


def protection_writes(go):
    return [(c[2], c[c.index("-X") + 1], json.loads(i)) for c, i in zip(go.calls, go.inputs)
            if c[:2] == ["gh", "api"] and "-X" in c]


def test_init_leaves_existing_required_checks_alone(init_run, ci_repo):
    commit_workflow(ci_repo)
    _, said = init_run(credential="key", gh={"required": ["build"]},
                       ask=lambda q, d: pytest.fail(f"asked {q!r}") if "required" in q else d)
    assert not protection_writes(init_run)
    assert "main requires build" in "\n".join(said)


def test_init_asks_to_require_each_job_and_protects_the_branch(init_run, ci_repo):
    commit_workflow(ci_repo)
    asked = []

    def ask(q, d):
        if "required" in q:
            asked.append(q)
            return "n" if "'lint'" in q else ""
        return d
    _, said = init_run(credential="key", ask=ask)
    assert asked == ["Mark 'Tests' as required so brindle can fix it when it fails? [Y/n]",
                     "Mark 'lint' as required so brindle can fix it when it fails? [Y/n]"]
    assert protection_writes(init_run) == [(f"repos/{REPO}/branches/main/protection", "PUT", {
        "required_status_checks": {"strict": False, "contexts": ["Tests"]}, "enforce_admins": False,
        "required_pull_request_reviews": None, "restrictions": None})]
    text = "\n".join(said)
    assert "warning: main requires no status checks" in text and "main now requires Tests" in text


def test_init_adds_the_check_to_existing_protection(init_run, ci_repo):
    commit_workflow(ci_repo)
    init_run(credential="key", gh={"protected": True, "branch": "trunk"})
    assert protection_writes(init_run) == [
        (f"repos/{REPO}/branches/trunk/protection/required_status_checks", "PATCH",
         {"contexts": ["Tests", "lint"]})]


def test_init_required_check_options(init_run, ci_repo):
    commit_workflow(ci_repo)
    never = lambda q, d: pytest.fail(f"asked {q!r}") if "required" in q else d  # noqa: E731
    init_run(credential="key", required_check="e2e", ask=never)
    assert [w[2] for w in protection_writes(init_run)] == [{
        "required_status_checks": {"strict": False, "contexts": ["e2e"]}, "enforce_admins": False,
        "required_pull_request_reviews": None, "restrictions": None}]
    init_run.calls.clear()
    init_run.inputs.clear()
    _, said = init_run(credential="key", no_required_check=True, ask=never)
    assert not protection_writes(init_run)
    assert "Require status checks to pass" in "\n".join(said)


def test_init_explains_the_settings_when_github_refuses_protection(init_run, ci_repo):
    commit_workflow(ci_repo)
    _, said = init_run(credential="key", gh={"refuse": PLAN_REFUSAL})
    text = "\n".join(said)
    assert "GitHub didn't let brindle require Tests, lint" in text and "Upgrade to GitHub Pro" in text
    assert "private repositories need a paid GitHub plan" in text and "now requires" not in text
    assert f"Settings > Branches on github.com/{REPO}" in text and "Require status checks to pass" in text
    assert "8/8 doctor" in text, "init carries on"


def test_init_reports_a_404_protection_put_as_refused(init_run, ci_repo):
    """GitHub answers 404 to a protection change by someone without admin
    rights: that is a refusal, not success."""
    commit_workflow(ci_repo)
    _, said = init_run(credential="key", gh={"refuse": NOT_FOUND})
    text = "\n".join(said)
    assert "GitHub refused to protect main (404: no admin rights, or private repositories need a paid" in text
    assert "now requires" not in text and "Require status checks to pass" in text


def test_init_reports_protection_without_status_checks(init_run, ci_repo):
    commit_workflow(ci_repo)
    _, said = init_run(credential="key", gh={"protected": True, "refuse": NOT_FOUND})
    text = "\n".join(said)
    assert ("this branch is protected but doesn't require status checks; "
            "add Tests, lint under Settings > Branches") in text
    assert "paid" not in text and "now requires" not in text


def test_init_warns_when_there_is_no_job_to_require(init_run):
    _, said = init_run(credential="key")
    text = "\n".join(said)
    assert "warning: main requires no status checks" in text and "no workflow jobs" in text
    assert not protection_writes(init_run)


@pytest.mark.parametrize("url", ["https://github.com/someone/brindle.git", None])
def test_init_refuses_a_checkout_of_another_repo(ci_repo, url):
    """--repo names one repository, the checkout here is another (or has no
    origin): init stops before gh or git does anything, rather than pushing
    the setup branch to the wrong repository."""
    sh(f"git remote set-url origin {url}" if url else "git remote remove origin", ci_repo)
    before = sh("git rev-parse --abbrev-ref HEAD", ci_repo)
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        raise AssertionError("gh must not run")
    with pytest.raises(CIError, match=f"run this inside a checkout of {REPO}"):
        ci_client.init(repo=REPO, org=None, providers=["claude"], cwd=str(ci_repo), env={}, run=run,
                       open_url=lambda url: None, client=ci_client.Client(BASE, FakeTransport({})),
                       say=lambda s: None)
    assert not calls and sh("git rev-parse --abbrev-ref HEAD", ci_repo) == before


@pytest.mark.parametrize("url", [f"git@github.com:{REPO}.git", f"https://github.com/{REPO}",
                                 f"ssh://git@github.com/{REPO}.git/", "https://github.com/ACME/Widgets.git"])
def test_origin_repo_matches_any_url_form(ci_repo, url):
    sh(f"git remote set-url origin {url}", ci_repo)
    ci_client.check_checkout(REPO, str(ci_repo))


def test_check_checkout_shows_an_unreadable_origin_without_credentials(ci_repo):
    sh("git remote set-url origin https://user:s3cret@git.example.test", ci_repo)
    with pytest.raises(CIError) as e:
        ci_client.check_checkout(REPO, str(ci_repo))
    msg = str(e.value)
    assert "origin here (https://git.example.test) isn't a GitHub owner/name" in msg
    assert "s3cret" not in msg and "user" not in msg


def test_check_checkout_when_git_fails(ci_repo, monkeypatch):
    from brindle import git as git_mod

    def boom(args, cwd, check=True):
        raise git_mod.GitError("git remote get-url origin: timed out")
    monkeypatch.setattr(git_mod, "run", boom)
    with pytest.raises(CIError, match=f"run this inside a checkout of {REPO} \\(no origin remote here\\)"):
        ci_client.check_checkout(REPO, str(ci_repo))


def test_init_builds_the_pro_account_itself(ci_repo, monkeypatch, ci_entitled):
    """Without ``account=``, init builds the brindle Pro account the way the
    ``brindle.account`` entry point does (it once called a class that didn't
    exist and crashed after step 2)."""
    from brindle.pro import account, auth, credentials

    class Proc:
        def __init__(self, out=""):
            self.stdout, self.stderr, self.returncode = out, "", 0

    def run(argv, **kw):
        if argv[:2] == ["gh", "api"]:
            return Proc("true\n")
        return Proc()

    class Store:
        def load(self):
            return {"org_id": "org_1", "base_url": BASE}

    store = Store()
    monkeypatch.setattr(credentials, "default_store", lambda *a, **kw: store)
    got = {}

    class Stop(Exception):
        pass

    def create_ci_token(client, s, org, name):
        got.update(client=client, store=s, org=org, name=name)
        raise Stop
    monkeypatch.setattr(auth, "create_ci_token", create_ci_token)
    said, opened = [], []
    sh(f"git remote set-url origin https://github.com/{REPO}.git", ci_repo)
    with pytest.raises(Stop):
        ci_client.init(repo=REPO, org=None, providers=["claude"], cwd=str(ci_repo), env={},
                       run=run, open_url=opened.append, client=ci_client.Client(BASE, FakeTransport({})),
                       say=said.append)
    assert any(s.startswith("3/8") for s in said)
    # The install page needs the org: the server answers 400 "org_id is required" without it.
    assert opened == [BASE + "/github/install?org_id=org_1"]
    assert got["store"] is store and got["org"] == "org_1" and got["name"] == f"ci:{REPO}"
    assert ci_entitled and ci_entitled[0]["store"] is store, "the entitlement checked is the account's"
    assert isinstance(got["client"], auth.Client)
    assert isinstance(account.make(str(ci_repo)), account.ProAccount)
