# brindle: the full guide

Everything brindle can do, CLI and config included. New here? Start with the [README](../README.md).

A supervisor for your coding agents that won't call work done until it's proven. See the [overview](https://pawdelta.com/brindle/docs/overview/) for product details.

## Install

Moved to https://pawdelta.com/brindle/docs/overview/

## Quick start

Moved to https://pawdelta.com/brindle/docs/overview/

## Recommended use

Moved to https://pawdelta.com/brindle/docs/recommended-use/

## Driving toward a verified goal

Moved to https://pawdelta.com/brindle/docs/autopilot/

## Settings API

Moved to https://pawdelta.com/brindle/docs/config/

## Settings UI

Moved to https://pawdelta.com/brindle/docs/config/

## Commands

[Commands](https://pawdelta.com/brindle/docs/commands/)

## Connect GitHub

Moved to https://pawdelta.com/brindle/docs/github/

## Token usage and history

Moved to https://pawdelta.com/brindle/docs/history/

## Shared knowledge across sessions

Moved to https://pawdelta.com/brindle/docs/shared-knowledge/

## Provider quota

Moved to https://pawdelta.com/brindle/docs/providers/

## Repo config: `.brindle/config.json`

Moved to https://pawdelta.com/brindle/docs/config/

## brindle Pro and Team

Moved to https://pawdelta.com/brindle/docs/pro-team/

## Cleanup, merging and workspace config

Moved to https://pawdelta.com/brindle/docs/autopilot/

## Agent profiles

Moved to https://pawdelta.com/brindle/docs/profiles/

## Open-weight models

Moved to https://pawdelta.com/brindle/docs/providers/

## Google Antigravity

Moved to https://pawdelta.com/brindle/docs/providers/

## How it works

Moved to https://pawdelta.com/brindle/docs/overview/

## Development

For development, install the project dependencies and run the focused tests for your change:

```sh
uv sync
uv run pytest
```

### CI

GitHub runs tests on pull requests and pushes to `main` on Ubuntu and macOS, using Python 3.12. Changes that only touch docs run no CI jobs.

To run the same checks locally on a clean export of the commit:

```sh
scripts/ci_local.sh
scripts/ci_local.sh --full
```

The first command runs Python 3.12 here and on Linux; `--full` also checks Python 3.11 and 3.13. The pre-push hook is off by default; `git config core.hooksPath .githooks` turns it back on.

### Releasing

brindle and its desktop app share one version number because the app bundles its engine.

1. Run `scripts/bump_version.py X.Y.Z` (`--dry-run` previews changes), then commit and push. It updates the package version and app package files; CI checks that they match.
2. Write release notes, run `scripts/release_footer.py NOTES.md`, and create a GitHub release tagged `v<version>`.
3. The Publish workflow tests, builds, and uploads to PyPI via Trusted Publishing.
4. Tag the same commit `app-v<version>` for the desktop app.

## License

brindle is source-available, not open source. You may install and use it under the [brindle License 1.0](../LICENSE), subject to its terms.

This project is unrelated to the Brindle desktop app at brindle.dev.
