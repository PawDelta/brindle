"""Starting, finding and stopping the daemon.

A start binds its sockets itself (so their permissions are brindle's, not the
server's defaults), makes a fresh token and writes ``~/.brindle/daemon.json``
(0600: socket, port, token, version, pid), which is how the app and
``daemon status`` / ``daemon stop`` find it. TCP binds 127.0.0.1 only. The
daemon exits on SIGTERM, after an upgrade of brindle's install (the app
starts the new one), and with ``--idle-exit`` once nobody has used it for
IDLE_SECONDS while no agent runs. It removes its files on the way out.

The app's daemon (``--idle-exit``) is a job of the person's launchd that the
app loaded. It outlives the app (quit, force quit, restarted into an update):
the sessions it runs carry on, and the next app adopts it when its build matches.
Reopening the app onto a surviving daemon neither pauses sessions nor consumes
a resume record. It stops when an app of a newer build replaces it, at
``brindle daemon stop`` (``stop``), at logout, once its install has changed,
or once idle. With that job switched off the daemon is the app's own child:
the app stops it when it exits, and it stops itself once the app has gone
after a force quit (APP_PID_ENV). On its way out it pauses every session it runs
(``pause_for_exit``), as the person's Pause does, saying so meanwhile
(``say_pausing``: only then does whoever stops it wait more than a moment for
it to go), and the next daemon the app starts resumes them (``resume_after_start``).
Nothing else holds up its exit, not a second stop (``serve``, ``_run``, ``leave``)."""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import ipaddress
import json
import logging
import os
import resource
import signal
import socket
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

import brindle
from brindle import debuglog, procs, spawn, upgrade
from brindle.config import brindle_home

HOST = "127.0.0.1"
IDLE_SECONDS = 600.0
WATCH_INTERVAL = 5.0     # seconds between the idle / upgrade checks
CODEX_HOOK_EVERY = 60.0  # seconds between looks at the Codex hook's trust (codex_hook.keep_trusted)
SHARED_DOCS_EVERY = 30.0  # seconds between bringing back what teammates wrote on shared plans
CODEX_HOOK_SETTLE = 120.0    # and how long another install's fresh trust is left alone
STOP_GRACE = 5.0         # seconds `daemon stop` waits before SIGKILL
SHUTDOWN_GRACE = 1.0     # seconds for requests on shutdown, within the app's replacement budget

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
    _pausing_path().unlink(missing_ok=True)     # a daemon killed while it paused said so last
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


RESUME_FILE = "resume-on-start.json"    # sessions from the last daemon shutdown, not the last app quit


def _resume_path() -> Path:
    return brindle_home() / RESUME_FILE


def _record_resume(entries: list[dict]) -> bool:
    """Replace the resume record with ``entries`` in one step: a reader sees
    the old record or the new one, never half of one. False when it couldn't
    be written: the old record, if any, is left as it was."""
    path = _resume_path()
    staged = path.with_name(f"{path.name}.{os.getpid()}")
    try:
        staged.write_text(json.dumps(entries))
        os.replace(staged, path)
    except OSError:
        staged.unlink(missing_ok=True)
        log.warning("brindle: couldn't record the paused sessions for the next daemon", exc_info=True)
        return False
    return True


PAUSING_FILE = "daemon-pausing"     # the pid of the app daemon pausing on its way out (daemon.rs PAUSING_FILE)
PAUSE_BUDGET = 10.0     # seconds pause_for_exit may take (its stopper waits up to PAUSE_GRACE while it says it's pausing)


def _pausing_path() -> Path:
    return brindle_home() / PAUSING_FILE


def say_pausing() -> None:
    """Tell whoever stops this daemon (the app, daemon.rs stop_daemon;
    ``brindle daemon stop``, stop) to give it time: it is on its way to
    pausing its sessions (pause_for_exit). Without this they kill it after a
    short wait."""
    try:
        _write_private(_pausing_path(), f"{os.getpid()}\n")
    except OSError:
        log.warning("brindle: couldn't say the daemon is pausing", exc_info=True)


def done_pausing() -> None:
    """The pause is over: the app's short wait applies again."""
    try:
        if _pausing_path().read_text().strip() == str(os.getpid()):
            _pausing_path().unlink()
    except OSError:
        pass


def pause_for_exit(recovery: Recovery | None = None) -> list[str]:
    """The app's daemon on its way out: every running session on this
    daemon's own tmux server is paused as the person's Pause does
    (agents.pause: windows closed, work kept), for the next daemon to bring
    back (resume_after_start). An app quitting while this daemon survives
    does not enter this path.
    A session on another tmux server (a demo's, a test's, another home's
    daemon's) is not this daemon's to pause: its windows aren't reachable from
    here, and its workspace's session name may name some other session here.

    The record of what is paused, with the server each session is on, is
    written before the pause, so a pause cut short (a tmux failure, the
    daemon killed mid-way) still leaves its sessions to recover: each stays
    on the record, with its count of tries, until the next start's reconcile
    with tmux confirms it, so whatever this pause leaves open is settled
    there. No record, no pause: a session paused with nothing to bring it back would stay
    paused, so one that can't be recorded is left running. Processes the
    panes hosted are left to a detached cull: the time to go is short (the
    next start stops what the cull left, resume_after_start). A pause that
    fails is only logged. Returns the roots paused.

    Whoever stops the daemon waits for this (daemon.rs PAUSE_GRACE, stop's), so it is quick and has
    an end: the sessions are paused all at once (agents.record_pauses, then
    agents.close_paused: a few tmux commands in all, not a few per
    session), and what isn't closed after PAUSE_BUDGET seconds (a tmux
    server that stopped answering) is left as a pause cut short.

    ``recovery``: this daemon's own start bringing sessions back
    (resume_after_start), stopped first: nothing is launched past this
    pause, and what it hadn't brought back yet is recorded again with the
    rest."""
    from brindle import agents, tmux
    from brindle.db import DB
    from brindle.service import sessions as svc

    try:
        # What is still to be brought back, by this daemon's start or the next: recorded again.
        unresumed = (recovery.stop() if recovery else []) + (_read_resume() or [])
        db = DB()
        try:
            roots = [a for a in db.list_agents() if a.parent_id is None and a.mode == "interactive"
                     and a.status not in ("done", "paused") and agents.same_server(a)]
            server = tmux.current_server()
            left, seen = [], {root.id for root in roots}
            tries = {e["id"]: e.get("tries") for e in reversed(unresumed)}      # stopping is no fresh start for them

            def recorded(session: str) -> dict:
                return {"id": session, "server": server, **({"tries": tries[session]} if tries.get(session) else {})}

            for entry in unresumed:
                root = db.get_agent(entry["id"])
                if (entry["id"] not in seen and entry.get("server") == server
                        and root is not None and root.status == "paused"):
                    left.append(recorded(entry["id"]))
                    seen.add(entry["id"])
            if not roots and not left:
                _resume_path().unlink(missing_ok=True)
                return []
            entries = [recorded(root.id) for root in roots]
            if not _record_resume(left + entries) or not roots:
                return []
            _record_window_size()       # before the windows close; after the record, which matters more
            closings = agents.record_pauses(db, [entry["id"] for entry in entries])
        finally:
            db.conn.close()
        finished = threading.Event()

        def close() -> None:
            try:
                db = DB()       # its own: SQLite connections stay on their thread
                try:
                    agents.close_paused(db, closings)
                finally:
                    db.conn.close()
                finished.set()
            except Exception:  # noqa: BLE001 - the next start finishes it
                log.warning("brindle: couldn't pause the running sessions on the way out", exc_info=True)

        worker = threading.Thread(target=close, name="brindle-pause-for-exit", daemon=True)
        worker.start()
        worker.join(PAUSE_BUDGET)
        if not finished.is_set():
            if worker.is_alive():
                log.warning("brindle: the sessions weren't paused within %gs; the next start finishes it",
                            PAUSE_BUDGET)
            return []
        svc.cull_detached()
        return [entry["id"] for entry in entries]
    except Exception:  # noqa: BLE001 - leaving must not fail over this
        log.warning("brindle: couldn't pause the running sessions on the way out", exc_info=True)
        return []


def _read_resume() -> list[dict] | None:
    """The resume record's entries, or None when there is none to read."""
    try:
        entries = json.loads(_resume_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str)]


WINDOW_SIZE_FILE = "agent-window-size"      # the size recovered windows start at (_record_window_size)


def _window_size_path() -> Path:
    return brindle_home() / WINDOW_SIZE_FILE


def _record_window_size() -> None:
    """The size the agents' windows were last shown at, for the next start:
    no view is attached while it brings them back and the daemon has no
    terminal, so they would come back at tmux's 80x24 and the resumed chats
    draw there. Kept when nothing can be read now. A hint: failing is only logged."""
    from brindle import tmux

    try:
        size = tmux.agent_window_size()
        if size:
            _write_private(_window_size_path(), json.dumps(size))
    except (tmux.TmuxError, OSError):
        log.warning("brindle: couldn't record the agents' window size", exc_info=True)


def _read_window_size() -> tuple[int, int] | None:
    """What _record_window_size kept, or None when there is nothing usable."""
    try:
        cols, rows = json.loads(_window_size_path().read_text())
    except (OSError, ValueError, TypeError):
        return None
    return (cols, rows) if isinstance(cols, int) and isinstance(rows, int) and cols > 0 and rows > 0 else None


VIEWED_FILE = "viewed-session"      # the session the app last showed (record_viewed)


def _viewed_path() -> Path:
    return brindle_home() / VIEWED_FILE


def record_viewed(root_id: str) -> None:
    """The app is showing session ``root_id``: the next start brings it back
    first, alone (resume_after_start). Replaced in one step, through a staging
    file of its own, so a reader sees one session or the other, never half."""
    path = _viewed_path()
    try:
        fd, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    except OSError:
        log.warning("brindle: couldn't record the session on screen", exc_info=True)
        return
    try:
        with os.fdopen(fd, "w") as f:
            f.write(root_id)
        os.replace(staged, path)
    except OSError:
        Path(staged).unlink(missing_ok=True)
        log.warning("brindle: couldn't record the session on screen", exc_info=True)


def _read_viewed() -> str | None:
    """The session the app last showed, or None when none is recorded or it
    can't be read (a hint only: recovery goes on without it)."""
    try:
        return _viewed_path().read_text().strip() or None
    except (OSError, ValueError):
        return None


RESUME_WAIT = 5.0     # seconds the way out waits for a start's resume still under way (Recovery)
LAUNCH_WAIT = 8.0      # and in all, launches under way included (within daemon.rs PAUSE_GRACE, with PAUSE_BUDGET)
RESUME_STALL = 120.0   # seconds with no session finished after which a start stops waiting (a resume that hangs)
RESUME_TRIES = 2       # starts that may fail to bring one session back before it is left paused
RESUME_PARALLEL = 4    # sessions a start brings back at once (resume_after_start)
RESUME_BUSY_WAIT = 3.0  # seconds a session other than the person's own waits on a machine short of CPU only
RESUME_PROMPT_WAIT = 90.0   # seconds to wait for Claude's input box after its pane is launched


class Recovery:
    """One daemon's resume_after_start as its own pause_for_exit sees it: the
    daemon may be stopped while it still brings sessions back. Once it is
    stopping, no further session is launched; the way out waits (up to
    RESUME_WAIT) for those under way and leaves the rest for the next start.

    Every launch of a resume is counted in ``launches``, under ``launch_lock``
    (launching, the gate agents.resume holds from a launch's first change of
    what runs to its end, not over its preflight), and stop waits for that
    count to reach none once ``stopping`` is set: sessions launch side by
    side, the launches under way finish before stop returns, and none begins
    after, however long a resume was held up, so the way out's pause sees
    every session launched. Only a launch that hangs (LAUNCH_WAIT) is left
    behind, and whatever it opens is the next start's to reconcile.

    The record stays on disk throughout: a session leaves it only once its
    reconcile has confirmed its panes (settled), so a daemon that dies at any
    point leaves the next start what is still to do. ``pending`` is those
    whose turn in this start isn't over, ``lanes`` those not yet begun."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.launch_lock = threading.Lock()
        self.launches = 0               # launches under way, under launch_lock
        self.launch_ended = threading.Condition(self.launch_lock)
        self.stopping = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self.record: list[dict] = []    # what is on disk: sessions not confirmed back yet
        self.pending: list[dict] = []   # those whose turn in this start isn't over
        self.returning: set[str] = set()  # eligible sessions queued or launching in this start
        self.lanes: deque[list[dict]] = deque()     # those whose turn hasn't begun, under lock
        self.total = 0                  # how many entries the record had
        self.handed_over = False        # stop has returned: the record is the way out's now
        self.ordered = False            # ``lanes`` is in place: a prioritize moves them at once
        self.wanted: list[str] = []     # sessions opened before then, in order, under lock

    def _save(self) -> bool:
        if self.handed_over:
            return False
        if self.record:
            return _record_resume(self.record)
        _resume_path().unlink(missing_ok=True)
        return True

    def trying(self, entry: dict) -> bool:
        """``entry``'s session is about to be brought back: counted on disk
        first, or it isn't (False), lest it kill the daemon at every start."""
        with self.lock:
            entry["tries"] = entry.get("tries", 0) + 1
            return self._save()

    def settled(self, entry: dict) -> None:
        """``entry``'s session is as it should be: nothing is left to do for it."""
        with self.lock:
            self.record.remove(entry)
            self._save()

    def finished(self, entry: dict) -> None:
        """``entry``'s turn in this start is over, back or not."""
        with self.lock:
            self.pending.remove(entry)
            self.returning.discard(entry["id"])

    def resuming(self) -> set[str]:
        """Sessions this start is bringing back, until their attempt ends."""
        with self.lock:
            return set() if self.idle.is_set() or self.stopping.is_set() else set(self.returning)

    def next_lane(self) -> list[dict] | None:
        with self.lock:
            return self.lanes.popleft() if self.lanes else None

    def prioritize(self, root_id: str) -> None:
        """Bring session ``root_id``'s lane to the front of those not yet begun
        (the person has opened it). Nothing, if it has begun or none is under way.
        Opened before the start has put its sessions in order, it goes first
        once it has (order)."""
        with self.lock:
            if not self.ordered:
                self.wanted.append(root_id)
                return
            self._forward(root_id)

    def _forward(self, root_id: str) -> list[dict] | None:
        for lane in self.lanes:
            if any(e["id"] == root_id for e in lane):
                self.lanes.remove(lane)
                self.lanes.appendleft(lane)
                return lane
        return None

    def order(self, lanes: deque[list[dict]]) -> None:
        """The start's ``lanes`` in place, behind any session opened meanwhile."""
        with self.lock:
            self.lanes, self.ordered = lanes, True
            for root_id in self.wanted:
                self._forward(root_id)

    def progress(self) -> dict | None:
        """How far the start's resume has come (the app's "3 of 6"): sessions
        finished with, whether they came back or not, and those tmux hasn't yet
        confirmed back. None when none is under way."""
        with self.lock:
            if self.idle.is_set() or not self.total:
                return None
            return {"done": self.total - len(self.pending), "total": self.total,
                    "pending": [e["id"] for e in self.record]}

    @contextmanager
    def launching(self):
        """One launch of a resume: never once the daemon is stopping
        (agents.Cancelled instead)."""
        from brindle import agents

        with self.launch_lock:
            if self.stopping.is_set():
                raise agents.Cancelled("the daemon is stopping")
            self.launches += 1
        try:
            yield
        finally:
            with self.launch_lock:
                self.launches -= 1
                self.launch_ended.notify_all()

    def stop(self) -> list[dict]:
        """Stop resuming; once those under way are done (or RESUME_WAIT has
        passed, and LAUNCH_WAIT for a launch that hangs), the entries still to
        resume. Nothing is launched after it returns."""
        began = time.monotonic()
        self.stopping.set()
        if not self.idle.wait(RESUME_WAIT):
            log.warning("brindle: a session still being resumed after %gs on the way out; "
                        "the next start resumes it again", RESUME_WAIT)
        with self.launch_lock:      # the launches under way end first; any later one sees stopping
            if not self.launch_ended.wait_for(lambda: self.launches == 0,
                                              max(0.0, began + LAUNCH_WAIT - time.monotonic())):
                log.warning("brindle: a launch still under way after %gs on the way out", LAUNCH_WAIT)
        with self.lock:
            self.handed_over = True
            return list(self.record)


def _most_recent_first(db, entries: list[dict]) -> list[dict]:
    """``entries``, the session last at work first. Not by status_since: the
    pause that recorded them set that, all at the same moment. What happened
    before the pause is in the event bus; a session whose events have been
    pruned hasn't done anything lately, and goes by when it was started."""
    def active(entry: dict) -> float:
        root = db.get_agent(entry["id"])
        if root is None:
            return 0.0
        return db.last_activity(root.id, root.status_since or time.time()) or root.created_at

    return sorted(entries, key=active, reverse=True)


def _lanes(db, entries: list[dict]) -> deque[list[dict]]:
    """``entries`` (in order) as work for resume_after_start's threads, one
    lane per tmux session: sessions sharing one come back one after another,
    as the first launch creates it (tmux.ensure_session)."""
    lanes: dict[str, list[dict]] = {}
    for entry in entries:
        root = db.get_agent(entry["id"])
        ws = db.get_workspace(root.workspace_id) if root else None
        lanes.setdefault(ws.tmux_session if ws else entry["id"], []).append(entry)
    return deque(lanes.values())


@contextmanager
def _timed(steps: list[str], step: str):
    """Note in ``steps`` how long ``step`` took and how it ended, however it
    ended: the step that fails may well be the slow one."""
    from brindle import agents

    began, outcome = time.monotonic(), "failed"
    try:
        yield
        outcome = "ok"
    except agents.Cancelled:
        outcome = "cancelled"
        raise
    finally:
        steps.append(f"{step} {time.monotonic() - began:.2f}s ({outcome})")


def _resume_session(db, entry: dict, server: str | None, recovery: Recovery, env: dict[str, str],
                    *, waited: float = 0.0) -> bool:
    """One idempotent reconcile of ``entry``'s session against tmux, on its
    thread's own ``db``, safe to run again after a crash at any point: what
    runs is left, what shouldn't is closed, what is missing is launched, and
    the session leaves the record only once tmux confirms it. Whether it was
    brought back. Raises agents.Cancelled once the daemon is stopping. Under
    the session's resume lock, against a sign-in sweep's or the person's resume."""
    from brindle import agents, signin_pause

    with agents.resume_lock(entry["id"]):
        root = db.get_agent(entry["id"])
        if root is None or root.status == "done":       # closed meanwhile (failed isn't: its pane may well be alive)
            recovery.settled(entry)
            return False
        if entry.get("server") != server or not agents.same_server(root):
            log.info("brindle: session %s is on another tmux server; not resumed here", root.id)
            recovery.settled(entry)
            return False
        tree = agents.tree(db, root.id)
        # What the sign-in sweep holds is its own: it alone knows the account each may come back under.
        held = set(signin_pause.paused_for_signin())
        if entry.get("tries", 0) > RESUME_TRIES:    # nothing more is launched, and it isn't left half-running
            if not agents.panes_match(db, root.id, held):
                log.warning("brindle: session %s isn't resumed: tried %d times", root.id, RESUME_TRIES)
                agents.settle_panes(db, [a for a in tree if a.status != "done"], keep=False)
            recovery.settled(entry)
            return False
        steps: list[str] = [f"room {waited:.2f}s"]     # how long it waited for the machine to have room
        size = _read_window_size()
        try:
            with _timed(steps, "resume"):
                agents.resume(db, root.id, only={a.id for a in tree} - held, settle=True,
                              gate=recovery.launching, recovery_env=env, **({"window_size": size} if size else {}))
        finally:
            # A warning, so it's seen: the daemon's log is its stderr (app-daemon.log), warnings only.
            debuglog.recovery_timing(log, "brindle: session %s recovery: %s", root.id, ", ".join(steps))
        if agents.panes_match(db, root.id, held):
            recovery.settled(entry)
        return True


def _await_resume_prompt(db, root_id: str, recovery: Recovery) -> None:
    """Keep the recovery display until Claude can take input. SessionStart
    marks it idle before a large conversation has finished loading; neither
    that status nor a live pane means its prompt is up. Read the pane using
    the provider's existing input detection, outside the resume/launch locks.
    If it never draws (or can't be read), expose the real status at the bound.
    Other providers and headless sessions keep their existing readiness path.
    """
    from brindle import agents, providers, tmux

    until = time.monotonic() + RESUME_PROMPT_WAIT
    while not recovery.stopping.is_set():
        root = db.get_agent(root_id)
        if (root is None or root.provider != "claude" or root.headless or root.mode != "interactive"
                or root.status not in ("starting", "idle", "processing", "waiting") or not root.tmux_window):
            return
        left = until - time.monotonic()
        if left <= 0:
            return
        try:
            screen = tmux.capture(root.tmux_window, lines=40, server=agents.server_of(root))
            if providers.get_provider(root.provider).input_ready(screen):
                return
        except tmux.TmuxError:
            pass   # a transient capture failure must not announce that it is ready
        recovery.stopping.wait(min(left, 0.5))


def _join(threads: list[threading.Thread], recovery: Recovery) -> None:
    """Wait for ``threads``, or until no session has finished for RESUME_STALL
    seconds: what hangs mustn't keep the app waiting or its progress up for
    good. Hung sessions stay on the record for the next start."""
    finished, since = -1, 0.0
    while alive := [t for t in threads if t.is_alive()]:
        left = len(recovery.pending)    # only a count, as it stands: not worth holding the work up for
        if left != finished:
            finished, since = left, time.monotonic()
        wait = since + RESUME_STALL - time.monotonic()
        if wait <= 0:
            log.warning("brindle: no session came back for %gs; %d left to the next start", RESUME_STALL, left)
            return
        alive[0].join(min(wait, 1.0))


def resume_after_start(recovery: Recovery | None = None) -> list[str]:
    """Recover sessions before optional timing authorization can contact the backend."""
    with debuglog.recovery_timings():
        return _resume_after_start(recovery)


def _resume_after_start(recovery: Recovery | None = None) -> list[str]:
    """Bring back the sessions on the record (pause_for_exit), each by one
    reconcile with tmux (_resume_session). A session stays on the record until
    tmux confirms it, so a start that dies leaves the rest to the next; each is
    counted as tried first, so one that can't come back isn't tried at every
    start. None that the person has closed meanwhile is resumed, and only those
    on this daemon's own tmux server, recorded there and still there.

    Up to RESUME_PARALLEL sessions come back at once, the one last at work
    first (_most_recent_first), each when the machine has room for it
    (spawn.wait_for_room): for the person's own (interactive) sessions,
    only critical memory pressure holds them, a machine short of CPU doesn't
    (the person is waiting for them; workers keep their gate, spawn.no_room);
    sessions sharing a tmux session go one after
    another (_lanes). A session that fails doesn't hold up the others.
    The session the app showed last (record_viewed) is taken first, and the
    others come back beside it: N sessions take about as long as the slowest.
    Their launches share each model check and preflight per provider and
    identity (agents.shared_launch_checks), rather than run them per agent.

    ``recovery``: the daemon's (serve), which may be stopped meanwhile: then
    nothing more is launched, and what is left stays on the record."""
    from brindle import agents, bus, secrets, signin_pause, tmux
    from brindle.db import DB

    recovery = recovery or Recovery()
    with recovery.lock:
        if recovery.stopping.is_set():
            return []       # stopped before it began: the record stays as it is
        entries = _read_resume()
        if not entries:
            return []
        recovery.record, recovery.pending = list(entries), list(entries)
        recovery.total = len(entries)
        recovery.idle.clear()
    began = time.monotonic()
    resumed: list[str] = []
    ordered = 0.0
    try:
        server = tmux.current_server()
        # The environment as this start found it, for every session: each resume writes the
        # credentials it chose to os.environ, which must not decide the next one's.
        env = {k: v for k, v in os.environ.items() if v and not secrets.is_job_secret(k)}
        db = DB()
        try:
            held = set(signin_pause.paused_for_signin())
            with recovery.lock:
                recovery.returning = {
                    entry["id"] for entry in entries
                    if entry.get("tries", 0) < RESUME_TRIES and entry.get("server") == server
                    and (root := db.get_agent(entry["id"])) is not None
                    and root.status != "done" and agents.same_server(root) and root.id not in held
                }
            lanes = _lanes(db, _most_recent_first(db, entries))
            for root_id in recovery.resuming():
                bus.publish_agent(db, "session.changed", root_id)
        finally:
            db.conn.close()
        # The session the app showed last is taken first, before its lane's others.
        viewed = _read_viewed()
        first = next((lane for lane in lanes if any(e["id"] == viewed for e in lane)), None)
        if first is not None:
            lanes.remove(first)
            first.sort(key=lambda e: e["id"] != viewed)
            lanes.appendleft(first)
        recovery.order(lanes)     # behind any session the person opened meanwhile
        ordered = time.monotonic() - began

        def work() -> None:
            db = DB()       # its own: SQLite connections stay on their thread
            try:
                while not recovery.stopping.is_set():
                    lane = recovery.next_lane()
                    if lane is None:
                        return
                    for entry in lane:
                        waiting = time.monotonic()
                        root = db.get_agent(entry["id"])
                        # The person's own session: they are waiting for it, so only critical memory holds it.
                        busy = 0.0 if root is not None and root.mode == "interactive" else RESUME_BUSY_WAIT
                        spawn.wait_for_room(recovery.stopping, busy_limit=busy)
                        waited = time.monotonic() - waiting
                        if recovery.stopping.is_set():
                            return
                        try:
                            if not recovery.trying(entry):
                                log.warning("brindle: session %s couldn't be noted as tried; left to the next start",
                                            entry["id"])
                            elif _resume_session(db, entry, server, recovery, env, waited=waited):
                                _await_resume_prompt(db, entry["id"], recovery)
                                resumed.append(entry["id"])
                        except agents.Cancelled:
                            entry["tries"] -= 1     # stopped, it didn't fail
                            return      # launched now, it would run on past the way out's pause: left pending
                        except Exception:  # noqa: BLE001 - the others still come back
                            log.warning("brindle: couldn't resume session %s", entry["id"], exc_info=True)
                        recovery.finished(entry)
                        bus.publish_agent(db, "session.changed", entry["id"])
            finally:
                db.conn.close()

        with agents.shared_launch_checks():
            # A copy of this context each (one can't be entered twice at once): a new thread starts with none.
            threads = [threading.Thread(target=contextvars.copy_context().run, args=(work,),
                                        name=f"brindle-resume-{n}", daemon=True)
                       for n in range(min(RESUME_PARALLEL, len(lanes)))]
            for thread in threads:
                thread.start()
            _join(threads, recovery)
    finally:
        recovery.idle.set()
        debuglog.recovery_timing(log, "brindle: recovery of %d session(s) took %.2fs (%d back; ordering %.2fs)",
                                len(entries), time.monotonic() - began, len(resumed), ordered)
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
    hook_due = clock() + CODEX_HOOK_EVERY
    shared_due = clock() + SHARED_DOCS_EVERY
    sharing: asyncio.Task | None = None     # on its own task: a slow Library never holds up these checks
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
        await asyncio.to_thread(_purge_typed_words)
        if clock() >= hook_due:
            hook_due = clock() + CODEX_HOOK_EVERY
            await asyncio.to_thread(_keep_codex_hook, CODEX_HOOK_SETTLE)
        if clock() >= shared_due and (sharing is None or sharing.done()):
            shared_due = clock() + SHARED_DOCS_EVERY
            sharing = asyncio.create_task(asyncio.to_thread(_sync_shared_docs))
        if install.changed():
            server.should_exit = True
        elif (idle_exit and activity.open == 0
              and time.monotonic() - activity.last >= IDLE_SECONDS
              and not await asyncio.to_thread(_agents_running)):
            server.should_exit = True


def _purge_typed_words() -> None:
    """visual_offers.purge_words, off the daemon's way: a failure is only logged."""
    from brindle import visual_offers
    from brindle.db import DB

    try:
        db = DB()
        try:
            visual_offers.purge_words(db)
        finally:
            db.conn.close()
    except Exception:  # noqa: BLE001 - a purge must never stop the watch
        log.debug("brindle: couldn't purge old typed words", exc_info=True)


def _sync_shared_docs() -> None:
    """shared_docs.sync_open, off the daemon's way: a failure is only logged."""
    from brindle.db import DB
    from brindle.pro import shared_docs

    try:
        db = DB()
        try:
            shared_docs.sync_open(db)
        finally:
            db.conn.close()
    except Exception:  # noqa: BLE001 - a sync must never stop the watch
        log.debug("brindle: couldn't sync shared plans", exc_info=True)
    _poll_review_queue()


def _poll_review_queue() -> None:
    """review_queue.poll for the signed-in account; a failure is only logged."""
    from brindle.pro import review_queue

    try:
        review_queue.poll()
    except Exception:  # noqa: BLE001 - a poll must never stop the watch
        log.debug("brindle: couldn't poll the review queue", exc_info=True)


def _reset_window_sizes() -> None:
    """terminal.reset_window_sizes, off the daemon's way: a failure is only logged."""
    from brindle.daemon import terminal

    try:
        terminal.reset_window_sizes()
    except Exception:
        log.warning("brindle: couldn't reset the old daemon's window sizes", exc_info=True)


def _keep_codex_hook(settle: float = 0.0) -> None:
    """codex_hook.keep_trusted, off the daemon's way, by the one daemon this
    brindle home answers to: one left running from another install (an app on
    its disk image) would otherwise take the hook's one slot back and forth with it."""
    from brindle import codex_hook

    info = read_info()
    if info is None or info["pid"] != os.getpid():
        return
    with spawn.waits_for_room():    # background work: it waits for room
        codex_hook.keep_trusted(settle)


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


def start_live_docs(state):
    """The shared docs' relay links (pro.live_docs), on the running loop, where
    the local live socket finds them. None if they couldn't start: shared docs
    still edit together on this machine."""
    from brindle.pro import live_docs

    try:
        manager = live_docs.Manager()
        manager.start()
    except Exception:  # noqa: BLE001 - co-editing between machines is never worth the daemon
        log.warning("brindle: the live relay links didn't start", exc_info=True)
        return None
    state.live_docs = manager
    return manager


async def stop_live_docs(manager) -> None:
    """Close every relay link cleanly on the way out: a clean close is not an
    author dropping mid-edit. Unsent edits wait in the outbox for the next daemon."""
    if manager is None:
        return
    try:
        await manager.stop()
    except Exception:  # noqa: BLE001
        log.warning("brindle: the live relay links didn't close cleanly", exc_info=True)


def _exit(sig, frame) -> None:
    raise SystemExit(0)


def make_server(app):
    """The uvicorn server the daemon runs ``app`` with, on sockets handed to
    its ``serve``."""
    import uvicorn
    from uvicorn.protocols.http.auto import AutoHTTPProtocol
    from uvicorn.protocols.websockets.auto import AutoWebSocketsProtocol

    from brindle.daemon.peer import with_peer

    class Server(uvicorn.Server):
        stopping = None     # called as it starts to shut down, however it was told to

        async def shutdown(self, sockets=None) -> None:
            if self.stopping is not None:
                self.stopping()
            await super().shutdown(sockets)

    # with_peer: the guard learns who calls on the Unix socket (see brindle.daemon.peer).
    return Server(uvicorn.Config(
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
    from brindle import login_shell_env

    # The person's proxy and certificate settings, read once, for the CLIs this daemon launches.
    login_shell_env.load_in_background()
    from brindle.daemon.events import start_poller

    poller = start_poller()
    from brindle import debuglog

    debuglog.Uploader().start()
    activity = Activity(build_app(token, port=info["port"], poller=poller))
    activity.state.resuming = idle_exit
    server = make_server(activity)
    recovery = Recovery()
    activity.state.recovery = recovery      # /v1/meta reports its progress to the app
    if idle_exit:
        def stopping() -> None:
            recovery.stopping.set()     # nothing more is resumed: it would outlive the pause
            say_pausing()               # the app's daemon pauses what it runs on its way out: the app gives it time

        server.stopping = stopping
    upgrade.on_stale_launch = lambda: setattr(server, "should_exit", True)

    async def run() -> None:
        async def resume() -> None:
            try:
                await asyncio.to_thread(resume_after_start, recovery)
            finally:
                activity.state.recovered_at = time.time()
                activity.state.resuming = False

        watcher = asyncio.create_task(_watch(server, activity, idle_exit, parent=parent))
        resizer = asyncio.create_task(asyncio.to_thread(_reset_window_sizes))
        sweeper = asyncio.create_task(asyncio.to_thread(_upgrade_sweep))
        hook = asyncio.create_task(asyncio.to_thread(_keep_codex_hook))   # an update may have changed it
        # The app's daemon brings back what the last one paused on its way out.
        resumer = asyncio.create_task(resume()) if idle_exit else None
        live = start_live_docs(activity.state)
        try:
            await server.serve(sockets=socks)
        finally:
            watcher.cancel()
            resizer.cancel()
            sweeper.cancel()
            hook.cancel()
            if resumer:
                resumer.cancel()
            await stop_live_docs(live)

    # uvicorn shuts down gracefully on SIGTERM, then raises it again with the
    # handler it found: make that an exit that still runs the cleanup below.
    signal.signal(signal.SIGTERM, _exit)
    try:
        _run(run())
    finally:
        # On its way out now: a further SIGTERM (the app's daemon outlives the app, so the
        # next app, `brindle daemon stop` and a logout may each stop it) would cut the pause
        # and the cleanup short. Whoever stops it kills it if it overstays (stop, daemon.rs).
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # The app's daemon, once it served: stopped (a newer app replacing it, `brindle
        # daemon stop`, a logout, or a child losing its app) or exiting by itself
        # (idle, its install changed). What it ran is paused for the next daemon. One that never served
        # (its start failed) runs nothing to pause.
        try:
            if idle_exit and server.started:
                pause_for_exit(recovery)
        finally:
            done_pausing()
        for s in socks:
            s.close()
        cleanup(info)
    return info


def _run(main) -> None:
    """asyncio.run(``main``) without its last step, which waits for every
    thread asyncio.to_thread started to finish: a sweep or a request's git
    still at work would hold up a stopping daemon (and the pause the app is
    waiting for) for as long as it runs. The request that wanted it has
    gone, and a sweep runs again at the next start."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main)
    finally:
        try:
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()     # its threads are left to finish, or to end with the process (leave)


EXIT_GRACE = 1.0    # seconds the at-exit hooks get once the daemon has stopped (leave)


def leave(code: int = 0) -> None:
    """End the process of a daemon that has stopped (serve has returned: its
    pause and cleanup are done) at once. Threads still at work for requests
    or sweeps aren't waited for; the reads brindle started are stopped; the
    at-exit hooks (Pro's queued events onto their spools, which the next
    daemon sends) get EXIT_GRACE seconds between them."""
    spawn._stop_reads_at_exit()
    finished = threading.Event()

    def at_exit() -> None:
        try:
            atexit._run_exitfuncs()
        finally:
            finished.set()

    threading.Thread(target=at_exit, name="brindle-at-exit", daemon=True).start()
    finished.wait(EXIT_GRACE)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - a closed stream: nothing left to say
            pass
    os._exit(code)


PAUSE_GRACE = 20.0     # seconds in all `daemon stop` waits while the daemon says it is pausing (daemon.rs PAUSE_GRACE)


def _says_pausing(pid: int) -> bool:
    """Whether the daemon ``pid`` says it is pausing on its way out (say_pausing)."""
    try:
        return _pausing_path().read_text().strip() == str(pid)
    except OSError:
        return False


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def stop(*, clock=time.monotonic, wait=threading.Event().wait) -> dict | None:
    """Stop the running daemon. Returns its info, or None if none ran (and
    clears a dead one's files). SIGTERM, then SIGKILL once STOP_GRACE has
    passed; while the daemon says it is pausing its sessions (say_pausing),
    the wait goes on, up to PAUSE_GRACE in all, and ends STOP_GRACE after it
    stops saying so, as the app's own stop does (daemon.rs stop_daemon). The
    app's daemon outlives the app, so this is how it is stopped with no app
    about, and it pauses what it runs on its way out."""
    info = read_info()
    if info is None:
        return None
    pid = info["pid"]
    if not _is_daemon(pid):
        cleanup(info)
        return None
    _signal(pid, signal.SIGTERM)
    stopped = clock()
    deadline, said = stopped + STOP_GRACE, False
    while procs.alive(pid):
        if _says_pausing(pid):
            deadline, said = stopped + PAUSE_GRACE, True
        elif said:      # its pause is over: what it still does on its way out gets the short wait
            deadline, said = min(clock() + STOP_GRACE, stopped + PAUSE_GRACE), False
        if clock() >= deadline:
            _signal(pid, signal.SIGKILL)
            break
        wait(0.1)
    cleanup(info)
    return info
