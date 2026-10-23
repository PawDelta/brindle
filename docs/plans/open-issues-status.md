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

Only T1 is assigned to this branch. Other task statuses belong to their workers;
the supervisor owns the phase-wide status and full-suite run.
