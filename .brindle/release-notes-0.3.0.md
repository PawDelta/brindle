**For everyone**

- **API keys on every tier.** `brindle keys set|list|unset NAME` stores a model API key once (keychain, Secret Service or a private file), and each agent gets only its own provider's keys. A key in your environment still wins.
- **Subscription or key, per profile.** A profile can say `auth: subscription`, `auth: api_key` or `auth: auto` (the default), so a stored key never silently replaces your login.
- **Claude Code starts on your key.** `brindle keys set ANTHROPIC_API_KEY` offers to approve the key so Claude Code doesn't stop on its "use this API key?" screen.
- **Doctor names the credential.** `brindle doctor` says which credential each agent CLI will use and warns when it isn't the one you expect:
  - Claude: a key that would take over a subscription login.
  - Codex: while signed in with ChatGPT, interactive Codex ignores API keys in the environment; use `codex login --with-api-key` to put agents on a key.
  - Antigravity: `GEMINI_API_KEY` is ignored unless agy's settings say `"modelProvider": "gemini"`; `brindle keys set` and `brindle doctor --fix` offer to set it.
- **Your org's Claude Code settings come first.** When your org's managed Claude Code settings supply the key or a gateway, brindle adds no Claude key of its own.
- **Light tasks run on Haiku.** The light tier starts on Claude Haiku 5.5, and the heavy tier starts on `developer` (Opus) with `developer-heavy` next. Haiku 5.5 is priced in cost reports.
- **Repos with the same folder name** each get their own workspace ids, worktrees and pool folders.
- **No false "stopped without reporting"** while a worker's command is still running.
- **`brindle prune`** no longer stops when a workspace's whole repo folder has been deleted.

**Pro**

- **Brindle-CI on your own cloud.** `brindle ci init --credential bedrock` runs Claude on your Amazon Bedrock account with no stored cloud secret; one-click templates in `deploy/` set up the trust. Google Vertex AI and Microsoft Foundry are available in preview with `--preview`.
- **Setup in the browser.** The dashboard's Brindle-CI setup wizard does what `brindle ci init` does.
- **Clear model-access errors.** `brindle ci doctor --models` says when a cloud account can't call a model and how to fix it; a refused model falls back to the next one in its tier.
- **Your signed-in agents reach your account.** Refreshing your entitlement sends the names of the agent CLIs this machine is signed in to (names only, never keys), so org admins can choose which agents the org uses.

**Team**

- **Change seats after purchase.** `brindle account seats N`, or Settings › Billing › Change seats in the dashboard. Stripe prorates the change.

**Enterprise**

- **Org-owned model keys.** An org policy can supply an Anthropic or OpenAI key from a variable or a command (for example a vault lookup) that reaches workers even when personal keys are denied.
- **Brindle-CI on GitLab** with Bedrock, Vertex or Foundry.
- **Start Enterprise yourself.** `brindle account upgrade --enterprise --seats N --org ORG`, with `--trial` for a free 7-day trial of up to 10 seats.

**Changes to note**

- Brindle-CI accepts only API keys, identity federation or cloud credentials. A Claude or ChatGPT subscription login is refused for every repository.
- `brindle keys` never stores a Claude subscription token (`CLAUDE_CODE_OAUTH_TOKEN`), and native profiles refuse it as their key.
- Org profile library uploads with a literal key value in an `env.` line are refused.
- `brindle account upgrade --seats 0` is refused.
- 0.2.2 was not published on its own; its changes are included here.
