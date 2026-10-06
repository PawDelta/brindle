brindle 0.0.3 adds multi-repo sessions, turns `brindle ci` into the client for the hosted brindle CI service (which adds fixing broken builds, repo knowledge and a Jira trigger), and makes brindle hold up when tmux stops answering.

- **Multi-repo sessions (brindle Pro).** One session can work across several repos. Attach them with `brindle repo add <path> [--name alias]`, and list or remove them with `brindle repo ls` and `brindle repo rm`. The supervisor hands work to workers in any attached repo. Each repo keeps its own checks, reviews and merges. Task dependencies work across repos, and a milestone can run its check in another repo (`check@alias: …`). `brindle ls` and the sidebar group work by repo. Without attached repos, nothing changes. Without Pro, `repo add` and cross-repo tasks say so.
- **`brindle ci` is now the client for brindle CI (brindle Pro).** The work runs in your own GitHub Actions with your own model keys: Claude Code, Codex and open-weight models. `brindle ci init` sets up a repo in one command; model keys go into GitHub's own secret prompt, and brindle never sees them. `brindle ci start`, `run` and `report` are what the generated workflow runs, and `brindle ci doctor` shows which agent CLIs and keys a CI job has. A run triggered from a Jira ticket whose text has expired ends cleanly with a message instead of failing the workflow.
  - **Breaking:** 0.0.2's local CI commands (`brindle ci entitle` and `brindle ci publish`, and the old `brindle ci run`) are gone. A workflow made by 0.0.2's `brindle ci init` stops working once it installs 0.0.3: run `brindle ci init` again to replace it.
- **New in the brindle CI service** (hosted by PawDelta; works with the 0.0.3 client):
  - **Issue to pull request (Team).** Label an issue, and brindle runs the work in your GitHub Actions and opens a PR. brindle never merges.
  - **Pull request checks (Team).** brindle checks pull requests and reports the results.
  - **Fixes broken builds (Pro).** When CI fails, brindle opens a fix PR, proven by your own CI. On Pro: 1 repo, with a monthly quota.
  - **Repo knowledge (Pro).** brindle learns what works in each repo, uses it in later runs, and can propose updates to your agent instructions file.
  - **Jira trigger (Team).** Start brindle runs from Jira tickets and get status back on the ticket. No Atlassian app needed.
- **Sign-in checks in `brindle doctor`.** For each installed agent CLI (Claude Code, Codex, Antigravity), `brindle doctor` shows how it's signed in and whether a quota limit is in effect. It names a key's environment variable, never its value.
- **Antigravity sign-in.** Antigravity works with `GEMINI_API_KEY`, including a key kept only in a profile's `env` lines. A worker whose Antigravity is signed out is refused with a clear message instead of sitting on its login screen.
- **tmux problems no longer take agents down.**
  - Every tmux call now times out (30s, or `BRINDLE_TMUX_TIMEOUT`) with a one-line explanation, instead of hanging brindle.
  - Cleanup never decides agents have died from a hung tmux server, or from another tmux server's pane list (for example `brindle demo`'s private server).
  - brindle never kills your default tmux server.
  - Starting brindle inside a tmux pane switches to the session instead of nesting a client, even when `TMUX` is unset.

Upgrade with `uv tool upgrade brindle`, then restart running sessions to pick up the new code.
