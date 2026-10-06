brindle 0.0.1 is copse, renamed. Same tool, new name: **Brindle by PawDelta**. This first brindle release also lets learning choose models, adds a savings report and company-wide learning for paid plans.

## Moving from copse
- **Install `brindle`**: `uv tool install brindle` (or `pipx install brindle`). The command is `brindle`.
- **Move your settings**: brindle doesn't read anything copse saved. Move `~/.copse` to `~/.brindle` and each repo's `.copse/` to `.brindle/`. Until you do, saved permission rules, policy and config there don't apply. `brindle doctor` lists every leftover it finds, including old `copse` entries in `.agents/hooks.json` and `mcp_config.json` to remove.
- **Paid plans**: sign in again with `brindle account login`. Hosted learning starts fresh.
- **Docs** are at [pawdelta.com/brindle](https://pawdelta.com/brindle/); old `/copse` links redirect.

## New
- **Learning picks models.** brindle no longer forces a Codex, local or second-model choice on its own. Each routing tier lists a baseline profile first and the candidates hosted learning may pick instead; set `reviewer` or `review_profile` (for example `reviewer-codex`) when you want a second model's review.
- **Local models stay off until you ask.** `local_models` now defaults to `false`: brindle uses an Ollama server that's already running and never starts one or preloads a model. `"local_models": true` brings back the old behaviour.
- **Savings report** (Pro): `brindle account savings` shows what learning's picks did in this repo, this month and last, against the baseline. Org admins and owners see monthly totals for the whole org on the account page.
- **Learning across your company** (Team): link several orgs into one company with `brindle account org company link`, and each org can opt in to pooling its learning with `brindle account org learning-share on`. Off by default; never shared with another company.
- **Policy per role** (Team): owners, admins and members can each get their own overrides, policies can limit agent profiles (`allowed_profiles`), and Enterprise can define custom roles (`brindle account org member policy-role`).
- **brindle CI** (Team): issues into verified pull requests on your own CI with your own model keys. Moving to a hosted control plane; back in a later release.
- **`brindle sidebar`** brings a session's sidebar into the tmux session you're in, or restarts it (`Ctrl-b S` does the same). The sidebar follows you between brindle windows, including on tmux 3.7.
- **`brindle permissions check`** shows the effective permission rules per provider without changing anything, and profiles can add their own `permission_denies`. brindle trusts its Codex permission hook itself the first time a Codex worker needs it.
- **A new look.** The dashboard's pine is now a brindle paw, and brindle's tmux theme and colours match pawdelta.com/brindle.

## Fixes
- **Check runs queue** instead of piling up: at most `check_concurrency` (default 2) run at once across branches, and a late check summary is never dropped.
- **Merges are serialized per checkout**, retry a transient git index lock, give up on a hung lock after 5 minutes, and a workspace merges only once.
- **One reviewer per workspace and commit**, with no repeated or stale notices.
- **Codex approval prompts** now mark the worker as needing you.
- **Antigravity messages** reach the agy pane, not the launcher's Claude inbox, and agents launch without the launcher's Claude Code inbox.
- **`brindle prune`** never closes an idle agent that's still running, and cleans up stale workspace leftovers.
- **Pinned keys only.** brindle trusts only its pinned signing keys for paid features, even in development mode.

## License
brindle is free to use and source-available under the **Brindle License 1.0** from PawDelta LLC. It is not open source: see `LICENSE` and `SCHEDULE-A` in the package. Contributions need a signed CLA. copse releases already published keep the license they were released under.

---

Docs and install: [pawdelta.com/brindle](https://pawdelta.com/brindle/) · Release notes: [pawdelta.com/brindle/changelog](https://pawdelta.com/brindle/changelog)
