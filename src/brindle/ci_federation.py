"""Anthropic workload identity federation for a brindle CI run job
(:mod:`brindle.ci_client`): the run keeps its own short-lived Anthropic
token fresh and lends it to every Claude Code process it starts.

The problem this solves: a federated token is minted from a GitHub Actions
OIDC token, and lives at most twice as long as what remains of that token
(about ten minutes), whatever the federation rule's lifetime says. The
workflow's one exchange gives the job ``ANTHROPIC_AUTH_TOKEN``, which Claude
Code reads once at start, so a run longer than that loses its access.

How it works here:

* :class:`TokenRefresher` fetches a fresh GitHub OIDC token (audience
  ``https://api.anthropic.com``; GitHub's tokens carry a ``jti`` and can be
  exchanged once each) and exchanges it at ``POST /v1/oauth/token`` (the
  ``jwt-bearer`` grant of the WIF reference), again at 70% of each token's
  ``expires_in``. The token lives in this process's memory only.
* :class:`CredentialProxy` listens on the loopback interface. The agents get
  ``ANTHROPIC_BASE_URL`` pointing at it and, as ``ANTHROPIC_AUTH_TOKEN``, a
  random secret made for this run (Claude Code sends it as a bearer token).
  The proxy checks that secret, replaces it with the current Anthropic token
  and forwards the request to ``https://api.anthropic.com``, streaming the
  answer back as it arrives. The agents never hold the Anthropic token, and
  nothing on the runner without the run's secret can use the proxy.

Why not Claude Code's own credential helper (``apiKeyHelper``): its output
is sent as both ``X-Api-Key`` and ``Authorization: Bearer``, and the API
judges ``X-Api-Key`` first, which rejects an OAuth token. Why not Claude
Code's own federation mode (``ANTHROPIC_IDENTITY_TOKEN_FILE``): each of the
run's Claude Code processes would exchange the same single-use GitHub token.

Nothing here logs a token, a header or a body. Without the federation IDs
and the Actions token endpoint, or when the first exchange fails, the run
goes on with whatever credential the workflow gave it.
"""

from __future__ import annotations

import hmac
import http.client
import http.server
import json
import logging
import secrets
import threading
import time
import urllib.parse
from typing import Callable, Mapping, MutableMapping

log = logging.getLogger(__name__)

UPSTREAM = "https://api.anthropic.com"
EXCHANGE_URL = UPSTREAM + "/v1/oauth/token"
AUDIENCE = "https://api.anthropic.com"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
REQUIRED_VARS = ("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_SERVICE_ACCOUNT_ID")
WORKSPACE_VAR = "ANTHROPIC_WORKSPACE_ID"
ACTIONS_URL = "ACTIONS_ID_TOKEN_REQUEST_URL"
ACTIONS_TOKEN = "ACTIONS_ID_TOKEN_REQUEST_TOKEN"
DISABLE_VAR = "BRINDLE_CI_FEDERATION_REFRESH"   # "0": keep the workflow's single exchange
# What the proxy's clients are told, and what they must not keep.
BASE_URL_VAR = "ANTHROPIC_BASE_URL"
AUTH_TOKEN_VAR = "ANTHROPIC_AUTH_TOKEN"
IDENTITY_VARS = ("ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE")
REFRESH_FRACTION = 0.7
MIN_REFRESH_S = 30.0
RETRY_DELAYS = (5.0, 15.0, 45.0)   # after a failed refresh, while the old token still works
RETRY_EVERY_S = 60.0
EXCHANGE_TIMEOUT = 30.0
UPSTREAM_TIMEOUT = 600.0           # a long reply streams for minutes
IDLE_TIMEOUT = 60.0                # a client that stops sending (a declared body that never comes)
MAX_REQUEST_BYTES = 32 * 1024 * 1024
CHUNK = 64 * 1024
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
                        "trailer", "transfer-encoding", "upgrade"})
# Not forwarded upstream: the hop-by-hop ones, the client's credential (the
# run's secret), and what the proxy sets itself.
NOT_FORWARDED = HOP_BY_HOP | {"host", "authorization", "x-api-key", "content-length"}
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


class FederationError(Exception):
    """A failed token fetch or exchange. Never carries a token."""


def configured(env: Mapping[str, str]) -> bool:
    """Whether this job can run its own refresher: the federation IDs and
    the Actions token endpoint are set, no API key is (a key would win in
    Claude Code anyway), no other base URL is, and it isn't turned off."""
    if env.get(DISABLE_VAR, "").strip() == "0":
        return False
    if env.get("ANTHROPIC_API_KEY") or env.get(BASE_URL_VAR):
        return False
    return all(env.get(k) for k in (*REQUIRED_VARS, ACTIONS_URL, ACTIONS_TOKEN))


# -- the exchange ------------------------------------------------------------------------------


def fetch_identity_token(url: str, request_token: str) -> str:
    """A fresh GitHub Actions OIDC token for Anthropic's audience."""
    from brindle import ci_client

    try:
        return ci_client._fetch_oidc(url, request_token, audience=AUDIENCE)
    except ci_client.CIError as e:
        raise FederationError(str(e)) from None


def exchange_token(assertion: str, ids: Mapping[str, str], *, url: str = EXCHANGE_URL,
                   timeout: float = EXCHANGE_TIMEOUT) -> tuple[str, float]:
    """``(access_token, expires_in)`` for ``assertion`` (the identity JWT)
    from Anthropic's token endpoint, per the WIF reference. The error of a
    refused exchange names the status and the API's message (never a token)."""
    import urllib.error
    import urllib.request

    if urllib.parse.urlsplit(url).scheme != "https":
        raise FederationError("the token exchange URL isn't https")
    body = {"grant_type": GRANT_TYPE, "assertion": assertion,
            "federation_rule_id": ids["ANTHROPIC_FEDERATION_RULE_ID"],
            "organization_id": ids["ANTHROPIC_ORGANIZATION_ID"],
            "service_account_id": ids["ANTHROPIC_SERVICE_ACCOUNT_ID"]}
    if ids.get(WORKSPACE_VAR):
        body["workspace_id"] = ids[WORKSPACE_VAR]
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(64 * 1024)
    except urllib.error.HTTPError as e:
        raise FederationError(f"the Anthropic token exchange failed (HTTP {e.code}: "
                              f"{_api_message(e.read(16 * 1024))})") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        why = type(getattr(e, "reason", None) or e).__name__
        raise FederationError(f"the Anthropic token exchange failed ({why})") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        data = None
    token = data.get("access_token") if isinstance(data, dict) else None
    expires = data.get("expires_in") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token.strip() or isinstance(expires, bool) \
            or not isinstance(expires, (int, float)) or expires <= 0:
        raise FederationError("the Anthropic token exchange answer held no token")
    return token.strip(), float(expires)


def _api_message(raw: bytes) -> str:
    """The ``error.message`` of an API error body, printable, short."""
    import re

    try:
        msg = json.loads(raw.decode("utf-8")).get("error", {}).get("message")
    except (ValueError, UnicodeDecodeError, AttributeError):
        msg = None
    text = msg if isinstance(msg, str) and msg else "no message"
    return re.sub(r"[^\x20-\x7e]", "?", text)[:120]


class TokenRefresher:
    """The current Anthropic token, exchanged again before it expires.
    ``start`` does the first exchange in the caller's thread (so a broken
    setup fails there) and then refreshes in a daemon thread. A failed
    refresh is retried while the old token is still good; ``token()`` is
    None once it isn't."""

    def __init__(self, ids: Mapping[str, str], oidc_url: str, request_token: str, *,
                 fetch: Callable[[str, str], str] | None = None,
                 exchange: Callable[..., tuple[str, float]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ids = {k: ids[k] for k in (*REQUIRED_VARS, WORKSPACE_VAR) if ids.get(k)}
        self._url, self._request_token = oidc_url, request_token
        self._fetch = fetch or fetch_identity_token
        self._exchange = exchange or exchange_token
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def token(self) -> str | None:
        with self._lock:
            return self._token if self._token and self._clock() < self._expires_at else None

    def exchange_now(self) -> float:
        """Fetch a fresh identity token and exchange it; the new token's
        ``expires_in``. Raises :class:`FederationError`."""
        assertion = self._fetch(self._url, self._request_token)
        token, expires_in = self._exchange(assertion, self._ids)
        with self._lock:
            self._token, self._expires_at = token, self._clock() + expires_in
        return expires_in

    def tick(self) -> float:
        """One refresh attempt; how long to wait before the next. Any
        failure is a failed refresh (the thread must not die): a
        :class:`FederationError` is logged as is, anything else by its
        class name only, since its text could quote a response."""
        try:
            expires_in = self.exchange_now()
        except Exception as e:   # noqa: BLE001 - see the docstring
            self._failures += 1
            why = str(e) if isinstance(e, FederationError) else f"unexpected {type(e).__name__}"
            log.warning("brindle ci: refreshing the Anthropic token failed (%s); retrying", why)
            n = self._failures
            return RETRY_DELAYS[n - 1] if n <= len(RETRY_DELAYS) else RETRY_EVERY_S
        self._failures = 0
        return max(MIN_REFRESH_S, expires_in * REFRESH_FRACTION)

    def start(self) -> None:
        delay = max(MIN_REFRESH_S, self.exchange_now() * REFRESH_FRACTION)
        self._thread = threading.Thread(target=self._loop, args=(delay,), name="brindle-ci-token", daemon=True)
        self._thread.start()

    def _loop(self, delay: float) -> None:
        while not self._stop.wait(delay):
            delay = self.tick()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            self._token = None


# -- the proxy -----------------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    proxy: "CredentialProxy"
    timeout = IDLE_TIMEOUT   # the socket's: a read that waits this long ends the request

    def log_message(self, format, *args) -> None:   # noqa: A002 - the base class's name
        """Nothing: a request line or an error could quote a header."""

    def _handle(self) -> None:
        self.close_connection = True
        # Only a path on the fixed upstream: no absolute URLs, no CONNECT.
        if not self.path.startswith("/") or self.command not in METHODS:
            self._error(400, "invalid_request_error", "not a request for the Anthropic API")
            return
        if not self.proxy.authorized(self.headers.get("Authorization") or ""):
            self._error(401, "authentication_error", "this credential isn't this run's")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if self.headers.get("Transfer-Encoding"):
            self._error(411, "invalid_request_error", "the proxy needs a Content-Length")
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._error(413, "invalid_request_error", "request body too large for the proxy")
            return
        token = self.proxy.token()
        if token is None:
            self._error(503, "api_error", "the run's Anthropic token isn't available right now")
            return
        body = self.rfile.read(length) if length else b""
        headers: dict[str, str] = {}
        for k, v in self.headers.items():
            name = k.lower()
            if name not in NOT_FORWARDED:   # a repeated header is one list, not the last value
                headers[name] = f"{headers[name]}, {v}" if name in headers else v
        headers["Authorization"] = f"Bearer {token}"
        if body:
            headers["Content-Length"] = str(len(body))
        conn = self.proxy.connection()
        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            self._error(502, "api_error", f"couldn't reach the Anthropic API ({type(e).__name__})")
            return
        try:
            self._relay(resp)
        except OSError:
            pass   # the client went away mid-reply
        finally:
            conn.close()

    def _relay(self, resp: http.client.HTTPResponse) -> None:
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in HOP_BY_HOP and k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("Connection", "close")
        if self.command == "HEAD" or resp.status in (204, 304):
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while True:
            chunk = resp.read1(CHUNK)   # what has arrived, without waiting for more
            if not chunk:
                break
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _error(self, status: int, kind: str, message: str) -> None:
        body = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


for _m in METHODS:
    setattr(_Handler, f"do_{_m}", _Handler._handle)


class CredentialProxy:
    """A loopback HTTP server that puts the run's Anthropic token on requests
    bearing the run's secret and forwards them to ``upstream`` (the
    Anthropic API; a test may name a local server). Not an open proxy: one
    fixed upstream, paths only."""

    def __init__(self, token: Callable[[], str | None], *, upstream: str = UPSTREAM,
                 secret: str | None = None, idle_timeout: float = IDLE_TIMEOUT) -> None:
        self.token = token
        self.secret = secret or secrets.token_urlsafe(32)
        self._upstream = urllib.parse.urlsplit(upstream)
        if self._upstream.scheme not in ("http", "https") or not self._upstream.hostname:
            raise ValueError("the proxy's upstream must be an http(s) URL")
        handler = type("Handler", (_Handler,), {"proxy": self, "timeout": idle_timeout})
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def authorized(self, authorization: str) -> bool:
        return hmac.compare_digest(authorization.encode(), f"Bearer {self.secret}".encode())

    def connection(self) -> http.client.HTTPConnection:
        u = self._upstream
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        return cls(u.hostname, u.port, timeout=UPSTREAM_TIMEOUT)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, name="brindle-ci-proxy", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


# -- the run's federation ------------------------------------------------------------------------


class Federation:
    """A running refresher and proxy, and what the agents are told."""

    def __init__(self, refresher: TokenRefresher, proxy: CredentialProxy) -> None:
        self.refresher, self.proxy = refresher, proxy

    def agent_env(self) -> dict[str, str]:
        return {BASE_URL_VAR: self.proxy.base_url, AUTH_TOKEN_VAR: self.proxy.secret}

    def apply(self, *envs: MutableMapping[str, str]) -> None:
        """Point ``envs`` (the run's, and the process's own, which the agents
        inherit) at the proxy, and drop any identity token they carry."""
        for env in envs:
            env.update(self.agent_env())
            for k in IDENTITY_VARS:
                env.pop(k, None)

    def stop(self) -> None:
        self.proxy.stop()
        self.refresher.stop()


def start(env: Mapping[str, str], *, upstream: str = UPSTREAM) -> Federation:
    """Exchange once now and start refreshing and proxying. Read ``env``
    before the job's secrets are scrubbed (it needs the Actions token
    endpoint). Raises :class:`FederationError` when the first exchange fails."""
    refresher = TokenRefresher(env, env[ACTIONS_URL], env[ACTIONS_TOKEN])
    refresher.start()
    proxy = CredentialProxy(refresher.token, upstream=upstream)
    try:
        proxy.start()
    except Exception:
        refresher.stop()
        raise
    return Federation(refresher, proxy)
