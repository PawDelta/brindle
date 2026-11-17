"""The storage report (``disk.storage_report``) as ``GET /v1/storage`` serves it.

Measuring walks every worktree of every known repo: hundreds of git, ``du``
and tmux reads, about half a minute on a quiet machine and past the request's
deadline on a busy one (the report then lost repos to "deadline passed").
So the daemon measures on a thread of its own, without a request's deadline
and waiting for room like other background work (spawn.waits_for_room), and
the route answers at once with the last report it has: ``refreshing`` says a
measurement is under way or queued, ``measured_at`` when the one shown was
taken (None: none yet), ``error`` why the last measurement failed (None
when it didn't).

Every report walks every known repo whatever ``repo`` says (``repo`` only adds
an unknown one and picks the disk for free space), so measurements never run
side by side: one thread per brindle home measures the wanted reports one
after another. A read while its report is wanted or being measured joins
that; a ``repo`` that isn't a git repo reads as no repo, so made-up values
can't queue more. A failed measurement keeps the last good report, says why,
and is tried again only on ``fresh`` or after FAILED_SECONDS, so polling
readers stop instead of starting the same failing walk every few seconds.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# A report older than this is measured again when it is next read.
STALE_SECONDS = 300.0
# After a failed measurement, a read (without ``fresh``) tries again only this much later.
FAILED_SECONDS = 300.0

# The shape of a report when none has been measured yet (measured_at None).
EMPTY = {"worktrees": [], "categories": [], "reclaimable": 0, "free_bytes": None,
         "low_disk": None, "dead_branches": []}

Key = tuple[str, str | None]    # (brindle home, repo)


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


def _repo(repo: str | None) -> str | None:
    """``repo`` as the key's: its real path when it is a git repo, else None."""
    if not repo:
        return None
    path = os.path.realpath(repo)
    return path if os.path.exists(os.path.join(path, ".git")) else None


class StorageReports:
    """The last report per (brindle home, repo), and the measurements wanted."""

    def __init__(self, measure: Callable[[str | None], dict] = _measure,
                 start: Callable[[Callable[[], None]], None] = _start,
                 clock: Callable[[], float] = time.time):
        self._measure, self._start, self._clock = measure, start, clock
        self._lock = threading.Lock()
        self._last: dict[Key, tuple[float, dict]] = {}
        self._failed: dict[Key, tuple[float, str]] = {}     # the last measurement's failure, when it failed
        self._wanted: dict[Key, int] = {}       # queued (in order) -> the generation it was asked in
        self._current: dict[str, Key] = {}      # home -> the report its thread is measuring
        self._workers: set[str] = set()         # homes with a measuring thread
        self._generation = 0     # bumped by forget: a measurement asked for before it isn't kept

    @staticmethod
    def _key(repo: str | None) -> Key:
        from brindle.config import brindle_home

        return str(brindle_home()), _repo(repo)

    def get(self, repo: str | None, *, fresh: bool = False) -> dict[str, Any]:
        """The last report for ``repo`` now. A measurement is queued when there
        is no report or it is older than STALE_SECONDS (unless the last one
        failed under FAILED_SECONDS ago), or when ``fresh`` asks for one;
        never a second for a report already wanted or being measured."""
        key = self._key(repo)
        home = key[0]
        with self._lock:
            now = self._clock()
            last, failed = self._last.get(key), self._failed.get(key)
            due = fresh or ((last is None or now - last[0] >= STALE_SECONDS)
                            and (failed is None or now - failed[0] >= FAILED_SECONDS))
            busy = key in self._wanted or self._current.get(home) == key
            if due and not busy:
                self._wanted[key] = self._generation
                self._failed.pop(key, None)
                failed, busy = None, True
            starting = busy and home not in self._workers
            if starting:
                self._workers.add(home)
        if starting:
            try:
                self._start(lambda: self._work(home))
            except Exception:   # noqa: BLE001 - no thread: nothing runs, so nothing is wanted
                with self._lock:
                    self._workers.discard(home)
                    for k in [k for k in self._wanted if k[0] == home]:
                        del self._wanted[k]
                raise
        at, report = last if last is not None else (None, EMPTY)
        return {**report, "measured_at": at, "refreshing": busy,
                "error": failed[1] if failed is not None else None}

    def forget(self) -> None:
        """Drop every kept report (after a clean): the next read measures afresh,
        and a measurement under way, asked for before the clean, is not kept
        (one still queued will start after it, so it counts)."""
        with self._lock:
            self._last.clear()
            self._failed.clear()
            self._generation += 1
            for k in self._wanted:
                self._wanted[k] = self._generation

    def _work(self, home: str) -> None:
        """This home's thread: measure the wanted reports one at a time."""
        while True:
            with self._lock:
                key = next((k for k in self._wanted if k[0] == home), None)
                if key is None:
                    self._workers.discard(home)
                    self._current.pop(home, None)
                    return
                generation = self._wanted.pop(key)
                self._current[home] = key
            self._run(key, generation)

    def _run(self, key: Key, generation: int) -> None:
        """Measure; kept only if no clean (``forget``) came since it was asked for."""
        began = self._clock()
        report, error = None, None
        try:
            report = self._measure(key[1])
        except Exception as e:  # noqa: BLE001 - kept as the report's error; the last good report stays
            log.warning("brindle: couldn't measure disk use for %s", key[1] or "every repo", exc_info=True)
            error = f"{type(e).__name__}: {e}"[:500]
        with self._lock:
            self._current.pop(key[0], None)
            if generation != self._generation:
                return
            if report is not None:
                self._last[key] = (began, report)
            else:
                self._failed[key] = (self._clock(), error or "the measurement failed")


REPORTS = StorageReports()
