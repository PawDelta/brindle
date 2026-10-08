**For everyone**

- **Reviews start after the checks.** When a repo has checks, brindle runs them first and starts the reviewer with the results in its prompt, so no verdict rests on the diff alone. A repo without checks gets its review straight away, as before.
- **Workers check their own work.** Every worker now goes through its finish line item by item before it reports, with a test for each behaviour it was asked for. When a task turns out too big for one branch, it finishes a coherent part and says exactly what's left.
- **A cheaper light tier.** The new built-in `developer-light` profile (Sonnet, low effort) is the first choice for light tasks. `developer-heavy` and `developer-codex` now build on `developer`, so a change to it reaches all three. Lint and type-check commands (`ruff check`, `mypy`, `tsc`, `eslint`) are pre-approved.
- **Optional second review.** `"second_review": {"heavy": "reviewer-codex"}` in `.brindle/config.json` asks a second reviewer for that weight. Both must approve before anything merges.
- **Sturdier check runs.** A check run for a commit the branch has moved past gives up, a run waiting on another's result reuses its failure instead of running again, and the supervisor is warned before a slow run hits its timeout.
- **Better milestone checks.** Supervisors are told to write milestone checks that test what each milestone promises, and to tighten the check that let a gap through when the completion audit finds one.

**Pro**

- **Smarter profile picks.** The learner now finds a better profile sooner on similar tasks, and when it keeps your default the routing line says why. `brindle learning seed` sends a repo's finished history to the learner once, so it starts from what you've already run.
- **Brief warnings.** `assign` and `handoff` warn when a task's brief looks like ones that went badly in this repo. This runs on your machine.
- **Permission suggestions.** Commands you approve are now recorded by default, and `get_progress` lists the ones brindle suggests pre-approving. Set `"permission_policy": "off"` to turn it off.
- **Results by profile.** `brindle cost report` shows each worker profile's merge rate, review rounds and cost per merged branch, and suggests where a second review would help.

**Team**

- **Hidden dollar amounts.** When your org hides dollar amounts from members, brindle shows a status word ("within limit", "getting close", "at limit") instead of org dollars. Limits are still enforced, and your own stricter limits still show dollars.

**Changes to note**

- With checks configured, the MCP `request_review` tool returns before a reviewer exists, so its reply no longer carries a reviewer id.
- Reviews on a repo with a slow suite now start once the checks finish.
