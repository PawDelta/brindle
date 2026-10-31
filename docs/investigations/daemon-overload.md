# Daemon overload investigation

Verdict: **reproduced on unmodified production code**, with an important
qualification about the pools. Abandoned session diffs occupy AnyIO workers
and delay a cheap synchronous read, `/v1/repos`. `/v1/meta` also missed its
300 ms bound in a clean run, but the asyncio default executor was empty.
The observation does not establish the proposed chain through that executor.

## Setup and method

Measured on macOS arm64, CPython 3.12.12, at production revision `56e845a4`.
Only the soak test and this report change. The test uses the existing isolated
repository and database fixtures, `build_app`, and the daemon's real uvicorn
HTTP protocol on an ephemeral loopback TCP port. No tmux session is needed.

`test_session_diff_overload` imports a real Git DAG with `git fast-import`:
216 session commits, comprising 12 two-parent supervisor merges and 204
worker commits, across 24 text files. The last merge deletes the base's
`app.py`, requiring deletion attribution to traverse every first-parent
step. Worker commit trees and their corresponding merge trees agree.

An isolated HTTP diff and both cheap reads establish the baseline. Eight
concurrent pollers then each issue five diff requests, abandon the response
after 75 ms, close their connections, and ask again. Connection establishment
has a separate, longer bound. AnyIO's default 40-token limiter is unchanged.
Both cheap reads run concurrently after the pollers finish; their complete
response times are compared with 300 ms, retaining the actual late latency.

Instrumentation delegates to the original `session_review.diff` and
`git.run`: thread-local counters count actual git invocations inside each
diff, and a monotonic clock measures execution. The HTTP diff measurement
also includes routing and serialization. `refresh_branch` launches one
additional git process outside the counted diff function. Limiter samples
record occupied slots and waiters after poll attempts and before the cheap
reads. The asyncio executor's existing thread count and queue size are
recorded before submitting one no-op probe to it. This TCP scenario does not
exercise Unix peer authentication or action routes.

All request tasks drain before server shutdown, database fixture teardown,
or removal of instrumentation. Completion and file-count assertions ensure
that an HTTP error or an unfinished handler cannot masquerade as overload.
The existing 50-second wall-clock guard bounds the network exercise.

## Final harness measurements

The final fixture failed on unchanged production code before adding any
xfail marker. Every handler completed, and the sole failure was the measured
cheap-read deadline:

| Measurement | Result |
| --- | ---: |
| Git processes inside each diff | 70 |
| Baseline HTTP diff | 0.797 s |
| Loaded diff execution, min–max | 5.943–10.461 s |
| Baseline `/v1/meta` / `/v1/repos` | 0.0013 / 0.0031 s |
| Loaded `/v1/meta` / `/v1/repos` | 0.311 / 5.044 s |
| Abandoned / completed loaded diffs | 40 / 40 |
| AnyIO occupied / capacity at probe | 40 / 40 |
| AnyIO queued diffs at probe | 0 |
| Asyncio executor threads / queued before probe | 0 / 0 |
| Asyncio executor no-op latency | 0.0194 s |
| Complete pytest run, including fixture and cleanup | 16.20 s |

The final test uses `xfail(strict=True, raises=ReadDeadlineMiss)`: only the
read-latency failure is expected. Fixture, HTTP, completion, and cleanup
failures remain failures. A future implementation that serves both reads
within 300 ms produces a strict XPASS requiring removal of the marker.
The variant marker identifies the deliberate concurrent incident input;
this is one added test, within the checker’s 150-line allowance.

## Observations while scaling

The first, larger fixture had 240 commits, 80 merges, and 32 text files.
It measured **282 git processes per diff**, 2.234 s baseline HTTP diff latency,
and 19.510–31.939 s execution times among completed loaded diffs. Eight
pollers making six requests each abandoned all 48 requests. AnyIO reached
40 occupied slots and 8 queued requests. `/v1/repos` took 33.711 s;
`/v1/meta` took 0.043 s and the executor probe 0.0018 s. This exploratory
run shut down before eight handlers finished, so it is not the final
regression result. Explicit draining replaced that premature shutdown.

A clean intermediate fixture used 216 commits, 24 merges, and 24 files:

| Measurement | Result |
| --- | ---: |
| Git processes inside each diff | 106 |
| Baseline HTTP diff | 3.186 s |
| Loaded diff execution, min–max | 26.333–36.740 s |
| Baseline `/v1/meta` / `/v1/repos` | 0.0079 / 0.0262 s |
| Loaded `/v1/meta` / `/v1/repos` | 3.460 / 22.028 s |
| Abandoned / completed loaded diffs | 40 / 40 |
| AnyIO occupied / capacity at probe | 40 / 40 |
| AnyIO queued diffs at probe | 0 |
| Asyncio executor threads / queued before probe | 0 / 0 |
| Asyncio executor no-op latency | 0.292 s |
| Complete pytest run | 50.24 s |

That run failed specifically with `ReadDeadlineMiss` after complete cleanup.
The final fixture keeps 216 commits and 24 files but halves the merges to
reduce subprocess work and leave more room in the CI time budget. A larger
56-merge trial exceeded the wall-clock guard; neither that failure nor an
early connection-establishment failure counts as the reproduction.

## Interpretation and limits

`routes_read._json` produces synchronous Starlette handlers, so a diff
occupies one AnyIO token until it returns even after its HTTP client leaves.
The same pool dispatches `/v1/repos`, which only reads workspace rows here.
Repeated requests can therefore fill the pool and stall an unrelated read.
The history cost follows first-parent steps, merges, and blamed files, not
just the number of commits: `_deleters` performs two git commands per step,
`_owners` one per merged parent, and `_blame` one per file with additions.

`/v1/meta` is asynchronous. With TCP authentication it needs neither pool.
Its missed deadline in the clean run is consistent with general scheduling
or process-launch contention, but this harness does not isolate that cause.
Its fast response in the larger exploratory run shows that AnyIO saturation
alone does not invariably stall meta. No evidence here shows diffs occupying
the asyncio executor used by actions and Unix peer checks.

The harness shares a process/event loop with its HTTP clients and uses a
synthetic history, not an incident repository. Host load affects timings;
CI platform timing and Unix-socket authentication remain unverified. The
linked [background artifact](https://claude.ai/artifact/R5h11rt88vo1SBTFdHkbWA)
returned only a browser shell and its content endpoint returned HTTP 403;
its Problem section could not be read. The supplied task hypothesis guided
the investigation.

Follow up by comparing a captured incident history, recording event-loop
lag and OS scheduling under load, and measuring Unix authentication and
action traffic independently. No production mitigation is included.

## Re-run

```sh
uv run pytest tests/test_daemon_soak.py -q -m serial -k overload
```

Add `-s` to see the JSON measurements. This requires permission to bind a
loopback socket. The exact finish-line command above completed with
`1 deselected, 1 xfailed in 34.08s` after the strict marker was added.
The full suite is left to brindle's merge gate.
