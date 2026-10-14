---
name: learned
description: Rules every profile in this repo follows, including ones learned from repeated review findings
---
Tests in this repo must earn their place. CI fails a pull request whose new test
covers no line or branch that other tests don't (scripts/check_new_tests.py), or
that adds more than 15 test functions or more test lines than source lines.
- Add the fewest tests that would fail without your change: usually one, a
  regression test for the bug or the new branch. Extend an existing test or
  parametrize table before writing a new test, and a new file only for a new module.
- Never test wording (help, prompts, docs, messages), constants, defaults, enum
  values or entry-point registration; never assert that a mock was called with
  what the test just gave it.
- A parametrize table gets one row per branch plus real edge cases, not one per
  input. A row or test that covers no new code is fine only as a deliberate input
  variant (a bypass attempt, a race, an input from a real incident), marked
  `@pytest.mark.variant("why")`.
- Use database rows and fakes, not real tmux sessions, subprocesses or sleeps,
  unless spawning or timing is what the test is about.
- Reviewers: treat any of the above in a diff as a blocker, the same as a missing
  test. Deleting a redundant test is an improvement, not a coverage loss.
- When a lookup takes an optional filter (such as a profile), treat None as 'no filter' only when the caller truly means any; never let a missing value match a live resource that belongs to a different identity. When an action can be triggered twice, make the second trigger check for and respect existing in-flight work instead of cancelling or replacing it, and cover that path with a test that runs the real dispatch code rather than a mock. (learned 2026-10-13 from 4 reviews of developer-sonnet work: Lookup ignores caller's filter and races on detached work; rule 8967a6e2)
- Before changing code for a reported bug, write a test that reproduces the reported symptom and fails on the unmodified code. Fix the root cause in the source, then confirm that test passes. A test that already passes before the change is not evidence of a fix. (learned 2026-10-13 from 3 reviews of developer-codex-heavy work: Bug fix not reproduced or root-caused; rule 634c00cd)
