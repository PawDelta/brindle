"""The daemon records its own health through brindle.debuglog.

The daemon's watch loop calls :meth:`Monitor.tick` every few seconds. A tick
measures how late the loop woke up, reads the default-executor and heavy-lane
queues, the gated processes spawn.py runs (and refuses), the load and the
room left. A line is written only when a value changed or every
:data:`SLOW_LOG_S`. When the loop lags or a queue backs up past its threshold
the line is a WARNING, once on entering the stall and then at the slow
interval, so a report has the numbers at hand.
"""

from __future__ import annotations

import os
import time

from brindle import debuglog, spawn

SOURCE = "daemon.health"
SLOW_LOG_S = 60.0       # an unchanged state is still written this often
LAG_WARN_S = 1.0        # the loop waking this late counts as a stall
QUEUE_WARN = 4          # this many waiting tasks in a queue counts as a stall


def _queues(app) -> tuple[int, int]:
    """(default-executor waiting, heavy-lane waiting). Runs on the event loop."""
    import anyio.to_thread

    executor = anyio.to_thread.current_default_thread_limiter().statistics().tasks_waiting
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
            "load": round(_load(), 2), "room": round(spawn.room(), 1)}


class Monitor:
    """Turns samples into debug log lines. ``clock`` is injectable for tests."""

    def __init__(self, interval: float, clock=time.monotonic) -> None:
        self.interval = interval
        self.clock = clock
        self.last_tick = clock()
        self.last_key: tuple | None = None
        self.last_written = float("-inf")
        self.stalled = False

    def tick(self, app) -> dict:
        now = self.clock()
        lag = max(0.0, now - self.last_tick - self.interval)
        self.last_tick = now
        data = sample(app, lag)
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
