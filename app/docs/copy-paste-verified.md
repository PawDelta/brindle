# Copy and paste in pane views: what was found, and the checks in the real app

Covers the session pane (`PtyTerminal` in `ProviderSurface`) and the Terminal
panel (`TerminalPanel`'s shell, also a `PtyTerminal`). Branch
`fix/copy-on-select`.

## What was found

Hannah Lerner checked these in the real app on 2026-10-22 (results below).
The Session pane copy-on-select still fails and is a follow-up. The worker that made this
change couldn't drive it: a debug build started from an agent session is
refused by the daemon ("the caller runs under brindle agent …; the daemon
serves the app and people, not agents"), and synthesizing clicks and keys
wasn't permitted in that session. Each result line below is to be filled in
from a check a person runs.

Headless, on real tmux 3.8 (an agent session; a view session linked to its
window with the view's key table, `brindle-view`; a client on a pty with
`TERM=xterm-256color` and the view's `-T` features; mouse input sent as the SGR
reports xterm.js makes; the server's own `set-clipboard external` and its
copy-mode `MouseDragEnd1Pane` binding):

- A plain drag over a plain pane (tmux copy mode, drag end
  `copy-pipe-and-cancel`): the copy comes back to the view's client as
  OSC 52, which the clipboard addon hands to `pty_view_copy`.
- A double-click on a word: comes back as OSC 52 too (tmux's stock binding
  waits 0.3 s, then `copy-pipe-and-cancel`).
- `printf … | tmux load-buffer -w -` from inside the pane (how a program
  with `$TMUX` copies through tmux, as Claude Code does): comes back as OSC
  52 to the view's client, even though that client is on the view session,
  not the agent's.
- The view's take-window (`switch-client` of its own client, sent on
  mousedown) at any point in a drag: the copy still comes back.
- **Root cause of lost tmux drag copies:** when the drag's last motion and
  its release reach tmux in the same read, tmux fills its paste buffer but
  sends no OSC 52. The copy never reaches the app's clipboard. The release
  copies (copy mode's drag-end `copy-pipe-and-cancel`) while the pane is
  still waiting to be redrawn for that motion, and tmux 3.8 drops the
  pane's clipboard write in that state. Headless, a drag sent as one write
  lost its copy 40 of 40 times. The same drag with its release in a write
  of its own copied 75 of 75 times, even with no delay between the writes.
  In the app, coalescing is likely: xterm.js's mouse reports arrive over
  IPC and are written to the client's pty, and a quick release can sit in
  the pty with the last motion before tmux's client reads it. An earlier
  note here called this a top-row or first-drag problem. That was a
  measuring artefact: those runs happened to send the release with the
  motion, and the ones that copied didn't.
- **Fix** (`pty_view.rs`, `write_input` and `RELEASE_GAP`): the view's writer
  sends each SGR mouse release (`ESC [ < b ; x ; y m`) at least 15 ms after
  the write before it, so tmux reads it on its own. A release long after
  the last write goes at once, and keys never wait. Regression tests on
  real tmux: `a_drag_released_with_its_last_motion_copies_as_osc_52`
  (three drags, each with its motion and release in one write, then a
  double-click in one write, all must come back as OSC 52; it fails without
  the gap) and `a_mouse_release_waits_behind_what_came_just_before_it`.
- **Diagnostics:** while debug logging is on (Settings, for staff), each
  OSC 52 that reaches xterm.js, each `pty_view_copy` and `pty_view_paste`,
  and why either failed go to the debug log. Only sizes are logged, never
  the text. `tauriCopy` and `tauriPaste` used to swallow failures. If a
  check below fails, the log shows which layer the copy or paste stopped
  at.

One more gap was certain. xterm.js's own selection (Option-drag,
Option-double-click; `macOptionClickForcesSelection`) only reached the
clipboard on ⌘C. Copy-on-select didn't exist for it.

## The change

`PtyTerminal` copies xterm.js's own selection onto the app's clipboard
(`copy`, so `pty_view_copy` and tauri-plugin-clipboard-manager, never
`navigator.clipboard`) when the mouse button that made it is released, even
off the view. Only the shown columns are copied when a sidebar is cropped.
Nothing is sent to the pane. tmux's selections already copy themselves on
release (OSC 52, above). ⌘C and ⌘V are unchanged.

Tests (`cd desktop && npx vitest run src/components/PtyTerminal src/components/TerminalPanel`):

- `PtyTerminal.test.tsx` › "select to copy: xterm.js's own selection … goes
  onto the app's clipboard as the mouse lets go, no ⌘C"
- `PtyTerminal.test.tsx` › "select to copy with columns cropped: only the
  shown columns …"
- `TerminalPanel.test.tsx` › "select to copy and ⌘V paste in the shell go
  through the app's clipboard, not the webview's" (the panel's real
  `PtyTerminal`)

All three fail with the mouseup listener removed.

## Session pane copy-on-select: root cause (branch `fix/session-pane-copy-on-select`)

The hand check above found a gap: in the session pane, a drag, double-click
or triple-click only reached the clipboard after Ctrl+C. In the Terminal
panel, selecting alone copied.

**Why the two panes differ.** Both are the same `PtyTerminal` on the same
kind of view, but different programs own the selection:

- In the Terminal panel the shell doesn't ask for the mouse
  (`#{mouse_any_flag}` is 0). tmux's stock bindings put a drag or
  multi-click into copy mode, and copy mode copies on release
  (`copy-pipe-and-cancel`, OSC 52 back to the view, `pty_view_copy`).
- In the session pane, Claude Code's fullscreen TUI asks for the mouse, so
  tmux forwards every press, motion and release to Claude Code (`send -M`).
  The highlight is Claude Code's own selection, not tmux's and not
  xterm.js's. Whether that selection copies on release is Claude Code's
  decision.

**What decides it.** Claude Code 2.1.308 (the binary installed here)
copies on release only while its global `copyOnSelect` setting is on (it
defaults to on). Its selection hook returns early when
`copyOnSelect` is false. With the setting off, Claude Code's footer shows a
"ctrl+c copy" hint, and its Ctrl+C handler copies the selection then. That
copy goes out through `pbcopy` on macOS, plus `tmux load-buffer -w` and
OSC 52. That matches the failure exactly: nothing on release, a copy on
Ctrl+C. The person's `~/.claude.json` had `"copyOnSelect": false`.
**Confirmed:** with it set to true, the user's hand check of a new session
on 2026-10-22 copied on selection alone, no ⌘C (result below). With
`copyOnSelect` false, Claude Code copies its selection only on Ctrl+C. ⌘C
can't reach it: the view sends tmux's copy key, and tmux has no selection of
its own.

**What reaches the view when Claude Code copies.** Inside tmux, Claude
Code 2.1.308 writes a copy three ways at once: `pbcopy` (macOS, straight to
the clipboard, the app never sees it), an OSC 52 of its own plus the same
wrapped for tmux passthrough (written to the pane), and
`tmux load-buffer -w -`. On real tmux (`pty_view.rs`
`a_programs_copy_through_tmux_reaches_the_view_and_its_own_osc_52_doesnt`),
only `load-buffer -w` reaches the view. It arrives as OSC 52 with no
selection named (`ESC ] 52 ; ; <base64> BEL`), which the clipboard addon
hands to `copy`. The pane's own OSC 52 is kept by tmux: the server's
`set-clipboard` is `external`, which takes no program's OSC 52. The
passthrough copy is kept too, with `allow-passthrough` off. The view still
gets the `load-buffer -w` copy with a person's terminal on the agent's own
session, as long as the view was used last: tmux sends it to the most
recently used client.

**The app's layers worked throughout.**

- tmux and the view: a program that asks for SGR mouse reports, as Claude
  Code does, received the whole drag on real tmux, release included, and a
  double-click's presses and releases. That held even with the view's
  take-window (`switch-client`, sent on mousedown) mid-drag. This was
  checked once on real tmux; the review asked for the test to be dropped,
  so it isn't kept.
- xterm.js: in mouse-reporting mode, its document-level `mouseup` listener
  reports the release, even off the view. `cancelEvents` is off, so nothing
  stops the event. `PtyTerminal`'s own mousedown and mouseup listeners only
  observe; they neither prevent nor stop the event. jsdom gives xterm.js no
  cell size, so it makes no mouse reports there, and a vitest couldn't
  show this without faking xterm.js internals.
- The clipboard write: Claude Code's Ctrl+C copy reaches the clipboard,
  and its copy-on-select uses the same function (`copySelectionNoClear`).
  The write path works; only the release trigger is switched off.

**"Copied" when a copy lands.** Once selecting copied, nothing in the app
showed it had. `PtyTerminal` now shows a small "Copied" label
(`role="status"`, top right of the view, out of the way of clicks) for
1.5 s (`COPIED_SHOWN`) on every copy with text that reaches the app's
clipboard. It shows only after the write succeeds: `tauriCopy` resolves once
`pty_view_copy` has written the clipboard and rejects when it fails, and a
failed write shows nothing (the reason goes to the debug log). That covers an xterm.js selection on release, ⌘C or Ctrl+C,
tmux's copy-mode OSC 52, and Claude Code's copy through
`tmux load-buffer -w`. It shows in the session pane and the Terminal panel
alike, since both use `PtyTerminal`. A copy that goes only through Claude
Code's `pbcopy` shows nothing, because the app never sees it. Inside tmux,
Claude Code always writes the `load-buffer -w` copy alongside, so the label
shows for its copies.

Tests (`cd desktop && npx vitest run src/components/PtyTerminal src/components/TerminalPanel`),
both failing without the label:

- `PtyTerminal.test.tsx` › 'says "Copied" over the view for a moment
  whenever a copy reaches the app's clipboard …': OSC 52 with mouse
  reporting on (Claude Code's copy through tmux), an xterm.js selection on
  release, the timer restarting on a second copy, and nothing shown for an
  empty copy.
- `PtyTerminal.test.tsx` › "says nothing for a copy the app's clipboard
  couldn't take …": a rejected write (OSC 52 with mouse reporting on, and a
  selection on release) shows nothing; a write that works shows "Copied"
  only once it resolves.
- `ptyView.test.tsx` › "logs each copy and paste … a copy says whether it
  landed": `tauriCopy` resolves on success and rejects when the plugin
  refuses.
- `TerminalPanel.test.tsx` › 'a copy in the shell says "Copied" in the
  panel …': tmux's copy-mode OSC 52 with the mouse reported, and a
  selection on release.

To check by hand: drag in each pane and watch for "Copied" at the view's
top right.

## Checks in the real app

Build the branch: `cd app && npx tauri build --debug --no-bundle`. Then
start `app/src-tauri/target/debug/brindle-desktop` from your own terminal
(not from inside an agent's tmux pane, or the daemon refuses it). Turn on
debug logging in Settings first, so a failure says where it stopped. Before
each check, put a marker on the clipboard: `printf MARKER | pbcopy`.

1. **Session pane: copy-on-select.** Open a session whose agent runs Claude
   Code. (a) Drag over a few words of Claude Code's output, release, and
   don't press ⌘C. Run `pbpaste`: it should print the dragged text. (b)
   Double-click a word, then run `pbpaste`. (c) Option-drag over text (the
   xterm.js selection), then run `pbpaste`. (d) Optional, for tmux copy
   mode: scroll up with the wheel on a plain shell pane, then drag; run
   `pbpaste`.
2. **Terminal panel: copy-on-select.** Open the Terminal (⌃\`) and run
   `echo copy-me-please`. (a) Drag over `copy-me-please` in the output,
   release, run `pbpaste`. (b) Double-click it, run `pbpaste`. (c)
   Triple-click the line, run `pbpaste`. (d) Option-drag, run `pbpaste`.
3. **Session pane: paste.** `printf 'pasted-into-claude' | pbcopy`, click in
   Claude Code's prompt, press ⌘V. `pasted-into-claude` should appear in
   the prompt, once, with no Enter sent.
4. **Terminal panel: paste.** `printf 'echo pasted-ok' | pbcopy`, click in
   the Terminal's shell, press ⌘V, then Enter. The shell should print
   `pasted-ok`, once.

Results (each line gets PASS or FAIL, and how it was checked):

- Session pane: copy-on-select PASS, by hand on 2026-10-22 in a new session, with Claude Code's `copyOnSelect` set to true: selecting alone copies, no ⌘C. Earlier the same day it was FAIL with `copyOnSelect` false: Claude Code copies its selection only on Ctrl+C then (root cause above).
- Terminal panel: copy-on-select PASS, by hand on 2026-10-22 in a debug build (dev/desktop-app + fix/copy-on-select + fix/scroll-smoothness)
- Session pane: paste PASS, by hand on 2026-10-22 in a debug build (dev/desktop-app + fix/copy-on-select + fix/scroll-smoothness)
- Terminal panel: paste PASS, by hand on 2026-10-22 in a debug build (dev/desktop-app + fix/copy-on-select + fix/scroll-smoothness)
