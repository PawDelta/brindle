"""The daemon's guard: a token on every request, and no foreign Origin.

The token is 32 random bytes made at each start and written only to
``~/.brindle/daemon.json`` (0600), so only the user who can read that file can
act, whichever transport they reach the daemon on. An HTTP request sends it as
``Authorization: Bearer <token>``. A WebSocket sends it as its first text
frame after the handshake, never in the URL (URLs end up in logs and
history); the guard accepts the connection itself, checks that frame, and
only then hands the socket to the route, which sees an ordinary new
connection.

A browser always names the page it runs on in ``Origin``: only the app's own
webview (``tauri://localhost``, ``http://tauri.localhost`` on Windows) and
the daemon's own TCP address may. Clients that aren't browsers send none.

Agents run as the same user, so they could read the token too: on the Unix
socket the guard also refuses a caller that is one of a brindle agent's
processes (see :mod:`brindle.daemon.peer`)."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from brindle.daemon import peer

APP_ORIGINS = ("tauri://localhost", "http://tauri.localhost")
FIRST_FRAME_TIMEOUT = 10.0      # seconds a WebSocket has to send its token
WS_UNAUTHORIZED = 4401          # close code for a WebSocket whose first frame isn't the token
CALLER_TTL = 5.0                # seconds a Unix-socket caller's agent check is reused


def new_token() -> str:
    return secrets.token_urlsafe(32)


class Guard:
    """ASGI middleware in front of every route."""

    def __init__(self, app: ASGIApp, token: str, port: int | None = None) -> None:
        self.app = app
        self._token = token.encode()
        self._origins = set(APP_ORIGINS)
        self._callers: dict[int, tuple[float, str | None]] = {}   # pid -> (when, agent_caller)
        if port:
            self._origins |= {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        origin_ok = "origin" not in headers or headers["origin"] in self._origins
        # Only the Unix socket names its caller: over TCP the token is all that keeps agents out.
        agent = await self._agent_caller(scope[peer.PEER_PID]) if peer.PEER_PID in scope else None
        if scope["type"] == "websocket":
            if not origin_ok or agent:
                await send({"type": "websocket.close", "code": 1008})   # refused before the handshake: a 403
                return
            await self._websocket(scope, receive, send)
            return
        if not origin_ok:
            await _reject(send, 403, "forbidden", "requests from another origin are refused")
            return
        if agent:
            await _reject(send, 403, "agent_caller",
                          f"refused: the caller runs under brindle agent {agent}; "
                          "the daemon serves the app and people, not agents")
            return
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not self._token_ok(token):
            await _reject(send, 401, "unauthorized", "missing or wrong token (see ~/.brindle/daemon.json)")
            return
        await self.app(scope, receive, send)

    async def _agent_caller(self, pid: int) -> str | None:
        """peer.agent_caller, remembered for CALLER_TTL: it lists every
        process, and the app makes many requests."""
        now = time.monotonic()
        hit = self._callers.get(pid)
        if hit is None or now - hit[0] > CALLER_TTL:
            self._callers = {p: h for p, h in self._callers.items() if now - h[0] <= CALLER_TTL}
            hit = self._callers[pid] = (now, await asyncio.to_thread(peer.agent_caller, pid))
        return hit[1]

    def _token_ok(self, token: object) -> bool:
        return isinstance(token, str) and hmac.compare_digest(token.strip().encode(), self._token)

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (await receive())["type"] != "websocket.connect":
            return
        await send({"type": "websocket.accept"})
        try:
            first = await asyncio.wait_for(receive(), FIRST_FRAME_TIMEOUT)
        except asyncio.TimeoutError:
            first = {"type": "timeout"}
        if first["type"] == "websocket.disconnect":
            return
        if first["type"] != "websocket.receive" or not self._token_ok(first.get("text")):
            await send({"type": "websocket.close", "code": WS_UNAUTHORIZED})
            return

        connected = False

        async def route_receive() -> Message:
            nonlocal connected
            if not connected:
                connected = True
                return {"type": "websocket.connect"}
            return await receive()

        async def route_send(message: Message) -> None:
            if message["type"] != "websocket.accept":   # already accepted above
                await send(message)

        await self.app(scope, route_receive, route_send)


async def _reject(send: Send, status: int, code: str, message: str) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
