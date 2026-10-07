**brindle CI**

- **`brindle ci init` sets up more for you.** It creates the `brindle` label, offers to mark a check as required (fix builds only fix required checks), and leaves you on the branch you started on.
- **Federated runs keep their Claude access for the whole run.** brindle refreshes the Anthropic token during the run through a local credential proxy, so the agents never see it. A 10-minute federation rule lifetime is enough.
- **Unattended federation setup:** `--rule-id`, `--organization-id` and `--service-account-id`.

**Fixes**

- Fixed bugs related to `brindle ci init`.
