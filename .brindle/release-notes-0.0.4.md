**brindle CI**

- **Sign Claude in with workload identity federation instead of an API key.** `brindle ci init` asks "API key or identity federation?" (or pass `--credential key|federation`). Federation stores your federation rule, organization and service-account IDs as repository variables, so there's no Anthropic secret to create, rotate or leak. Each run exchanges its GitHub OIDC token once for a short-lived Anthropic token that all of the run's agents share. `init` prints the Claude Console rule to create: it's limited to brindle's workflows, with a token lifetime of at least 2 hours.

**Fixes**

- **`brindle ci init` no longer crashes** with an `AttributeError` after step 2. It now builds your brindle Pro account the way `brindle account` does.
- **The GitHub App install page opens with your org.** It used to open without it, and the server answered `org_id is required`.
- **`init` refuses to run outside a checkout of `--repo`.** It used to build the setup pull request from whatever repository you ran it in.
- **An unset `ANTHROPIC_API_KEY` secret no longer shadows other Claude credentials in CI.**
- `brindle account` and the README list brindle CI with `brindle ci init` instead of "back in a later release".
