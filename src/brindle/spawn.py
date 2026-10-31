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
- :func:`start` (and :func:`detach`) starts a process the caller looks after
  itself: a helper that outlives it, a server, a child it talks to over
  pipes. It waits for room before it starts as an action does.

Every process brindle's daemon can reach starts through here: the repo's
rule checks fail a branch that adds a direct ``subprocess`` start anywhere
else (``direct_spawn_allowed`` in ``.brindle/rules/learned.md`` lists the
CLI-only, interactive and tmux-server files that may).

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
from typing import Any, Callable, Iterator, TypeVar

T = TypeVar("T")

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
    and whether it was stopped early (the client went away).

    ``committed``: an action of this request has started. From then on the
    request is a transaction that must run to its end (the reads after a
    commit, its bookkeeping, a rollback), so neither the deadline nor a stop
    applies to anything it starts any more."""

    at: float
    reason: str | None = None
    committed: bool = False
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


def begin_write() -> None:
    """The request is about to change something itself (a file written from
    Python, say), not through :func:`action`: raise Busy if its deadline has
    passed or it was stopped, so nothing was done; otherwise commit it, so
    the reads, bookkeeping and rollback that follow run to their end."""
    dl = _live()
    if dl is None:
        return
    if dl.stopped or dl.left() <= 0:
        raise Busy(f"nothing was changed: {dl.reason or 'the request deadline passed'}")
    dl.committed = True


def _live() -> Deadline | None:
    """The deadline in force: none once the request has committed to an action."""
    dl = _deadline.get()
    return None if dl is None or dl.committed else dl


def carry(fn: Callable[..., T]) -> Callable[..., T]:
    """``fn``, to run on a thread pool's threads under the caller's deadline:
    ``ThreadPoolExecutor`` threads don't copy the context, so a request's
    reads fanned out to a pool would otherwise have none. Every call shares
    the one Deadline, so stopping the request stops them all."""
    dl = _deadline.get()

    def run(*args: Any, **kwargs: Any) -> T:
        token = _deadline.set(dl)
        try:
            return fn(*args, **kwargs)
        finally:
            _deadline.reset(token)

    return run


# -- room ---------------------------------------------------------------------

_gated = False
_running = 0
_refused = 0
_stopped = 0
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


def no_room() -> str | None:
    """Why a new worker or check run should wait before it starts (what
    :func:`why` says), or None when the machine has room for it. Unlike a
    read or an action this applies in every process, gate on or off: a worker
    or a check run is the heaviest thing brindle starts. It waits until there
    is room, however long, or until it is cancelled."""
    return why() if room() < 1 else None


def _has_room() -> bool:
    return not _gated or _running == 0 or room() >= 1


def stats() -> dict:
    """How many gated processes run now, how many starts were refused
    (skipped or stopped for want of room), and how many reads were stopped
    while running (their request ended, or its deadline passed), since this
    process started."""
    with _cond:
        return {"running": _running, "refused": _refused, "stopped": _stopped}


def _count_stopped() -> None:
    global _stopped
    with _cond:
        _stopped += 1


def _admit(end: float, dl: Deadline | None, skip: bool, what: str, start_anyway: bool = False) -> None:
    """Count a start in, once there's room. Raises Skipped (``skip``) or
    Busy (``end`` passed, or ``dl`` stopped) instead; with ``start_anyway``
    it starts at ``end`` instead of failing."""
    global _running, _refused
    with _cond:
        while True:
            if dl is not None and dl.stopped:
                _refused += 1
                raise Busy(f"{what} was not started: {dl.reason}")
            left = end - time.monotonic()
            if left <= 0 and not start_anyway:   # past it: never start, room or not
                _refused += 1
                raise Busy(f"{what} was not started: "
                           + (why() if not _has_room() else "the request's deadline passed"))
            if left <= 0 or _has_room():
                break
            if skip:
                _refused += 1
                raise Skipped(f"{what} skipped: {why()}")
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


def _name(argv: list[str] | str) -> str:
    return " ".join(argv.split()[:2] if isinstance(argv, str) else argv[:2])


def left(seconds: float) -> float:
    """``seconds``, cut to what is left of the request's deadline (none once
    the request has committed to an action): the limit for a process the
    caller started with :func:`start` as a read and stops itself."""
    dl = _live()
    return seconds if dl is None else max(0.0, min(seconds, dl.left()))


def start(argv: list[str] | str, *, write: bool = True, until: float | None = None,
          **popen: Any) -> subprocess.Popen:
    """Start a process the caller looks after itself (a helper that outlives
    it, a server, a child it talks to over pipes) and return its Popen.

    Like an action it may wait for room before it starts: until the
    request's deadline (then Busy, and nothing was started), or with no
    deadline up to ACTION_WAIT, after which it starts regardless. ``write``:
    it changes something, so once it starts the request commits
    (Deadline.committed), as after an action. A read (``write=False``)
    doesn't, and the caller bounds how long it runs (see :func:`left`).
    ``until`` (``time.monotonic()``): the caller's own budget, which ends
    the wait for room too: past it (or the deadline, whichever is sooner)
    the start is refused with Busy, never made late.

    It isn't counted in running() once started, and it runs at normal
    priority: it may outlive the request, and whatever it starts (a worker's
    agents, a server's clients) would inherit a background priority."""
    dl = _live()
    if dl is not None:
        _admit(dl.at if until is None else min(dl.at, until), dl, False, _name(argv))
    elif until is not None:
        _admit(until, None, False, _name(argv))
    else:
        _admit(time.monotonic() + ACTION_WAIT, None, False, _name(argv), start_anyway=True)
    try:
        proc = subprocess.Popen(argv, **popen)
        if write and dl is not None:
            dl.committed = True
        return proc
    finally:
        _release()


def detach(argv: list[str], **popen: Any) -> subprocess.Popen:
    """:func:`start` a helper that outlives this process: its own session, no
    terminal (stdin and output to /dev/null unless given)."""
    for name in ("stdin", "stdout", "stderr"):
        popen.setdefault(name, subprocess.DEVNULL)
    return start(argv, start_new_session=True, **popen)


def read(argv: list[str], *, cwd: Any = None, env: dict[str, str] | None = None,
         input: str | bytes | None = None, text: bool = True, timeout: float | None = None,
         skip: bool = False, reap: bool = False) -> subprocess.CompletedProcess:
    """Run a command that changes nothing and return what it printed, like
    ``subprocess.run(capture_output=True)``.

    It gets only the time left on the request's deadline (and at most
    ``timeout``), including any wait for room. Past that, or when the
    request is stopped, its process group is stopped (SIGTERM, then SIGKILL
    after GRACE) and Busy is raised; past its own ``timeout`` (sooner than
    any deadline) it raises ``subprocess.TimeoutExpired``, as
    ``subprocess.run`` would. ``skip``: raise Skipped at once when there's
    no room instead of waiting. ``reap``: once it has exited, SIGKILL
    whatever it left running in its process group (a cloud CLI's local auth
    server). Once the request has started an action
    (Deadline.committed), only ``timeout`` applies."""
    dl = _live()
    now = time.monotonic()
    own_end = now + timeout if timeout is not None else math.inf
    end = min(own_end, dl.at) if dl is not None else own_end
    _admit(end, dl, skip, _name(argv))
    try:
        return _run_read(argv, cwd, env, input, text, timeout, dl, end, own_end, reap)
    finally:
        _release()


def _run_read(argv: list[str], cwd: Any, env: dict[str, str] | None, input: str | bytes | None,
              text: bool, timeout: float | None, dl: Deadline | None, end: float,
              own_end: float, reap: bool = False) -> subprocess.CompletedProcess:
    env = {**(os.environ if env is None else env), "GIT_OPTIONAL_LOCKS": "0"}
    # Its own session, so stopping it reaches everything it started (git's hooks, a pager).
    p = subprocess.Popen(_background(argv), cwd=cwd, env=env, text=text, start_new_session=True,
                         stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    first = True
    try:
        while True:
            if dl is not None and dl.committed:   # another thread of the request started an action
                dl, end = None, own_end
            left = end - time.monotonic()
            if left <= 0 or (dl is not None and dl.stopped):
                break
            try:
                out, err = p.communicate(input if first else None, timeout=min(POLL, left))
            except subprocess.TimeoutExpired:
                first = False
                continue
            if reap:     # (the group id could in theory be reused once it is empty)
                _signal_group(p.pid, signal.SIGKILL)
            return subprocess.CompletedProcess(argv, p.returncode, out, err)
    except BaseException:
        _count_stopped()
        stop(p)
        raise
    _count_stopped()
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
    starts regardless. Once started, no deadline stops it, nor anything else
    the request starts after it (Deadline.committed); ``timeout`` is the
    caller's own hard limit, as with ``subprocess.run``."""
    dl = _live()
    if dl is not None:
        _admit(dl.at, dl, False, _name(argv))
        dl.committed = True      # admitted: the rest of the request is its transaction
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
