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
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock

from brindle import procs
from brindle.daemon import process_probe

PEER_PID = "brindle.peer_pid"   # scope key: the pid of a Unix-socket caller
SOL_LOCAL = 0                    # macOS <sys/un.h>
LOCAL_PEERPID = 2
PANE_TTL = 5.0
_pane_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="brindle-agent-panes")
_pane_lock = Lock()
_pane_cache: tuple[float, dict[str, list[int]]] | None = None
_pane_refresh: Future | None = None


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
    try:
        panes = tmux.list_panes()
        owners = agents.pane_owners(db, panes)
        return {a.id: tmux.window_pids(a.tmux_window) for a in db.list_agents()
                if a.status not in ("done", "paused") and a.tmux_window
                and agents.same_server(a, panes) and panes.get(a.tmux_window)
                and agents.owns_pane(db, a, owners)}
    finally:
        db.conn.close()


def agent_caller(pid: int) -> str | None:
    """Why process ``pid`` is refused: it belongs to a brindle agent (it has
    BRINDLE_AGENT_ID in its environment, or it or an ancestor is one of a
    live agent's processes), or it can't be checked. None for anyone else's."""
    me = process_probe.process(pid, environment=True)
    if me is None:
        # Gone already (a request sent before exiting) or not this user's:
        # unchecked is refused, or an agent could fire and exit.
        return "the caller can't be checked (it exited, or isn't this user's process)"
    if (m := re.search(r"(?:^|\s)BRINDLE_AGENT_ID=(\S+)", me.text)):
        return f"the caller runs under brindle agent {m.group(1)}"
    chain, current = set(), me
    while current is not None and current.pid not in chain:
        chain.add(current.pid)
        current = process_probe.process(current.ppid) if current.ppid > 1 else None
    owned = _cached_agent_panes()
    return next((f"the caller runs under brindle agent {aid}"
                 for aid, pids in owned.items() if set(pids) & chain), None)


def _cached_agent_panes() -> dict[str, list[int]]:
    """Share the initial snapshot; refresh stale snapshots off the caller lane.

    Keep serving the last snapshot while a refresh is in flight. Never
    substitute an empty snapshot when the initial collection fails.
    """
    global _pane_cache, _pane_refresh
    with _pane_lock:
        if _pane_refresh is not None and _pane_refresh.done():
            finished, _pane_refresh = _pane_refresh, None
            try:
                _pane_cache = (time.monotonic(), finished.result())
            except Exception:
                if _pane_cache is None:
                    raise
        if _pane_cache is not None and time.monotonic() - _pane_cache[0] <= PANE_TTL:
            return _pane_cache[1]
        if _pane_refresh is None:
            _pane_refresh = _pane_executor.submit(_agent_panes)
        pending = _pane_refresh
        if _pane_cache is not None:
            return _pane_cache[1]
    # Cold start must know the pane roots before it can clear a caller.
    result = pending.result()
    with _pane_lock:
        if _pane_refresh is pending:
            _pane_cache = (time.monotonic(), result)
            _pane_refresh = None
    return result


def caller_agents(pid: int) -> set[str]:
    """The live agents (in brindle's database, with a tmux pane brindle owns)
    one of whose pane processes ``pid`` descends from: empty when none, or it
    can't be checked. Only what brindle itself knows counts, never what a
    process says of itself (its environment, its command line), which an
    agent could set to pass for another. A permission hook holds requests
    only for these (brindle.daemon.holds)."""
    table = procs.table()
    if pid not in table:
        return set()
    chain, p = set(), pid
    while p in table and p not in chain:
        chain.add(p)
        p = table[p].ppid
    return {aid for aid, pids in _agent_panes().items() if set(pids) & chain}
