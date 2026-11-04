# brindle

**A team of coding agents that only calls work done once it's proven.**

Tell brindle what you want built. A supervisor agent breaks it into pieces and hands
them to several agents (Claude Code, Codex, or a local model), each working on its own
branch. Another agent reviews every branch, your tests run, and only then does it merge.
You get the result, not a pile of half-finished branches.

![The brindle app running its practice demo: a supervisor splits a goal between two workers, each branch is reviewed and merged, and both milestones turn green once their checks pass](https://raw.githubusercontent.com/PawDelta/brindle/main/assets/brindle-demo.gif)

*The demo you can run yourself from the app in a few minutes: two workers in parallel,
each branch reviewed and merged, both milestones verified by their tests.*

## Why brindle

**One agent is fast. Several are faster, if they don't trip over each other.**
Every agent gets its own copy of your repo and its own branch, so five of them can work
at once without overwriting each other's files or fighting over ports.

**"Done" means a test passed, not that a model said so.**
Each goal is split into milestones, and each milestone has a command that must succeed.
brindle runs it itself, and re-runs it after later merges to catch regressions.

**Nothing reaches your branch unreviewed.**
A reviewer approves the exact commit, then your checks run. Want a second opinion? Have
Codex review Claude's work, or the other way round.

**You can walk away.**
brindle keeps going until every milestone passes. It pauses when you hit your usage
limit and picks up after the reset. Close the app and the session resumes later,
workers included.

**It asks you only when it matters.**
Decisions, permission requests and finished work land in one inbox. You don't need to
watch every agent's terminal.

**Use the agents you already pay for.**
brindle drives the Claude Code, Codex and Google Antigravity CLIs you're already signed
in to, or open-weight models through Ollama or any OpenAI- or Anthropic-style endpoint.

## How it feels

1. **Open a project.** Pick any folder with a git repo in it.
2. **Say what you want.** "Add CSV export to the reports page, with tests." The
   supervisor writes the milestones and the checks that prove each one.
3. **Watch it, or don't.** See every agent's progress, read the diff of any branch, and
   step in from the inbox when something needs you.
4. **Get a reviewed, tested result** merged into your session's branch, ready for a PR.

For a one-line fix, plain `claude` is quicker. brindle pays off when work splits into
parallel pieces, or runs long enough that you'd rather not babysit it.

## Get started

**The app** (macOS and Linux): download it from
[pawdelta.com/brindle](https://pawdelta.com/brindle/getting-started/), open it, and
under **New session** choose **Watch brindle work on a practice repo** to see a full run on a practice repo before pointing it at
your own code.

**The terminal**, if you'd rather stay there (macOS, Linux, or Windows through WSL2):

```sh
curl -fsSL pawdelta.com/brindle/install | sh
brindle demo      # the same practice run, in tmux
cd ~/code/myapp && brindle
```

Either way you need at least one agent CLI installed and signed in, such as Claude Code
(`npm install -g @anthropic-ai/claude-code`, then `claude auth login`). The app and the
CLI run on the same engine, so you can use both.

## Pricing

The core workflow is free, with no limit on agents, workers or sessions, and no account
needed. brindle Pro ($20/month) and Team ($40/seat/month) add things like budgets,
several repos in one session and shared rules
([compare plans](https://pawdelta.com/brindle/pricing/)).

## Learn more

- [The full guide](https://github.com/PawDelta/brindle/blob/main/docs/guide.md): every command, config option, provider and plan
  feature.
- [Website and docs](https://pawdelta.com/brindle/)

## License

Source-available, not open source. brindle is free to use, including at work, under the
[brindle License 1.0](https://github.com/PawDelta/brindle/blob/main/LICENSE): you may install and run it, but not modify it (beyond
the changes the license permits), redistribute it, or use it to build a competing
service. Details in the [guide](https://github.com/PawDelta/brindle/blob/main/docs/guide.md#license).

This project is unrelated to the Brindle desktop app at brindle.dev.
