"""Brindle-CI end to end, driven the way a CI job drives it: the documented
``brindle ci start|run|report|doctor`` commands through the CLI, against a
fake brindle server, a fake GitLab API and fake model adapters. No network."""

import http.server
import json
import os
import threading
import time
import urllib.request
import uuid

import pytest
from typer.testing import CliRunner

from brindle import ci_adapters, ci_client, ci_federation, ci_hosts, pricing
from brindle.ci_client import CIError
from brindle.cli import app
from brindle.db import DB, Agent
from conftest import sh
from pro_fixtures import BASE, TEST_KID, FakeTransport, sign, signing_key  # noqa: F401

REPO = "acme/widgets"
NESTED = "acme/sub/widgets"
RUN_TOKEN = "crt_" + "r" * 32
CI_TOKEN = "cpc_" + "c" * 32
API = "https://gitlab.example.com/api/v4"


# -- fixtures (this file only) ---------------------------------------------------------------------


def claims(kind, repo=REPO, **over):
    now = int(time.time())
    c = {"iss": BASE, "aud": "brindle-pro", "sub": "ci:ct_1", "org_id": "org_1", "kid": TEST_KID,
         "jti": "p1", "iat": now, "exp": now + 3600, "token_use": "ci_plan", "plan_kind": kind, "repo": repo}
    if kind == "run":
        c.update({"id": "run_1", "goal": {"title": "Fix it"}, "branch": "brindle/ci-1", "base_branch": "main",
                  "base_sha": "0" * 40, "milestones": [{"id": 1, "title": "tests pass", "check": "true"}],
                  "instructions": "do the thing", "provider": "claude", "profile": None,
                  "limits": {"timeout_min": 10, "token_budget": 0, "heartbeat_s": 0}, "attempt": 1})
    else:
        c.update({"id": "val_1", "pr": 7, "head_sha": "0" * 40,
                  "checks": [{"id": "c1", "command": "echo ok", "timeout_s": 30}],
                  "reviewers": [{"id": "r1", "provider": "claude", "instructions": "review it"}],
                  "limits": {"token_budget": 0}})
    c.update(over)
    return c


@pytest.fixture
def mkplan(signing_key):  # noqa: F811
    return lambda kind="run", **over: sign(signing_key, claims(kind, **over), {"typ": ci_client.PLAN_TYP})


@pytest.fixture
def job(repo, monkeypatch, tmp_path):
    """A checkout, a clean CI environment, and the brindle server's answers."""
    monkeypatch.chdir(repo)
    for k in list(os.environ):
        # not BRINDLE_HOME & co: the autouse fixtures point those at the test's own directory
        if k.startswith(("GITHUB_", "GITLAB_", "CI_", "ACTIONS_", "ANTHROPIC_", "BRINDLE_CI_", "GH_")) \
                or k in ("CI", "BRINDLE_PRO_TOKEN", "BRINDLE_ID_TOKEN", "BRINDLE_GITLAB_TOKEN"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("BRINDLE_PRO_TOKEN", CI_TOKEN)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    return repo


class FakeAdapter(ci_adapters.Adapter):
    """A supervisor that is a row in the DB: ``on_launch`` moves its autopilot."""

    def __init__(self, name="claude", *, finish_on_beat=None, usage=None, available=True):
        self.name, self._usage, self._available = name, usage, available
        self.finish_on_beat, self.beats = finish_on_beat, 0
        self.launched, self.stopped, self.reviews = [], [], []

    def installed(self, env=None):
        return self._available

    def credential(self, env):
        return ci_adapters.Credential("api_key" if self._available else None, ("FAKE_KEY",))

    def launch(self, db, ws, instructions, profile):
        aid = f"{self.name[:2]}{len(self.launched)}{uuid.uuid4().hex[:5]}"
        db.add_agent(Agent(id=aid, workspace_id=ws.id, profile=profile or "supervisor", provider=self.name,
                           parent_id=None, mode="interactive", status="idle", tmux_window="", result=None,
                           created_at=time.time(), task=instructions))
        db.add_autopilot(aid)
        self.launched.append((aid, instructions))
        return db.get_agent(aid)

    def stop(self, db, root_id):
        self.stopped.append(root_id)

    def alive(self, db, root):
        return True

    def usage(self, db, root_id):
        # called once per heartbeat, after the state was read: the supervisor reaches its goal after that beat
        self.beats += 1
        if self.finish_on_beat and self.beats >= self.finish_on_beat:
            db.update_autopilot(root_id, state="done")
        return dict(self._usage if self._usage is not None else {"fake-model": {"input": 10, "output": 5}})

    def review(self, instructions, cwd, env, *, timeout=0, profile=None):
        self.reviews.append(instructions)
        return ci_adapters.Review("LGTM", model="fake-model", exit=0, usage={"fake-model": {"input": 3}})


class Server:
    """The brindle CI server: records requests, scripts heartbeat answers."""

    def __init__(self, run_plan=None, validation_plan=None, actions=None, evidence_status="posted"):
        self.run_plan, self.validation_plan = run_plan, validation_plan
        self.actions = list(actions or [])
        self.events, self.results, self.evidence, self.job_status = [], [], [], []
        self.fail_events = False
        self.transport = FakeTransport({
            "POST /ci/runs": lambda f, h: (201, {"run_id": "run_1", "plan": self.run_plan, "run_token": RUN_TOKEN}),
            "POST /ci/validations": lambda f, h: (201, {"validation_id": "val_1", "plan": self.validation_plan,
                                                        "run_token": RUN_TOKEN}),
            "POST /ci/runs/run_1/events": self._events,
            "PUT /ci/runs/run_1/result": self._result,
            "PUT /ci/validations/val_1/evidence": self._evidence,
            "POST /ci/runs/run_1/job-status": self._status,
            "POST /ci/validations/val_1/job-status": self._status,
        })
        self.client = ci_client.Client(BASE, self.transport)
        self.evidence_status = evidence_status

    def _events(self, form, headers):
        if self.fail_events:
            from brindle.pro.auth import TransportError
            raise TransportError("down")
        self.events.append(dict(form))
        return 200, self.actions.pop(0) if self.actions else {"action": "continue"}

    def _result(self, form, headers):
        self.results.append(form.data)
        return 200, {"status": "published", "url": "https://gh.test/pr/9"}

    def _evidence(self, form, headers):
        self.evidence.append(json.loads(json.dumps(form)))
        return 200, {"status": self.evidence_status, "conclusion": "success"}

    def _status(self, form, headers):
        self.job_status.append((headers["Authorization"], dict(form)))
        return 200, {}


def evidence_of(body):
    i = body.index(b'name="evidence"')
    start = body.index(b"\r\n\r\n", i) + 4
    return json.loads(body[start:body.index(b"\r\n--", start)])


@pytest.fixture
def wire(monkeypatch):
    """Point the CLI at ``server`` and ``adapter``, as the workflow's runner would reach them."""
    def go(server, adapter, host=ci_hosts.GitHubHost):
        monkeypatch.setattr(host, "client", lambda self, env=None, **kw: server.client)
        monkeypatch.setattr(ci_adapters, "default_adapters", lambda root=None: {"claude": adapter})
    return go


def cli(*args):
    return CliRunner().invoke(app, ["ci", *args])


# -- GitHub: the documented commands, start -> run -> report -------------------------------------


def test_github_start_run_report_through_the_cli(job, mkplan, wire, tmp_path):
    base = sh("git rev-parse HEAD", job)
    adapter = FakeAdapter(finish_on_beat=1)
    server = Server(run_plan=mkplan(base_sha=base))
    wire(server, adapter)
    out = tmp_path / "plan"

    r = cli("start", "--out", str(out), "--issue", "3", "--providers", "claude")
    assert r.exit_code == 0, r.output
    assert (out / "plan.jwt").exists() and (out / "run_token").read_text().strip() == RUN_TOKEN
    assert oct((out / "run_token").stat().st_mode & 0o777) == "0o600"
    start_call = server.transport.calls[0]
    assert start_call[1]["providers_available"] == ["claude"] and start_call[1]["trigger"] == {"kind": "issue", "issue": 3}

    r = cli("run", "--plan", str(out / "plan.jwt"), "--run-token-file", str(out / "run_token"))
    assert r.exit_code == 0, r.output
    assert "published: https://gh.test/pr/9" in r.output
    assert not (out / "run_token").exists(), "the run token is deleted once read"
    assert "BRINDLE_PRO_TOKEN" not in os.environ, "the job's secrets are scrubbed before the agents start"
    assert evidence_of(server.results[0])["final_state"] == "finished"

    # the report step is its own job with its own environment
    os.environ["BRINDLE_PRO_TOKEN"] = CI_TOKEN
    r = cli("report", "--plan-dir", str(out), "--start", "success", "--run", "failure")
    assert r.exit_code == 0, r.output
    assert [s[1]["conclusion"] for s in server.job_status] == ["success", "failure"]


def test_report_sends_each_conclusion(job, mkplan, wire, tmp_path):
    server = Server(run_plan=mkplan())
    wire(server, FakeAdapter())
    out = tmp_path / "plan"
    assert cli("start", "--out", str(out), "--goal-text", "Fix\nthe detail", "--providers", "claude").exit_code == 0
    r = cli("report", "--plan-dir", str(out), "--start", "success", "--run", "cancelled")
    assert r.exit_code == 0, r.output
    assert [s[1]["job"] + ":" + s[1]["conclusion"] for s in server.job_status] == ["start:success", "run:cancelled"]
    assert all(s[0] == f"Bearer {CI_TOKEN}" for s in server.job_status)
    assert cli("report", "--plan-dir", str(out), "--run", "exploded").exit_code != 0
    assert "nothing to report" in cli("report", "--plan-dir", str(tmp_path / "none")).output


def test_start_needs_exactly_one_trigger_and_a_valid_validate(job, tmp_path):
    out = str(tmp_path / "o")
    assert cli("start", "--out", out).exit_code != 0
    assert cli("start", "--out", out, "--issue", "1", "--goal-text", "x").exit_code != 0
    assert cli("start", "--out", out, "--validate", "--pr", "1", "--head", "abc").exit_code != 0
    assert cli("start", "--out", out, "--dispatch", "nope").exit_code != 0


def test_validate_start_and_run_through_the_cli(job, mkplan, wire, tmp_path):
    sha = sh("git rev-parse HEAD", job)
    adapter = FakeAdapter()
    server = Server(validation_plan=mkplan("validation", head_sha=sha))
    wire(server, adapter)
    out = tmp_path / "v"
    r = cli("start", "--out", str(out), "--validate", "--pr", "7", "--head", sha, "--fork", "false",
            "--providers", "claude")
    assert r.exit_code == 0, r.output
    assert server.transport.calls[0][1]["fork"] is False
    r = cli("run", "--plan", str(out / "plan.jwt"), "--run-token-file", str(out / "run_token"))
    assert r.exit_code == 0, r.output
    ev = server.evidence[0]
    assert ev["checks"][0]["exit"] == 0 and ev["reviews"][0]["reply"] == "LGTM"
    assert ev["usage"] == {"fake-model": {"input": 3, "output": 0, "cache_read": 0}}
    assert "posted: success" in r.output


def test_validate_checks_never_see_secrets_or_model_keys(job, mkplan, wire, tmp_path, monkeypatch):
    sha = sh("git rev-parse HEAD", job)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok-secret")
    plan = mkplan("validation", head_sha=sha,
                  checks=[{"id": "c1", "command": "env", "timeout_s": 30}])
    server = Server(validation_plan=plan)
    wire(server, FakeAdapter())
    out = tmp_path / "v"
    assert cli("start", "--out", str(out), "--validate", "--pr", "7", "--head", sha).exit_code == 0
    assert cli("run", "--plan", str(out / "plan.jwt"), "--run-token-file", str(out / "run_token")).exit_code == 0
    seen = server.evidence[0]["checks"][0]["output_excerpt"]
    assert "sk-ant-secret" not in seen and "tok-secret" not in seen and CI_TOKEN not in seen


# -- GitLab: the same commands in a GitLab job ----------------------------------------------------


def gitlab_env(monkeypatch):
    for k, v in {"GITLAB_CI": "true", "CI_PROJECT_PATH": NESTED, "CI_API_V4_URL": API, "CI_JOB_TOKEN": "jobtok",
                 "CI_MERGE_REQUEST_SOURCE_PROJECT_PATH": NESTED}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_REPOSITORY")
    monkeypatch.setattr(ci_hosts, "entitled", lambda: True)


def test_gitlab_validation_through_the_cli(job, mkplan, wire, tmp_path, monkeypatch):
    gitlab_env(monkeypatch)
    sha = sh("git rev-parse HEAD", job)
    server = Server(validation_plan=mkplan("validation", repo=NESTED, head_sha=sha))
    wire(server, FakeAdapter(), host=ci_hosts.GitLabHost)
    out = tmp_path / "v"
    r = cli("start", "--out", str(out), "--validate", "--pr", "7", "--head", sha)
    assert r.exit_code == 0, r.output
    call = server.transport.calls[0][1]
    assert call["repo"] == NESTED and call["fork"] is False, "same-project merge request is no fork"
    # GitLab is fail-closed: reported as not usable subscription-only credentials (org_hint True): api key is fine.
    assert call["providers_available"] == ["claude"]
    r = cli("run", "--plan", str(out / "plan.jwt"), "--run-token-file", str(out / "run_token"))
    assert r.exit_code == 0, r.output
    assert "CI_JOB_TOKEN" not in os.environ


def test_gitlab_merge_request_from_a_fork_is_flagged(job, mkplan, wire, tmp_path, monkeypatch):
    gitlab_env(monkeypatch)
    monkeypatch.setenv("CI_MERGE_REQUEST_SOURCE_PROJECT_PATH", "someone/widgets")
    sha = sh("git rev-parse HEAD", job)
    server = Server(validation_plan=mkplan("validation", repo=NESTED, head_sha=sha))
    wire(server, FakeAdapter(), host=ci_hosts.GitLabHost)
    assert cli("start", "--out", str(tmp_path / "v"), "--validate", "--pr", "7", "--head", sha).exit_code == 0
    assert server.transport.calls[0][1]["fork"] is True


def test_gitlab_is_refused_without_the_entitlement(job, monkeypatch, tmp_path):
    gitlab_env(monkeypatch)
    monkeypatch.setattr(ci_hosts, "entitled", lambda: False)
    r = cli("start", "--out", str(tmp_path / "o"), "--issue", "1")
    assert r.exit_code != 0 and "Enterprise" in r.output
    r = cli("report", "--plan-dir", str(tmp_path))
    assert r.exit_code != 0


def test_gitlab_personal_subscription_is_never_used(job, monkeypatch):
    gitlab_env(monkeypatch)
    adapter = ci_adapters.ClaudeAdapter()
    monkeypatch.setattr(adapter, "installed", lambda env=None: True)
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "x"}
    assert ci_adapters.usable(adapter, env, ci_hosts.GitLabHost.org_hint)[0] is False


def test_gitlab_api_merge_request_and_note(monkeypatch):
    calls = []

    def api(method, url, headers, body):
        calls.append((method, url, headers, body))
        if url.endswith("/merge_requests"):
            return 201, {"iid": 4, "web_url": "https://gitlab.example.com/x/-/merge_requests/4"}
        return 201, {"id": 5}

    env = {"CI_PROJECT_PATH": NESTED, "CI_API_V4_URL": API + "/", "CI_JOB_TOKEN": "jobtok"}
    h = ci_hosts.get_host("gitlab", env, is_entitled=lambda: True, request=api)
    assert h.create_merge_request(env, source="a", target="main", title="t")["iid"] == 4
    assert h.comment(env, 4, "hi") == "5"
    assert calls[0][1] == f"{API}/projects/acme%2Fsub%2Fwidgets/merge_requests"
    # a malformed answer is an error, never a half-built result
    h2 = ci_hosts.get_host("gitlab", env, is_entitled=lambda: True, request=lambda *a: (201, {"iid": True}))
    with pytest.raises(CIError):
        h2.create_merge_request(env, source="a", target="main", title="t")
    # a 204/other success is not a failure that leaks the token
    h3 = ci_hosts.get_host("gitlab", env, is_entitled=lambda: True, request=lambda *a: (500, {"message": "boom jobtok"}))
    with pytest.raises(CIError) as e:
        h3.comment(env, 1, "x")
    assert e.value.code == "http_500"


# -- the CI budget: unpriced usage counts as over ----------------------------------------------------


def run_plan_of(mkplan, job, **over):
    return mkplan(base_sha=sh("git rev-parse HEAD", job), **over)


def run_it(job, token, server, adapter, tmp_path, *, sleep=lambda s: None, clock=time.time, env=None):
    tf = tmp_path / "rt"
    tf.write_text(RUN_TOKEN + "\n")
    return ci_client.run(token, tf, cwd=str(job), env=env or {"GITHUB_REPOSITORY": REPO, "PATH": os.environ["PATH"]},
                         client=server.client, db=DB(), adapters={"claude": adapter}, sleep=sleep, clock=clock,
                         say=lambda s: None)


def test_unpriced_usage_ends_a_budgeted_run(job, mkplan, tmp_path):
    token = run_plan_of(mkplan, job, budget_usd=5)
    server = Server(run_plan=token)
    run_it(job, token, server, FakeAdapter(), tmp_path)   # "fake-model" has no price
    assert len(server.events) == 1
    assert evidence_of(server.results[0])["final_state"] == "budget"


def test_a_run_without_a_budget_is_not_cut_off_by_unpriced_usage(job, mkplan, tmp_path):
    token = run_plan_of(mkplan, job)
    server = Server(run_plan=token, actions=[{"action": "continue"}] * 2 + [{"action": "stop", "reason": "timeout"}])
    run_it(job, token, server, FakeAdapter(), tmp_path)
    assert len(server.events) == 3 and evidence_of(server.results[0])["final_state"] == "timeout"


def test_the_dollar_limit_holds_while_the_server_is_unreachable(job, mkplan, tmp_path):
    token = run_plan_of(mkplan, job, budget_usd=5)
    server = Server(run_plan=token)
    server.fail_events = True
    clock = {"t": time.time()}

    def tick(s):
        clock["t"] += 1

    run_it(job, token, server, FakeAdapter(), tmp_path, sleep=tick, clock=lambda: clock["t"])
    assert evidence_of(server.results[0])["final_state"] == "budget", \
        "a run past the org's dollar limit must not keep spending because heartbeats fail"


def test_escalation_does_not_reset_the_dollar_spend(job, mkplan, tmp_path, monkeypatch):
    monkeypatch.setitem(pricing.PRICES, "fake-model", pricing.Price(1_000_000, 0, 0, 0))
    first = FakeAdapter(usage={"fake-model": {"input": 3}})
    token = run_plan_of(mkplan, job, budget_usd=5)
    esc = run_plan_of(mkplan, job, budget_usd=5)
    server = Server(run_plan=token, actions=[{"action": "escalate", "plan": esc}, {"action": "continue"},
                                             {"action": "continue"}])
    # attempt 1 spent $3 of $5; the escalated attempt's own usage is $3 too: $6 in all.
    seq = iter([{"fake-model": {"input": 3}}, {"fake-model": {"input": 3}}, {"fake-model": {"input": 3}}])
    first.usage = lambda db, root_id: next(seq)
    run_it(job, token, server, first, tmp_path)
    assert evidence_of(server.results[0])["final_state"] == "budget"


# -- adapters and doctor ---------------------------------------------------------------------------


def test_doctor_names_credentials_not_values_and_applies_the_org_rule(job, monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "event.json"))
    (tmp_path / "event.json").write_text(json.dumps({"repository": {"owner": {"type": "Organization"}}}))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sub-secret-value")
    monkeypatch.setattr(ci_adapters.ClaudeAdapter, "installed", lambda self, env=None: True)
    r = cli("doctor")
    assert r.exit_code == 0, r.output
    assert "CLAUDE_CODE_OAUTH_TOKEN" in r.output and "sub-secret-value" not in r.output
    assert "personal subscription" in r.output and "an organization" in r.output
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    out = cli("doctor").output
    assert "sk-ant-x" not in out and "usable for CI" in out


def test_doctor_in_a_gitlab_job_names_the_project_without_gh(job, monkeypatch):
    gitlab_env(monkeypatch)
    monkeypatch.setattr(ci_adapters, "repo_is_org", lambda *a, **k: pytest.fail("gh lookup on GitLab"))
    r = cli("doctor")
    assert r.exit_code == 0, r.output
    assert f"repository: {NESTED}, owner is an organization" in r.output


def test_native_endpoint_host_rejects_ambiguous_urls():
    host = ci_adapters.NativeAdapter.endpoint_host
    assert host("https://api.example.com/v1") == "api.example.com"
    for bad in ("https://user@evil.com/", "https://a.com\\@evil.com", "https://a.com/?x", "https://a.com#f",
                "https://a.com/x\n", "https://a.com\n", "https://[::1]/", "ftp://a.com"):
        assert host(bad) is None, bad


def test_native_keys_go_only_to_the_paired_host():
    allowed = ci_adapters.NativeAdapter.allowed_keys
    env = {"BRINDLE_CI_NATIVE_KEYS": "MY_KEY@api.example.com, OTHER, ANTHROPIC_API_KEY@evil.com, GITHUB_TOKEN@x.com"}
    got = allowed(env)
    assert got["MY_KEY"] == frozenset({"api.example.com"})
    assert got["OTHER"] == ci_adapters.LOOPBACK
    assert "ANTHROPIC_API_KEY" not in got and "GITHUB_TOKEN" not in got


def test_empty_model_keys_are_dropped_before_claude_starts():
    env = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "fed", "X": "1"}
    assert ci_adapters.claude_env(env) == {"ANTHROPIC_AUTH_TOKEN": "fed", "X": "1"}
    assert env["ANTHROPIC_API_KEY"] == "", "the caller's environment is untouched"


def test_review_usage_is_merged_across_models():
    into = {}
    ci_adapters.merge_usage(into, {"a": {"input": 1, "output": 2, "cache_read": 3}, "bad": 7})
    ci_adapters.merge_usage(into, {"a": {"input": 1}})
    assert into == {"a": {"input": 2, "output": 2, "cache_read": 3}}


# -- identity federation ----------------------------------------------------------------------------


@pytest.fixture
def upstream():
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append((self.path, self.headers.get("Authorization"), self.headers.get("x-api-key"),
                         self.rfile.read(n)))
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()
    srv.server_close()


FED_ENV = {"ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1", "ANTHROPIC_ORGANIZATION_ID": "0f8e2b1c-6a4d-4e3b-9c7a-1d2e3f4a5b6c",
           "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_1", "ACTIONS_ID_TOKEN_REQUEST_URL": "https://actions.test/t",
           "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "reqtok"}


def test_federation_configured_only_for_a_federated_job():
    assert ci_federation.configured(FED_ENV)
    assert not ci_federation.configured({**FED_ENV, "ANTHROPIC_API_KEY": "k"})
    assert not ci_federation.configured({**FED_ENV, "BRINDLE_CI_FEDERATION_REFRESH": "0"})
    assert not ci_federation.configured({k: v for k, v in FED_ENV.items() if k != "ANTHROPIC_SERVICE_ACCOUNT_ID"})


def test_the_proxy_swaps_the_run_secret_for_the_current_anthropic_token(upstream, monkeypatch):
    url, seen = upstream
    tokens = iter([("anthropic-tok-1", 1000.0), ("anthropic-tok-2", 1000.0)])
    monkeypatch.setattr(ci_federation, "fetch_identity_token", lambda u, t: "idtoken")
    monkeypatch.setattr(ci_federation, "exchange_token", lambda assertion, ids, **kw: next(tokens))
    fed = ci_federation.start(FED_ENV, upstream=url)
    try:
        env, proc_env = {"ANTHROPIC_IDENTITY_TOKEN": "x"}, {"ANTHROPIC_IDENTITY_TOKEN_FILE": "/f"}
        fed.apply(env, proc_env)
        assert env["ANTHROPIC_BASE_URL"] == fed.proxy.base_url and "ANTHROPIC_IDENTITY_TOKEN" not in env
        assert "ANTHROPIC_IDENTITY_TOKEN_FILE" not in proc_env
        secret = env["ANTHROPIC_AUTH_TOKEN"]
        assert secret != "anthropic-tok-1"

        def post(auth):
            req = urllib.request.Request(fed.proxy.base_url + "/v1/messages", data=b"{}", method="POST",
                                         headers={"Authorization": auth, "x-api-key": "personal"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        assert post(f"Bearer {secret}") == (200, b'{"ok":true}')
        assert seen[-1][1] == "Bearer anthropic-tok-1" and seen[-1][2] is None
        assert post("Bearer wrong")[0] == 401 and len(seen) == 1, "nothing without the run's secret is forwarded"
        fed.refresher.tick()
        assert post(f"Bearer {secret}")[0] == 200 and seen[-1][1] == "Bearer anthropic-tok-2"
    finally:
        fed.stop()
    assert fed.refresher.token() is None


def test_a_failed_first_exchange_keeps_the_workflows_token(monkeypatch):
    def boom(u, t):
        raise ci_federation.FederationError("no")
    monkeypatch.setattr(ci_federation, "fetch_identity_token", boom)
    said = []
    assert ci_client.start_federation(FED_ENV, say=said.append) is None
    assert "using the workflow's token" in said[0]


def test_the_refresher_survives_failures_and_backs_off():
    r = ci_federation.TokenRefresher(FED_ENV, "u", "t", fetch=lambda u, t: (_ for _ in ()).throw(RuntimeError("x")),
                                     exchange=lambda *a: ("t", 1.0))
    assert [r.tick() for _ in range(5)] == [5.0, 15.0, 45.0, 60.0, 60.0]
    ok = ci_federation.TokenRefresher(FED_ENV, "u", "t", fetch=lambda u, t: "j", exchange=lambda *a: ("tok", 600.0))
    assert ok.tick() == 420.0 and ok.token() == "tok"
