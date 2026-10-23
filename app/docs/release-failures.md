# Desktop release failures, rc.1 to rc.8

Every release workflow run that didn't succeed, from the run's own logs
(`gh run view <id> --log-failed`, `gh api .../runs/<id>/attempts/<n>/jobs`).
Each one lists its cause, the log line that shows it, and the commit that
fixed it. These runs predate the app's move into this repo, so the commits
are from its earlier history; the workflow is now
`.github/workflows/app-release.yml`. "Intel" is the `macos (x86_64)` job on
`macos-15-intel`.

| Tag | Result | Job | Step | Cause | Fixed by |
|---|---|---|---|---|---|
| (pre-rc) | failure, no jobs | — | — | Workflow file rejected: actions not on the repo's allow-list (as rc.1) | 98dd06b |
| rc.1 | startup_failure | — | — | Actions not on the repo's allow-list | 98dd06b |
| rc.2 | failure | macos (both) | Build | No `uv` on the runners for build_engine.sh | cd68db8 |
| rc.3 | cancelled | macos (aarch64) | Build | Signing identity not in a keychain when build_engine.sh signs; the rest cancelled by hand | fe0b4c5 |
| rc.4 | failure | Intel | Gatekeeper | .app notarized but no ticket stapled | 0bcfbbf, 8212852 |
| rc.5 | failure | Intel | Gatekeeper | `stapler validate` lost Apple's connection (-1005, error 68) | aece976 |
| rc.6 | **success** | | | | |
| rc.7 attempt 1 | failure | Intel | Build | `codesign` of libpython failed; its error was hidden | acb2695, the rc.9 fixes |
| rc.7 attempt 1 | failure | windows | WSL client tests | pty_view's tests don't compile on Windows | 9ed9a81 |
| rc.7 attempt 2 (re-run) | startup_failure | — | — | GitHub created no jobs; no annotation says why | don't re-run tag runs |
| rc.8 | failure | Intel | Notarize and staple the DMG | `stapler staple` lost Apple's connection (-1005, error 68) | 9ed9a81, the rc.9 fixes |
| rc.8 | failure | windows | WSL client tests | Same pty_view compile errors as rc.7 | 9ed9a81 |

## Each run

### Pre-rc pushes and rc.1: actions the repo doesn't allow

rc.1 ended `startup_failure` with no jobs. The two earlier runs on branch
pushes ended `failure` with no jobs (GitHub records a failed run for a
workflow file it rejects). At those commits (e.g. f2a4deb, the rc.1 tag) the
workflow used `dtolnay/rust-toolchain`, `swatinem/rust-cache`,
`tauri-apps/tauri-action` and `aws-actions/configure-aws-credentials`. The
repo the app was released from then allowed only GitHub-owned actions and
`astral-sh/setup-uv@*`.

**Fixed by** 98dd06b (rc.2): rustup, actions/cache, `npx tauri build` and an
OIDC/STS exchange in plain shell steps. Today's workflow uses only
`actions/*` and the pinned `astral-sh/setup-uv`.

### rc.2: no uv

Both Mac builds, step Build:

```
build_engine: needs uv (https://docs.astral.sh/uv) to build brindle's wheel
Error beforeBuildCommand `npm run build && bash scripts/build_engine.sh` failed with exit code 1
```

**Fixed by** cd68db8 (rc.3): `astral-sh/setup-uv` in the macos job.

### rc.3: the signing identity wasn't in a keychain yet

macos (aarch64), step Build:

```
build_engine: codesign failed for .../target/engine-build/stage/python/lib/libpython3.13.dylib
```

build_engine.sh signs the engine in Tauri's beforeBuildCommand, before Tauri
imports `APPLE_CERTIFICATE` itself, so codesign found no identity. The
workflow at cd68db8 has no keychain step. The other jobs were cancelled by
hand at 16:40, just before rc.4 was pushed.

**Fixed by** fe0b4c5 (rc.4): the "Signing keychain" step imports the identity
into a throwaway keychain first.

### rc.4: the .app had no ticket

Intel, step Gatekeeper:

```
brindle.app does not have a ticket stapled to it.
##[error]Process completed with exit code 65.
```

Tauri staples the .app right after notarization. Apple's ticket wasn't
downloadable yet, and Tauri carried on without one.

**Fixed by** 0bcfbbf and 8212852 (rc.5): the "Staple the app" step retries
until `stapler validate` passes (10 tries, 30 s apart).

### rc.5: Apple dropped the connection during `stapler validate`

Intel, step Gatekeeper (the app's staple step had succeeded: "ticket
stapled (attempt 2)"):

```
error is Error Domain=NSURLErrorDomain Code=-1005 "The network connection was lost." ...
NSErrorFailingURLStringKey=https://api.apple-cloudkit.com/database/1/com.apple.gk.ticket-delivery/production/public/records/lookup
The validate action failed! Error 68.
```

**Fixed by** aece976 (rc.6): Gatekeeper's `stapler validate` is retried 5 times.

### rc.7, attempt 1: Intel codesign, and Windows tests that don't compile

Intel, step Build:

```
build_engine: codesign failed for .../stage/python/lib/libpython3.13.dylib
```

build_engine.sh threw away codesign's own message, so the cause isn't in the
log. The keychain step had passed and the same identity signed rc.6 and
rc.8. libpython is the first file signed, and `--timestamp` asks Apple's
timestamp service. So the likely cause is that service failing for a moment.

**Fixed by** acb2695 (retry three times, print codesign's error). The rc.9
fixes go further: five tries over about two minutes, each failure printed
(`build_engine: codesign (try N of 5): ...`). The whole Tauri build, whose own
codesign `--timestamp` and notarytool calls go to Apple, is now tried twice.

windows, step WSL client tests (`cargo test --lib wsl::` compiles every
test in the crate):

```
error[E0425]: cannot find function `spawn_terminal_sized` in module `crate::setup`
    --> src\pty_view.rs:1342:94
note: found an item that was configured out ... #[cfg(unix)]
error[E0425]: cannot find function `socket_of` in this scope
    --> src\pty_view.rs:1354:42
```

pty_view's test `Server` (and its `Drop`) used unix-only helpers without
being `#[cfg(unix)]` like the tests that use it.

**Fixed by** 9ed9a81: `#[cfg(unix)]` on the test `Server`. I checked
every other test module the Windows build compiles (daemon, engine, setup,
wsl, lib): their ungated tests call only ungated code, and libc (a
`cfg(unix)` dependency) appears only inside `#[cfg(unix)]` items.

### rc.7, attempt 2 (re-run): startup_failure, no jobs

The re-run started 14:57:32 and ended `startup_failure` at 15:00:52. GitHub
created no jobs and no check runs for it (`.../attempts/2/jobs` reports
`total_count: 0`, and the check suite has 0 check runs), and the REST API
shows no annotation. It wasn't the workflow file: attempt 1 ran that same
file (32a6cec) and started six jobs, and the environment's tag policy
admits the tag. rc.8 ran normally 16 minutes later.

**Fixed by**: the workflow's header now says not to re-run a tag's run: push
the next release candidate's tag instead. A re-run would also publish its
Linux bundles again.

### rc.8: Apple dropped the connection during the DMG's staple, and Windows tests again

Intel, step "Notarize and staple the DMG" (notarization itself was Accepted):

```
error is Error Domain=NSURLErrorDomain Code=-1005 "The network connection was lost." ...
NSErrorFailingURLStringKey=https://api.apple-cloudkit.com/database/1/com.apple.gk.ticket-delivery/production/public/records/lookup
The staple and validate action failed! Error 68.
##[error]Process completed with exit code 68.
```

windows: the same two pty_view errors as rc.7 (rc.8 was tagged before the
fix).

**Fixed by** 9ed9a81 (the DMG staple retried 6 times; pty_view's test Server
`cfg(unix)`). The rc.9 fixes moved the step into
`app/scripts/notarize_dmg.sh`, which retries each call to Apple
separately: the upload until it gets a submission id, `notarytool wait`
until Apple decides, and the staple until it validates. Before, a dropped
connection during `submit --wait` failed the step outright. Only Apple's own
`Invalid`/`Rejected` fails at once, with Apple's log.

Note: rc.8's Linux job published its bundles to `rc/0.1.0-rc.8/` even though
the Macs failed. The Linux job doesn't wait for macOS.

## What retries now

- `app/scripts/retry.sh` wraps every step that downloads: rustup, `npm ci`,
  apt-get, brew, `uv tool install`, and the Tauri builds (macOS, Windows,
  Linux via smoke_bundle.sh), which fetch their bundlers' tools and on macOS
  sign and notarize with Apple.
- `app/scripts/notarize_dmg.sh`: notarytool submit, wait and the DMG's staple.
- The .app's staple (10 tries), Gatekeeper's `stapler validate` (5) and
  `spctl` (3) retry in place.
- build_engine.sh: `curl --retry 5 --retry-all-errors` for its pinned
  downloads (plain `--retry` doesn't retry a dropped connection), and
  timestamped codesign 5 times.
- The AWS steps: `curl --retry 5 --retry-all-errors` for the OIDC token, and
  `AWS_RETRY_MODE=adaptive`, `AWS_MAX_ATTEMPTS=8` for sts and s3.

`app/scripts/publish_gate.test.ts` checks all of these: retry.sh's
behaviour, notarize_dmg.sh against a fake `xcrun` that drops connections, and
that each such command in the workflow goes through a retry.

A dispatch on a branch can't exercise the Apple steps: it runs outside the
`desktop-release` environment, which holds every Apple, Tauri and AWS secret,
so it builds unsigned and publishes nothing.

## Dry run

A `workflow_dispatch` of the rc.9 fixes (5be75a2): macos (aarch64) 4m26s,
macos (x86_64) 22m33s, linux (build and smoke) 12m4s, windows 24m35s (WSL
client tests: `test result: ok. 6 passed; 0 failed`), macos-publish 13s
(gate: "not a pushed tag", nothing published), nightly skipped. Green.
