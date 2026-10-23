# The bundled engine

brindle.app for macOS carries its own engine and runs only that one: nobody
installs brindle, Python or tmux to use the app, and an app update is an engine
update. The pieces:

| Where | What |
|---|---|
| `app/scripts/build_engine.sh` | builds the engine into `app/src-tauri/engine/` (git-ignored) |
| `app/scripts/engine/brindle` | the launcher the engine runs brindle through |
| `app/scripts/engine/engine.entitlements` | the hardened runtime's exceptions for the engine's python3 |
| `app/src-tauri/tauri.macos.conf.json` | runs build_engine.sh before every macOS `tauri build`; bundles the engine as `Contents/Resources/engine` |
| `app/src-tauri/src/engine.rs` | finds the engine; `install_cli` |
| `app/src-tauri/src/daemon.rs` | `start()` launches the engine's `brindle daemon --idle-exit` |
| `app/scripts/smoke_bundle.sh --engine` | builds the app and runs its engine from inside the .app under `env -i` |

## What's in it

```
Contents/Resources/engine/
  bin/brindle    the launcher: python/bin/python3 -I -B -X brindle-build=<BUILD> -m brindle
  bin/tmux       tmux 3.8, libevent 2.1 and utf8proc linked in statically
  python/        python-build-standalone CPython 3.13 (install_only_stripped),
                 brindle and its uv.lock-pinned dependencies in site-packages
  .stamp         the build's inputs; a build with the same ones does nothing
  BUILD          the build's id, a hash of .stamp (see "Which running daemon it uses")
```

**Python, not a frozen binary.** python-build-standalone plus a wheel, rather
than PyInstaller. brindle re-enters itself as `[sys.executable, "-P", "-m",
"brindle"]` (`providers.brindle_invocation`): agents' MCP servers, hooks, a
re-exec'd sidebar. Those commands are written into agents' configs. With a real
interpreter they work as they do in a uv install. A PyInstaller onedir has no
`-m`, so each one would need special-casing. Those configs name the engine's
python3 by its path in the app (`/Applications/brindle.app/...`): an update
keeps them working, but moving the app elsewhere breaks the ones already written.
Those re-entries run without the launcher's `-I`, so a `PYTHONHOME` or
`PYTHONPATH` in an agent's environment would reach them (as with any uv
install). They write no bytecode either: every module already has an
unchecked-hash `.pyc`, which Python uses as is.

**Where brindle comes from.** This same checkout: the engine at the repo's
root (`BRINDLE_SRC` overrides it) at `HEAD` (`BRINDLE_REF`), so uncommitted
engine changes aren't in it. Only the engine's own paths (`src`,
`pyproject.toml`, `uv.lock`, `README.md`, `LICENSE`, `SCHEDULE-A`) are
exported with `git archive`, and the checkout is only read. The build is
keyed on those paths' trees, so a commit that only changes the app doesn't
rebuild the engine.
Its dependencies are installed from `uv export --frozen`, so they are the
versions and hashes its `uv.lock` pins. Python, tmux, libevent and utf8proc
downloads are pinned by sha256.

**The launcher** (`bin/brindle`) is a POSIX sh script. It finds the engine
through any symlink to itself (`install_cli` makes one) and runs the engine's
Python with:

- `-I`: no `PYTHON*` variables, no user site-packages, no cwd on `sys.path`. A
  person's own Python setup can't leak in.
- `-B`: it never writes `__pycache__` into the bundle, which would break the
  app's signature. All bytecode is compiled at build time with
  `--invalidation-mode unchecked-hash`, so it stays valid whatever mtimes the
  copy into the .app gives the sources.
- A PATH of the engine's `bin` first, then the caller's absolute directories
  (each once; with no PATH, sh makes one up that contains `.`, which is
  dropped), then `/usr/bin:/bin:/usr/sbin:/sbin` if they're missing. brindle
  needs `/usr/bin/security` (the Keychain, below) and `ps` (the daemon's
  caller check). Without them, under `env -i` or a launchd job, it would read
  as signed out of Pro and refuse every caller.

**Which engine runs** (`engine::brindle_to_start`): a release build on macOS
starts only the bundled engine, and an app missing its engine says so
instead of falling back. A dev build (`tauri dev`, `cargo test`), which has no
bundle around it, and the Linux build, which carries no engine yet, start
`$BRINDLE_BIN` or the first `brindle` on PATH, as before.

**Which running daemon it uses** (`daemon::ensure_in`, `daemon::reuse`).
Every brindle shares `~/.brindle/daemon.json` and its socket, so a daemon may
already be running when the app opens: an older app's, a uv install's, or the
one this engine ran before an update.

Before any daemon is looked for:
- a macOS release that finds no engine in its bundle fails with "missing its
  engine";
- one whose engine has no build id fails with "engine is incomplete".

Only a dev build (or Linux's), which may take brindle from PATH, uses whatever
daemon runs.

**Build identity.** build_engine.sh writes the build's id (a hash of its
`.stamp`: Python, tmux, brindle's commit, the build scripts, the signing
identity) to `engine/BUILD`. The launcher puts it on the command line of every
brindle it runs, `python3 -I -B -X brindle-build=<id> -m brindle …`. Python
keeps `-X` options in `sys._xoptions` and otherwise ignores them. So the
identity lives in the process itself, exactly as long as it does. A file's
time or a path can't vouch for it: an update can rename in a bundle staged
before the old daemon started, and the path stays the same.

The app reads the running daemon's command line with `ps`:

| The running daemon | What the app does |
|---|---|
| this engine's (the engine's `python/bin/python3`) with this build's `-X brindle-build=` | uses it |
| this engine's path with another build's id, or none (from before an update, however the update put the new files in place) | stops it with SIGTERM, waits up to 15 s for it to exit (it removes its files), starts the engine's own |
| another install's, started by an app (`--idle-exit`: an older app's uv brindle, or a copy run from a staging place) | the same: stops it, then starts its own |
| another install's, started by a person (`brindle daemon` in a terminal), or one whose command line `ps` can't give | leaves it running, and fails with `engine conflict: …`, naming it and how to stop it (`brindle daemon stop`) |

The same rule applies to a daemon that comes up while the app is starting its
own (one the app's own start spawned, by pid, is accepted). brindle's own
daemon also exits within 5 s once its install's files change, but requests
could reach it until then, so the app doesn't rely on that. Since all of them
use the same `~/.brindle`, the sign-in and its refresh lock stay shared
whichever daemon runs. The webview shows an `engine conflict` as a plain start
failure for now (api/engine.ts knows only `engine too old`).

**Minimum macOS: 11.0**, for the app as for its engine.
- `tauri.macos.conf.json` sets `minimumSystemVersion`, so macOS refuses to
  open the app on 10.13–10.15, where its engine couldn't run. Tauri's default
  was 10.13.
- The engine is built with `MACOSX_DEPLOYMENT_TARGET=11.0`, python-build-standalone's
  own floor.
- `smoke_bundle.sh --engine` checks that the app's `LSMinimumSystemVersion` is
  at least the newest `minos` of every Mach-O file in it.

**`install_cli`** (Tauri command; the Settings button comes separately) links
`~/.local/bin/brindle`, or `dir` if given, to the engine's launcher, so
`brindle` in a terminal is the app's. It refuses to:

- replace another `brindle` there (uv's, say) unless `replace`;
- replace a directory;
- link while the app runs from the .dmg or a translocated copy (that link
  would break).

The new link replaces an old one with a single rename. A known nit: brindle's
`--help` names itself `python -m brindle`, since click takes the name from
how it was started.

## Size

| | on disk | download |
|---|---|---|
| brindle.app, with the engine | 135 MB | 46.7 MB (the .dmg, UDZO) |
| the app's own executable | 15.8 MB | about 5 MB (zipped alone) |
| the engine | 120 MB | about 41 MB (the difference) |
| of which python/ (site-packages: 62 MB) | 118 MB | |
| of which bin/tmux | 1.7 MB | |

So the engine adds about 120 MB installed and about 41 MB to the download,
measured on arm64 (one architecture: see below). An image of the engine on
its own comes out at 49.7 MB, image overhead included, so take 41 MB as
approximate. The 62 MB of site-packages is
mostly cryptography, pydantic-core and their bytecode. The build already drops
what the engine never uses: Tk, IDLE, ensurepip and pip, headers, man pages.

**One architecture per build.** build_engine.sh builds for the Mac it runs on
and refuses a different `TAURI_ENV_TARGET_TRIPLE`. A universal app would need
two engines, built on (or cross-built for) each architecture and joined with
`lipo` file by file, which would double the size. So the release ships separate
arm64 and x86_64 downloads: `app-release.yml` builds `aarch64-apple-darwin`
on an Apple Silicon runner and `x86_64-apple-darwin` on an Intel one, and
`latest.json` points each architecture at its own updater bundle (see the
README's "Releases").

## Code signing and notarization

Tauri signs the app's own executable (with `APPLE_SIGNING_IDENTITY`, hardened
runtime on) and notarizes and staples the .app. It signs the DMG but does not
notarize it, and Gatekeeper rejects an un-notarized DMG ("Unnotarized
Developer ID") even with a notarized app inside, so the release workflow
submits the DMG to `notarytool` and staples it separately, then checks both
with `spctl` before publishing (the README's "Releases"). It does not sign Mach-O files in
`Contents/Resources`, and notarization rejects a bundle with any unsigned or
non-hardened Mach-O inside. So build_engine.sh signs the engine itself
whenever `APPLE_SIGNING_IDENTITY` is set, before Tauri copies it into the app
and seals the bundle's resources:

1. every `.so` and `.dylib` (extension modules, libpython's), with
   `--options runtime --timestamp` and no entitlements;
2. `python/bin/python3.13` and `bin/tmux`, the same, plus `engine.entitlements`.

The launcher is a shell script and needs no signature of its own: the app's
seal covers it as a resource.

**Without a Developer ID the app is signed ad hoc.** tauri.macos.conf.json sets
`signingIdentity` to `-`, which Tauri uses only when `APPLE_SIGNING_IDENTITY`
isn't set (the variable wins, so a release still signs with its Developer ID).
With no identity at all Tauri doesn't sign the bundle: its executable keeps
just the arm64 linker's ad-hoc signature, there is no `_CodeSignature`, and
`codesign --verify --deep --strict` fails with "code has no resources but
signature indicates they must be present", so macOS calls the app damaged.
Every plain `tauri build` hit this, the engine's "up to date" path or not.
smoke_bundle.sh runs that verify on the .app and on the .app in the .dmg.
The ad-hoc identity isn't passed to build_engine.sh, so the engine's own files
stay as they are; the app's seal covers them as resources.

**Entitlements for the embedded Python** (`engine.entitlements`):

- `com.apple.security.cs.allow-unsigned-executable-memory`: libffi, under
  ctypes (truststore calls Security.framework through it) and cffi
  (cryptography), may write callback trampolines into memory and then execute
  them. On arm64 it uses static trampolines and doesn't need this: an ad-hoc
  hardened build without it loaded every module and ran a ctypes callback and
  truststore. It's kept for x86_64, which wasn't tested here.
- *Not* `disable-library-validation`. Library validation lets a process load
  only libraries signed by Apple or by its own team. Every module in the engine
  is signed with the app's Developer ID, so it passes, and the engine can't
  load a module from anywhere else.
- *Not* `allow-jit` or `allow-dyld-environment-variables`: nothing in the
  engine asked for them in the arm64 check above. Add one only when a failing
  signed build shows it's needed.

**Trying it without a Developer ID:** `APPLE_SIGNING_IDENTITY=- bash
app/scripts/build_engine.sh --force` signs ad hoc with the hardened
runtime. An ad-hoc signature has no team, so library validation would refuse
every module ("mapping process and mapped file (non-platform) have different
Team IDs"); this mode alone adds `disable-library-validation`. The signing
identity is part of `.stamp`, so the next unsigned build replaces it. Not
checked here: a real Developer ID signature and a `notarytool` submission
(no identity on this machine). Run both before the first signed release, and
check `codesign --verify --deep --strict` on the built .app.

**Gatekeeper and the CLI link.** Notarization and the stapled ticket cover the
whole bundle, the engine included. A quarantined download runs from a
translocated copy until it's moved, which is why `install_cli` refuses then.

## Keychain continuity

brindle keeps its Pro sign-in in the login Keychain
(`src/brindle/pro/credentials.py`, backends in `src/brindle/keystore.py`):
generic passwords under service `brindle-pro`. Account `default` holds the
entitlement and refresh token, and other accounts hold per-install keys. Model
API keys live under `brindle-keys`. brindle writes and reads them only by
running `/usr/bin/security` (`add-generic-password` on stdin through `security
-i`, `find-generic-password -w`). It never calls Security.framework itself.

**What decides who may read an item.** An item `security` creates has an
access list that trusts one application: `/usr/bin/security`, by its
designated requirement (`identifier "com.apple.security" and anchor apple`).
Checked by creating an item the same way in a scratch keychain and dumping its
ACL (`security dump-keychain -a`). So it doesn't matter which brindle, which
Python or which app version runs `security`: a `uv tool install --reinstall`,
an app update or an ad-hoc dev build all read the item without a prompt.
Neither the reinstall nor the app's signature can change access to it.

**The sign-out after `uv tool install --reinstall`: cause unconfirmed.** It
didn't come back: a later `uv tool install --force --reinstall` left Pro signed
in. What is verified, and what is only a hypothesis:

- *Verified, by the ACL above:* a reinstall can't make the item unreadable.
- *Verified, and fixed:* a brindle that can't find `/usr/bin/security` on PATH
  uses the file store and reads as signed out (`keystore.backend_name()`). The
  WIP launcher did this under `env -i`. A uv install is exposed the same way
  only when started with a PATH lacking `/usr/bin`. Nothing shows that
  happened that day.
- *Observed, attributes only* (`security find-generic-password -s brindle-pro
  -a default`, no `-w`/`-g`): the item now there was created at 2026-10-21
  01:16:40Z. This only says an item was saved then, by a new login. It
  doesn't say when or why an earlier one went, or whether one went at all.
- *Hypotheses, none confirmed:* brindle deletes the item in three places:
  - `brindle account logout`;
  - a login whose first entitlement fetch fails;
  - a refresh the backend answers `invalid_grant` / `reauth_required`
    (`auth._rotate`), for instance a session that expired or was revoked.

  The sign-out could also have been the file-store fallback above, or a
  refresh that failed offline past the entitlement's grace, with the item
  never touched.

One hypothesis is worth guarding against whatever happened that day, because
the app makes it more likely. Rotations are serialised by
`$BRINDLE_HOME/pro/refresh.lock`, but the Keychain item is per user, not per
`BRINDLE_HOME`. Two brindles with different homes (a test run with its own
`BRINDLE_HOME`, a second install configured differently) can each spend the
same refresh token. The second gets `invalid_grant` and deletes the item, even
though the first just saved a good one.

- Upstream fix (brindle, not this repo): a refresh lock that doesn't depend on
  `BRINDLE_HOME` when the backend is the Keychain, or a re-read before
  deleting on `invalid_grant`.
- Here: the smoke test runs every daemon with `BRINDLE_PRO_CREDENTIAL_STORE=file`
  in its own home, so it never reads or rotates the owner's sign-in.

Non-secret evidence that would settle the cause, if it happens again:

- **What `brindle account` printed** at the time: "not logged in" means no
  credentials were found; a renewal or air-gap message means they were found.
- **Whether the item existed then:** the exit status of `security
  find-generic-password -s brindle-pro -a default` (attributes only), and its
  `cdat`/`mdat` before and after.
- **The engine's log lines:** `license.current` logs "entitlement refresh
  failed (<code>)" at info level; `invalid_grant` there points at the backend.
- **The backend's view:** its record of refresh-token reuse or revocation for
  that session.
- **The environment of the brindle that read signed out:** its PATH, and
  whether `BRINDLE_PRO_CREDENTIAL_STORE` or `BRINDLE_HOME` was set.

**What keeps the bundle's access stable across app updates:**

- brindle reaches the Keychain only through `/usr/bin/security`, an Apple
  binary that the items' ACL names and no app update changes. Keep it that
  way. If the engine (or the Rust app) ever used Security.framework directly,
  new items would trust the calling binary instead: by cdhash for an ad-hoc or
  unsigned build, which changes with every build, so every update would prompt
  or fail. Even signed, continuity would then hang on the team ID and on
  identifier `com.pawdelta.brindle` never changing.
- The launcher always puts `/usr/bin` on PATH. Without `security` on PATH,
  `keystore.backend_name()` picks the file store, which has no sign-in, so a
  daemon started with a bare environment read as signed out. The WIP launcher
  had that bug. `engine::tests::the_launcher_runs_the_engines_python_with_its_bin_first_and_macos_own_dirs`
  covers it.
- The app's engine uses the same `BRINDLE_HOME` (`~/.brindle`, unless the
  person sets one) as a uv install. It therefore shares `refresh.lock` with any
  brindle the person also runs from a terminal.
- Nothing in the Keychain names the app's path or version, so replacing the
  bundle on update leaves the items as they were. The service and account
  names are brindle's own constants and must not change.

## Checking it

```
bash app/scripts/build_engine.sh            # the engine alone (cached; --force rebuilds)
bash app/scripts/smoke_bundle.sh --engine   # build the app, then check its engine
cargo test --manifest-path app/src-tauri/Cargo.toml
```

`smoke_bundle.sh --engine` checks:

- the engine's files, and that tmux runs under `env -i` and links only macOS's
  libraries;
- that the app's declared minimum macOS covers every binary in it;
- that its `bin/brindle daemon --idle-exit`, run from inside the .app under
  `env -i`, listens and answers `GET /v1/sessions` with 200 over its Unix
  socket;
- that the release app under `env -i` (`--headless-check`) starts that same
  engine's daemon and gets `GET /v1/meta` answered.

Each check runs with a fresh `HOME`, `BRINDLE_HOME` and `TMUX_TMPDIR` and a
file credential store. Those homes contain no agents, so this works from inside
a brindle agent too. The daemons are stopped and the homes removed on exit.
