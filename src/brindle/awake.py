"""Keep the Mac awake while brindle-launched agents run. One shared
``caffeinate -i -t <lease>`` assertion for the whole machine, whichever brindle
process notices the agents (the cull sweep of a dashboard, a one-shot CLI
cull, the desktop daemon's watch loop). It is detached, so it outlives the
process that started it, and it is a lease: whoever sweeps while agents are
active renews it before it runs out, and one that nobody renews ends by
itself. The sweep that finds no active agent releases it. State lives in
``awake.json`` under a file lock, so processes and threads share one
assertion. macOS only; a no-op elsewhere."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager

LEASE_SECONDS = 1200   # a lease nobody renews ends after this
RENEW_BELOW = 600      # renewed once less than this is left


def _dir():
    from brindle.config import brindle_home

    d = brindle_home() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def _locked():
    with open(_dir() / "awake.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _read() -> dict:
    try:
        data = json.loads((_dir() / "awake.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _stop(state: dict, now: float) -> None:
    """End the recorded assertion, unless its lease is long over (its pid may be another process's)."""
    pid, until = state.get("pid"), state.get("until") or 0
    if isinstance(pid, int) and now < until + 60 and _alive(pid):
        _terminate(pid)


def sync(active: bool) -> None:
    """Hold the shared assertion while ``active``, release it otherwise.
    Never raises."""
    if sys.platform != "darwin":
        return
    try:
        with _locked():
            now = time.time()
            state = _read()
            held = isinstance(state.get("pid"), int) and (state.get("until") or 0) > now and _alive(state["pid"])
            path = _dir() / "awake.json"
            if not active:
                _stop(state, now)
                path.unlink(missing_ok=True)
            elif not held or state["until"] - now < RENEW_BELOW:
                proc = subprocess.Popen(
                    ["caffeinate", "-i", "-t", str(LEASE_SECONDS)], start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                path.write_text(json.dumps({"pid": proc.pid, "until": now + LEASE_SECONDS}))
                if held:
                    _stop(state, now)
    except OSError:
        pass
