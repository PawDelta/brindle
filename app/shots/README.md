# Renderings

For the Settings sign-in comparison, run `node shots/settings-login.mjs before`
before editing the UI, then `node shots/settings-login.mjs after`. An optional
second argument selects an existing Chromium executable. This captures the
provider overview and active browser sign-in in both themes as
`shots/settings-login-{before,after}-{light,dark}[-signin].png`, using the
`settings-login` scene (Claude signed in, Codex signed out, others not installed).
The after capture also saves `settings-login-after-{light,dark}-keys.png`
with the optional API keys expanded.

A dev-only screenshot setup: the app over the in-memory fake daemon
(`src/test/fakeDaemon.ts`) with realistic fixtures, screenshotted as
full-page PNGs in both themes. Nothing here ships: `vite build` bundles
`index.html` only, and Playwright is fetched by `npx`, not a dependency.

```sh
npm ci                                   # once
node shots/shots.mjs --step 06-final     # shots/out/06-final/<scene>-<theme>.png
node shots/shots.mjs --step x --out /some/dir --scene sessions,inbox --theme dark
node shots/shots.mjs --step x --scene session-done --browser webkit   # in WebKit, as Tauri on macOS
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
session in its goal), `inbox` (the Inbox page:
every decision across sessions as banner cards, permissions, sign-ins,
plans, checks and notices acted on in the card, the rest going to the
session), `agent-supervisor` / `agent-working` / `agent-waiting` /
`agent-done` / `agent-reviewer` / `agent-failed` (one scene per agent of
the session, each pane its provider's own in the state its row shows),
`agents` (how a task gets run, and a row per kind of work under the
org's policy), `agents-make` (Medium's model list open, the custom agent
builder under it),
`knowledge` (three decisions as cards, two document folders),
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
engine) and `account-signed-out` (sign-in, the plans compared). Themes
(`--theme`): `dark`, `light`.

## Timing the wheel over a live Claude Code

`npm run test:e2e -- --grep smooth` swipes a live Claude Code (the `pty`
scene, `&probe=1`) in WebKit and holds each redraw's wait for its paint to
a budget. `spike/pty/measure.mjs` times the same pane in more detail
(notches, swipes, typing, CPU) against a native tmux client's baseline.
