frith 0.14.3 gives the frith window the same look as pawdelta.com/frith.

## Changed
- **Canopy colors in tmux and the sidebar.** The old indigo theme is gone. A frith session now has a dark forest-green background, a green status bar badge and active pane border, and grey-green secondary text. The sidebar draws its logo, selection bar and idle agents in canopy green, working agents in lavender and "needs you" in amber. Terminals with only 8 colors and `frith watch --once` follow the same scheme.

## Upgrading
`uv tool upgrade frith-agents`. A frith session that is already running keeps its old colors until you restart it.
