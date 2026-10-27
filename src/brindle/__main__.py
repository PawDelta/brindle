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

# Fast path for Claude Code's PreToolUse hook, which runs before every tool
# call it matches: the same work as `brindle _hook pre-tool --agent <id>`
# (cli.hook), without typer building every command first. Every rule source
# is still read on each call (agents.pre_tool_decision).
if len(sys.argv) == 5 and sys.argv[1:3] == ["_hook", "pre-tool"] and sys.argv[3] == "--agent":
    from brindle import agents, tmux
    from brindle.db import DB

    try:
        out = agents.hook_main(DB(), sys.argv[4], "pre-tool", sys.stdin.read())
    except tmux.TmuxTimeout as e:  # as cli._App reports it
        print(e, file=sys.stderr)
        raise SystemExit(1)
    if out:
        print(out)
    raise SystemExit(0)

from brindle.cli import app

app()
