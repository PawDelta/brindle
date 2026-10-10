**For everyone**

- **Profile effort reaches Codex and Antigravity workers.** A profile's `effort` now goes to Codex (`-c model_reasoning_effort=…`) and to agy (`--effort`). Before this, only Claude Code workers honored it.
- **Antigravity workers without approval prompts: `"agy_approvals": "brindle"`.** agy ignores a hook's allow, so agy workers stopped on prompts even for commands brindle's rules allow, such as the repo's checks. With this setting (and the permission policy on), agy workers start with `--dangerously-skip-permissions`, and brindle's hook answers every call with allow or deny:
  - Allowed: what a rule allows (a repo's checks included), the shell commands in the profile's `allowed_tools`, files inside the worker's own worktree, brindle's tools, and agy tools that act only on the conversation.
  - Denied: everything else, with a reason telling the agent to ask its supervisor. You allow it with `brindle permissions allow`.
  - Writes under `.agents/`, `.git/` and `.brindle/` (the hook's config, git's hooks, the repo's checks) need a rule, at any depth and in any case.
  - Hook failures and tools brindle doesn't know are denied.
  The default stays `"prompt"`: agy asks as before.

**Changes to note**

- Under `"agy_approvals": "brindle"`, brindle denies a call rather than guessing when it can't be sure what agy will act on: a JSON-encoded value, a relative or `~` path, a call naming several paths, a path with `..`, a write through a symlink, or a command run outside the worktree. Ask for a rule, or have the worker restate the call plainly.
- Nobody can approve a prompt in an agy worker's pane in this mode; brindle's rules are the only gate.
- Running sessions keep their old code until restarted. Reconnect brindle (`/mcp` in Claude Code) or start a new session before agy workers pick up the setting.
