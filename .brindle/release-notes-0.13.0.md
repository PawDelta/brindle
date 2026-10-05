brindle 0.13.0 adds plugin extension points and the client for brindle Pro and brindle Team, the optional paid plans (https://pawdelta.com/brindle#pricing). Everything brindle does on your machine stays free and open source.

## New
- **`brindle account`**: `login` (device sign-in, approved in your browser), `status`, `upgrade`, `portal`, `org list|use|policy`, `logout`. Credentials are kept in your OS keychain, or in a 0600 file when there's no keychain.
- **Hosted learning (Pro).** With the new default `"learning": "auto"`, brindle uses hosted learning when your plan includes it, and otherwise nothing changes. It sends only a task's coarse kind and size, its weight, profile names and counters, keyed by HMACs of the repo's root commit. It never sends code, prompts, file paths or branch names. Offline, it falls back to local routing.
- **Team plugins.** Org policy (allowed providers and models, required human review, a worker cap) and audit events. Both are inert without a Team license.
- **Plugin extension points**: `brindle.events`, `brindle.policy` and `brindle.account` join `brindle.learning`, and the new `plugins` config key picks one per group. Policy plugins fail closed: an error, a missing answer, or a configured plugin that won't load refuses the delegation or merge.

## Fixes
- A reviewer's `submit_review` no longer fails with "no agent 'rev0'" when its approval makes the pipeline merge and remove the workspace.

## Upgrading
- Run `uv tool install brindle-agents --force` (or `uv tool upgrade brindle-agents`) so the new plugin entry points are registered.
- `cryptography` is now a direct dependency.
