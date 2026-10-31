/* A tmux pane as a terminal shows it, through a real tmux client on a pty
   (api/ptyView.ts, docs/terminal-architecture.md): an agent's window, or
   an action terminal's (a sign-in's details). xterm.js is a plain
   terminal here: fitted to the view, its bytes from the client as they
   come, its own input (keys, pastes, mouse and wheel reports) back as it
   makes it. tmux does the rest, as under any terminal: the provider's mouse
   reports and modes, scrolling a plain pane's history (copy mode, on the
   wheel), and the window's size among the clients that share it (the
   view opened, focused or clicked, or the app brought to the front with
   it on screen, counts as used, as typing in it does). No
   scrollback of xterm's own: the history is tmux's, or the provider's.

   Drawn with the WebGL renderer, or xterm.js's own DOM renderer where WebGL
   is missing or its context is lost.

   Copy, as a terminal does by default (Ghostty's, iTerm2's): an OSC 52
   write (what tmux copies, a drag or double-click in copy mode, or what a
   program copies through tmux, as Claude Code copies its own drag's
   selection with `tmux load-buffer -w`; a program's own OSC 52 tmux keeps)
   goes through xterm's clipboard addon to `copy`, the app's clipboard. Each
   copy with text in it shows "Copied" over the view for a moment
   (COPIED_SHOWN), once the clipboard has it. A program asking
   to read the clipboard (OSC 52 "?") is always told nothing. Option-drag
   selects in xterm.js itself, and that selection is copied as the mouse lets
   go (copy on select, as iTerm2's). ⌘C copies xterm.js's selection through the
   app's clipboard (`copy`), not the webview's: WKWebView copies only
   through the Edit menu, whose Copy it disables with no text selected in
   the page (xterm.js's selection is its own). With none, ⌘C has tmux copy
   its copy-mode selection (COPY_KEY, the view's key table binds it), which
   comes back as OSC 52; with neither, nothing. On Windows and Linux,
   Ctrl+C copies xterm.js's selection or tmux's (COPY_OR_INTERRUPT), and
   with neither is ^C. On a Mac ⌘V pastes
   through the webview's own paste (its Edit menu's, which the system
   counts as the person's: no prompt to read the clipboard; one with no
   text reads the app's clipboard, `paste`); elsewhere ⌘V reads the app's
   clipboard, the webview's paste prevented. What's pasted goes to
   the pane as xterm.js pastes (bracketed, where the program asked), and
   nothing else does. A press anywhere in the view, its margin too, gives
   xterm.js the keyboard, so the ⌘V after it reaches the pane.

   Links: a URL the pane prints (xterm's web-links addon) or a program's OSC
   8 hyperlink, ⌘-clicked (Ctrl-clicked elsewhere), opens in the system
   browser (api/open.ts openExternal), http(s) only (linkOpener).

   A window with brindle's CLI sidebar pane (a person's terminal shows it)
   is shown without it: the view says which columns to crop (api/ptyView.ts
   Crop, before the client's first bytes), its client is that much wider
   than the view, and so is xterm.js, in a box the view's size that clips
   it, shifted left by the client's columns before the agent's (a left
   sidebar's, or what of it a wider terminal's window, panned, leaves on the
   client). The agent's pane fills the view; clicks, the wheel and selection
   land on its cells, as xterm.js reads them from where its screen is.
   Every column past those shown is masked: the box's last fraction of a
   cell, and, where a narrower terminal having the window narrows the
   agent's pane, a right sidebar inside the view. ⌘C of a selection copies
   only the columns shown (selectionIn), not xterm.js's whole rows. */

import { ClipboardAddon, type IClipboardProvider } from "@xterm/addon-clipboard";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { WebglAddon } from "@xterm/addon-webgl";
import { Terminal as XTerm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useEffect, useRef, useState } from "react";
import { logDebug } from "../api/debug";
import { openExternal, type OpenUrl } from "../api/open";
import type { Crop, PtyViewOpener, PtyViewTarget } from "../api/ptyView";
import { scrollByWheel } from "./terminalScroll";

/* What the view is doing: opening, showing the pane, closed (the client
   exited: its view session or the window went), or why it couldn't open. */
export type TerminalView = { kind: "connecting" } | { kind: "live" } | { kind: "error"; message: string } | { kind: "closed" };

export type Renderer = "webgl" | "dom";

/* The shell block palette (styles.css --shell-*, the site's: dark in both
   themes), read from the tokens as the page has them; xterm takes none of
   its own where a token is missing (a test page with no stylesheet). */
export function shellTheme(): { background?: string; foreground?: string; cursor?: string; selectionBackground?: string } {
  if (typeof document === "undefined") return {};
  const style = getComputedStyle(document.documentElement);
  const token = (name: string) => style.getPropertyValue(name).trim() || undefined;
  // The cursor is the shell ink, softened (--shell-cursor), not the green accent.
  const cursor = token("--shell-cursor");
  return {
    background: token("--shell-bg"),
    foreground: token("--shell-ink"),
    cursor,
    // xterm reads hex: the cursor colour at a quarter, where the token is a six-digit hex.
    selectionBackground: cursor && /^#[0-9a-f]{6}$/i.test(cursor) ? `${cursor}40` : undefined,
  };
}

/* WebGL where it works, else xterm.js's own DOM renderer (xterm 6 has no
   canvas one), which is many times slower on a full screen redrawn a frame
   at a time. A lost WebGL context (the GPU switched, the machine slept, the
   webview was hidden) is asked for again, up to `retries` times; the DOM
   renderer takes over only once a context can't be had. */
export function loadRenderer(term: XTerm, onChange?: (r: Renderer) => void, retries = 3): Renderer {
  try {
    const webgl = new WebglAddon();
    webgl.onContextLoss(() => {
      webgl.dispose();
      onChange?.(retries > 0 ? loadRenderer(term, onChange, retries - 1) : "dom");
    });
    term.loadAddon(webgl);
    return "webgl";
  } catch {
    return "dom";
  }
}

export interface PtyTerminalProps {
  at: PtyViewTarget;
  open: PtyViewOpener;
  /* Where what tmux copies goes (the app's clipboard): settled once the clipboard holds it, failed where it couldn't
     be written. */
  copy?(text: string): Promise<void>;
  /* What the app's clipboard holds, for ⌘V. */
  paste?(): Promise<string>;
  /* Where a link ⌘-clicked in the pane opens: the system browser (openExternal) unless given. */
  openUrl?: OpenUrl;
  onView?(view: TerminalView): void;
  /* The view's size in cells, once it opens and on each change (what its client asks tmux for, the crop's columns not counted). */
  onSize?(cols: number, rows: number): void;
}

/* The width of xterm.js's cells in CSS pixels, as its fit addon reads it. */
function cellWidth(term: XTerm): number {
  return (term as unknown as { _core: { _renderService: { dimensions: { css: { cell: { width: number } } } } } })._core._renderService.dimensions.css.cell.width;
}

/* xterm.js's selection, as its own copy would give it, but only columns `from` to `to` (end
   exclusive) of each row: the selection's ends where they are, the rows between from `from`
   to `to`. A row wrapped onto the next joins it as xterm.js joins them. */
export function selectionIn(term: XTerm, from: number, to: number): string {
  const at = term.getSelectionPosition();
  if (!at) return "";
  const buffer = term.buffer.active;
  let text = "";
  for (let y = at.start.y; y <= at.end.y; y++) {
    const line = buffer.getLine(y);
    const a = Math.max(from, y === at.start.y ? at.start.x : 0);
    const b = Math.min(to, y === at.end.y ? at.end.x : term.cols);
    const joined = y < at.end.y && buffer.getLine(y + 1)?.isWrapped;
    if (line && a < b) text += line.translateToString(!joined, a, b);
    if (y < at.end.y && !joined) text += "\n";
  }
  return text;
}

/* What's pasted, with no ESC or 8-bit CSI in it, so no escape sequence at all: a bracketed paste's end marker in the
   clipboard (however nested) would end the paste early, and what follows reach the program as typed keys (an Enter
   that runs it). */
export const unbracketed = (text: string): string => text.replace(/[\x1b\x9b]/g, "");

/* ⌘C with nothing selected in xterm.js: Ctrl+Option+Shift+F12, which the view's key table binds to copy tmux's
   copy-mode selection (pty_view.rs COPY_BINDING); tmux keeps it from the pane. */
export const COPY_KEY = "\x1b[24;8~";

/* Ctrl+C on Windows and Linux with nothing selected in xterm.js: Ctrl+Option+Shift+F11, which the view's key table
   binds to copy tmux's copy-mode selection, or with none to send C-c to the pane (COPY_OR_INTERRUPT_BINDING). */
export const COPY_OR_INTERRUPT = "\x1b[23;8~";

/* How long a Mac's ⌘V holds what's typed after it for the webview's own paste; none by then, it is given up (and its
   paste, coming later, pasted then). */
export const PASTE_WAIT = 1000;

/* How long "Copied" shows over the view after a copy reaches the app's clipboard, in ms. */
export const COPIED_SHOWN = 1500;

/* Whether the keys are a Mac's: ⌘C, ⌘V through the webview's Edit menu (Tauri's default on macOS), Ctrl+C always ^C. */
const onMac = () => typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

/* A link in the pane, a URL it prints or a program's OSC 8 hyperlink (tmux passes them on): ⌘-clicked (Ctrl-clicked
   off a Mac), as iTerm2 and Ghostty open one, it opens in the system browser. A plain click is the pane's, so a drag
   over a URL copies it; only http(s) links open. */
export function linkOpener(openUrl: () => OpenUrl, mac: boolean): (e: MouseEvent, uri: string) => void {
  return (e, uri) => {
    if (!(mac ? e.metaKey : e.ctrlKey) || !/^https?:\/\//i.test(uri)) return;
    openUrl()(uri).catch((err) => logDebug("pty_view", "error", `couldn't open a link: ${String(err)}`));
  };
}

export function PtyTerminal({ at, open, copy, paste, openUrl, onView, onSize }: PtyTerminalProps) {
  const host = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const mask = useRef<HTMLDivElement>(null);
  const [renderer, setRenderer] = useState<Renderer | null>(null);
  const [view, setView] = useState<TerminalView>({ kind: "connecting" });
  const [copiedShown, setCopiedShown] = useState(false);
  const viewed = useRef(onView);
  viewed.current = onView;
  const sized = useRef(onSize);
  sized.current = onSize;
  const copied = useRef(copy);
  copied.current = copy;
  const pasted = useRef(paste);
  pasted.current = paste;
  const browsed = useRef(openUrl ?? openExternal);
  browsed.current = openUrl ?? openExternal;
  useEffect(() => {
    viewed.current?.(view);
  }, [view]);
  const { server, target } = at;

  useEffect(() => {
    const el = host.current;
    const inner = box.current;
    if (!el || !inner) return;
    const mac = onMac();
    const follow = linkOpener(() => browsed.current, mac);
    const term = new XTerm({
      cursorBlink: true,
      fontFamily: '"Geist Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 13,
      scrollback: 0,
      macOptionClickForcesSelection: true,
      theme: shellTheme(),
      linkHandler: { activate: follow },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new WebLinksAddon(follow));
    let gone = false;
    // Every copy onto the app's clipboard, whoever made it (xterm.js's selection, tmux's, the program's), and
    // "Copied" over the view for a moment once the clipboard holds text. A copy that failed says nothing here
    // (`copy` logs why).
    let hide: ReturnType<typeof setTimeout> | undefined;
    const copyOut = (text: string) => {
      const copyTo = copied.current;
      if (!copyTo || gone) return;
      copyTo(text).then(
        () => {
          if (!text || gone) return;
          setCopiedShown(true);
          clearTimeout(hide);
          hide = setTimeout(() => setCopiedShown(false), COPIED_SHOWN);
        },
        () => {},
      );
    };
    // Writes onto the app's clipboard; reads answered with nothing.
    const clipboard: IClipboardProvider = {
      readText: () => "",
      writeText: (_selection, text) => {
        // To the debug log while it's on (size only): an OSC 52 reached xterm.js, whatever happens to it after.
        logDebug("pty_view", "info", "OSC 52 copy from the pane's client", { bytes: text.length, target });
        copyOut(text);
      },
    };
    term.loadAddon(new ClipboardAddon(undefined, clipboard));
    term.open(inner);
    setRenderer(loadRenderer(term, setRenderer));
    let crop: Crop = { cols: 0, left: 0 };
    // The client's columns shown, from the crop's left on: the view's, or fewer where the agent's pane is narrower.
    const shownCols = () => Math.min(term.cols - crop.cols, crop.width ?? Infinity);
    // Fitted to the box, and the crop's columns wider; shifted left by the client's columns before the agent's; masked
    // past those shown, where xterm.js goes on: the box's last fraction of a cell would show the next column's.
    const layout = () => {
      const fits = fit.proposeDimensions();
      if (fits && Number.isFinite(fits.cols) && Number.isFinite(fits.rows)) {
        const cols = fits.cols + crop.cols;
        if (cols !== term.cols || fits.rows !== term.rows) term.resize(cols, fits.rows);
      }
      if (term.element) term.element.style.marginLeft = crop.left ? `${-crop.left * cellWidth(term)}px` : "";
      if (mask.current) {
        mask.current.hidden = !crop.cols;
        mask.current.style.left = crop.cols ? `${shownCols() * cellWidth(term)}px` : "";
      }
      el.dataset.crop = `${crop.cols},${crop.left}`;
      el.dataset.shown = String(shownCols());
    };
    layout();
    const pty = open({ server, target }, { cols: term.cols, rows: term.rows }, {
      onData: (bytes) => term.write(bytes),
      onCrop: (c) => {
        crop = c;
        layout();
      },
      onExit: () => setView({ kind: "closed" }),
      onError: (message) => setView({ kind: "error", message }),
    });
    setView({ kind: "live" });
    sized.current?.(term.cols, term.rows);
    // What goes to the pane, in the order the person made it: a ⌘V holds its place while the clipboard is read,
    // and what's typed after waits behind it.
    let waiting = 0;
    let line: Promise<void> = Promise.resolve();
    const queued = (step: () => void | Promise<void>) => {
      waiting++;
      line = line
        .then(step)
        .catch(() => {})
        .finally(() => void waiting--);
    };
    const send = (data: string) => (waiting ? queued(() => void (gone || pty.write(data))) : pty.write(data));
    // A dead key can finish through keypress or IME input. Retain it across
    // keyup until xterm delivers the completed input through onData.
    let deadKey = false;
    const typed = term.onData((data) => {
      deadKey = false;
      send(data);
    });
    const binary = term.onBinary(send);
    // The wheel's reports as a native terminal sends them (terminalScroll.ts), not xterm.js's one an event.
    const wheel = scrollByWheel(term, send);
    const resized = term.onResize(({ cols, rows }) => {
      // The view's own size: the crop's columns are the client's to add.
      const shown = cols - crop.cols;
      pty.resize(shown, rows);
      sized.current?.(shown, rows);
    });
    // Focused (the app's window too, coming to the front) or clicked: the window takes
    // the view's size, though nothing is typed (a terminal sharing the pane had it). A
    // click on a view that already has focus fires no focus event; captured, as xterm
    // takes the mouse for itself.
    const focused = () => pty.focus();
    term.textarea?.addEventListener("focus", focused);
    el.addEventListener("mousedown", focused, true);
    // A press anywhere in the view gives xterm.js the keyboard. It takes it itself only for presses on its own element;
    // one on the view's margin, the strip under its last row or the mask would leave the focus on nothing, and the ⌘V
    // (or key) after it would reach no pane: the webview's paste goes to the page, not the view.
    const keyboard = (e: MouseEvent) => {
      if (term.element?.contains(e.target as Node)) return;
      e.preventDefault();
      term.focus();
    };
    el.addEventListener("mousedown", keyboard, true);
    // So does the view opening, and the app's window coming to the front (or the app shown again) with the view on
    // screen, its keyboard focus elsewhere: the person is at the app, looking at the pane. A wider terminal typed in
    // last would otherwise keep the window, the agent's pane wider than the view and its lines cut at its edge.
    const atApp = () => {
      if (document.visibilityState !== "hidden" && el.isConnected && el.getClientRects().length) pty.focus();
    };
    atApp();
    window.addEventListener("focus", atApp);
    document.addEventListener("visibilitychange", atApp);
    // ⌘C of xterm.js's own selection (Option-drag) with columns cropped: only the shown ones, row by row.
    // Captured before xterm.js's own copy, which would take whole rows, the hidden sidebar's columns too.
    const copying = (e: ClipboardEvent) => {
      if (!term.hasSelection() || !crop.cols) return;
      e.clipboardData?.setData("text/plain", selectionIn(term, crop.left, crop.left + shownCols()));
      e.preventDefault();
      e.stopPropagation();
    };
    el.addEventListener("copy", copying, true);
    // What's pasted goes as xterm.js's paste would (its line ends a terminal's, bracketed where the program asked), but
    // in its place in the line, not through term.paste (which would put it behind what was typed after).
    const toPane = (pasting: string) => {
      const text = unbracketed(pasting).replace(/\r?\n/g, "\r");
      if (gone || !text) return;
      pty.write(term.modes.bracketedPasteMode ? `\x1b[200~${text}\x1b[201~` : text);
    };
    // The webview's own pastes, taken from xterm.js: each fills the ⌘V waiting longest for it, or (Edit > Paste
    // clicked, or a ⌘V's that came late) is pasted as it comes. Every paste the webview sends is pasted once.
    const awaiting: ((text: string) => void)[] = [];
    const pasting = (e: ClipboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const text = e.clipboardData?.getData("text/plain") ?? "";
      const waiter = awaiting.shift();
      if (waiter) return waiter(text);
      queued(() => toPane(text));
    };
    el.addEventListener("paste", pasting, true);
    const selected = () => (crop.cols ? selectionIn(term, crop.left, crop.left + shownCols()) : term.getSelection());
    // Copy on select, as iTerm2's: xterm.js's own selection (an Option-drag, an Option-double-click's word, a
    // triple-click's line) goes onto the app's clipboard as the mouse that made it lets go, no ⌘C needed. Let go
    // anywhere (a drag can end off the view): the window's mouseup, after xterm.js's own (on the document) has
    // finished the selection. tmux's selections copy themselves (OSC 52, above).
    let pressed = false;
    const pressing = () => void (pressed = true);
    const letGo = () => {
      if (!pressed) return;
      pressed = false;
      if (gone || !term.hasSelection()) return;
      const text = selected();
      if (text) copyOut(text);
    };
    el.addEventListener("mousedown", pressing, true);
    window.addEventListener("mouseup", letGo);
    // ⌘C and ⌘V through the app's clipboard, Ctrl+C's copy on Windows and Linux. ⌘C taken (its default prevented):
    // neither the webview's copy nor a key goes to the pane. Each ⌘V has one way to paste, chosen before its default
    // goes on or not: on a Mac (the shell's Edit menu) the webview's paste, `pasting` taking it; elsewhere the key is
    // taken, no webview paste follows, and the app's clipboard is read.
    // xterm 6 defers ASCII uppercase letters to keypress, even with Caps Lock.
    // Send these on keydown so a webview needn't supply that legacy event.
    // Keep IME composition and actual modifier shortcuts on xterm's usual path.
    let capsLetter = false;
    term.attachCustomKeyEventHandler((e) => {
      if (e.type === "keypress" && capsLetter) {
        e.preventDefault();
        return false;
      }
      if (e.type === "keyup") capsLetter = false;
      if (e.type !== "keydown") return true;
      capsLetter = false;
      if (e.key === "Dead") deadKey = true;
      if (
        !deadKey &&
        e.getModifierState("CapsLock") &&
        !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey &&
        !e.isComposing && e.keyCode !== 229 &&
        /^[A-Z]$/.test(e.key)
      ) {
        // Prevent the textarea's default insertion too: its input event must
        // not deliver the same character a second time.
        capsLetter = true;
        term.input(e.key, true);
        e.preventDefault();
        return false;
      }
      const key = e.key.toLowerCase();
      const copyTo = copied.current;
      if (!mac && key === "c" && e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) {
        // A copy of xterm.js's selection (cleared, as Windows Terminal does: the next Ctrl+C is ^C), else tmux's
        // (copy mode left) or, with none, ^C: tmux's key table decides.
        if (copyTo && term.hasSelection()) {
          copyOut(selected());
          term.clearSelection();
        } else send(COPY_OR_INTERRUPT);
        e.preventDefault();
        return false;
      }
      if (!e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return true;
      if (key === "c" && copyTo) {
        // xterm.js's selection, else tmux's (its copy comes back as OSC 52).
        if (term.hasSelection()) copyOut(selected());
        else send(COPY_KEY);
        e.preventDefault();
        return false;
      }
      const pasteFrom = pasted.current;
      if (key === "v" && pasteFrom) {
        const fromApp = () => (gone ? "" : pasteFrom().catch(() => ""));
        if (!mac) {
          e.preventDefault();
          const reading = fromApp();
          queued(async () => toPane(await reading));
          return false;
        }
        // The webview's paste (its Edit menu's): its text, or the app's clipboard where it has none. None in a
        // moment: this ⌘V given up, nothing pasted for it; its paste coming later is pasted then, once.
        const reading = new Promise<string>((ok) => {
          const waiter = (text: string) => {
            clearTimeout(timer);
            ok(text || fromApp());
          };
          const timer = setTimeout(() => {
            awaiting.splice(awaiting.indexOf(waiter), 1);
            ok("");
          }, PASTE_WAIT);
          awaiting.push(waiter);
        });
        queued(async () => toPane(await reading));
        return false;
      }
      return true;
    });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(layout);
    observer?.observe(el);
    return () => {
      gone = true;
      clearTimeout(hide);
      setCopiedShown(false);
      term.textarea?.removeEventListener("focus", focused);
      el.removeEventListener("mousedown", focused, true);
      el.removeEventListener("mousedown", keyboard, true);
      window.removeEventListener("focus", atApp);
      document.removeEventListener("visibilitychange", atApp);
      el.removeEventListener("copy", copying, true);
      el.removeEventListener("paste", pasting, true);
      el.removeEventListener("mousedown", pressing, true);
      window.removeEventListener("mouseup", letGo);
      observer?.disconnect();
      typed.dispose();
      binary.dispose();
      wheel.dispose();
      resized.dispose();
      pty.close();
      term.dispose();
    };
  }, [server, target, open]);

  return (
    <div ref={host} className="terminal-host relative min-h-0 min-w-0 flex-1 overflow-hidden bg-shell-bg" data-testid="terminal" data-renderer={renderer ?? undefined} data-view={view.kind} data-pty="1" data-target={target}>
      {/* The view's box: xterm.js fits it, and is clipped to it where it is wider (a crop). */}
      <div ref={box} className="absolute inset-1 overflow-hidden">
        {/* Over the columns past those shown, with columns cropped: the box's last fraction of a cell, and a right
            sidebar a narrower terminal's window puts in the view. */}
        <div ref={mask} hidden className="absolute inset-y-0 right-0 z-10 bg-shell-bg" data-testid="terminal-mask" />
      </div>
      {/* A copy reached the app's clipboard: said for a moment, out of the way of the pane's text and clicks. */}
      {copiedShown && (
        <div role="status" className="pointer-events-none absolute right-s2 top-s2 z-20 rounded-md border border-shell-line bg-shell-bg px-s2 py-0.5 text-xs text-shell-acc" data-testid="terminal-copied">
          Copied
        </div>
      )}
    </div>
  );
}
