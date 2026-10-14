"""Long-lived processes that brindle can be upgraded under.

`uv tool install --reinstall` or a PyPI upgrade replaces brindle's files under
processes that keep running: each session's `brindle mcp` server and its
`brindle watch --sidebar`. Their lazy imports and directory reads (the built-in
profiles, say) would then see a half-replaced tree, or a newer one than the
code they already loaded.

The MCP server never restarts for that: its client doesn't reconnect a stdio
server that exits, so every agent would lose brindle's tools. Instead it loads
everything it will ever read from the install when it starts (`preload`), and
keeps serving that version consistently; a server started after the upgrade
gets the new one. The sidebar checks between refreshes whether the install
changed and, if it did, execs itself again in its pane."""

from __future__ import annotations

import importlib
import os
import pkgutil
import sys
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


def _stamp() -> tuple[int, int, int] | None:
    """The installed ``brindle/__init__.py``'s identity: an upgrade writes a
    new file (a new inode, mtime or size). None while it's missing mid-upgrade."""
    try:
        st = os.stat(brindle.__file__)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


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


def reexec() -> None:
    """Replace this process with a fresh run of the same brindle command, from
    the install as it is now. Never returns."""
    from brindle.providers import brindle_invocation

    argv = [*brindle_invocation(), *sys.argv[1:]]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(argv[0], argv)
