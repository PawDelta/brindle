"""Permission holds: the one channel a person's answer from the app reaches a
waiting permission hook by.

A hook that holds a request (brindle.agents._hold_for_app) makes a random
nonce it keeps only in memory and registers the hold here (``POST
/v1/holds``), before it records the request anywhere: the agent (one the
calling process runs under, by its ancestry), the request's id, and the
digest of the exact request and suggested rule (permissions.request_digest). It then
waits (``POST /v1/holds/{id}/wait``) for the decision, which only the holder
of that nonce is given, and ends the hold when it stops waiting.

Answers come in only through the person-only inbox route
(service.inbox.answer_permission), which the guard refuses to agent callers:
``claim`` binds the answer to the agent, the request and its digest (an
answer to a request that differs from the one the hook recorded is
refused), ``release`` hands it to the hook. Nothing in the database is ever
taken as an answer, and the hook finds the daemon by its fixed socket path,
not by anything in daemon.json.

The hold routes are the only ones a process descended from an agent may
call (a hook is one), only over the Unix socket, and without the token:
the nonce is what they check (see brindle.daemon.auth)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import json
import secrets
import socket
import threading
import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

PREFIX = "/v1/holds"
WAIT_SECONDS = 20.0      # longest one wait call lasts before the hook asks again
WAIT_STEP = 0.1          # seconds between a wait's looks at its hold (no thread is held meanwhile)
MAX_PER_AGENT = 16       # holds at once, per agent and in all
MAX_HOLDS = 256


def _hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


@dataclass
class Hold:
    agent_id: str
    request_id: str
    digest: str
    nonce_hash: str
    answer: str | None = None      # claimed by a person's answer
    released: bool = False         # handed to the hook


class Holds:
    """The daemon's live holds, in memory only."""

    def __init__(self) -> None:
        self._holds: dict[str, Hold] = {}
        self._changed = threading.Lock()

    def register(self, agent_id: str, request_id: str, digest: str, nonce: str) -> bool:
        """False when the id is held already, or the agent (or everyone) holds too many."""
        with self._changed:
            mine = sum(h.agent_id == agent_id for h in self._holds.values())
            if request_id in self._holds or mine >= MAX_PER_AGENT or len(self._holds) >= MAX_HOLDS:
                return False
            self._holds[request_id] = Hold(agent_id, request_id, digest, _hash(nonce))
            return True

    def held(self, agent_id: str, request_id: str) -> Hold | None:
        with self._changed:
            hold = self._holds.get(request_id)
            return hold if hold is not None and hold.agent_id == agent_id else None

    def claim(self, agent_id: str, request_id: str, digest: str, answer: str) -> bool:
        """Claim the hold for ``answer``, the person's: False when it isn't
        held, is another agent's, answers a request that differs from the one
        the hook recorded, or another answer won."""
        with self._changed:
            hold = self._holds.get(request_id)
            if (hold is None or hold.agent_id != agent_id or hold.answer is not None
                    or not hmac.compare_digest(hold.digest, digest)):
                return False
            hold.answer = answer
            return True

    def release(self, agent_id: str, request_id: str) -> bool:
        """Hand a claimed answer to the hook: False when the hold ended meanwhile."""
        with self._changed:
            hold = self._holds.get(request_id)
            if hold is None or hold.agent_id != agent_id or hold.answer is None or hold.released:
                return False
            hold.released = True
            return True

    def unclaim(self, agent_id: str, request_id: str) -> None:
        with self._changed:
            hold = self._holds.get(request_id)
            if hold is not None and hold.agent_id == agent_id and not hold.released:
                hold.answer = None

    def take(self, request_id: str, nonce: str) -> tuple[bool, str | None]:
        """For the hook holding ``nonce``, without blocking: (whether the hold
        is its own and still there, the answer released to it, if any). An
        answer taken ends the hold."""
        with self._changed:
            hold = self._holds.get(request_id)
            if hold is None or not hmac.compare_digest(hold.nonce_hash, _hash(nonce)):
                return False, None
            if hold.released:
                del self._holds[request_id]
                return True, hold.answer
            return True, None

    async def wait(self, request_id: str, nonce: str, seconds: float) -> tuple[bool, str | None]:
        """``take``, looking again every WAIT_STEP for up to ``seconds``: on
        the event loop, so a waiting hook holds no thread the daemon's other
        routes need."""
        deadline = time.monotonic() + seconds
        while True:
            held, answer = self.take(request_id, nonce)
            if not held or answer is not None or time.monotonic() >= deadline:
                return held, answer
            await asyncio.sleep(WAIT_STEP)

    def end(self, request_id: str, nonce: str) -> None:
        """The hook stopped waiting: no answer reaches it from now on."""
        with self._changed:
            hold = self._holds.get(request_id)
            if hold is not None and hmac.compare_digest(hold.nonce_hash, _hash(nonce)):
                del self._holds[request_id]


HOLDS = Holds()


# -- the hook-facing routes ---------------------------------------------------------------------


def _str(body: object, key: str) -> str | None:
    value = body.get(key) if isinstance(body, dict) else None
    return value if isinstance(value, str) and value else None


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def register(request: Request) -> JSONResponse:
    """A hook's hold, for an agent it runs under (by its processes' ancestry)."""
    from brindle.daemon import peer

    body = await _body(request)
    fields = [_str(body, k) for k in ("agent_id", "request_id", "digest", "nonce")]
    if not all(fields):
        return JSONResponse({"error": {"code": "invalid", "message": "agent_id, request_id, digest and nonce"}}, 400)
    pid = request.scope.get(peer.PEER_PID)
    if pid is None or fields[0] not in await asyncio.to_thread(peer.caller_agents, pid):
        return JSONResponse({"error": {"code": "forbidden", "message": "a hook holds only its own agent's requests"}}, 403)
    if not HOLDS.register(*fields):
        return JSONResponse({"error": {"code": "conflict", "message": "already held, or too many holds"}}, 409)
    return JSONResponse({"held": True})


async def wait(request: Request) -> JSONResponse:
    body = await _body(request)
    nonce = _str(body, "nonce") or ""
    seconds = body.get("seconds")
    seconds = min(float(seconds), WAIT_SECONDS) if isinstance(seconds, (int, float)) and seconds > 0 else WAIT_SECONDS
    held, answer = await HOLDS.wait(request.path_params["id"], nonce, seconds)
    return JSONResponse({"held": held, "answer": answer})


async def end(request: Request) -> JSONResponse:
    HOLDS.end(request.path_params["id"], _str(await _body(request), "nonce") or "")
    return JSONResponse({"held": False})


ROUTES = [
    Route(PREFIX, register, methods=["POST"]),
    Route(PREFIX + "/{id}/wait", wait, methods=["POST"]),
    Route(PREFIX + "/{id}/end", end, methods=["POST"]),
]


# -- the hook's side ----------------------------------------------------------------------------


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def call(path: str, body: dict, timeout: float) -> dict | None:
    """POST ``body`` to the daemon's hold route ``path`` over its Unix socket
    (its fixed path: nothing read from daemon.json); None when it can't be
    reached or answers with an error."""
    from brindle.daemon import lifecycle

    conn = _UnixConnection(str(lifecycle.socket_path()), timeout)
    try:
        conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        return data if resp.status == 200 and isinstance(data, dict) else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


class Holder:
    """A hook's hold on one request: its nonce stays here, in memory."""

    def __init__(self, agent_id: str, request_id: str, digest: str) -> None:
        self.agent_id, self.request_id, self.digest = agent_id, request_id, digest
        self.nonce = secrets.token_urlsafe(32)

    def register(self) -> bool:
        """Whether the daemon holds it now (False: no daemon to answer through)."""
        return call(PREFIX, {"agent_id": self.agent_id, "request_id": self.request_id,
                             "digest": self.digest, "nonce": self.nonce}, timeout=5.0) is not None

    def wait(self, seconds: float) -> tuple[bool, str | None]:
        """(still held, the person's answer if one came within ``seconds``)."""
        got = call(f"{PREFIX}/{self.request_id}/wait", {"nonce": self.nonce, "seconds": seconds},
                   timeout=seconds + 10)
        if got is None:
            return False, None
        answer = got.get("answer")
        return bool(got.get("held")), answer if isinstance(answer, str) else None

    def end(self) -> None:
        call(f"{PREFIX}/{self.request_id}/end", {"nonce": self.nonce}, timeout=5.0)
