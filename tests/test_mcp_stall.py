"""The MCP server's stall watchdog: a tools/call unanswered too long is logged once."""

from __future__ import annotations

import json

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
    assert any("test_stalled_call" in stack for stack in detail["threads"].values())
    json.dumps(detail)

    conn._pending.clear()                       # answered: a later stall of a new call is reported again
    conn._pending["8"] = ("assign", clock.now - 500)
    assert dog.check() == 1


def test_nothing_recorded_while_debug_logs_off(monkeypatch):
    recorded = []
    monkeypatch.setattr(debuglog, "enabled", lambda: False)
    monkeypatch.setattr(debuglog, "record", lambda *a: recorded.append(a) or True)
    conn = mcp_stdio.Connection(-1, -1, wants_reload=lambda _: False, replace=lambda: False)
    clock = _Clock()
    conn._pending["1"] = ("assign", clock.now - 999)
    assert mcp_stdio.StallWatchdog(conn, after=120, clock=clock).check() == 0
    assert recorded == []
