# Terminal architecture: a real tmux client per view

## Status: the only terminal (desktop/drop-control-mode-terminal)

Every pane the app shows goes through a real tmux client on a pty
(`PtyTerminal`): the session's supervisor and each worker, a dead pane's
last screen ("Show what it left": the pane tmux keeps, remain-on-exit), and
a provider sign-in's CLI. The control-mode path is gone, app and engine
alike: `components/Terminal`, `api/terminal.ts`, `BRINDLE_PTY_TERMINAL=0`,
and the daemon's `WS /v1/terminal/{agent}` (brindle
feat/drop-control-mode-stream). The webview asks the shell whether the pty
view can run (`pty_view_status`: ready, or unusable with why).

- **Action terminals.** `POST /v1/providers/{p}/login` (and `keys set`)
  answer the pane (`target`) and tmux `server` of the CLI's window. The
  sign-in card reads the CLI through a view no one sees (`api/paneText.ts`:
  an unopened xterm.js parses the client's redraws, and the screen is read
  back; a row drawn to its last column runs on, so a long link reads
  whole). tmux's "Pane is dead" on that screen, or the client exiting, is
  the CLI's end. "Show details" is a `PtyTerminal` on the same pane.
- **No pane to show.** In a browser tab (no shell), on Windows with an
  engine in WSL that has no `/v1/pty_view` (its `/v1/meta` doesn't say
  `pty_view`), with a tmux too old, or for an agent the engine names no
  `window` for, the surface's card says why, with "Check again".
- **Dead panes.** Nothing streams "no pane" any more: the liveness sweep
  (`liveness` on the event stream) is what turns a dead agent's surface
  into its card.
- **The seam** (`api/ptyView.ts`): `PtyViewOpener` (open, write, resize, and
  bytes, exit and error back, then close). The webview has one
  implementation, the shell's `pty_view_*` commands. On macOS and Linux the
  shell runs the tmux client itself; on Windows (`src/pty_bridge.rs`) the
  engine's daemon in WSL runs it and the shell relays its WebSocket
  `/v1/pty_view` (the daemon token first, then `{"server", "target", "cols",
  "rows"}`, then `examples/pty_ws.rs`'s frames) onto the same events. The
  e2e tests reach the same Rust through `examples/pty_ws.rs`.
- **tmux 3.2 or newer.** Both the tmux the view runs (the bundled one, else
  PATH in a dev build) and the server it attaches to must be 3.2 or newer.
  An older one shows "<provider> can't be shown" on the surface's card,
  saying which tmux is too old, with "Check again". The shell is asked again
  without a reload:
  - at start;
  - when setup reaches ready (a first run may just have installed tmux);
  - each time a surface on that card is opened;
  - on "Check again".
- **A client of the server's version.** A tmux client attaches only to a
  server of its own version: 3.7's fails on 3.2's with "open terminal
  failed: not a terminal". When the agents' server runs a tmux other than
  the app's, the view's client is that server's own executable, found from
  its pid and checked by its `-V`. If it can't be found, the view refuses
  and names both versions. With the bundled engine both are the bundled
  tmux.
- **Only the view session changes.** The prepare command sets these session
  options on `brindle-view-<pid>-<n>`, and nothing else on the server:
  - `status off`, `prefix None`, `prefix2 None`, `mouse on`;
  - `key-table brindle-view`;
  - `destroy-unattached on`, set by the client once it is attached.
  It sets no `-g` or `-s` option, no option of the agent's session or window
  (no `window-size`: tmux's default `latest` applies), and touches no other
  key table.
- **The person's root-table bindings.** The view's client looks up keys in
  `brindle-view`, not `root`, so `bind -n` lines in a config the server loaded
  never capture keys typed in the app. That table holds tmux's *stock* pane
  mouse bindings (`MOUSE_BINDINGS`), chosen by the server's version:
  - every server from 3.2 gets tmux 3.2a's own six: select, drag and
    double/triple-click to copy, the wheel's copy mode, middle-click paste;
  - a 3.7 or newer server also gets 3.7's wheel for a full-screen program
    without the mouse, and the scrollbar keys, which an older server would
    refuse, failing the whole view;
  - left out at every version: the menus, borders, status line and swaps,
    which reach other panes or kill and split them;
  - and at every version, ⌘C's key (`COPY_BINDING`, C-M-S-F12): tmux copies
    its copy-mode selection, if any, without clearing it; and Ctrl+C's on
    Windows and Linux (`COPY_OR_INTERRUPT_BINDING`, C-M-S-F11): tmux copies
    its selection and leaves copy mode, or with none sends C-c to the pane.
- **Copy and paste.** The client declares the `clipboard` feature, so tmux
  hands its copies to xterm.js as OSC 52. xterm's clipboard addon passes them
  to `pty_view_copy`, which writes through Tauri's clipboard plugin. The
  webview itself has no clipboard permission. A program asking to *read* the
  clipboard gets nothing. A mouse release is written to the client at least
  `RELEASE_GAP` after the write before it: read together with a drag's last
  motion, tmux copies the drag into its buffer but sends no OSC 52 (the pane
  still waits to be redrawn). An xterm.js selection is copied as the mouse
  lets go (copy on select). A plain drag is tmux's (copied at the drag's end) or
  the program's own (Claude Code copies on release, through tmux's buffer).
  Option-drag selects in xterm.js. ⌘C copies xterm.js's selection, or sends
  ⌘C's key so tmux copies its own; on Windows and Linux, Ctrl+C copies
  xterm.js's selection, else sends Ctrl+C's key (tmux's selection, or ^C).
  Each ⌘V has one way to paste, chosen before the key does anything: on a
  Mac, the webview's own paste (Tauri's default Edit menu, which macOS
  doesn't prompt for; one with no text reads `pty_view_paste`); elsewhere
  the key is taken and `pty_view_paste` read. Every webview paste is pasted
  once, a late one when it comes. `set-clipboard` is left as the server has it, so a
  program's own OSC 52 reaches the app only if the server allows it.
- **Cleanup.**
  - The view session goes when the view closes, the page reloads, or the app
    quits (`RunEvent::Exit`).
  - If the app crashes, the client dies with its pty and `destroy-unattached`
    takes the session.
  - A view session an app left before its client attached (its pid is gone)
    is killed when the next view opens on that server.
  - A view still opening when its page goes (a reload) or closes it isn't
    registered. `pty_view_open` is synchronous, like `daemon_stream_open`: it
    claims the id and the page generation as the ask is dispatched. The open
    then runs on its own thread, and on registration, under the lock, a stale
    generation or a dropped claim drops the view, its session and client
    with it.
- **A view that can't open** (the agents' tmux too old, its server gone)
  shows "<provider> can't be shown" and why, with "Check again". That reopens
  the view and asks the shell again. There is no Restart: the problem is the
  view's, not the agent's.

Tests (titles with "pty"):
- `cargo test --manifest-path app/src-tauri/Cargo.toml pty_view`. The
  server's version is faked for a newer client on an older server. With
  `BRINDLE_TEST_OLD_TMUX=<a tmux 3.2a binary>` a real 3.2 server is driven
  by today's tmux too.
- `npm --prefix app run test:e2e -- --grep pty`: WebKit, against a live
  Claude Code on a private tmux server with a private HOME and config, and no
  API (`e2e/pty/rig.ts`).

The rest of this document is the spike's record.

---

SPIKE on `spike/pty-tmux-client`, not for merging as is. It asks whether the
app should stop streaming panes through the engine and attach a real tmux
client in a pty, as a person's terminal does. The numbers below come from one
rig that times a live Claude Code three ways.

## Recommendation

**Yes: show a pane through a real tmux client on a pty owned by the Tauri
shell, and delete the control-mode stream.** Over two runs it's much faster
where the current path hurts, and no slower anywhere:

- **Trackpad swipe:** bytes reach the screen in 9–11 ms (median) instead of
  75–176 ms, and in 22–29 ms instead of 676–987 ms at the 95th percentile.
- **Wheel notch:** paints in 11–12 ms instead of 28–40 ms.
- **Typing:** a tie at the median (13–18 ms against 17–23 ms).
- **CPU:** the same within noise.

The pty view is within about one frame of what a native terminal is handed
(the tmux client's first byte: 6.5 ms for a notch, 2.5 ms for a key).

**The swipe gap can also be closed inside the current design.**
fix/smooth-scroll's client-side fix holds each synchronized update until it
ends. f0863629 reports it brings the control-mode path to bytes→paint p50
9–16 ms live, about 25 ms p95 on replay. That's their measurement, not
this rig's.

So the case for the pty client is not speed alone. It's that the same
latency comes for free, as a side effect of letting tmux be the terminal,
instead of from one more piece of stream surgery:

- the 2026 hold-and-release, whose review found two edge cases of its own
- the seed and resync
- wheel-report synthesis
- focus sizing

With the pty client, each of those is deleted rather than maintained.

It also deletes about 2,000 lines of our own terminal semantics on the engine
and app side, plus about 1,800 lines of their tests and fixtures. Scroll,
mouse, modes, multi-client sizing, seeding and resync all become tmux's job,
done the way the owner already sees them in their own terminal.

Ship it in this order:

1. **The pty lives in Tauri Rust** (`src-tauri/src/pty_view.rs`, which reuses
   the `openpty` code the first run's installer already has; no new crate).
   It doesn't belong in the engine: the measured win comes from tmux drawing
   to a real client. Adding back a Python hop and the shell's polled stream
   bridge would add back latency (see "What the current path costs"). The
   engine keeps one job: saying where an agent's pane is. It already does,
   in `AgentDTO.window` and `tmux_server`.
2. **One view session per open view**, made with `link-window`, not a grouped
   session (see "Attaching to one agent's pane").
3. **The status line, prefix and mouse are set on the view session only**
   (`status off`, `prefix None`, `mouse on`). tmux allows it: they are session
   options, so a person's terminal attached to the agent's session keeps its
   own.
4. **Windows/WSL: the engine grows a raw-bytes pty endpoint** (`tmux attach`
   in a pty inside WSL, bytes over the existing socket, about 100 lines),
   rather than ConPTY around `wsl.exe`. It's the one place a pty can't live in
   the shell.
5. Before the default flips: copy/paste under tmux's mouse (OSC 52 plus
   xterm's clipboard addon), a check that agent tmux servers don't load the
   person's `~/.tmux.conf` root-table bindings, and tmux ≥ 3.2 enforced (see
   "Risks").

## What was measured

Rig: the spike's `spike/pty/rig.py` and `measure.mjs` (prototypes, not in
this repo). They are adapted from
fix/smooth-scroll's `scroll-rig.py` and `measure-scroll.mjs` (worker
f0863629) so the two branches' numbers line up: the same swipe profiles, the
same probes (`probe.ts`), the same native baseline.

- A tmux server of the rig's own (`-L ptyspike-<pid>`, `-f /dev/null`, never
  the default server). Claude Code (haiku) runs in it as one of the daemon's
  action terminals, with a ~400-line markdown and code reply on screen. It is
  on the alternate screen and asks for any-event SGR mouse reports.
- **current**: the app's `Terminal` against the real engine daemon
  (`brindle.daemon.app`, imported unchanged and instrumented), over WS
  `/v1/terminal/{id}`.
- **pty**: `PtyTerminal` against `examples/pty_ws.rs`, a localhost WebSocket
  around the same `PtyView` Rust that the app's `pty_view_*` commands use.
  That makes it pty → Rust reader thread → page. In the app the last hop is
  Tauri's IPC channel instead of a WebSocket.
- **native**: a `tmux attach` client in a pty of the pane's size, fed the same
  wheel reports and keys. Times are to the client's first and last byte back,
  which is what a terminal gets to draw. The terminal's own paint (about one
  frame) is not included.
- Both browser paths run in Playwright's WebKit (WKWebView's engine), at
  1200×800, with the WebGL renderer.
- **Phases**:
  - 10 single wheel notches, 700 ms apart, one report each.
  - Trackpad swipes, an event a frame: up, down, then a 120 Hz flick up and
    down.
  - Typing: 10 letters, then 10 backspaces, into Claude Code's prompt,
    400 ms apart.
  - CPU per phase from `ps` cumulative TIME for each process group: WebKit's
    processes, the engine (daemon plus its control-mode client), the bridge
    (plus its tmux client), the tmux server, and Claude Code.
- Paint time comes from xterm's `onRender`. "Bytes→paint" is each frame
  received during the swipes timed to the next render (f0863629's paintwait).
  It separates our latency from Claude Code's own pauses.

Raw results were kept with the rig (`spike/pty/results/run{1,2}/`:
`summary.json`, and per path `current.json`, `pty.json`, `native.json`). Run 1
measured current, then pty. Run 2 measured pty, then current, each on a fresh
Claude Code. The bridge still builds from here:

    cargo build --release --example pty_ws --manifest-path app/src-tauri/Cargo.toml

### Results

Milliseconds, p50 / p95, run 1 · run 2. The native column times the tmux
client's bytes, not a paint.

| | current (control mode) | pty client (prototype) | native terminal |
|---|---|---|---|
| Wheel notch → first byte | 25 / 473 · 12 / 16 | **1 / 117 · 1 / 1** | 6.5 / 12 · 6.6 / 13 |
| Wheel notch → first paint | 40 / 476 · 28 / 56 | **12 / 118 · 11 / 16** | — |
| Wheel notch → last paint (native: last byte) | 40 / 560 · 28 / 56 | 24 / 144 · 30 / 58 | 11 / 17 · 17 / 24 |
| Notches with no reply within 700 ms | 3 of 9 · 0 of 10 | 1 of 10 · 0 of 10 | 0 · 0 |
| Key → first byte | 16 / 209 · 8 / 49 | **4 / 32 · 3 / 26** | 2.7 / 73 · 2.4 / 7.5 |
| Key → first paint | 23 / 215 · 17 / 50 | 13 / 33 · 18 / 45 | — |
| Keys with no reply within 400 ms | 4 of 20 · 0 of 20 | 0 · 0 | 0 · 0 |
| Swipe: bytes → paint | 176 / 987 · 75 / 676 | **11 / 22 · 9 / 29** | — |
| Swipe: bytes → paint, max | 1509 · 823 | **38 · 45** | — |
| Swipe up: renders, render gap p50 | 5, 169 · 22, 34 | **26, 19 · 27, 19** | — |
| Flick down: renders, render gap p50 | 2, 1586 · 2, 667 | **32, 18 · 21, 17** | — |

The no-reply rows count samples whose answer came after the next sample went
out. In run 1 the current path's misses were its first three notches and
first four keys (answered 473 ms and 209 ms late). In run 2, where it was
measured second, it had none. They may be first-contact effects of the
current path (its seed and resize on connect) and not steady state. The
medians above don't depend on them.

CPU-seconds per phase, run 1 · run 2. Each phase lasts about the same wall
time on every path (singles ≈ 8 s, swipes ≈ 38 s, typing ≈ 9 s).

| phase | path | WebKit | engine | bridge | tmux server | Claude Code |
|---|---|---|---|---|---|---|
| swipes | current | 4.64 · 6.52 | 0.42 · 0.47 | — | 0.08 · 0.12 | 2.08 · 2.21 |
| | pty | 5.26 · 4.90 | 0.23 · 0.09 ¹ | 0.04 · 0.04 | 0.08 · 0.12 | 1.62 · 1.40 |
| | native ³ | — | — | 0.00 ² | 0.05 · 0.10 | 1.14 · 0.94 |
| typing | current | 1.36 · 1.00 | 0.09 · 0.08 | — | 0.01 · 0.01 | 0.49 · 0.92 |
| | pty | 1.15 · 1.07 | 0.05 · 0.02 ¹ | 0.00 · 0.00 | 0.01 · 0.01 | 0.35 · 0.11 |
| | native | — | — | 0.00 ² | 0.01 · 0.02 | 0.44 · 0.26 |

Notes:

1. The daemon still runs in the rig, idle; on the pty path nothing streams
   through it.
2. The tmux client process itself.
3. Replaying the pty path's swipes (fewer reports; see below). Replaying the
   current path's: tmux 0.15 · 0.16, Claude Code 2.49 · 2.34.

What the numbers say:

- **The swipe is where the current path breaks down.** Claude Code wraps each
  redraw in DEC 2026 synchronized output. The control-mode stream splits it
  into %output pieces, coalesced 4 ms at a time. So xterm.js is mid-update at
  almost every frame, and holds its render until the update ends or its 1 s
  timeout fires. f0863629 found the same thing and is fixing it client-side on
  fix/smooth-scroll. With a real client, tmux redraws its own screen and hands
  it out whole, so nothing is held: bytes are painted within a frame or two
  (p95 22 ms). No fix is needed: the problem doesn't arise.
- **The pty path sends fewer, larger wheel reports than the current path.**
  xterm.js sends one report per wheel event, and scales trackpad-sized pixel
  deltas by 0.3 (18 reports for the swipe-up, against 61 from the app's own
  wheel code). That is xterm.js's behaviour as a plain terminal. Claude Code
  scrolls by its own step either way. `scrollSensitivity` tunes it if the
  distance per swipe should match the current one.
- **Typing is a tie at the median in the browser rig.** The current path
  sends every key as a `send-keys` command through the daemon's control
  client. The pty path writes the byte to the client's pty, which is all a
  terminal does. The bytes come back sooner on the pty path (3–4 ms vs
  8–16 ms), but the paint dominates. In the app, the current path also pays
  the shell's stream bridge (next section); the rig doesn't measure that.
- **CPU is the same within run-to-run noise.** WebKit draws more frames on
  the pty path (it isn't starved) for about the same CPU. Each tmux client
  costs the server about one more redraw per update: under 0.1 CPU-seconds
  over a 38-second swipe phase.

### What the current path costs in the app, not measured here

The rig's current path talks to the daemon's WebSocket directly. In the app
it goes webview → Tauri → `daemon.rs` → Unix socket → daemon. `run_stream`
holds the stream's lock while it reads with a 40 ms timeout
(`STREAM_POLL`). A control frame (a key, a wheel report) waits for that read
to time out before `send_stream` can write: up to 40 ms more per key when the
pane is quiet. The pty view's writes go through their own channel and never
wait on a read.

## How the prototype works

Dev flag: build the webview with `VITE_PTY_TERMINAL=1` and start the app with
`BRINDLE_PTY_TERMINAL=1`. A session's provider surface
(`screens/Session/ProviderSurface.tsx`) then shows `PtyTerminal` for an agent
whose `window` and `tmux_server` are known, instead of `Terminal`. Without
both flags nothing changes, and the Rust commands refuse.

| File | Lines | What |
|---|---|---|
| `src-tauri/src/pty_view.rs` | 290 + 120 tests | Makes the view session, runs `tmux -T <features> attach` on a pty (`setup::spawn_terminal_sized`), and passes bytes both ways. Commands: `pty_view_open`, `_write`, `_resize`, `_close`. Attaches to a named, socket-path or default tmux server. |
| `src/api/ptyView.ts` | 113 | The transport: Tauri commands and a channel, or a WebSocket to the rig's bridge. |
| `src/components/PtyTerminal.tsx` | 72 | xterm.js as a plain terminal: fitted to the view, `scrollback: 0`, `onData` / `onBinary` → write, `onResize` → resize. |
| `src-tauri/examples/pty_ws.rs` | 97 | The rig's bridge: the same `PtyView` behind a localhost WebSocket. |
| `spike/pty/` (not in this repo) | | The rig: `rig.py`, `measure.mjs`, `probe.ts`, `rig.html`/`rig.tsx`, and `results/`. |

Tests:
- `cargo test --lib pty_view` attaches a real client to a tmux server of the
  test's own. It checks the view draws the agent's pane, has no status line
  (while the agent's session keeps tmux's default), and takes the view's size,
  that a key reaches the pane, that a resize reaches the window, and that
  closing the view leaves the agent's window in its session.
- A second test runs a native client of another size alongside the view (see
  the sharing results below). Others cover the refusal of the default server.
- `cargo test --example pty_ws` covers the rig bridge's refusals.
- `PtyTerminal.test.tsx` checks the client's bytes are drawn raw and xterm's
  input (keys, escape sequences, mouse reports) goes out raw.

### Hiding the status line, for the app's client only

tmux allows it. `status` is a session option, so it's set on the view's own
session (`set-option -t =<view>: status off`). The prototype also sets these
on the view session:

- `prefix None` and `prefix2 None`: every key reaches the provider. C-b no
  longer opens tmux's command prefix in the app.
- `mouse on`: tmux passes the wheel to a provider that asks for mouse reports
  (Claude Code). On a plain pane it scrolls tmux's history in copy mode.
- `destroy-unattached on`: the session goes when its client does. It's set
  after attaching, by the client's own command list (`attach ; set-option`):
  set on a session with no client yet, it destroys the session at once.

A person's terminal attached to the agent's own session keeps its status
line, prefix and mouse setting: the test asserts this. The fallback options,
had tmux not allowed it, were `status-format` tricks or cropping the last row
in xterm.js. Neither is needed.

### Attaching to one agent's pane: link-window, not a grouped session

- **`link-window` (chosen).** The view session is
  `new-session -d -s brindle-view-<n> … cat`, then
  `link-window -s <agent window> -t =<view>:`, then
  `kill-window` of the placeholder. All of it runs as one tmux command list.
  The session holds exactly one window, the agent's, so `next-window` and the
  like can't reach another agent. Killing the view session unlinks the
  window; the window stays in the agent's session, which the test checks.
- **A grouped session per view** (`new-session -t <agent session>`) shares
  *all* of the agent session's windows. The view could step to other agents'
  windows, and the view session's current window would need managing. No
  advantage here.

### The window's size, shared with a native terminal of another size

The view sets `window-size latest` on the agent's window. A daemon's
`resize-window` had left it `manual`. This is tmux's own rule between two
terminals: the window takes the size of the client used last (typed into,
clicked, scrolled). The other client sees it letterboxed with tmux's fill, or
cropped and panned to the cursor. That's what the owner sees today with two
terminals on one session. It replaces the engine's focus/blur arbitration
(`Pane.focus`, `blur`, `_unsize`, `request`, `SHARED_SECONDS` polling) and the
app's focus frames.

**Observed with both attached at once.** `cargo test --lib pty_view::tests::a_view_shares_the_pane_with_a_terminal_of_another_size`
runs this against a tmux server of its own. The agent's window starts at
100×30. The view is 90×25. A native client, standing in for a person's
terminal, is attached to the agent's own session at 120×40, with its status
line on.

| Step | Window | Notes |
|---|---|---|
| View attaches | 90×25 | |
| Terminal attaches (both now attached) | 120×39 | The terminal's status line takes a row. `list-clients` shows the view's client at 90×25 in its own session and the terminal's at 120×40 in `agents`. The view's session has `status off`; the terminal keeps tmux's default `on`. |
| Type in the view | 90×25 | |
| Type in the terminal | 120×39 | |
| View resized to 70×20 | 70×20 | A resize counts as using the client, as resizing a terminal window does. |
| Type in the terminal | 120×39 | |
| Type in the view | 70×20 | |
| Type in the terminal | 120×39 | |
| Terminal detaches | 70×20 | The window goes back to the remaining client, the view. |
| View closes | — | The agent's window is still in its session. Every key from both clients reached the pane in order. |

While one client has the window, the other client shows it as tmux does
under any terminal:
- the smaller client sees the larger window cropped, panned to follow the
  cursor;
- the larger client sees the smaller window with tmux's fill around it.

One consequence for the app: a view resize (a window drag, a sidebar
toggled) takes the window from a person's terminal, exactly as resizing a
terminal would. With `window-size latest` there's no "only while focused"
rule, which today's focus arbitration provides. The pty view could approximate
that by sending its resize only while the app is focused, and replaying the
last one on focus: about 10 lines in `PtyTerminal`, not done here.

**Focus and clicks count as using the view.** tmux counts only a key, a
mouse report or a resize as using a client (and focus, which it hears only
with the global `focus-events` on), so a view a person had only clicked
into stayed at the terminal's size until they typed. `PtyTerminal` calls
the view's `focus()` when xterm.js takes focus (the app's window coming to
the front too) and on any mousedown in it (a click on a view that already
has focus fires no focus event); the shell's `pty_view_focus` runs
`switch-client -E -c <the view's client> -t =<its own session>`. That marks
the client the window's latest and resizes the window (3.2's
cmd-switch-client.c sets `w->latest` and calls `recalculate_sizes`; later
tmux does both in `server_client_set_session`). Nothing reaches the pane,
and nothing but the view's own client is touched: no option, hook or key
table, no `window-size manual`. `refresh-client` never sets `latest`, and
`select-window` only for the client that runs it, which from outside is no
attached one. Typing in the person's terminal takes the window
back as before. `pty_view::tests::focusing_the_view_gives_it_the_window_*`
(PATH's tmux, the bundled 3.8 via `BRINDLE_TEST_BUNDLED_TMUX`, a real 3.2a
via `BRINDLE_TEST_OLD_TMUX`) and the e2e test "the view focused or clicked
has the window" check it, with the pane recording every byte it is sent.

The "shared with a terminal" badge goes away. If it's still wanted,
`#{session_attached}` on the agent's session would say it.

## What would be deleted (rough; done: see Status)

Engine (brindle repo, `src/brindle/daemon/terminal.py`, 895 lines, 18
commits). The parts that go:

- `Ring`, `Viewer`, `Reply`
- `Control`: the control-mode client, `%output` parsing, command replies
- `Pane`: seed (`capture-pane` + RIS + cursor), the focus and blur sizing,
  resize debouncing, shared polling, the capture-pane polling fallback
- `Hub`, `_stream`, `_coalesce`, `_flush`, `_control`, `_apply`
- `_redraw`, `_cursor`, `_modes`, `_size`

That's about 700 lines. What stays: `_find` / `_find_action` (where a pane is),
`open_action`, `send_input`, `_leftover` / `_as_terminal` (a dead pane's last
words), and the no-pane answer, about 190 lines. Tests:
`tests/test_daemon_terminal.py` (522) and `tests/test_terminal_fidelity.py`
(180) mostly go. Some `tmux.py` helpers used only by the stream (`control_env`,
the seed's capture options) go with them.

App (this repo):

| What | Lines |
|---|---|
| `src/components/Terminal.tsx`: wheel → report synthesis, SGR tracking, resync scroll-back, focus/blur, the pane-size-not-view-size rule, `reveal` | ~440 → replaced by `PtyTerminal` (72) plus a small static view for a dead pane's last screen |
| `src/api/terminal.ts`: `openTerminal`, key-name mapping (`KEYS`), xterm-reply filtering (`REPORT`), control frames | ~213 → ~30 (types for the no-pane answer) |
| `src/components/Terminal.test.tsx` | 198 |
| `e2e/scrollback.e2e.ts`, `src/test/recording.ts`, `scripts/record-pane.mjs` | 90 + 97 + 217 |
| `src/test/fixtures/claude-code-{live,wheel}.json` | 125 KB of recorded control-mode frames |
| `src/test/fakeDaemon.ts`'s terminal socket (hello, seed, resync, control frames) | ~250 |
| fix/smooth-scroll's 2026 hold-and-release (client-side) | not needed: tmux hands a real client whole redraws |

Roughly 2,000 lines of logic and 1,800 lines of tests and fixtures go,
against about 500 added (prototype size, before hardening). The Tauri
`daemon_stream_*` bridge stays: the event stream uses it.

## Risks

- **A pty and a tmux client per open view.** Each costs a process, a pty, two
  threads in the shell, and a redraw per update in the tmux server (measured:
  noise). Views exist only while a session is on screen, and the
  `on_page_load` hook closes them all on reload. Leaks are bounded by
  `destroy-unattached`: if the shell dies, its clients die with the pty, and
  their view sessions go with them.
- **tmux versions.** Needs `-T` (client features, 3.2+), `window-size latest`
  (3.1+), `prefix None`, `link-window`, and the `=session:` target form. The
  macOS app bundles tmux 3.8. On Linux, Ubuntu 22.04 ships 3.2a (fine) but
  20.04 ships 3.0a (too old): enforce ≥ 3.2 in `brindle doctor`, or bundle
  tmux on Linux too. Without `-T`, tmux guesses from `TERM=xterm-256color` and
  24-bit colour falls back to 256 colours.
- **The person's tmux config.** `prefix None` stops prefix bindings, but
  root-table bindings (`bind -n …`) in a config the agents' server loads
  would still capture keys typed in the app. The view can't override those
  per session. Check what config brindle's servers start with (a private
  `-f`, or none) before shipping.
- **Copy and paste.** With tmux's mouse on, a drag selects in tmux's copy
  mode and copies into tmux's buffer, not the system clipboard. Needs
  `set-clipboard on`, plus OSC 52 handled in xterm.js (`@xterm/addon-clipboard`
  or a handler writing through Tauri's clipboard). xterm.js's own selection is
  still there with Option held (`macOptionClickForcesSelection`). Copy mode's
  look (position indicator, highlight) is tmux's, as in a terminal.
- **History lives in tmux or the provider, not in xterm.js.** `scrollback: 0`
  is deliberate: otherwise lines tmux scrolls off a full-screen pane would pile
  up in xterm's scrollback, out of step with tmux. xterm-side search over
  history would have to go through tmux (`capture-pane`).
- **Windows/WSL.** The shell can't `openpty` a Linux tmux. The options:
  - ConPTY around `wsl.exe -- tmux attach`. ConPTY is a terminal of its own:
    it re-renders output and has mangled mouse input; its passthrough mode
    needs recent Windows 11.
  - (recommended) The engine runs `tmux attach` in a pty inside WSL and
    streams raw bytes over its existing socket. That's about 100 lines, with
    no parsing. It needs the `daemon.rs` stream fix above, or that path keeps
    the 40 ms.
  - The same engine endpoint also serves a browser front end, which has no
    pty either.
- **The ToS rule that provider TUIs stay unmodified.** The pty path is the
  stricter of the two. The provider's output reaches the person exactly as
  tmux renders it to any terminal: nothing seeded, reset, filtered or
  synthesized by us. Input is the person's own keystrokes and mouse reports,
  byte for byte. The current path synthesizes wheel reports and filters
  xterm's replies. What we change is tmux's chrome on our own session (status
  line, prefix), never the provider's screen.
- **Security of the commands.** `pty_view_open` takes a server and target from
  the webview. The prototype validates both. Production should take an agent
  id and have the shell resolve the window through the daemon, so the webview
  can't name a pane at all.
- **The default tmux server is attached to like any other.** Agents with no
  `tmux_server` live there, beside a person's own sessions. No server or
  `default` in any case (macOS's file system ignores case) attaches with no
  `-L`/`-S`; a socket path, the default one's included, with `-S` to it; any
  other valid name with `-L`. A name that isn't one (a space, `;`) is
  refused. The view's footprint is the same on every server: its own
  `brindle-view-*` session (`destroy-unattached`) and key table, nothing
  global, and stale view sessions of an app that died are cleared there too.
  A test runs a default server of its own (a `TMUX_TMPDIR` of the test's) and
  never touches the person's.
- **The rig's bridge** (`examples/pty_ws.rs`) is a localhost WebSocket that
  types into panes. Any open web page could otherwise reach it, so:
  - it listens on a free port;
  - it accepts only the rig page's Origin (403 otherwise);
  - it requires a random per-run token in the first frame;
  - it serves only the rig's own tmux server, named on its command line.
  `cargo test --example pty_ws` covers the refusals.
- **Lost or moved features.** The "shared with a terminal" badge (could come
  back from `#{session_attached}`). Action terminals (sign-ins) would move to
  the same view. A dead pane's last screen stays an engine answer
  (`_leftover`), drawn statically.
