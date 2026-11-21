# Renderings

A dev-only screenshot setup: the app over the in-memory fake daemon
(`src/test/fakeDaemon.ts`) with realistic fixtures, screenshotted as
full-page PNGs in both themes. Nothing here ships: `vite build` bundles
`index.html` only, and Playwright is fetched by `npx`, not a dependency.

```sh
npm ci                                   # once
node shots/shots.mjs --step 06-final     # shots/out/06-final/<scene>-<theme>.png
node shots/shots.mjs --step x --out /some/dir --scene sessions,inbox --theme dark
node shots/shots.mjs --step x --scene session-done --browser webkit   # in WebKit, as Tauri on macOS
node shots/shots.mjs --step x --scene library --channel chrome        # the Chrome already installed, no download
```

- `shots.mjs` serves `preview.html` with Vite (in-process, on a free port
  from 4178), runs `npx -y playwright@1.49.1 install chromium` (a download
  the first time), and for each scene and theme runs Playwright's
  `screenshot` CLI at 1440×960, full page, after a short settle.
- `preview.tsx` is the entry: it builds a `FakeDaemon` from `fixtures.ts`,
  stores the theme the way the theme switch reads it, and renders `App` on
  the scene's route. A runtime error shows as a strip at the page's foot
  rather than a blank shot.
- `fixtures.ts`: two repos; a live autopilot session with a supervisor and
  four workers (one at work, one waiting on the person, one reported and
  under review, one failed), a goal with milestones, branch diffs, terminal
  panes, and an inbox with one of each kind; plus paused sessions. Routing:
  the built-in profiles on a Mac with Claude Code and Codex signed in, the
  team's config excluding Gemini and reviewing with Codex, and this person's
  own heavy tier. Knowledge: three decisions and two sources. Settings: three
  key names, the repo's config, a doctor run with a newer brindle on PyPI.
  Account: signed in on Pro with a month's spend by model.

Scenes (`--scene`, comma-separated): `new` (the empty state with the New
session composer), `session` (the worker at work: its provider session
filling the main area, the session's context in the sidebar above the
tree), `session-waiting` (the worker stopped at a permission prompt),
`pty` (the worker's pane through a real tmux client, over the e2e tests'
bridge to a live Claude Code: `e2e/pty.e2e.ts` drives it, and with
`&probe=1` `probe.ts` times it for its smooth-scroll test),
`session-paused` (a paused session's state card), `session-failed` (a
worker whose sign-in lapsed: its state card), `session-done` (the
session's goal reached: "Done · Close" on its sidebar row and Close
session in its goal), `chat-empty` / `chat-typing` / `chat-long` /
`chat-paste` / `chat-queued` / `chat-drop` (the supervisor's message box:
empty, a draft with two files and a screenshot, a 25-line draft at the
box's tallest, a 412-line paste as a chip with its preview open, two
messages queued behind a working Claude, and a screenshot dragged over
the session), `inbox` (the Inbox page as a queue, `shots/inbox.ts`:
Waiting on you with the first question open on its answer box, From the
team with a design's threads, a notice and sign-off dots, Done today
folded), and, not in the default list, `inbox-thread` (the thread open
with its reply box), `inbox-news` (nothing waiting, news only),
`inbox-clear` (all clear, Done today open), `inbox-loading` and
`inbox-many` (tabs by kind, one teammate's requests as one row), `agent-supervisor` / `agent-working` / `agent-waiting` /
`agent-done` / `agent-reviewer` / `agent-failed` (one scene per agent of
the session, each pane its provider's own in the state its row shows),
`agents` (how a task gets run, and a row per kind of work under the
org's policy), `agents-make` (Medium's model list open, the custom agent
builder under it),
`library` (the Library open from the foot's button: the sidebar as its
navigation, eight designs from `library.ts` as a list, Needs you pinned and
the rest by folder), `library-board` (the same designs on the board, a column
per stage), `library-decisions` (its Decisions
source: three decisions as cards),
`knowledge` (three decisions as cards, two document folders),
`knowledge-map` (Map on open: zero hosted docs in three scopes, local decisions, imported folders and indexed KB documents),
`knowledge-record` (the record form open), `knowledge-search` (a search
with a decision and two document passages found), `knowledge-empty` (a
repo with no knowledge yet: how it gets there), `knowledge-add` (the add
folder form open, with the picker), `settings` (the four
sections: this repo's settings, the person's own, keys and sign-ins as
rows, the engine and its health list with a pending update),
`settings-edit` (checks and workers at once edited, the save bar),
`settings-key` (a key being set, write-only), `settings-signin` (a
provider's browser-first sign-in card up), `settings-health` (the page
scrolled to its foot: the sign-in rows and the engine's health list),
`account` (signed in on Pro: the plan's card and the spend by model),
`account-team`, `account-enterprise`, `account-free` (signed in on the
free plan: the pricing page's plans compared, cost reports locked by the
engine) and `account-signed-out` (sign-in, the plans compared). The app's
own update, over a fake shell (not in the default list, so pass `--scene`):
`update-inbox` (offered: the strip over the Inbox), `update-downloading`
(Update now pressed, downloading), `update-later` (set aside: the sidebar's
pill) and `update-session` (the strip over a session). The chat pane on its
own (`shots/chatPane.tsx`, also only by `--scene`): `chat-pane` (Claude Code
idle, its prompt box rows cropped under brindle's message box) and
`chat-pane-permission` (a permission question: every row shown) and
`chat-pane-diff` (an Edit's diff as Claude Code's colour-diff draws it in
the theme's own colours, in truecolour and as 256-colour slots, then its
accents on the pane's field: what the pane's contrast floors act on). Themes
(`--theme`): `dark`, `light`.

## The design doc

`node shots/plan-shots.mjs --out <dir> [--channel chrome]` opens the plan in
`visuals.tsx` (a design doc with a diagram, three decisions, a suggestion
and comment threads, over a small in-memory engine) and takes it with its
review column open, the next decision's table, a highlight with its comment bar, the comment landed in
the column, the column collapsed to its strip, Suggesting with a tracked
change on the page, and signed off under its live board, in both themes.

## The Library and sharing review

`node shots/library-ux-shots.mjs [--out <dir>] [--only current|mockups] [--channel chrome]`
takes today's Settings › Library (the `settings-library*` scenes, `libraryUx.ts`: the defaults,
the location just switched, an org's enforced switches, an admin), the share panel's states and
a teammate's reader (`panels.tsx`), the Library and the Inbox into `<dir>/current`, and the
improved flows drawn in `library-ux.tsx` into `<dir>/mockups`, each in both themes. `<dir>`
defaults to `~/Documents/brindle-design-docs/library-ux`.

## The organisation settings mockups

`node shots/admin-settings-shots.mjs [--out <dir>] [--channel chrome]` takes the two layouts drawn
in `admin-settings.tsx` for an organisation's Library settings (one sheet; read first, change one at
a time), each as the admin's screen and the member's, in both themes. `<dir>` defaults to
`~/Documents/brindle-design-docs/admin-settings`.

## Timing the wheel over a live Claude Code

`npm run test:e2e -- --grep smooth` swipes a live Claude Code (the `pty`
scene, `&probe=1`) in WebKit and holds each redraw's wait for its paint to
a budget. `spike/pty/measure.mjs` times the same pane in more detail
(notches, swipes, typing, CPU) against a native tmux client's baseline.
