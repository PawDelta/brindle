"""The stdio transport ``brindle mcp`` serves on, which can hand its client
over to a newer brindle in place.

A brindle update under a running server leaves it on the old code (see
upgrade.py), and once the new version migrates the database the old code
must stop serving. Instead of refusing every call until the user reconnects,
the server replaces itself between requests: it waits until every request it
was serving has been answered, then execs the installed ``brindle mcp`` on the
same stdin and stdout. The client never sees a restart:

- the request that found the new install isn't served here: it, and every
  byte read past it, goes to the new process unread;
- the client's ``initialize`` is replayed there (its answer dropped, the
  client already has one), so the new process serves an initialized session;
- environment and argv are the same, so the caller's identity is too.

The SDK's own stdio transport can't do this: it reads ahead into buffers that
an exec would lose, and its duplicates of fds 0 and 1 are close-on-exec.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
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
_CHUNK = 65536
_NEVER = object()


@dataclass
class Handover:
    """What the new process needs of the connection: the client's
    ``initialize`` request as sent, whether it sent ``initialized``, and
    the bytes read from stdin but not served."""
    initialize: str | None = None
    initialized: bool = False
    unread: bytes = b""


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
    return Handover(state.get("initialize"), bool(state.get("initialized")), unread)


def _write_handover(handover: Handover) -> int:
    """An unlinked file holding the handover, open on an fd the exec keeps."""
    home = brindle_home()
    home.mkdir(parents=True, exist_ok=True)
    f = tempfile.TemporaryFile(dir=home)
    head = json.dumps({"initialize": handover.initialize, "initialized": handover.initialized})
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
        self._in_flight: set = set()
        self._idle = anyio.Event()
        self._idle.set()
        self._wake = anyio.Event()
        self._failed_on = _NEVER     # the install's stamp when an exec of it last failed
        self.replaced = False

    def wake(self) -> None:
        """Check for a reload now, without waiting for the client's next request."""
        self._wake.set()

    async def serve(self, run: Callable[..., Awaitable[None]]) -> None:
        """Serve until the client closes stdin, or this process is replaced.
        ``run(read_stream, write_stream)`` drives the MCP server."""
        read_send, read_recv = anyio.create_memory_object_stream(0)
        write_send, write_recv = anyio.create_memory_object_stream(0)
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._write_all, write_recv)
            tg.start_soon(run, read_recv, write_send)
            async with read_send:
                await self._replay(read_send)
                await self._read_all(read_send)
            if self.replaced:
                tg.cancel_scope.cancel()

    # -- reading ---------------------------------------------------------------

    async def _replay(self, send) -> None:
        if self._initialize is None:
            return
        init = json.loads(self._initialize)
        init["id"] = _INIT_ID
        await self._dispatch(send, json.dumps(init).encode())
        if self._initialized:
            await self._dispatch(send, _INITIALIZED.encode())

    async def _read_all(self, send) -> None:
        held: list[bytes] | None = None     # lines read while waiting to hand over
        while True:
            if self._wake.is_set():
                self._wake = anyio.Event()
                if held is None and self._should_reload():
                    held = []
            if held is not None and self._idle.is_set():
                if self._hand_over(b"".join(held)):
                    return
                self._failed_on = upgrade.stamp()
                for line in held:
                    await self._dispatch(send, line)
                held = None
                continue
            line = await self._next_line(waiting=held is not None)
            if line is None:
                continue        # idle, or woken: look again
            if line == b"":
                return          # the client closed stdin
            message = _parse(line)
            if held is not None:
                if _answers_old_work(message, self._in_flight):
                    await self._dispatch(send, line)
                else:
                    held.append(line)
                continue
            if isinstance(message, types.JSONRPCRequest) and self._should_reload():
                held = [line]
                continue
            await self._dispatch(send, line)

    def _should_reload(self) -> bool:
        """After an exec failed, only once the install changed again (it was
        mid-update, say): each request would otherwise wait for every other
        to finish, only for the exec to fail again."""
        if self._failed_on is not _NEVER and upgrade.stamp() == self._failed_on:
            return False
        return self._wants_reload(self.reloaded)

    async def _next_line(self, *, waiting: bool) -> bytes | None:
        """The next line from the client (b"" at EOF), or None once this
        connection became idle (``waiting``) or was woken first."""
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
            self._in_flight.add(message.id)
            self._idle = anyio.Event()
            # A request the client cancelled is never answered: it ends here.
            metadata = ServerMessageMetadata(on_request_unanswered=lambda rid=message.id: self._settled(rid))
        elif isinstance(message, types.JSONRPCNotification) and message.method == "notifications/initialized":
            self._initialized = True
        await send.send(SessionMessage(message, metadata))

    # -- writing ---------------------------------------------------------------

    async def _write_all(self, recv) -> None:
        async with recv:
            async for session_message in recv:
                message = session_message.message
                answered = getattr(message, "id", None) if isinstance(
                    message, types.JSONRPCResponse | types.JSONRPCError) else None
                if answered != _INIT_ID:
                    data = (message.model_dump_json(by_alias=True, exclude_unset=True) + "\n").encode()
                    while data:
                        data = data[os.write(self.out_fd, data):]
                if answered is not None:
                    await self._settled(answered)

    async def _settled(self, request_id) -> None:
        self._in_flight.discard(request_id)
        if not self._in_flight:
            self._idle.set()

    # -- handing over ----------------------------------------------------------

    def _hand_over(self, held: bytes) -> bool:
        """Exec the new install with ``held`` and the unread buffer as its
        input. False, with nothing lost, when it couldn't."""
        handover = Handover(self._initialize, self._initialized, held + self._buffer)
        try:
            fd = _write_handover(handover)
        except OSError:
            return False
        os.environ[HANDOVER_ENV] = str(fd)
        logging.getLogger(__name__).warning("brindle mcp: reloading on the installed brindle (pid %d)", os.getpid())
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
        return self.replaced


def _parse(line: bytes):
    try:
        return types.jsonrpc_message_adapter.validate_json(line, by_name=False)
    except Exception:  # noqa: BLE001 - _dispatch reports it
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


def replace() -> bool:
    """Exec the installed ``brindle mcp`` (the same argv this one was started with)."""
    return upgrade.exec_install(sys.argv[1:])
