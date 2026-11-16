"""The storage report (``disk.storage_report``) as ``GET /v1/storage`` serves it.

Measuring walks every worktree of every known repo: hundreds of git, ``du``
and tmux reads, about half a minute on a quiet machine and past the request's
deadline on a busy one (the report then lost repos to "deadline passed").
So the daemon measures on a thread of its own, without a request's deadline
and waiting for room like other background work (spawn.waits_for_room), and
the route answers at once with the last report it has: ``refreshing`` says a
measurement is under way, ``measured_at`` when the one shown was taken
(None: none yet). One measurement at a time per brindle home and repo; a
read while one runs joins it rather than starting another.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# A report older than this is measured again when it is next read.
STALE_SECONDS = 300.0

# The shape of a report when none has been measured yet (measured_at None).
EMPTY = {"worktrees": [], "categories": [], "reclaimable": 0, "free_bytes": None,
         "low_disk": None, "dead_branches": []}


def _measure(repo: str | None) -> dict:
    from brindle import disk, spawn
    from brindle.db import DB

    db = DB()
    try:
        with spawn.waits_for_room():
            return disk.storage_report(db, repo)
    finally:
        db.conn.close()


def _start(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="brindle-storage-report", daemon=True).start()


class StorageReports:
    """The last report per (brindle home, repo), and the measurement under way."""

    def __init__(self, measure: Callable[[str | None], dict] = _measure,
                 start: Callable[[Callable[[], None]], None] = _start,
                 clock: Callable[[], float] = time.time):
        self._measure, self._start, self._clock = measure, start, clock
        self._lock = threading.Lock()
        self._last: dict[tuple[str, str | None], tuple[float, dict]] = {}
        self._running: set[tuple[str, str | None]] = set()
        self._generation = 0     # bumped by forget: a measurement begun before it isn't kept

    @staticmethod
    def _key(repo: str | None) -> tuple[str, str | None]:
        from brindle.config import brindle_home

        return str(brindle_home()), repo

    def get(self, repo: str | None, *, fresh: bool = False) -> dict[str, Any]:
        """The last report for ``repo`` now, starting a measurement when there
        is none, it is older than STALE_SECONDS or ``fresh`` asks for one,
        unless one is already running."""
        key = self._key(repo)
        with self._lock:
            last = self._last.get(key)
            due = fresh or last is None or self._clock() - last[0] >= STALE_SECONDS
            starting = due and key not in self._running
            if starting:
                self._running.add(key)
            refreshing, generation = key in self._running, self._generation
        if starting:
            try:
                self._start(lambda: self._run(key, generation))
            except Exception:   # noqa: BLE001 - no thread: nothing runs, so nothing is under way
                with self._lock:
                    self._running.discard(key)
                raise
        at, report = last if last is not None else (None, EMPTY)
        return {**report, "measured_at": at, "refreshing": refreshing}

    def forget(self) -> None:
        """Drop every kept report (after a clean): the next read measures afresh,
        and a measurement under way, begun before the clean, is not kept."""
        with self._lock:
            self._last.clear()
            self._generation += 1

    def _run(self, key: tuple[str, str | None], generation: int) -> None:
        """Measure; kept only if no clean (``forget``) came since it was asked for."""
        began = self._clock()
        try:
            report = self._measure(key[1])
        except Exception:  # noqa: BLE001 - the last report stays; the next read measures again
            log.warning("brindle: couldn't measure disk use for %s", key[1] or "every repo", exc_info=True)
            report = None
        with self._lock:
            self._running.discard(key)
            if report is not None and generation == self._generation:
                self._last[key] = (began, report)


REPORTS = StorageReports()
