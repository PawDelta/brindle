# Coverage-based scoped checks: measurement and no-go decision

Measured on 2026-11-06 at `11c3a481`, on macOS with Python 3.12.12,
pytest 9.1.1, pytest-cov 7.1.0 and coverage 7.16.2.

**Leave the selector unchanged for now**, using the task's explicit no-change
allowance. None of the three sampled changes had a usable base-commit map.
Safe fallback therefore saves zero tests. The optimistic projection below
would omit six tests that actually failed in the unchanged checkout. The other
source-change sample reduced the count but did not improve elapsed time.

This commit records the investigation only. It does not implement a coverage
cache, change CI, or add `test_checks_pick_tests_by_coverage`. Those are deferred
under the no-go decision, not claimed as completed implementation work.

## Measurements

The baseline is the existing **import fallback**, including its default
40-file cap. It uses `Imports.tests_reaching` for changed source modules,
includes changed test files, and uses `capped_targets` when necessary. Graph
lookups were not benchmarked. Test counts include parametrized cases.

Each commit supplies its changed paths via `git diff --name-only COMMIT^ COMMIT`.
Tests execute against the current checkout, not a historical checkout. The
changed source files for the two source-change rows are byte-identical between
the sampled commit and HEAD, verified with `git diff`. The test-only row uses
the current version of its test file. No full suite was run.

| Change | Import-reached files → files run | Baseline cases | Baseline pytest time | Optimistic coverage files / cases | Projected-selection pytest time |
| --- | ---: | ---: | ---: | ---: | ---: |
| `3b621d51` conservative plan approval | 122 → 2 | 51 | 31.79 s | 1 / 10 | 4.36 s |
| `f193ee47d` import-scope fixes | 3 → 3 | 42 | 22.04 s | 2 / 33 | 23.62 s |
| `0c6a0013` fold plan reminder test | 1 → 1 | 10 | 4.36 s | 1 / 10 | 4.36 s |

The two 10-case projections are the same `tests/test_plan_first.py` run; its
measurement is reused, not presented as three independent runs. Baseline file
sets, in table order:

- `tests/test_agents.py tests/test_plan_first.py`
- `tests/test_check_scope.py tests/test_notices.py tests/test_scoped_checks.py`
- `tests/test_plan_first.py`

The second projection removes `tests/test_notices.py`. Baseline import-selection
time was 1.240 s, 1.857 s and 0.021 s respectively. Single-run wall times are
indicative: coverage even made the first sample appear faster. These results
do not establish a stable instrumentation overhead or whole-suite speedup.

Coverage collection used the same file sets with:

```text
.venv/bin/python -m pytest <files> -q --dist=no --cov=brindle --cov=scripts --cov-branch --cov-context=test --cov-report=
```

| Change | Collection pytest time | Data size | Recorded contexts | Lookup time |
| --- | ---: | ---: | ---: | ---: |
| `3b621d51` | 22.80 s | 3,260,416 bytes | 154 | 0.0223 s |
| `f193ee47d` | 23.73 s | 2,224,128 bytes | 127 | 0.0178 s |
| `0c6a0013` | 7.52 s | 839,680 bytes | 31 | 0.0016 s |

The projection unions test-file paths from `CoverageData.contexts_by_lineno`
for the added lines of each zero-context patch, plus changed test files. This
is deliberately an optimistic **post-change, partial-suite** projection, not
a safe selector. It cannot stand in for complete coverage of the old commit.
The source-change hunks also had 2 and 4 lines without recorded execution;
the second touched anonymous/import-time contexts. A production selector
must resolve these cases conservatively, not discard them.

## Availability and failures

No coverage data was present in this worktree or the repository cache at the
start. CI records contexts on the Linux pull-request job, but does not upload
the data. It does not record contexts on pushes to main or on macOS. Exact-base
cache availability for the sample was **0/3 (0%)**, so missing/stale-map fallback
would retain **51, 42 and 10 cases**: zero count reduction and zero test-execution
time saved relative to the measured baseline. Lookup would add overhead.

There is also a producer issue in the measured environment. With the repo's
`--dist worksteal` addopts, no workers, and `--cov-context=test`, the recorded
context set was exactly `[""]`. This was reproduced with all four cases in
`tests/test_check_scope.py`. Explicit `--dist=no` produced the per-test contexts
in the table. The installed pytest-cov plugin chooses its distributed master
when `dist != 'no'`. CI's serial coverage command has the same option shape;
its artifact must be checked before relying on those contexts.

The 51-case baseline had **45 passes and 6 failures**, all in `test_agents.py`:
`test_watch_pane_shares_the_window_and_messages_reach_the_agent`,
`test_chat_exit_closes_its_session_even_with_extra_windows`, and four rows of
`test_review_base_after_merge`. The first two failed connecting to test tmux
sockets; the other four failed merge-base assertions. Git is 2.33.1 here;
their root cause was not established. The same six failures recurred in the
coverage run and in a focused run requested outside the sandbox.

The optimistic 10-case selection would skip **all six observed failures**.
They existed without source changes and are not evidence of a regression
caused by the sampled commit. Nevertheless this experiment cannot claim
that narrowing preserved failures. Missing-map fallback skips **zero** of
them. The 42-case and 10-case baselines passed, as did the 33-case projection.

## Validation and next decision

`.venv/bin/python -m pytest tests/test_check_scope.py -q` passed: **4 passed in
1.00 s**. The coverage reproduction also passed all four cases. The literal
`uv run pytest tests/test_check_scope.py -q` could not start pytest: uv 0.9.15
panics in macOS system configuration, including with escalation. Homebrew uv
0.12.15 could start but dependency downloads failed DNS resolution, including
with escalation. Tests used cached packages exposed only to this worktree's
venv. Production files and existing tests were never edited.

Before reconsidering the selector, establish complete, successful coverage
artifacts keyed by the exact tested commit, with platform/Python/dependency
identity and both serial and parallel contexts. Measure their availability at
actual worker merge bases; do not stamp partial or anonymous data as complete.
Then evaluate a selector separately, with the requested fallback regression
table for missing/stale maps, unplaced changes, config, conftest, fixtures,
non-Python paths and new files. A Linux-only map must not silently narrow a
macOS check. The supervisor should also investigate the six unchanged baseline
failures before interpreting a future skipped-failure comparison.
