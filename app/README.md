# brindle desktop app

A Tauri 2 shell (React 19, TypeScript, Vite, Tailwind 4, the site's palette
and braid mark) over the local brindle daemon: everything it shows and does
comes from the engine's v1 API, never from logic of its own. The engine is the
one in this same repo (its root, above `app/`): `npm run gen:api` reads its API
schema from there, and the macOS bundle carries that engine
([docs/bundled-engine.md](docs/bundled-engine.md)).

A sidebar on the site's warm surfaces beside the provider's own
session, which fills the main area: the app opens straight into the
last-used session's Claude Code (or Codex, OpenCode ...) screen, front and
centre. The sidebar is brindle's whole layer: "Inbox · N" at its head (the
Inbox page: every decision across sessions, each with "Go to it",
permission and sign-in items completable in the row), the selected
session's context (its goal and milestones, what it asks of the person,
with permission prompts answerable in place as the provider's own choices,
and its PR status) above a draggable split, and under it the CLI's tmux
sidebar: every open session, grouped by repo, with its agents nested in
the CLI's rows (its glyphs, colours and labels, the needs-you markers),
paused ones folded under "Paused · N" with their goal and a Resume button that
continues them and opens their chat. A chat paused while on screen stays
there and says "This session was paused", with Reopen. No home board and no right-hand panel.
Behind the sidebar's foot the setup pages: Knowledge (a repo's
decisions and document sources), Profiles (who brindle can run as, and which
providers are signed in), Routing (which models do each weight of work and
who reviews), Settings (keys, the repo's settings, permission rules, doctor,
the engine's update) and Account (the brindle Pro plan, sign-in through the
system browser, spend). Light and dark follow the system, or the switch at
the sidebar's foot.

Needs Node 20.19+ (22 in CI). Rust and the Tauri toolchain are needed only for
`npm run tauri dev` / `tauri build`.

```sh
npm ci
npm test              # Vitest (watch); npm test -- --run for one pass
npm run build         # type-check and build the front end into dist/
npm run tauri dev     # the app window, against `npm run dev`
```

`bash app/scripts/smoke_bundle.sh` (macOS) builds the bundle without a
Developer ID (signed ad hoc) for this Mac's architecture and checks it: the
`.app` and `.dmg` exist, each `.app` passes `codesign --verify --deep
--strict`, the bundle is named brindle at package.json's version, and the
binary, from the `.app`
and from the mounted `.dmg`, prints `brindle <version>` for `--version`
(headless: no window, no daemon). `--skip-build` checks the last build;
`--engine` also runs `brindle --headless-check` from each, which finds or
starts the daemon as the app does and must get `GET /v1/meta` answered (needs
an engine with `brindle daemon`, run from your own terminal: the daemon refuses
a brindle agent's descendants). `--linux` builds and checks the Linux bundles
instead: the AppImage and the `.deb` exist, the `.deb`'s control file says
package `brindle` at package.json's version, and the AppImage and the `.deb`'s
binary each print `brindle <version>` for `--version` under `xvfb-run`. On
Linux it builds in place (Tauri's Linux dependencies installed, as in the
release workflow's `linux` job); on a Mac it runs itself in an Ubuntu 22.04
container built from `scripts/linux-build.Dockerfile` (it starts Docker
Desktop if needed; cargo, npm, `node_modules` and `target/` are cached in
`brindle-desktop-*` docker volumes, so only the first run is slow), for the
Mac's own architecture, and copies the two bundles to
`src-tauri/target/linux-bundle/`. The release workflow's `nightly` job runs
`--engine` on macOS against the engine installed from the same commit
(`uv tool install` of the checkout), every night on a schedule and by hand
(workflow_dispatch with `nightly: true`).

## Layout

- `src/App.tsx`: the shell: the sidebar (`src/components/Sidebar.tsx`), the
  engine banner, and the screen the route names (`src/routes.ts`, kept in
  state; no URL routing in a desktop window). It lands on the session kept
  from the last launch (`routes.ts` `readLastSession`), else the first live
  one, else the empty state (`screens/New`). `src/main.tsx` wires the
  store, the daemon client and the sync to it.
- `src/components/Sidebar.tsx`: the merged sidebar, drawn to
  `design/options-v2/M3b-multi.html` (its layout, spacing, type sizes,
  dots and section headings; `styles.css`'s `.side-*` rules) with
  `design/options-v3/C2b-inbox-only.html`'s one entry for what needs a
  person, brindle's whole layer in one column. From the top: "Inbox · N"
  (the Inbox page; "empty" when nothing waits), the only thing about
  attention in the sidebar; the selected session's context
  (`SessionContext.tsx`: the goal's title with the session's id, repo and
  mode; the goal as "Milestone N of T · state" with a ring, a bar per
  milestone and the milestones listed, then the last check, the autopilot
  switch and "Check milestone N" (`screens/Session/GoalPanel` compact; no
  switch for a paused session); and its PR status, the engine's word, a
  "✓ Ready for PR" entry once it says ready), a handle that drags the
  split (kept in localStorage, `brindle.sidebar.split`, guarded), then
  "Sessions": every live session by repo (its goal, its id and mode under
  it, a caret folding its agents) with the supervisor and its workers
  nested as "id · branch" rows with a dot for the CLI's status
  (`store/overview.ts` `cliStatus`, `Sidebar.tsx` `dotTone`) and its state
  in plain words at the right (`plainLabel`: "waiting", "stopped"; never a
  count or a needs-you mark); paused and failed sessions dimmed at the
  foot of their repo's group; the pages as small links at the foot. The
  sidebar sits on the site's warm surfaces (canvas-2 as its ground), with
  purple (`branch`) only as the accent for what is selected or current;
  every colour in it is a token (`styles.css`'s `.side-*` rules). Keys as
  in the CLI: ↑ ↓ (j k) move, Enter opens, ⌘1-9 jump to a session, ⌘N
  starts one.
- `src/screens/New/`: the calm empty state and the New session composer
  (the prompt, the repo, Start; branch, CLI and autopilot behind Options).
  A start with sessions already running asks what to do; one the engine
  refuses with 409 `{preflight}` shows the CLI's preflight items
  (`components/Preflight.tsx`: a sign-in through `ProviderSignIn`, a key in
  a password field to `PUT /v1/keys/{name}`, a trust or consent as a button
  to `POST /v1/providers/{p}/preflight/{item}`; engine branch
  desktop/provider-preflight, stubbed in the fake daemon until it merges)
  and starts again once they are settled.
- `src/styles.css`: the tokens (the pawdelta.com site's palette,
  type, scale and radii in both themes, the same values as the site's; the
  app's own fail red and on-sap / on-branch; each light accent and ink-3
  at least 4.5:1 on canvas-2 and on its own soft pill, in both themes, the
  site's tokens changed to match) mapped onto Tailwind names. No colour
  is written anywhere but there: the terminal reads the shell tokens from
  the page, the sidebar's rules use the same tokens. `src/components/ui.ts` holds the shared looks (buttons,
  fields, cards, headings) every screen composes from; `PageHeader.tsx` and
  `ThemeToggle.tsx` beside it.
- `src/api/client.ts`: the typed client for the daemon's HTTP routes
  (`Authorization: Bearer <token>`, engine errors thrown as `DaemonError`).
- `src/api/events.ts`: `WS /v1/events`. Sends the token as the first frame
  (never in the URL), reconnects with backoff and resumes with
  `?since=<last event id>`; stops for good on close code 4401.
- `src/api/ptyView.ts`: every pane the app shows (an agent's window, an
  action terminal such as a sign-in) through a real tmux client on a pty of
  the shell's (`pty_view_*` commands, `src-tauri/src/pty_view.rs`; the e2e
  tests reach the same Rust over `examples/pty_ws.rs`). Bytes go both ways
  as they are; see `docs/terminal-architecture.md`.
- `src/components/PtyTerminal.tsx`: xterm.js on such a view, fitted to the
  view, WebGL renderer with a DOM fallback, OSC 52 copies onto the app's
  clipboard. `src/api/paneText.ts` reads a pane as text through a view no one
  sees (a sign-in's link, question, code prompt and end).
- `src/api/schema.ts`: the engine's DTOs. `npm run gen:api` regenerates it from
  the engine's `src/brindle/daemon/api-schema.json` (looked for in
  `$BRINDLE_ENGINE`, then in a brindle checkout beside this repo, or pass the
  path: `npm run gen:api -- <file>`).
- `src/api/tauri.ts`: the daemon through the shell: `tauriClient()` (a
  `DaemonClient` whose requests go to the `daemon_request` command) and
  `tauriSocket` (the event stream over `daemon_stream_open`: text and binary
  frames come back on a channel, the base64 binary ones decoded here, and
  what the client sends goes out through `daemon_stream_send`). The webview
  never sees the token: the shell sends it as each socket's first frame
  itself and drops the client's.
- `src/api/engine.ts`: `checkEngine` reads `GET /v1/meta` and judges its
  `api_version` against the range the app speaks (>=1.0,<2);
  `src/components/EngineBanner.tsx` shows the result (upgrade the engine,
  update the app, unreachable, refused).
- `src/store/`: the reducer (snapshots are authoritative; events patch in place
  or mark the store stale) and `startSync`, which keeps it in step with a daemon.
- `src/components/Sheet.tsx`: brindle's layer over a screen: a sheet that
  slides in from the right (a review, a terminal of the daemon's), one
  header / body / footer shape, Escape closes it; the screen beneath stays
  mounted and untouched. `EngineUpgrade.tsx` is the engine's upgrade as a
  background progress card, following its job (`POST /v1/engine/upgrade`)
  on the bus. `ProviderSignIn.tsx` is a provider's sign-in, browser first:
  the button starts the CLI's login (`POST /v1/providers/{p}/login`, which
  answers where its pane is), a card waits for the browser and reads what
  the CLI shows through a view no one sees (`api/paneText.ts`: a link to
  open again, a choice as buttons, a code to paste as a field, each typed
  in with `POST /v1/terminals/{id}/input`; tmux's "Pane is dead" is its
  end), says "Signed in ✓" once the provider reads so again, and keeps the
  CLI's pane behind "Show details" (`PtyTerminal`).
- `src/screens/Inbox/`: `InboxScreen`, the Inbox page
  (`design/options-v3/C2b-inbox-only.html`): every decision across
  sessions as banner cards grouped by session (the kind, one readable line
  of what is asked, its detail, the agent and branch, the session, the
  age), each group with "Open the session ›" and each card with "Go to
  session ›" (the item's agent in its session: its provider screen, with
  nothing over it, ever). Each card either acts right there or goes to the
  session, with no popup, reply field or moment to undo: one click, sent
  at once (`POST /v1/inbox/{id}/{action}`, `useInboxActions`), and the
  card goes; the engine's refusal comes back as one line on it. Acted on
  in the card: a permission request brindle's hook holds (the provider's
  own choices; `POST /v1/inbox/{id}/permission {request, digest,
  choice}`), a lapsed sign-in (Sign in, `ItemActions`: the provider CLI's
  own login on the engine, or brindle's cloud sign-in), a plan (Approve;
  changes are asked for in its session), proposed checks (Accept /
  Decline), an org notice (Mark read). Everything else (an agent waiting
  at its own prompt, a permission only that prompt can take, a failure, a
  branch to review or merge again) says so in one quiet line and only
  goes to the session. Engine branch desktop/permission-items, stubbed in
  the fake daemon until it merges (`InboxItemDTO.permission`, `.root_id`,
  `.action` in `api/schema.ts`; `items.ts` `permissionLine` reads the
  request as one line, the whole on click). `watchInbox` sends a system
  notification for each new inbox item (and one when a session becomes
  ready for a PR) and sets the tray badge.
- `src/screens/Routing/`: `RoutingScreen({ client })`: one row per weight
  tier and one for the reviewer, in the website's words: the model chosen
  (a dropdown of "model · agent", with its cost), the fallbacks tried after
  it behind a disclosure (reordered, dropped, added to), and where each
  value comes from (`GET /v1/routing` for the repo); presets (prefer Claude,
  cheapest, balanced); keeping a whole agent out of routing under Advanced;
  edits saved with `PUT /v1/routing` to the person's own config, this
  repo's `config.local.json`, or the repo's shared config (marked as
  changing the team's). The org's policy (`policy` in `GET /v1/routing`,
  for a Team or Enterprise org) is one line, "Managed by <org>", with the
  details behind a disclosure: a model the org doesn't allow never appears
  in a dropdown or a list, a locked provider shows the lock and who set it,
  and nothing is editable while the policy applies but can't be read; the
  engine refuses a forbidden profile with a 403 and the screen shows why.
  `routing.ts` holds the draft, the presets and the first-choice logic,
  apart from React.
- `src/screens/Knowledge/`: `KnowledgeScreen({ client })`: the repo's
  accepted decisions (`GET /v1/decisions`) and a form to record one
  (`POST /v1/decisions`; a refusal names the decision in the way and one
  click supersedes it); the knowledge base's sources (`GET /v1/kb/sources`);
  one search box over the decisions (by what they say) and the documents
  (`GET /v1/kb/search`, the engine's own ranking). Adding a source and the
  coordinator's log stay in the CLI.
- `src/screens/Profiles/`: `ProfilesScreen({ client, profile, onSelect,
  onRouting })`: every profile the engine resolves for the repo
  (`GET /v1/profiles`) by role; the picked one as `brindle profile show`
  resolves it (`GET /v1/profiles/{name}`: source, extends chain, rule
  packs, the full prompt) with `brindle profile lint`'s findings, and
  "Check every profile" to mark the whole list; each provider's sign-in
  state from `GET /v1/routing`, with a Sign in button for one signed out
  (`ProviderSignIn`: the app never signs in to a provider, it starts the
  CLI's own login and follows it) and the org's refusal of a profile its
  policy doesn't allow.
- `src/screens/Settings/`: `SettingsScreen({ client })`: keys as names and
  last four (`GET /v1/config`), each set or changed in a password field
  whose value goes to `PUT /v1/keys/{name}` (write-only: the engine stores
  it as `brindle keys set` does and answers the last four; the app keeps
  nothing of it), the repo's settings with a UI edited as a draft and saved
  with `PATCH /v1/config` to the repo's `config.local.json` or the person's
  config (a number is required, since the engine stores what it is sent;
  checks only count from the repo's files, so they pin the save there), a
  permission rule to add and the reset (`POST /v1/permissions`; the list
  of rules is the CLI's), the doctor (`GET /v1/doctor`, a failure shown as
  one) and from it the engine's update, run by the engine itself in the
  background and followed as a progress card (`POST /v1/engine/upgrade`,
  `EngineUpgrade`). `settings.ts` holds the settings table and the draft
  logic.
- `src/screens/Account/`: `AccountScreen({ client, openUrl, onNavigate })`:
  the entitlement (`GET /v1/account`: plan, org, status, seats, renewal,
  grace, whether hosted learning is on and the learner's kept note, and the
  account's orgs, as a picker for the org switch), a sign-in run on the
  daemon (`POST /v1/account/login` answers a job; its `job.progress`
  carries the authorize URL, opened in the system browser with `openUrl`, or
  the device code's lines), sign-out and org switch (after which everything
  engine-gated is asked for again), and the spend (`GET /v1/cost`), locked
  by the engine's `not_entitled` refusal. A paying customer sees their
  plan's card as the pricing page has it, without the price, each included
  feature a link to where it is used (Profiles, Routing, Settings, the
  Spend card, or the site's docs), a quiet "Get the most from <plan> ↗"
  link and, below Enterprise, a quiet "Upgrade to <next tier> ↗" link, and
  nothing else for sale; someone on the free plan, or signed out, sees the
  pricing page's four plans compared and the site's own upgrade.
  `account.ts` holds the plans as `web/app/pricing/page.tsx` lists them.
- `src/api/open.ts`: `openExternal(url)`: the system browser, through the
  shell's `open_url` command (http(s) only) in the app, a new tab otherwise.
- `src/screens/Session/`: `SessionScreen({ client, root, repo, refresh })`,
  the provider's session front and centre: `ProviderSurface` shows what
  the selected agent's CLI draws, edge to edge (`PtyTerminal` on its
  window: no header, no pane chrome), named by provider and agent ("Claude Code · ux revamp"),
  and never a shell: when the process isn't running (exited, failed,
  paused, done, not started) a native state card takes the surface, with
  Continue / Restart (`POST /v1/sessions/{root}/continue`), a word that a
  lapsed sign-in is done from the Inbox, what the pane left (the dead pane
  tmux keeps: a finished worker's final screen shown from the start; a pane
  that merely died, a shell prompt by now, behind "Show what it left"), and the engine's
  preflight items when a
  launch is refused with 409 `{preflight}`. Above it one slim bar, the
  selected agent's alone (provider and agent with its id, its state in the
  CLI's words, pause / continue, and the agent's ⋯ with close / kill,
  `AgentMenu.tsx`; nothing of the session's: its goal is the sidebar's,
  what waits the Inbox's). Continue on the card and Continue in the bar are one
  handler (`useContinue.ts`): the engine's 409 `{preflight}` items show on
  the surface whichever was pressed, and a success bumps `attempt`, which
  remounts the pane's socket, so a pane that had said "no pane" streams
  again. The provider session fills the main area alone: brindle's layer
  for the session (its goal, its PR status) is the sidebar's context pane,
  not a panel beside the surface; what waits on the person is the Inbox's.
  No message box anywhere (the person talks to an agent in its own
  terminal) and no per-worker review entry. Nothing ever pops up over the
  surface: the one sheet, "Review & open PR", opens only from the
  sidebar's "Ready for PR" entry (`routes.ts` `sheet`), on purpose.
- `src/screens/Session/ReviewSheet.tsx`: "Review & open PR", per
  supervisor session (`design/options-v2/V2-stepped-sheet`'s one calm
  sheet with `V1-fullscreen-tree`'s file list, and no worker attribution:
  the engine's `authors` / `workers` are never shown). It exists only
  once the engine says the session is ready (`review.ts` `usePrReadiness`:
  `GET /v1/sessions/{root}/pr/readiness`, read again on `session.pr_ready`,
  `session.edited`, autopilot activity and the PR job's end; unknown, and
  so nothing shown, for an engine without the route). Before that there is
  nothing of it (not from the milestones either); at the moment it flips,
  a system notification (`screens/Inbox/notify.ts`) and the sidebar
  context's "✓ Ready for PR" entry (`SessionContext.tsx`), which opens the
  sheet (`routes.ts` `SheetRef` `{kind: "review"}`); nothing pops up over
  the session. Open, the sheet stays through the engine re-checking the
  session (a quiet note says so; a diff it refused meanwhile is read again
  once it is ready). An unsaved edit is a draft the sheet owns, one per
  file (its text, the blob it started from, hunk or whole file, and a
  hunk's original lines): every save is sent against that blob, so a file
  that changed meanwhile is a conflict, never a write; the box keeps its
  text and says "Changed since you started editing" with Copy my edit /
  Discard / Reload, and nothing the engine sends ever replaces the text
  of an open box (a hunk that moved or vanished keeps its draft above the
  hunks, with the lines it was made from). Every hunk shown is checked
  against the file as the checkout has it (`review.ts` `hunkMatches`)
  before an edit can start. The
  sheet: the session branch against its PR base (`GET
  /v1/sessions/{root}/diff`, reshaped into `Branch/parseDiff`'s files),
  the changed files down the left, the selected one hunk by hunk; click a
  hunk (or `e`) and its new side is a box, ⌘S reads the file (`GET
  .../file`, `{path, content, base}`), splices the lines in and writes it
  back (`PUT .../file {path, content, base}`) pinned to the blob the file
  had when it was opened here (a file that moved on is refused before
  anything is written; the engine's 409 stale / busy shows in the save
  bar); "Full file" edits the whole file in a box; "+ New file" writes one
  (no base); a file's ✕ deletes it (`DELETE .../file {path, base}`); saved,
  the diff is read again. "Open PR…" is the form, title and body from
  `GET .../pr/draft`, sent with `POST .../pr` (a job whose `job.done`
  result carries the URL, opened in the system browser). When the repo's
  `auto_pr` setting lets brindle open PRs on its own (engine branch
  desktop/auto-pr, stubbed: `review.ts` `autoPrOn` reads it from `GET
  /v1/config`, the fake daemon's readiness carries `pr`), the sheet says
  brindle opened it, with the link, and stays editable: edits go to the
  branch, which is the PR's.
- `src/api/context.ts`: `DaemonContext` / `useDaemon()`, the client screens
  call actions through.
- `src/screens/Branch/`: the diff a review item's sheet shows: `DiffView`
  and `useDiff` (`GET /v1/workspaces/{id}/diff`, split by `parseDiff` into
  files and hunks), the changed files down the left, the selected one hunk
  by hunk; `useJob` follows a long action's `job.progress` / `job.done` /
  `job.failed` events. Changed lines are editable in place while the
  worker is idle (the pill says so; dashed while an agent in the workspace
  is mid-turn): click a hunk's header or press `e` (`j` / `k` move between
  hunks) and its new side becomes a box; ⌘S reads the file from the
  worktree (`GET /v1/workspaces/{id}/file?path=`, answering `{path,
  content, base}`), splices the hunk's lines into it and writes the whole
  back with `PUT /v1/workspaces/{id}/file {path, content, base}` (the blob
  the diff's `index` line named), committed on the worker's branch; a 409
  (stale, busy) shows in the save bar; Escape discards. Engine branch
  desktop/review-edit.
- `src/test/fakeDaemon.ts`: an in-memory daemon (fetch + WebSocket) with the
  engine's guard and replay rules, its terminal frames (`terminals`,
  `print`, `share`), the org policy in `GET /v1/routing` (`routingPolicy`),
  the action terminals (`actions`, `exitAction`, `inputs`), the keys
  stored (`keysStored`), the worktrees' files for the review sheet's edits
  (`files`, `edits`, `fakeBlob`), each provider's preflight items
  (`preflight`) and each session's PR review (`sessionReview`: ready or
  why not, its diff as a patch, the PR brindle opened; `sessionFiles`,
  `sessionEdits`, `prs`; `readyForPr()` flips one and publishes
  `session.pr_ready`), for tests.
- `demo/`: `npm run build:demo` builds the app over the fake daemon and
  the renderings' fixtures as one self-contained `demo/dist/index.html`
  (classic script, styles inlined: it opens from `file://`), with a little
  life (terminals that print, a sign-in that completes, keys that store)
  and a floating switcher for the plan and the org policy. Nothing of it
  ships.
- `src/screens/FirstRun/`: `Launch` finds the daemon before the app shows;
  when it can't be found or started, `FirstRun` shows what brindle needs and
  installs it (see "The first run").
- `src-tauri/`: the Rust shell: the engine connection (`src/daemon.rs`), the
  first run's checks and install pty (`src/setup.rs`), the notification plugin and the tray / menu-bar icon (its count set by the
  `set_inbox_count` command). `app-icon.svg` is the source of `icons/`
  (`npm run tauri icon src-tauri/app-icon.svg`).

## The engine connection

The shell reads `~/.brindle/daemon.json` (`$BRINDLE_HOME` if set). When no
live daemon is named there it starts `brindle daemon --idle-exit` (found on
PATH, in `~/.local/bin`, Homebrew's bin or uv's tool directory (`uv tool dir
--bin`), or at `$BRINDLE_BIN`) and waits for
it to listen; its stderr goes to `~/.brindle/app-daemon.log`, and its PATH
gets those directories added, so a Finder-launched app's daemon still finds
tmux and the agents' CLIs. Requests go to
the Unix socket with the token added in Rust; a daemon that has gone away
(idle exit, an engine upgrade) is found or started again on the next request.

The daemon refuses Unix-socket callers that descend from a brindle agent's
process, so `npm run tauri dev` started from an agent's terminal shows
"refused": run it from your own.

## The first run

When the daemon can't be found or started at launch, the app shows a setup
screen instead: uv, tmux, brindle and Claude Code (Codex and Antigravity
optional), found as `brindle doctor` finds them, with what each is for and how
to get what's missing. Install runs the equivalent of brindle's
`scripts/install.sh` (uv from astral.sh, tmux with Homebrew, `uv tool install
--upgrade brindle`) on a pty of the shell's, shown in a terminal pane the
person can type into; then the app starts `brindle daemon` and goes on.
Nothing signs in for anyone: when Claude Code isn't signed in, the screen says
to run `claude` once in a terminal. Without Homebrew on macOS, tmux is left to
the person (brew.sh), as the install script does.

`npm run dev` in a browser has no shell: point it at a daemon with
`VITE_BRINDLE_DAEMON_URL` and `VITE_BRINDLE_DAEMON_TOKEN` (development builds
only; these win over the shell when set). `VITE_BRINDLE_REPO` (a repo's root
path) and `VITE_BRINDLE_ROOT` (a session's root agent) open it on that session.

## Updates

The shell registers Tauri's updater plugin (`src-tauri/src/updates.rs`) and
checks once a launch against
`https://pawdelta.com/brindle/downloads/desktop/latest.json` (a release
candidate makes no launch check; staff update it from Settings against
`rc/latest.json`: see "The rc channel" under Releases); an update found
is downloaded, its minisign signature checked against the public key built into
the app, installed, and a notification says it starts with the next launch.
No check is made when brindle is air-gapped or updates are off:
`"app_updates": false` or `"airgap": true` in `~/.brindle/config.json`,
`BRINDLE_AIRGAP=1` in the environment, or the engine: a repo's own
`.brindle/config.json` can turn air-gap on, and a freshly started daemon hasn't
read it yet, so the app first has the daemon load every known repo's config
(`GET /v1/repos`, then `GET /v1/config?repo=` for each) and checks only if each
says `"airgap": false` and `GET /v1/meta` then does too. Any failed or
unexpected answer means no check. The public key is committed
(`plugins.updater.pubkey` in `tauri.conf.json`), so every build checks, a local
`tauri build` too. Staff installs of a staff build check against a
separate staff key instead (`src-tauri/staff-updater.pub`; see "Staff builds"). Since `bundle.createUpdaterArtifacts` is on as well, a local
`tauri build` needs the minisign private key in `TAURI_SIGNING_PRIVATE_KEY`
(the key file's contents, as the release secret holds it; tauri-cli 2.12 ignores
`TAURI_SIGNING_PRIVATE_KEY_PATH` and fails with "A public key has been found,
but no private key") and its `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, or `--config
'{"bundle":{"createUpdaterArtifacts":false}}'` to skip the updater bundle
(`scripts/smoke_bundle.sh` does that).

## Releases

The version lives in `package.json` (`tauri.conf.json` reads it from there).
Bump it, merge, then push a tag `app-v<version>`:
`.github/workflows/app-release.yml` checks the tag matches and builds
twice, each on a runner of that architecture, since the bundled engine is
built for the runner's own architecture only
([docs/bundled-engine.md](docs/bundled-engine.md), "One architecture per
build"): `aarch64-apple-darwin` on `macos-latest` (Apple Silicon) and
`x86_64-apple-darwin` on `macos-15-intel` (GitHub retired the `macos-13` Intel
image in December 2025). Each `macos` job builds with the Tauri CLI (`npx tauri build`), which signs
the app with the Developer ID, notarizes and staples the .app and signs the
updater bundle. Tauri does not notarize the DMG, and Gatekeeper rejects an
un-notarized DMG ("Unnotarized Developer ID") even with a notarized app
inside, so the job then submits the DMG itself (`xcrun notarytool submit
--wait`) and staples it (`xcrun stapler staple`). Before anything is kept for
publishing, `spctl -a -vvv -t exec` on the .app and `spctl -a -vvv -t open
--context context:primary-signature` on the DMG must both say accepted,
`source=Notarized Developer ID`, or the job fails. Once both architectures
have passed, the `macos-publish` job writes `latest.json` from their artifacts
and publishes to the site's downloads (an S3 bucket the site's infrastructure
makes; the site answers `/brindle/downloads/desktop/*` with a redirect there):

- `<version>/brindle_<version>_aarch64.dmg` and
  `<version>/brindle_<version>_x86_64.dmg`; the latest of each as
  `brindle_aarch64.dmg` and `brindle_x86_64.dmg`, and `brindle.dmg` (the
  Apple Silicon one, under the stable name the universal build used)
- `<version>/brindle_<version>_<arch>.app.tar.gz` and its `.sig` for each
  architecture (the updater bundles)
- `latest.json` (the updater's manifest: `darwin-aarch64` and `darwin-x86_64`,
  each with its own bundle and signature), written last

Each piece is skipped cleanly when its secrets are missing: without the
Developer ID the builds are unsigned, and without notarization or the bucket
they are only kept as the run's artifacts. Nothing unsigned or un-notarized is
published. Run by hand (workflow_dispatch), even on an existing tag, the
workflow builds and never publishes: only a pushed tag does
(`scripts/publish_gate.sh`, tested by `scripts/publish_gate.test.ts`). A
dispatch also runs outside the `desktop-release` environment (below), so it
gets none of the signing secrets and builds unsigned.

**The engine in a release.** The bundled engine is built from this same repo
at the tagged commit (`scripts/build_engine.sh`, `BRINDLE_SRC` the checkout and
`BRINDLE_REF` `HEAD`), so an app release bundles whatever engine `main` has at
that commit. That may be ahead of the latest engine release on PyPI: the app
can ship engine changes before `pip install brindle` gets them.

**No GitHub Releases.** An app tag makes no GitHub Release: the engine's
`publish.yml` runs on published releases (PyPI), and its jobs skip any tag
that doesn't start with `v`. App downloads live only on the site.

**Release candidates.** A tag `app-v<X.Y.Z>-rc<N>` (with `package.json` at
the same version, e.g. `0.2.0-rc.1` for `app-v0.2.0-rc.1`) builds and
gates exactly like a release but publishes under `rc/<version>/` only: its
DMGs, updater bundles and its own `rc/<version>/latest.json`, then copies that
manifest to `rc/latest.json`, the rc channel. It never writes or overwrites
the top-level `latest.json` that stable apps read, nor the stable
`brindle*.dmg`, `brindle_amd64.AppImage` and `brindle_amd64.deb` names. Only
a final `app-v<X.Y.Z>` tag updates those.

**The rc channel.** An rc build (its version has an `-rc` pre-release tag)
never updates by itself (owner decision): it makes no check at launch.
Instead brindle staff (the account `GET /v1/debug` calls `staff`, as for
debug logging) get an **App update** section at the foot of Settings with one
Update button, which checks
`https://pawdelta.com/brindle/downloads/desktop/rc/latest.json` (`endpoint`
in `src-tauri/src/updates.rs` picks it from the app's version;
`tauri.conf.json` keeps the stable URL), installs a newer rc and offers
Restart (the shell's `app_update_install` and `app_restart` commands; the
shell asks the engine about staff again before installing, and air-gap or
`"app_updates": false` still turn it off). Anyone else on an rc sees nothing
and keeps the build they installed. A stable build keeps the automatic
launch check against the stable feed only, so a release never offers an rc.
An rc tester moves to a final release by installing its DMG: the rc channel
names release candidates only.

### Staff builds

A signed build of a commit on `main` gives brindle staff an installable,
updating app without a tag or version bump. Each push to `main` that touches
the app runs `app-release.yml`:

1. `release-gate` (`scripts/release_publish.mjs ci-gate`) refuses unless the
   commit is on `origin/main` and the newest `App` (`app.yml`) run for exactly
   that commit, pushed or dispatched on `main`, completed with success. The
   `CI` (`ci.yml`) run must also pass when one exists; CI skips app-only
   pushes. A pull request's
   runs, other branches' and other commits' don't count; a newer failed
   attempt outweighs an older pass. Workflows and runs come from the Actions
   API by path, all pages.
2. The `macos` jobs build both architectures with the app's version set to
   `<package.json version>+release.<run number>.<12-hex commit>` (e.g.
   `0.1.0-rc.9+release.42.0123456789ab`): Info.plist's
   `CFBundleShortVersionString` and `CFBundleVersion`, and the version the
   updater compares, all carry it. No Linux or Windows build.
3. `release-publish` (`release_publish.mjs verify`) refuses unless both
   builds are signed, notarized, have their updater bundle, and their signed
   Info.plist and binary (`--version`) both say that version. It writes
   `release/<run>.<attempt>-<12-hex>/` with
   `brindle_<package version>-release.<run>.<12-hex>_<arch>.dmg`, `.app.tar.gz`
   and `.sig` (no `+` in published names), `latest.json` (Tauri's format,
   signed with the staff updater key, notes naming the full commit) and `build.json` (commit,
   run, attempt, versions). Then `release_publish.mjs promote` decides whether
   `release/latest.json`, the channel's feed, moves to it: only to a strictly
   later run, of a version no lower (semver precedence), at a descendant of
   the commit the feed names. It is written with an S3 conditional put
   (`--if-match` the ETag read, or `--if-none-match '*'` for the first), so a
   stale run finishing late, a re-run or two runs at once can never move it
   back. It never writes `latest.json`, `rc/` or the stable names.

The feed's files are public at an unlisted path, like every download. Only
staff apps can install from it through Settings; normal users' apps never
read the feed.

**A separate updater key.** Staff builds are signed with their own
minisign updater key, not the production one. Its private key and password
are held only in `desktop-staff-release` (`TAURI_SIGNING_PRIVATE_KEY` and
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` there); its public key is committed at
`src-tauri/staff-updater.pub`. The `macos` job of a staff build
checks its updater key against that file and fails if it is the production
key (or if `TAURI_UPDATER_PUBKEY` names another key); a tag build still
checks against `tauri.conf.json`. In the app, only Settings' "Install latest
staff build" verifies with the staff key (`STAFF_UPDATER_KEY` in
`src-tauri/src/updates.rs`); every other update, a staff build's own
included, keeps the production key from `tauri.conf.json`. Installed
production apps never trust the staff key: a bundle signed with it fails
their update check, and their updater never reads the release feed. An app
built before this key existed verifies staff builds with the production key,
so it refuses them: install one staff build by hand first.

**When the gate refuses.** Once the missing or failed checks have passed,
rerun the refused `app-release.yml` run; the gate checks the same commit
again. The gate waits for the App and CI workflow runs started by the main
push, through `release_publish.mjs ci-gate`.

The staff role can write only `brindle/downloads/desktop/release/*`.
Tag publishing uses a different role and environment. There is no fallback
to the production role when the staff role is missing: publishing is skipped.
The gate is code from the pushed commit, so it is a correctness check;
GitHub deployment restrictions and IAM enforce the publishing boundary.

Re-running `release-publish` alone publishes into that attempt's own
directory (`verify` refuses an earlier attempt's). Its GitHub artifact also
includes the attempt number, preserving the earlier attempt's artifact after
an OIDC or S3 failure. Re-running all jobs after a completed Mac build can
still collide with that build's artifact name; use the publish job alone for
a publishing failure. Fix build failures with a new promotion/run.

Configure `desktop-release` to admit only `app-v*` tags and require owner
approval. The `stable-gate` job verifies each pushed tag's commit is on
`main` before any tag build runs. Configure `desktop-staff-release` to admit
`main` pushes. Provision the same Apple signing and notarization credentials
there as in `desktop-release`, plus the separate staff updater key matching
`src-tauri/staff-updater.pub`. Set
`AWS_DESKTOP_STAFF_RELEASE_ROLE_ARN` in that environment to `StaffRoleArn`
from `deploy/aws/desktop-release.json`.

### Linux

The same tag also runs the workflow's `linux` job on ubuntu-22.04 (x86_64):
`scripts/smoke_bundle.sh --linux` builds the AppImage and the `.deb` and
checks them, and they go to the same downloads under the same gate (a pushed
tag and the bucket set; Linux bundles need no OS signing, so there is no
signing check):

- `<version>/brindle_<version>_amd64.AppImage` and `brindle_amd64.AppImage`
- `<version>/brindle_<version>_amd64.deb` and `brindle_amd64.deb`

There is no Linux updater bundle yet: `latest.json` lists the Macs only, so a
Linux install updates by downloading the new AppImage or `.deb`.

What a Linux machine needs to run the app:

- **WebKitGTK 4.1** (`libwebkit2gtk-4.1-0`, Ubuntu 22.04 / Debian 12 or
  newer): the `.deb` depends on it; the AppImage bundles it.
- **The engine and its tools**: `uv` and `tmux` from the distro (`apt install
  tmux`; uv from astral.sh) or brindle's install script, which installs both
  and then `uv tool install brindle`. The app finds `brindle` on PATH or in
  `~/.local/bin` and starts `brindle daemon` itself, as on macOS.
- **Notifications**: the notification plugin speaks the freedesktop
  notification service over D-Bus (`org.freedesktop.Notifications`, which
  every desktop and xdg-desktop-portal provide), so no Linux-specific code;
  without a notification daemon they are dropped silently.
- **Tray icon**: the "needs you" count shows through libayatana-appindicator
  (GNOME needs the AppIndicator extension to show tray icons at all).

### Windows (WSL)

The same tag runs the workflow's `windows` job, which builds the NSIS installer
(`brindle_<version>_x64-setup.exe`). With the Azure Artifact Signing secrets
set (table below; `azure/login` through GitHub's OIDC, no client secret)
Tauri's `signCommand` (signtool with the Azure dlib) signs the app exe and the installer, and `signtool verify
/pa` must accept both or the job fails. Under the same gate as the other
platforms (a pushed tag, the bucket set) the signed installer is published as
`<version>/brindle_<version>_x64-setup.exe` and `brindle_x64-setup.exe`
(a release candidate only under `rc/<version>/`). Without the secrets the
installer is unsigned and kept as a workflow artifact only, nothing is
published, and it shows a SmartScreen warning.

The Windows app is a thin client of an engine running inside WSL
(`src-tauri/src/wsl.rs`): it uses `$BRINDLE_WSL_DISTRO`, else WSL's default
distribution, starts `brindle daemon --tcp --idle-exit` there with `wsl.exe`,
reads its port and token from `\\wsl$\<distro>\home\<user>\.brindle\daemon.json`
and talks to it on 127.0.0.1 (WSL2 forwards localhost). A daemon counts as
running when it answers an authenticated `GET /v1/meta`. The first run checks
and installs inside the distribution (no pty there: the install's output comes
over pipes). A daemon already started in WSL without `--tcp` can't be reached:
stop it (`brindle daemon stop`) and the app starts one that can.

What the owner sets up, once. The Apple, Tauri and Azure secrets and the
`TAURI_UPDATER_PUBKEY` variable live in the `desktop-release` GitHub
environment (repository settings → Environments). The stable-gate job uses
this environment and requires reviewer approval for pushed tags. Staff builds
use the `desktop-staff-release` environment for staff updater signing and
publishing; configure its secrets and AWS role for the staff feed. `macos`
selects the environment by ref; `macos-publish`, `linux` and `windows` use
production, and `release-publish` uses staff.
Downloads bucket and region variables may be repository variables (Secrets
and variables → Actions):

| Name | Kind | What |
| --- | --- | --- |
| `APPLE_CERTIFICATE` | secret (`desktop-release` env) | The Developer ID Application certificate as a base64 `.p12` (`base64 -i cert.p12`). |
| `APPLE_CERTIFICATE_PASSWORD` | secret (`desktop-release` env) | The `.p12`'s export password. |
| `APPLE_SIGNING_IDENTITY` | secret (`desktop-release` env) | e.g. `Developer ID Application: PawDelta (TEAMID)` (`security find-identity -v -p codesigning`). |
| `APPLE_API_KEY` | secret (`desktop-release` env) | Notarization through an App Store Connect API key: its key ID. |
| `APPLE_API_ISSUER` | secret (`desktop-release` env) | That key's issuer ID. |
| `APPLE_API_PRIVATE_KEY` | secret (`desktop-release` env) | The key's `.p8` contents; the workflow writes it to a file and sets `APPLE_API_KEY_PATH`. |
| `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID` | secrets (`desktop-release` env) | Instead of the API key: an Apple ID, an app-specific password and the team ID. |
| `TAURI_SIGNING_PRIVATE_KEY` | secret (`desktop-release` env) | The production updater minisign private key matching the committed public key. `desktop-staff-release` holds the staff key instead (matching `src-tauri/staff-updater.pub`). |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | secret (`desktop-release` env) | Its password, if it has one. |
| `TAURI_UPDATER_PUBKEY` | variable (`desktop-release` env) | Optional. The matching public key is committed in `tauri.conf.json` (`plugins.updater.pubkey`); when this is set, the build fails unless it is that same key. In `desktop-staff-release` it must be `src-tauri/staff-updater.pub`'s key, never the production one. |
| `AZURE_CLIENT_ID` | secret (`desktop-release` env) | Windows signing: the application (client) ID of a Microsoft Entra app registration that has the **Artifact Signing Certificate Profile Signer** role on the signing account (Entra admin center → App registrations). |
| `AZURE_TENANT_ID` | secret (`desktop-release` env) | The Entra tenant (directory) ID, from the same app registration's overview. |
| `AZURE_SUBSCRIPTION_ID` | secret (`desktop-release` env) | The subscription holding the signing account (`azure/login` needs one). There is no client secret: add a federated credential to the app registration (Certificates & secrets → Federated credentials; issuer `https://token.actions.githubusercontent.com`, subject `repo:OWNER/REPO:environment:desktop-release`, audience `api://AzureADTokenExchange`). |
| `AZURE_SIGNING_ENDPOINT` | secret (`desktop-release` env) | The signing account's regional endpoint, e.g. `https://eus.codesigning.azure.net/` (the account's overview page in the Azure portal). |
| `AZURE_SIGNING_ACCOUNT` | secret (`desktop-release` env) | The Artifact Signing account's name (Azure portal → Artifact Signing accounts; formerly Trusted Signing). |
| `AZURE_SIGNING_PROFILE` | secret (`desktop-release` env) | The certificate profile's name, under that account (needs identity validation done first). Signing is on when this and `AZURE_CLIENT_ID` are set. |
| `AWS_DESKTOP_RELEASE_ROLE_ARN` | secret (`desktop-release` env) | ProductionRoleArn from the publishing stack; tags only. |
| `AWS_DESKTOP_STAFF_RELEASE_ROLE_ARN` | secret (`desktop-staff-release` env) | StaffRoleArn from `deploy/aws/desktop-release.json`; main pushes only. |
| `DESKTOP_DOWNLOADS_BUCKET` | variable | The downloads bucket's name, from the same setup. |
| `AWS_REGION` | variable | Optional; `us-east-1` when unset. |

Before the first release: join the Apple Developer Program, and have the site's
infrastructure create the downloads bucket, GitHub's OIDC provider and the
release roles and restrict their GitHub environments as described above.
Only `app-v*` tags may enter production; staff builds come from pushes to
`main` after the commit's App run and any matching CI run pass.
Keep the
minisign private key safe: losing it means no installed app can take another
update.
