"""Permission grants: what a person allowed one agent to do beyond
brindle's rules, from the app (brindle.permission_grants).

Two ways in, both only by the person's answer on the person-only inbox
route (service.inbox.answer_permission):

* ``allow_for`` on a request brindle's hook holds: a grant for that agent,
  repo and exact request (kind and match: the whole command, the resolved
  path, the URL or the tool), until ``expires_at`` and for ``uses_left``
  more calls.
* ``allow_once`` on a one-time grant an agent's hook asked for (``Asked``)
  after one of the built-in denies an adapter knows refused it: one use,
  bound to the exact scope the adapter worked out too (``scope``), for the
  agent's next try of the same request.

Grants live in the daemon's memory only: a restart forgets every one.

A hook spends a use (``POST /v1/holds/grants/use``) or asks for a one-time
grant (``POST /v1/holds/grants/ask``): like the hold routes, over the Unix
socket only, without the token, and only for an agent the calling process
runs under (by its ancestry). Spending is one atomic compare-and-decrement:
two calls racing for the last use get one allow between them, and a use
older than the policy allows now is refused. Nothing on those routes can
make, widen or extend a grant: asking only puts a request in front of the
person, and what it would cover and what the person is shown are worked
out here from the agent's workspace (permission_grants.ask_for), never
taken from the hook. The person lists grants (``GET /v1/grants``) and
revokes one (``POST /v1/grants/{id}/revoke``) behind the daemon's usual
guard (routes_read.list_grants, routes_actions.revoke_grant).

A use is spent when the hook allows the call, not when the tool succeeds:
a hook can't see the result, so a call that then fails still used it."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
import time
from dataclasses import asdict, dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from brindle.daemon.holds import PREFIX, call

USE_PATH = PREFIX + "/grants/use"
ASK_PATH = PREFIX + "/grants/ask"
MAX_GRANTS = 256
MAX_ASKED_PER_AGENT = 8
ASK_MINUTES = 15         # how long a one-time grant may wait for the person, and then for its use
USE_SECONDS = 5.0        # a hook waits this long on the daemon


@dataclass
class Grant:
    id: str
    agent_id: str
    repo_root: str
    kind: str
    match: str
    expires_at: float
    uses_left: int
    created: float
    scope: str = ""      # a one-time grant's exact scope (JSON), as the adapter worked it out

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Asked:
    """A one-time grant an agent's hook asked the person for."""
    id: str
    agent_id: str
    repo_root: str
    kind: str
    match: str
    scope: str
    action: str          # permission_grants.OVERRIDABLE's action
    describe: str        # what the person is shown
    expires_at: float
    created: float

    @property
    def digest(self) -> str:
        """Of everything the person is shown and allows: an answer to anything else is refused."""
        return hashlib.sha256("\0".join([self.id, self.repo_root, self.kind, self.match, self.scope,
                                         self.describe]).encode()).hexdigest()


class Grants:
    """The daemon's live grants and one-time asks, in memory only. ``clock``: wall time, for tests."""

    def __init__(self, clock=time.time) -> None:
        self._grants: dict[str, Grant] = {}
        self._asked: dict[str, Asked] = {}
        self._changed = threading.Lock()
        self.clock = clock

    def _purge(self) -> None:
        now = self.clock()
        for gid in [g.id for g in self._grants.values() if g.expires_at <= now or g.uses_left <= 0]:
            del self._grants[gid]
        for aid in [a.id for a in self._asked.values() if a.expires_at <= now]:
            del self._asked[aid]

    def _find(self, agent_id: str, repo_root: str, kind: str, match: str, scope: str) -> Grant | None:
        return next((g for g in self._grants.values() if (g.agent_id, g.repo_root, g.kind, g.match, g.scope)
                     == (agent_id, repo_root, kind, match, scope)), None)

    def add(self, agent_id: str, repo_root: str, kind: str, match: str, minutes: int, uses: int,
            scope: str = "") -> Grant | None:
        """A grant, or the live one for the same scope as it is (a second
        answer neither extends nor replaces it). None when there are too many."""
        with self._changed:
            self._purge()
            live = self._find(agent_id, repo_root, kind, match, scope)
            if live is not None and self.clock() < live.created + minutes * 60:
                return live
            if live is not None:     # older than this answer allows (the policy shortened): a new one
                del self._grants[live.id]
            if len(self._grants) >= MAX_GRANTS or uses <= 0 or minutes <= 0:
                return None
            now = self.clock()
            g = Grant(secrets.token_hex(8), agent_id, repo_root, kind, match, now + minutes * 60, uses, now, scope)
            self._grants[g.id] = g
            return g

    def use(self, agent_id: str, repo_root: str, kind: str, match: str, scope: str = "",
            max_minutes: int | None = None) -> Grant | None:
        """Spend one use of the agent's live grant for exactly this request
        (and scope), unless it is older than the policy allows now
        (``max_minutes``: a policy that shortened since the grant was given)."""
        with self._changed:
            self._purge()
            g = self._find(agent_id, repo_root, kind, match, scope)
            if g is not None and max_minutes is not None and self.clock() >= g.created + max_minutes * 60:
                del self._grants[g.id]     # over the policy's limit now: gone, so a new answer makes a new one
                return None
            if g is None:
                return None
            g.uses_left -= 1
            spent = Grant(**asdict(g))
            self._purge()
            return spent

    def ask(self, agent_id: str, repo_root: str, kind: str, match: str, scope: str, action: str,
            describe: str) -> Asked | None:
        """Put a one-time grant in front of the person, or the same one asked
        already as it is. None when the agent has too many waiting."""
        with self._changed:
            self._purge()
            same = next((a for a in self._asked.values() if (a.agent_id, a.repo_root, a.kind, a.match, a.scope)
                         == (agent_id, repo_root, kind, match, scope)), None)
            if same is not None:
                return same
            if sum(a.agent_id == agent_id for a in self._asked.values()) >= MAX_ASKED_PER_AGENT:
                return None
            now = self.clock()
            a = Asked(secrets.token_hex(8), agent_id, repo_root, kind, match, scope, action, describe,
                      now + ASK_MINUTES * 60, now)
            self._asked[a.id] = a
            return a

    def asked_of(self, agent_id: str) -> list[Asked]:
        with self._changed:
            self._purge()
            return sorted((a for a in self._asked.values() if a.agent_id == agent_id), key=lambda a: a.created)

    def asked(self, agent_id: str, asked_id: str) -> Asked | None:
        with self._changed:
            self._purge()
            a = self._asked.get(asked_id)
            return a if a is not None and a.agent_id == agent_id else None

    def answer(self, agent_id: str, asked_id: str, digest: str, allow: bool,
               max_minutes: int = ASK_MINUTES) -> Grant | bool | None:
        """The person's answer to a one-time ask, exactly as shown: its
        one-use grant (allow), for ASK_MINUTES or the policy's
        ``max_minutes`` if shorter; True (denied); or None when it isn't
        waiting any more or differs from what was shown (another answer won)."""
        with self._changed:
            self._purge()
            a = self._asked.get(asked_id)
            if a is None or a.agent_id != agent_id or a.digest != digest:
                return None
            del self._asked[asked_id]
        if not allow:
            return True
        return self.add(a.agent_id, a.repo_root, a.kind, a.match, min(ASK_MINUTES, max_minutes), 1, a.scope)

    def revoke(self, grant_id: str) -> Grant | None:
        with self._changed:
            self._purge()
            return self._grants.pop(grant_id, None)

    def drop_agent(self, agent_id: str) -> None:
        """The agent was closed: its grants and asks go with it."""
        with self._changed:
            for gid in [g.id for g in self._grants.values() if g.agent_id == agent_id]:
                del self._grants[gid]
            for aid in [a.id for a in self._asked.values() if a.agent_id == agent_id]:
                del self._asked[aid]

    def live(self) -> list[Grant]:
        with self._changed:
            self._purge()
            return sorted(self._grants.values(), key=lambda g: g.created)


GRANTS = Grants()


# -- the hook-facing routes ---------------------------------------------------------------------


async def _body(request: Request) -> dict | JSONResponse:
    """The body, for an agent the calling process runs under."""
    from brindle.daemon import peer

    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("agent_id"), str):
        return JSONResponse({"error": {"code": "invalid", "message": "a JSON object with agent_id"}}, 400)
    pid = request.scope.get(peer.PEER_PID)
    if pid is None or body["agent_id"] not in await asyncio.to_thread(peer.caller_agents, pid):
        return JSONResponse({"error": {"code": "forbidden", "message": "a hook acts only for its own agent"}}, 403)
    return body


async def _use(request: Request) -> JSONResponse:
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    fields = [body.get(k) for k in ("agent_id", "repo_root", "kind", "match", "scope")]
    limit = body.get("max_minutes")
    if (not all(isinstance(f, str) for f in fields) or not all(fields[:4])
            or not (limit is None or (isinstance(limit, int) and not isinstance(limit, bool)))):
        return JSONResponse({"error": {"code": "invalid", "message": "repo_root, kind, match and scope"}}, 400)
    g = GRANTS.use(*fields, max_minutes=limit)
    if g is None:
        return JSONResponse({"error": {"code": "not_found", "message": "no live grant for this request"}}, 404)
    return JSONResponse({"grant": g.to_json()})


async def _ask(request: Request) -> JSONResponse:
    """A hook asks the person for a one-time grant of the request it was
    refused. Only the request comes from the hook: what the grant covers
    and what the person is shown are worked out here, from the agent's own
    workspace and the repository as it is (permission_grants.ask_for)."""
    from brindle import permission_grants

    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    raw = body.get("request")
    got = await asyncio.to_thread(permission_grants.ask_for, body["agent_id"], raw if isinstance(raw, dict) else {})
    if got is None:
        return JSONResponse({"error": {"code": "refused", "message": "no one-time grant applies to this request"}},
                            409)
    a = GRANTS.ask(body["agent_id"], *got)
    if a is None:
        return JSONResponse({"error": {"code": "conflict", "message": "too many one-time grants waiting"}}, 409)
    return JSONResponse({"asked": a.id})


ROUTES = [Route(USE_PATH, _use, methods=["POST"]), Route(ASK_PATH, _ask, methods=["POST"])]


# -- the hook's side ----------------------------------------------------------------------------


def use(agent_id: str, repo_root: str, kind: str, match: str, scope: str = "",
        max_minutes: int | None = None) -> dict | None:
    """Spend a use of the agent's grant for this exact request through the
    daemon; the grant as it is now, or None (none, or no daemon)."""
    got = call(USE_PATH, {"agent_id": agent_id, "repo_root": repo_root, "kind": kind, "match": match,
                          "scope": scope, "max_minutes": max_minutes}, USE_SECONDS)
    grant = got.get("grant") if got else None
    return grant if isinstance(grant, dict) else None


def ask(agent_id: str, request: dict) -> str | None:
    """Ask the person, through the daemon, for a one-time grant of the
    request (permissions.Request.to_dict) the agent was refused: its id, or
    None (no daemon, nothing to grant, or too many waiting)."""
    got = call(ASK_PATH, {"agent_id": agent_id, "request": request}, USE_SECONDS)
    asked = got.get("asked") if got else None
    return asked if isinstance(asked, str) else None
