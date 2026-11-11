"""The stdio transport ``brindle mcp`` serves on, which can hand its client
over to a newer brindle in place.

A brindle update under a running server leaves it on the old code (see
upgrade.py), and once the new version migrates the database the old code
must stop serving. Instead of refusing every call until the user reconnects,
the server replaces itself between requests: it waits until every request it
was serving has been answered, then execs the installed ``brindle mcp`` on the
same stdin and stdout. The client never sees a restart:

- the request that found the new install isn't served here: it, and every
  byte read past it, goes to the new process unread; unless a long call
  (a handoff, say) keeps the server busy past ``_MAX_HOLD``: then what was
  held is served here, and the reload waits for the next idle moment;
- the client's ``initialize`` is replayed there (its answer dropped, the
  client already has one), so the new process serves an initialized session;
- so are its open ``subscriptions/listen`` requests (MCP 2026-07-28: a
  request whose response is a stream of server events, open for as long as
  the client is connected, and Claude Code keeps one open from the start).
  They are never waited for, as they never end; the new process carries
  them on, and drops its acknowledgement of each the client was already
  sent in full (one never sent, it sends);
- no frame is cut: the exec waits for the one being written to finish;
- environment and argv are the same, so the caller's identity is too.

The SDK's own stdio transport can't do this: it reads ahead into buffers that
an exec would lose, and its duplicates of fds 0 and 1 are close-on-exec.
"""

from __future__ import annotations

import json
import logging
import math
import os
import select
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anyio
import mcp_types as types
from mcp.shared.message import ServerMessageMetadata, SessionMessage

from brindle import upgrade
from brindle.config import brindle_home

HANDOVER_ENV = "BRINDLE_MCP_HANDOVER"
_INIT_ID = "brindle-reload-initialize"   # the replayed initialize's id; its answer isn't written
_INITIALIZED = '{"jsonrpc":"2.0","method":"notifications/initialized"}'
_LISTEN = "subscriptions/listen"
_LISTEN_ACK = "notifications/subscriptions/acknowledged"
_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"     # a listen stream's frames carry its request id
_CHUNK = 65536
_NEVER = object()
# Seconds a request is held for a reload that waits on the connection going
# idle: past that it's served here (a handoff or wait_for_worker in flight can
# take many minutes), and the reload waits for the next idle moment instead.
_MAX_HOLD = 3.0


# -- replies a tool waits on --------------------------------------------------

# Request id (as a string) -> what to call once its response is written, or
# once it never will be (``after_reply``).
_after: dict[str, Callable[[bool], None]] = {}
_serving = 0   # Connections serving in this process


def after_reply(request_id, settle: Callable[[bool], None]) -> bool:
    """Have ``settle(True)`` called once the response to ``request_id`` has
    been written to the client, or ``settle(False)`` once it never will be:
    the client cancelled the request, or went away (EOF, a failed write)
    first. Called on the event loop: ``settle`` must be quick. False, with
    nothing registered, when no Connection serves this process."""
    if not _serving:
        return False
    _after[str(request_id)] = settle
    return True


def _settle(request_id, written: bool) -> None:
    settle = _after.pop(str(request_id), None)
    if settle is not None:
        settle(written)


def _settle_all() -> None:
    for request_id in list(_after):
        _settle(request_id, False)


STALL_AFTER = 120.0     # seconds a tools/call may go unanswered before it's logged
_STALL_POLL = 10.0


class StallWatchdog:
    """A daemon thread that records one debug-log entry per tools/call left
    unanswered past ``after`` seconds: the tool, its age, every thread's stack
    and the connection's state. It never touches the event loop, and nothing
    it does can raise into serving. ``clock`` is injectable for tests."""

    def __init__(self, conn: Connection, *, after: float = STALL_AFTER, poll: float = _STALL_POLL,
                 clock: Callable[[], float] | None = None) -> None:
        self._conn, self._after, self._poll = conn, after, poll
        self._clock = clock or conn._clock
        self._reported: set = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Best-effort: serving goes on without a watchdog if no thread can start."""
        try:
            self._thread = threading.Thread(target=self._run, name="brindle-mcp-watchdog", daemon=True)
            self._thread.start()
            return True
        except Exception:  # noqa: BLE001
            return False

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            self.check()

    def check(self) -> int:
        """Record each newly stalled call; how many were recorded."""
        try:
            now = self._clock()
            pending = list(self._conn._pending.items())
            self._reported &= {rid for rid, _ in pending}
            stalled = [(rid, tool, now - started) for rid, (tool, started) in pending
                       if now - started >= self._after and rid not in self._reported]
            if not stalled:
                return 0
            from brindle import debuglog

            if not debuglog.enabled():
                return 0
            state = self._conn.state()
            stacks = _thread_stacks()
            done = 0
            for rid, tool, age in stalled:
                self._reported.add(rid)
                done += bool(debuglog.record(
                    "mcp", "warning", f"tools/call {tool} unanswered for {age:.0f}s",
                    {"tool": tool, "request_id": str(rid), "age_s": round(age, 1),
                     "connection": state, "threads": stacks}))
            return done
        except Exception:  # noqa: BLE001 - a watchdog must never break serving
            return 0


_STACK_FRAMES = 8       # innermost frames kept per thread
_STACK_THREADS = 24     # debuglog drops a detail past MAX_ENTRY, and cuts any one string at MAX_TEXT


def _thread_stacks() -> dict:
    """Each thread's innermost frames, innermost first, one short string per
    frame: so the blocking call survives debuglog's per-string cut."""
    names = {t.ident: t.name for t in threading.enumerate()}
    out: dict = {}
    for ident, frame in list(sys._current_frames().items())[:_STACK_THREADS]:
        frames = traceback.extract_stack(frame)[::-1][:_STACK_FRAMES]
        out[f"{names.get(ident, '?')}:{ident}"] = [
            f"{os.path.basename(f.filename)}:{f.lineno} {f.name}: {(f.line or '')[:60]}" for f in frames]
    return out


@dataclass
class Handover:
    """What the new process needs of the connection: the client's
    ``initialize`` request as sent, whether it sent ``initialized``, its
    open ``subscriptions/listen`` requests as sent, the ids of those whose
    acknowledgement the client was sent in full, and the bytes read from
    stdin but not served."""
    initialize: str | None = None
    initialized: bool = False
    unread: bytes = b""
    listens: tuple[str, ...] = ()
    acked: tuple = ()


def take_handover() -> Handover | None:
    """The handover this process was exec'd with, if it was; it's read once
    and not passed on to anything this process starts."""
    fd = os.environ.pop(HANDOVER_ENV, None)
    if fd is None:
        return None
    try:
        with os.fdopen(int(fd), "rb") as f:
            f.seek(0)
            head, _, unread = f.read().partition(b"\n")
        state = json.loads(head)
    except (OSError, ValueError):
        return None
    return Handover(state.get("initialize"), bool(state.get("initialized")), unread,
                    tuple(state.get("listens") or ()), tuple(state.get("acked") or ()))


def _write_handover(handover: Handover) -> int:
    """An unlinked file holding the handover, open on an fd the exec keeps."""
    home = brindle_home()
    home.mkdir(parents=True, exist_ok=True)
    f = tempfile.TemporaryFile(dir=home)
    head = json.dumps({"initialize": handover.initialize, "initialized": handover.initialized,
                       "listens": list(handover.listens), "acked": list(handover.acked)})
    f.write(head.encode() + b"\n" + handover.unread)
    f.flush()
    fd = os.dup(f.fileno())
    f.close()
    os.set_inheritable(fd, True)
    return fd


class Connection:
    """One client on ``in_fd``/``out_fd``. ``wants_reload(reloaded)`` is asked
    before each request is served (``reloaded``: this process was exec'd by a
    reload); ``replace()`` execs the new install and returns only if it
    couldn't (False) or the exec was a fake (True). ``std``: in_fd and
    out_fd are private duplicates of fds 0 and 1, to put back before an exec."""

    def __init__(self, in_fd: int, out_fd: int, handover: Handover | None = None, *,
                 wants_reload: Callable[[bool], bool], replace: Callable[[], bool],
                 std: bool = False) -> None:
        self.in_fd, self.out_fd, self.std = in_fd, out_fd, std
        self.reloaded = handover is not None
        self._handover = handover
        self._initialize = handover.initialize if handover else None
        self._initialized = handover.initialized if handover else False
        self._buffer = handover.unread if handover else b""
        self._wants_reload, self._replace = wants_reload, replace
        self._in_flight: set = set()    # requests to answer before a hand over
        self._listens: dict = {}        # open listen streams: request id -> the request as sent
        self._acked: set = set()        # listens whose acknowledgement the client was sent in full
        # Replayed listens whose acknowledgement the client already has (from the replaced process).
        self._client_has_ack: set = set(handover.acked) if handover else set()
        self._idle = anyio.Event()
        self._idle.set()
        self._wrote = anyio.Event()     # set while no frame is part-written: an exec would cut it
        self._wrote.set()
        self._wake = anyio.Event()
        self._failed_on = _NEVER     # the install's stamp when an exec of it last failed
        self._max_hold = _MAX_HOLD
        self._hold_until = 0.0          # when held requests stop waiting for the hand over
        self._deferred = False          # held requests outwaited the hold: reload once idle
        self.replaced = False
        self._pending: dict = {}        # tools/call request id -> (tool name, first seen)
        self._clock: Callable[[], float] = time.monotonic

    def state(self) -> dict:
        """A read-only snapshot of the connection, for the stall watchdog."""
        return {"idle": self._idle.is_set(), "in_flight": len(self._in_flight),
                "deferred_reload": self._deferred,
                "held_for_reload": self._hold_until > time.monotonic(),    # anyio's clock is the loop's monotonic
                "writer_busy": not self._wrote.is_set(), "reloaded": self.reloaded}

    def _track(self, message) -> None:
        """Note a tools/call for the stall watchdog, keeping the time it was first seen."""
        if isinstance(message, types.JSONRPCRequest) and message.method == "tools/call":
            self._pending.setdefault(message.id, (str((message.params or {}).get("name", "?")), self._clock()))

    def wake(self) -> None:
        """Check for a reload now, without waiting for the client's next request."""
        self._wake.set()

    async def serve(self, run: Callable[..., Awaitable[None]]) -> None:
        """Serve until the client closes stdin, or this process is replaced.
        ``run(read_stream, write_stream)`` drives the MCP server."""
        global _serving
        read_send, read_recv = anyio.create_memory_object_stream(0)
        write_send, write_recv = anyio.create_memory_object_stream(0)
        _serving += 1
        watchdog = StallWatchdog(self)
        try:
            watchdog.start()
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._write_all, write_recv)
                tg.start_soon(run, read_recv, write_send)
                async with read_send:
                    await self._replay(read_send)
                    await self._read_all(read_send)
                if self.replaced:
                    tg.cancel_scope.cancel()
        finally:
            watchdog.stop()
            _serving -= 1
            if not _serving:
                _settle_all()   # whatever is still unwritten never will be

    # -- reading ---------------------------------------------------------------

    async def _replay(self, send) -> None:
        if self._initialize is not None:
            init = json.loads(self._initialize)
            init["id"] = _INIT_ID
            await self._dispatch(send, json.dumps(init).encode())
            if self._initialized:
                await self._dispatch(send, _INITIALIZED.encode())
        for line in self._handover.listens if self._handover else ():
            await self._dispatch(send, line.encode())

    async def _read_all(self, send) -> None:
        held: list[bytes] | None = None     # lines read while waiting to hand over
        while True:
            if self._wake.is_set():
                self._wake = anyio.Event()
                if held is None and not self._deferred and self._should_reload():
                    held = self._hold([])
            if self._deferred and self._idle.is_set():
                self._deferred = False      # idle, nothing held: the moment the hold gave up waiting for
                if self._should_reload():
                    held = self._hold([])
            if held is not None and self._idle.is_set():
                if not self._wrote.is_set():
                    await self._wrote.wait()    # finish the frame being written first
                    continue
                attempted = upgrade.stamp()     # what the readiness check is about to look at
                if self._hand_over(b"".join(held)):
                    return
                self._failed_on = attempted
                if upgrade.stamp() != attempted:
                    self.wake()     # the update went on during the attempt: look at what it left
                    # (the loop tries again once the held requests are answered)
                for line in held:
                    await self._dispatch(send, line)
                held = None
                continue
            if held is not None and anyio.current_time() >= self._hold_until:
                # Still busy (a long call in flight): serve what waited here,
                # and hold nothing more until the connection is next idle.
                self._deferred = True
                for line in held:
                    await self._dispatch(send, line)
                held = None
                continue
            line = await self._next_line(waiting=held is not None or self._deferred,
                                         until=self._hold_until if held is not None else math.inf)
            if line is None:
                continue        # idle, or woken: look again
            if line == b"":
                _settle_all()   # the client closed stdin: no reply still to come reaches it
                return
            message = _parse(line)
            self._track(message)    # from the moment it's read: a hung reload check or hand over counts
            if held is not None:
                if _answers_old_work(message, self._in_flight | self._listens.keys()):
                    await self._dispatch(send, line)
                else:
                    held.append(line)
                continue
            if isinstance(message, types.JSONRPCRequest) and not self._deferred and self._should_reload():
                held = self._hold([line])
                continue
            await self._dispatch(send, line)

    def _hold(self, held: list[bytes]) -> list[bytes]:
        """Start holding requests for a hand over, for at most ``_max_hold`` seconds."""
        self._hold_until = anyio.current_time() + self._max_hold
        return held

    def _should_reload(self) -> bool:
        """After an exec failed, only once the install changed again (it was
        mid-update, say): each request would otherwise wait for every other
        to finish, only for the exec to fail again."""
        if self._failed_on is not _NEVER and upgrade.stamp() == self._failed_on:
            return False
        return self._wants_reload(self.reloaded)

    async def _next_line(self, *, waiting: bool, until: float = math.inf) -> bytes | None:
        """The next line from the client (b"" at EOF), or None once this
        connection became idle (``waiting``), was woken, or ``until`` (an
        anyio clock time) passed first."""
        while (nl := self._buffer.find(b"\n")) < 0:
            chunk = None

            async def read(scope):
                nonlocal chunk
                await anyio.wait_readable(self.in_fd)
                chunk = os.read(self.in_fd, _CHUNK)     # no await after the wait: nothing read is lost
                scope.cancel()

            async def interrupt(event, scope):
                await event.wait()
                scope.cancel()

            async with anyio.create_task_group() as tg:
                tg.cancel_scope.deadline = until    # cancels only the waits: no await after the read
                tg.start_soon(read, tg.cancel_scope)
                tg.start_soon(interrupt, self._wake, tg.cancel_scope)
                if waiting:
                    tg.start_soon(interrupt, self._idle, tg.cancel_scope)
            if chunk is None:
                return None
            if chunk == b"":
                rest, self._buffer = self._buffer, b""
                return rest + b"\n" if rest.strip() else b""
            self._buffer += chunk
        line, self._buffer = self._buffer[:nl + 1], self._buffer[nl + 1:]
        return line

    async def _dispatch(self, send, line: bytes) -> None:
        if not line.strip():
            return
        try:
            message = types.jsonrpc_message_adapter.validate_json(line, by_name=False)
        except Exception as exc:  # noqa: BLE001 - the SDK answers a malformed line itself
            await send.send(exc)
            return
        metadata = None
        if isinstance(message, types.JSONRPCRequest):
            if message.method == "initialize" and self._initialize is None:
                self._initialize = line.decode("utf-8", "replace")
            if message.method == _LISTEN:
                self._listens[message.id] = line.decode("utf-8", "replace").strip()
            else:
                self._in_flight.add(message.id)
                self._idle = anyio.Event()
                self._track(message)
            # A request the client cancelled is never answered: it ends here.
            metadata = ServerMessageMetadata(on_request_unanswered=lambda rid=message.id: self._unanswered(rid))
        elif isinstance(message, types.JSONRPCNotification) and message.method == "notifications/initialized":
            self._initialized = True
        elif isinstance(message, types.JSONRPCNotification) and message.method == "notifications/cancelled":
            # Also after its handler returned, while the response still waits to
            # be written: the client stops waiting for it either way.
            _settle((message.params or {}).get("requestId"), False)
        await send.send(SessionMessage(message, metadata))

    # -- writing ---------------------------------------------------------------

    async def _write_all(self, recv) -> None:
        async with recv:
            async for session_message in recv:
                message = session_message.message
                answered = getattr(message, "id", None) if isinstance(
                    message, types.JSONRPCResponse | types.JSONRPCError) else None
                ack = _acknowledges(message)
                if ack in self._client_has_ack:
                    self._client_has_ack.discard(ack)   # the replaced process sent it
                    self._acked.add(ack)
                elif answered != _INIT_ID:
                    data = (message.model_dump_json(by_alias=True, exclude_unset=True) + "\n").encode()
                    self._wrote = anyio.Event()
                    try:
                        await self._write(data)
                    except BaseException:
                        _settle_all()   # the client is gone: nothing more reaches it
                        raise
                    finally:
                        self._wrote.set()
                    if ack is not None:
                        self._acked.add(ack)    # only now does the client have it
                if answered is not None:
                    _settle(answered, True)
                    await self._settled(answered)

    async def _write(self, data: bytes) -> None:
        """Write ``data`` to the client a pipe buffer's worth at a time,
        waiting (not blocking) for room in between: a client slow to read its
        end never stalls the event loop, so a cancel or EOF it sends
        meanwhile is still read, and settles the request before its response
        is through. A pipe only reports writable with PIPE_BUF bytes free,
        so a write of at most that never blocks."""
        view = memoryview(data)
        while view:
            await anyio.wait_writable(self.out_fd)
            view = view[os.write(self.out_fd, view[:select.PIPE_BUF]):]

    async def _unanswered(self, request_id) -> None:
        _settle(request_id, False)
        await self._settled(request_id)

    async def _settled(self, request_id) -> None:
        self._in_flight.discard(request_id)
        self._pending.pop(request_id, None)
        self._listens.pop(request_id, None)     # a stream that ended isn't handed over
        self._acked.discard(request_id)
        if not self._in_flight:
            self._idle.set()

    # -- handing over ----------------------------------------------------------

    def _hand_over(self, held: bytes) -> bool:
        """Exec the new install with ``held`` and the unread buffer as its
        input. False, with nothing lost, when it couldn't."""
        handover = Handover(self._initialize, self._initialized, held + self._buffer,
                            tuple(self._listens.values()), tuple(i for i in self._listens if i in self._acked))
        try:
            fd = _write_handover(handover)
        except OSError:
            return False
        os.environ[HANDOVER_ENV] = str(fd)
        logging.getLogger(__name__).warning(
            "brindle mcp: brindle or its database changed; reloading if the installed brindle is ready (pid %d)",
            os.getpid())
        if self.std:
            os.dup2(self.in_fd, 0)
            os.dup2(self.out_fd, 1)
        try:
            self.replaced = self._replace()
        finally:
            if not self.replaced:
                os.environ.pop(HANDOVER_ENV, None)
                os.close(fd)
                if self.std:
                    _divert()
        if self.replaced:
            self._buffer = b""
        else:
            logging.getLogger(__name__).warning("brindle mcp: no working newer install; not reloaded")
        return self.replaced


def _parse(line: bytes):
    try:
        return types.jsonrpc_message_adapter.validate_json(line, by_name=False)
    except Exception:  # noqa: BLE001 - _dispatch reports it
        return None


def _acknowledges(message):
    """The listen stream ``message`` acknowledges (its request id), or None."""
    if isinstance(message, types.JSONRPCNotification) and message.method == _LISTEN_ACK:
        return ((message.params or {}).get("_meta") or {}).get(_SUBSCRIPTION_ID)
    return None


def _answers_old_work(message, in_flight: set) -> bool:
    """Whether a line read while waiting to hand over belongs to a request
    this process is still serving: the client's answer to a request of ours
    (an elicitation), or a cancel of one of its requests."""
    if isinstance(message, types.JSONRPCResponse | types.JSONRPCError):
        return True
    if isinstance(message, types.JSONRPCNotification) and message.method == "notifications/cancelled":
        return (message.params or {}).get("requestId") in in_flight
    return False


def _divert() -> None:
    """Point fds 0 and 1 away from the wire, so children and stray prints
    can't read the client's input or corrupt its output."""
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    os.dup2(2, 1)


def claim_stdio() -> tuple[int, int]:
    """Private, close-on-exec duplicates of stdin and stdout; fds 0 and 1 are
    diverted while this process serves on them."""
    import fcntl

    in_fd = fcntl.fcntl(0, fcntl.F_DUPFD_CLOEXEC, 3)
    out_fd = fcntl.fcntl(1, fcntl.F_DUPFD_CLOEXEC, 3)
    _divert()
    return in_fd, out_fd


PREFLIGHT_SECONDS = 60.0


def _ready(argv: list[str]) -> bool:
    """Whether ``argv`` starts an MCP server that can take over: ``argv
    --check`` imports its whole server and finds the database no newer than
    its code. An install that is still being written, an interpreter left
    without its brindle, or an install as old as this one all fail it."""
    from brindle import spawn

    env = {k: v for k, v in os.environ.items() if k != HANDOVER_ENV}
    try:
        checked = spawn.read([*argv, "--check"], env=env, input="", timeout=PREFLIGHT_SECONDS)
    except Exception:  # noqa: BLE001 - a launcher that can't run, or hangs, isn't ready
        return False
    return checked.returncode == 0


def replace() -> bool:
    """Exec the installed ``brindle mcp`` (the same argv this one was started
    with), once it has shown it works."""
    return upgrade.exec_install(sys.argv[1:], ready=_ready)
