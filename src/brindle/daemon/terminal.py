"""``WS /v1/terminal/{agent}``: an agent's live terminal.

After the guard has taken the token (the socket's first frame), the client gets
a JSON hello, then the pane as binary frames: a seed (the last SEED_LINES of
scrollback and the screen, with colours, and the cursor put back), then every
byte the pane prints. tmux allows one pipe per pane, so the daemon owns it
(:class:`Pipe`): ``pipe-pane -O`` into a FIFO it reads into a ring buffer that
every viewer of the pane reads from; it unpipes when the last viewer leaves. A
viewer that falls a whole ring behind is told ``resync`` and seeded afresh
rather than shown a torn stream.

The client sends JSON control frames: ``{"type": "key", "keys": [...]}`` (tmux
key names) or ``{"type": "key", "text": "..."}`` (typed as is), ``{"type":
"paste", "text": "..."}`` (one bracketed paste, not submitted) and ``{"type":
"resize", "cols": c, "rows": r}``, applied RESIZE_SECONDS after the last one.
While a terminal client is attached to the pane's session, tmux's own sizing
rule applies: resizes are skipped and viewers are told ``shared``.

An agent with no pane to stream (exited, or never had one) gets ``no_pane`` and
what is left: the dead pane's last screen, else the last thing its transcript
says; then the socket closes. A locked agent (see identity_lock) gets
``no_pane`` and nothing of its conversation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from starlette.websockets import WebSocket, WebSocketDisconnect

from brindle import tmux
from brindle.config import brindle_home

log = logging.getLogger(__name__)

SEED_LINES = 2000
RING_BYTES = 1 << 20
READ_BYTES = 1 << 16
RESIZE_SECONDS = 0.25
SHARED_SECONDS = 2.0
MAX_SIZE = 1000                 # columns or rows a resize may ask for
WS_NOT_FOUND = 4404
RESET = b"\x1bc"                # RIS: the client's terminal starts from a clean slate


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
        self.wake = asyncio.Event()


class Pipe:
    """The daemon's one ``pipe-pane`` on a pane, shared by every viewer of it."""

    def __init__(self, target: str, server: str | None, directory: Path) -> None:
        self.target, self.server = target, server
        self.ring = Ring(RING_BYTES)
        self.viewers: set[Viewer] = set()
        self.shared = False
        self._fifo = directory / f"{uuid.uuid4().hex}.fifo"
        self._fds: list[int] = []
        self._manual = False
        self._size: tuple[int, int] | None = None
        self._resize: asyncio.TimerHandle | None = None
        self._poll: asyncio.Task | None = None
        self._closed = False
        self._loop = asyncio.get_running_loop()

    async def run(self, fn, *args):
        """A tmux call on this pane's server, off the event loop."""
        return await self._loop.run_in_executor(None, lambda: fn(*args, server=self.server))

    async def open(self) -> None:
        self._fifo.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.mkfifo(self._fifo, 0o600)
        try:
            rd = os.open(self._fifo, os.O_RDONLY | os.O_NONBLOCK)
            # Our own write end keeps the FIFO from reading as closed before
            # tmux's `cat` opens it, and after it exits.
            self._fds = [rd, os.open(self._fifo, os.O_WRONLY | os.O_NONBLOCK)]
            self._loop.add_reader(rd, self._readable)
            await self.run(tmux.pipe_pane, self.target, str(self._fifo))
            await self._check_shared()
        except BaseException:
            await self.close()
            raise
        self._poll = asyncio.ensure_future(self._poll_shared())

    def _readable(self) -> None:
        try:
            chunk = os.read(self._fds[0], READ_BYTES)
        except BlockingIOError:
            return
        if chunk:
            self.ring.append(chunk)
            self._wake()

    def _wake(self) -> None:
        for v in self.viewers:
            v.wake.set()

    def add(self) -> Viewer:
        v = Viewer(self.ring.end, self.shared)
        self.viewers.add(v)
        return v

    async def seed(self) -> bytes:
        """The pane as it stands: scrollback and screen, then the cursor."""
        text = await self.run(tmux.capture, self.target, SEED_LINES, True)
        view = await self.run(tmux.pane_view, self.target)
        out = _as_terminal(text)
        if view:
            out += f"\x1b[{view[1] + 1};{view[0] + 1}H".encode()
        return out

    def resize(self, cols: int, rows: int) -> None:
        """Size the window to ``cols`` x ``rows`` once no resize has come for
        RESIZE_SECONDS (a dragged window sends dozens)."""
        self._size = (cols, rows)
        if self._resize:
            self._resize.cancel()
        self._resize = self._loop.call_later(
            RESIZE_SECONDS, lambda: asyncio.ensure_future(self._apply_resize()))

    async def _apply_resize(self) -> None:
        self._resize = None
        if self._closed or not self._size:
            return
        try:
            await self._check_shared()      # a terminal may have attached since the last poll
            if self.shared:
                return
            if not self._manual:
                await self.run(tmux.set_window_size_manual, self.target)
                self._manual = True
            await self.run(tmux.resize_window, self.target, *self._size)
        except tmux.TmuxError:
            log.debug("brindle: can't resize %s", self.target, exc_info=True)

    async def _check_shared(self) -> None:
        view = await self.run(tmux.pane_view, self.target)
        shared = bool(view and view[2])
        if shared == self.shared:
            return
        self.shared = shared
        if shared and self._manual:     # the terminal's size rules again
            self._manual = False
            await self.run(tmux.unset_window_size, self.target)
        self._wake()

    async def _poll_shared(self) -> None:
        while True:
            await asyncio.sleep(SHARED_SECONDS)
            try:
                await self._check_shared()
            except tmux.TmuxError:
                log.debug("brindle: can't read %s", self.target, exc_info=True)

    async def close(self) -> None:
        self._closed = True
        for task in (self._poll, self._resize):
            if task:
                task.cancel()
        if self._fds:
            self._loop.remove_reader(self._fds[0])
        try:
            await self.run(tmux.unpipe_pane, self.target)
            if self._manual:
                await self.run(tmux.unset_window_size, self.target)
        except tmux.TmuxError:
            log.debug("brindle: can't unpipe %s", self.target, exc_info=True)
        finally:
            for fd in self._fds:
                os.close(fd)
            self._fds = []
            self._fifo.unlink(missing_ok=True)


class Hub:
    """Every pipe the daemon owns, by (tmux server, pane)."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory
        self.pipes: dict[tuple[str | None, str], Pipe] = {}
        self._lock = asyncio.Lock()

    async def join(self, target: str, server: str | None) -> tuple[Pipe, Viewer]:
        async with self._lock:
            pipe = self.pipes.get((server, target))
            if pipe is None:
                pipe = Pipe(target, server, self._dir or brindle_home() / "terminals")
                await pipe.open()
                self.pipes[(server, target)] = pipe
            return pipe, pipe.add()

    async def leave(self, pipe: Pipe, viewer: Viewer) -> None:
        async with self._lock:
            pipe.viewers.discard(viewer)
            key = (pipe.server, pipe.target)
            if not pipe.viewers and self.pipes.get(key) is pipe:
                del self.pipes[key]
                await pipe.close()


def _find(agent_id: str) -> dict:
    """The hello for ``agent_id``: its pane to stream (``target``, ``server``),
    or ``no_pane`` with what is left of it (``text``), or an error."""
    from brindle import agents, identity_lock
    from brindle.db import DB

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
        pipe, viewer = await hub.join(hello.pop("target"), hello.pop("server"))
    except (tmux.TmuxError, OSError) as e:
        await ws.send_json({"type": "error", "code": "unavailable", "message": str(e)})
        await ws.close(1011)
        return
    reader = asyncio.ensure_future(_control(ws, pipe))
    try:
        await ws.send_json({**hello, "shared": viewer.shared})
        await ws.send_bytes(await pipe.seed())
        while True:
            waker = asyncio.ensure_future(viewer.wake.wait())
            await asyncio.wait({reader, waker}, return_when=asyncio.FIRST_COMPLETED)
            waker.cancel()
            if reader.done():
                break
            viewer.wake.clear()
            await _flush(ws, pipe, viewer)
    finally:
        reader.cancel()
        await asyncio.shield(hub.leave(pipe, viewer))


async def _flush(ws: WebSocket, pipe: Pipe, viewer: Viewer) -> None:
    if viewer.shared != pipe.shared:
        viewer.shared = pipe.shared
        await ws.send_json({"type": "shared", "shared": pipe.shared})
    data = pipe.ring.since(viewer.pos)
    if data is None:
        viewer.pos = pipe.ring.end
        await ws.send_json({"type": "resync"})
        await ws.send_bytes(await pipe.seed())
    elif data:
        viewer.pos += len(data)
        await ws.send_bytes(data)


async def _control(ws: WebSocket, pipe: Pipe) -> None:
    """The client's control frames, applied in order, until it goes."""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        try:
            frame = json.loads(message.get("text") or "")
            await _apply(pipe, frame)
        except ValueError:
            continue
        except tmux.TmuxError:
            log.debug("brindle: control frame for %s failed", pipe.target, exc_info=True)


async def _apply(pipe: Pipe, frame: object) -> None:
    if not isinstance(frame, dict):
        return
    kind = frame.get("type")
    # "--": nothing the client sends is read as a send-keys flag (-t would retarget it)
    if kind == "key" and isinstance(frame.get("text"), str) and frame["text"]:
        await pipe.run(tmux.send_keys, pipe.target, "-l", "--", frame["text"])
    elif kind == "key" and isinstance(frame.get("keys"), list) and frame["keys"] \
            and all(isinstance(k, str) for k in frame["keys"]):
        await pipe.run(tmux.send_keys, pipe.target, "--", *frame["keys"])
    elif kind == "paste" and isinstance(frame.get("text"), str) and frame["text"]:
        await pipe.run(tmux.paste, pipe.target, frame["text"], False)
    elif kind == "resize":
        cols, rows = frame.get("cols"), frame.get("rows")
        if isinstance(cols, int) and isinstance(rows, int):
            pipe.resize(max(1, min(cols, MAX_SIZE)), max(1, min(rows, MAX_SIZE)))
