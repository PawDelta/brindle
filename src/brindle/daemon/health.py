"""The daemon records its own health through brindle.debuglog.

The daemon's watch loop calls :meth:`Monitor.tick` after each timed wait. A
tick measures how late the wait ended (against the deadline set just before
the wait, so maintenance the loop awaits afterwards is not counted), reads the
asyncio default executor's and the heavy lane's waiting work, the gated
processes spawn.py runs, refuses and stops, the load and the room left. A line
is written only when a value changed or every :data:`SLOW_LOG_S`. When the
loop lags or a queue backs up past its threshold the line is a WARNING, once
on entering the stall and then at the slow interval, so a report has the
numbers at hand.
"""

from __future__ import annotations

import asyncio
import os
import time

from brindle import debuglog, spawn

SOURCE = "daemon.health"
SLOW_LOG_S = 60.0       # an unchanged state is still written this often
LAG_WARN_S = 1.0        # the loop waking this late counts as a stall
QUEUE_WARN = 4          # this many waiting tasks in a queue counts as a stall


def _queues(app) -> tuple[int, int]:
    """(default executor's queued jobs, heavy lane's waiting reads). Runs on the event loop."""
    pool = getattr(asyncio.get_running_loop(), "_default_executor", None)
    work = getattr(pool, "_work_queue", None)      # None until something first used the pool
    executor = work.qsize() if work is not None else 0
    lane = getattr(getattr(app, "state", None), "heavy_read_lane", None)
    heavy = lane.workers.statistics().tasks_waiting if lane is not None else 0
    return int(executor), int(heavy)


def _load() -> float:
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def sample(app, lag: float) -> dict:
    """The daemon's health right now; ``lag`` is how late the loop woke."""
    executor, heavy = _queues(app)
    stats = spawn.stats()
    return {"lag_s": round(lag, 3), "executor_queue": executor, "heavy_queue": heavy,
            "children": stats["running"], "refused": stats["refused"],
            "stopped": stats["stopped"], "load": round(_load(), 2),
            "room": round(spawn.room(), 1)}


class Monitor:
    """Turns samples into debug log lines. ``clock`` is injectable for tests."""

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.last_key: tuple | None = None
        self.last_written = float("-inf")
        self.stalled = False

    def tick(self, app, due: float) -> dict:
        """Record the daemon's health after a wait that was due at ``due``."""
        now = self.clock()
        data = sample(app, max(0.0, now - due))
        stalled = (data["lag_s"] >= LAG_WARN_S or data["executor_queue"] >= QUEUE_WARN
                   or data["heavy_queue"] >= QUEUE_WARN)
        if stalled:
            if not self.stalled or now - self.last_written >= SLOW_LOG_S:
                self._write("warning", "daemon stalled: loop lag or queues past threshold", data, now)
            self.stalled = True
            return data
        self.stalled = False
        # Ignore lag when deciding whether state changed: it jitters every tick.
        key = tuple(v for k, v in data.items() if k != "lag_s")
        if key != self.last_key or now - self.last_written >= SLOW_LOG_S:
            self._write("info", "daemon health", data, now)
        return data

    def _write(self, level: str, message: str, data: dict, now: float) -> None:
        debuglog.record(SOURCE, level, message, data)
        self.last_key = tuple(v for k, v in data.items() if k != "lag_s")
        self.last_written = now
