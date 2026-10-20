"""``WS /v1/terminal/{agent}``: an agent's live terminal, or one of the
daemon's action terminals (a sign-in, ``brindle keys set``, an upgrade; see
:func:`open_action`), named by its ``t-...`` id instead of an agent id.

After the guard has taken the token (the socket's first frame), the client gets
a JSON hello, then the pane as binary frames: a seed (the last SEED_LINES of
scrollback and the screen, with colours, and the cursor put back), then every
byte the pane prints. The daemon watches a tmux session through one
control-mode client (:class:`Control`, ``tmux -C attach``): tmux writes what
every pane of the session prints and the replies to the daemon's own commands
into one ordered stream, so a viewer's seed, captured through that same
client, ends exactly where its stream starts (nothing is shown twice, nothing
is lost), and keys go down the client's stdin instead of through a tmux
process each. Each pane's output goes into a ring buffer (:class:`Pane`)
that every viewer of the pane reads from, gathering what arrives within
COALESCE_SECONDS (or COALESCE_BYTES) into one frame. A viewer that falls a
whole ring behind is told ``resync`` and seeded afresh rather than shown a
torn stream. When control mode can't be had, the pane is polled with
capture-pane instead and viewers get the rows that changed.

The client sends JSON control frames: ``{"type": "key", "keys": [...]}`` (tmux
key names) or ``{"type": "key", "text": "..."}`` (typed as is), ``{"type":
"paste", "text": "..."}`` (one bracketed paste, not submitted) and ``{"type":
"resize", "cols": c, "rows": r}``, applied at once, then at most one per RESIZE_SECONDS (the last size always
lands), and viewers are seeded again once it settles.
While a terminal client is attached to the pane's session, tmux's own sizing
rule applies: resizes are skipped and viewers are told ``shared``.

An agent with no pane to stream (exited, or never had one) gets ``no_pane`` and
what is left: the dead pane's last screen, else the last thing its transcript
says; then the socket closes. A locked agent (see identity_lock) gets
``no_pane`` and nothing of its conversation."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import secrets
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from starlette.websockets import WebSocket, WebSocketDisconnect

from brindle import tmux

log = logging.getLogger(__name__)

SEED_LINES = 2000
RING_BYTES = 1 << 20
COALESCE_SECONDS = 0.004       # tmux hands out output a few dozen bytes at a time
COALESCE_BYTES = 64 << 10
CONTROL_LIMIT = 1 << 24        # one %output line can carry a whole pty read, escaped
RESIZE_SECONDS = 0.25
SHARED_SECONDS = 2.0
POLL_SECONDS = 0.1
MAX_SIZE = 1000                 # columns or rows a resize may ask for
WS_NOT_FOUND = 4404
RESET = b"\x1bc"                # RIS: the client's terminal starts from a clean slate
ACTION_SESSION = "brindle-app"  # the daemon's own tmux session: the app's action terminals
ACTION_TAG = "@brindle_terminal"
ACTION_ID = re.compile(r"t-[0-9a-f]{16}")
_opening = threading.Lock()


class Ring:
    """The last ``size`` bytes a pane printed, addressed by absolute offset."""

    def __init__(self, size: int = RING_BYTES) -> None:
        self.size, self.start, self.data = size, 0, bytearray()

    @property
    def end(self) -> int:
        return self.start + len(self.data)

    def append(self, chunk: bytes) -> None:
        self.data += chunk
        over = len(self.data) - self.size
        if over > 0:
            del self.data[:over]
            self.start += over

    def since(self, pos: int) -> bytes | None:
        """What was printed from ``pos`` on; None when that has left the ring."""
        return None if pos < self.start else bytes(self.data[pos - self.start:])


class Viewer:
    def __init__(self, pos: int, shared: bool) -> None:
        self.pos, self.shared = pos, shared
        self.reseed = False             # the pane was resized: seed again
        self.wake = asyncio.Event()


@dataclass
class Reply:
    """What tmux answered one control-mode command: its output lines, whether
    it failed, and where each pane's ring stood when the answer came (the
    output before it is in a capture's answer, the output after is not)."""

    lines: list[bytes]
    error: bool
    ends: dict[str, int]

    @property
    def text(self) -> str:
        return b"\n".join(self.lines).decode("utf-8", "replace")


async def spawn(argv: list[str]) -> asyncio.subprocess.Process:
    """The control client process (tests put a fake here)."""
    return await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=CONTROL_LIMIT, env=tmux.control_env())


class Control:
    """The daemon's one ``tmux -C`` client on a session: what its panes print
    comes up its stdout, in order with the replies to the commands the daemon
    writes to its stdin."""

    def __init__(self, session: str, server: str | None) -> None:
        self.session, self.server = session, server
        self.panes: dict[str, Pane] = {}
        self.closed = False
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._replies: collections.deque[asyncio.Future] = collections.deque()
        self._loop = asyncio.get_running_loop()

    async def open(self) -> None:
        self._proc = await spawn(tmux.control_argv(self.session, self.server))
        attached = self._expect()           # attach-session answers first
        self._reader = asyncio.ensure_future(self._read())
        try:
            reply = await attached
            if reply.error:
                raise tmux.TmuxError(f"tmux attach-session: {reply.text.strip()}")
        except tmux.TmuxError:
            await self.close()
            raise
        # Never a say in how windows are sized: the daemon's client has no screen.
        await self.send("refresh-client -f ignore-size")

    def _expect(self) -> asyncio.Future:
        fut = self._loop.create_future()
        self._replies.append(fut)
        return fut

    def send(self, line: str) -> asyncio.Future:
        """Write one command and get a future of its Reply."""
        if self.closed or self._proc is None or self._proc.stdin is None:
            fut = self._loop.create_future()
            fut.set_exception(tmux.TmuxError(f"the tmux control client on {self.session} has exited"))
            return fut
        fut = self._expect()
        self._proc.stdin.write(line.encode() + b"\n")
        return fut

    async def _read(self) -> None:
        out = self._proc.stdout
        block: list[bytes] | None = None
        guard = b""
        try:
            while True:
                line = await out.readline()
                if not line:
                    break
                if line.endswith(b"\n"):
                    line = line[:-1]
                if block is not None:
                    # A reply ends with the "time number flags" its %begin had.
                    if line.startswith((b"%end ", b"%error ")) and line.split(b" ", 1)[1] == guard:
                        self._answer(block, line.startswith(b"%error"))
                        block = None
                    else:
                        block.append(line)
                elif line.startswith(b"%output "):
                    pane_id, _, data = line[8:].partition(b" ")
                    pane = self.panes.get(pane_id.decode("ascii", "replace"))
                    if pane is not None:
                        pane.printed(tmux.decode_output(data))
                elif line.startswith(b"%begin "):
                    block, guard = [], line.split(b" ", 1)[1]
                elif line.startswith(b"%exit"):
                    break
        except ValueError:          # a line over CONTROL_LIMIT: the stream is lost
            log.warning("brindle: the tmux control stream on %s overflowed", self.session)
        finally:
            self.closed = True
            while self._replies:
                fut = self._replies.popleft()
                if not fut.done():
                    fut.set_exception(tmux.TmuxError(
                        f"the tmux control client on {self.session} has exited"))
            for pane in self.panes.values():
                pane.stream_ended()

    def _answer(self, lines: list[bytes], error: bool) -> None:
        # A reply block is pane-written text between tmux's guard lines, so a
        # program printing a guard-shaped row can cut a capture short and have
        # its next rows read as protocol. It can only garble what its own
        # viewers see (they see everything it prints anyway); it must not
        # kill the stream, so a block nobody is waiting for is dropped.
        if not self._replies:
            log.debug("brindle: stray reply block on %s", self.session)
            return
        fut = self._replies.popleft()
        if not fut.done():
            fut.set_result(Reply(lines, error, {t: p.ring.end for t, p in self.panes.items()}))

    async def close(self) -> None:
        self.closed = True
        if self._reader:
            self._reader.cancel()
        proc = self._proc
        if proc is None:
            return
        if proc.stdin:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 2)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


class Pane:
    """One pane the daemon streams: what it prints, who is watching, its size.
    With a :class:`Control`, its output comes through that; without one
    (``control`` is None) it is polled with capture-pane and viewers get the
    rows that changed."""

    def __init__(self, target: str, server: str | None, control: Control | None) -> None:
        self.target, self.server, self.control = target, server, control
        self.ring = Ring(RING_BYTES)
        self.viewers: set[Viewer] = set()
        self.shared = False
        self.ended = False
        self._manual = False
        self._size: tuple[int, int] | None = None
        self._resize: asyncio.TimerHandle | None = None
        self._next = 0.0                # when the next resize may be applied at once
        self._applied: tuple[int, int] | None = None
        self._runner: asyncio.Task | None = None
        self._dirty = False             # a size was asked for and not yet applied
        self._unseeded = False          # a resize was applied and viewers not seeded since
        self._tasks: list[asyncio.Task] = []
        self._screen: list[str] | None = None
        self._cursor: tuple[int, int] | None = None
        self._closed = False
        self._loop = asyncio.get_running_loop()

    async def run(self, fn, *args, **kwargs):
        """A tmux call on this pane's server, off the event loop."""
        return await self._loop.run_in_executor(
            None, lambda: fn(*args, server=self.server, **kwargs))

    async def open(self) -> None:
        try:
            if self.control is None:
                self._screen, self._cursor = await self._capture_screen()
                self._tasks.append(asyncio.ensure_future(self._poll_screen()))
            await self._check_shared()
        except BaseException:
            await self.close()
            raise
        self._tasks.append(asyncio.ensure_future(self._poll_shared()))

    def printed(self, chunk: bytes) -> None:
        self.ring.append(chunk)
        self._wake()

    def stream_ended(self) -> None:
        self.ended = True
        self._wake()

    def _wake(self) -> None:
        for v in self.viewers:
            v.wake.set()

    def add(self) -> Viewer:
        v = Viewer(self.ring.end, self.shared)
        self.viewers.add(v)
        return v

    async def seed(self, viewer: Viewer) -> bytes:
        """The pane as it stands: scrollback and screen, then the cursor.
        ``viewer.pos`` is set to where its stream must go on from."""
        if self.control is not None:
            # The capture's reply comes after the %output it includes and
            # before the %output it doesn't, so the stream starts at its end.
            capture = self.control.send(
                f"capture-pane -p -e -J -S -{SEED_LINES} -t {self.target}")
            cursor = self.control.send(f"display-message -p -t {self.target} '{tmux.CURSOR_FORMAT}'")
            rows, at = await capture, await cursor
            if rows.error:
                raise tmux.TmuxError(f"tmux capture-pane: {rows.text.strip()}")
            viewer.pos = rows.ends.get(self.target, self.ring.end)
            out = RESET + b"\r\n".join(rows.lines)
            view = tmux.parse_cursor(at.text) if not at.error else None
        else:
            text = await self.run(tmux.capture, self.target, SEED_LINES, True)
            view = await self.run(tmux.pane_view, self.target)
            viewer.pos = self.ring.end
            out = _as_terminal(text)
        if view:
            out += _cursor(view)
        return out

    async def send_keys(self, keys: list[str] | None = None, text: str | None = None) -> None:
        """Type ``text`` as is, or press ``keys`` (tmux key names)."""
        if self.control is None:
            args = ["-l", "--", text] if text else ["--", *(keys or [])]
            await self.run(tmux.send_keys, self.target, *args)
            return
        # "--": nothing the client sends is read as a send-keys flag (-t would retarget it)
        args = f"-l -- {tmux.control_quote(text)}" if text \
            else "-- " + " ".join(tmux.control_quote(k) for k in keys or [])
        await self._command(f"send-keys -t {self.target} {args}")

    async def paste(self, text: str) -> None:
        if self.control is None:
            await self.run(tmux.paste, self.target, text, False)
            return
        buf = f"brindle-{uuid.uuid4().hex[:8]}"
        # "--": a paste starting with "-" is data, not a set-buffer option
        await self._command(f"set-buffer -b {buf} -- {tmux.control_quote(text)}")
        await self._command(f"paste-buffer -p -d -b {buf} -t {self.target}")

    async def _command(self, line: str) -> Reply:
        reply = await self.control.send(line)
        if reply.error:
            raise tmux.TmuxError(f"tmux {line.split(' ', 1)[0]}: {reply.text.strip()}")
        return reply

    def resize(self, cols: int, rows: int) -> None:
        """Size the window to ``cols`` x ``rows``: at once when none was
        applied in the last RESIZE_SECONDS, else at the end of that time, with
        the last size asked for (a dragged window sends dozens)."""
        self._size = (cols, rows)
        self._dirty = True
        if self._resize or self._runner:    # one is waiting or running: it takes the latest size
            return
        self._schedule()

    def _schedule(self) -> None:
        wait = self._next - self._loop.time()
        if wait > 0:
            self._resize = self._loop.call_later(wait, self._fire_resize)
        else:
            self._start_drain()

    def _fire_resize(self) -> None:
        self._resize = None
        self._start_drain()

    def _start_drain(self) -> None:
        self._dirty = False
        self._runner = asyncio.ensure_future(self._drain(self._size))

    async def _drain(self, size: tuple[int, int] | None) -> None:
        """Apply ``size``, then the latest asked for meanwhile: one resize at a
        time and never sooner than RESIZE_SECONDS after the last. When none is
        left to apply, seed the viewers again."""
        try:
            while size and not self._closed:
                self._next = self._loop.time() + RESIZE_SECONDS
                await self._apply_resize(size)
                if not self._dirty:
                    break
                if self._next > self._loop.time():
                    self._runner = None
                    self._schedule()
                    return
                self._dirty = False
                size = self._size
        finally:
            if self._runner is asyncio.current_task():
                self._runner = None
        if self._unseeded and not self._closed:
            # A program that doesn't redraw on SIGWINCH leaves viewers with the
            # old layout; tmux has reflowed it, so seed them again.
            self._unseeded = False
            for v in self.viewers:
                v.reseed = True
            self._wake()

    async def _apply_resize(self, size: tuple[int, int]) -> None:
        if size == self._applied:
            return
        try:
            await self._check_shared()      # a terminal may have attached since the last poll
            if self.shared:
                self._applied = None
                return
            if not self._manual:
                await self.run(tmux.set_window_size_manual, self.target)
                self._manual = True
            await self.run(tmux.resize_window, self.target, *size)
            self._applied = size
            self._unseeded = True
        except tmux.TmuxError:
            log.debug("brindle: can't resize %s", self.target, exc_info=True)

    async def _attached(self) -> int | None:
        """Terminals showing the pane's session (the daemon's own control
        client doesn't count); None when the pane is gone."""
        if self.control is None:
            return await self.run(tmux.attached_terminals, self.target)
        reply = await self.control.send(f"list-clients -t {self.target} -F '#{{client_flags}}'")
        if reply.error:
            return None
        return tmux.count_terminals(line.decode("ascii", "replace") for line in reply.lines)

    async def _check_shared(self) -> None:
        attached = await self._attached()
        if attached is None:
            raise tmux.TmuxError(f"can't find pane: {self.target}")
        shared = attached > 0
        if shared == self.shared:
            return
        self.shared = shared
        if shared and self._manual:     # the terminal's size rules again
            self._manual = False
            self._applied = None
            await self.run(tmux.unset_window_size, self.target)
        self._wake()

    async def _poll_shared(self) -> None:
        while True:
            await asyncio.sleep(SHARED_SECONDS)
            try:
                await self._check_shared()
            except tmux.TmuxError:
                log.debug("brindle: can't read %s", self.target, exc_info=True)

    async def _capture_screen(self) -> tuple[list[str], tuple[int, int] | None]:
        text = await self.run(tmux.capture, self.target, 0, True, join=False)
        return text.removesuffix("\n").split("\n"), await self.run(tmux.pane_view, self.target)

    async def _poll_screen(self) -> None:
        """The no-stream fallback: redraw the rows that changed since the last
        look (a cursor that moved on its own, Enter onto a blank line, counts)."""
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                rows, cursor = await self._capture_screen()
            except tmux.TmuxError:
                self.stream_ended()
                return
            if rows != self._screen or cursor != self._cursor:
                self.printed(_redraw(self._screen, rows, cursor))
                self._screen, self._cursor = rows, cursor

    async def close(self) -> None:
        self._closed = True
        for task in (*self._tasks, self._runner, self._resize):
            if task:
                task.cancel()
        if self._manual:
            try:
                await self.run(tmux.unset_window_size, self.target)
            except tmux.TmuxError:
                log.debug("brindle: can't unsize %s", self.target, exc_info=True)


def _redraw(previous: list[str] | None, rows: list[str], cursor: tuple[int, int] | None) -> bytes:
    """Rows that differ from ``previous``, each erased first so a full-width
    row never leaves the terminal in its pending-wrap state."""
    parts = [f"\x1b[{i + 1};1H\x1b[0m\x1b[2K{row}" for i, row in enumerate(rows)
             if previous is None or i >= len(previous) or previous[i] != row]
    out = ("\x1b[?25l" + "".join(parts)).encode()
    return out + (_cursor(cursor) if cursor else b"") + b"\x1b[?25h"


def _cursor(view: tuple[int, int]) -> bytes:
    return f"\x1b[0m\x1b[{view[1] + 1};{view[0] + 1}H".encode()


class Hub:
    """Every pane the daemon streams, by (tmux server, pane), and the control
    client of each session they are in, by (tmux server, session)."""

    def __init__(self) -> None:
        self.panes: dict[tuple[str | None, str], Pane] = {}
        self.controls: dict[tuple[str | None, str], Control] = {}
        self._lock = asyncio.Lock()

    async def join(self, target: str, server: str | None) -> tuple[Pane, Viewer]:
        async with self._lock:
            pane = self.panes.get((server, target))
            if pane is None:
                pane = await self._open(target, server)
                self.panes[(server, target)] = pane
            return pane, pane.add()

    async def _open(self, target: str, server: str | None) -> Pane:
        loop = asyncio.get_running_loop()
        session = await loop.run_in_executor(
            None, lambda: tmux.pane_session(target, server=server))
        if session is None:
            raise tmux.TmuxError(f"can't find pane: {target}")
        control = self.controls.get((server, session))
        if control is not None and control.closed:     # the session went and came back
            del self.controls[(server, session)]
            await control.close()
            control = None
        if control is None:
            control = Control(session, server)
            try:
                await control.open()
            except (tmux.TmuxError, OSError):
                log.warning("brindle: no tmux control mode on %s, polling %s instead",
                            session, target, exc_info=True)
                control = None
            else:
                self.controls[(server, session)] = control
        pane = Pane(target, server, control)
        if control is not None:
            control.panes[target] = pane
        try:
            await pane.open()
        except BaseException:
            await self._release(pane)
            raise
        return pane

    async def leave(self, pane: Pane, viewer: Viewer) -> None:
        async with self._lock:
            pane.viewers.discard(viewer)
            key = (pane.server, pane.target)
            if not pane.viewers and self.panes.get(key) is pane:
                del self.panes[key]
                await pane.close()
                await self._release(pane)

    async def _release(self, pane: Pane) -> None:
        """Drop ``pane`` from its control client, and the client once it has
        no pane left to stream."""
        control = pane.control
        if control is None or control.panes.get(pane.target) is not pane:
            return
        del control.panes[pane.target]
        key = (pane.server, control.session)
        if not control.panes and self.controls.get(key) is control:
            del self.controls[key]
            await control.close()


def open_action(kind: str, command: list[str], cwd: str | None = None,
                env: dict[str, str] | None = None) -> dict:
    """Run ``command`` in a new window (named ``kind``: the action and what
    it acts on, e.g. "login codex") of the daemon's own tmux session
    (ACTION_SESSION) and return ``{"terminal": id, "started": True}``; the id
    is what ``WS /v1/terminal/{id}`` streams like an agent's pane. While a
    window for ``kind`` is still running, a second press gets that one
    (``started`` False) rather than a second sign-in or upgrade racing it;
    once it has exited, a new one starts. The person types into it there (a
    sign-in, a secret): nothing they type passes through the API. The pane
    stays after the command exits, so its last words can be read."""
    with _opening:      # find-or-create as one step: two presses at once start one
        for _pane, name, alive, terminal_id in tmux.tagged_panes(ACTION_SESSION, ACTION_TAG):
            if alive and name == kind:
                return {"terminal": terminal_id, "started": False}
        terminal_id = "t-" + secrets.token_hex(8)
        cwd = cwd or str(Path.home())
        tmux.ensure_session(ACTION_SESSION, cwd, {})
        tmux.new_window(ACTION_SESSION, kind, cwd, command, env or {}, tag=(ACTION_TAG, terminal_id))
        return {"terminal": terminal_id, "started": True}


def _find_action(terminal_id: str) -> dict:
    found = [(pane, alive) for pane, _, alive, tag in tmux.tagged_panes(ACTION_SESSION, ACTION_TAG)
             if tag == terminal_id]
    if not found:
        return {"type": "error", "code": "not_found", "message": f"no terminal {terminal_id}"}
    pane, alive = found[0]
    if alive:
        return {"type": "terminal", "terminal": terminal_id, "target": pane, "server": tmux.current_server()}
    try:
        text = tmux.capture(pane, lines=SEED_LINES, escapes=True)
    except tmux.TmuxError:
        text = None
    return {"type": "no_pane", "terminal": terminal_id, "reason": "exited",
            "source": "capture" if text else None, "text": text}


def send_input(terminal_id: str, text: str, submit: bool = True) -> bool:
    """Type ``text`` (one line, as is) into the action terminal ``terminal_id``
    and press Enter unless ``submit`` is False. False when there is no such
    terminal still running. Only action terminals: an agent's pane is never
    typed into this way."""
    if not ACTION_ID.fullmatch(terminal_id):
        return False
    found = _find_action(terminal_id)
    if found["type"] != "terminal":
        return False
    tmux.send_keys(found["target"], "-l", "--", text)
    if submit:
        tmux.send_keys(found["target"], "Enter")
    return True


def _find(agent_id: str) -> dict:
    """The hello for ``agent_id``: its pane to stream (``target``, ``server``),
    or ``no_pane`` with what is left of it (``text``), or an error. An action
    terminal's id (see open_action) names that terminal instead."""
    from brindle import agents, identity_lock
    from brindle.db import DB

    if ACTION_ID.fullmatch(agent_id):
        return _find_action(agent_id)
    try:
        a = agents.get(DB(), agent_id)
    except agents.AgentError as e:
        return {"type": "error", "code": "not_found", "message": str(e)}
    who = identity_lock.locked_by(a.id)
    if who:
        return {"type": "no_pane", "agent": a.id, "reason": "locked", "locked_by": who}
    has_window = bool(a.tmux_window) and agents.runs_process(a)
    if has_window and agents.is_alive(a):
        return {"type": "terminal", "agent": a.id, "target": a.tmux_window,
                "server": agents.server_of(a)}
    source, text = _leftover(a)
    return {"type": "no_pane", "agent": a.id, "reason": "exited" if has_window else "no_window",
            "source": source, "text": text}


def _leftover(a) -> tuple[str | None, str | None]:
    """What a pane-less agent left: its dead pane's screen, else the last thing
    its transcript says."""
    from brindle import agents, rewind

    if a.tmux_window:
        try:
            text = tmux.capture(a.tmux_window, lines=SEED_LINES, escapes=True,
                                server=agents.server_of(a))
            if text.strip():
                return "capture", text
        except tmux.TmuxError:
            pass
    path = agents.transcript_file(a)
    said = rewind._last_assistant_text(path) if path else None
    return ("transcript", said) if said else (None, None)


def _as_terminal(text: str) -> bytes:
    return RESET + text.removesuffix("\n").replace("\n", "\r\n").encode()


async def terminal(ws: WebSocket) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()
    hello = await loop.run_in_executor(None, _find, ws.path_params["agent"])
    try:
        if hello["type"] == "error":
            await ws.send_json(hello)
            await ws.close(WS_NOT_FOUND)
        elif hello["type"] == "no_pane":
            text = hello.pop("text", None)
            await ws.send_json(hello)
            if text:
                await ws.send_bytes(_as_terminal(text))
            await ws.close()
        else:
            await _stream(ws, ws.app.state.terminals, hello)
    except (WebSocketDisconnect, RuntimeError):
        pass


async def _stream(ws: WebSocket, hub: Hub, hello: dict) -> None:
    try:
        pane, viewer = await hub.join(hello.pop("target"), hello.pop("server"))
    except (tmux.TmuxError, OSError) as e:
        await ws.send_json({"type": "error", "code": "unavailable", "message": str(e)})
        await ws.close(1011)
        return
    reader = asyncio.ensure_future(_control(ws, pane))
    try:
        await ws.send_json({**hello, "shared": viewer.shared})
        await ws.send_bytes(await pane.seed(viewer))
        while not pane.ended:
            waker = asyncio.ensure_future(viewer.wake.wait())
            await asyncio.wait({reader, waker}, return_when=asyncio.FIRST_COMPLETED)
            waker.cancel()
            if reader.done():
                break
            await _coalesce(pane, viewer)
            try:
                await _flush(ws, pane, viewer)
            except tmux.TmuxError:          # a reseed with the pane gone
                break
    finally:
        reader.cancel()
        await asyncio.shield(hub.leave(pane, viewer))


async def _coalesce(pane: Pane, viewer: Viewer) -> None:
    """Gather what the pane prints next into the same frame: wait up to
    COALESCE_SECONDS, or until COALESCE_BYTES are pending, whichever is
    first."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + COALESCE_SECONDS
    while not pane.ended and pane.ring.end - viewer.pos < COALESCE_BYTES:
        viewer.wake.clear()
        left = deadline - loop.time()
        if left <= 0:
            break
        try:
            await asyncio.wait_for(viewer.wake.wait(), left)
        except asyncio.TimeoutError:
            break
    viewer.wake.clear()


async def _flush(ws: WebSocket, pane: Pane, viewer: Viewer) -> None:
    if viewer.shared != pane.shared:
        viewer.shared = pane.shared
        await ws.send_json({"type": "shared", "shared": pane.shared})
    data = None if viewer.reseed else pane.ring.since(viewer.pos)
    viewer.reseed = False
    if data is None:
        await ws.send_json({"type": "resync"})
        await ws.send_bytes(await pane.seed(viewer))
    elif data:
        viewer.pos += len(data)
        await ws.send_bytes(data)


async def _control(ws: WebSocket, pane: Pane) -> None:
    """The client's control frames, applied in order, until it goes."""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        try:
            frame = json.loads(message.get("text") or "")
            await _apply(pane, frame)
        except ValueError:
            continue
        except tmux.TmuxError:
            log.debug("brindle: control frame for %s failed", pane.target, exc_info=True)


async def _apply(pane: Pane, frame: object) -> None:
    if not isinstance(frame, dict):
        return
    kind = frame.get("type")
    if kind == "key" and isinstance(frame.get("text"), str) and frame["text"]:
        await pane.send_keys(text=frame["text"])
    elif kind == "key" and isinstance(frame.get("keys"), list) and frame["keys"] \
            and all(isinstance(k, str) for k in frame["keys"]):
        await pane.send_keys(keys=frame["keys"])
    elif kind == "paste" and isinstance(frame.get("text"), str) and frame["text"]:
        await pane.paste(frame["text"])
    elif kind == "resize":
        cols, rows = frame.get("cols"), frame.get("rows")
        if isinstance(cols, int) and isinstance(rows, int):
            pane.resize(max(1, min(cols, MAX_SIZE)), max(1, min(rows, MAX_SIZE)))
