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
processes (see :mod:`brindle.daemon.peer`). The one exception is the
permission hold routes (:mod:`brindle.daemon.holds`): a hook waiting on the
person's answer is an agent's child, so they take no token and no agent
check, only the Unix socket and the hold's own nonce."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from brindle.daemon import peer

APP_ORIGINS = ("tauri://localhost", "http://tauri.localhost")
FIRST_FRAME_TIMEOUT = 10.0      # seconds a WebSocket has to send its token
WS_UNAUTHORIZED = 4401          # close code for a WebSocket whose first frame isn't the token
CALLER_TTL = 5.0                # seconds a Unix-socket caller's agent check is reused
CALLER_CHECK_LIMIT = 5.0         # includes time queued behind other caller checks
# Process-wide, so guards cannot grow a separate pool for every connection.
# A stuck process probe must not occupy the daemon's default executor.
_CALLER_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="brindle-caller")
_CALLER_UNCHECKED = "the caller can't be checked"


def _hold_route(path: str) -> bool:
    from brindle.daemon.holds import PREFIX

    return path == PREFIX or path.startswith(PREFIX + "/")


def new_token() -> str:
    return secrets.token_urlsafe(32)


class Guard:
    """ASGI middleware in front of every route."""

    def __init__(self, app: ASGIApp, token: str, port: int | None = None) -> None:
        self.app = app
        self._token = token.encode()
        self._origins = set(APP_ORIGINS)
        self._callers: dict[int, tuple[float, str | None]] = {}   # pid -> (when, agent_caller)
        self._caller_checks: dict[int, asyncio.Future[str | None]] = {}
        self._caller_deadlines: dict[int, float] = {}
        if port:
            self._origins |= {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @property
    def state(self):
        """The wrapped app's state (heavy_read_lane, poller), as if unwrapped."""
        return self.app.state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        origin_ok = "origin" not in headers or headers["origin"] in self._origins
        if scope["type"] == "http" and _hold_route(scope["path"]):
            # A permission hook (an agent's own child) waiting on the person's
            # answer: over the Unix socket only, with no browser Origin, and
            # checked by its own nonce instead (brindle.daemon.holds).
            if peer.PEER_PID in scope and "origin" not in headers:
                await self.app(scope, receive, send)
            else:
                await _reject(send, 403, "forbidden", "permission holds are for brindle's hooks, on the Unix socket")
            return
        # Only the Unix socket names its caller: over TCP the token is all that keeps agents out.
        refused = await self._agent_caller(scope[peer.PEER_PID]) if peer.PEER_PID in scope else None
        if scope["type"] == "websocket":
            if not origin_ok or refused:
                await send({"type": "websocket.close", "code": 1008})   # refused before the handshake: a 403
                return
            await self._websocket(scope, receive, send)
            return
        if not origin_ok:
            await _reject(send, 403, "forbidden", "requests from another origin are refused")
            return
        if refused:
            await _reject(send, 403, "agent_caller",
                          f"refused: {refused}; the daemon serves the app and people, not agents")
            return
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not self._token_ok(token):
            await _reject(send, 401, "unauthorized", "missing or wrong token (see ~/.brindle/daemon.json)")
            return
        await self.app(scope, receive, send)

    async def _agent_caller(self, pid: int) -> str | None:
        """Share a bounded caller check and remember its result for CALLER_TTL.

        Threads cannot be stopped when their waiter expires. Keep the future
        until the worker finishes, including after refusal or cancellation,
        so repeated requests cannot queue more work for that pid.
        """
        now = time.monotonic()
        hit = self._callers.get(pid)
        if hit is not None and now - hit[0] <= CALLER_TTL:
            return hit[1]
        self._callers = {p: h for p, h in self._callers.items() if now - h[0] <= CALLER_TTL}
        check = self._caller_checks.get(pid)
        if check is None:
            try:
                check = asyncio.get_running_loop().run_in_executor(_CALLER_EXECUTOR, peer.agent_caller, pid)
            except Exception:
                self._callers[pid] = (time.monotonic(), _CALLER_UNCHECKED)
                return _CALLER_UNCHECKED
            self._caller_checks[pid] = check
            self._caller_deadlines[pid] = now + CALLER_CHECK_LIMIT
            check.add_done_callback(partial(self._caller_checked, pid))
        deadline = self._caller_deadlines[pid]
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            # One disconnected request must not cancel everyone else's check.
            refused = await asyncio.wait_for(asyncio.shield(check), remaining)
            # Completion and the deadline can become ready in the same turn.
            if time.monotonic() >= deadline:
                raise asyncio.TimeoutError
            return refused
        except Exception:
            # Never expose process data from an exception to the requester.
            self._callers[pid] = (time.monotonic(), _CALLER_UNCHECKED)
            return _CALLER_UNCHECKED

    def _caller_checked(self, pid: int, check: asyncio.Future[str | None]) -> None:
        """Publish the result before removing the in-flight entry.

        This also consumes failures when every request has disconnected.
        Late results are consumed but cannot authorize a caller.
        """
        refused = _CALLER_UNCHECKED
        if not check.cancelled():
            try:
                result = check.result()
                if time.monotonic() < self._caller_deadlines[pid]:
                    refused = result
            except Exception:
                pass
        self._callers[pid] = (time.monotonic(), refused)
        self._caller_checks.pop(pid, None)
        self._caller_deadlines.pop(pid, None)

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
