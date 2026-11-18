"""The pre-PR verification, run through brindle so the sidebar shows it.

A supervisor verifies a branch before opening a PR: the full suite with
per-test coverage, the serial pass, then ``scripts/check_new_tests.py``. Run from
the supervisor's own shell, the sidebar can't tell, and shows "Working". Run
through ``run`` the session's checks mark (``autopilot.set_checking``, the one a
milestone check uses) is set for as long as it runs and cleared when it ends,
however it ends, so the row reports the ``checks`` stage meanwhile.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from brindle import autopilot, bus, spawn
from brindle.db import DB

STEPS: list[tuple[str, list[str]]] = [
    ("suite", ["uv", "run", "pytest", "-q", "-n", "2", "-m", "not serial",
               "--cov=brindle", "--cov=scripts", "--cov-branch", "--cov-context=test"]),
    ("serial", ["uv", "run", "pytest", "-q", "-m", "serial", "tests",
                "--cov=brindle", "--cov=scripts", "--cov-branch", "--cov-context=test", "--cov-append"]),
    ("new tests", ["uv", "run", "python", "scripts/check_new_tests.py", "--base", "origin/main"]),
]
TAIL_LINES = 15
BEAT = autopilot.CHECKING_TIMEOUT / 3   # seconds between refreshes of the checking mark


@dataclass
class Step:
    name: str
    code: int
    tail: str


@dataclass
class Result:
    steps: list[Step] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.steps) and all(s.code == 0 for s in self.steps)

    def render(self) -> str:
        lines = [f"pre-PR check: {'PASSED' if self.passed else 'FAILED'}"]
        for s in self.steps:
            lines.append(f"- {s.name}: {'ok' if s.code == 0 else f'failed (exit {s.code})'}")
            if s.code != 0 and s.tail:
                lines.append("  " + s.tail.replace("\n", "\n  "))
            elif s.tail:
                lines.append("  " + s.tail.splitlines()[-1])   # the summary line: counts, gate verdict
        return "\n".join(lines)


def _run(argv: list[str], cwd: str) -> tuple[int, str]:
    p = spawn.action(argv, cwd=cwd)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class Busy(Exception):
    """A check is already running for the session (a milestone check or another pr-check)."""


class _Mark:
    """The session's checking mark, claimed atomically and kept alive while held.

    ``autopilot.checking`` lets a mark lapse after 900 s, so a heartbeat
    refreshes it; every write is conditional on our own stamp, so a mark
    someone else owns is never touched."""

    def __init__(self, db: DB, root_id: str) -> None:
        self.db, self.root_id = db, root_id
        self.stamp = 0.0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def _swap(self, db: DB, old: float | None, new: float | None) -> bool:
        if old is None:   # claim: only when nothing live holds it
            now = time.time()
            cur = db.conn.execute(
                "UPDATE autopilot SET checking_since=? WHERE root_id=? AND (checking_since IS NULL OR checking_since<?)",
                (new, self.root_id, now - autopilot.CHECKING_TIMEOUT))
        else:
            cur = db.conn.execute("UPDATE autopilot SET checking_since=? WHERE root_id=? AND checking_since=?",
                                  (new, self.root_id, old))
        return cur.rowcount == 1

    def __enter__(self) -> "_Mark":
        stamp = time.time()
        if not self._swap(self.db, None, stamp):
            raise Busy
        self.stamp = stamp
        bus.publish_agent(self.db, "autopilot.checking", self.root_id, checking=True)
        self.thread = threading.Thread(target=self._beat, daemon=True)
        self.thread.start()
        return self

    def _beat(self) -> None:
        db = DB(self.db.path)   # a connection belongs to its thread
        while not self.stop.wait(BEAT):
            self.refresh(db)

    def refresh(self, db: DB) -> None:
        with self.lock:
            new = time.time()
            if self._swap(db, self.stamp, new):
                self.stamp = new

    def __exit__(self, *exc) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        with self.lock:
            self._swap(self.db, self.stamp, None)
        bus.publish_agent(self.db, "autopilot.checking", self.root_id, checking=False)


def run(db: DB, root_id: str, path: str, runner: Callable[[list[str], str], tuple[int, str]] = _run) -> Result:
    """Run the steps in ``path`` (stopping at the first failure), with session
    ``root_id`` marked as checking throughout. Raises ``Busy`` when a check is
    already running for it."""
    result = Result()
    with _Mark(db, root_id):
        for name, argv in STEPS:
            code, out = runner(argv, path)
            result.steps.append(Step(name, code, "\n".join(out.strip().splitlines()[-TAIL_LINES:])))
            if code != 0:
                break
    return result


def run_for_caller(path: str) -> str:
    """The CLI and MCP entry: the caller's own session, from BRINDLE_AGENT_ID."""
    db = DB()
    agent_id = os.environ.get("BRINDLE_AGENT_ID")
    if not agent_id or db.get_agent(agent_id) is None:
        return "Not running as a brindle agent."
    root_id = autopilot.root_of(db, agent_id)
    if db.get_autopilot(root_id) is None:
        return "Autopilot isn't on for this session, so the sidebar has no row to mark."
    try:
        return run(db, root_id, path).render()
    except Busy:
        return "A check is already running for this session; wait for it to finish."
