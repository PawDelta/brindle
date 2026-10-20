"""Starting, finding and stopping the daemon.

A start binds its sockets itself (so their permissions are brindle's, not the
server's defaults), makes a fresh token and writes ``~/.brindle/daemon.json``
(0600: socket, port, token, version, pid), which is how the app and
``daemon status`` / ``daemon stop`` find it. TCP binds 127.0.0.1 only. The
daemon exits on SIGTERM, after an upgrade of brindle's install (the app
starts the new one), and with ``--idle-exit`` once nobody has used it for
IDLE_SECONDS while no agent runs. It removes its files on the way out."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import brindle
from brindle import procs
from brindle.config import brindle_home

HOST = "127.0.0.1"
IDLE_SECONDS = 600.0
WATCH_INTERVAL = 5.0     # seconds between the idle / upgrade checks
STOP_GRACE = 5.0         # seconds `daemon stop` waits before SIGKILL


class DaemonError(Exception):
    pass


def info_path() -> Path:
    return brindle_home() / "daemon.json"


def socket_path() -> Path:
    return brindle_home() / "daemon.sock"


def read_info() -> dict | None:
    try:
        info = json.loads(info_path().read_text())
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and isinstance(info.get("pid"), int) else None


def _is_daemon(pid: int) -> bool:
    """Whether ``pid`` is alive and still a brindle daemon (not a process
    that got the pid of one that died without cleaning up)."""
    if not procs.alive(pid):
        return False
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                         capture_output=True, text=True).stdout
    return "brindle" in cmd and "daemon" in cmd


def running() -> dict | None:
    """``daemon.json`` of the daemon that runs now, or None."""
    info = read_info()
    return info if info and _is_daemon(info["pid"]) else None


def _private_dir(d: Path) -> None:
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)


def bind_unix(path: Path) -> socket.socket:
    _private_dir(path.parent)
    path.unlink(missing_ok=True)    # a dead daemon's; a live one is refused before this
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(str(path))
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return sock


def bind_tcp(port: int = 0) -> socket.socket:
    # TCP has no OS user boundary: it must never reach past this machine.
    if not ipaddress.ip_address(HOST).is_loopback:
        raise DaemonError(f"refusing to listen on {HOST}: the daemon binds loopback only")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port))
    return sock


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def prepare(token: str, *, unix: bool = True, tcp: bool = False,
            sock_path: Path | None = None) -> tuple[list[socket.socket], dict]:
    """Bind the listening sockets and write ``daemon.json``. Refuses when a
    daemon already runs."""
    if not (unix or tcp):
        raise DaemonError("nothing to listen on: drop --no-socket or add --tcp")
    if (other := running()):
        raise DaemonError(f"a brindle daemon is already running (pid {other['pid']})")
    _private_dir(brindle_home())
    socks: list[socket.socket] = []
    info: dict = {"socket": None, "port": None, "token": token,
                  "version": brindle.__version__, "pid": os.getpid()}
    try:
        if unix:
            path = sock_path or socket_path()
            socks.append(bind_unix(path))
            info["socket"] = str(path)
        if tcp:
            socks.append(bind_tcp())
            info["port"] = socks[-1].getsockname()[1]
        _write_private(info_path(), json.dumps(info, indent=2) + "\n")
    except BaseException:
        for s in socks:
            s.close()
        raise
    return socks, info


def connection(info: dict | None) -> dict:
    """How a client reaches the daemon ``info`` describes (None: none runs),
    for ``brindle daemon --print-connection``. The token stays in
    ``daemon.json``: inside WSL, ``windows_info_path`` names that file as
    Windows sees it (``\\\\wsl$\\<distro>\\...``), where the Windows app reads
    the token before it calls ``url`` (WSL2 forwards localhost)."""
    path = info_path().absolute()     # BRINDLE_HOME may be relative; a UNC path can't be
    distro = os.environ.get("WSL_DISTRO_NAME")
    port = info.get("port") if info else None
    return {"running": info is not None,
            "pid": info["pid"] if info else None,
            "url": f"http://{HOST}:{port}" if port else None,
            "socket": info.get("socket") if info else None,
            "info_path": str(path),
            "windows_info_path": "\\\\wsl$\\" + distro + str(path).replace("/", "\\") if distro else None}


def cleanup(info: dict) -> None:
    """Remove this daemon's files, unless another daemon has replaced them."""
    current = read_info()
    if current and current.get("pid") == info["pid"]:
        info_path().unlink(missing_ok=True)
    if info.get("socket") and (current is None or current.get("pid") == info["pid"]):
        Path(info["socket"]).unlink(missing_ok=True)


class Activity:
    """ASGI wrapper that counts open connections and when the last one ended."""

    def __init__(self, app) -> None:
        self.app = app
        self.open = 0
        self.last = time.monotonic()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        self.open += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.open -= 1
            self.last = time.monotonic()


def _agents_running() -> bool:
    from brindle import agents, tmux
    from brindle.db import DB

    try:
        panes = tmux.list_panes()
        return any(agents.is_alive(a, panes) for a in DB().list_agents()
                   if a.status not in ("done", "paused"))
    except Exception:
        return True     # can't tell: stay up rather than strand an app


async def _watch(server, activity: Activity, idle_exit: bool) -> None:
    from brindle.daemon.session_review import ReadinessWatch
    from brindle.upgrade import InstallWatch

    install = InstallWatch()
    ready = ReadinessWatch()
    while not server.should_exit:
        await asyncio.sleep(WATCH_INTERVAL)
        await asyncio.to_thread(ready.sweep_safely)     # session.pr_ready when a session becomes ready
        if install.changed():
            server.should_exit = True
        elif (idle_exit and activity.open == 0
              and time.monotonic() - activity.last >= IDLE_SECONDS
              and not await asyncio.to_thread(_agents_running)):
            server.should_exit = True


def _exit(sig, frame) -> None:
    raise SystemExit(0)


def make_server(app):
    """The uvicorn server the daemon runs ``app`` with, on sockets handed to
    its ``serve``."""
    import uvicorn
    from uvicorn.protocols.http.auto import AutoHTTPProtocol
    from uvicorn.protocols.websockets.auto import AutoWebSocketsProtocol

    from brindle.daemon.peer import with_peer

    # with_peer: the guard learns who calls on the Unix socket (see brindle.daemon.peer).
    return uvicorn.Server(uvicorn.Config(
        app, lifespan="off", log_level="warning", access_log=False,
        http=with_peer(AutoHTTPProtocol),
        ws=with_peer(AutoWebSocketsProtocol) if AutoWebSocketsProtocol else "none"))


def serve(*, unix: bool = True, tcp: bool = False, idle_exit: bool = False) -> dict:
    """Run the daemon in this process until it's stopped. Returns its info."""
    from brindle.daemon.app import build_app
    from brindle.daemon.auth import new_token

    token = new_token()
    socks, info = prepare(token, unix=unix, tcp=tcp)
    from brindle.daemon.events import start_poller

    poller = start_poller()
    activity = Activity(build_app(token, port=info["port"], poller=poller))
    server = make_server(activity)

    async def run() -> None:
        watcher = asyncio.create_task(_watch(server, activity, idle_exit))
        try:
            await server.serve(sockets=socks)
        finally:
            watcher.cancel()

    # uvicorn shuts down gracefully on SIGTERM, then raises it again with the
    # handler it found: make that an exit that still runs the cleanup below.
    signal.signal(signal.SIGTERM, _exit)
    try:
        asyncio.run(run())
    finally:
        for s in socks:
            s.close()
        cleanup(info)
    return info


def stop() -> dict | None:
    """Stop the running daemon. Returns its info, or None if none ran (and
    clears a dead one's files)."""
    info = read_info()
    if info is None:
        return None
    if not _is_daemon(info["pid"]):
        cleanup(info)
        return None
    procs.terminate([info["pid"]], grace=STOP_GRACE)
    cleanup(info)
    return info
