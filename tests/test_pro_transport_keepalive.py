"""The Pro transport keeps connections alive instead of paying a TLS
handshake on every Library call."""

import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from brindle.pro import auth
from brindle.pro.auth import TransportError


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive unless a side says close

    def setup(self):
        super().setup()
        self.server.connections.append(self.connection)
        self.served = 0

    def _serve(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.requests.append(self.command)
        if self.served and self.server.close_reused:
            # The server gave up on this kept-alive connection just as the
            # client sent on it: no response, the socket closes.
            self.close_connection = True
            return
        self.served += 1
        body = b"<html>not json</html>" if self.path == "/bad" else b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _serve

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("BRINDLE_PRO_DEV", "1")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.connections, srv.requests, srv.close_reused = [], [], False
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    for sock in srv.connections:   # end kept-alive connections so handler threads exit
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    srv.shutdown()
    srv.server_close()


def test_requests_reuse_one_connection(server):
    srv, base = server
    # Call sites build a new transport per call, so the pool is shared by all of them.
    assert auth.UrllibTransport().request("GET", base + "/me", None, {}) == (200, {"ok": True})
    assert auth.UrllibTransport().request("POST", base + "/members", {"a": "b"}, {}) == (
        200, {"ok": True})
    assert len(srv.connections) == 1


def test_a_response_that_does_not_decode_drops_its_connection(server):
    srv, base = server
    t = auth.UrllibTransport()
    with pytest.raises(TransportError, match="non-JSON"):
        t.request("GET", base + "/bad", None, {})
    assert t.request("GET", base + "/me", None, {}) == (200, {"ok": True})
    assert len(srv.connections) == 2


def test_handshake_timeout_reaches_the_app_as_library_slow(monkeypatch):
    from brindle.daemon import routes_actions as actions

    monkeypatch.setenv("BRINDLE_PRO_DEV", "1")

    def handshake_times_out(self):
        raise TimeoutError("_ssl.c:1015: The handshake operation timed out")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", handshake_times_out)
    with pytest.raises(TransportError) as caught:
        auth.UrllibTransport().request("GET", "http://127.0.0.1:9/me", None, {})
    try:   # how the Library helpers wrap it (shared_docs raises RuntimeError from it)
        raise RuntimeError("could not read access") from caught.value
    except RuntimeError as wrapped:
        err = actions.upstream_error(wrapped)
    assert (err.code, err.http_status) == ("library_slow", 503)
    assert "_ssl" not in err.message


@pytest.mark.parametrize("method,sent", [
    ("GET", ["GET", "GET", "GET"]),   # stale reused connection: sent again on a fresh one
    ("POST", ["GET", "POST"]),        # never sent twice
])
def test_stale_reused_connection(server, method, sent):
    srv, base = server
    t = auth.UrllibTransport()
    assert t.request("GET", base + "/warm", None, {}) == (200, {"ok": True})
    srv.close_reused = True
    form = {"a": "b"} if method == "POST" else None
    if method == "GET":
        assert t.request(method, base + "/x", form, {}) == (200, {"ok": True})
    else:
        with pytest.raises(TransportError):
            t.request(method, base + "/x", form, {})
    assert srv.requests == sent
    assert len(srv.connections) == len(sent) - 1
