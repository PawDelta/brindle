**For everyone**

- **Build on a profile with `extends`.** A profile can extend another and add to it, so you only write what differs.
- **Rule packs and rule checks.** Reusable sets of rules for workers, shipped with brindle (security, minimal diffs, tests only) or your own, with checks that keep a branch to them.
- **`brindle profile`.** `new`, `lint` and `show` write a profile, check every profile you have, and print one as brindle resolves it.
- **Rewind a worker.** `brindle agent rewind` puts a worker's worktree back to how it was after an earlier turn and restarts it from there with a note. Works with any provider.
- **Conflict-aware merging.** Branches that touch the same files merge one at a time, and a conflict goes back to the worker that owns it. `protected_paths` keeps chosen files for you to resolve.
- **Secrets stay out of local workers.** Local workers no longer inherit brindle's own secrets or CI tokens.
- **`brindle cost`.** A summary of what your agents spent.

**Behavior change**

- A profile whose `api_key_env` names a job secret (such as `GITHUB_TOKEN`) no longer receives it.

**Pro**

- **Cost reports, estimates and budgets.** See spend by day, profile and goal, estimate what a goal will cost, and set limits.
- **Learned rules.** Rules suggested from review findings that keep coming back.
- **Guardrails.** Limit where a profile can write and read, and which environment variables it gets.

**Team**

- **Org library.** Admins share profiles and rule packs with everyone in the org.
- **Org budgets and protected paths.** Set in your org policy.
- **Single sign-on.** Sign in with Google, GitHub, SAML or OIDC, available on every tier; admins can require it.

**Enterprise**

- **Cost centers.** Track spend per center and request approval for more.
- **Managed models and rollout.** Set the models your org uses and control which versions run.
- **Audit export.** Ship the audit log to your SIEM.
- **SCIM.** Provision and remove members from your identity provider.

**Fixes**

- Agent environment variables are no longer passed on the tmux command line.
- Org budgets no longer let spend through when a model's price is unknown.
