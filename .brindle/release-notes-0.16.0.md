brindle 0.16.0 checks a finished goal against what you asked for, and can answer workers' permission prompts for you within rules you set.

## New
- **Completion audit.** A passing milestone check doesn't prove the milestone is done: the test can assert too little or check the wrong thing. Now, once every milestone passes, autopilot doesn't call the goal reached yet. A read-only reviewer (from a different model when one is available) audits everything since the goal was set against the goal and each milestone, looking for what's missing, stubbed or weakly tested. Only its approval of the current commit finishes the goal. If it finds gaps, the supervisor gets them and keeps working until a fresh audit approves. On by default; `"goal_audit": false` turns it off. An approval of an older commit (the checkout moved, or had uncommitted changes, while the audit ran) doesn't finish the goal: the next `check_milestone` audits the current commit. (#51)
- **Permission policy.** With `"permission_policy": "on"`, brindle decides workers' tool-permission requests from the structured request each agent CLI hands a hook: tool name, command, file path. It never reads the screen. Deny beats allow, and anything else falls through to your normal prompt. Off by default. (#50)
  - **Starting rules:** allow reading files git tracks in the worktree or repo, your `checks` commands, and plain `git status`/`diff`/`log`/`show`; deny `git push`, force flags, and reads of `~/.ssh`, `~/.aws`, `~/.gnupg` and `.env*`. A command is only ever auto-allowed as a single simple command, with no shell metacharacters.
  - **Adjusted by you:** approving the same thing twice at a prompt makes it a suggestion, never a silent rule. `brindle permissions list | suggestions | accept | allow | deny | forget | reset` manages your rules in `~/.brindle/permissions.json`. A repo's `.brindle/permissions.json` can only add denies.
  - **Claude Code:** through its `PermissionRequest` hook.
  - **Codex:** through its `PermissionRequest` hook. Trust it once with `brindle permissions install-codex-hook --yes`; that covers every worktree.
  - **Antigravity:** its hook denies what the policy denies; because agy currently ignores a hook's allow, brindle copies the rules agy can express into `~/.gemini/antigravity-cli/settings.json`, touching only its own entries, with a one-time backup. `brindle permissions sync-agy` does it by hand.
  - **Supervisor notices:** a stuck worker's notice now names the pending request and why the policy left it to you.
- **PyPI links.** The PyPI page links the docs and the changelog.

## Upgrading
`uv tool upgrade brindle-agents`.

---

Docs and install: [pawdelta.com/brindle](https://pawdelta.com/brindle/) · Release notes: [pawdelta.com/brindle/changelog](https://pawdelta.com/brindle/changelog)
