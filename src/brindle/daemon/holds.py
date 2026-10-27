"""Permission holds: the one channel a person's answer from the app reaches a
waiting permission hook by.

A hook that holds a request (brindle.agents._hold_for_app) makes a random
nonce it keeps only in memory and registers the hold here (``POST
/v1/holds``), before it records the request anywhere: the agent (one the
calling process runs under, by its ancestry), the request's id, and the
digest of the exact request and suggested rule (permissions.request_digest), and when it stops
holding (``hold_until``). It then waits (``POST /v1/holds/{id}/wait``) for
the decision, which only the holder of that nonce is given, and ends the
hold when it stops waiting. The daemon owns the hold's lifetime all the
same: past ``hold_until``, or once its holder stops waiting for
LEASE_SECONDS (a hook killed before it could end it), the hold is gone.

Answers come in only through the person-only inbox route
(service.inbox.answer_permission), which the guard refuses to agent callers:
``claim`` binds the answer to the agent, the request and its digest (an
answer to a request that differs from the one the hook recorded is
refused), ``release`` hands it to the hook. The one other is a worker's
supervisor's, while the hold is still with it (``supervise_until``): the
supervise route, which tells the supervisor by the calling process's
ancestry (brindle.permission_routing.answer). Nothing in the database is ever
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
# Seconds a hold lives past its holder's last wait: a hook that vanished
# without ending it (killed, crashed) stops renewing, and its hold lapses.
LEASE_SECONDS = 60.0
OUTCOMES_KEPT = 1024     # how many ended holds' outcomes are remembered
CALL_SECONDS = 5.0       # longest a hook's register, end or a supervisor's answer waits on the daemon


def _hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


@dataclass
class Hold:
    agent_id: str
    request_id: str
    digest: str
    nonce_hash: str
    hold_until: float              # when the hold ends whatever happens (the hook's permission_hold_seconds)
    lease_until: float             # when it lapses unless its holder waits again
    answer: str | None = None      # claimed by a person's answer
    released: bool = False         # handed to the hook
    # Until when the worker's supervisor may answer it, before the person sees it
    # (brindle.permission_routing); None: the person's alone (person-only, or escalated).
    supervise_until: float | None = None
    supervised_for: float = 0.0    # how long that was, for the person's note once it ran out
    escalation: str | None = None  # the supervisor's reason for handing it to the person
    # Registered while the same request of the agent's waited on its supervisor,
    # unanswered: the supervisor's answer to that one settles this one too (``settle``).
    joined: bool = False

    def supervised(self, now: float) -> bool:
        return self.supervise_until is not None and now < self.supervise_until

    def routed_note(self, now: float) -> str | None:
        """Why the person sees a request its supervisor had first, if it had."""
        if self.escalation is not None:
            return f"Supervisor escalated: {self.escalation}" if self.escalation else "Supervisor escalated this"
        if self.supervise_until is not None and now >= self.supervise_until:
            span = self.supervised_for
            return f"Supervisor didn't answer in {round(span / 60)} min" if span >= 60 else \
                f"Supervisor didn't answer in {round(span)}s"
        return None


class Holds:
    """The daemon's live holds, in memory only. The daemon owns their
    lifetime: a hold is live until its ``hold_until`` and while its holder
    keeps waiting on it (its lease); past either it is gone, can't be
    claimed or released, and frees its place. ``clock``: wall time, for tests.

    How each hold left is kept (``outcome``): "consumed" (its hook took the
    person's answer), "expired" (past hold_until or its lease), "ended" (its
    hook stopped holding first) or "dropped" (its agent was closed). Only
    "consumed" means an answer reached the hook."""

    def __init__(self, clock=time.time) -> None:
        self._holds: dict[str, Hold] = {}
        self._outcomes: dict[str, str] = {}
        self._changed = threading.Lock()
        self.clock = clock

    def _leave(self, request_id: str, outcome: str) -> Hold:
        """Remove a hold, keeping how it left (with the lock held)."""
        self._outcomes[request_id] = outcome
        while len(self._outcomes) > OUTCOMES_KEPT:
            del self._outcomes[next(iter(self._outcomes))]
        return self._holds.pop(request_id)

    def _purge(self) -> None:
        """Drop the holds past their hold_until or lease (with the lock held)."""
        now = self.clock()
        for request_id in [r for r, h in self._holds.items() if now >= min(h.hold_until, h.lease_until)]:
            self._leave(request_id, "expired")

    def outcome(self, request_id: str) -> str | None:
        """How the hold left (see the class), or None while it is live (or unknown)."""
        with self._changed:
            self._purge()
            return self._outcomes.get(request_id)

    def register(self, agent_id: str, request_id: str, digest: str, nonce: str, hold_until: float,
                 supervise_until: float | None = None) -> bool:
        """False when the id is held already, the hold would already be over,
        or the agent (or everyone) holds too many live ones. ``supervise_until``:
        its supervisor may answer it until then (``settle``); it joins the same
        request already waiting there unanswered, if one is (``joined``)."""
        with self._changed:
            self._purge()
            now = self.clock()
            mine = sum(h.agent_id == agent_id for h in self._holds.values())
            if (request_id in self._holds or hold_until <= now or mine >= MAX_PER_AGENT
                    or len(self._holds) >= MAX_HOLDS):
                return False
            if supervise_until is not None:
                supervise_until = min(supervise_until, hold_until)
            self._holds[request_id] = Hold(
                agent_id, request_id, digest, _hash(nonce), hold_until, now + LEASE_SECONDS,
                supervise_until=supervise_until, supervised_for=max(0.0, (supervise_until or now) - now),
                joined=supervise_until is not None and bool(self._waiting_on_supervisor(agent_id, digest, now)))
            self._outcomes.pop(request_id, None)
            return True

    def held(self, agent_id: str, request_id: str) -> Hold | None:
        """The agent's live hold ``request_id``, if it is one."""
        with self._changed:
            self._purge()
            hold = self._holds.get(request_id)
            return hold if hold is not None and hold.agent_id == agent_id else None

    def of_agent(self, agent_id: str) -> list[Hold]:
        """Every live hold of the agent's."""
        with self._changed:
            self._purge()
            return [h for h in self._holds.values() if h.agent_id == agent_id]

    def agents(self) -> list[str]:
        """The agents with a live hold, in the order their holds were registered."""
        with self._changed:
            self._purge()
            return list(dict.fromkeys(h.agent_id for h in self._holds.values()))

    def drop_agent(self, agent_id: str) -> None:
        """The agent was closed: none of its holds can be answered any more."""
        with self._changed:
            for request_id in [r for r, h in self._holds.items() if h.agent_id == agent_id]:
                self._leave(request_id, "dropped")

    def claim(self, agent_id: str, request_id: str, digest: str, answer: str) -> bool:
        """Claim the hold for ``answer``, the person's: False when it isn't
        held (or no longer live), is another agent's, answers a request that
        differs from the one the hook recorded, or another answer won."""
        with self._changed:
            self._purge()
            hold = self._holds.get(request_id)
            if (hold is None or hold.agent_id != agent_id or hold.answer is not None
                    or not hmac.compare_digest(hold.digest, digest)):
                return False
            hold.answer = answer
            return True

    def _waiting_on_supervisor(self, agent_id: str, digest: str, now: float) -> list[Hold]:
        """The agent's holds of this request still with its supervisor, unanswered (lock held)."""
        return [h for h in self._holds.values()
                if h.agent_id == agent_id and h.digest == digest and h.supervised(now) and h.answer is None]

    def settle(self, agent_id: str, request_id: str, answer: str | None, reason: str = "") -> list[str]:
        """The supervisor's ``answer`` to the agent's request ``request_id``,
        handed to its hook at once, or (None) the request handed to the person
        with ``reason``; the same for every identical request of the agent's
        still waiting on the supervisor. In one step with ``register``, so a
        duplicate either joins before and is settled here, or comes after and
        is asked about afresh. Empty when ``request_id`` isn't the agent's,
        live, with its supervisor and unanswered."""
        with self._changed:
            self._purge()
            now = self.clock()
            hold = self._holds.get(request_id)
            if hold is None or hold.agent_id != agent_id or hold.answer is not None or not hold.supervised(now):
                return []
            same = self._waiting_on_supervisor(agent_id, hold.digest, now)
            for h in same:
                if answer is None:
                    h.supervise_until, h.escalation = None, reason
                else:
                    h.answer, h.released = answer, True
            return [h.request_id for h in same]

    def release(self, agent_id: str, request_id: str) -> bool:
        """Hand a claimed answer to the hook: False when the hold ended (or
        lapsed) meanwhile."""
        with self._changed:
            self._purge()
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
        is its own and still live, the answer released to it, if any). Its
        waiting renews the lease; an answer taken ends the hold."""
        with self._changed:
            self._purge()
            hold = self._holds.get(request_id)
            if hold is None or not hmac.compare_digest(hold.nonce_hash, _hash(nonce)):
                return False, None
            if hold.released:
                self._leave(request_id, "consumed")
                return True, hold.answer
            hold.lease_until = self.clock() + LEASE_SECONDS
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
                self._leave(request_id, "ended")


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
    hold_until = body.get("hold_until")
    if not all(fields) or not isinstance(hold_until, (int, float)) or isinstance(hold_until, bool):
        return JSONResponse({"error": {"code": "invalid",
                                       "message": "agent_id, request_id, digest, nonce and hold_until"}}, 400)
    supervise_until = body.get("supervise_until")
    if supervise_until is not None and (not isinstance(supervise_until, (int, float)) or isinstance(supervise_until, bool)):
        return JSONResponse({"error": {"code": "invalid", "message": "supervise_until is a number"}}, 400)
    pid = request.scope.get(peer.PEER_PID)
    if pid is None or fields[0] not in await asyncio.to_thread(peer.caller_agents, pid):
        return JSONResponse({"error": {"code": "forbidden", "message": "a hook holds only its own agent's requests"}}, 403)
    if not HOLDS.register(*fields, float(hold_until),
                          float(supervise_until) if supervise_until is not None else None):
        return JSONResponse({"error": {"code": "conflict", "message": "already held, or too many holds"}}, 409)
    hold = HOLDS.held(fields[0], fields[1])
    return JSONResponse({"held": True, "joined": bool(hold and hold.joined)})


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


async def supervise(request: Request) -> JSONResponse:
    """A supervisor's answer to its worker's request (brindle.permission_routing.answer):
    who is calling is read from the calling process's ancestry, never from the body."""
    from brindle import permission_routing
    from brindle.daemon import peer
    from brindle.db import DB

    body = await _body(request)
    pid = request.scope.get(peer.PEER_PID)
    callers = await asyncio.to_thread(peer.caller_agents, pid) if pid is not None else set()
    reason = body.get("reason")

    def run() -> dict:
        db = DB()       # on the thread that uses it: a connection stays on its own thread
        try:
            return permission_routing.answer(db, callers, _str(body, "agent_id") or "", request.path_params["id"],
                                             _str(body, "answer") or "", reason if isinstance(reason, str) else "")
        finally:
            db.conn.close()
    try:
        out = await asyncio.to_thread(run)
    except permission_routing.RoutingError as e:
        return JSONResponse({"error": {"code": e.code, "message": str(e)}}, e.status)
    return JSONResponse(out)


ROUTES = [
    Route(PREFIX, register, methods=["POST"]),
    Route(PREFIX + "/{id}/wait", wait, methods=["POST"]),
    Route(PREFIX + "/{id}/end", end, methods=["POST"]),
    Route(PREFIX + "/{id}/supervise", supervise, methods=["POST"]),
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


def post(path: str, body: dict, timeout: float) -> tuple[int, dict] | None:
    """POST ``body`` to the daemon's hold route ``path`` over its Unix socket
    (its fixed path: nothing read from daemon.json): its status and JSON
    body, or None when it can't be reached or answers with no JSON object."""
    from brindle.daemon import lifecycle

    conn = _UnixConnection(str(lifecycle.socket_path()), timeout)
    try:
        conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        return (resp.status, data) if isinstance(data, dict) else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def call(path: str, body: dict, timeout: float) -> dict | None:
    """``post``, None also when the daemon answers with an error."""
    got = post(path, body, timeout)
    return got[1] if got is not None and got[0] == 200 else None


class Holder:
    """A hook's hold on one request: its nonce stays here, in memory."""

    joined = False      # the daemon joined it to the same request waiting on the supervisor (Holds.register)

    def __init__(self, agent_id: str, request_id: str, digest: str) -> None:
        self.agent_id, self.request_id, self.digest = agent_id, request_id, digest
        self.nonce = secrets.token_urlsafe(32)

    def register(self, hold_until: float, supervise_until: float | None = None) -> bool:
        """Whether the daemon holds it now, until ``hold_until`` at most
        (False: no daemon to answer through), with the worker's supervisor
        until ``supervise_until`` when given."""
        body = {"agent_id": self.agent_id, "request_id": self.request_id, "digest": self.digest,
                "nonce": self.nonce, "hold_until": hold_until}
        if supervise_until is not None:
            body["supervise_until"] = supervise_until
        got = call(PREFIX, body, CALL_SECONDS)
        self.joined = bool(got and got.get("joined"))
        return got is not None

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
