---
name: learned
description: Rules every profile in this repo follows, including ones learned from repeated review findings
deny_patterns:
  - sleep
  - retry
  - timeout.*=.*[0-9]+
require_tests_for:
  - tests/conftest.py
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
- When a lookup takes an optional filter (such as a profile), treat None as 'no filter' only when the caller truly means any; never let a missing value match a live resource that belongs to a different identity. When an action can be triggered twice, make the second trigger check for and respect existing in-flight work instead of cancelling or replacing it, and cover that path with a test that runs the real dispatch code rather than a mock. (learned 2026-10-19 from 3 reviews of developer-heavy work: Lookup ignores caller's filter and races on detached work; rule 6bd8a8a9)
- When a worker commits changes before the stop hook in the first turn, ensure that a snapshot is created for that turn's committed state. Track the initial baseline separately or detect when HEAD advances from the worktree’s starting commit and snapshot accordingly. (learned 2026-10-20 from 3 reviews of developer-heavy work: Rewind Point Missing for First Turn; rule 05a9357b)
- When a function calls another function that wasn't previously called, ensure all tests that mock the original function also mock the new function. Add mocks for any newly added calls in existing test patches. (learned 2026-10-20 from 3 reviews of developer-codex work: Test Mock Incomplete After Function Addition; rule ed107119)
- When implementing session recovery procedures, ensure that both live and paused sessions clear credential blocks and transition states consistently. Share common recovery logic between paths to avoid duplication and inconsistencies. (learned 2026-10-20 from 3 reviews of developer-heavy work: Paused-session recovery leaves credentials blocked; rule 1a2ca134)
- Before making changes that fix a lifecycle or concurrency issue, write a test that reproduces the problem on unmodified code and demonstrates the fix. The test must be deterministic and exercise the actual failure scenario without relying on sleeps or retries. (learned 2026-10-21 from 3 reviews of developer-sonnet work: Missing Regression Test for Lifecycle Issue; rule 9ff0b711)
- Ensure all tmux commands are applied in a way that doesn't skip options on older versions. When a command fails, previous commands should still be applied if they're not impacted. (learned 2026-10-21 from 3 reviews of developer-sonnet work: Regression in tmux version compatibility; rule 188f14c5)
- When handling concurrent writes to a shared configuration file, ensure each write operation uses a unique temporary file in the destination directory. Use atomic replace operations with cleanup on failure and optionally serialize read/modify/replace if preserving concurrent changes is required. (learned 2026-10-21 from 3 reviews of developer-sonnet work: Concurrent Write to Config File Causes Race Condition; rule 22abfd5e)
