**For everyone**

- **Light tasks run on Haiku.** The light tier now starts on Claude Haiku 5.5, and the heavy tier starts on `developer` with `developer-heavy` next. Haiku 5.5 is priced in cost reports.
- **Repos with the same folder name.** Attaching two repos whose folders share a name now gives each its own workspace ids, worktree and pool folders, so they no longer collide.
- **No false "stopped without reporting".** The supervisor is no longer told a worker stopped unreported while that worker's command is still running.

**Pro**

- **Your signed-in agents reach your account.** When brindle refreshes your account entitlement, it now sends the names of the agent CLIs this machine is signed in to (for example `claude`, `codex`). Names only, never keys. Your org's admins use this to choose which agents and models the org can use, in the dashboard under Settings › Models and providers.

**Enterprise**

- **Start Enterprise yourself.** `brindle account upgrade --enterprise --seats N --org ORG` opens Enterprise checkout. Add `--trial` for a free 7-day trial of up to 10 seats, with no card to start.

**Changes to note**

- `brindle account upgrade --seats 0` is now refused, for `--team` as well.
