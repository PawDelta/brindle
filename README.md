<p align="center">
  <img src="https://raw.githubusercontent.com/PawDelta/brindle/main/assets/brindle-mark.svg" width="120" alt="brindle logo">
</p>

<h1 align="center">brindle</h1>

<p align="center"><strong>A supervisor for your coding agents that won't call work done until it's proven.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/brindle/"><img alt="PyPI" src="https://img.shields.io/pypi/v/brindle?style=flat-square"></a>
  <a href="https://github.com/PawDelta/brindle/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/PawDelta/brindle/ci.yml?branch=main&style=flat-square&label=CI"></a>
  <a href="https://pypi.org/project/brindle/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/brindle?style=flat-square"></a>
  <img alt="Platforms: macOS, Linux, WSL2" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20WSL2-555?style=flat-square">
  <a href="https://github.com/PawDelta/brindle/blob/main/LICENSE"><img alt="License: source-available" src="https://img.shields.io/badge/license-source--available-7c6bd6?style=flat-square"></a>
</p>

<p align="center">
  <a href="https://pawdelta.com/brindle/">Website</a> ·
  <a href="https://pawdelta.com/brindle/docs">Docs</a> ·
  <a href="https://pawdelta.com/brindle/getting-started/">Download the app</a> ·
  <a href="https://pawdelta.com/brindle/changelog/">Changelog</a>
</p>

brindle runs Claude Code, Codex, Google Antigravity, OpenCode and open-weight models side by
side, each in its own git worktree. A supervisor agent splits the goal into milestones and
hands the work out. Every branch merges only after a reviewer approves that exact commit and
your checks pass. A milestone counts as done only when its check command exits 0.

For a one-line fix, plain `claude` is quicker. brindle pays off when work splits into parallel
pieces, or runs long enough that you'd rather not babysit it.

## How a goal gets done

You give the supervisor a goal and define what done means with checks. It assigns non-overlapping
tasks to workers, each in its own worktree and branch. A reviewer approves each exact commit;
pre-commit hooks and your checks must pass before it merges. The milestone is verified when its
check command exits 0. See the [full docs](https://pawdelta.com/brindle/docs) for planning,
review and recovery details.

## Install

You need at least one agent CLI installed and signed in. Claude Code is the default for the
supervisor, workers and reviewer (`npm install -g @anthropic-ai/claude-code`, then
`claude auth login`). The core workflow is free. The desktop app needs a free brindle account,
with no payment details; the CLI needs no account.

**Desktop app** (macOS and Linux): download from
[pawdelta.com/brindle/getting-started](https://pawdelta.com/brindle/getting-started/). The app
and CLI run on the same engine, so you can use both on the same sessions.

**Terminal** (macOS, Linux, or Windows through WSL2): the installer installs uv if needed,
then brindle, then runs `brindle doctor`. brindle needs tmux; on macOS the installer adds it
with Homebrew. On Linux and WSL2, install it first (`sudo apt install tmux` on Debian/Ubuntu).

```sh
curl -fsSL pawdelta.com/brindle/install | sh
```

## Your first five minutes

Start with the guided practice run, then try brindle on a repository:

```sh
brindle demo
cd ~/code/myapp
brindle init
brindle
```

`brindle init` detects setup and test commands. In the supervisor chat, describe a goal and
how to verify it. For example: “Add a settings API and UI; run the API tests and settings UI
tests before calling each milestone done.” The sidebar tracks workers, review, checks and merges.
Use `brindle continue` later to resume a paused session. More in the
[getting started guide](https://pawdelta.com/brindle/docs).

## Built with brindle

brindle is built with brindle. Historical snapshot from `brindle history`, 2026-10-05 to
2026-11-04 (the installed command could not open its read-only database to refresh these figures):

| | |
|---|---|
| Worker tasks completed | 1,609 across 24 repos |
| Reviews | 1,567, of which 729 (47%) sent the branch back with changes requested before it could merge |
| Branches merged | 529, each only after approval and passing checks |
| Milestone checks passed | 642 |
| Pull requests merged into brindle itself | 252 |

Nearly half of all reviews requested changes. That's the review gate doing its job, and it's
the number to look at when deciding whether unreviewed agent output belongs on your main branch.

## More

See [brindle docs](https://pawdelta.com/brindle/docs) for the FAQ, commands, agent and model
support, configuration, provider setup and troubleshooting.

## Contributing

Bug reports and pull requests are welcome. Contributions are covered by the
[Contributor License Agreement](https://github.com/PawDelta/brindle/blob/main/CONTRIBUTING.md);
the CLA bot asks you to sign it on your first pull request.

## License

Source-available, not open source. brindle is free to use, including at work, under the
[brindle License 1.0](https://github.com/PawDelta/brindle/blob/main/LICENSE): you may install
and run it, but not modify it (beyond the changes the license permits), redistribute it, or
use it to build a competing service. Details in the
[docs](https://pawdelta.com/brindle/docs).

This project is unrelated to the Brindle desktop app at brindle.dev.
