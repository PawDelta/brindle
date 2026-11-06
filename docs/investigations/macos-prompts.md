# macOS system prompts on a new user's first launch and first session

Reported: new users see many macOS popups when opening brindle (keychain,
"something about Chrome", others). This lists every system prompt found, what
triggers it, who macOS blames, and how a well-behaved Mac app avoids it.

Date: 2026-11-05, against the app at 0.6.1 (installed) and this branch,
Claude Code 2.1.318, on the owner's Mac.

**How this was checked, and its limit.** Nothing here was observed on a fresh
account, and the app was never launched. Evidence is brindle's code, the
installed bundle (`codesign`, `spctl`), the CLIs' flags and the strings in their
binaries (minified code read by grep), man pages, and read-only `launchctl`.
No Chrome profile, keychain item or TCC database was read or changed. "Code"
below means the mechanism is read from code; **whether macOS actually shows the
dialog is a separate claim, and is marked unverified unless stated**. See
[What needs a fresh macOS account](#what-needs-a-fresh-macos-account).

## The short answer

- **Chrome:** the trigger is Claude Code, not brindle. On a config with no
  Chrome choice yet, a subscriber's interactive `claude` lists the profile
  folders of seven browsers at every start. brindle's isolated config home
  always starts with no choice. Fixed: panes start with `--no-chrome` until the
  person opts in.
- **The biggest issue was identity.** Everything agents do was attributed
  to the engine's `python3`, not to brindle.app and not to the Terminal a CLI
  user already granted. It has no bundle, so it can't carry a name or usage
  strings. **Built since** (decision 2): the daemon's job now runs the engine
  through a helper app named brindle. See
  [The engine's helper app](#the-engines-helper-app). The table below still
  says "python3" where it describes what was found.
- **Keychain:** brindle's own items should not prompt. Most keychain popups are
  more likely git and `gh`, which `fix/background-git-no-keychain-prompt` owns.
- **Background items:** one new launchd label per app process. The repeat notice
  is not fixed here, for a recorded reason. See
  [Login item](#login-item-smappservice-compared-with-daemonrs).

## Every prompt

"Python3" below means `Contents/Resources/engine/python/bin/python3`: identifier
`python3`, team `LWH38TMT84`, no Info.plist.

| Prompt | Trigger | Who attributes it | How a well-behaved Mac app avoids it; can brindle? | Fixed or why kept | How verified |
|---|---|---|---|---|---|
| "brindle is an app downloaded from the internet" (first open) | Gatekeeper, quarantine flag on first open | macOS, names brindle | Nobody can: notarizing only removes the scarier wording. brindle already does | Kept, unavoidable. No warning needed: everyone knows this dialog | Live: `spctl` says accepted, Notarized Developer ID; ticket stapled. Dialog itself: unverified |
| "Background Items Added" / "can run in the background" | `launchctl bootstrap` of the daemon's plist (`daemon.rs` `start_daemon`) | macOS Login Items (BTM), by the job's program. That was the engine's unsigned launcher script, which has no Team ID | One fixed label via `SMAppService`; on a legacy plist, `AssociatedBundleIdentifiers` with a program of the app's team. Partly | **Partly fixed:** the plist names `com.pawdelta.brindle`, as `launchd.plist(5)` asks, and the job now runs the engine's signed Python directly (`start_command`), the same process the launcher would make. Apple DTS says a Mach-O program is what makes that key work ([forum thread](https://developer.apple.com/forums/thread/755904)). So the item should list under brindle. Repeats kept, see below | Code; test against the real launcher script; team match live (`codesign`); launchd accepts the key (live, test label). macOS logged nothing about it either way, so the listing and notice wording are unverified |
| Same notice, repeated | A new label per app process that has to start a daemon (`launchd_label`: `<hash>.<app pid>`). Live: six such jobs loaded here, five idle | Same | One fixed label. Yes, with a relaunch test | **Labels kept**, owner's decision below. On start, background cleanup boots out older idle jobs with the same home hash, re-reading each immediately before bootout and keeping any with a pid. Failures are logged and ignored without holding up launch. Whether BTM notifies per label is unverified | Pile-up live (`launchctl list`); `idle_predecessors` tests cleanup, running and other-home jobs, a job starting between reads, and a failed bootout. Per-label notice: unverified |
| "Chrome": a privacy prompt about another app's data, or a browser prompt | `claude` at startup lists `Default` and `Profile *` and checks `Extensions/<id>` under Chrome, Brave, Arc, Edge, Chromium, Vivaldi and Opera's folders, when the `claudeInChromeDefaultEnabled` setting is unset, the session is interactive and the login is a subscription | The engine's python3, as responsible process (panes descend from it) | Don't touch other apps' folders until asked. Yes | **Fixed:** `--no-chrome` unless the person chose it (`providers.py` `claude_chrome_chosen`), judged from the config folder the pane really runs with: a profile's, unless an enforced identity drops it. `/chrome` still works and takes effect on the next launch | Code (Claude's `kno`, `Zrs`, `dM`); test. That this scan is what raises the reported dialog: **unverified**, and Chrome's folder isn't an app container, so it's the best candidate rather than proven |
| Browser tab `clau.de/chrome/reconnect`; files written to seven browsers' `NativeMessagingHosts` | Claude in Chrome turned on (`mM`): manifests installed, tab opened on first install when the extension is present | Claude Code | Only when asked. Now the case | Kept: only after the person opts in | Code |
| "would like to access data from other apps" | Anything reading `~/Library/Containers` or `Group Containers`. brindle's code reads neither (grep); Codex and agy's binaries name neither; Claude Code carries a macOS guard list naming them | Engine's python3, for an agent's own command | Don't scan `~` or `~/Library`. brindle doesn't; an agent told to can | Kept: only when an agent's own work reaches there | Code. Dialog: unverified |
| "would like to access files in your Documents / Desktop / Downloads folder" | Daemon's or an agent's first touch of a project there. The folder picker's choice doesn't carry over: it's the app's, and the daemon is another identity | Engine's python3; brindle.app for the app's own reads (`open_local_path`) | Do the work inside the app's identity, with a usage string, and ask when the person acts. Needs the helper bundle | **Kept, warned:** one line on onboarding's project step. Usage strings added for the app's own prompts (`app/src-tauri/Info.plist`). brindle clones into `~/Projects` by default, which is unprotected, so this only fires for an existing project there | Code; that the prompt names `python3` and that it asks once per folder: unverified |
| Removable and network volume prompts; "files in <cloud provider>" | Same, for a project on such a volume or under a cloud-storage folder (File Provider). brindle only string-compares `/Volumes` (`engine.rs`) | Engine's python3 | As above | Kept: only for a project there. Strings added for the app | Code. Dialog: unverified |
| "The git command requires the command line developer tools" | Running `/usr/bin/git`, a shim, on a Mac without them. brindle bundles no git, and nothing checks first (`grep`, `setup.rs`); first use comes at Pick a project | macOS, naming git | Bundle git, or check `xcode-select -p` first and explain. Can do either; see owner decisions | **Fixed** (`fix/git-tools-check-first`): `spawn.ensure_git()` runs `xcode-select -p` (silent) once per process before any `git` start, read or action (`spawn.start/read/action`), only when `git` on PATH is `/usr/bin/git`; if missing it raises `spawn.ToolsMissing`, which `git.py` types as `GitToolsMissing` and the API maps to `developer_tools_missing`. The CLI, `brindle doctor` and the app's project step show it; the app offers an Install button that runs `xcode-select --install` only after an explicit click, plus Try again. Homebrew git skips the check | Code. Dialog from general knowledge of the shim: unverified on a fresh account |
| Notifications | First `sendNotification`: first new inbox item after the first snapshot (`notify.ts`), or an update while the window is hidden (`updates.rs`) | brindle.app | Ask when there's something to say. Already so | **Kept, no change.** The ask in `notify.ts:75` never reaches macOS: `tauri-plugin-notification` answers Granted on desktop and delivers with `NSUserNotification`. Can't fire during onboarding: the inbox is empty until a session runs, and the first snapshot is silent | Code (plugin source). Whether macOS shows anything on first delivery: unverified |
| Keychain: brindle's own items (`brindle-pro`, `brindle-keys`) | `keystore.py`, through `/usr/bin/security`. Many reads at startup for missing items | `security`, if ever | An item's creator is trusted on its ACL. Already so: `security` creates and reads | Kept: no prompt expected. Missing item: silent exit 44, no dialog (live, on a made-up name). `security(1)`: "the application which creates an item is trusted" | Live for a missing item; man page for ACL. Real item on a fresh account: unverified |
| Keychain: Claude Code's sign-in | `claude` via `security` (`find-generic-password` in the binary) | `security` | As above | Kept: not expected to prompt | Code |
| Keychain: Antigravity's sign-in | `agy` via go-keyring (`macOSXKeychain`, `find-generic-password`) | `security`, by go-keyring's design | As above | Kept: not expected to prompt | Code; go-keyring's use of `security` from memory of its source |
| Keychain: Codex | `codex` links `SecItemCopyMatching` directly (`keyring`); `cli_auth_credentials_store` picks file, keyring or auto. Default not documented; brindle's comment says on macOS they're in the keychain | `codex`, team `2DC432GLL2`, so a stable identity | Sign with one Team ID: approvals last. Codex does | Kept: its own identity and item. brindle could force `file` but that lowers the protection on a sign-in, so not done | Code; signature live. Whether it prompts: unverified |
| Keychain: git credentials, `gh` | `git-credential-osxkeychain`, `gh`, from background git and agents | The helper / `gh` | | **Not this branch:** `fix/background-git-no-keychain-prompt`. The most likely source of the keychain popups reported | Not examined, by brief |
| "wants to control <app>" (Automation) | brindle sends no Apple Events (grep). Claude Code runs `osascript` for Terminal or iTerm only in its own setup and notification paths, and for Finder/System Events in its copy of `open` only with `app: "browser"` | The caller | Avoid AppleScript. Mostly so | Kept: not expected on a first session. `mac-notification-sys` has an AppleScript fallback that's skipped once the plugin has set the app | Code. Unverified |
| Local Network; "accept incoming connections" | Daemon: Unix socket and 127.0.0.1 (`lifecycle.py`); OAuth listener 127.0.0.1 (`oauth.rs`). Neither prompts. An agent's command reaching a LAN host could | Engine's python3 | Bind loopback. Already so | Kept | Code |
| Microphone | Voice in a CLI (Codex's voice host, `agy mic-serve`, Claude's voice), only on request | Engine's python3, which has no usage string: macOS may refuse or kill rather than ask | A usage string in the responsible bundle. Needs the helper bundle | Kept: not on a first session | Unverified |
| Clipboard ("paste from another app") | App reads the clipboard on a pane's paste (`pty_view_paste`), through its own plugin | brindle.app | Read only on a user's paste. Already so | Kept | Code. Whether this macOS version asks: unverified |
| "would like to update or delete other apps" (App Management) | Installing an update over `/Applications/brindle.app`. Same team replacing itself shouldn't ask. CLI installers touch no bundles and use no `sudo` (`providers_install.py`) | brindle.app | Same Team ID. Already so | Kept | Identity live; dialog unverified |
| Full Disk, Screen Recording, Accessibility, Input Monitoring | Nothing in brindle asks (grep). Claude's computer use is opt-in | n/a | | Kept: not a first-session prompt | Code |

Browser tabs for sign-in aren't system prompts. They're opened with `open <url>`
through LaunchServices (`lib.rs` `open_url`), with no Apple Events and no
access to the browser's data, so they aren't the Chrome prompt.

## How other Mac apps avoid these, and what applies here

### Responsible process

macOS attributes a process's protected access to its *responsible process*. A
CLI run from Terminal is attributed to Terminal, which usually holds the grants
already. Apps started by Finder or `open` are their own
([Qt on the responsible process](https://www.qt.io/blog/the-curious-case-of-the-responsible-process)).

Under brindle, launchd starts the engine's python3, and tmux and every pane
descend from it. So the same `claude` that was silent in
Terminal asks again, as `python3`. (`launchctl procinfo` needs root, so this is
from the published mechanism, not measured.)

**Can tmux, launchd or how panes are spawned change that, without tricks? No.**

- Handing panes to Terminal would need Terminal open and scripted, which is the
  trick, and a new Automation prompt.
- A child of the app *would* be attributed to brindle.app, but that's what
  `daemon.rs` moved away from: it put tmux and every agent in the app's
  coalition, and quitting the old Dock icon killed them (commit `2f22e895`).
- `responsibility_spawnattrs_setdisclaim` gives a child its own attribution. On
  panes that means more identities and more prompts, and that same commit
  measured that it leaves the coalition unchanged. Not useful.

What can change is **what the responsible process is**: a helper bundle rather
than a bare `python3`. See below.

### Stable identity

Good news, checked live:

- App, `python3` and `tmux` are all signed by team `LWH38TMT84`, hardened.
- `python3`'s designated requirement is `identifier python3` plus that team, with
  no hash and no version. Grants tied to it should survive updates (unverified
  across a real update).
- brindle installs the engine at one path inside the bundle. Claude Code's
  versioned path isn't a problem here because panes aren't the responsible process.

The weakness isn't stability but **presentation**: the identity is called
`python3`. The launcher `engine/bin/brindle` is an unsigned script, which is why
the daemon's job now starts python3 directly. CLI use still goes through the
launcher, attributed to the terminal.

### Login item: `SMAppService` compared with `daemon.rs`

| | Today: `launchctl bootstrap`, per-pid label | One fixed label, same mechanism | `SMAppService` |
|---|---|---|---|
| Notice repeats | Each new label, if BTM keys by label (unverified) | Once | Once |
| Person's "off" switch | `launchd_disabled` looks up this process's new label, so an earlier switch-off is likely not seen; the job loads again or fails with launchd's I/O error (from code, unverified) | Works as written | System-managed |
| Listed as | brindle, with this branch's key (unverified) | Same | The app, by design |
| Size | | About a day; see below | Several days: a helper in the bundle, a Rust bridge, a static plist. Today's plist carries the app's environment and PATH at each start, which a signed plist can't, so that moves to a file. Requires macOS 13; the app supports 11 |

### Usage strings

The bundle declared none. Added for Desktop, Documents, Downloads, removable and
network volumes, merged by the Tauri CLI from `app/src-tauri/Info.plist`
([Tauri's docs](https://v2.tauri.app/distribute/macos-application-bundle/)). No
bundle was built here, so check them in the next build's `Contents/Info.plist`.

**They only help prompts attributed to brindle.app**, which is the minority. The
engine's prompts stay generic because its `python3` carries no Info.plist
(`codesign`: not bound). A bare executable can embed one: Claude Code's binary
does (`codesign`: 9 entries). So the strings become the real fix with decision 2
or 3 below.

### Keychain: an item with the app's signed identity on its ACL

Could brindle use this? Yes, but it wouldn't remove a prompt, as `security` is
already creator and reader. What it would buy is security: today any process
that can run `security find-generic-password -s brindle-keys` reads a key without
a dialog, and only brindle's permission rules (`permissions.py`) stand in the
way for agents. With Security.framework from the signed engine, an agent's
`security` call would hit a dialog. Cost: a native binding, migrating existing
items, and the CLI, daemon and hooks all sharing one identity. Worth doing for
that reason, separately from this work.

## The engine's helper app

Decision 2 below, built on `feat/engine-named-helper-app` (how it is made:
`app/docs/bundled-engine.md`, "The helper app"). The daemon's launchd job runs
`Contents/Library/LoginItems/brindle.app`, an app bundle of its own
(`com.pawdelta.brindle.engine`, named brindle, with usage strings) that runs
the engine's Python in its own process. The daemon is then the responsible
process as that bundle, for itself, tmux and every pane.

**Why a nested app, there.** Apple's DTS on the forums: a launchd job needs an
app for TCC to show "a nice prompt"
([thread 697278](https://developer.apple.com/forums/thread/697278)); a helper
embedded in an app was attributed correctly on macOS 11.6
([thread 692758](https://developer.apple.com/forums/thread/692758)). Two
shapes work in those threads. A bare executable in the app's `Contents/MacOS`
is taken for the app itself: one identity, but it has worked "for years"
without being documented, and DTS was surprised it does
([thread 853725](https://developer.apple.com/forums/thread/853725)). A helper
with its own bundle is its own identity with its own name and strings, which
is how every app works, so that is what was built. `Library/LoginItems` is one
of the places a bundle's signature takes nested code, and where
`SMAppService` would want it later. `SMAppService` wasn't needed for
attribution.

**Checked here** (ad hoc signatures only, the app never launched):

- The helper, run outside the app with a temp `BRINDLE_HOME`, starts the
  daemon, which answers `GET /v1/meta`. `codesign -dvv +<pid>` on that running
  daemon says `Identifier=com.pawdelta.brindle.engine`, `Info.plist
  entries=21`; the engine's `python3` says `Info.plist=not bound`. `lsof`
  shows the engine's libpython loaded in the helper's process. The same with
  the hardened runtime on.
- `brindle daemon status` and `stop`, from the launcher's `python3`, find and
  stop it; the app's `reuse` uses it for its build and replaces another's
  (Rust test).

**When existing users are asked again.** The identity changes from `python3`
to the helper, so what was granted to `python3` no longer counts. Exactly:

1. The update's first launch replaces the old build's daemon, as every engine
   update does: the old daemon pauses its sessions, the new one (the helper)
   resumes them. No session is lost, and nothing is asked at that moment.
2. macOS asks the next time the engine or an agent touches something
   protected, once per kind: a project in Documents, Desktop or Downloads, on
   a removable or network drive or in a cloud storage folder; a device on the
   local network; another app's Automation; the microphone. Each asks once,
   naming brindle. Someone whose projects are all under `~/Projects` is asked
   nothing.
3. One exception: panes of a tmux server that outlived the old daemon keep
   that server's attribution until it exits. Normally there is none: the old
   daemon closes every window as it pauses, tmux exits with its last session,
   and the new daemon's first session starts a server under the helper. It
   survives only if the person runs their own sessions on the default tmux
   server, or a pause was cut short.
4. The old `python3` rows stay in System Settings and do nothing. The job's
   program changed too, so one more Background Items notice is likely.

**What a signed staff build must confirm** (none of it can be seen with an
ad hoc signature, or without launching the app):

1. Notarization accepts the bundle with the helper in it; `spctl` accepts the
   app; the release's Gatekeeper step passes its helper checks (team, bound
   Info.plist, and the Automation and microphone entitlements on the app's
   and the helper's signatures). Both architectures.
2. The app starts its daemon and reuses it on the next open, with no "another
   install" message. `ps` shows the daemon as
   `…/Contents/Library/LoginItems/brindle.app/Contents/MacOS/brindle -I -B -X
   brindle-build=… -m brindle daemon --idle-exit`.
3. **The dialog names brindle.** Open a project in `~/Documents` and start a
   session: the prompt says brindle, with the helper's reason under it, not
   python3. Note the exact wording.
4. **One grant covers later agents.** After allowing it: a second agent, a
   new session, another provider's CLI and a `brindle` hook in that folder
   ask nothing more. Then quit and reopen the app (daemon kept), and let the
   daemon exit idle and start again: still nothing.
5. **Automation and microphone consent.** The helper and the app are signed
   with `com.apple.security.automation.apple-events` and
   `com.apple.security.device.audio-input`; without them the hardened runtime
   refuses with no prompt. From an agent's pane, `osascript -e 'tell
   application "Finder" to get name of home'` raises "brindle wants to control
   Finder" with the helper's reason, and allowing it lets the command through.
   Turning on a CLI's voice input raises the microphone prompt naming brindle,
   and it then hears. Both again with Allow in the Background off (the app's
   strings and entitlements). An ad hoc build can't show either.
6. System Settings → Privacy & Security → Files and Folders shows one brindle
   row for the engine, with brindle's icon and name (not "brindle.app" twice
   in a confusing way, not a bare identifier). Login Items → Allow in the
   Background still lists brindle.
7. An update from a build without the helper: sessions come back after the
   restart, and the first protected access asks once more, as above. An update
   between two builds with the helper asks nothing.
8. With brindle switched off under Allow in the Background (the daemon is then
   the app's child), the same prompts name brindle through the app's own
   strings.
9. After the daemon that started tmux has exited and a new one runs (idle
   exit with a person's own tmux session keeping the server), a new pane's
   prompt still names brindle, or asks nothing. What macOS does when the
   responsible process is gone was not established here.
10. `open -a brindle` and Spotlight still open the app, not the helper (both
    are named brindle; the helper is background-only and nothing registers it
    with LaunchServices).
11. On Intel and on Apple Silicon, and on the oldest macOS at hand (the app
    supports 11).
12. The app itself is now signed with entitlements for the first time: it
    opens, its webview works, and an update installs, as before.

**Not done: one fixed daemon label.** It would stop the repeat Login Items
notice (decision 1). Nothing found here makes it safe without relaunching the
real app into an update, which is the case `d27fb029` broke, so it is left
out.

## Decisions for the owner

1. **One fixed daemon label: about a day, not done here.** It would remove the
   repeat notice and make the off switch work, if BTM does key by label. Why not
   in this branch: per-pid labels were introduced because launchd tore down a
   relaunched app's job under a shared label (`d27fb029`), which would kill
   sessions. Quit now keeps the daemon, so that race may be gone, but proving it
   means relaunching the real app, which I can't do from here. `launchd_plan`
   already handles a shared label's idle and running jobs; the work is an adopted
   job ending mid-start, and the known bootout-then-bootstrap I/O error.
   First check whether it's needed, on any Mac, with no admin rights:
   `sfltool dumpbtm`, counting entries for `com.pawdelta.brindle.daemon`. Several
   means per-label is real.
2. **Helper bundle for the engine: built, see above.** As it was put:
   A small `brindle engine.app` inside the bundle, with Info.plist and usage
   strings, as the daemon's responsible process. It would replace "python3" in
   every prompt and System Settings row, and let prompts explain themselves. It
   would not remove the prompts. It re-asks anyone already granted `python3`,
   and needs care with the Python path. Combine with `SMAppService` if doing
   both, as that also wants a helper in the bundle.
3. **A lighter alternative to 2: about a day or two.** A small compiled
   `brindle-engine` executable that embeds an Info.plist (name, usage strings)
   and runs Python in-process through the engine's libpython, signed with its own
   identifier. It can't just `exec` python3: that swaps the identity back. It gets
   the same result without a nested app, if macOS reads an embedded plist for
   these prompts as it should (unverified). `is_daemon` and the launcher need
   checking, and it also re-asks anyone already granted `python3`. I'd try this
   before 2, unless `SMAppService` is also wanted.
4. **Check for git's tools before running git:** `xcode-select -p` is silent, so
   brindle could explain and offer the install itself. Small, but in the git
   layer, which another branch owns right now.

## What needs a fresh macOS account

Nothing above was seen on a fresh account, so please run through this once with
a release build, signed in with a **Claude subscription**, and with Chrome
installed. Note the exact wording and name on each dialog.

1. Open from the DMG, copy to Applications, open. Which dialogs, in what order?
2. Does the Background Items notice, and the Login Items row, name brindle (the
   new plist key, with python3 as the job's program)? And does the daemon start
   and get reused as before (no "another install" message)?
3. Quit, wait for the daemon to exit idle, reopen. A second notice? (Decision 1.)
4. Start a first session. **Is the Chrome prompt gone?** To confirm the cause,
   run `claude` from a pane on a build without this branch and compare.
5. Open a project in `~/Documents`. Does the prompt name `python3`? Is it asked
   once per folder, or by each agent?
6. Do the same on a Mac without Command Line Tools: what does opening a project do?
7. Which keychain dialogs remain, on a build that also has
   `fix/background-git-no-keychain-prompt`?
8. Does a first notification raise anything?
9. Install an update. Do the Documents grant and keychain access survive?
10. Turn brindle off under Allow in the Background and reopen. Does the app start?
