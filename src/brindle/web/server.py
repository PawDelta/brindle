"""The web UI's HTTP server: loopback only, one random token per run.

Every request must carry the token (``?token=`` once, then the cookie that
response sets), name a loopback Host (a DNS-rebinding page names its own
domain), and, unless it's a GET, come from this server's own Origin (a
cross-site form or fetch can't). Anything else gets a bare 403.

Routes live in the other modules of ``brindle.web``: each registers its
handlers with ``@route(method, pattern)``, and ``load_routes`` imports them
all when the server starts. A handler takes a ``Request`` and returns a
``Response`` (``json_response`` for an API)."""

from __future__ import annotations

import hmac
import importlib
import json
import logging
import pkgutil
import re
import secrets
import webbrowser
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from brindle.db import DB

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
COOKIE = "brindle_web"
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")
MAX_BODY = 1 << 20
# Methods that never need an Origin: they change nothing (handlers must keep
# it that way), and a top-level navigation sends none.
SAFE_METHODS = ("GET",)


@dataclass
class App:
    """What every handler shares: the session the UI shows and the token."""
    repo_root: str | None
    root_id: str | None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    port: int = 0


@dataclass
class Request:
    app: App
    method: str
    path: str
    query: dict[str, str]
    params: dict[str, str]
    headers: dict[str, str]
    body: bytes = b""
    _db: DB | None = None

    @property
    def db(self) -> DB:
        """A connection of this request's own (requests run on their own threads)."""
        if self._db is None:
            self._db = DB()
        return self._db

    def json(self) -> object:
        return json.loads(self.body or b"null")


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


def json_response(obj: object, status: int = 200) -> Response:
    return Response(status, json.dumps(obj, default=str).encode(), "application/json")


Handler = Callable[[Request], Response]
_ROUTES: list[tuple[str, re.Pattern, Handler]] = []


def route(method: str, pattern: str) -> Callable[[Handler], Handler]:
    """Register a handler for ``method`` on paths matching ``pattern``, where
    ``{name}`` matches one path segment and arrives in ``request.params``."""
    rx = re.compile("^" + re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(pattern)) + "$")

    def register(fn: Handler) -> Handler:
        _ROUTES.append((method.upper(), rx, fn))
        return fn
    return register


def match(method: str, path: str) -> tuple[Handler, dict[str, str]] | None:
    for m, rx, fn in _ROUTES:
        if m == method and (hit := rx.match(path)):
            return fn, hit.groupdict()
    return None


def load_routes() -> None:
    """Import every route module of this package (each registers on import)."""
    import brindle.web as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name != "server":
            importlib.import_module(f"{pkg.__name__}.{mod.name}")


def _hostname(host: str) -> str:
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class _Handler(BaseHTTPRequestHandler):
    server: "WebServer"

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, format: str, *args: object) -> None:
        log.debug("brindle web: " + format, *args)

    def _host_ok(self) -> bool:
        return _hostname(self.headers.get("Host", "")) in LOOPBACK_NAMES

    def _origin_ok(self) -> bool:
        port = self.server.app.port
        own = {f"http://{n}:{port}" for n in ("127.0.0.1", "localhost", "[::1]")}
        return self.headers.get("Origin") in own

    def _cookie_token(self) -> str | None:
        try:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        return jar[COOKIE].value if COOKIE in jar else None

    def _dispatch(self, method: str) -> None:
        app = self.server.app
        url = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        given = query.pop("token", None)
        from_query = given is not None and hmac.compare_digest(given, app.token)
        cookie = self._cookie_token()
        authed = from_query or (cookie is not None and hmac.compare_digest(cookie, app.token))
        if not self._host_ok() or (method not in SAFE_METHODS and not self._origin_ok()) \
                or not authed:
            self._send(Response(403, b"forbidden\n"))
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(Response(413, b"too large\n"))
            return
        body = self.rfile.read(length) if length else b""
        found = match(method, url.path)
        if found is None:
            resp = Response(404, b"not found\n")
        else:
            fn, params = found
            req = Request(app, method, url.path, query, params, dict(self.headers.items()), body)
            try:
                resp = fn(req)
            except Exception:
                log.exception("brindle web: %s %s failed", method, url.path)
                resp = Response(500, b"internal error\n")
            finally:
                if req._db is not None:
                    req._db.conn.close()
        if from_query:
            resp.headers["Set-Cookie"] = (f"{COOKIE}={app.token}; Path=/; HttpOnly; "
                                          "SameSite=Strict")
        self._send(resp)

    def _send(self, resp: Response) -> None:
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "frame-ancestors 'none'")
        for k, v in resp.headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(resp.body)


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app: App, port: int = 0) -> None:
        super().__init__((HOST, port), _Handler)
        self.app = app
        app.port = self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.app.port}/?token={self.app.token}"


def make_server(repo_root: str | None, root_id: str | None, port: int = 0) -> WebServer:
    """A server bound to loopback (``port`` 0: any free one), routes loaded."""
    load_routes()
    return WebServer(App(repo_root, root_id), port)


def serve(server: WebServer, open_browser: bool = True) -> None:
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
