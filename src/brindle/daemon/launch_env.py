"""What an agent's pane asks the daemon for as it starts: the proxy and
certificate settings of the person's login shell (:mod:`brindle.login_shell_env`).

The daemon adds those to every CLI it launches itself. A worker is launched by
a helper under its supervisor's CLI, which never has them and must not: so the
worker's pane asks here, through its shim, before it becomes the CLI.

One route, ``POST /v1/launch-env`` with ``{"agent_id": ...}``, reached as the
permission holds are (:mod:`brindle.daemon.auth`): on the Unix socket only, with
no token. What keeps it closed:

- Who is asking is the calling process, as the kernel names it on the socket,
  never anything in the request. It must be, or descend from, a pane brindle
  itself recorded for the agent the request names (``peer.caller_agents``, as
  for a hook's hold). Any other caller gets a 403 and nothing else: another
  agent's pane, the person's own shell, or the launch helper, which is no
  pane's process.
- The answer is only what a launch of that agent by this daemon would have
  added (``agents.login_shell_additions``): names on the module's fixed list,
  for that agent's provider (Claude Code's client certificate, key and
  passphrase go to a Claude pane only), less what the pane has anyway and what
  its profile's or its repo's environment rules take out. Nothing else of the
  daemon's environment, of the login shell's, or of any other agent can be
  read through it.
- The values go into the response body and nowhere else: this module has no
  logger, stores nothing and sends no message, and a failure says only that
  there was one.
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from brindle.login_shell_env import FETCH_PATH as PATH


def _additions(agent_id: str) -> dict[str, str]:
    from brindle import agents
    from brindle.db import DB

    db = DB()       # on the thread that uses it: a connection stays on its own thread
    try:
        agent = db.get_agent(agent_id)
        ws = db.get_workspace(agent.workspace_id) if agent is not None else None
        return agents.login_shell_additions(agent, ws) if ws is not None else {}
    finally:
        db.conn.close()


async def fetch(request: Request) -> JSONResponse:
    from brindle.daemon import holds, peer

    agent_id = holds._str(await holds._body(request), "agent_id")
    pid = request.scope.get(peer.PEER_PID)
    if not agent_id or pid is None or agent_id not in await asyncio.to_thread(peer.caller_agents, pid):
        return JSONResponse({"error": {"code": "forbidden",
                                       "message": "a pane is given only its own agent's settings"}}, 403)
    try:
        found = await asyncio.to_thread(_additions, agent_id)
    except Exception:  # noqa: BLE001 - what went wrong may carry a value: say only that it did
        return JSONResponse({"error": {"code": "unavailable", "message": "the settings could not be worked out"}}, 503)
    return JSONResponse({"env": found})


ROUTES = [Route(PATH, fetch, methods=["POST"])]
