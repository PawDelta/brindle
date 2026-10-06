**brindle CI**

- **Sign Claude in with workload identity federation instead of an API key.** `brindle ci init` asks "API key or identity federation?" (or pass `--credential key|federation`) and stores your federation rule, organization and service-account IDs as repository variables, so there's no Anthropic secret to create, rotate or leak. `init` prints the Claude Console rule to create.

**Fixes**

- Fixed bugs related to `brindle ci init`.
- Fixed bugs related to Claude credentials in CI.
