# Scrolling a session pane: why it lagged, and the fix

Scrolling a pane view with the wheel or the trackpad felt laggy next to
scrolling the same tmux session in a native terminal. This note records what
was measured, the root cause, and the numbers before and after the fix
(`src/components/terminalScroll.ts`, wired into `PtyTerminal`).

## How the wheel reaches tmux

`PtyTerminal` is xterm.js as a plain terminal on a real tmux client
(`src-tauri/src/pty_view.rs`). Its `scrollback` is 0: the history is tmux's.
The view session has `mouse on`, so tmux asks the terminal for mouse reports
(buttons, SGR). A wheel turned over the pane goes as wheel reports
(`ESC[<64;col;rowM` up, `65` down) to the tmux client. tmux then does one of
two things:

- **A pane whose program asks for no mouse** (a shell, or Claude Code's
  transcript on the normal screen): the first report enters copy mode
  (`copy-mode -e`). Each later report scrolls copy mode **5 lines**, tmux's
  stock `copy-mode` binding (`send-keys -X -N 5 scroll-up`). The view's key
  table is the session's default, so tmux uses that binding as it does under
  any terminal. Copy-mode tables are server-wide and stay untouched.
- **A program that asks for the mouse** (Claude Code's fullscreen renderer):
  tmux passes each report on, and the program scrolls by its own step.

Either way, the *terminal* decides how many reports a gesture makes. That
number is the only scroll input tmux gets.

## What was measured, and how

`e2e/scroll.e2e.ts` runs in Playwright's WebKit (WKWebView's engine) with
the WebGL renderer. It drives `PtyTerminal` over the e2e bridge
(`src-tauri/examples/pty_ws.rs`), which runs the same `PtyView` Rust as the
app's `pty_view_*` commands, against a private tmux server (`e2e/pty/rig.ts`).
The pane holds 4,000 lines of coloured history and asks for no mouse, so the
wheel goes through copy mode.

At every paint the test reads tmux's own copy-mode position (`[N/M]`, top
right). A paint that changes it moved the view. Gestures:

- **notches**: ten mouse notches, 300 ms apart. Each is WebKit's 40 px: on
  macOS a plain wheel's event is its line delta (1 for a notch) × WebKit's
  line step of 40 px (`WebEventFactory::createWebWheelEvent`).
- **spin**: a wheel spun, 14 notches 16 ms apart, with macOS's acceleration
  (40 px up to 400 px).
- **swipe60**: a trackpad swipe, one event a frame (82 events, ~1,000 px).
- **flick120**: a 120 Hz trackpad flick, two events a frame (182 events,
  ~1,800 px).

The metrics:

- **wheel → paint**: from each wheel event to the next paint that moved the
  view. A notch that sends nothing waits for a later one.
- **step → paint**: from the event that completed a step (its reports went
  out) to the paint that shows that step (tmux's position covers all
  reports so far). This is the round trip.
- **fps while scrolling**: paints that moved the view per second during the
  gesture's first 500 ms (the push and early momentum), and the longest wait
  between two of them.

Every number here was measured in this e2e rig, not in the app
(`npm run tauri dev`): a worker can't drive a real trackpad in the Tauri
window. The rig shares the app's component, `PtyView` Rust, tmux and WebKit
engine. It is not the app: its last hop is a localhost WebSocket where the
app has Tauri's IPC channel, and its wheel events are synthesized ones with
the pixel deltas WebKit gives a real mouse and trackpad. Scrolling in the app
itself is to be checked by hand.

## Real app, checked by hand

On 2026-10-22, Hannah Lerner checked a debug build of the app
(`tauri build --debug`, dev/desktop-app + this branch + fix/copy-on-select)
by hand: scrolling a long session pane with the trackpad felt smooth, like a
native terminal (Terminal.app / iTerm2). Nothing was timed in the app; the
numbers below come from the rig.

## Suspects, measured

| Suspect | Measured (before) | Verdict |
|---|---|---|
| Each step a round trip that redraws the whole screen | Step → paint p50 17 ms, p95 18–24 ms: about a frame. About 1.6–2.2 KB comes back per report. tmux scrolls the region and redraws the 5 new lines and its position, not the screen. | Not the cause |
| #215's batching in `pty_view::forward` holding output frames | One message per report (`messages` ≈ `reports`). None held: each redraw goes as one message well within the round trip above. | Not the cause |
| WebGL falling back to the DOM renderer | The view reports `data-renderer="webgl"`, and the test asserts it. Paints land within a frame of their bytes. | Not the cause here. If a WKWebView ever loses WebGL, `loadRenderer` retries, and `data-renderer` on the terminal says which renderer is in use. |
| One report per wheel event, no coalescing or acceleration | **Yes**, see below. | **Root cause** |

## Root cause

xterm.js 6 turns the wheel into mouse reports in
`CoreBrowserTerminal.bindMouse` and `CoreMouseService.consumeWheelEvent`:

1. **At most one report per wheel event**, however far the event goes. It
   works out the event's lines, and if that is at least one it sends a
   single report and throws the rest away. A spun wheel's 400 px event
   (25 lines of text) sends one report.
2. **Any pixel event under 50 px is taken for a trackpad and scaled by 0.3.**
   A Mac mouse's notch is 40 px in WebKit, so it counts as 0.77 of a line and
   the first notch sends nothing. Over ten notches, three sent nothing; each
   of those waited a notch (300 ms) to move the view. A trackpad needs about
   52 px of finger travel per report (16 px cells ÷ 0.3), so a swipe moved
   copy mode 5 lines at a time only a few times a second.

A native terminal counts the same gesture differently. iTerm2's
`iTermScrollAccumulator`, with `proportionalScrollWheelReporting` on by
default, sends one report per line of text the gesture covers: a trackpad's
`scrollingDeltaY / lineHeight`, with the fraction kept for the next event.
It sends at least one report per notch, more when macOS accelerates. Its
`fastTrackpad` mode is modelled on Terminal.app, which rounds the coarse line
delta away from zero. Ghostty does the same per line. So under iTerm2 the
same swipe makes about 3.5× as many reports, close to one a frame, and tmux
moves the view every frame.

So the lag was input starvation, not slow output. tmux answered every report
within about a frame, but it got too few of them, unevenly spaced, and the
first notch of a gesture got none.

## The fix

`terminalScroll.ts` (`scrollByWheel`) takes the wheel on its way into
xterm.js's element (a capture listener, so xterm.js never sees it) while the
terminal reports the mouse. It sends reports as a native terminal does:

- **Counting** (`WheelLines`): a trackpad's pixels give one report per line
  of text (the cell height as xterm.js lays it out), with the fraction kept
  for the gesture's next event. The fraction is let go after a 250 ms pause,
  or when the gesture turns the other way. A mouse's notch gives one report
  per line, at least one, as does a line- or page-mode delta. That matches
  iTerm2's counts. Mouse or trackpad is decided per gesture, not per event. A
  gesture is a mouse's while every event in it is a whole number of notches
  (WebKit's 40 px; 100 px on Windows' WebView2). Its first event that isn't
  makes it a trackpad's to the end, so a trackpad event that happens to be
  40 or 80 px is counted as pixels and keeps its fraction of a line.
- **Coalesced per frame**: a gesture's first reports go at once, so the
  first notch or swipe isn't held. After that there is at most one write per
  animation frame, carrying every report since. tmux reads them in one go
  and redraws once for them. No report waits on the one before (there is no
  per-event round trip), and a 120 Hz trackpad's two events a frame make one
  write.
- **Bounded**: one write carries at most a screen's rows of reports. Turning
  the other way drops what hadn't gone the first way, even when the turn is
  less than a line (judged by the event's own direction). Nothing is left to
  keep scrolling after the wheel stops.
- Reports are SGR-encoded as xterm.js encodes its own, with the modifiers
  held, at the cell under the pointer (still right with brindle's sidebar
  cropped). With no mouse reporting, the wheel is left to xterm.js.

`PtyTerminal.tsx` changes by three lines: the import, `scrollByWheel(term,
send)` beside the other input handlers, and its `dispose()`.

## Numbers

Playwright WebKit, WebGL, 1440×960, the plain pane in copy mode. Each run is
one pass of all four gestures. Re-runs gave the same numbers within a
millisecond or two.

Before: notches: 7 of 10 notches moved the view; wheel → paint p50 17 ms, p95 331 ms.
Before: spin: 14 reports for 14 events (65 lines); wheel → paint p50 17 ms, p95 19 ms.
Before: swipe60: 18 reports for 82 events (85 lines); first paint 79 ms after the first event; 30 fps while scrolling, longest stall 79 ms; wheel → paint p95 133 ms.
Before: flick120: 33 reports for 182 events (160 lines); 46 fps while scrolling, longest stall 35 ms; wheel → paint p95 167 ms.
Before: step → paint (round trip) p50 17 ms, p95 18–24 ms in every gesture.

After: notches: 10 of 10 notches moved the view; wheel → paint p50 16 ms, p95 27 ms.
After: spin: 74 reports for 14 events (365 lines, iTerm2's count for those deltas), one write each; wheel → paint p50 17 ms, p95 33 ms.
After: swipe60: 62 reports in 40 writes (305 lines); first paint 42 ms after the first event; 54 fps while scrolling, longest stall 42 ms.
After: flick120: 109 reports in 55 writes (540 lines); 58 fps while scrolling, longest stall 31 ms.
After: step → paint (round trip) p50 16–17 ms, p95 18–34 ms: unchanged, about a frame.

Notes:

- A trackpad's wheel → paint p95 stays high after the fix (100–200 ms). That
  comes from the momentum's last events, each under a pixel, which take many
  frames to make up a line. A native terminal accumulates them the same way.
  The fps figures are the ones that measure how the swipe feels.
- A gesture now covers about 3.5× the distance it did. That is the distance
  a native terminal covers under tmux's stock 5-line copy-mode step. Making
  it shorter would mean changing tmux's server-wide copy-mode table, which
  the view never does.
- "60 fps" is the most a 60 Hz page paints. Moves now land on nearly every
  frame of the busy part of a swipe.

To measure again:

    npx playwright test -c app/playwright.config.ts scroll.e2e --project webkit

It prints one `scroll <gesture> {...}` line per gesture and fails if a notch
doesn't move the view, if a notch or a step isn't painted within 50 ms, or if
a swipe moves the view at under 30 fps or stalls more than 70 ms while busy.
