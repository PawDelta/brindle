"""Long-lived processes that brindle can be upgraded under.

`uv tool install --reinstall` or a PyPI upgrade replaces brindle's files under
processes that keep running: each session's `brindle mcp` server and its
`brindle watch --sidebar`. Their lazy imports and directory reads (the built-in
profiles, say) would then see a half-replaced tree, or a newer one than the
code they already loaded.

The MCP server doesn't exit for that: Claude Code (2.1.318) starts an exited
stdio server again, but loses the calls it was serving, and other clients may
not start it again at all. It loads
everything it will ever read from the install when it starts (`preload`), so
it serves one version consistently, and once the install changed or the
database moved past it, it execs the new install in place on the same stdio,
between requests (mcp_stdio.py), after `brindle mcp --check` showed that
install works with the database. It looks at its install while idle too. When
no reload works, the server refuses every tool once a newer version has
migrated the database, and tells its client to reconnect. The sidebar checks between
refreshes whether the install changed and, if it did, execs itself again in
its pane."""

from __future__ import annotations

import importlib
import os
import pkgutil
import re
import shutil
import sys
import time
from collections.abc import Callable
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


def exec_install(args: list[str], ready: Callable[[list[str]], bool] | None = None) -> bool:
    """Replace this process with ``brindle <args>`` from the install as it is
    now. False when no install could be exec'd; True only from a fake exec.
    ``ready(argv)``: exec ``argv`` only if it says the install there works
    (an exec can't be taken back, so a half-written install would kill this
    process instead of leaving it serving).

    An app replacement can remove the running engine's interpreter entirely,
    and the replacement may be reachable only through the installed CLI at a
    different path.
    """
    from brindle.providers import _in_worktree, brindle_invocation

    sys.stdout.flush()
    sys.stderr.flush()
    argv = [*brindle_invocation(), *args]
    if _exec(argv, ready):
        return True     # only a fake returns
    # sys.executable still names the old engine after it is removed.
    # Discover the installed launcher again on each pass; never use a
    # worktree's venv, whose lifetime is shorter than this session's.
    installed = shutil.which("brindle")
    if installed and installed != argv[0] and not _in_worktree(installed):
        return _exec([installed, *args], ready)
    return False


def _exec(argv: list[str], ready: Callable[[list[str]], bool] | None) -> bool:
    if ready is not None and not ready(argv):
        return False
    try:
        os.execv(argv[0], argv)
        return True     # only a fake returns
    except OSError:
        return False


# -- MCP servers that can't reload themselves -------------------------------
#
# Servers from before this one (0.5.x, and 0.6.0, whose reload waited forever
# on Claude Code's open subscriptions/listen) never leave the code they
# started with, so the daemon of a newer install stops them. Claude Code
# (2.1.318) does NOT start a stopped server again: its session needs a
# `/mcp` reconnect (2026-11-04). So a server that can reload itself says so
# (`register_reloading`) and is never stopped: it finishes its requests and
# reloads; its mark is dropped only once the process is gone.

_MCP_COMMAND = re.compile(r"(?:-m brindle|/brindle) mcp(?:\s|$)")


def _reloading_dir():
    from brindle.config import brindle_home

    return brindle_home() / "mcp-reloads"


def register_reloading() -> Callable[[], None]:
    """Mark this process as a ``brindle mcp`` that reloads itself after an
    update. Returns what unmarks it, for its exit (an exec keeps the mark)."""
    mark = _reloading_dir() / str(os.getpid())
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.touch()
    except OSError:
        pass
    return lambda: mark.unlink(missing_ok=True)


def stop_stale_mcp_servers() -> bool:
    """Stop this brindle's ``brindle mcp`` servers (this BRINDLE_HOME, this
    install's interpreter) that run a replaced executable and can't reload
    themselves. One with processes of its own (a merge's git, a check) is
    busy and left for now. Returns whether any was left, to look again."""
    from brindle import procs

    table = procs.table()
    parents = {p.ppid for p in table.values()}
    marks = _reloading_dir()
    stale, left = set(), False
    for p in table.values():
        command = procs.command_line(p)
        if not (_MCP_COMMAND.search(command) and procs._home_matches(p) and _same_interpreter(command)):
            continue
        if (marks / str(p.pid)).exists() or not procs.runs_replaced_executable(p):
            continue
        if p.pid in parents:
            left = True
        else:
            stale.add(p.pid)
    if stale:
        import logging

        logging.getLogger(__name__).warning(
            "brindle: stopping %d MCP server(s) left on a replaced install that can't reload themselves",
            len(stale))
        procs.terminate(stale)
    # Only a server that is gone loses its mark: the table is a snapshot, and
    # a server that started after it (the sessions an app restart resumes,
    # while `ps` crawls under load) has marked itself but isn't in it. Its
    # mark dropped, the next update would stop it instead of letting it
    # reload (2026-11-04: a daemon started at 02:27:5x, the resumed sessions'
    # servers at 02:28:04; at 03:46 the 64->68 update stopped two of them).
    for mark in marks.glob("*") if marks.is_dir() else ():
        if mark.name.isdigit() and not procs.alive(int(mark.name)):
            mark.unlink(missing_ok=True)     # a server that died without unmarking itself
    return left


def _same_interpreter(command: str) -> bool:
    try:
        return os.path.samefile(command.split(" ", 1)[0], sys.executable)
    except OSError:
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
