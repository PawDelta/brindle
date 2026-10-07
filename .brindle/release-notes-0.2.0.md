**For everyone**

- **Usage by model in the sidebar.** The sidebar's Costs section shows this month's token share by model (top three plus "other"), with your budget bar when a budget is set.
- **Free models read "Free".** Usage on a model priced at $0 (a local profile, or an all-zero `pricing` override) shows **Free** instead of `$0.00`, and its tokens still count. A mixed total reads like `$4.00 (+1.2M tokens free)`.
- **Supervisor tools.** `list_tasks(full=true)` shows each task's brief and finish line. `requeue` brings back a cancelled task. `assign` and `handoff` take `dry_run=true` to preview the profile, the matched files and any overlaps without starting anything.
- **No duplicate supervisors.** Moving a scratch session into a repo pauses the scratch chat, so the repo never has two supervisors.
- **Local models start and stop with brindle.** brindle starts the Ollama server a local profile needs, and stops it when the last session closes. An Ollama server you started yourself is never stopped.

**Team**

- **Your org's budgets, applied to you.** brindle now enforces the budget your org sets for you, whether it comes from the org-wide default, your role or you personally, including a new per-task budget. A repo can still only tighten it.
- **Org messages.** Messages from your org's admins appear in the sidebar's Messages section, in the bottom bar, and in `brindle org messages`.
- **Bottom-bar alerts.** The status bar next to the version shows when you're paused or throttled and when you have unread messages. Admins also see the org's month total at 80% and 100%.
- **Throttles and pause.** When an admin limits your models or parallel workers, or pauses you, brindle applies it within about a minute. A pause stops new workers and reviewers; running workers keep their branches.

**Enterprise**

- **Remote shutdown and throttle.** Admins can stop or throttle a member, a role or the whole org from the website. brindle stops running workers (their worktrees and branches are kept), refuses new ones, shows the reason, and confirms back so admins can see who has stopped. The org kill switch now arrives the same way, within about a minute.

**How it works**

- brindle checks in with your org about once a minute while a session is active, and every 5 minutes when idle. It never checks in air-gap mode, or with a server that doesn't support it.

**Fixes**

- Cost estimates show dollars again. An import error had made every estimate read "no price for some models".
- The brindle CI org dollar limit is now enforced even when a heartbeat fails, and across the whole run.
- `brindle ci doctor` on GitLab uses the job's project path.
- A profile header without a closing `---` is reported as a profile error instead of crashing.
- `brindle account org policy` reads `.brindle/policy.json` in air-gap mode, and `brindle audit ship` reports when it has nothing to send.
- Copying in the chat pane shows "copied to the clipboard", and Ctrl+C works again after a drag leaves the pane in copy mode.
- Finished goals are cleared, so the next session starts fresh.
- Assigning a task reuses a workspace on the same branch instead of making duplicates.

**Security improvements**

- Text your org sends (messages, pause and shutdown reasons) is stripped of control characters before it reaches your terminal, and is handed to the supervisor as quoted data, not as instructions.
