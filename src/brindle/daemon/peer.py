"""Who is on the other end of the daemon's Unix socket.

Agents run as the same OS user as the person, so they can read the token in
``daemon.json`` like anyone else. On the Unix socket the kernel names the
connecting process (LOCAL_PEERPID on macOS, SO_PEERCRED on Linux), so the
guard refuses a caller that is, or descends from, a live brindle agent's
process (its tmux pane, its CLI, its MCP server: see :mod:`brindle.procs`),
or one whose environment carries BRINDLE_AGENT_ID. A platform that names no
peer keeps token-only, as TCP does."""

from __future__ import annotations

import asyncio
import os
import re
import socket
import struct
import sys
import time
from collections.abc import Iterator
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
# (when taken, agent_panes_stamp it was taken under, agent id -> pane pids)
_pane_cache: tuple[float, str, dict[str, list[int]]] | None = None
_pane_refresh: tuple[str, Future] | None = None


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


def _ancestry(pid: int, identity: bool) -> Iterator[procs.Proc]:
    """``pid`` and then its ancestors, read one process at a time: nothing
    when ``pid`` can't be read. ``identity`` reads each one's command line
    and environment too, and stops below Claude Code's daemon and this
    process, which :func:`brindle.procs.agent_pids` never walks through."""
    seen: set[int] = set()
    current = process_probe.process(pid, environment=identity, command=identity)
    while current is not None and current.pid not in seen:
        if identity and seen and (current.pid == os.getpid() or procs.is_daemon(current)):
            return
        yield current
        seen.add(current.pid)
        current = (process_probe.process(current.ppid, environment=identity, command=identity)
                   if current.ppid > 1 else None)


def _launched_by(p: procs.Proc) -> str | None:
    """The agent whose environment, launch command or brindle MCP server
    makes ``p`` one of its processes (see :mod:`brindle.procs`)."""
    if (m := re.search(r"(?:^|\s)BRINDLE_AGENT_ID=(\S+)", p.text)):
        return m.group(1)
    if (aid := procs.root_agent(p)) is not None:
        return aid
    if procs.is_spare(p):
        for child in process_probe.children(p.pid):
            c = process_probe.process(child, environment=True, command=True)
            if c is not None and (aid := procs.root_agent(c)) is not None:
                return aid
    return None


def agent_caller(pid: int) -> str | None:
    """Why process ``pid`` is refused: it belongs to a brindle agent (it or an
    ancestor has BRINDLE_AGENT_ID in its environment, is an agent's CLI or
    brindle MCP server, or is one of a live agent's panes), or it can't be
    checked. None for anyone else's. Reads only ``pid``'s own ancestry, never
    the whole process table."""
    chain: set[int] = set()
    for p in _ancestry(pid, identity=True):
        if (aid := _launched_by(p)) is not None:
            return f"the caller runs under brindle agent {aid}"
        chain.add(p.pid)
    if not chain:
        # Gone already (a request sent before exiting) or not this user's:
        # unchecked is refused, or an agent could fire and exit.
        return "the caller can't be checked (it exited, or isn't this user's process)"
    owners = _pane_owners(chain)
    return next((f"the caller runs under brindle agent {aid}" for aid in sorted(owners)), None)


def _pane_owners(chain: set[int]) -> set[str]:
    """The agents one of whose pane processes is in ``chain``.

    A snapshot shows that a pane is an agent's even after it expires, but
    it shows that one isn't only while it is fresh: younger than PANE_TTL
    and taken since the last new agent pane (brindle.procs.agent_panes_changed).
    Otherwise this waits for a new snapshot, shared by every caller, and
    raises if it can't be taken, so the guard answers "unavailable" rather
    than letting the caller through.
    """
    global _pane_cache, _pane_refresh
    stamp = procs.agent_panes_stamp()
    with _pane_lock:
        if _pane_refresh is not None and _pane_refresh[1].done():
            (taken, finished), _pane_refresh = _pane_refresh, None
            if not finished.cancelled() and finished.exception() is None:
                _pane_cache = (time.monotonic(), taken, finished.result())
        if _pane_cache is not None:
            when, taken, panes = _pane_cache
            if owners := {aid for aid, pids in panes.items() if chain.intersection(pids)}:
                return owners
            if taken == stamp and time.monotonic() - when <= PANE_TTL:
                return set()
        if _pane_refresh is None or _pane_refresh[0] != stamp:
            _pane_refresh = (stamp, _pane_executor.submit(_agent_panes))
        pending = _pane_refresh
    panes = pending[1].result()
    with _pane_lock:
        if _pane_refresh is pending:
            _pane_cache = (time.monotonic(), pending[0], panes)
            _pane_refresh = None
    return {aid for aid, pids in panes.items() if chain.intersection(pids)}


def caller_agents(pid: int) -> set[str]:
    """The live agents (in brindle's database, with a tmux pane brindle owns)
    one of whose pane processes ``pid`` descends from: empty when none, or it
    can't be checked. Only what brindle itself knows counts, never what a
    process says of itself (its environment, its command line), which an
    agent could set to pass for another. A permission hook holds requests
    only for these (brindle.daemon.holds). Like :func:`agent_caller`, it
    reads only ``pid``'s own ancestry."""
    try:
        chain = {p.pid for p in _ancestry(pid, identity=False)}
        return _pane_owners(chain) if chain else set()
    except Exception:
        return set()
