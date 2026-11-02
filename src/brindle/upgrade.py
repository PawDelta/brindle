"""Long-lived processes that brindle can be upgraded under.

`uv tool install --reinstall` or a PyPI upgrade replaces brindle's files under
processes that keep running: each session's `brindle mcp` server and its
`brindle watch --sidebar`. Their lazy imports and directory reads (the built-in
profiles, say) would then see a half-replaced tree, or a newer one than the
code they already loaded.

The MCP server never exits for that: its client doesn't reconnect a stdio
server that exits, so every agent would lose brindle's tools. It loads
everything it will ever read from the install when it starts (`preload`), so
it serves one version consistently, and once the install changed or the
database moved past it, it execs the new install in place on the same stdio,
between requests (mcp_stdio.py). A newly started daemon wakes every live
server (`notify_mcp_servers`) so an idle one reloads too. Only when that exec
fails does the server refuse every tool once the new version has migrated
the database, and tell its client to reconnect. The sidebar checks between
refreshes whether the install changed and, if it did, execs itself again in
its pane."""

from __future__ import annotations

import fcntl
import importlib
import os
import pkgutil
import shutil
import signal
import sys
import time
from contextlib import asynccontextmanager

import brindle


def preload() -> None:
    """Import every brindle module and read the built-in profiles and rule
    packs into memory now, so nothing this process does later reads brindle's
    files from an install that may since have been replaced."""
    from brindle import profiles

    for mod in pkgutil.walk_packages(brindle.__path__, "brindle."):
        if mod.name.rsplit(".", 1)[-1] == "__main__":
            continue    # runs the CLI when imported
        try:
            importlib.import_module(mod.name)
        except Exception:   # an optional dependency or platform module this process never uses
            pass
    profiles.freeze_builtins()


@asynccontextmanager
async def preloaded(_server):
    """MCP server lifespan: `preload` before the first request is served."""
    preload()
    yield {}


def _stamp() -> tuple[int, ...] | None:
    """The installed ``brindle/__init__.py``'s identity: an upgrade writes a
    new file (a new inode, mtime or size). A reinstall of the same build links
    the very same cached file, so the package directory's identity counts too:
    it is deleted and made again. None while it's missing mid-upgrade."""
    try:
        st = os.stat(brindle.__file__)
        parent = os.stat(os.path.dirname(brindle.__file__))
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size, parent.st_ino, parent.st_ctime_ns)


def stamp() -> tuple[int, ...] | None:
    """The install's identity now (see `_stamp`)."""
    return _stamp()


class InstallWatch:
    """Whether brindle's installed files changed since this was made (at the
    process's start). Once changed it stays changed: a tree that was replaced
    once is never the one this process loaded, whatever it looks like later."""

    def __init__(self) -> None:
        self._start = _stamp()
        self._changed = False

    def changed(self) -> bool:
        if not self._changed:
            self._changed = _stamp() != self._start
        return self._changed


install = InstallWatch()    # this process's code is the install as of its start
on_stale_launch = None      # the daemon sets this to start its clean shutdown


class StaleInstall(Exception):
    pass


def check_launch() -> None:
    """Called before an agent launches. A process that can restart itself (the
    daemon) starts doing so and refuses the launch; one that can't (the MCP
    server) carries on and tells its client instead."""
    if install.changed() and on_stale_launch is not None:
        on_stale_launch()
        raise StaleInstall("brindle was updated; it is restarting on the new version, try again in a moment")


# Wakes a live `brindle mcp` to check for a reload. Its default action is to
# ignore it, so a server caught between exec and installing its handler lives.
MCP_RELOAD_SIGNAL = signal.SIGURG


def _mcp_servers_dir():
    from brindle.config import brindle_home

    return brindle_home() / "mcp-servers"


def register_mcp_server() -> int | None:
    """Record this process as a live MCP server: a file named by its pid,
    locked for as long as this process (not one exec'd from it) runs. Returns
    the lock's fd, None when it couldn't be registered."""
    try:
        directory = _mcp_servers_dir()
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory / str(os.getpid()), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def notify_mcp_servers() -> int:
    """Wake every live MCP server, so each one that runs an older install
    reloads now rather than at its client's next call. A pid file nobody
    holds locked is stale (its server exited, or exec'd and hasn't locked it
    again): it's removed, and its pid, which may be anyone's now, not
    signalled. Returns how many were woken."""
    try:
        entries = list(_mcp_servers_dir().iterdir())
    except OSError:
        return 0
    woken = 0
    for path in entries:
        try:
            pid = int(path.name)
            fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        except (ValueError, OSError):
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:     # held: its server is alive
            try:
                os.kill(pid, MCP_RELOAD_SIGNAL)
                woken += 1
            except OSError:
                pass
        else:
            path.unlink(missing_ok=True)
        finally:
            os.close(fd)
    return woken


def exec_install(args: list[str]) -> bool:
    """Replace this process with ``brindle <args>`` from the install as it is
    now. False when no install could be exec'd; True only from a fake exec.

    An app replacement can remove the running engine's interpreter entirely,
    and the replacement may be reachable only through the installed CLI at a
    different path.
    """
    from brindle.providers import _in_worktree, brindle_invocation

    sys.stdout.flush()
    sys.stderr.flush()
    argv = [*brindle_invocation(), *args]
    try:
        os.execv(argv[0], argv)
        return True     # only a fake returns
    except OSError:
        # sys.executable still names the old engine after it is removed.
        # Discover the installed launcher again on each pass; never use a
        # worktree's venv, whose lifetime is shorter than this session's.
        installed = shutil.which("brindle")
        if installed and installed != argv[0] and not _in_worktree(installed):
            try:
                os.execv(installed, [installed, *args])
                return True     # only a fake returns
            except OSError:
                pass
    return False


def reexec() -> None:
    """Replace this process with a fresh run of the same brindle command, from
    the install as it is now. Never returns: it keeps this pane alive until
    an engine is available."""
    # An upgrade deletes the old environment before the new one is in place, so
    # the exec fails until the install is complete: wait for it, don't exit.
    while True:
        if exec_install(sys.argv[1:]):
            return      # only a fake returns
        else:
            time.sleep(0.5)
