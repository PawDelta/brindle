**For everyone**

- **`brindle login`.** The same as `brindle account login`, and it also signs you in to your company's Claude and Codex route when one is set up. `brindle doctor --fix` does the same.
- **Use your company's access yourself.** When your org has no company setup, `brindle login` looks, read-only, at what this machine is already signed in to (Claude Code's managed settings, `claude auth status`, AWS SSO profiles, the active `gcloud` project and `az` subscription, `codex login status`) and offers to use it. The default is no; nothing is saved without a yes, and no token or credential file is read. You must be authorized to use the access you connect.
- **Doctor shows who each agent runs as.** `brindle doctor` names the identity behind each agent CLI (Claude org, AWS account and role, GCP project, Azure subscription) and who is billed.
- **Signing out pauses agents.** While agents run, brindle checks every 30 s that Claude and Codex are still signed in. When one signs out, its workers pause with their worktrees, commits and conversations kept. They resume after you sign back in, but only as the account they started under.
- **Sessions stay with their identity.** A Claude agent records the identity it launched under (ids only, never tokens). While someone else is signed in, brindle won't resume, rewind, message, hand off to or show that agent's session, and lists it as `locked: <org/account>`. This is a guard inside brindle, not a sandbox: on one OS user the files stay readable outside brindle.
- **Clearer key warnings.** `brindle doctor` and `brindle keys set` say when Codex, signed in with ChatGPT, will ignore an API key, and Antigravity's `GEMINI_API_KEY` warning appears only when it applies.

**Team**

- **Company agent setup.** Org admins choose the route members' agents use (a Claude plan, Bedrock, Vertex, Foundry or an org key, plus Codex) in Settings › Company agents. `brindle login` then signs each member in to it, and the dashboard's Getting started page shows members the company path.

**Enterprise**

- **Enforce the company identity.** With enforcement on, Claude agents start only under the company's identity and run on exactly the route brindle checked: a profile's env lines can't change it, and other cloud credentials and Claude keys are left out of their panes. They pause when the signed-in identity changes.

**Changes to note**

- Codex reports only "signed in", so Codex sessions are paused on sign-out but can't be locked to one account. A Codex agent paused this way stays paused until you run `brindle continue`.
- Agents started before this release, or without a company setup, have no recorded identity and are never locked.
- Signing out of brindle keeps the stored company environment for brindle's panes; `brindle login` replaces it.
- Org policies ignore `agent_setup` fields this version doesn't know, so a newer dashboard setting doesn't break older clients.
