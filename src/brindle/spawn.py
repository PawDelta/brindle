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
  Stopping a read loses nothing: the next request computes it again. A
  read still running when this process exits is stopped too: it runs in its
  own session, so nothing else would (see :func:`exit_on_hangup`). Every
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
cores, minus the cores' worth of CPU busy right now (sampled CPU idle time,
not the load average, which on macOS counts throttled threads too), minus
this process's own running children, shrunk when the kernel reports memory
pressure. A start needs at least one unit of room, except that one process
of ours may always run, so the daemon never stalls entirely. Only work
marked with :func:`waits_for_room` (the heavy read lane, background sweeps)
waits for room; a request's own reads and actions start at once. Reads of
that waiting work start at background priority (``taskpolicy -b`` on
macOS, ``nice`` elsewhere); everything else (a request's own reads, actions
such as setup commands, merges and their hooks, check runs, agents) runs at
normal priority: at background priority macOS starves a request's ``ps``
on a busy machine, and a throttled check run on an idle machine only makes
everything behind it wait longer.

The gate (waiting for room, background reads) is on only in the daemon
(:func:`enable`); in the CLI, MCP server and hooks, reads and actions start
at once, as they always have. Deadlines and the read's clean stop apply
everywhere.
"""

from __future__ import annotations

import atexit
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
SAMPLE = 1.0         # seconds a CPU-busy sample stays current
# Past this, a new sample measures afresh over WINDOW (blocking that long) instead
# of averaging since the last one. The daemon's 5 s health tick keeps it fresh.
SAMPLE_STALE = 30.0
WINDOW = 0.2         # seconds a fresh CPU-busy sample measures over

# host_statistics (macOS): the HOST_CPU_LOAD_INFO flavor, CPU_STATE_MAX ticks.
HOST_CPU_LOAD_INFO, CPU_STATES = 3, 4

# kern.memorystatus_vm_pressure_level (macOS): 1 normal, 2 warn, 4 critical.
PRESSURE_WARN, PRESSURE_CRITICAL = 2, 4
CHECK_LOAD_FACTOR = 2.0   # a check holding a slot waits only above this load per core


class Busy(RuntimeError):
    """The machine had no room for the process before the deadline, or a
    read was stopped at its request's deadline or because the request was
    abandoned. For an action: nothing was started, so asking again is safe."""


class ToolsMissing(OSError):
    """macOS without the Command Line Developer Tools: ``/usr/bin/git`` would
    raise Apple's install dialog. An OSError, like a missing program, so every
    caller that already handles one keeps working; git.py and the service layer
    turn it into their typed errors."""


TOOLS_MISSING = ("git needs Apple's Command Line Developer Tools, which aren't installed. "
                 "Install them with `xcode-select --install` (or `brew install git`), then try again.")
_TOOLS_CHECK_SECONDS = 10
_tools_checked = False


def ensure_git() -> None:
    """On macOS, ``/usr/bin/git`` is a shim that pops the install dialog when the
    tools are missing. Ask ``xcode-select -p`` (silent) first and raise ToolsMissing.
    A git elsewhere on PATH (Homebrew) needs no tools. Checked once per process,
    when it passes. Every start, read and action of ``git`` comes through here."""
    global _tools_checked
    if _tools_checked or sys.platform != "darwin" or shutil.which("git") != "/usr/bin/git":
        return
    try:
        probe = read(["xcode-select", "-p"], timeout=_TOOLS_CHECK_SECONDS)
        found = probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        found = False
    if not found:
        raise ToolsMissing(TOOLS_MISSING)
    _tools_checked = True


def _guard(argv: list[str] | str) -> None:
    if not isinstance(argv, str) and argv and os.path.basename(str(argv[0])) == "git":
        ensure_git()


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


# Only work marked here (the heavy read lane, background sweeps) or asked to
# `skip` waits for room. A request's own reads, the caller check and actions
# start at once: on a machine whose load stays above its core count, making
# them wait starved every request the app made (0.5.8).
_waits: contextvars.ContextVar[bool] = contextvars.ContextVar("brindle_spawn_waits_for_room", default=False)


@contextlib.contextmanager
def waits_for_room() -> Iterator[None]:
    """Processes started inside (and in threads that copy this context) wait
    for room before they start; everything else starts at once."""
    token = _waits.set(True)
    try:
        yield
    finally:
        _waits.reset(token)


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


_libc: Any = None
_host: Any = None
_sample: tuple[float, tuple[int, int], float] | None = None   # (when, ticks, busy cores then)
_sample_lock = threading.Lock()


def _cpu_ticks() -> tuple[int, int] | None:
    """The CPUs' (busy, total) ticks since boot, summed over every core, or
    None where they can't be read."""
    global _libc, _host
    if sys.platform == "darwin":
        try:
            if _libc is None:
                _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            if _host is None:
                _libc.mach_host_self.restype = ctypes.c_uint32
                _host = _libc.mach_host_self()   # once: each call takes another port right
            info = (ctypes.c_uint32 * CPU_STATES)()      # user, system, idle, nice
            count = ctypes.c_uint32(CPU_STATES)
            if _libc.host_statistics(ctypes.c_uint32(_host), HOST_CPU_LOAD_INFO, info,
                                     ctypes.byref(count)) != 0:
                return None
            return info[0] + info[1] + info[3], sum(info)
        except (OSError, AttributeError):
            return None
    try:
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    if len(fields) < 4:
        return None
    total = sum(fields[:8])                  # guest time is counted in user already
    return total - fields[3] - (fields[4] if len(fields) > 4 else 0), total   # idle + iowait


def _load() -> float:
    """How many cores' worth of CPU is busy now, from the CPUs' idle time
    over the last second or so (at most :func:`_cores`). Not the load
    average: on macOS that counts throttled runnable threads too, and read 96
    on a machine whose CPUs were 63% idle. Where the CPU time can't be read,
    the one-minute load average, capped at the core count."""
    global _sample
    with _sample_lock:
        now = time.monotonic()
        if _sample is not None and now - _sample[0] < SAMPLE:
            return _sample[2]
        if _sample is None or now - _sample[0] > SAMPLE_STALE:
            base = _cpu_ticks()                 # the last sample says nothing about now
            if base is None:
                return _loadavg()
            threading.Event().wait(WINDOW)
            now = time.monotonic()
        else:
            base = _sample[1]
        ticks = _cpu_ticks()
        if ticks is None:
            return _loadavg()
        busy, total = ((t - b) % 2 ** 32 if sys.platform == "darwin" else t - b
                       for t, b in zip(ticks, base))   # mach's tick counters wrap at 32 bits
        cores = _cores()
        used = cores * busy / total if total > 0 else (_sample[2] if _sample else 0.0)
        _sample = (now, ticks, used)
        return used


def _loadavg() -> float:
    try:
        return min(os.getloadavg()[0], float(_cores()))
    except OSError:
        return 0.0


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
    cores' worth of CPU busy (:func:`_load`) minus this process's running
    children, with nothing at all when memory pressure is critical. May be
    zero or negative; a start needs at least 1 (see :func:`read`)."""
    level = pressure()
    if level >= PRESSURE_CRITICAL:
        return 0.0 - running()
    return _cores() - _load() - running()


def why() -> str:
    """What room() saw, for a Busy message."""
    level = pressure()
    mem = {PRESSURE_WARN: ", memory pressure", PRESSURE_CRITICAL: ", critical memory pressure"}
    return (f"the machine is busy (load {_load():.1f} on {_cores()} cores, "
            f"{running()} of brindle's processes running{mem.get(level, '')})")


def no_room(repo_root: str | None = None) -> str | None:
    """Why a new worker or check run should wait before it starts (what
    :func:`why` says), or None when the machine has room for it. Unlike a
    read or an action this applies in every process, gate on or off: a worker
    or a check run is the heaviest thing brindle starts. It waits until there
    is room (after ``room_wait_max`` its supervisor is told once), or until
    it is cancelled (a supervisor can start a waiting worker anyway:
    ``tasks.start_now``), unless the repo's ``wait_for_room`` is false.
    Low free disk (``disk.low_disk``, "waiting for disk: N GB free") holds it
    the same way, whatever ``wait_for_room`` says."""
    from brindle import disk

    if (low := disk.low_disk(repo_root)) is not None:
        return low
    if room() >= 1:
        return None
    if repo_root is not None:
        from brindle.config import load_repo_config

        if not load_repo_config(repo_root).wait_for_room:
            return None
    return why()


def check_no_room(repo_root: str | None = None) -> str | None:
    """Why a check run holding a check slot should still wait, or None. The
    slots (``check_concurrency``, with the xdist workers split across them)
    already bound check load, so unlike :func:`no_room` this waits only when
    the machine is in real trouble: memory pressure, or a load above
    ``CHECK_LOAD_FACTOR`` times its cores. ``wait_for_room`` false skips it.
    Low free disk holds it too (``disk.low_disk``)."""
    from brindle import disk

    if (low := disk.low_disk(repo_root)) is not None:
        return low
    # Level 2 (warn) alone is not "no room" for a run already holding a slot.
    if pressure() < PRESSURE_CRITICAL and _load() <= CHECK_LOAD_FACTOR * _cores():
        return None
    if repo_root is not None:
        from brindle.config import load_repo_config

        if not load_repo_config(repo_root).wait_for_room:
            return None
    return why()


def forced_starts() -> int:
    """How many workers a supervisor started despite the load (durable: it is
    counted from their tasks, so any process sees every process's)."""
    try:
        from brindle.db import DB

        db = DB()
        try:
            return db.count_forced_tasks()
        finally:
            db.conn.close()
    except Exception:  # noqa: BLE001 - a health number, never a reason to fail
        return 0


def _has_room(waits: bool = True) -> bool:
    return not _gated or not waits or _running == 0 or room() >= 1


def wait_for_room(stop: threading.Event, limit: float = ACTION_WAIT, *, busy_limit: float | None = None) -> None:
    """Hold work that is many processes rather than one (a session coming
    back, lifecycle.resume_after_start) until the gate has room, ``stop`` is
    set, or ``limit`` seconds have passed: it then goes ahead, as an action
    with no deadline does. ``busy_limit``: seconds that are enough when the
    machine is short of CPU only; critical memory pressure gets all of ``limit``,
    even with nothing running (which lets a single process through, _has_room)."""
    began = time.monotonic()
    end = began + limit
    busy_end = end if busy_limit is None else min(end, began + busy_limit)
    with _cond:
        while not (_has_room() and not (_gated and pressure() >= PRESSURE_CRITICAL)) and not stop.is_set():
            now = time.monotonic()
            if now >= end or (now >= busy_end and pressure() < PRESSURE_CRITICAL):
                break
            _cond.wait(POLL)


def stats() -> dict:
    """How many gated processes run now, how many starts were refused
    (skipped or stopped for want of room), and how many reads were stopped
    while running (their request ended, or its deadline passed), since this
    process started."""
    with _cond:
        counts = {"running": _running, "refused": _refused, "stopped": _stopped}
    return {**counts, "forced": forced_starts()}


def _count_stopped() -> None:
    global _stopped
    with _cond:
        _stopped += 1


def _admit(end: float, dl: Deadline | None, skip: bool, what: str, start_anyway: bool = False) -> None:
    """Count a start in, once there's room. Raises Skipped (``skip``) or
    Busy (``end`` passed, or ``dl`` stopped) instead; with ``start_anyway``
    it starts at ``end`` instead of failing."""
    global _running, _refused
    waits = skip or _waits.get()
    with _cond:
        while True:
            fits = _has_room(waits)   # first: sampling the CPU takes a moment, which counts against the deadline
            if dl is not None and dl.stopped:
                _refused += 1
                raise Busy(f"{what} was not started: {dl.reason}")
            left = end - time.monotonic()
            if left <= 0 and not start_anyway:   # past it: never start, room or not
                _refused += 1
                raise Busy(f"{what} was not started: "
                           + (why() if not fits else "the request's deadline passed"))
            if left <= 0 or fits:
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
    """``argv`` at background priority, for a read of work that waits for
    room (the heavy read lane, background sweeps: :func:`waits_for_room`)
    when the gate is on. A request's own reads keep normal priority: at
    background priority macOS starves them on a busy machine (a `ps` 0.03 s
    → 0.8 s at load 12), and every request waited on its caller check
    (0.5.9). Actions never use it: on macOS a background process can sit
    runnable with no CPU on an idle machine. Both prefixes exec the command,
    so the child keeps the pid it was started with."""
    global _prefix
    if not (_gated and _waits.get()):
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
    _guard(argv)
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


def become(argv: list[str]) -> None:
    """Replace this process with ``argv`` (found on PATH), in this process's
    environment: the last step of a pane's shim (brindle.login_shell_env).
    No process is started, so there is nothing to gate or to look after."""
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        print(f"brindle: could not start {argv[0]}: {e.strerror}", file=sys.stderr)
        raise SystemExit(127) from None


@dataclass
class ReadTiming:
    """Process wall time for an observed read, excluding admission waits."""

    process_seconds: float = 0.0


def read(argv: list[str], *, cwd: Any = None, env: dict[str, str] | None = None,
         input: str | bytes | None = None, text: bool = True, timeout: float | None = None,
         skip: bool = False, reap: bool = False, to_its_end: bool = False,
         timing: ReadTiming | None = None) -> subprocess.CompletedProcess:
    """Run a command that changes nothing and return what it printed, like
    ``subprocess.run(capture_output=True)``.

    ``timing`` accumulates process wall time (including cleanup), separately
    from any admission wait; it does not change the command's budget.

    It gets only the time left on the request's deadline (and at most
    ``timeout``), including any wait for room. Past that, or when the
    request is stopped, its process group is stopped (SIGTERM, then SIGKILL
    after GRACE) and Busy is raised; past its own ``timeout`` (sooner than
    any deadline) it raises ``subprocess.TimeoutExpired``, as
    ``subprocess.run`` would. ``skip``: raise Skipped at once when there's
    no room instead of waiting. ``reap``: once it has exited, SIGKILL
    whatever it left running in its process group (a cloud CLI's local auth
    server). Once the request has started an action
    (Deadline.committed), only ``timeout`` applies. ``to_its_end``: a request
    already over still starts nothing, but once started only ``timeout`` applies,
    for a read that stopping would only start over (a keychain lookup, and the
    dialog it may be waiting on)."""
    _guard(argv)
    dl = _live()
    now = time.monotonic()
    own_end = now + timeout if timeout is not None else math.inf
    end = min(own_end, dl.at) if dl is not None else own_end
    _admit(end, dl, skip, _name(argv))
    if to_its_end:
        dl, end = None, own_end
    began = time.monotonic() if timing is not None else None
    try:
        return _run_read(argv, cwd, env, input, text, timeout, dl, end, own_end, reap)
    finally:
        if began is not None:
            timing.process_seconds += time.monotonic() - began
        _release()


def _run_read(argv: list[str], cwd: Any, env: dict[str, str] | None, input: str | bytes | None,
              text: bool, timeout: float | None, dl: Deadline | None, end: float,
              own_end: float, reap: bool = False) -> subprocess.CompletedProcess:
    env = {**(os.environ if env is None else env), "GIT_OPTIONAL_LOCKS": "0"}
    # Its own session, so stopping it reaches everything it started (git's hooks, a pager).
    # Tracked as it starts: an exit in between would miss it and leave it running.
    with _reads_lock:
        p = subprocess.Popen(_background(argv), cwd=cwd, env=env, text=text, start_new_session=True,
                             stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _reads.add(p)
    try:
        return _wait_read(p, argv, input, timeout, dl, end, own_end, reap)
    finally:
        with _reads_lock:
            _reads.discard(p)


def _wait_read(p: subprocess.Popen, argv: list[str], input: str | bytes | None,
               timeout: float | None, dl: Deadline | None, end: float, own_end: float,
               reap: bool) -> subprocess.CompletedProcess:
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


# The reads running now. Each runs in its own session, beyond the reach of the
# hangup that ends this process (the sidebar's, when tmux closes its pane), so
# this process stops them as it exits, or nothing would.
_reads: set[subprocess.Popen] = set()
_reads_lock = threading.Lock()


@atexit.register
def _stop_reads_at_exit() -> None:
    with _reads_lock:
        left = list(_reads)
    for p in left:
        _signal_group(p.pid, signal.SIGKILL)
    for p in left:
        try:
            p.wait(timeout=GRACE)
        except subprocess.TimeoutExpired:
            pass


def exit_on_hangup() -> None:
    """Exit normally on SIGHUP and SIGTERM, so a long-lived process (the
    sidebar) stops its running reads on the way out instead of dying with
    them still running. Call it from the main thread."""
    def _exit(signum: int, frame: Any) -> None:
        raise SystemExit(128 + signum)

    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, _exit)


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
           wait: float | None = None, no_terminal: bool = False) -> subprocess.CompletedProcess:
    """Run a command that changes something, like
    ``subprocess.run(capture_output=True)``. ``no_terminal``: in a session of
    its own, so nothing it starts can ask at the terminal. It may wait for room before it
    starts: until the request's deadline (then Busy, and nothing was done),
    or with no deadline up to ``wait`` seconds (ACTION_WAIT), after which it
    starts regardless. Once started, no deadline stops it, nor anything else
    the request starts after it (Deadline.committed); ``timeout`` is the
    caller's own hard limit, as with ``subprocess.run``. It runs at normal
    priority: someone waits on it (a setup command, a merge and its hooks)."""
    _guard(argv)
    dl = _live()
    if dl is not None:
        _admit(dl.at, dl, False, _name(argv))
        dl.committed = True      # admitted: the rest of the request is its transaction
    else:
        _admit(time.monotonic() + (ACTION_WAIT if wait is None else wait), None, False,
               _name(argv), start_anyway=True)
    try:
        return subprocess.run(argv, cwd=cwd, env=env, text=text, input=input,
                              capture_output=True, timeout=timeout, start_new_session=no_terminal)
    finally:
        _release()
