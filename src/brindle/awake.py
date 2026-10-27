"""Keep the Mac awake while brindle-launched agents run: one
``caffeinate -i -w <pid>`` assertion per owning process, started when the
first agent becomes active and released when none are. macOS only; a no-op
elsewhere. The owners are the processes that already watch the agents: the
cull sweep (the dashboard and the CLI) and the desktop daemon's watch loop,
both through :func:`sync`. ``-w`` ties the assertion to the owner's pid, so
it ends with it even if the owner dies without releasing."""

from __future__ import annotations

import os
import subprocess
import sys


class Awake:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def held(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def update(self, active: bool) -> None:
        """Hold the assertion while ``active``, release it otherwise. Never raises."""
        if sys.platform != "darwin":
            return
        if not active:
            self.release()
        elif not self.held():
            try:
                self._proc = subprocess.Popen(
                    ["caffeinate", "-i", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self._proc = None

    def release(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait()


_process = Awake()   # this process's one assertion, however many callers sync it


def sync(active: bool) -> None:
    _process.update(active)


def release() -> None:
    _process.release()
