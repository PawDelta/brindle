"""``WS /v1/pty_view``: a tmux pane shown through a real tmux client on a pty
in the engine's host, for the Windows app, whose shell can't open a pty on
WSL's tmux (app/docs/terminal-architecture.md, step 4).

The engine's side of app/src-tauri/src/pty_view.rs, and it does as that
does: each open view gets a tmux session of its own
(``brindle-view-<the daemon's pid>-<n>``) holding only the pane's window
(``link-window``), with no status line, no prefix, the mouse on, the key
table ``brindle-view`` (tmux's stock pane mouse bindings for the server's
version, and the app's copy keys) in place of the server's root table and,
once its client is attached, ``destroy-unattached``. Nothing global on the
server changes. The view session goes when the socket closes or drops.

Only brindle's own tmux servers are served (:func:`admit`): the one the
daemon uses (its action terminals') and those its agents' windows are on.

Frames, after the token the guard takes:

- up, first: ``{"server": <-L name, socket path or null>, "target": <window>, "cols": c, "rows": r}``;
- down: the client's bytes as binary frames, a redraw at a time (:func:`forward`);
  ``{"crop": {"cols", "left", "width"}}`` before the first bytes and on each change;
  ``{"exit": code}`` last; ``{"error": message}`` when the view can't open;
- up, after: ``{"type": "input", "data"}``, ``{"type": "resize", "cols", "rows"}``
  and ``{"type": "focus"}``, as app/src-tauri/examples/pty_ws.rs takes them."""

from __future__ import annotations

import asyncio
import dataclasses
import fcntl
import itertools
import json
import logging
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import threading
from pathlib import Path

from starlette.websockets import WebSocket, WebSocketDisconnect

from brindle import tmux

log = logging.getLogger(__name__)

# What tmux is told xterm.js can do (`terminal-features`); hyperlinks (3.4) last,
# as a 3.2 server reads the list up to the first name it doesn't know.
FEATURES = ("RGB,256,mouse,title,bpaste,focus,sync,usstyle,strikethrough,overline,cstyle,"
            "clipboard,hyperlinks")
MIN_TMUX = (3, 2)               # `-T` (client features) is 3.2's
VIEW_PREFIX = "brindle-view-"
KEY_TABLE = "brindle-view"
# tmux's stock root-table bindings for the pane's own mouse, by the oldest
# server each is for (pty_view.rs's MOUSE_BINDINGS: a later one replaces its key's).
MOUSE_BINDINGS = (
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
)
# ⌘C and Ctrl+C with nothing selected in xterm.js (PtyTerminal's COPY_KEY and COPY_OR_INTERRUPT).
COPY_BINDING = r'C-M-S-F12 if-shell -F "#{selection_present}" "send-keys -X copy-selection-no-clear"'
COPY_OR_INTERRUPT_BINDING = (r'C-M-S-F11 if-shell -F "#{selection_present}" '
                             r'"send-keys -X copy-selection-and-cancel" "send-keys C-c"')
MAX_SIZE = 1000                 # the largest pty a view asks for

# What crop_of reads, a line a pane, and what crop_now reads of the view's client.
LAYOUT_FORMAT = ("#{@brindle_sidebar}\t#{pane_left}\t#{pane_right}\t#{pane_top}\t#{pane_bottom}"
                 "\t#{window_width}\t#{window_height}\t#{window_zoomed_flag}\t#{pane_active}\t#{cursor_x}")
CLIENT_FORMAT = "#{client_pid}\t#{client_width}\t#{window_offset_x}"

# A synchronized update's start and end (DEC 2026): tmux brackets each redraw so.
SYNC_START = b"\x1b[?2026h"
SYNC_END = b"\x1b[?2026l"
QUIET = 0.001                   # outside one, what follows a read this soon goes with it,
GATHER_MAX = 0.008              # up to this long from the first byte;
SYNC_MAX = 0.05                 # inside one, up to this long
GATHER_BYTES = 1 << 18          # the most bytes sent as one
CROP_SECONDS = 1.0              # the crop is looked at after each redraw, and this often besides
LOOK_GAP = 0.05                 # and no sooner than this after the last look
RELEASE_GAP = 0.015             # a mouse release waits this long behind the input before it
CLOSE_SECONDS = 5.0             # the client's time to exit once its session has gone

_views = itertools.count(1)


class ViewError(Exception):
    """Why a view can't open, or a command on it failed, in words the app shows."""


def parse_version(version: str) -> tuple[int, int] | None:
    """A tmux version's major and minor: ``tmux 3.7c``, ``3.2a``, ``next-3.4``,
    ``openbsd-7.5`` (OpenBSD's own, which is new); ``master`` is the newest."""
    v = version.strip().removeprefix("tmux").strip()
    if v == "master" or v.startswith("openbsd-"):
        return (sys.maxsize, 0)
    major, _, rest = v.removeprefix("next-").partition(".")
    if not major.isdigit():
        return None
    return int(major), int("".join(itertools.takewhile(str.isdigit, rest)) or 0)


def check_version(version: str, what: str) -> None:
    """Refuses a tmux older than 3.2. ``what`` says which tmux it is."""
    major, minor = MIN_TMUX
    v = parse_version(version)
    if v is None:
        raise ViewError(f"{what} says its version is {version.strip()!r}, which brindle can't read: "
                        f"it needs tmux {major}.{minor} or newer.")
    if v < MIN_TMUX:
        raise ViewError(f"{what} is {version.strip()}, and brindle needs tmux {major}.{minor} or newer "
                        "to show a session. Update tmux, then reopen the session.")


def server_args(server: str | None) -> list[str]:
    """The arguments that name ``server``: ``-S`` for a socket path, ``-L``
    for a name, none for the default server (no server, or ``default``)."""
    s = (server or "").strip()
    if not s:
        return []
    if "/" in s:
        return ["-S", s]
    if not all(c.isascii() and (c.isalnum() or c in "-_.") for c in s):
        raise ViewError(f"not a tmux server name: {s}")
    return [] if s.lower() == "default" else ["-L", s]


def valid_target(target: str) -> bool:
    """A tmux target (``session:window``, ``%pane``, ``@window``): never a command, never a flag."""
    return bool(target) and not target.startswith("-") \
        and all(c.isascii() and (c.isalnum() or c in "-_.:@%$=+") for c in target)


def view_bindings(version: tuple[int, int]) -> str:
    """The view's key table for a server of ``version``, as ``source-file`` reads it."""
    lines = [b for since, b in MOUSE_BINDINGS if version >= since] + [COPY_BINDING, COPY_OR_INTERRUPT_BINDING]
    return "".join(f"bind-key -T {KEY_TABLE} {b}\n" for b in lines)


def prepare_args(view: str, target: str, cols: int, rows: int) -> list[str]:
    """One tmux command list that makes the view session ``view`` of ``cols``
    x ``rows`` showing only ``target``'s window, with the app's options, and
    reads the view's key table (view_bindings) from stdin."""
    placeholder = "brindle-view-placeholder"
    session = f"={view}:"       # set-option's -t is a pane's: the session's current window
    cmds = [
        # A session's first window is a command's; `cat` waits on a pty and does nothing.
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
    return [*itertools.chain.from_iterable([*c, ";"] for c in cmds)][:-1]


def attach_args(view: str) -> list[str]:
    """The client's command line. ``destroy-unattached`` only once it is
    attached: set on a session with no client, it takes the session at once."""
    session = f"={view}:"
    return ["-u", "-T", FEATURES, "attach-session", "-t", session,
            ";", "set-option", "-t", session, "destroy-unattached", "on"]


def view_owner(session: str) -> int | None:
    """The pid in a view session's name, if it is one."""
    pid, _, n = session.removeprefix(VIEW_PREFIX).partition("-")
    if not session.startswith(VIEW_PREFIX) or not pid.isdigit() or not n.isdigit():
        return None
    return int(pid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _clamp(n: int) -> int:
    return max(1, min(int(n), MAX_SIZE))


def _socket(args: list[str]) -> Path:
    """The socket the server ``args`` (server_args) names."""
    if args[:1] == ["-S"]:
        return Path(args[1]).resolve()
    return (tmux.socket_dir() / (args[1] if args else "default")).resolve()


def own_servers() -> set[str | None]:
    """The tmux servers brindle's panes are on: the daemon's own (its action
    terminals') and each agent's window's. None is the default server."""
    from brindle import agents
    from brindle.db import DB

    db = DB()
    try:
        return {tmux.current_server(), *(agents.server_of(a) for a in db.list_agents() if a.tmux_window)}
    finally:
        db.conn.close()


def admit(server: str | None) -> list[str]:
    """``server``'s arguments (server_args) if it is one of brindle's own
    servers (by its socket, however it is named), else ViewError: no other
    of this user's tmux servers is shown, the default one included."""
    args = server_args(server)
    sockets = set()
    for own in own_servers():
        try:
            sockets.add(_socket(server_args(own)))
        except ViewError:
            continue
    if _socket(args) not in sockets:
        raise ViewError(f"only brindle's own tmux servers are shown, not {server or 'the default one'}")
    return args


def _tmux(server: list[str], *args: str, input: str | None = None) -> subprocess.CompletedProcess:
    """A tmux command on ``server``. ``-u``: UTF-8 whatever the locale."""
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    io = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run([tmux._bin(), "-u", *server, *args], capture_output=True, text=True,
                              env=env, timeout=tmux.timeout(), **io)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ViewError(f"couldn't run tmux: {e}") from None


def _clear_stale(server: list[str]) -> None:
    """View sessions a daemon that is gone left on the server (it died before
    its client attached): killed. Another running process's stay."""
    out = _tmux(server, "list-sessions", "-F", "#{session_name}")
    for session in out.stdout.splitlines():
        pid = view_owner(session)
        if pid is not None and pid != os.getpid() and not _alive(pid):
            _tmux(server, "kill-session", "-t", f"={session}")


@dataclasses.dataclass(frozen=True)
class Crop:
    """Columns of the view's window the app doesn't show: brindle's CLI
    sidebar and its border (pty_view.rs's Crop). The client is ``cols``
    wider than the view, which shows its columns from ``left`` on;
    ``width``: the agent's columns there, when fewer than the view's."""
    cols: int = 0
    left: int = 0
    width: int | None = None

    def json(self) -> dict:
        return dataclasses.asdict(self)


def crop_of(panes: str, visible: int, client: tuple[int, int] | None) -> Crop:
    """The crop for a window's panes as LAYOUT_FORMAT lists them, for a view
    ``visible`` columns wide, on a client ``client`` (its width, and how far
    tmux has panned the window in it) when it is there to say: a sidebar pane
    the window's full height at its left or right edge, beside another pane,
    is cropped with its border. None while a pane is zoomed, for a sidebar
    along the bottom, or one alone."""
    rows = [line.split("\t") for line in panes.splitlines()]
    rows = [r for r in rows if len(r) == 10]
    for sidebar, *nums, zoomed, _active, _cursor in rows:
        if not sidebar or zoomed == "1" or not all(n.isdigit() for n in nums):
            continue
        left, right, top, bottom, width, height = map(int, nums)
        if top != 0 or bottom + 1 != height:
            continue
        # The client's columns the sidebar and its border take, and the agent's columns (end exclusive).
        if left == 0 and right + 1 < width:
            cols, agent = right + 2, (right + 2, width)
        elif left > 0 and right + 1 == width:
            cols, agent = width - left + 1, (0, left - 1)
        else:
            continue
        client_width, panned = client or (_clamp(visible + cols), 0)
        start = min(max(agent[0] - panned, 0), client_width)
        end = min(max(agent[1] - panned, 0), client_width)
        shown, at_left = end - start, start
        if shown > visible:
            # Panned: more of the agent's columns are on the client than the view
            # shows. Its cursor kept in view, as tmux keeps it on the client.
            cursor = next((int(r[1]) + int(r[9]) for r in rows
                           if r[8] == "1" and not r[0] and r[1].isdigit() and r[9].isdigit()), None)
            if cursor is not None and cursor - panned >= start + visible:
                at_left = min(cursor - panned + 1 - visible, end - visible)
        return Crop(cols, at_left, shown if shown < visible else None)
    return Crop()


def crop_now(server: list[str], view: str, visible: int, client: int | None) -> Crop | None:
    """The crop of the view session's window as it is now, if tmux can say.
    ``client``: the pid of the view's client, once it is attached."""
    try:
        out = _tmux(server, "list-panes", "-t", f"={view}:", "-F", LAYOUT_FORMAT)
        if out.returncode:
            return None
        on = None
        if client is not None:
            listed = _tmux(server, "list-clients", "-t", f"={view}", "-F", CLIENT_FORMAT)
            for line in listed.stdout.splitlines():
                pid, width, panned = (line.split("\t") + ["", "", ""])[:3]
                if pid == str(client) and width.isdigit():
                    on = (int(width), int(panned) if panned.isdigit() else 0)
    except ViewError:
        return None
    return crop_of(out.stdout, visible, on)


def sync_open_after(open_: bool, before: bytes, data: bytes) -> bool:
    """Whether a synchronized update is open after ``data``, given whether one
    was before it; ``before``: the bytes just ahead (a marker split between them)."""
    keep = min(len(before), len(SYNC_START) - 1)
    seen = before[len(before) - keep:] + data
    start, end = seen.rfind(SYNC_START), seen.rfind(SYNC_END)
    return open_ if start == end == -1 else start > end


def sgr_releases(data: bytes) -> list[int]:
    """Where in ``data`` each SGR mouse release (``ESC [ < b ; x ; y m``) starts."""
    at, i = [], 0
    while i + 3 <= len(data):
        if not data.startswith(b"\x1b[<", i):
            i += 1
            continue
        end = i + 3
        while end < len(data) and (data[end:end + 1].isdigit() or data[end] == ord(";")):
            end += 1
        if end == len(data):
            break
        if data[end] == ord("m") and end > i + 3:
            at.append(i)
        i = end
    return at


async def _pause(seconds: float) -> None:
    """Let ``seconds`` go by: a rate limit or a gap, never a poll."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    handle = loop.call_later(seconds, done.set_result, None)
    try:
        await done
    finally:
        handle.cancel()


async def forward(reads: asyncio.Queue, on_data, now) -> int:
    """The client's reads (bytes, then its exit code) to ``on_data`` a redraw
    at a time; returns the exit code. All of a synchronized update goes as
    one (nothing of it is painted before its end anyway), and outside one,
    what comes within QUIET of the last read (pty_view.rs's forward)."""
    open_, tail = False, b""

    def read(data: bytes) -> None:
        nonlocal open_, tail
        open_ = sync_open_after(open_, tail, data)
        tail = (tail + data)[-(len(SYNC_START) - 1):]

    while True:
        first = await reads.get()
        if isinstance(first, int):
            return first
        data, code = bytearray(first), None
        read(first)
        began = now()
        while len(data) < GATHER_BYTES:
            until = began + (SYNC_MAX if open_ else GATHER_MAX)
            left = max(0.0, until - now())
            try:
                more = await asyncio.wait_for(reads.get(), left if open_ else min(QUIET, left))
            except TimeoutError:
                break
            if isinstance(more, int):
                code = more
                break
            read(more)
            data += more
            if now() >= until:
                break
        await on_data(bytes(data))
        if code is not None:
            return code


class PtyView:
    """One open view: a tmux client on a pty of its own. Its methods block
    (tmux runs as a process): the route calls them on threads."""

    def __init__(self, server: list[str], view: str, proc: subprocess.Popen, master: int, tty: str,
                 size: tuple[int, int], crop: Crop) -> None:
        self.server, self.view, self.proc, self.tty = server, view, proc, tty
        self.size, self.crop = size, crop
        self._master = master
        self._lock = threading.Lock()       # the master fd, its size, and closing
        self._closed = False
        self._reader: threading.Thread | None = None
        self.last_input = 0.0

    @classmethod
    def open(cls, server: str | None, target: str, cols: int, rows: int) -> PtyView:
        """Make the view session for ``target`` on ``server`` (one of brindle's
        own) and attach a client to it, ``cols`` x ``rows`` and the crop's columns."""
        args = admit(server)
        if not valid_target(target):
            raise ViewError(f"not a tmux target: {target}")
        ours = _tmux([], "-V").stdout.strip()
        check_version(ours, "The engine's tmux")
        running = _tmux(args, "display-message", "-p", "#{version}")
        if running.returncode:
            raise ViewError(f"the agent's tmux server isn't there: {running.stderr.strip()}")
        theirs = f"tmux {running.stdout.strip()}"
        check_version(theirs, "The agents' tmux server")
        if theirs != ours:
            # A client attaches only to a server of its own version.
            raise ViewError(f"The agents' tmux server is {theirs} and the engine's is {ours}: "
                            "a tmux client only attaches to a server of its own version.")
        _clear_stale(args)
        cols, rows = _clamp(cols), _clamp(rows)
        view = f"{VIEW_PREFIX}{os.getpid()}-{next(_views)}"
        made = _tmux(args, *prepare_args(view, target, cols, rows), input=view_bindings(parse_version(theirs)))
        if made.returncode:
            _tmux(args, "kill-session", "-t", f"={view}")
            raise ViewError(f"tmux couldn't make the view: {made.stderr.strip()}")
        crop = crop_now(args, view, cols, None) or Crop()
        try:
            master, slave = pty.openpty()
        except OSError as e:
            _tmux(args, "kill-session", "-t", f"={view}")
            raise ViewError(f"couldn't attach: {e}") from None
        try:
            _set_size(slave, (cols, rows), crop)
            env = {k: v for k, v in os.environ.items() if k != "TMUX"} | {"TERM": "xterm-256color"}
            # No controlling terminal: resize() tells the client its size changed itself.
            proc = subprocess.Popen([tmux._bin(), *args, *attach_args(view)], stdin=slave, stdout=slave,
                                    stderr=slave, env=env, start_new_session=True)
            tty = os.ttyname(slave)
        except OSError as e:
            os.close(master)
            _tmux(args, "kill-session", "-t", f"={view}")
            raise ViewError(f"couldn't attach: {e}") from None
        finally:
            os.close(slave)     # the client's alone: its exit ends the master's reads
        return cls(args, view, proc, master, tty, (cols, rows), crop)

    def start_reading(self, post) -> None:
        """The client's bytes to ``post`` as the pty gives them, then its exit code."""
        def run() -> None:
            while True:
                try:
                    data = os.read(self._master, 1 << 16)
                except InterruptedError:
                    continue
                except OSError:     # EIO on Linux once the client's side has closed
                    break
                if not data:
                    break
                post(data)
            post(self.proc.wait())

        self._reader = threading.Thread(target=run, name=f"{self.view}-reader", daemon=True)
        self._reader.start()

    def write(self, data: bytes) -> None:
        """What the person typed, to the client, all of it."""
        with self._lock:
            while data and not self._closed:
                data = data[os.write(self._master, data):]

    def resize(self, cols: int, rows: int) -> None:
        """The view is ``cols`` x ``rows``: its client's pty is that and the crop's columns."""
        with self._lock:
            self.size = (_clamp(cols), _clamp(rows))
            self._fit()

    def look(self) -> Crop | None:
        """The window's crop looked at again: the new one when it has changed, else None."""
        crop = crop_now(self.server, self.view, self.size[0], self.proc.pid)
        if crop is None or crop == self.crop:
            return None
        self.crop = crop
        return crop

    def fit(self) -> None:
        """The client's pty sized to the view and the crop as it is now."""
        with self._lock:
            self._fit()

    def _fit(self) -> None:
        if self._closed:
            return
        _set_size(self._master, self.size, self.crop)
        if self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGWINCH)

    def take(self) -> None:
        """The person is at the view: the window takes its size, as it would
        had they typed in it. Switching the view's own client to the session
        it is already on makes it the window's latest client, and sends the
        pane nothing (pty_view.rs's take_window)."""
        out = _tmux(self.server, "switch-client", "-E", "-c", self.tty, "-t", f"={self.view}")
        if out.returncode:
            raise ViewError(f"tmux couldn't give the view the window: {out.stderr.strip()}")

    def close(self) -> None:
        """The view session goes, which detaches its client; the agent's window
        stays in the agent's session. Then the pty."""
        try:
            _tmux(self.server, "kill-session", "-t", f"={self.view}")
        except ViewError:
            log.debug("brindle: couldn't kill %s", self.view, exc_info=True)
        try:
            self.proc.wait(CLOSE_SECONDS)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        if self._reader is not None:
            self._reader.join()
        with self._lock:
            if not self._closed:
                self._closed = True
                os.close(self._master)


def _set_size(fd: int, size: tuple[int, int], crop: Crop) -> None:
    cols, rows = size
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", _clamp(rows), _clamp(cols + crop.cols), 0, 0))


async def _type(view: PtyView, data: bytes) -> None:
    """What the person typed, to the client: as it comes, but each mouse
    release RELEASE_GAP after the write before it, so tmux reads it apart
    from a drag's last motion (whose copy it would otherwise lose)."""
    loop = asyncio.get_running_loop()

    async def write(part: bytes) -> None:
        await asyncio.to_thread(view.write, part)
        view.last_input = loop.time()

    start = 0
    for at in sgr_releases(data):
        if start < at:
            await write(data[start:at])
        gap = view.last_input + RELEASE_GAP - loop.time()
        if gap > 0:
            await _pause(gap)
        start = at
    if start < len(data):
        await write(data[start:])


async def _up(ws: WebSocket, view: PtyView) -> None:
    """The app's frames, until it closes."""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        try:
            up = json.loads(message.get("text") or "")
            kind = up.get("type")
            if kind == "input":
                await _type(view, str(up["data"]).encode())
            elif kind == "resize":
                view.resize(int(up["cols"]), int(up["rows"]))
            elif kind == "focus":
                await asyncio.to_thread(view.take)
        except (ValueError, KeyError, TypeError, AttributeError, ViewError, OSError) as e:
            log.info("brindle: pty view %s: %s", view.view, e)


async def _crops(ws: WebSocket, view: PtyView, redrawn: asyncio.Event) -> None:
    """The crop looked at again after each redraw (a layout change, the
    window panned, is redrawn) and each CROP_SECONDS besides (a sidebar
    marked after it was drawn redraws nothing); told before the bytes drawn
    at its new size."""
    while True:
        try:
            await asyncio.wait_for(redrawn.wait(), CROP_SECONDS)
        except TimeoutError:
            pass
        redrawn.clear()
        cols = view.crop.cols
        crop = await asyncio.to_thread(view.look)
        if crop is not None:
            await ws.send_json({"crop": crop.json()})
            if crop.cols != cols:
                await asyncio.to_thread(view.fit)
        await _pause(LOOK_GAP)


async def _down(ws: WebSocket, view: PtyView) -> None:
    """The client's bytes, the crop's changes, then its exit code."""
    loop = asyncio.get_running_loop()
    reads: asyncio.Queue = asyncio.Queue()
    redrawn = asyncio.Event()

    def post(item: bytes | int) -> None:
        try:
            loop.call_soon_threadsafe(reads.put_nowait, item)
        except RuntimeError:    # the loop has closed: nobody to tell
            pass

    async def send(data: bytes) -> None:
        await ws.send_bytes(data)
        redrawn.set()

    view.start_reading(post)
    looks = asyncio.ensure_future(_crops(ws, view, redrawn))
    try:
        code = await forward(reads, send, loop.time)
    finally:
        looks.cancel()
    await ws.send_json({"exit": code})


def _quietly(task: asyncio.Future) -> None:
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    task.cancel()


async def pty_view(ws: WebSocket) -> None:
    await ws.accept()
    try:
        ask = json.loads(await ws.receive_text())
        server, target = ask.get("server"), ask.get("target")
        cols, rows = int(ask["cols"]), int(ask["rows"])
        if not isinstance(target, str) or not isinstance(server, (str, type(None))):
            raise TypeError("server and target")
    except (WebSocketDisconnect, RuntimeError):
        return
    except (ValueError, KeyError, TypeError, AttributeError):
        await ws.send_json({"error": "the frame after the token says what to show: "
                                     '{"server", "target", "cols", "rows"}'})
        await ws.close()
        return
    opening = asyncio.ensure_future(asyncio.to_thread(PtyView.open, server, target, cols, rows))
    try:
        view = await asyncio.shield(opening)
    except ViewError as e:
        await ws.send_json({"error": str(e)})
        await ws.close()
        return
    except asyncio.CancelledError:
        # Gone while it opened: the view, once open, is closed.
        opening.add_done_callback(lambda f: f.cancelled() or f.exception()
                                  or asyncio.ensure_future(asyncio.to_thread(f.result().close)))
        raise
    down = up = None
    try:
        await ws.send_json({"crop": view.crop.json()})
        down = asyncio.ensure_future(_down(ws, view))
        up = asyncio.ensure_future(_up(ws, view))
        done, _ = await asyncio.wait({down, up}, return_when=asyncio.FIRST_COMPLETED)
        if down in done and not down.cancelled() and not down.exception():
            await ws.close()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for task in (down, up):
            if task is not None:
                _quietly(task)
        # Shielded: the session goes even when the route is cancelled meanwhile.
        await asyncio.shield(asyncio.to_thread(view.close))
