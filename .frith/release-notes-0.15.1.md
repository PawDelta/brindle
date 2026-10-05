frith 0.15.1 tidies two commands.

## Fixed
- **`frith learning` no longer offers `--reset`,** which did nothing with hosted learning. It shows whether hosted learning is on for the repo, and if not, why.
- **`frith prune --help` is accurate:** a removed worktree's branch is deleted once it's fully merged; unmerged branches are kept.

## Upgrading
`uv tool upgrade frith-agents`.
