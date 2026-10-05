frith 0.14.6 lets you run two frith sessions in the same repo at once.

## New
- **A second `frith` in a busy folder asks first.** If a session is already running where you start `frith`, you choose: open that session, start the new one in its own worktree, or pause the old one and start fresh. A new session gets its own branch, `frith/session-N`, cut from what you have checked out, so both sessions run at the same time without sharing files. Before, the running session was always paused.

## Changed
- Without a terminal to ask in (scripts, `--no-attach`), frith still pauses the running session and starts fresh, as before.

## Upgrading
`uv tool upgrade frith-agents`, or `curl -fsSL pawdelta.com/frith/install | sh`.
