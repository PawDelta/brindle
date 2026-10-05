brindle 0.14.4 makes brindle quicker to install and to try for the first time.

## New
- **One-line install.** `curl -fsSL pawdelta.com/brindle/install | sh` installs uv and tmux if they're missing, then brindle, then runs `brindle doctor`. Run it again to upgrade.
- **`brindle init` detects your stack.** It reads your lockfiles and manifests (uv, Poetry, npm/pnpm/yarn/bun, Cargo, Go, Bundler, Make) and writes `.brindle/config.json` with the setup a new worktree needs, the checks that must pass before a branch merges, and the git-ignored env files to copy into each worktree. It never overwrites an existing config.
- **`brindle demo`.** Watch brindle finish a tiny practice repo in a few minutes: two workers in parallel, a review of each branch, gated merges, and milestones that turn green only once their tests pass. `brindle demo --local` runs it on Ollama.
- **`brindle history --share`.** Sums up a session in a few lines to paste into Slack or a post: goal, milestones verified, workers, merges, reviews, parallel speedup and tokens.

## Changed
- **Bare `brindle` checks before it launches.** If tmux or the chat's CLI is missing, it says how to install it instead of opening an empty window.
- **Quieter `brindle doctor`.** Optional tools (Codex, local models, gh, ...) are listed separately and no longer count as warnings.
- **The supervisor asks for checks.** In a repo with no `checks`, it proposes one (the test command brindle detected) before assigning work.
- **PR footer.** `brindle pr` and `brindle ci` end the PR description with one "built with brindle" line. Set `"pr_footer": false` in `.brindle/config.json` to leave it out.
- Workers no longer stop for approval on `python3 -m pytest` or `python -m unittest`.

## Upgrading
`uv tool upgrade brindle-agents`, or run the install line again. If you keep your own profiles in `~/.brindle/agents/`, add `Bash(python3 -m pytest:*)` and `Bash(python3 -m unittest:*)` to their `allowed_tools` to get the new approvals.
