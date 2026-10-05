import sys

# Fast path for the sidebar-follow hook: it runs on every window/session
# switch in every brindle session, so it skips importing typer and the rest of
# cli.py's module-level imports. Never raises:
# this runs from a tmux hook, where an uncaught error would show as a message
# popup or a nonzero exit tmux might complain about.
if len(sys.argv) >= 3 and sys.argv[1] == "_sidebar-follow":
    try:
        from brindle.db import DB
        from brindle.sidebar_follow import sidebar_follow

        sidebar_follow(DB(), sys.argv[2])
    except Exception:
        pass
    raise SystemExit(0)

from brindle.cli import app

app()
