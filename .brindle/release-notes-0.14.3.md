brindle 0.14.3 gives the brindle window the same look as pawdelta.com/brindle.

## Changed
- **Canopy colors in tmux and the sidebar.** The old indigo theme is gone. A brindle session now has a dark forest-green background, a green status bar badge and active pane border, and grey-green secondary text. The sidebar draws its logo, selection bar and idle agents in canopy green, working agents in lavender and "needs you" in amber. Terminals with only 8 colors and `brindle watch --once` follow the same scheme.

## Upgrading
`uv tool upgrade brindle-agents`. A brindle session that is already running keeps its old colors until you restart it.
