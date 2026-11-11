"""The MCP server's stall watchdog: a tools/call unanswered too long is logged once."""

from __future__ import annotations

import json
import os
import threading

import anyio

from brindle import debuglog, mcp_stdio


class _Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_stalled_call_is_recorded_once_with_stacks_and_state(monkeypatch):
    recorded = []
    monkeypatch.setattr(debuglog, "enabled", lambda: True)
    monkeypatch.setattr(debuglog, "record", lambda *a: recorded.append(a) or True)
    conn = mcp_stdio.Connection(-1, -1, wants_reload=lambda _: False, replace=lambda: False)
    clock = _Clock()
    dog = mcp_stdio.StallWatchdog(conn, after=120, clock=clock)
    conn._pending["7"] = ("wait_for_worker", clock.now)

    clock.now += 119
    assert dog.check() == 0                     # not stalled yet
    clock.now += 2
    assert dog.check() == 1
    assert dog.check() == 0                     # one entry per stall, not one per poll

    (source, level, message, detail), = recorded
    assert (source, level) == ("mcp", "warning")
    assert "wait_for_worker" in message
    assert detail["tool"] == "wait_for_worker" and detail["age_s"] == 121.0
    assert {"idle", "deferred_reload", "held_for_reload", "writer_busy"} <= detail["connection"].keys()
    assert any("test_stalled_call" in f for stack in detail["threads"].values() for f in stack)
    json.dumps(detail)

    conn._pending.clear()                       # answered: a later stall of a new call is reported again
    conn._pending["8"] = ("assign", clock.now - 500)
    assert dog.check() == 1


def test_hung_reload_check_is_logged_through_real_read_and_serialization(monkeypatch):
    """A call stuck in the reload check (before dispatch) is tracked, and the entry
    that reaches the log file still names the blocking frame."""
    monkeypatch.setattr(debuglog, "enabled", lambda: True)
    r, w = os.pipe()
    out_r, out_w = os.pipe()
    clock = _Clock()
    seen = []

    def wants_reload(_reloaded):
        clock.now += 121                        # the check hangs for 121 s
        seen.append(dog.check())
        return False

    conn = mcp_stdio.Connection(r, out_w, wants_reload=wants_reload, replace=lambda: False)
    conn._clock = clock
    dog = mcp_stdio.StallWatchdog(conn, after=120)
    os.write(w, b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"assign"}}\n')
    os.close(w)

    async def scenario():
        send, _recv = anyio.create_memory_object_stream(10)
        await conn._read_all(send)

    anyio.run(scenario)
    for fd in (r, out_r, out_w):
        os.close(fd)
    assert seen == [1]
    entry = json.loads(debuglog.log_path().read_text().splitlines()[-1])
    assert entry["detail"]["tool"] == "assign"
    frames = [f for stack in entry["detail"]["threads"].values() for f in stack]
    assert any("wants_reload" in f for f in frames)


def test_watchdog_that_cannot_start_does_not_stop_serving(monkeypatch):
    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    r, w = os.pipe()
    os.close(w)
    conn = mcp_stdio.Connection(r, -1, wants_reload=lambda _: False, replace=lambda: False)

    async def run(recv, send):
        async with recv, send:      # a server that ends when its input does
            async for _ in recv:
                pass

    before = mcp_stdio._serving
    anyio.run(conn.serve, run)
    os.close(r)
    assert mcp_stdio._serving == before


def test_nothing_recorded_while_debug_logs_off(monkeypatch):
    recorded = []
    monkeypatch.setattr(debuglog, "enabled", lambda: False)
    monkeypatch.setattr(debuglog, "record", lambda *a: recorded.append(a) or True)
    conn = mcp_stdio.Connection(-1, -1, wants_reload=lambda _: False, replace=lambda: False)
    clock = _Clock()
    conn._pending["1"] = ("assign", clock.now - 999)
    assert mcp_stdio.StallWatchdog(conn, after=120, clock=clock).check() == 0
    assert recorded == []
