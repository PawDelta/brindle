"""Starting, finding and stopping the daemon.

A start binds its sockets itself (so their permissions are brindle's, not the
server's defaults), makes a fresh token and writes ``~/.brindle/daemon.json``
(0600: socket, port, token, version, pid), which is how the app and
``daemon status`` / ``daemon stop`` find it. TCP binds 127.0.0.1 only. The
daemon exits on SIGTERM, after an upgrade of brindle's install (the app
starts the new one), and with ``--idle-exit`` once nobody has used it for
IDLE_SECONDS while no agent runs. It removes its files on the way out.

The app's daemon (``--idle-exit``) is a job of the person's launchd that the
app loaded, and launchd stops it when the app goes away (quit, force quit,
restarted into an update); the app replacing an older build's daemon stops
it the same way. With that job switched off the daemon is the app's own
child: the app stops it when it exits, and it stops itself once the app has
gone after a force quit (APP_PID_ENV). On its way out it pauses every session it runs
(``pause_for_exit``), as the person's Pause does, and the next daemon the app
starts resumes them (``resume_after_start``)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import resource
import signal
import socket
import time
from pathlib import Path

import brindle
from brindle import procs, spawn, upgrade
from brindle.config import brindle_home

HOST = "127.0.0.1"
IDLE_SECONDS = 600.0
WATCH_INTERVAL = 5.0     # seconds between the idle / upgrade checks
STOP_GRACE = 5.0         # seconds `daemon stop` waits before SIGKILL
SHUTDOWN_GRACE = 1.0     # seconds a stopping daemon waits for requests still running (the app kills it at 2)

log = logging.getLogger(__name__)


def _raise_file_limit() -> None:
    """Finder/launchd can give the daemon a soft limit of just 256 fds."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        # An unlimited hard limit needn't mean an unlimited daemon; some
        # kernels also reject RLIM_INFINITY as the soft limit.
        target = 65536 if hard == resource.RLIM_INFINITY else min(hard, 65536)
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (OSError, ValueError):
        log.warning("brindle: couldn't raise the daemon's file descriptor limit", exc_info=True)


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
    cmd = spawn.read(["ps", "-o", "command=", "-p", str(pid)], timeout=procs.PS_LIMIT).stdout
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

    @property
    def state(self):
        """The wrapped app's state (heavy_read_lane), as if unwrapped."""
        return self.app.state

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
        db = DB()
        try:
            return any(agents.is_alive(a, panes) for a in db.list_agents()
                       if a.status not in ("done", "paused"))
        finally:
            db.conn.close()
    except Exception:
        return True     # can't tell: stay up rather than strand an app


RESUME_FILE = "resume-on-start.json"    # the sessions the last app daemon paused on its way out


def _resume_path() -> Path:
    return brindle_home() / RESUME_FILE


def _record_resume(entries: list[dict]) -> None:
    """Replace the resume record with ``entries`` in one step: a reader sees
    the old record or the new one, never half of one."""
    path = _resume_path()
    staged = path.with_name(f"{path.name}.{os.getpid()}")
    try:
        staged.write_text(json.dumps(entries))
        os.replace(staged, path)
    except OSError:
        staged.unlink(missing_ok=True)
        log.warning("brindle: couldn't record the paused sessions for the next daemon", exc_info=True)


def pause_for_exit() -> list[str]:
    """The app's daemon on its way out: every running session on this
    daemon's own tmux server is paused as the person's Pause does
    (agents.pause: windows closed, work kept, so nothing keeps acting with
    nobody watching), for the next daemon to bring back (resume_after_start).
    A session on another tmux server (a demo's, a test's, another home's
    daemon's) is not this daemon's to pause: its windows aren't reachable from
    here, and its workspace's session name may name some other session here.

    The record of what is paused, with the server each session is on, is
    written before the first pause, so a pause cut short (a tmux failure,
    the daemon killed mid-way) still leaves its session to recover; each
    entry is marked paused once its pause has finished. Processes the panes
    hosted are left to a detached cull: the time to go is short. A session
    that can't be paused is only logged. Returns the roots paused."""
    from brindle import agents, tmux
    from brindle.db import DB
    from brindle.service import sessions as svc

    paused: list[str] = []
    try:
        db = DB()
        try:
            roots = [a for a in db.list_agents() if a.parent_id is None and a.mode == "interactive"
                     and a.status not in ("done", "paused") and agents.same_server(a)]
            if not roots:
                _resume_path().unlink(missing_ok=True)
                return paused
            server = tmux.current_server()
            entries = [{"id": root.id, "server": server, "paused": False} for root in roots]
            _record_resume(entries)
            for entry in entries:
                try:
                    agents.pause(db, entry["id"], stop_procs=False)
                except Exception:  # noqa: BLE001 - the others still get paused
                    log.warning("brindle: couldn't pause session %s on the way out", entry["id"], exc_info=True)
                    continue
                entry["paused"] = True
                paused.append(entry["id"])
                _record_resume(entries)
        finally:
            db.conn.close()
        if paused:
            svc.cull_detached()
    except Exception:  # noqa: BLE001 - leaving must not fail over this
        log.warning("brindle: couldn't pause the running sessions on the way out", exc_info=True)
    return paused


def _read_resume() -> list[dict] | None:
    """The resume record's entries, or None when there is none to read."""
    try:
        entries = json.loads(_resume_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str)]


def resume_after_start() -> list[str]:
    """Bring back the sessions the last app daemon paused on its way out
    (pause_for_exit), once: the record goes before any is resumed, so one
    that can't come back isn't tried at every start. Only roots still paused
    are resumed (the person may have closed one meanwhile), and only those on
    this daemon's own tmux server, recorded there and still there. One whose
    pause never finished is paused again first, its processes stopped, so
    what still runs of it isn't left beside the restarted session."""
    from brindle import agents, tmux
    from brindle.db import DB

    entries = _read_resume()
    if entries is None:
        return []
    _resume_path().unlink(missing_ok=True)
    server = tmux.current_server()
    resumed: list[str] = []
    db = DB()
    try:
        for entry in entries:
            root = db.get_agent(entry["id"])
            if root is None or root.status != "paused":
                continue
            if entry.get("server") != server or not agents.same_server(root):
                log.info("brindle: session %s is on another tmux server; not resumed here", root.id)
                continue
            try:
                if not entry.get("paused"):
                    agents.pause(db, root.id, stop_procs=True)
                agents.resume(db, root.id)
                resumed.append(root.id)
            except Exception:  # noqa: BLE001 - the others still come back
                log.warning("brindle: couldn't resume session %s", root.id, exc_info=True)
    finally:
        db.conn.close()
    return resumed


async def _elapse(seconds: float) -> None:
    """Return once ``seconds`` have passed on the event loop's timer."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    handle = loop.call_later(seconds, done.set_result, None)
    try:
        await done
    finally:
        handle.cancel()


APP_PID_ENV = "BRINDLE_APP_PID"     # the app that started this daemon as its child (daemon.rs start_child)


def _app_parent() -> int | None:
    """The pid of the app this daemon is a child of, from APP_PID_ENV, which
    is taken out of the environment so nothing the daemon starts inherits it."""
    value = os.environ.pop(APP_PID_ENV, "")
    return int(value) if value.isdigit() else None


async def _watch(server, activity: Activity, idle_exit: bool, *,
                 wait=_elapse, clock=time.monotonic, parent: int | None = None) -> None:
    """The daemon's periodic checks. ``parent``: the app whose child this
    daemon is (``_app_parent``); once it has gone (force quit: it had no
    chance to stop its daemon) the daemon shuts down, gracefully, so the
    sessions it ran are paused on its way out (``serve``)."""
    from brindle.daemon import health
    from brindle.daemon.session_review import ReadinessWatch
    from brindle.upgrade import InstallWatch

    install = InstallWatch()
    ready = ReadinessWatch()
    monitor = health.Monitor(clock)
    stale_servers = _StaleServerSweep()
    while not server.should_exit:
        due = clock() + WATCH_INTERVAL      # the wait's deadline, before any maintenance below
        await wait(WATCH_INTERVAL)
        if parent is not None and not procs.alive(parent):
            server.should_exit = True
            break
        try:
            monitor.tick(activity, due)     # the daemon's own health, into debuglog
        except Exception:  # noqa: BLE001 - health logging must never stop the watch
            log.debug("brindle: couldn't record the daemon's health", exc_info=True)
        with spawn.waits_for_room():                    # background work: it waits for room
            await asyncio.to_thread(ready.sweep_safely)     # session.pr_ready when a session becomes ready
            if stale_servers.due():
                await asyncio.to_thread(stale_servers.run)
        if install.changed():
            server.should_exit = True
        elif (idle_exit and activity.open == 0
              and time.monotonic() - activity.last >= IDLE_SECONDS
              and not await asyncio.to_thread(_agents_running)):
            server.should_exit = True


def _stop_stale_mcp_servers() -> bool:
    """upgrade.stop_stale_mcp_servers: whether to look again."""
    return upgrade.stop_stale_mcp_servers()


STALE_SWEEP_BACKOFF = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0)  # seconds to the next look after each failed one
STALE_SWEEP_GIVE_UP = 30 * 60.0     # seconds after the first failure to stop looking


class _StaleServerSweep:
    """Stops the MCP servers an update left on a replaced install, until none
    is left. A failed look (a slow ``ps`` while the machine is busy right after
    an update) is tried again with a growing gap, and given up 30 minutes after
    the first failure: giving up at once would leave every server the update
    stranded hanging until someone reconnects it. Only the first failure and
    the giving up are logged as warnings."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.pending = True
        self.failures = 0
        self.first_failure: float | None = None
        self.next_at = 0.0

    def due(self) -> bool:
        return self.pending and self.clock() >= self.next_at

    def run(self) -> None:
        try:
            self.pending = _stop_stale_mcp_servers()
        except Exception:
            now = self.clock()
            if self.first_failure is None:
                self.first_failure = now
                log.warning("brindle: couldn't look for MCP servers left on a replaced install; trying again",
                            exc_info=True)
            else:
                log.debug("brindle: still couldn't look for MCP servers left on a replaced install", exc_info=True)
            if now - self.first_failure >= STALE_SWEEP_GIVE_UP:
                self.pending = False
                log.warning("brindle: gave up looking for MCP servers left on a replaced install")
                return
            self.next_at = now + STALE_SWEEP_BACKOFF[min(self.failures, len(STALE_SWEEP_BACKOFF) - 1)]
            self.failures += 1


def _reset_window_sizes() -> None:
    """terminal.reset_window_sizes, off the daemon's way: a failure is only logged."""
    from brindle.daemon import terminal

    try:
        terminal.reset_window_sizes()
    except Exception:
        log.warning("brindle: couldn't reset the old daemon's window sizes", exc_info=True)


def _upgrade_sweep() -> None:
    """disk.upgrade_sweep (once per machine), off the daemon's way."""
    from brindle import disk
    from brindle.db import DB

    try:
        db = DB()
        try:
            disk.upgrade_sweep(db)
        finally:
            db.conn.close()
    except Exception:
        log.warning("brindle: the one-time disk sweep didn't run", exc_info=True)


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
        timeout_graceful_shutdown=SHUTDOWN_GRACE,
        http=with_peer(AutoHTTPProtocol),
        ws=with_peer(AutoWebSocketsProtocol) if AutoWebSocketsProtocol else "none"))


def serve(*, unix: bool = True, tcp: bool = False, idle_exit: bool = False) -> dict:
    """Run the daemon in this process until it's stopped. Returns its info."""
    from brindle.daemon.app import build_app
    from brindle.daemon.auth import new_token

    parent = _app_parent()      # before anything is started that would inherit it
    _raise_file_limit()
    token = new_token()
    socks, info = prepare(token, unix=unix, tcp=tcp)
    from brindle import spawn

    spawn.enable()      # every process the daemon starts waits for room (brindle.spawn)
    from brindle.daemon.events import start_poller

    poller = start_poller()
    from brindle import debuglog

    debuglog.Uploader().start()
    activity = Activity(build_app(token, port=info["port"], poller=poller))
    server = make_server(activity)
    upgrade.on_stale_launch = lambda: setattr(server, "should_exit", True)

    async def run() -> None:
        watcher = asyncio.create_task(_watch(server, activity, idle_exit, parent=parent))
        resizer = asyncio.create_task(asyncio.to_thread(_reset_window_sizes))
        sweeper = asyncio.create_task(asyncio.to_thread(_upgrade_sweep))
        # The app's daemon brings back what the last one paused on its way out.
        resumer = asyncio.create_task(asyncio.to_thread(resume_after_start)) if idle_exit else None
        try:
            await server.serve(sockets=socks)
        finally:
            watcher.cancel()
            resizer.cancel()
            sweeper.cancel()
            if resumer:
                resumer.cancel()

    # uvicorn shuts down gracefully on SIGTERM, then raises it again with the
    # handler it found: make that an exit that still runs the cleanup below.
    signal.signal(signal.SIGTERM, _exit)
    try:
        asyncio.run(run())
    finally:
        # The app's daemon, once it served: the app has gone (or replaced it), and
        # what it ran is paused for the next daemon. One that never served
        # (its start failed) runs nothing to pause.
        if idle_exit and server.started:
            pause_for_exit()
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
