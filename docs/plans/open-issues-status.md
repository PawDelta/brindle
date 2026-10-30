# Open issues: worker status

| Task | Issue | Branch | Status (done / blocked / skipped) | Failing-before test | Full suite | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| T1 | #199 | `fix/test-tmux-lifetime` | done | `tests/test_auth_failure_handling.py::test_test_server_survives_closing_its_first_session`: failed before fixture change with `TmuxError: no server running`; passes after | Deferred to supervisor after Phase 1 | Per-test keeper session survives closing the first test-owned session; existing teardown reaps it. Required auth-failure file: 16 passed. |

The GitHub issue body was read with `gh issue view 199`; graphify identified
the fixture and tmux session operations before editing. Closing the only session
reproduced the server exit deterministically. The original xdist interleaving
and the suspected detached helper were not reproduced. The fixture already used
unique per-test sockets; this change adds the lifetime keeper required by T1.

Validation:

- Before: `uv run pytest tests/test_auth_failure_handling.py::test_test_server_survives_closing_its_first_session -q` — 1 failed at `new-window`, with no server running.
- After: `uv run pytest tests/test_auth_failure_handling.py -q` — 16 passed.
- `uv run pytest tests/test_cleanup_leftovers.py tests/test_tmux_hang.py -q` — 16 passed.
- `uv run python scripts/check_test_quality.py` — 2736/4213 tests, 0 new issues.

Review correction: processless fake agent rows used `@0`, which now names the
keeper's window. The native-subagent and shared orchestration helpers now use
non-numeric fake pane targets that cannot alias a real tmux pane. The fallback
review test explicitly models exited reviewers and a busy live supervisor,
without reading the keeper's shell to determine message delivery. Existing
behavioral assertions and the lifetime regression are unchanged.

The following selectors failed together before that correction (3 failed) and
passed afterward (3 passed):

- `tests/test_native_subagents.py::test_agent_entry_drops_running_subs_once_the_parent_is_not_running`
- `tests/test_review_after_checks.py::test_a_review_that_cannot_start_after_the_checks_reaches_the_supervisor`
- `tests/test_pipeline.py::test_reviewer_without_verdict_gets_fallback_then_an_escalation_event`

The affected helper consumers also pass: `uv run pytest tests/test_native_subagents.py tests/test_merge_order.py tests/test_pipeline.py tests/test_review_after_checks.py tests/test_pipeline_idempotent.py tests/test_pipeline_scratch_files.py tests/test_workspaces.py tests/test_reviewer_dedupe.py tests/test_routing.py tests/test_task_deps.py tests/test_conflict_resolve.py tests/test_heavy_escalation.py -q` — 123 passed.
The required `uv run pytest tests/test_auth_failure_handling.py -q` was rerun
after the correction — 16 passed, including the keeper lifetime regression.

Only T1 is assigned to this branch. Other task statuses belong to their workers;
the supervisor owns the phase-wide status and full-suite run.
