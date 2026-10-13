"""The backend client (device-flow login, refresh-token rotation, transport
rules) and the ``brindle account`` plugin."""
import io
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from typer.testing import CliRunner

from brindle import plugins
from brindle.cli import app
from brindle.pro import account, auth, credentials, license
from brindle.pro.auth import AuthError, TransportError
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, FakeTransport, backend, claims, pro_env, sign, signing_key, token,
)


class Clock:
    def __init__(self):
        self.t = time.time()
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def __call__(self):
        return self.t


def authorize(interval=5, expires_in=600, **extra):
    return 200, {"device_code": "dev-123", "user_code": "BCDF-GHJK",
                 "verification_uri": "https://pawdelta.com/api/brindle/v1/device/approve",
                 "verification_uri_complete":
                     "https://pawdelta.com/api/brindle/v1/device/approve?user_code=BCDF-GHJK",
                 "expires_in": expires_in, "interval": interval, **extra}


def pending(code="authorization_pending", **extra):
    return 400, {"error": code, **extra}


def device_backend(backend, token_responses, authorize_response=None):
    """``backend`` plus scripted device endpoints; ``"ISSUE"`` in the token
    responses is replaced by a fresh token set from the backend."""
    queue = list(token_responses)

    def device_token(form, headers):
        assert form["grant_type"] == auth.DEVICE_GRANT and form["client_id"] == "brindle-cli"
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(r, Exception):
            raise r
        return (200, backend.issue()) if r == "ISSUE" else r

    backend.routes["POST /device/authorize"] = [authorize_response or authorize()]
    backend.routes["POST /device/token"] = device_token
    return backend


@pytest.fixture
def store(tmp_path):
    return credentials.FileStore(tmp_path / "pro")


def login(transport, store, clock=None):
    clock = clock or Clock()
    shown = []
    ent = auth.login(auth.Client(BASE, transport), store, show=shown.append,
                     sleep=clock.sleep, clock=clock)
    return ent, shown, clock


# -- device flow -----------------------------------------------------------------------


def test_slow_down_uses_the_servers_new_interval(store, backend):
    device_backend(backend, [pending("slow_down", interval=10), pending(),
                             pending("slow_down", interval=15), "ISSUE"],
                   authorize(interval=5))
    _, _, clock = login(backend, store)
    assert clock.sleeps == [5, 10, 10, 15]


def test_expired_token_from_the_server_stops_polling(store, backend):
    device_backend(backend, [pending(), pending("expired_token")])
    with pytest.raises(AuthError) as e:
        login(backend, store)
    assert e.value.code == "expired_token"
    assert store.load() is None


def test_local_deadline_stops_polling(store, backend):
    device_backend(backend, [pending()], authorize(interval=5, expires_in=12))
    with pytest.raises(AuthError) as e:
        login(backend, store)
    assert e.value.code == "expired_token"
    assert backend.paths().count("POST /device/token") == 2


def test_access_denied(store, backend):
    device_backend(backend, [pending("access_denied")])
    with pytest.raises(AuthError, match="denied"):
        login(backend, store)


def test_network_errors_back_off_while_polling(store, backend):
    device_backend(backend, [TransportError("down"), "ISSUE"], authorize(interval=1))
    _, _, clock = login(backend, store)
    assert clock.sleeps == [1, 6]


def test_untrusted_entitlement_is_not_stored_and_the_session_is_revoked(store, backend, signing_key):
    device_backend(backend, ["ISSUE"])
    backend.routes["GET /entitlement"] = [
        (200, {"entitlement": sign(signing_key, claims())[:-6] + "AAAAAA"})]
    with pytest.raises(license.LicenseError):
        login(backend, store)
    assert store.load() is None
    assert "POST /token/revoke" in backend.paths()


def test_non_https_verification_uri_is_refused(store, backend):
    device_backend(backend, [], authorize(verification_uri="http://evil.test/device"))
    with pytest.raises(AuthError, match="https"):
        login(backend, store)


def test_malformed_authorization_response(store):
    t = FakeTransport({"POST /device/authorize": [(200, {"user_code": "X"})]})
    with pytest.raises(AuthError, match="malformed"):
        login(t, store)


# -- refresh-token rotation --------------------------------------------------------------------


def seed(store, backend, *, access_valid=False, entitlement=""):
    t = backend.issue()
    creds = {"access_token": t["access_token"], "refresh_token": t["refresh_token"],
             "access_expires_at": time.time() + (900 if access_valid else -1),
             "entitlement": entitlement, "base_url": BASE}
    store.save(creds)
    return creds


def test_refresh_rotates_and_saves_the_new_pair(store, backend):
    seen = seed(store, backend)
    backend.plan = "enterprise"
    ent = auth.refresh(auth.Client(BASE, backend), store, seen)
    assert ent.plan == "enterprise"
    creds = store.load()
    assert creds["refresh_token"] == "cpr_2" and creds["access_token"] == "at_2"
    assert backend.refresh_tokens == {"cpr_1": "used", "cpr_2": "active"}
    assert backend.calls[0][1] == {"grant_type": "refresh_token", "refresh_token": "cpr_1",
                                   "client_id": "brindle-cli"}


def test_refresh_without_rotation_is_refused(store, backend):
    seen = seed(store, backend)
    backend.routes["POST /token/refresh"] = [(200, {"access_token": "at_x", "refresh_token": "cpr_1"})]
    with pytest.raises(AuthError, match="rotate"):
        auth.refresh(auth.Client(BASE, backend), store, seen)
    assert store.load()["refresh_token"] == "cpr_1"


def test_refresh_missing_new_refresh_token_is_refused(store, backend):
    seen = seed(store, backend)
    backend.routes["POST /token/refresh"] = [(200, {"access_token": "at_x"})]
    with pytest.raises(AuthError, match="missing"):
        auth.refresh(auth.Client(BASE, backend), store, seen)


def test_reused_refresh_token_logs_out(store, backend):
    seen = seed(store, backend)
    backend.refresh_tokens["cpr_1"] = "used"    # someone else already spent it
    with pytest.raises(AuthError) as e:
        auth.refresh(auth.Client(BASE, backend), store, seen)
    assert e.value.revoked and backend.revoked
    assert store.load() is None


def test_401_on_a_fresh_looking_access_token_rotates_once(store, backend):
    seen = seed(store, backend, access_valid=True)
    backend.access_tokens.clear()     # e.g. the server restarted its signing key
    ent = auth.refresh(auth.Client(BASE, backend), store, seen)
    assert ent.plan == "pro"
    assert backend.paths() == ["GET /entitlement", "POST /token/refresh", "GET /entitlement"]


def test_two_concurrent_refreshers_spend_the_refresh_token_once(tmp_path, backend):
    """Two processes (here: threads with their own store objects and lock file
    descriptors) find the same expired credentials and refresh at once. The
    second must wait for the lock, re-read, and use what the first rotated;
    otherwise the backend sees reuse and revokes the session."""
    backend.delay = 0.2
    seen = seed(credentials.FileStore(tmp_path / "pro"), backend)
    results, errors = [], []
    barrier = threading.Barrier(2)

    def worker():
        store = credentials.FileStore(tmp_path / "pro")
        client = auth.Client(BASE, backend)
        barrier.wait()
        try:
            results.append(auth.refresh(client, store, dict(seen)))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == [] and len(results) == 2
    assert backend.paths().count("POST /token/refresh") == 1
    assert not backend.revoked
    assert credentials.FileStore(tmp_path / "pro").load()["refresh_token"] == "cpr_2"


def test_without_the_lock_concurrent_refreshers_would_trip_reuse_detection(
        tmp_path, backend, monkeypatch):
    """The control for the test above: the fake backend really does punish reuse."""
    from contextlib import nullcontext

    monkeypatch.setattr(auth, "refresh_lock", lambda *a, **k: nullcontext())
    backend.delay = 0.2
    seen = seed(credentials.FileStore(tmp_path / "pro"), backend)
    barrier = threading.Barrier(2)
    errors = []

    def worker():
        barrier.wait()
        try:
            auth.refresh(auth.Client(BASE, backend), credentials.FileStore(tmp_path / "pro"), dict(seen))
        except AuthError as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert backend.revoked and errors


def test_refresh_lock_times_out(tmp_path):
    with auth.refresh_lock():
        t0 = time.monotonic()
        result = []

        def other():
            try:
                with auth.refresh_lock(timeout=0.2):
                    result.append("got it")
            except AuthError as e:
                result.append(e.code)

        th = threading.Thread(target=other)
        th.start()
        th.join(5)
    assert result == ["lock_timeout"] and time.monotonic() - t0 >= 0.2
    assert oct(auth.lock_path().stat().st_mode & 0o777) == "0o600"


def test_server_errors_are_sanitized(store, backend):
    seen = seed(store, backend)
    backend.routes["POST /token/refresh"] = [(500, {"error": "Server\nError<script>",
                                                    "error_description": "x\x1b[31m" + "y" * 500})]
    with pytest.raises(AuthError) as e:
        auth.refresh(auth.Client(BASE, backend), store, seen)
    assert e.value.code == "servererrorscript"
    assert "\x1b" not in str(e.value) and len(str(e.value)) < 300


def test_authed_calls_refresh_an_expired_access_token_first(store, backend):
    seed(store, backend)
    info = auth.me(auth.Client(BASE, backend), store)
    assert info["email"] == "dev@example.test"
    assert backend.paths() == ["POST /token/refresh", "GET /entitlement", "GET /me"]


def test_authed_calls_retry_once_after_401(store, backend):
    seed(store, backend, access_valid=True)
    backend.access_tokens.discard("at_1")
    assert auth.portal_url(auth.Client(BASE, backend), store) == "https://billing.stripe.test/p/xyz"
    assert backend.paths() == ["POST /billing/portal", "POST /token/refresh", "GET /entitlement",
                               "POST /billing/portal"]


# -- URL and transport rules ----------------------------------------------------------------------


def test_bad_urls_are_rejected():
    with pytest.raises(AuthError):
        auth.check_url("file:///etc/passwd")


def test_tls_verification_is_always_on():
    t = auth.UrllibTransport()
    https = [h for h in t.opener.handlers if h.__class__.__name__ == "HTTPSHandler"][0]
    ctx = https._context
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert not any(h.__class__.__name__ in ("FileHandler", "FTPHandler", "DataHandler")
                   for h in t.opener.handlers)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("BRINDLE_PRO_DEV", "1")
    routes = {}

    class H(BaseHTTPRequestHandler):
        def _serve(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            status, headers, body = routes[(self.command, self.path)]
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _serve

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield routes, srv.server_address[1]
    srv.shutdown()


def post(url):
    return auth.UrllibTransport().request("POST", url, {}, {})


def test_response_size_is_capped(server):
    routes, port = server
    routes[("POST", "/big")] = (200, {}, json.dumps({"x": "y" * 5000}).encode())
    with pytest.raises(TransportError, match="too large"):
        auth.UrllibTransport(max_bytes=1024).request("POST", f"http://127.0.0.1:{port}/big", {}, {})


def test_redirect_to_another_host_is_refused(server):
    routes, port = server
    routes[("POST", "/r")] = (307, {"Location": f"http://localhost:{port}/ok"}, b"")
    with pytest.raises(TransportError, match="redirect"):
        post(f"http://127.0.0.1:{port}/r")


def test_redirect_to_the_same_origin_is_followed(server):
    routes, port = server
    routes[("POST", "/r")] = (307, {"Location": "/ok"}, b"")
    routes[("POST", "/ok")] = (200, {}, b'{"ok": true}')
    assert post(f"http://127.0.0.1:{port}/r") == (200, {"ok": True})


def test_method_changing_redirects_are_refused(server):
    routes, port = server
    routes[("POST", "/r")] = (302, {"Location": "/ok"}, b"")
    with pytest.raises(TransportError, match="302"):
        post(f"http://127.0.0.1:{port}/r")


def test_non_json_response(server):
    routes, port = server
    routes[("POST", "/html")] = (502, {}, b"<html>bad gateway</html>")
    with pytest.raises(TransportError, match="non-JSON"):
        post(f"http://127.0.0.1:{port}/html")


# -- the account plugin ---------------------------------------------------------------------------


def run(args, **kw):
    out, err = io.StringIO(), io.StringIO()
    acct = account.ProAccount("/repo", out=out, err=err, **kw)
    return acct.run(args), out.getvalue(), err.getvalue()


def test_account_login_status_upgrade_portal_logout(store, backend, monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    device_backend(backend, ["ISSUE"])
    code, out, _ = run(["login"], store=store, transport=backend)
    assert code == 0 and "BCDF-GHJK" in out and "plan pro" in out
    code, out, _ = run(["status"], store=store, transport=backend)
    assert code == 0 and "active" in out and "dev@example.test" in out
    assert "autopilot, learning" in out
    assert "at_1" not in out and "cpr_1" not in out
    code, out, _ = run(["upgrade"], store=store, transport=backend)
    assert code == 0 and out.strip() == "https://checkout.stripe.test/c/abc"
    assert backend.calls[-1][0] == "POST /billing/checkout"
    assert backend.calls[-1][2] == {"Authorization": "Bearer at_1"}
    code, out, _ = run(["portal"], store=store, transport=backend)
    assert code == 0 and out.strip() == "https://billing.stripe.test/p/xyz"
    code, out, _ = run(["logout"], store=store, transport=backend)
    assert code == 0 and store.load() is None
    assert backend.calls[-1][0] == "POST /token/revoke" and backend.calls[-1][1]["token"] == "cpr_1"
    code, out, _ = run(["status"], store=store, transport=backend)
    assert code == 1 and "not logged in" in out




def test_login_after_reauth_keeps_team_org_and_org_use_applies_setup(store, backend, monkeypatch):
    from types import SimpleNamespace

    from brindle import cloud_credential, company_login

    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    device_backend(backend, ["ISSUE"])
    switched, applied = [], []
    team = SimpleNamespace(org_id="acme", plan="team", features=[], sub="u1", role="member")
    monkeypatch.setattr(auth, "switch_org", lambda client, st, org: switched.append(org) or team)
    monkeypatch.setattr(company_login, "apply_current", lambda *a, **k: applied.append(1))
    members = {"acme"}
    monkeypatch.setattr(auth, "member_orgs", lambda client, st: members)
    backend.routes["GET /me"] = lambda form, headers: (200, {"sub": "u1", "email": "dev@example.test",
                                                              "name": None})
    cloud_credential.note_reauth("acme")
    code, out, _ = run(["login"], store=store, transport=backend)
    assert code == 0 and switched == ["acme"] and "(acme)" in out and applied == [1]
    assert "Logged in as dev@example.test" in out and "None" not in out  # a null name shows the email
    assert cloud_credential.take_reauth() is None                       # the note is used up
    assert run(["org", "use", "acme"], store=store, transport=backend)[0] == 0
    assert switched == ["acme", "acme"] and applied == [1, 1]           # org use applies the org's setup

    members.clear()                                                     # left the org since: stay put
    cloud_credential.note_reauth("acme")
    device_backend(backend, ["ISSUE"])
    run(["login"], store=store, transport=backend)
    assert switched == ["acme", "acme"] and cloud_credential.take_reauth() is None

    cloud_credential.note_reauth("acme")                                # a deliberate logout drops the note
    run(["logout"], store=store, transport=backend)
    assert cloud_credential.take_reauth() is None


def test_account_rejects_a_non_https_billing_url(store, backend):
    seed(store, backend, access_valid=True)
    backend.routes["POST /billing/checkout"] = [(200, {"url": "http://evil.test/pay", "id": "x"})]
    code, _, err = run(["upgrade"], store=store, transport=backend)
    assert code == 1 and "billing URL" in err


def test_account_usage(store):
    assert run(["features", "x"], store=store)[0] == 2
    assert run(["frobnicate"], store=store)[0] == 2
    assert run(["--help"], store=store)[0] == 0
    code, _, err = run(["login", "--base-url", "http://evil.test"], store=store)
    assert code == 1 and "https" in err


# -- `brindle account` out of the box ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_plugins():
    plugins.reset()
    yield
    plugins.reset()


def test_brindle_account_can_still_be_turned_off(repo, monkeypatch):
    from brindle import account as account_mod

    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text('{"plugins": {"account": "off"}}')
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["account", "status"])
    assert res.exit_code == 0, res.output
    assert account_mod.NOT_INSTALLED in res.output
