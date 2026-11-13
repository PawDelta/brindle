"""The ``brindle`` entry point, shared by the console script and ``python -m
brindle``. Claude Code's PreToolUse hook runs before every tool call it
matches, so ``_hook pre-tool --agent <id>`` is handled here, the same work as
cli.hook without typer building every command first. Every rule source is
still read on each call (agents.pre_tool_decision). So is ``_launch-env <agent>
-- <command>``, the shim an agent's pane starts its command through
(brindle.login_shell_env): it becomes the command, so it loads as little as
it can first. Anything else is cli:app."""
import sys


def main() -> None:
    if len(sys.argv) > 4 and sys.argv[1] == "_launch-env" and sys.argv[3] == "--":
        from brindle import login_shell_env

        login_shell_env.start_pane(sys.argv[2], sys.argv[4:])   # becomes the command: never returns
        raise SystemExit(1)

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
