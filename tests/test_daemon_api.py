import asyncio
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from brindle.daemon import lifecycle
from brindle.daemon.app import build_app
from brindle.daemon.auth import WS_UNAUTHORIZED, Guard

TOKEN = "t" * 43


def _get(path: str, headers: dict[str, str]) -> tuple[int, dict]:
    """One HTTP request straight through the ASGI app."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "server": ("127.0.0.1", 8123), "client": ("127.0.0.1", 50000),
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    asyncio.run(build_app(TOKEN, port=8123)(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], json.loads(body)


@pytest.mark.parametrize("headers,status", [
    ({}, 401),
    ({"Authorization": "Bearer wrong"}, 401),
    ({"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"}, 403),
    ({"Authorization": f"Bearer {TOKEN}", "Origin": "tauri://localhost"}, 200),
])
def test_requests_need_the_token_and_no_foreign_origin(headers, status):
    code, body = _get("/v1/meta", headers)
    assert code == status
    assert ("api_version" in body) == (status == 200)


@pytest.mark.parametrize("query,frame,reached", [
    (f"token={TOKEN}".encode(), "wrong", False),    # a token in the URL counts for nothing
    (b"", TOKEN, True),
])
def test_websocket_token_is_its_first_frame(query, frame, reached):
    seen: list[dict] = []
    sent: list[dict] = []
    incoming = [{"type": "websocket.connect"},
                {"type": "websocket.receive", "text": frame},
                {"type": "websocket.receive", "text": "hello"}]

    async def route(scope, receive, send):
        seen.append(await receive())
        await send({"type": "websocket.accept"})
        seen.append(await receive())

    async def receive():
        return incoming.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": "/v1/events", "query_string": query, "headers": []}
    asyncio.run(Guard(route, TOKEN)(scope, receive, send))
    assert [m["type"] for m in sent].count("websocket.accept") == 1
    if reached:
        assert seen == [{"type": "websocket.connect"}, {"type": "websocket.receive", "text": "hello"}]
    else:
        assert seen == [] and sent[-1] == {"type": "websocket.close", "code": WS_UNAUTHORIZED}


def test_start_binds_loopback_and_keeps_its_files_private():
    # A short directory: a Unix socket's path is limited to ~104 bytes.
    d = Path(tempfile.mkdtemp(prefix="bd-"))
    try:
        os.chmod(d, 0o755)
        socks, info = lifecycle.prepare(TOKEN, unix=True, tcp=True, sock_path=d / "daemon.sock")
        try:
            assert socks[1].getsockname() == ("127.0.0.1", info["port"])
            assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
            assert stat.S_IMODE(os.stat(d / "daemon.sock").st_mode) == 0o600
            assert stat.S_IMODE(os.stat(lifecycle.info_path()).st_mode) == 0o600
            assert lifecycle.read_info() == info
        finally:
            for s in socks:
                s.close()
            lifecycle.cleanup(info)
    finally:
        shutil.rmtree(d, ignore_errors=True)
