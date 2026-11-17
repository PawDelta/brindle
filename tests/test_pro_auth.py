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


def login(transport, store, clock=None, watch=None):
    clock = clock or Clock()
    shown = []
    ent = auth.login(auth.Client(BASE, transport), store, show=shown.append, watch=watch,
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


@pytest.mark.parametrize("saved_elsewhere", [
    pytest.param(False, marks=pytest.mark.variant("security: a reused refresh token with no replacement must clear credentials")),
    True,
])
def test_reused_refresh_token_logs_out(store, backend, saved_elsewhere):
    """A refused refresh token signs out, unless the brindle that spent it
    first saved its new pair while this request was out: that is the sign-in."""
    seen = seed(store, backend)
    backend.refresh_tokens["cpr_1"] = "used"    # someone else already spent it
    if saved_elsewhere:
        def refused_after_the_other_saved(form, headers):
            other = backend.issue()
            store.save({**seen, "access_token": other["access_token"],
                        "refresh_token": other["refresh_token"], "access_expires_at": time.time() + 900})
            return 400, {"error": "invalid_grant"}

        backend.routes["POST /token/refresh"] = refused_after_the_other_saved
        assert auth.refresh(auth.Client(BASE, backend), store, seen).plan == "pro"
        assert store.load()["refresh_token"] == "cpr_2"
        return
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


@pytest.mark.variant("race: competing refreshers must reread the rotated credentials instead of revoking the session")
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


@pytest.mark.variant("the reported incident: the app's engine and a CLI with different BRINDLE_HOMEs refreshing the one keychain sign-in at once")
def test_refreshers_with_different_homes_spend_the_keychain_sign_in_once(tmp_path, backend, monkeypatch):
    """The keychain holds one sign-in per OS user, whatever BRINDLE_HOME a
    process has (the app's engine, a CLI or worker with a home of its own), so
    they share one refresh lock. A spends the token and holds the request
    until B has either hit A's lock or spent the same token itself."""
    import fcntl
    from subprocess import CompletedProcess
    from brindle import config, keystore

    external = credentials.FileStore(tmp_path / "keychain")

    def run(self, args, stdin=None):
        if args[0] == "-i":
            external.save(keystore._decode(stdin.split('"')[-2]))
        record = external.load()
        return CompletedProcess(args, 0 if record else 44, keystore._encode(record) if record else "", "")

    monkeypatch.setenv(keystore.FORCE_ENV, "keychain")
    monkeypatch.setattr(keystore, "_keychain_reads", {})
    monkeypatch.setattr(keystore.KeychainStore, "_run", run)
    homes, real_home = threading.local(), config.brindle_home
    monkeypatch.setattr(config, "brindle_home", lambda: getattr(homes, "path", None) or real_home())
    seen = seed(external, backend)
    spent, a_spending, b_moved = [], threading.Event(), threading.Event()
    real_refresh, real_flock = backend.routes["POST /token/refresh"], fcntl.flock

    def refresh_route(form, headers):
        spent.append(form["refresh_token"])
        if len(spent) == 1:
            a_spending.set()
            b_moved.wait(5)
        else:
            b_moved.set()
        return real_refresh(form, headers)

    def flock(fd, op):
        try:
            return real_flock(fd, op)
        except BlockingIOError:
            b_moved.set()        # B is waiting on A's lock
            raise

    backend.routes["POST /token/refresh"] = refresh_route
    monkeypatch.setattr(fcntl, "flock", flock)
    results, errors = {}, []

    def worker(home):
        homes.path = tmp_path / home
        try:
            results[home] = auth.refresh(auth.Client(BASE, backend), credentials.default_store(), dict(seen))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    a = threading.Thread(target=worker, args=("home-a",))
    a.start()
    a_spending.wait(5)
    b = threading.Thread(target=worker, args=("home-b",))
    b.start()
    a.join(10)
    b.join(10)
    assert errors == [] and set(results) == {"home-a", "home-b"}
    assert spent == ["cpr_1"] and not backend.revoked
    assert external.load()["refresh_token"] == "cpr_2"


class FakeCallbackServer:
    """``loopback.CallbackServer`` without a socket: the browser's redirect is
    ``answer`` (None: none comes until the sign-in is finished from outside)."""

    answer = None
    redirect_uri = "http://127.0.0.1:49152/callback"

    def __init__(self, state):
        self.result, self.answered = None, threading.Event()

    def __enter__(self):
        if self.answer is not None:
            self.finish(self.answer)
        return self

    def __exit__(self, *exc):
        pass

    def finish(self, result):
        if not self.answered.is_set():
            self.result = result
            self.answered.set()

    def wait(self, limit):
        self.answered.wait(limit)
        return self.result


def _signing_in(db, store, backend, monkeypatch, on_progress=lambda fields: None, answer=None):
    """The daemon's real sign-in route against the fake backend: ``login(**body)``
    posts it; the jobs it starts are held (run one with ``held[i]()``);
    ``on_progress`` sees each job.progress published (after it is); the
    browser's redirect is ``answer`` (see FakeCallbackServer)."""
    from brindle import bus
    from brindle.daemon import routes_actions
    from brindle.pro import loopback

    held, real_publish = [], bus.publish

    def publish(db, kind, **fields):
        got = real_publish(db, kind, **fields)
        if kind == "job.progress":
            on_progress(fields)
        return got

    monkeypatch.setattr(loopback, "CallbackServer", type("Server", (FakeCallbackServer,), {"answer": answer}))
    monkeypatch.setattr(bus, "publish", publish)
    monkeypatch.setattr(routes_actions, "_spawn", held.append)
    monkeypatch.setattr(routes_actions, "_SIGN_IN", [])
    monkeypatch.setattr(routes_actions, "_pro_account",
                        lambda out=None: account.ProAccount(store=store, transport=backend, out=out))
    return (lambda **body: routes_actions.account_login(db, {}, body)), held


def test_a_second_sign_in_request_joins_the_one_waiting_on_the_browser(db, store, backend, monkeypatch):
    """One click that reaches the daemon twice (a remounted sign-in screen,
    another screen's button) starts one browser flow; Try again (restart)
    ends the waiting one, whose listener stops, and starts a fresh one."""
    from brindle import bus

    urls = threading.Event()
    login, held = _signing_in(db, store, backend, monkeypatch,
                              on_progress=lambda fields: "url" in fields and urls.set())
    first = login()
    flow = threading.Thread(target=held[0], daemon=True)
    flow.start()
    urls.wait(5)
    since = bus.tail(db)[-1]["id"]
    assert login() == first and len(held) == 1
    again = [e["payload"] for e in bus.tail(db, since) if "url" in e["payload"]]
    assert [(p["job"], p["url"].startswith(BASE + "/cli/authorize")) for p in again] == [(first.id, True)]
    fresh = login(restart=True)
    flow.join(5)
    assert fresh != first and len(held) == 2 and not flow.is_alive()
    ended = [e["payload"] for e in bus.tail(db, since, kinds=("job.failed",))]
    assert [p["job"] for p in ended] == [first.id]
    assert "POST /cli/token" not in backend.paths()


@pytest.mark.parametrize("stage", ["before listening", "before the URL",
                                   pytest.param("code exchange", marks=pytest.mark.variant("a race: the sign-in is replaced while its code is being exchanged"))])
def test_a_replaced_browser_sign_in_shows_and_stores_nothing(db, store, backend, monkeypatch, stage):
    """Try again replaces a browser sign-in at any stage: replaced before it
    listens, or while its first progress line is being published, it never
    publishes a URL to open; replaced while its code is exchanged, the tokens
    it got are revoked, not saved over the new sign-in."""
    from brindle import bus
    from brindle.pro import loopback

    replaced = []

    def on_progress(fields):     # the line before the URL: "Opening your browser…"
        if stage == "before the URL" and "text" in fields and not replaced:
            replaced.append(login(restart=True))

    login, held = _signing_in(db, store, backend, monkeypatch, on_progress=on_progress,
                              answer=loopback.CallbackResult(code="cac_" + "g" * 43))
    first = login()
    if stage == "before listening":
        login(restart=True)
    elif stage == "code exchange":
        def exchange(form, headers):
            login(restart=True)
            return 200, backend.issue()

        backend.routes["POST /cli/token"] = exchange
    held[0]()
    mine = [e for e in bus.tail(db) if e["payload"].get("job") == first.id]
    assert [e["kind"] for e in mine if e["kind"] != "job.progress"] == ["job.failed"]
    assert any("url" in e["payload"] for e in mine) == (stage == "code exchange")
    assert store.load() is None and "POST /device/authorize" not in backend.paths()
    assert ("POST /token/revoke" in backend.paths()) == (stage == "code exchange")


@pytest.mark.parametrize("approved", [False, pytest.param(True, marks=pytest.mark.variant("a race: the device code is approved just as the sign-in is replaced"))])
def test_a_replaced_device_sign_in_stops_polling_and_stores_nothing(store, backend, approved):
    """Replaced while its code is polled (the daemon's watch): a pending code
    is not polled again; one approved anyway has its tokens revoked, unsaved."""
    from brindle.daemon import routes_actions

    watch = routes_actions._SignIn(routes_actions.Job("j1", "account.login"), True)

    def device_token(form, headers):
        watch.end()       # Try again, while this poll is out
        return (200, backend.issue()) if approved else pending()

    device_backend(backend, [])
    backend.routes["POST /device/token"] = device_token
    with pytest.raises(AuthError) as e:
        login(backend, store, watch=watch)
    assert e.value.code == auth.RESTARTED and store.load() is None
    assert backend.paths().count("POST /device/token") == 1
    assert ("POST /token/revoke" in backend.paths()) == approved


def test_joining_a_device_sign_in_tells_its_code_again(db, store, backend, monkeypatch):
    """A screen that joins a device sign-in already showing its code (after
    navigating away and back) is told the code again, by the same job."""
    from brindle import bus

    class Joined(Exception):
        pass

    joined, joining = [], []
    real_publish = bus.publish

    def publish(db, kind, **fields):
        got = real_publish(db, kind, **fields)
        if "BCDF-GHJK" in str(fields.get("text")) and not joining:     # the code is on screen
            joining.append(got)
            joined.append((login(device=True), [e["payload"] for e in bus.tail(db, got)]))
            raise Joined     # end the job here: what's checked is what the second request got
        return got

    login, held = _signing_in(db, store, backend, monkeypatch)
    monkeypatch.setattr(bus, "publish", publish)
    device_backend(backend, [])
    first = login(device=True)
    held[0]()
    (job, told), = joined
    assert job == first and len(held) == 1
    assert {p["job"] for p in told} == {first.id} and not any("url" in p for p in told)
    assert any("BCDF-GHJK" in p["text"] for p in told)


@pytest.mark.variant("credentials rotate between routing's initial read and refresh lock entry")
@pytest.mark.parametrize("entry", ["routing", "captured"])
def test_routing_refresh_observes_rotation_at_lock_entry(backend, monkeypatch, entry):
    from contextlib import contextmanager
    from subprocess import CompletedProcess
    from brindle import keystore

    external = credentials.default_store()

    def run(self, args, stdin=None):
        if args[0] == "-i":
            external.save(keystore._decode(stdin.split('"')[-2]))
        elif args[0] == "delete-generic-password":
            external.delete()
        record = external.load()
        return CompletedProcess(args, 0 if record else 44, keystore._encode(record) if record else "", "")

    monkeypatch.setenv(keystore.FORCE_ENV, "keychain")
    monkeypatch.setattr(keystore, "_keychain_reads", {})
    monkeypatch.setattr(keystore.KeychainStore, "_run", run)
    store = credentials.default_store()
    seen = seed(external, backend)
    assert store.load() == seen  # prime this process's cache before external rotation
    client = auth.Client(BASE, backend)
    real_lock = auth.refresh_lock
    rotated = False

    @contextmanager
    def lock():
        nonlocal rotated
        if not rotated:
            rotated = True
            auth.refresh(client, external, seen)
        with real_lock():
            yield

    monkeypatch.setattr(auth, "refresh_lock", lock)
    monkeypatch.setattr(auth, "Client", lambda *a, **k: client)
    if entry == "routing":
        with license.reading():
            assert license.current().plan == "pro"
    else:
        with pytest.raises(AuthError) as exc:
            auth.refresh_captured(store, seen, backend)
        assert exc.value.code == "record_changed"
    assert backend.paths().count("POST /token/refresh") == 1
    assert not backend.revoked
    assert store.load()["refresh_token"] == "cpr_2"


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


@pytest.mark.variant("credential recovery: an unauthorized API call refreshes the token and repeats the original request")
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


@pytest.mark.variant("security: transport must verify hostnames and certificates and disable non-HTTP handlers")
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


def test_binary_response_is_returned_without_decoding(server):
    routes, port = server
    raw = b"PK\x03\x04\x00\xff"
    routes[("GET", "/binary")] = (200, {"Content-Type": "application/vnd.test+octet-stream"}, raw)
    assert auth.UrllibTransport().request("GET", f"http://127.0.0.1:{port}/binary", None, {}) == (
        200, {auth.BINARY_KEY: raw, auth.CONTENT_TYPE_KEY: "application/vnd.test+octet-stream"})


@pytest.mark.parametrize("status,location,body,error", [
    (502, None, b"<html>bad gateway</html>", "non-JSON"),
    (307, "http://localhost:{port}/ok", b"", "redirect"),   # a redirect to another host
    (302, "/ok", b"", "302"),                               # method-changing
    (307, "/ok", b"", None),                                # same origin: followed
])
def test_non_json_response(server, status, location, body, error):
    routes, port = server
    headers = {"Location": location.format(port=port)} if location else {}
    routes[("POST", "/r")] = (status, headers, body)
    routes[("POST", "/ok")] = (200, {}, b'{"ok": true}')
    if error is None:
        assert post(f"http://127.0.0.1:{port}/r") == (200, {"ok": True})
    else:
        with pytest.raises(TransportError, match=error):
            post(f"http://127.0.0.1:{port}/r")


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
