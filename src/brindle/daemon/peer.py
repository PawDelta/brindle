"""Who is on the other end of the daemon's Unix socket.

Agents run as the same OS user as the person, so they can read the token in
``daemon.json`` like anyone else. On the Unix socket the kernel names the
connecting process (LOCAL_PEERPID on macOS, SO_PEERCRED on Linux), so the
guard refuses a caller that is, or descends from, a live brindle agent's
process (its tmux pane, its CLI, its MCP server: see :mod:`brindle.procs`),
or whose own environment carries BRINDLE_AGENT_ID. A platform that names no
peer keeps token-only, as TCP does."""

from __future__ import annotations

import asyncio
import re
import socket
import struct
import sys

from brindle import procs

PEER_PID = "brindle.peer_pid"   # scope key: the pid of a Unix-socket caller
SOL_LOCAL = 0                    # macOS <sys/un.h>
LOCAL_PEERPID = 2


def peer_pid(sock) -> int | None:
    """The pid of the process connected to Unix socket ``sock``, or None
    where the OS doesn't say."""
    try:
        if sys.platform == "darwin":
            return struct.unpack("i", sock.getsockopt(SOL_LOCAL, LOCAL_PEERPID, 4))[0]
        if hasattr(socket, "SO_PEERCRED"):
            cred = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", cred)[0]
    except OSError:
        pass
    return None


def with_peer(base: type[asyncio.Protocol]) -> type[asyncio.Protocol]:
    """``base`` (one of uvicorn's HTTP or WebSocket protocols), handing each
    Unix-socket connection's peer pid to the app as ``scope[PEER_PID]``."""

    class PeerProtocol(base):  # type: ignore[valid-type, misc]
        def connection_made(self, transport) -> None:
            super().connection_made(transport)
            sock = transport.get_extra_info("socket")
            if sock is None or sock.family != socket.AF_UNIX or (pid := peer_pid(sock)) is None:
                return
            app = self.app

            async def app_with_peer(scope, receive, send) -> None:
                scope[PEER_PID] = pid
                await app(scope, receive, send)

            self.app = app_with_peer

    PeerProtocol.__name__ = PeerProtocol.__qualname__ = f"Peer{base.__name__}"
    return PeerProtocol


def _agent_panes() -> dict[str, list[int]]:
    """Live agents on this tmux server -> the pids of their panes."""
    from brindle import agents, tmux
    from brindle.db import DB

    db = DB()
    panes = tmux.list_panes()
    owners = agents.pane_owners(db, panes)
    return {a.id: tmux.window_pids(a.tmux_window) for a in db.list_agents()
            if a.status not in ("done", "paused") and a.tmux_window
            and agents.same_server(a, panes) and panes.get(a.tmux_window)
            and agents.owns_pane(db, a, owners)}


def agent_caller(pid: int) -> str | None:
    """Why process ``pid`` is refused: it belongs to a brindle agent (it has
    BRINDLE_AGENT_ID in its environment, or it or an ancestor is one of a
    live agent's processes), or it can't be checked. None for anyone else's."""
    table = procs.table()
    me = table.get(pid)
    if me is None:
        # Gone already (a request sent before exiting) or not this user's:
        # unchecked is refused, or an agent could fire and exit.
        return "the caller can't be checked (it exited, or isn't this user's process)"
    if (m := re.search(r"(?:^|\s)BRINDLE_AGENT_ID=(\S+)", me.text)):
        return f"the caller runs under brindle agent {m.group(1)}"
    return next((f"the caller runs under brindle agent {aid}" for aid in sorted(_owners(pid, table))), None)


def _owners(pid: int, table) -> set[str]:
    """The live agents one of whose processes is ``pid`` or an ancestor of it."""
    chain, p = set(), pid
    while p in table and p not in chain:
        chain.add(p)
        p = table[p].ppid
    panes = _agent_panes()
    owned = procs.agent_pids(procs.all_agent_ids(table) | set(panes), panes, table,
                             through_ancestors=True)
    return {aid for aid, pids in owned.items() if pids & chain}


def caller_agents(pid: int) -> set[str]:
    """The agents process ``pid`` runs under, by its processes' ancestry (not
    its environment, which can be a stale launch's): empty when none, or it
    can't be checked. A permission hook holds requests only for these."""
    table = procs.table()
    return _owners(pid, table) if pid in table else set()
