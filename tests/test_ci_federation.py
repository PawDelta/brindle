"""brindle CI's own identity federation (brindle.ci_federation): the token
exchange, the refresher's schedule, and the loopback credential proxy
against a fake upstream."""

import http.client
import http.server
import json
import logging
import socket
import threading
import urllib.error

import pytest

from brindle import ci_federation
from brindle.ci_federation import CredentialProxy, FederationError, TokenRefresher

IDS = {"ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1", "ANTHROPIC_ORGANIZATION_ID": "org-uuid",
       "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_1"}
ACTIONS = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.test/x?api-version=2",
           "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req-secret"}
WIF = "sk-ant-oat01-federated-token"


# -- configured --------------------------------------------------------------------------------


def test_configured_needs_the_ids_and_the_actions_endpoint():
    assert ci_federation.configured({**IDS, **ACTIONS})
    assert ci_federation.configured({**IDS, **ACTIONS, "ANTHROPIC_WORKSPACE_ID": "wrkspc_1", "ANTHROPIC_API_KEY": ""})
    for missing in (*IDS, *ACTIONS):
        env = {**IDS, **ACTIONS}
        del env[missing]
        assert not ci_federation.configured(env), missing
    # A key wins in Claude Code; another base URL is some gateway's; and off is off.
    assert not ci_federation.configured({**IDS, **ACTIONS, "ANTHROPIC_API_KEY": "sk-ant-api03-x"})
    assert not ci_federation.configured({**IDS, **ACTIONS, "ANTHROPIC_BASE_URL": "https://gw.test"})
    assert not ci_federation.configured({**IDS, **ACTIONS, "BRINDLE_CI_FEDERATION_REFRESH": "0"})


# -- the exchange ------------------------------------------------------------------------------


class FakeExchangeEndpoint:
    def __init__(self, answer=None, status=None, body=b""):
        self.answer = answer if answer is not None else {"access_token": WIF, "token_type": "Bearer",
                                                         "expires_in": 598, "scope": "workspace:developer"}
        self.status, self.body = status, body
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req.full_url, req.get_method(), dict(req.header_items()), json.loads(req.data)))
        if self.status:
            raise urllib.error.HTTPError(req.full_url, self.status, "nope", {}, _Body(self.body))

        class Resp:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                pass

            def read(s, n=-1):
                return self.answer if isinstance(self.answer, bytes) else json.dumps(self.answer).encode()
        return Resp()


class _Body:
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        return self.data

    def close(self):
        pass


@pytest.fixture
def endpoint(monkeypatch):
    import urllib.request

    ep = FakeExchangeEndpoint()
    monkeypatch.setattr(urllib.request, "urlopen", ep)
    return ep


def test_exchange_posts_the_jwt_bearer_grant(endpoint):
    token, expires = ci_federation.exchange_token("h.p.s", {**IDS, "ANTHROPIC_WORKSPACE_ID": "wrkspc_1"})
    assert (token, expires) == (WIF, 598.0)
    url, method, headers, body = endpoint.requests[0]
    assert url == "https://api.anthropic.com/v1/oauth/token" and method == "POST"
    assert headers["Content-type"] == "application/json"
    assert body == {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": "h.p.s",
                    "federation_rule_id": "fdrl_1", "organization_id": "org-uuid",
                    "service_account_id": "svac_1", "workspace_id": "wrkspc_1"}


def test_exchange_omits_an_unset_workspace(endpoint):
    ci_federation.exchange_token("h.p.s", {**IDS, "ANTHROPIC_WORKSPACE_ID": ""})
    assert "workspace_id" not in endpoint.requests[0][3]


def test_exchange_reports_a_refusal_without_the_token(endpoint):
    endpoint.status, endpoint.body = 401, b'{"type":"error","error":{"type":"authentication_error","message":"Authentication failed"}}'
    with pytest.raises(FederationError, match=r"HTTP 401: Authentication failed"):
        ci_federation.exchange_token("h.p.s", IDS)
    endpoint.status, endpoint.body = 400, b"<html>\x01"
    with pytest.raises(FederationError, match=r"HTTP 400: no message"):
        ci_federation.exchange_token("h.p.s", IDS)


@pytest.mark.parametrize("answer", [b"not json", {"token_type": "Bearer"}, {"access_token": "", "expires_in": 5},
                                    {"access_token": "t", "expires_in": 0}, {"access_token": "t", "expires_in": True},
                                    {"access_token": "t", "expires_in": "soon"}, [WIF]])
def test_exchange_rejects_an_answer_without_a_token(endpoint, answer):
    endpoint.answer = answer
    with pytest.raises(FederationError, match="held no token"):
        ci_federation.exchange_token("h.p.s", IDS)


def test_exchange_insists_on_https():
    with pytest.raises(FederationError, match="isn't https"):
        ci_federation.exchange_token("h.p.s", IDS, url="http://api.anthropic.com/v1/oauth/token")


def test_identity_token_is_fetched_for_anthropics_audience(monkeypatch):
    from brindle import ci_client

    seen = {}

    def fetch(url, request_token, timeout=15.0, audience=None):
        seen.update(url=url, token=request_token, audience=audience)
        return "h.p.s"
    monkeypatch.setattr(ci_client, "_fetch_oidc", fetch)
    assert ci_federation.fetch_identity_token("https://t.actions.test/x", "req") == "h.p.s"
    assert seen == {"url": "https://t.actions.test/x", "token": "req", "audience": "https://api.anthropic.com"}

    def down(url, request_token, timeout=15.0, audience=None):
        raise ci_client.CIError("couldn't get the GitHub Actions OIDC token (HTTP 503)", code="oidc")
    monkeypatch.setattr(ci_client, "_fetch_oidc", down)
    with pytest.raises(FederationError, match="HTTP 503"):
        ci_federation.fetch_identity_token("https://t.actions.test/x", "req")


# -- the refresher ------------------------------------------------------------------------------


def make_refresher(clock, *, answers=None, expires_in=600.0):
    """A refresher whose exchange hands out tokens from ``answers`` (strings
    or exceptions), each from a freshly fetched identity token."""
    fetched, exchanged = [], []
    answers = list(answers or [])

    def fetch(url, request_token):
        fetched.append((url, request_token))
        return f"jwt-{len(fetched)}"

    def exchange(assertion, ids):
        exchanged.append((assertion, dict(ids)))
        a = answers.pop(0) if answers else f"tok-{len(exchanged)}"
        if isinstance(a, Exception):
            raise a
        return a, expires_in
    r = TokenRefresher({**IDS, "ANTHROPIC_WORKSPACE_ID": "", **ACTIONS}, ACTIONS["ACTIONS_ID_TOKEN_REQUEST_URL"],
                       ACTIONS["ACTIONS_ID_TOKEN_REQUEST_TOKEN"], fetch=fetch, exchange=exchange,
                       clock=lambda: clock["t"])
    return r, fetched, exchanged


def test_refresher_exchanges_a_fresh_identity_token_each_time():
    clock = {"t": 1000.0}
    r, fetched, exchanged = make_refresher(clock)
    assert r.token() is None
    assert r.exchange_now() == 600.0
    assert r.token() == "tok-1"
    assert r.tick() == pytest.approx(420.0), "the next refresh at 70% of expires_in"
    assert r.token() == "tok-2"
    # GitHub's tokens are single-use: one fetch per exchange, for Anthropic's audience endpoint.
    assert [a for a, _ in exchanged] == ["jwt-1", "jwt-2"]
    assert fetched == [(ACTIONS["ACTIONS_ID_TOKEN_REQUEST_URL"], "req-secret")] * 2
    assert exchanged[0][1] == IDS, "an unset workspace isn't sent"


def test_refresher_token_expires_without_a_refresh():
    clock = {"t": 1000.0}
    r, _, _ = make_refresher(clock, expires_in=100.0)
    r.exchange_now()
    clock["t"] += 99
    assert r.token() == "tok-1"
    clock["t"] += 1
    assert r.token() is None


def test_refresher_retries_a_failed_refresh_while_the_old_token_works(caplog):
    clock = {"t": 1000.0}
    down = FederationError("the Anthropic token exchange failed (HTTP 503: overloaded)")
    r, _, _ = make_refresher(clock, answers=["sk-ant-oat01-first", down, down, down, down, "sk-ant-oat01-second"],
                             expires_in=600.0)
    r.exchange_now()
    with caplog.at_level(logging.WARNING, logger="brindle.ci_federation"):
        delays = [r.tick() for _ in range(4)]
    assert delays == [*ci_federation.RETRY_DELAYS, ci_federation.RETRY_EVERY_S]
    assert r.token() == "sk-ant-oat01-first", "kept until it expires"
    assert all("HTTP 503" in rec.message for rec in caplog.records) and len(caplog.records) == 4
    assert "sk-ant-oat01" not in caplog.text
    assert r.tick() == pytest.approx(420.0) and r.token() == "sk-ant-oat01-second"
    assert r.tick() == pytest.approx(420.0), "the failure count starts over"


def test_refresher_survives_an_unexpected_error_and_names_only_its_class(caplog):
    clock = {"t": 1000.0}
    r, _, _ = make_refresher(clock, answers=["sk-ant-oat01-first", RuntimeError("body: sk-ant-oat01-leak"),
                                             "sk-ant-oat01-second"])
    r.exchange_now()
    with caplog.at_level(logging.WARNING, logger="brindle.ci_federation"):
        assert r.tick() == ci_federation.RETRY_DELAYS[0]
    assert r.token() == "sk-ant-oat01-first"
    assert "unexpected RuntimeError" in caplog.text and "leak" not in caplog.text and "sk-ant-oat01" not in caplog.text
    assert r.tick() == pytest.approx(420.0) and r.token() == "sk-ant-oat01-second"


def test_refresher_never_refreshes_faster_than_the_floor():
    clock = {"t": 1000.0}
    r, _, _ = make_refresher(clock, expires_in=20.0)
    r.exchange_now()
    assert r.tick() == ci_federation.MIN_REFRESH_S


def test_refresher_start_exchanges_in_the_caller_then_stop_forgets_the_token():
    clock = {"t": 1000.0}
    r, _, exchanged = make_refresher(clock)
    r.start()
    try:
        assert r.token() == "tok-1" and len(exchanged) == 1
    finally:
        r.stop()
    assert r.token() is None

    r2, _, _ = make_refresher(clock, answers=[FederationError("the Anthropic token exchange failed (HTTP 401: Authentication failed)")])
    with pytest.raises(FederationError, match="HTTP 401"):
        r2.start()


# -- the proxy ---------------------------------------------------------------------------------


class Upstream:
    """A fake Anthropic API on the loopback: records requests, answers with
    JSON, or streams two chunks with a pause the test controls."""

    def __init__(self):
        self.requests = []
        self.release = threading.Event()
        up = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                up.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if self.path.startswith("/stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    first = b"event: message_start\ndata: {}\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(first), first))
                    self.wfile.flush()
                    up.release.wait(10)
                    second = b"event: message_stop\ndata: {}\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(second), second))
                    self.wfile.flush()
                    return
                answer = json.dumps({"echo": body.decode(), "auth": self.headers.get("Authorization")}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.send_header("x-upstream", "yes")
                self.end_headers()
                self.wfile.write(answer)

            do_GET = do_POST

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    up = Upstream()
    yield up
    up.stop()


@pytest.fixture
def proxy(upstream):
    tokens = {"current": WIF}
    p = CredentialProxy(lambda: tokens["current"], upstream=upstream.url, secret="run-secret")
    p.start()
    yield p, tokens
    p.stop()


def call(base_url, method, path, body=None, headers=None):
    u = base_url.split("://", 1)[1]
    host, port = u.split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def test_proxy_swaps_the_run_secret_for_the_anthropic_token(proxy, upstream):
    p, _ = proxy
    assert p.base_url.startswith("http://127.0.0.1:")
    resp, data = call(p.base_url, "POST", "/v1/messages?beta=true", body=b'{"model":"m"}',
                      headers={"Authorization": "Bearer run-secret", "x-api-key": "run-secret",
                               "anthropic-beta": "x-2026", "Content-Type": "application/json",
                               "Connection": "keep-alive", "Host": "api.anthropic.com"})
    assert resp.status == 200 and json.loads(data) == {"echo": '{"model":"m"}', "auth": f"Bearer {WIF}"}
    assert resp.getheader("x-upstream") == "yes" and resp.getheader("Connection") == "close"
    path, headers, body = upstream.requests[0]
    assert path == "/v1/messages?beta=true" and body == b'{"model":"m"}'
    assert headers["anthropic-beta"] == "x-2026" and headers["content-type"] == "application/json"
    assert "x-api-key" not in headers and headers["host"] != "api.anthropic.com"
    assert "run-secret" not in json.dumps(headers)


def test_proxy_refuses_anything_but_the_run_secret(proxy, upstream):
    p, _ = proxy
    for auth in ({}, {"Authorization": "Bearer other"}, {"Authorization": "Bearer run-secret "},
                 {"x-api-key": "run-secret"}, {"Authorization": f"Bearer {WIF}"}):
        resp, data = call(p.base_url, "POST", "/v1/messages", body=b"{}", headers=auth)
        assert resp.status == 401 and json.loads(data)["error"]["type"] == "authentication_error"
    assert upstream.requests == []


def test_proxy_uses_the_current_token(proxy, upstream):
    p, tokens = proxy
    tokens["current"] = "sk-ant-oat01-rotated"
    resp, data = call(p.base_url, "POST", "/v1/messages", body=b"{}", headers={"Authorization": "Bearer run-secret"})
    assert json.loads(data)["auth"] == "Bearer sk-ant-oat01-rotated"
    tokens["current"] = None
    resp, data = call(p.base_url, "POST", "/v1/messages", body=b"{}", headers={"Authorization": "Bearer run-secret"})
    assert resp.status == 503 and len(upstream.requests) == 1


def test_proxy_streams_each_chunk_as_it_arrives(proxy, upstream):
    p, _ = proxy
    host, port = p.base_url.split("://", 1)[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.request("POST", "/stream", body=b"{}", headers={"Authorization": "Bearer run-secret"})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
    first = resp.read1(4096)
    assert first == b"event: message_start\ndata: {}\n\n", "arrived before the upstream sent the rest"
    upstream.release.set()
    assert resp.read() == b"event: message_stop\ndata: {}\n\n"
    conn.close()


def test_proxy_is_not_an_open_proxy(proxy, upstream):
    p, _ = proxy
    host, port = p.base_url.split("://", 1)[1].split(":")
    for line in (b"POST http://evil.test/v1/messages HTTP/1.1", b"CONNECT evil.test:443 HTTP/1.1",
                 b"POST evil.test/x HTTP/1.1"):
        s = socket.create_connection((host, int(port)), timeout=10)
        s.sendall(line + b"\r\nHost: x\r\nAuthorization: Bearer run-secret\r\nContent-Length: 0\r\n\r\n")
        answer = b""
        while True:
            part = s.recv(4096)
            if not part:
                break
            answer += part
        s.close()
        assert answer.startswith(b"HTTP/1.1 400") or answer.startswith(b"HTTP/1.1 501"), line
    assert upstream.requests == []


def test_proxy_forwards_a_repeated_header_as_one_list(proxy, upstream):
    p, _ = proxy
    host, port = p.base_url.split("://", 1)[1].split(":")
    s = socket.create_connection((host, int(port)), timeout=10)
    s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer run-secret\r\n"
              b"anthropic-beta: one\r\nAnthropic-Beta: two\r\nContent-Length: 2\r\n\r\n{}")
    answer = b""
    while True:
        part = s.recv(4096)
        if not part:
            break
        answer += part
    s.close()
    assert answer.startswith(b"HTTP/1.1 200")
    assert upstream.requests[0][1]["anthropic-beta"] == "one, two"


def test_proxy_drops_a_client_that_never_sends_its_body(upstream):
    p = CredentialProxy(lambda: WIF, upstream=upstream.url, secret="run-secret", idle_timeout=0.5)
    p.start()
    try:
        host, port = p.base_url.split("://", 1)[1].split(":")
        s = socket.create_connection((host, int(port)), timeout=10)
        s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer run-secret\r\n"
                  b"Content-Length: 10\r\n\r\n")
        assert s.recv(4096) == b"", "closed on the idle timeout, nothing forwarded"
        s.close()
        assert upstream.requests == []
        resp, data = call(p.base_url, "POST", "/v1/messages", body=b"{}", headers={"Authorization": "Bearer run-secret"})
        assert resp.status == 200, "still serving"
    finally:
        p.stop()


def test_proxy_caps_the_request_body(proxy, upstream, monkeypatch):
    p, _ = proxy
    monkeypatch.setattr(ci_federation, "MAX_REQUEST_BYTES", 10)
    resp, data = call(p.base_url, "POST", "/v1/messages", body=b"x" * 11, headers={"Authorization": "Bearer run-secret"})
    assert resp.status == 413
    resp, _ = call(p.base_url, "POST", "/v1/messages", body=b"x" * 10, headers={"Authorization": "Bearer run-secret"})
    assert resp.status == 200 and len(upstream.requests) == 1


def test_proxy_answers_502_when_the_upstream_is_down():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()   # nothing listens there now
    p = CredentialProxy(lambda: WIF, upstream=f"http://127.0.0.1:{port}", secret="run-secret")
    p.start()
    try:
        resp, data = call(p.base_url, "POST", "/v1/messages", body=b"{}", headers={"Authorization": "Bearer run-secret"})
        assert resp.status == 502 and "couldn't reach the Anthropic API" in json.loads(data)["error"]["message"]
    finally:
        p.stop()
    with pytest.raises(OSError):
        call(p.base_url, "GET", "/", headers={})


def test_proxy_logs_nothing(proxy, caplog):
    p, _ = proxy
    with caplog.at_level(logging.DEBUG):
        call(p.base_url, "POST", "/v1/messages?secret=in-path", body=b"{}",
             headers={"Authorization": "Bearer run-secret"})
        call(p.base_url, "POST", "/v1/messages", body=b"{}", headers={"Authorization": "Bearer nope"})
    assert "in-path" not in caplog.text and "run-secret" not in caplog.text and WIF not in caplog.text


def test_proxy_has_a_random_secret_and_binds_the_loopback_only(upstream):
    a = CredentialProxy(lambda: WIF, upstream=upstream.url)
    b = CredentialProxy(lambda: WIF, upstream=upstream.url)
    assert a.secret != b.secret and len(a.secret) >= 32
    assert a._server.server_address[0] == "127.0.0.1"
    a._server.server_close()
    b._server.server_close()
    with pytest.raises(ValueError):
        CredentialProxy(lambda: WIF, upstream="ftp://x")


# -- the run's federation ----------------------------------------------------------------------


def test_federation_apply_points_envs_at_the_proxy_and_drops_identity_tokens(upstream):
    p = CredentialProxy(lambda: WIF, upstream=upstream.url, secret="run-secret")
    fed = ci_federation.Federation(TokenRefresher(IDS, "https://t", "r", fetch=lambda u, t: "j",
                                                  exchange=lambda a, i: (WIF, 600.0)), p)
    env = {"ANTHROPIC_AUTH_TOKEN": WIF, "ANTHROPIC_IDENTITY_TOKEN_FILE": "/tmp/jwt", "PATH": "/bin"}
    own = {"ANTHROPIC_IDENTITY_TOKEN": "h.p.s"}
    fed.apply(env, own)
    assert env == {"ANTHROPIC_AUTH_TOKEN": "run-secret", "ANTHROPIC_BASE_URL": p.base_url, "PATH": "/bin"}
    assert own == {"ANTHROPIC_AUTH_TOKEN": "run-secret", "ANTHROPIC_BASE_URL": p.base_url}
    p._server.server_close()


def test_scrub_job_removes_job_secrets_but_the_agents_still_reach_the_proxy(upstream, monkeypatch):
    from brindle import ci_client, secrets

    p = CredentialProxy(lambda: WIF, upstream=upstream.url, secret="run-secret")
    fed = ci_federation.Federation(TokenRefresher(IDS, "https://t", "r", fetch=lambda u, t: "j",
                                                  exchange=lambda a, i: (WIF, 600.0)), p)
    env = {"ANTHROPIC_AUTH_TOKEN": WIF, "BRINDLE_PRO_TOKEN": "cpc_x", "GITHUB_TOKEN": "g", "GH_TOKEN": "g",
           "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t", "ACTIONS_ID_TOKEN_REQUEST_URL": "https://t", "PATH": "/bin"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    ci_client.scrub_job(env, fed)
    assert env == {"ANTHROPIC_AUTH_TOKEN": "run-secret", "ANTHROPIC_BASE_URL": p.base_url, "PATH": "/bin"}
    import os
    assert not any(secrets.is_job_secret(k) for k in os.environ)
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "run-secret"
    # the proxy's URL and secret are a Claude agent's sign-in, so a pane scrub leaves them
    keep = secrets.provider_credentials("claude")
    assert {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"} <= keep
    unset = secrets.pane_unset(env, keep=keep, allow=["PATH"])
    assert not {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"} & set(unset)
    p._server.server_close()


def test_start_exchanges_first_and_serves_the_token(monkeypatch, upstream):
    monkeypatch.setattr(ci_federation, "fetch_identity_token", lambda url, tok: "h.p.s")
    monkeypatch.setattr(ci_federation, "exchange_token", lambda a, ids: (WIF, 598.0))
    fed = ci_federation.start({**IDS, **ACTIONS}, upstream=upstream.url)
    try:
        resp, data = call(fed.proxy.base_url, "POST", "/v1/messages", body=b"{}",
                          headers={"Authorization": f"Bearer {fed.proxy.secret}"})
        assert json.loads(data)["auth"] == f"Bearer {WIF}"
        assert fed.agent_env() == {"ANTHROPIC_BASE_URL": fed.proxy.base_url, "ANTHROPIC_AUTH_TOKEN": fed.proxy.secret}
    finally:
        fed.stop()
    with pytest.raises(OSError):
        call(fed.proxy.base_url, "GET", "/", headers={})

    def refused(a, ids):
        raise FederationError("the Anthropic token exchange failed (HTTP 401: Authentication failed)")
    monkeypatch.setattr(ci_federation, "exchange_token", refused)
    with pytest.raises(FederationError, match="HTTP 401"):
        ci_federation.start({**IDS, **ACTIONS}, upstream=upstream.url)
