"""One load-aware gate for the processes brindle's daemon starts.

The daemon used to start any number of git, tmux and ``ps`` processes at
once, at full priority, however busy the machine was, and kept them running
after the app had stopped waiting for the answer. This module is the one way
in:

- :func:`read` starts a process that changes nothing (``git status``, ``git
  log``, ``tmux list-panes``, ``ps``). It may wait for room, but only until
  the request's deadline (see :func:`deadline`) or its own ``timeout``; at
  that point, or when the request is stopped (the app disconnected), its
  whole process group gets SIGTERM, then SIGKILL after :data:`GRACE`.
  Stopping a read loses nothing: the next request computes it again. Every
  read runs with ``GIT_OPTIONAL_LOCKS=0`` so git never takes the index lock
  for it, and git removes its own lock files on SIGTERM anyway.
- :func:`action` starts a process that changes something (a merge, a commit,
  a checkout). It may wait for room *before* it starts, never past the
  request's deadline (then :class:`Busy`: nothing was done), and once started
  it is never stopped by a deadline.

Room (:func:`room`) is how many more processes the machine can take: its
cores, minus the current one-minute load, minus this process's own running
children, shrunk when the kernel reports memory pressure. A start needs at
least one unit of room, except that one process of ours may always run, so
the daemon never stalls entirely. Children start at background priority
(``taskpolicy -b`` on macOS, ``nice`` elsewhere).

The gate (waiting for room, background priority) is on only in the daemon
(:func:`enable`); in the CLI, MCP server and hooks, reads and actions start
at once, as they always have. Deadlines and the read's clean stop apply
everywhere.
"""

from __future__ import annotations

import contextlib
import contextvars
import ctypes
import ctypes.util
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

GRACE = 2.0          # seconds a stopped read has between SIGTERM and SIGKILL
POLL = 0.25          # seconds between checks while a read runs or a start waits
ACTION_WAIT = 30.0   # seconds an action with no deadline waits for room before starting anyway
NICE = 10            # the niceness of background children where taskpolicy doesn't exist

# kern.memorystatus_vm_pressure_level (macOS): 1 normal, 2 warn, 4 critical.
PRESSURE_WARN, PRESSURE_CRITICAL = 2, 4


class Busy(RuntimeError):
    """The machine had no room for the process before the deadline, or a
    read was stopped at its request's deadline or because the request was
    abandoned. For an action: nothing was started, so asking again is safe."""


class Skipped(Busy):
    """No room, and the caller (a background loop) asked to skip this round
    rather than wait for it."""


@dataclass
class Deadline:
    """When a request's reads must be done (``at``, ``time.monotonic()``),
    and whether it was stopped early (the client went away)."""

    at: float
    reason: str | None = None
    _stopped: threading.Event = field(default_factory=threading.Event, repr=False)

    def left(self) -> float:
        return self.at - time.monotonic()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def stop(self, reason: str = "the request was abandoned") -> None:
        """Stop this request's reads now, and fail any still waiting for room."""
        if not self._stopped.is_set():
            self.reason = reason
            self._stopped.set()
        with _cond:
            _cond.notify_all()


_deadline: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar("brindle_spawn_deadline",
                                                                           default=None)


@contextlib.contextmanager
def deadline(seconds: float) -> Iterator[Deadline]:
    """Give the reads started inside (and in threads that copy this context,
    as Starlette's thread pool and ``asyncio.to_thread`` do) ``seconds`` in
    all. A deadline already in force that ends sooner wins."""
    outer = _deadline.get()
    dl = Deadline(time.monotonic() + seconds)
    if outer is not None and outer.at < dl.at:
        dl.at = outer.at
    token = _deadline.set(dl)
    try:
        yield dl
    finally:
        _deadline.reset(token)


def current() -> Deadline | None:
    return _deadline.get()


# -- room ---------------------------------------------------------------------

_gated = False
_running = 0
_cond = threading.Condition()


def enable(on: bool = True) -> None:
    """Turn the gate on for this process (the daemon does, at start)."""
    global _gated
    _gated = on


def running() -> int:
    """How many processes this process started through the gate are running."""
    return _running


def _cores() -> int:
    return os.cpu_count() or 1


def _load() -> float:
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


_libc: Any = None


def pressure() -> int:
    """The kernel's memory-pressure level (macOS: 1 normal, 2 warn, 4
    critical), or 0 where it can't be read."""
    global _libc
    if sys.platform != "darwin":
        return 0
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        level = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(level))
        if _libc.sysctlbyname(b"kern.memorystatus_vm_pressure_level", ctypes.byref(level),
                              ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
            return 0
        return level.value
    except (OSError, AttributeError):
        return 0


def room() -> float:
    """How many more processes the machine can take now: cores minus the
    one-minute load minus this process's running children, halved under
    memory-pressure warnings and nothing at all when it's critical. May be
    zero or negative; a start needs at least 1 (see :func:`read`)."""
    level = pressure()
    if level >= PRESSURE_CRITICAL:
        return 0.0 - running()
    free = _cores() - _load() - running()
    return free / 2 if level == PRESSURE_WARN and free > 0 else free


def why() -> str:
    """What room() saw, for a Busy message."""
    level = pressure()
    mem = {PRESSURE_WARN: ", memory pressure", PRESSURE_CRITICAL: ", critical memory pressure"}
    return (f"the machine is busy (load {_load():.1f} on {_cores()} cores, "
            f"{running()} of brindle's processes running{mem.get(level, '')})")


def _has_room() -> bool:
    return not _gated or _running == 0 or room() >= 1


def _admit(end: float, dl: Deadline | None, skip: bool, what: str, start_anyway: bool = False) -> None:
    """Count a start in, once there's room. Raises Skipped (``skip``) or
    Busy (``end`` passed, or ``dl`` stopped) instead; with ``start_anyway``
    it starts at ``end`` instead of failing."""
    global _running
    with _cond:
        while True:
            if dl is not None and dl.stopped:
                raise Busy(f"{what} was not started: {dl.reason}")
            if _has_room():
                break
            if skip:
                raise Skipped(f"{what} skipped: {why()}")
            left = end - time.monotonic()
            if left <= 0:
                if start_anyway:
                    break
                raise Busy(f"{what} was not started: {why()}")
            _cond.wait(min(POLL, left))
        _running += 1


def _release() -> None:
    global _running
    with _cond:
        _running -= 1
        _cond.notify_all()


_prefix: list[str] | None = None


def _background(argv: list[str]) -> list[str]:
    """``argv`` at background priority, when the gate is on. Both prefixes
    exec the command, so the child keeps the pid it was started with."""
    global _prefix
    if not _gated:
        return argv
    if _prefix is None:
        if sys.platform == "darwin":
            tp = shutil.which("taskpolicy") or ("/usr/sbin/taskpolicy"
                                                 if os.path.exists("/usr/sbin/taskpolicy") else None)
            _prefix = [tp, "-b"] if tp else []
        else:
            nice = shutil.which("nice")
            _prefix = [nice, "-n", str(NICE)] if nice else []
    return [*_prefix, *argv]


# -- starting processes -------------------------------------------------------


def _name(argv: list[str]) -> str:
    return " ".join(argv[:2])


def read(argv: list[str], *, cwd: Any = None, env: dict[str, str] | None = None,
         input: str | bytes | None = None, text: bool = True, timeout: float | None = None,
         skip: bool = False) -> subprocess.CompletedProcess:
    """Run a command that changes nothing and return what it printed, like
    ``subprocess.run(capture_output=True)``.

    It gets only the time left on the request's deadline (and at most
    ``timeout``), including any wait for room. Past that, or when the
    request is stopped, its process group is stopped (SIGTERM, then SIGKILL
    after GRACE) and Busy is raised; past its own ``timeout`` (sooner than
    any deadline) it raises ``subprocess.TimeoutExpired``, as
    ``subprocess.run`` would. ``skip``: raise Skipped at once when there's
    no room instead of waiting."""
    dl = _deadline.get()
    now = time.monotonic()
    own_end = now + timeout if timeout is not None else math.inf
    end = min(own_end, dl.at) if dl is not None else own_end
    _admit(end, dl, skip, _name(argv))
    try:
        return _run_read(argv, cwd, env, input, text, timeout, dl, end, own_end)
    finally:
        _release()


def _run_read(argv: list[str], cwd: Any, env: dict[str, str] | None, input: str | bytes | None,
              text: bool, timeout: float | None, dl: Deadline | None, end: float,
              own_end: float) -> subprocess.CompletedProcess:
    env = {**(os.environ if env is None else env), "GIT_OPTIONAL_LOCKS": "0"}
    # Its own session, so stopping it reaches everything it started (git's hooks, a pager).
    p = subprocess.Popen(_background(argv), cwd=cwd, env=env, text=text, start_new_session=True,
                         stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    first = True
    try:
        while True:
            left = end - time.monotonic()
            if left <= 0 or (dl is not None and dl.stopped):
                break
            try:
                out, err = p.communicate(input if first else None, timeout=min(POLL, left))
            except subprocess.TimeoutExpired:
                first = False
                continue
            return subprocess.CompletedProcess(argv, p.returncode, out, err)
    except BaseException:
        stop(p)
        raise
    out, err = stop(p)
    if dl is not None and dl.stopped:
        raise Busy(f"{_name(argv)} was stopped: {dl.reason}")
    if own_end <= end:
        raise subprocess.TimeoutExpired(argv, timeout or 0.0, out, err)
    raise Busy(f"{_name(argv)} was stopped: the request's deadline passed")


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def stop(p: subprocess.Popen, grace: float | None = None) -> tuple[Any, Any]:
    """Stop a read started in its own session: SIGTERM to its whole group,
    so git can remove its lock files, then SIGKILL to whatever is left after
    ``grace`` seconds. Returns what it had printed."""
    grace = GRACE if grace is None else grace
    out = err = None
    _signal_group(p.pid, signal.SIGTERM)
    try:
        out, err = p.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(p.pid, signal.SIGKILL)   # the leader may be gone and a child of it not
    if p.returncode is None or out is None:
        try:
            out, err = p.communicate(timeout=grace)
        except subprocess.TimeoutExpired:   # a grandchild that left the group holds the pipe
            p.kill()
            for f in (p.stdout, p.stderr):
                if f is not None:
                    f.close()
            p.wait()
    return out, err


def action(argv: list[str], *, cwd: Any = None, env: dict[str, str] | None = None,
           input: str | bytes | None = None, text: bool = True, timeout: float | None = None,
           wait: float | None = None) -> subprocess.CompletedProcess:
    """Run a command that changes something, like
    ``subprocess.run(capture_output=True)``. It may wait for room before it
    starts: until the request's deadline (then Busy, and nothing was done),
    or with no deadline up to ``wait`` seconds (ACTION_WAIT), after which it
    starts regardless. Once started, no deadline stops it; ``timeout`` is
    the caller's own hard limit, as with ``subprocess.run``."""
    dl = _deadline.get()
    if dl is not None:
        _admit(dl.at, dl, False, _name(argv))
    else:
        _admit(time.monotonic() + (ACTION_WAIT if wait is None else wait), None, False,
               _name(argv), start_anyway=True)
    try:
        proc = subprocess.run(_background(argv), cwd=cwd, env=env, text=text, input=input,
                              capture_output=True, timeout=timeout)
        proc.args = argv
        return proc
    finally:
        _release()
