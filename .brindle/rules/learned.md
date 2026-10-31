---
name: learned
description: Rules every profile in this repo follows, including ones learned from repeated review findings
deny_patterns:
  - sleep
  - retry
  - timeout.*=.*[0-9]+
require_tests_for:
  - tests/conftest.py
# Every process the daemon can reach starts through brindle.spawn (read,
# action, start, detach); only these files may start one directly.
direct_spawn_allowed:
  - src/brindle/spawn.py          # the gate itself
  - src/brindle/tmux.py           # starts the tmux server; changes run at tmux's own priority
  - src/brindle/cli.py            # CLI commands: editor, pager, attach, the person's terminal
  - src/brindle/demo.py           # `brindle demo`
  - src/brindle/upgrade.py        # re-execs the CLI
  - src/brindle/watch.py          # `brindle watch` and the sidebar: tmux attach
  - src/brindle/sidebar_logo.py   # the sidebar pane's own tmux display
  - src/brindle/ci_client.py      # `brindle ci` and the CI runner, never the daemon
  - src/brindle/ci_adapters.py    # the CI runner's review adapters
  - src/brindle/native/tools.py   # the native agent runner's own tool calls (in the agent's process)
  - src/brindle/native/lsp.py     # the native agent runner's language server
  - scripts/*                     # repo scripts
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
- Before changing code for a reported bug, write a test that reproduces the reported symptom and fails on the unmodified code. Fix the root cause in the source, then confirm that test passes. A test that already passes before the change is not evidence of a fix. (learned 2026-10-22 from 5 reviews of developer-gemini work: Bug fix not reproduced or root-caused; rule 298b715f)
- When implementing session recovery procedures, ensure that both live and paused sessions clear credential blocks and transition states consistently. Share common recovery logic between paths to avoid duplication and inconsistencies. (learned 2026-10-22 from 3 reviews of developer-haiku work: Paused-session recovery leaves credentials blocked; rule 5a9063bb)
- When a template resource refers to another resource in the same template by a literal ARN string (for example via !Sub in a trust or policy document), declare the dependency explicitly with Ref/GetAtt or DependsOn so CloudFormation creates resources in the right order. Keep the dependency in place when a condition can disable the referenced resource, and lint the template before pushing. (learned 2026-10-25 from 3 reviews of developer-codex work: Implicit cross-resource dependency by string ARN; rule 074b12b7)
- Before making changes that fix a lifecycle or concurrency issue, write a test that reproduces the problem on unmodified code and demonstrates the fix. The test must be deterministic and exercise the actual failure scenario without relying on sleeps or retries. (learned 2026-10-25 from 3 reviews of developer-heavy work: Missing Regression Test for Lifecycle Issue; rule eb35d432)
- When updating an API that accepts optional parameters, maintain backward compatibility in existing stubs and function signatures. In `check_summary`, conditionally pass arguments like `cancel` only if they're present to avoid breaking tests using old lambda definitions. (learned 2026-10-26 from 3 reviews of developer-heavy work: Check Gate Stalls Due to API Change; rule 200fea08)
- When adding parameterized test cases, ensure all requested scenarios are included in the @pytest.mark.parametrize list. If a review requests a specific test case, verify it is present before merging. (learned 2026-10-26 from 3 reviews of developer-codex work: Missing Parameterized Test Case; rule cf9c7227)
- When awaiting asynchronous responses in a UI component, capture the state at request initiation and validate it before updating the DOM or state. If the state has changed during the wait, discard the stale response to prevent overwriting incorrect data. (learned 2026-10-30 from 9 reviews of developer-heavy work: Stale Response Handling in Web Chat; rule 58312e78)
- When a policy resolution returns a denied decision, ensure it is propagated upward instead of silently falling back to a default or less restrictive policy. This prevents unauthorized access through misconfigured or missing policies. (learned 2026-10-30 from 7 reviews of developer-heavy work: Managed-device resolver drops denied member policy; rule 2bbd4c30)
- When implementing failure-safe mutations, ensure that the original content is preserved until the replacement is fully written and committed. Use atomic write patterns that prevent data loss on write failures, such as writing to a temporary file first and then moving it into place. (learned 2026-10-30 from 5 reviews of developer-heavy work: Failure-Safe Mutation Incomplete; rule 43fdb4cd)
- When handling credential errors, ensure that sensitive data such as API keys or secrets are never exposed in error messages, logs, or responses. Sanitize error paths to prevent secret leakage. (learned 2026-10-30 from 3 reviews of developer-heavy work: Secret Exposure in Credential Error; rule 1150b51d)
- When checking model permissions, ensure that the same predicate used in `ProPolicy.check_assign` is also applied during alias resolution and policy construction. Normalize provider/region prefixes for permission checks but maintain exact matching for pinned models. Update tests to validate both `GET` offering and `PUT` acceptance behaviors. (learned 2026-10-30 from 3 reviews of developer-heavy work: Model Permission Mismatch in Policy and Routing; rule f6ae9636)
- When checking test coverage for production changes, do not treat deleted test files as valid coverage. Track added/modified test paths separately and require at least one test with new content to pass validation. (learned 2026-10-30 from 3 reviews of developer-heavy work: Test Deletion Allows Bypass of Coverage; rule eb5d2120)
- In patch parsing, only recognize file headers (`--- ` or `+++ `) before entering a hunk. Ensure that body lines starting with `--- ` are not mistaken for file headers and cause misattribution in overlap scoring. (learned 2026-10-30 from 3 reviews of developer-heavy work: Diff Parser Misinterprets Hunk Content; rule a170d30e)
