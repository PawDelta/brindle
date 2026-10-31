# Daemon overload investigation

Verdict: **pool saturation reproduced before (`56e845a4`), avoided after
the heavy-route lane (`a085f9f1`); the 300 ms deadline remains intermittent**.
Previously, abandoned session diffs filled AnyIO's default pool and delayed `/v1/repos`.
After rebasing onto `brindle/session-21`, the same load occupied the separate
four-worker heavy lane and shed excess requests. Both cheap reads passed
their unchanged 300 ms bound in one completed run, producing strict XPASS;
the marker was removed. The next run missed the bound for `/v1/repos`
despite zero default-pool occupancy. All results, including this failure,
are retained below. The gate now checks the lane's isolation guarantee,
successful reads, complete request accounting, and a loose 2-second sanity
bound, with no xfail. The tighter latency target moves to follow-up work
after the diff cache and spawn gate land.

| Historical measurement | Loaded meta | Loaded repos | Default pool at probe |
| --- | ---: | ---: | ---: |
| Before (`56e845a4`) | 311 ms | 5,044 ms | 40/40 |
| After lane, run 1 (`a085f9f1`) | 50 ms | 47 ms | 0/40 |
| After lane, run 2 (`a085f9f1`) | 215 ms | 377 ms | 0/40 |

The supervisor reported host load around 50 from parallel workers during
run 2. The harness did not measure host load itself.

## Setup and method

Measured on macOS arm64, CPython 3.12.12, before at production revision
`56e845a4` and after at `a085f9f1` (which includes the heavy-route lane).
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
After the lane change, received HTTP 503 responses count separately as shed
only when their error code is `busy`; other received errors still fail.
Both cheap reads run concurrently after the pollers finish. At that probe,
the gate requires zero default-pool waiters and fewer borrowed tokens than
capacity. Both reads must succeed within a 2-second sanity bound. All 40
client outcomes must be accounted for as successful responses, abandoned
responses, or received 503 `busy` responses. Every started diff must finish,
and all default/heavy limiter tokens must be released after draining.
The historical measurements below used the original 300 ms target.

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

## Before (`56e845a4`)

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

The original test used `xfail(strict=True, raises=ReadDeadlineMiss)`:
only the read-latency failure was expected. Fixture, HTTP, completion, and
cleanup failures remained failures. The lane change subsequently produced
strict XPASS, so the marker has been removed.
The variant marker identifies the deliberate concurrent incident input;
this is one added test, within the checker’s 150-line allowance.

## After heavy-route lane (`a085f9f1`)

These two historical runs kept the fixture, eight pollers, 40 attempts,
75 ms response bound, and original 300 ms cheap-read assertion unchanged.
The harness also recorded the app's separate heavy admission/worker
limiters as well as the default pool.

| Measurement | Result |
| --- | ---: |
| Git processes inside each admitted diff | 70 |
| Baseline HTTP diff | 2.005 s |
| Loaded diff execution, min–max | 2.767–2.907 s |
| Baseline `/v1/meta` / `/v1/repos` | 0.0067 / 0.0129 s |
| Loaded `/v1/meta` / `/v1/repos` | 0.0503 / 0.0474 s |
| Client-abandoned requests | 19 |
| Received 503 `busy` responses (shed) | 21 |
| Admitted / completed loaded diffs | 4 / 4 |
| Default AnyIO occupied / capacity at probe | 0 / 40 |
| Default AnyIO sampled peak occupied / queued | 0 / 0 |
| Heavy worker occupied / capacity at probe | 4 / 4 |
| Heavy admission occupied / capacity at probe | 4 / 4 |
| Heavy worker sampled peak occupied | 4 |
| Asyncio executor threads / queued before probe | 0 / 0 |
| Asyncio executor no-op latency | 0.0516 s |
| Complete pytest run, including fixture and cleanup | 7.03 s |
| Result with original strict marker | XPASS |

The immediate finish-line run after removing the marker failed the unchanged
read assertion (no intervening workload or production changes):

| Measurement | Result |
| --- | ---: |
| Git processes inside each admitted diff | 70 |
| Baseline HTTP diff | 1.513 s |
| Loaded diff execution, min–max | 2.694–2.769 s |
| Baseline `/v1/meta` / `/v1/repos` | 0.0031 / 0.0083 s |
| Loaded `/v1/meta` / `/v1/repos` | 0.2150 / 0.3771 s |
| Client-abandoned / received shed | 7 / 33 |
| Admitted / completed loaded diffs | 4 / 4 |
| Default AnyIO occupied / capacity; queued at probe | 0 / 40; 0 |
| Heavy admission / worker occupancy at probe | 4 / 4 |
| Asyncio executor threads / queued before probe | 0 / 0 |
| Asyncio executor no-op latency | 0.0023 s |
| Complete pytest run | 6.40 s |
| Result without xfail | `ReadDeadlineMiss` |

These results led to an explicit supervisor decision: gate on default-pool
availability, successful reads, complete request accounting, and a 2-second
sanity bound. There is no xfail. Restoring a strict xfail would merely
exchange intermittent FAIL for intermittent XPASS. The original 300 ms
miss remains recorded; the tighter performance target belongs to the
follow-up task once the diff cache and spawn gate land.

A client can abandon before receiving even a `busy` response. Therefore
the 19 abandoned requests include 15 requests that did not start a diff;
the received shed count is not the server's total rejection count. Cleanup
checks every started diff completed, rather than assuming every abandoned
client acquired a worker. Both default and heavy limiters must release all
tokens after draining.

The initial after-lane trial exposed that accounting mistake: 9 abandoned,
31 received shed, and only 4 loaded diffs completed. That trial also measured
meta/repos at 0.381/0.402 s despite zero default-pool occupancy. It failed
the old completion assertion, not the read assertion. This is evidence that
the 300 ms threshold remains sensitive to host scheduling; the passing run
does not prove an unconditional latency guarantee. The original bound was
unchanged throughout those historical runs.

## Before: observations while scaling

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

Before the lane, `routes_read._json` dispatched session diffs through
Starlette's default AnyIO limiter, retaining a token even after the HTTP
client left. The same pool dispatches `/v1/repos`, which only reads workspace
rows here. After the lane, session diffs use `_heavy_json`: admission rejects
excess work immediately and admitted work uses a separate four-token limiter.
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

Follow up on the tight-latency target after the diff cache and spawn gate
land. Compare a captured incident history, record event-loop lag and OS
scheduling under load, and measure Unix authentication and action traffic
independently. No production mitigation is included in this branch.

## Re-run

```sh
uv run pytest tests/test_daemon_soak.py -q -m serial -k overload
```

Add `-s` to see the JSON measurements. This requires permission to bind a
loopback socket. Before the lane, the exact finish-line command completed
with `1 deselected, 1 xfailed in 34.08s` after the strict marker was added.
After the lane and removal of xfail, the historical 300 ms gate failed in
6.40 s as detailed above. The current gate uses the lane guarantees and
the supervisor-approved 2-second sanity bound.
The current gate passed with `-s`: `1 passed, 1 deselected in 5.63s`.
It accounted for 11 client abandonments, 29 received `busy` responses, and
zero successful poll responses; all four admitted diffs completed after
client abandonment. At the probe, default AnyIO was 0/40 with zero waiters,
heavy admission/workers were 4/4, and meta/repos took 255/284 ms. Every
limiter token was released after cleanup. The full suite is left to
brindle's merge gate.
After the session diff cache (`perf/session-diff-cache`), git processes per diff were [0, 49], diffs took 0.7–6.7 ms (so no client abandoned one and abandonment is no longer required), and meta/repos took 2.4/4.6 ms.

## Cheap-read bound tightened (`perf/cheap-read-latency`)

A first attempt probed `/v1/meta` and `/v1/repos` only after every poller
finished. With the cache warm, no heavy work was left by then, so those
2–9 ms numbers measured an idle daemon and are discarded. The test now
drops the cached diff (`session_review._forget()`) before polling, so the
first poll recomputes the full history (49 git processes, 1–2.5 s), and a
sampler reads `/v1/meta` and `/v1/repos` back to back (the awaits yield to
the loop) while the pollers run. The bound applies to every sample taken in
the polling window, including the cold first pair. Each request is also
marked busy if the heavy lane held a worker when it began or ended, and the
test separately fails if no request overlapped heavy work.

An intermediate version applied the bound only to requests marked busy
before they were sent, which excluded the cold first pair; it is replaced.
Its numbers (max 6–55 ms) are discarded. Runs of the final harness: every
sample in the window counts toward "max". Run 1 was the first at the 100 ms
bound; runs 2–6 are the five consecutive passes after the final edit:

| Run | Busy / all samples | Max over all samples ms | Recompute s |
| ---: | ---: | ---: | ---: |
| 1 | 3 / 4 | 77.7 | 1.06 |
| 2 | 35 / 36 | 37.5 | 1.00 |
| 3 | 17 / 18 | 25.4 | 1.06 |
| 4 | 5 / 6 | 67.3 | 0.92 |
| 5 | 15 / 16 | 45.8 | 0.95 |
| 6 | 23 / 24 | 34.6 | 1.08 |

The slowest samples (67–78 ms) are the cold first pair, issued as the
recompute starts. After-probe `/v1/meta` / `/v1/repos` were 1.4–1.9 / 2.7–5.3
ms. Default AnyIO peaked at 1 occupied of 40 with no waiters; the asyncio
executor no-op took 0.2–0.34 ms.

Cheap reads under a real recompute therefore peak at 25–78 ms, tracking host
load. The bound is 100 ms (originally 2.0 s); 77.7 ms is the worst seen, so
the margin is thin on a loaded host. The remaining delay
is consistent with the in-process recompute's CPU and GIL contention plus
host load; the harness shares one process with the clients and cannot
separate those, so this does not show that no daemon cause remains. Moving
the recompute out of process would be the next step if the bound must drop
further.
