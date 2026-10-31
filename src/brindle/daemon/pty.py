"""``WS /v1/pty``: a tmux pane shown through a real tmux client on a pty that
runs here, in the daemon, for an app that can't run one itself. On Windows the
engine and its tmux run inside WSL and the app outside it, so the app's own pty
view (app/src-tauri/src/pty_view.rs) can't attach a client; this endpoint makes
the same view and streams the client's bytes over the daemon's socket instead
(app/docs/terminal-architecture.md, "Windows: the engine's pty").

The view is the app's, made the same way: a session of its own,
``brindle-view-<daemon pid>-<n>``, holding only the agent's window
(``link-window``), with ``status off``, ``prefix None``, ``mouse on``, the
``brindle-view`` key table and, once its client is attached,
``destroy-unattached on``. Nothing else on the server changes. Its client runs
``tmux -u -T <features> attach-session`` on a pty of the view's size. The two
copies of the view's tmux commands are held to one record:
tests/fixtures/view_session.json, which the app's tests and this module's both
check against.

The socket (after the guard's token) takes ``?server=&target=&cols=&rows=`` and
then, as the app's examples/pty_ws.rs bridge does:
- down: the client's bytes as binary frames, a redraw at a time; the window's
  crop as ``{"crop": {...}}`` (before the first bytes, and on each change);
  ``{"exit": code}`` last, or ``{"error": why}`` if it couldn't open;
- up: ``{"type": "input", "data": ...}``, ``{"type": "resize", "cols", "rows"}``
  and ``{"type": "focus"}``.
The view session goes when the socket closes, the client exits or the daemon
stops; if the daemon dies, its client dies with the pty and
``destroy-unattached`` takes the session."""

from __future__ import annotations

import asyncio
import fcntl
import itertools
import json
import os
import queue
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from brindle import tmux

FEATURES = "RGB,256,mouse,title,bpaste,focus,sync,usstyle,strikethrough,overline,cstyle,clipboard,hyperlinks"
MIN_TMUX = (3, 2)
VIEW_PREFIX = "brindle-view-"
KEY_TABLE = "brindle-view"
# tmux's stock root-table pane mouse, by the oldest server each is for (pty_view.rs MOUSE_BINDINGS).
MOUSE_BINDINGS: list[tuple[tuple[int, int], str]] = [
    ((3, 2), r'MouseDown1Pane select-pane -t = \; send-keys -M'),
    ((3, 2), r'MouseDown2Pane select-pane -t = \; if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" "send -M" "paste -p"'),
    ((3, 2), r'MouseDrag1Pane if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" "send -M" "copy-mode -M"'),
    ((3, 2), r'WheelUpPane if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" "send -M" "copy-mode -e"'),
    ((3, 2), r'DoubleClick1Pane select-pane -t = \; if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" "send -M" "copy-mode -H ; send -X select-word ; run -d0.3 ; send -X copy-pipe-and-cancel"'),
    ((3, 2), r'TripleClick1Pane select-pane -t = \; if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" "send -M" "copy-mode -H ; send -X select-line ; run -d0.3 ; send -X copy-pipe-and-cancel"'),
    ((3, 7), r'WheelUpPane if-shell -F "#{||:#{alternate_on},#{pane_in_mode},#{mouse_any_flag}}" { send-keys -M } { copy-mode -e }'),
    ((3, 7), r'MouseDown1ScrollbarUp if-shell -F -t = "#{pane_in_mode}" { send-keys -X page-up } { copy-mode -u }'),
    ((3, 7), r'MouseDown1ScrollbarDown if-shell -F -t = "#{pane_in_mode}" { send-keys -X page-down } { copy-mode -d }'),
    ((3, 7), r'MouseDrag1ScrollbarSlider if-shell -F -t = "#{pane_in_mode}" { send-keys -X scroll-to-mouse } { copy-mode -S }'),
]
# ⌘C's and Ctrl+C's keys (pty_view.rs COPY_BINDING, COPY_OR_INTERRUPT_BINDING).
COPY_BINDING = r'C-M-S-F12 if-shell -F "#{selection_present}" "send-keys -X copy-selection-no-clear"'
COPY_OR_INTERRUPT_BINDING = r'C-M-S-F11 if-shell -F "#{selection_present}" "send-keys -X copy-selection-and-cancel" "send-keys C-c"'
LAYOUT_FORMAT = ("#{@brindle_sidebar}\t#{pane_left}\t#{pane_right}\t#{pane_top}\t#{pane_bottom}"
                 "\t#{window_width}\t#{window_height}\t#{window_zoomed_flag}\t#{pane_active}\t#{cursor_x}")
CLIENT_FORMAT = "#{client_pid}\t#{client_width}\t#{window_offset_x}"
MAX_SIZE = 1000
NEWEST = (2**32 - 1, 0)     # `master` and OpenBSD's own: newer than any numbered tmux

SYNC_START, SYNC_END = b"\x1b[?2026h", b"\x1b[?2026l"
QUIET, GATHER_MAX, SYNC_MAX = 0.001, 0.008, 0.050    # seconds (pty_view.rs forward)
GATHER_BYTES = 1 << 18
RELEASE_GAP = 0.015     # a mouse release this long behind the input before it (pty_view.rs RELEASE_GAP)
ATTACH_WAIT = 2.0       # how long focus waits for the client to show up on the server

_next_view = itertools.count(1)
_open: set[View] = set()
_open_lock = threading.Lock()


class ViewError(Exception):
    """Why a view couldn't open, in words the app shows as they are."""


# -- what the view's tmux commands are (held to tests/fixtures/view_session.json) ------------------


def parse_version(version: str) -> tuple[int, int] | None:
    v = version.strip().removeprefix("tmux").strip()
    if v == "master" or v.startswith("openbsd-"):
        return NEWEST
    v = v.removeprefix("next-")
    major, _, rest = v.partition(".")
    if not major.isdigit():
        return None
    minor = "".join(itertools.takewhile(str.isdigit, rest or "0"))
    return int(major), int(minor or 0)


def check_version(version: str, what: str) -> str | None:
    """Why a tmux older than 3.2 is refused, in the app's words; None when it isn't."""
    major, minor = MIN_TMUX
    parsed = parse_version(version)
    if parsed is None:
        return f"{what} says its version is {json.dumps(version.strip())}, which brindle can't read: it needs tmux {major}.{minor} or newer."
    if parsed < MIN_TMUX:
        return (f"{what} is {version.strip()}, and brindle needs tmux {major}.{minor} or newer to show a session. "
                "Update tmux, then reopen the session.")
    return None


def server_args(server: str | None) -> list[str]:
    """The arguments naming a pane's tmux server: ``-S`` for a socket path, ``-L`` for a name,
    none for the default server. ValueError for anything else."""
    s = (server or "").strip()
    if not s:
        return []
    if "/" in s:
        return ["-S", s]
    if not all(c.isascii() and (c.isalnum() or c in "-_.") for c in s):
        raise ValueError(f"not a tmux server name: {s}")
    return [] if s.lower() == "default" else ["-L", s]


def valid_target(target: str) -> bool:
    return bool(target) and not target.startswith("-") and all(
        c.isascii() and (c.isalnum() or c in "-_.:@%$=+") for c in target)


def view_bindings(version: tuple[int, int]) -> str:
    lines = [b for since, b in MOUSE_BINDINGS if version >= since] + [COPY_BINDING, COPY_OR_INTERRUPT_BINDING]
    return "".join(f"bind-key -T {KEY_TABLE} {b}\n" for b in lines)


def prepare_args(view: str, target: str, cols: int, rows: int) -> list[str]:
    """The one tmux command list that makes the view session; its key table comes on stdin."""
    placeholder = "brindle-view-placeholder"
    session = f"={view}:"
    cmds = [
        ["new-session", "-d", "-s", view, "-n", placeholder, "-x", str(cols), "-y", str(rows), "cat"],
        ["link-window", "-s", target, "-t", session],
        ["kill-window", "-t", f"={view}:{placeholder}"],
        ["set-option", "-t", session, "status", "off"],
        ["set-option", "-t", session, "prefix", "None"],
        ["set-option", "-t", session, "prefix2", "None"],
        ["set-option", "-t", session, "mouse", "on"],
        ["set-option", "-t", session, "key-table", KEY_TABLE],
        ["source-file", "-"],
    ]
    return [a for i, c in enumerate(cmds) for a in ([";"] if i else []) + c]


def attach_args(view: str) -> list[str]:
    session = f"={view}:"
    return ["-u", "-T", FEATURES, "attach-session", "-t", session, ";",
            "set-option", "-t", session, "destroy-unattached", "on"]


def view_owner(session: str) -> int | None:
    rest = session.removeprefix(VIEW_PREFIX)
    pid, sep, n = rest.partition("-")
    if rest == session or not sep or not all(c.isdigit() for c in n) or not pid.isdigit():
        return None
    return int(pid)


def clamp(n: int) -> int:
    return max(1, min(MAX_SIZE, n))


@dataclass(frozen=True)
class Crop:
    """pty_view.rs Crop: the columns of brindle's CLI sidebar and its border the
    view's client has beyond the view, where the view starts on it, and the
    agent's columns shown where fewer than the view's."""
    cols: int = 0
    left: int = 0
    width: int | None = None

    def to_json(self) -> dict:
        return {"cols": self.cols, "left": self.left, "width": self.width}


def crop_of(panes: str, visible: int, client: tuple[int, int] | None) -> Crop:
    """pty_view.rs crop_of, line for line."""
    rows = [line.split("\t") for line in panes.splitlines()]
    rows = [f for f in rows if len(f) == 10]

    def num(s: str) -> int | None:
        return int(s) if s.isdigit() and int(s) <= 0xFFFF else None

    for f in rows:
        sidebar, zoomed = f[0], f[7]
        if not sidebar or zoomed == "1":
            continue
        nums = [num(x) for x in f[1:7]]
        if any(n is None for n in nums):
            continue
        left, right, top, bottom, width, height = nums  # type: ignore[misc]
        if top != 0 or bottom + 1 != height:
            continue
        if left == 0 and right + 1 < width:
            cols, agent = right + 2, (right + 2, width)
        elif left > 0 and right + 1 == width:
            cols, agent = width - left + 1, (0, left - 1)
        else:
            continue
        client_width, panned = client or (clamp(visible + cols), 0)
        start = lo = min(max(agent[0] - panned, 0), client_width)
        hi = min(max(agent[1] - panned, 0), client_width)
        shown = hi - lo
        if shown > visible:
            active = next((g for g in rows if g[8] == "1" and not g[0]), None)
            at, offset = (num(active[1]), num(active[9])) if active else (None, None)
            cursor = at + offset if at is not None and offset is not None else None
            if cursor is not None and cursor >= panned and cursor - panned >= lo + visible:
                start = min(cursor - panned + 1 - visible, hi - visible)
        return Crop(cols, start, shown if shown < visible else None)
    return Crop()


def sync_open_after(open_: bool, before: bytes, data: bytes) -> bool:
    """Whether a synchronized update is open after ``data``, given whether one was before it."""
    seen = before[-(len(SYNC_START) - 1):] + data if before else data
    start, end = seen.rfind(SYNC_START), seen.rfind(SYNC_END)
    return open_ if start == end == -1 else start > end


def sgr_releases(data: bytes) -> list[int]:
    """Where in ``data`` each SGR mouse release (``ESC [ < b ; x ; y m``) starts."""
    at, i = [], 0
    while i + 3 <= len(data):
        if data.startswith(b"\x1b[<", i):
            end = i + 3
            while end < len(data) and (chr(data[end]).isdigit() or data[end] == ord(";")):
                end += 1
            if end == len(data):
                break
            if data[end] == ord("m") and end > i + 3:
                at.append(i)
            i = end
            continue
        i += 1
    return at


# -- running tmux --------------------------------------------------------------------------------


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "TMUX"}


def _run(server: list[str], *args: str, input: str | None = None, binary: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([binary or tmux._bin(), "-u", *server, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", input=input, env=_env(),
                              stdin=None if input is not None else subprocess.DEVNULL, timeout=tmux.timeout())
    except FileNotFoundError:
        raise ViewError("tmux isn't installed where brindle runs") from None
    except subprocess.TimeoutExpired:
        raise ViewError(str(tmux._hung([binary or tmux._bin(), *server], args))) from None


def client_version(binary: str | None = None) -> str:
    return _run([], "-V", binary=binary).stdout.strip()


def status() -> dict:
    """What the app's pty_view_status says, for this engine's tmux."""
    try:
        version = client_version()
    except ViewError as e:
        return {"kind": "unusable", "message": str(e)}
    if (why := check_version(version, "The engine's tmux")):
        return {"kind": "unusable", "message": why}
    return {"kind": "ready", "tmux": version}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _socket_path(server: list[str]) -> str:
    if server[:1] == ["-S"]:
        return server[1]
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return os.path.join(tmpdir, f"tmux-{os.getuid()}", server[1] if server else "default")


def _trusted(path: str) -> bool:
    """It and its folder owned by root or this user, and writable by no one else."""
    def safe(p: str) -> bool:
        try:
            st = os.stat(p)
        except OSError:
            return False
        return st.st_uid in (0, os.getuid()) and not st.st_mode & 0o022
    real = os.path.realpath(path)
    return safe(real) and safe(os.path.dirname(real))


def client_for(ours: str, theirs: str, server: list[str]) -> str:
    """The tmux the view's client runs: ours when the server runs the same version,
    else the executable of the process listening on its socket, if it is this
    user's, trusted, and says it is that version (pty_view.rs client_for). Nothing
    the server answers picks it: the kernel names the listener."""
    if ours == theirs:
        return tmux._bin()
    from brindle.daemon.peer import peer_pid

    exe = None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(_socket_path(server))
            pid = peer_pid(s)
        if pid is not None and sys.platform.startswith("linux") and os.stat(f"/proc/{pid}").st_uid == os.getuid():
            exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        pass
    if exe and _trusted(exe) and client_version(exe) == theirs:
        return exe
    raise ViewError(f"The agents' tmux server is {theirs} and the engine's is {ours}: a tmux client only "
                    "attaches to a server of its own version, and the server's own tmux couldn't be found.")


def _clear_stale(server: list[str]) -> None:
    """View sessions a daemon or app that is gone left on the server: killed."""
    listed = _run(server, "list-sessions", "-F", "#{session_name}")
    for session in listed.stdout.splitlines():
        pid = view_owner(session)
        if pid is not None and pid != os.getpid() and not _alive(pid):
            _run(server, "kill-session", "-t", f"={session}")


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", clamp(rows), clamp(cols), 0, 0))


# -- one view ------------------------------------------------------------------------------------


class View:
    """A view session and its client on a pty of the daemon's."""

    def __init__(self, binary: str, server: list[str], name: str, fd: int, proc: subprocess.Popen,
                 size: tuple[int, int], crop: Crop) -> None:
        self.binary, self.server, self.name, self.fd, self.proc = binary, server, name, fd, proc
        self.size, self.crop = size, crop
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.client_tty: str | None = None

    @classmethod
    def open(cls, server_name: str | None, target: str, cols: int, rows: int) -> View:
        try:
            server = server_args(server_name)
        except ValueError as e:
            raise ViewError(str(e)) from None
        if not valid_target(target):
            raise ViewError(f"not a tmux target: {target}")
        ours = client_version()
        if (why := check_version(ours, "The engine's tmux")):
            raise ViewError(why)
        running = _run(server, "display-message", "-p", "#{version}")
        if running.returncode != 0:
            raise ViewError(f"the agent's tmux server isn't there: {running.stderr.strip()}")
        theirs = f"tmux {running.stdout.strip()}"
        if (why := check_version(theirs, "The agents' tmux server")):
            raise ViewError(why)
        binary = client_for(ours, theirs, server)
        version = parse_version(theirs) or MIN_TMUX
        _clear_stale(server)
        cols, rows = clamp(cols), clamp(rows)
        name = f"{VIEW_PREFIX}{os.getpid()}-{next(_next_view)}"
        made = _run(server, *prepare_args(name, target, cols, rows), input=view_bindings(version), binary=binary)
        if made.returncode != 0:
            _run(server, "kill-session", "-t", f"={name}", binary=binary)
            raise ViewError(f"tmux couldn't make the view: {made.stderr.strip()}")
        crop = _crop_now(binary, server, name, cols, None) or Crop()
        fd, proc = _spawn_client(binary, server, name, clamp(cols + crop.cols), rows)
        view = cls(binary, server, name, fd, proc, (cols, rows), crop)
        with _open_lock:
            _open.add(view)
        return view

    def resize(self, cols: int, rows: int) -> None:
        with self.lock:
            self.size = (clamp(cols), clamp(rows))
            _set_size(self.fd, self.size[0] + self.crop.cols, self.size[1])

    def take_window(self) -> None:
        """pty_view.rs take_window: the view's own client switched to its own session,
        which makes it the window's latest client (its size wins), sending the pane nothing."""
        if self.client_tty is None:
            deadline = time.monotonic() + ATTACH_WAIT
            while self.client_tty is None:
                out = _run(self.server, "list-clients", "-t", f"={self.name}", "-F", "#{client_pid} #{client_tty}")
                self.client_tty = next((tty for pid, _, tty in (line.partition(" ") for line in out.stdout.splitlines())
                                        if pid == str(self.proc.pid)), None)
                if self.client_tty is None:
                    if time.monotonic() > deadline:
                        raise ViewError(f"the view's client isn't attached: {out.stderr.strip()}")
                    time.sleep(0.025)
        out = _run(self.server, "switch-client", "-E", "-c", self.client_tty, "-t", f"={self.name}")
        if out.returncode != 0:
            raise ViewError(f"tmux couldn't give the view the window: {out.stderr.strip()}")

    def close(self) -> None:
        """The view session goes (its client exits with it); a client that doesn't is killed."""
        if self.closed.is_set():
            return
        self.closed.set()
        with _open_lock:
            _open.discard(self)
        try:
            _run(self.server, "kill-session", "-t", f"={self.name}")
        except ViewError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        try:
            os.close(self.fd)
        except OSError:
            pass


def _crop_now(binary: str, server: list[str], view: str, visible: int, client: int | None) -> Crop | None:
    out = _run(server, "list-panes", "-t", f"={view}:", "-F", LAYOUT_FORMAT, binary=binary)
    if out.returncode != 0:
        return None
    on = None
    if client is not None:
        clients = _run(server, "list-clients", "-t", f"={view}", "-F", CLIENT_FORMAT, binary=binary)
        for line in clients.stdout.splitlines() if clients.returncode == 0 else []:
            fields = line.split("\t")
            if len(fields) == 3 and fields[0] == str(client) and fields[1].isdigit():
                on = (int(fields[1]), int(fields[2]) if fields[2].isdigit() else 0)
    return crop_of(out.stdout, visible, on)


# The client starts on a shell that opens the pty itself: a session leader (start_new_session)
# opening a terminal makes it its controlling one, so the client gets SIGWINCH on a resize. No
# preexec_fn (not safe in the daemon's threads) and no fork of the daemon's own.
_ON_TTY = 'tty=$1; shift; exec "$@" <>"$tty" >&0 2>&0'


def _spawn_client(binary: str, server: list[str], view: str, cols: int, rows: int) -> tuple[int, subprocess.Popen]:
    master, slave = os.openpty()
    try:
        _set_size(master, cols, rows)
        env = _env() | {"TERM": "xterm-256color"}
        proc = subprocess.Popen(["/bin/sh", "-c", _ON_TTY, "sh", os.ttyname(slave), binary, *server, *attach_args(view)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, close_fds=True, env=env)
    except OSError as e:
        os.close(master)
        raise ViewError(f"couldn't attach: {e}") from None
    finally:
        os.close(slave)
    return master, proc


def close_all() -> None:
    """The daemon stops: every view still open goes."""
    with _open_lock:
        views = list(_open)
    for view in views:
        view.close()


# -- the client's bytes, gathered (pty_view.rs forward) -------------------------------------------


def _reader(view: View, reads: queue.Queue) -> None:
    while True:
        try:
            data = os.read(view.fd, 1 << 16)
        except InterruptedError:
            continue
        except OSError:     # EIO on Linux once the client's side has closed
            data = b""
        if not data:
            break
        reads.put(data)
    code = view.proc.wait()
    reads.put(code if code >= 0 else -1)


def forward(reads: queue.Queue, on_data, now=time.monotonic) -> int:
    """The client's bytes to ``on_data``, a redraw at a time (all of a synchronized
    update, else what comes within QUIET of the last read); its exit code back."""
    open_, tail = False, b""

    def seen(data: bytes) -> bool:
        nonlocal tail
        was = sync_open_after(open_, tail, data)
        tail = (tail + data)[-(len(SYNC_START) - 1):]
        return was

    while True:
        got = reads.get()
        if isinstance(got, int):
            return got
        data, exit_code = bytearray(got), None
        open_ = seen(got)
        first = now()
        while len(data) < GATHER_BYTES:
            until = first + (SYNC_MAX if open_ else GATHER_MAX)
            wait = max(0.0, until - now()) if open_ else max(0.0, min(QUIET, until - now()))
            try:
                more = reads.get(timeout=wait)
            except queue.Empty:
                break
            if isinstance(more, int):
                exit_code = more
                break
            open_ = seen(more)
            data += more
            if now() >= until:
                break
        on_data(bytes(data))
        if exit_code is not None:
            return exit_code


def write_input(fd: int, data: bytes, last: list[float]) -> None:
    """What the person typed, to the client: each SGR mouse release RELEASE_GAP after the
    write before it, so tmux reads them apart (pty_view.rs write_input)."""
    def write(chunk: bytes) -> None:
        view = memoryview(chunk)
        while view:
            view = view[os.write(fd, view):]
        last[:] = [time.monotonic()]

    start = 0
    for at in sgr_releases(data):
        if start < at:
            write(data[start:at])
        if last and (wait := RELEASE_GAP - (time.monotonic() - last[0])) > 0:
            time.sleep(wait)
        start = at
    if start < len(data):
        write(data[start:])


# -- the routes ----------------------------------------------------------------------------------


async def pty_status(request: Request) -> JSONResponse:
    """GET /v1/pty/status: whether this engine can show panes (its tmux, 3.2 or newer)."""
    return JSONResponse(await asyncio.to_thread(status))


async def pty(ws: WebSocket) -> None:
    await ws.accept()
    q = ws.query_params
    try:
        cols, rows = int(q.get("cols", "80")), int(q.get("rows", "24"))
        view = await asyncio.to_thread(View.open, q.get("server"), q.get("target", ""), cols, rows)
    except (ValueError, ViewError) as e:
        await ws.send_json({"error": str(e) if isinstance(e, ViewError) else "cols and rows are numbers"})
        await ws.close()
        return
    try:
        await _serve(ws, view)
    finally:
        await asyncio.to_thread(view.close)


async def _serve(ws: WebSocket, view: View) -> None:
    loop = asyncio.get_running_loop()
    reads: queue.Queue = queue.Queue()
    redrawn = threading.Event()
    exited: asyncio.Future = loop.create_future()

    def send(message: dict | bytes) -> None:
        """From a thread: one frame, in order with the thread's others."""
        if view.closed.is_set():
            return
        coro = ws.send_bytes(message) if isinstance(message, bytes) else ws.send_json(message)
        try:
            asyncio.run_coroutine_threadsafe(coro, loop).result()
        except Exception:   # the socket is gone: the receive loop ends the view
            pass

    def gather() -> None:
        def on_data(data: bytes) -> None:
            send(data)
            redrawn.set()
        code = forward(reads, on_data)
        loop.call_soon_threadsafe(lambda: exited.done() or exited.set_result(code))

    def follow_crop() -> None:
        # Looked at again a little after the client draws, and each second besides (pty_view.rs).
        while not view.closed.is_set() and not exited.done():
            redrawn.wait(1.0)
            time.sleep(0.05)
            redrawn.clear()
            try:
                crop = _crop_now(view.binary, view.server, view.name, view.size[0], view.proc.pid)
            except ViewError:
                continue
            if crop is None or view.closed.is_set():
                continue
            with view.lock:
                if crop == view.crop:
                    continue
                resized = crop.cols != view.crop.cols
                view.crop = crop
                send({"crop": crop.to_json()})      # told first: what's drawn at the new size comes after
                if resized:
                    _set_size(view.fd, view.size[0] + crop.cols, view.size[1])

    keys: queue.Queue = queue.Queue()

    def type_keys() -> None:
        last: list[float] = []
        while (data := keys.get()) is not None:
            try:
                write_input(view.fd, data, last)
            except OSError:
                return

    await ws.send_json({"crop": view.crop.to_json()})
    threads = [threading.Thread(target=f, daemon=True, name=f"brindle-pty-{f.__name__}")
               for f in (lambda: _reader(view, reads), gather, follow_crop, type_keys)]
    for t in threads:
        t.start()
    receiving = asyncio.ensure_future(_receive(ws, view, keys))
    try:
        await asyncio.wait({receiving, exited}, return_when=asyncio.FIRST_COMPLETED)
        if exited.done():
            await ws.send_json({"exit": exited.result()})
            await ws.close()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        receiving.cancel()
        keys.put(None)


async def _receive(ws: WebSocket, view: View, keys: queue.Queue) -> None:
    try:
        while True:
            try:
                said = json.loads(await ws.receive_text())
            except (json.JSONDecodeError, KeyError):
                continue
            kind = said.get("type") if isinstance(said, dict) else None
            if kind == "input" and isinstance(said.get("data"), str):
                keys.put(said["data"].encode())
            elif kind == "resize":
                try:
                    view.resize(int(said["cols"]), int(said["rows"]))
                except (KeyError, TypeError, ValueError, OSError):
                    pass
            elif kind == "focus":
                try:
                    await asyncio.to_thread(view.take_window)
                except ViewError:
                    pass
    except (WebSocketDisconnect, RuntimeError):
        return
