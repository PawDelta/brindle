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
   the pane bracketed (tmux passes the markers on only to a program that asked),
   and nothing else does, once tmux has the terminal (ATTACH_WAIT). A press anywhere in
   the view, its margin too, gives xterm.js the keyboard, so the ⌘V after it
   reaches the pane; so does a view replacing one that had it, and a ⌘V with
   the keyboard on nothing goes to the view last used. A pane scrolled back
   leaves copy mode for a paste (the shell's writer, LEAVE_COPY_MODE_BINDING).
   Each paste dropped says why in the debug log, by size, never its text.

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
   only the columns shown (selectionIn), not xterm.js's whole rows.

   Rows are cropped the same way (`cropRows`, Session/Chat/ChatPane.tsx: a
   provider's own prompt box while brindle's message box stands in for it):
   the client is that many rows taller than the view, xterm.js too, and the
   box clips its foot, masked from the last whole row shown. Nothing the
   pane prints is changed; the crop going back to none shows those rows. */

import { ClipboardAddon, type IClipboardProvider } from "@xterm/addon-clipboard";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { WebglAddon } from "@xterm/addon-webgl";
import { Terminal as XTerm, type ITheme } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useContext, useEffect, useRef, useState } from "react";
import { logDebug } from "../api/debug";
import { clipboardAttachments, listenForDrops, type Attachment } from "../api/attachments";
import { startupStep } from "../api/startup";
import { openExternal, type OpenUrl } from "../api/open";
import type { Crop, PtyViewOpener, PtyViewTarget } from "../api/ptyView";
import { visualLink } from "../screens/Session/Visuals/client";
import { VISUAL_LINK, VisualLinkContext } from "../screens/Session/Visuals/link";
import { message } from "../screens/Session/Visuals/chrome";
import { scrollByWheel } from "./terminalScroll";
import { useAppTheme } from "./useAppTheme";

/* What the view is doing: opening, showing the pane, closed (the client
   exited: its view session or the window went), or why it couldn't open. */
export type TerminalView = { kind: "connecting" } | { kind: "live" } | { kind: "error"; message: string } | { kind: "closed" };

export type Renderer = "webgl" | "dom";

/* The shell block palette (styles.css --shell-*, set per theme), read from the
   tokens as the page has them; xterm takes none of
   its own where a token is missing (a test page with no stylesheet). */
export function shellTheme(): ITheme {
  if (typeof document === "undefined") return {};
  const style = getComputedStyle(document.documentElement);
  const token = (name: string) => style.getPropertyValue(name).trim() || undefined;
  // The cursor is the shell ink, softened (--shell-cursor), not the green accent.
  const cursor = token("--shell-cursor");
  // The 16 ANSI colours: what the CLIs draw diffs and code in, so light and dark each carry their own.
  const ansi = Object.fromEntries(Object.entries(ANSI_TOKENS).map(([colour, name]) => [colour, token(name)]));
  return {
    ...ansi,
    background: token("--shell-bg"),
    foreground: token("--shell-ink"),
    cursor,
    // xterm reads hex: the cursor colour at a quarter, where the token is a six-digit hex.
    selectionBackground: cursor && /^#[0-9a-f]{6}$/i.test(cursor) ? `${cursor}40` : undefined,
  };
}

/* xterm.js's colours for a view: the shell block's palette (shellTheme) under whatever the caller gives (the
   session pane's own light palette, which sets its ANSI colours itself). */
export function terminalTheme(theme?: ITheme): ITheme {
  return { ...shellTheme(), ...theme };
}

/* The ANSI colours as styles.css tokens (--shell-*), xterm's names. */
const ANSI_TOKENS = {
  black: "--shell-black", red: "--shell-red", green: "--shell-green", yellow: "--shell-yellow",
  blue: "--shell-blue", magenta: "--shell-magenta", cyan: "--shell-cyan", white: "--shell-white",
  brightBlack: "--shell-bright-black", brightRed: "--shell-bright-red", brightGreen: "--shell-bright-green",
  brightYellow: "--shell-bright-yellow", brightBlue: "--shell-bright-blue", brightMagenta: "--shell-bright-magenta",
  brightCyan: "--shell-bright-cyan", brightWhite: "--shell-bright-white",
} as const;

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
  attachments?: { acceptsFiles: boolean; attach(file: Attachment): Promise<{ text: string }> };
  /* Where a link ⌘-clicked in the pane opens: the system browser (openExternal) unless given. */
  openUrl?: OpenUrl;
  onView?(view: TerminalView): void;
  /* The view's size in cells, once it opens and on each change (what its client asks tmux for, the crop's columns not counted). */
  onSize?(cols: number, rows: number): void;
  /* xterm.js's colours, applied as they change (the app's theme switched): the shell block's (shellTheme) unless given. */
  theme?: ITheme;
  /* xterm.js's minimum contrast between a cell's text and its ground, applied as it changes (with the theme): 1 (none) unless given. */
  minimumContrastRatio?: number;
  /* Rows under the view's foot the client draws and the view clips (the module's comment): none unless given. */
  cropRows?: number;
  /* While rows are cropped, the client's row their mask starts at where that is above the view's foot (a provider's
     prompt box drawn under a short transcript, blank rows under it): those rows hide too. The view's foot unless given. */
  cropFrom?: number;
  /* While given, what the person types or pastes in the view goes here, not to the pane (its input cropped away,
     another field standing in for it): text keys and pastes as their text, keys that would edit or submit the
     pane's hidden input taken, IME composition handed over as "". Escape, Ctrl and ⌘ keys, the mouse and the
     terminal's own replies still reach the pane. */
  typeElsewhere?(text: string): void;
  /* The agent's pane as the view's client draws it, its rows' text top to bottom (the crop's columns shown, the
     cropped rows under the foot too), after each write once tmux has the terminal: for reading what its foot is. */
  onRows?(rows: string[]): void;
}

/* Keys that would move in, edit or submit a program's input line: taken, not sent, while typing goes elsewhere. */
const EDITING_KEYS = new Set(["Enter", "Backspace", "Delete", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"]);

/* xterm.js's cells in CSS pixels, as its fit addon reads them. */
function cell(term: XTerm): { width: number; height: number } {
  return (term as unknown as { _core: { _renderService: { dimensions: { css: { cell: { width: number; height: number } } } } } })._core._renderService.dimensions.css.cell;
}
const cellWidth = (term: XTerm): number => cell(term).width;

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

/* What's pasted, with half an emoji (a lone surrogate, as a truncated copy leaves) made U+FFFD: the shell's JSON refuses
   one, and the whole paste with it. With the u flag, this matches only surrogates that aren't half of a pair. */
const wellFormed = (text: string): string => text.replace(/[\ud800-\udfff]/gu, "�");

/* ⌘C with nothing selected in xterm.js: Ctrl+Option+Shift+F12, which the view's key table binds to copy tmux's
   copy-mode selection (pty_view.rs COPY_BINDING); tmux keeps it from the pane. */
export const COPY_KEY = "\x1b[24;8~";

/* Ctrl+C on Windows and Linux with nothing selected in xterm.js: Ctrl+Option+Shift+F11, which the view's key table
   binds to copy tmux's copy-mode selection, or with none to send C-c to the pane (COPY_OR_INTERRUPT_BINDING). */
export const COPY_OR_INTERRUPT = "\x1b[23;8~";

/* How long a Mac's ⌘V holds what's typed after it for the webview's own paste. None by then, the app's clipboard is
   read in its place, typing still behind it. Long, as that read isn't the system's "person pasting": macOS may ask. */
export const PASTE_WAIT = 3000;

/* How long after that a webview paste of the same text is the late answer to that ⌘V, not pasted again. */
export const LATE_PASTE_WINDOW = 10000;

/* How long a paste waits for tmux to take the view's terminal (a resume, a reconnect, the client still attaching). A
   read can end anywhere, so it's what xterm.js has parsed that says, not the first bytes. Not by now: dropped, and said. */
export const ATTACH_WAIT = 5000;

/* What a ⌘V with the keyboard elsewhere is meant for, and what a view leaves the keyboard on. */
const TEXT_FIELD = "input, textarea, select, [contenteditable]";
const DIALOG = '[role="dialog"], [role="alertdialog"], dialog[open]';

/* The view last given the keyboard: a ⌘V with the keyboard on nothing is its, while it's on screen. */
let lastFocused: HTMLElement | null = null;
const onScreen = (el: HTMLElement) => document.visibilityState !== "hidden" && el.isConnected && el.getClientRects().length > 0;

/* How long "Copied" shows over the view after a copy reaches the app's clipboard, in ms. */
export const COPIED_SHOWN = 1500;

/* Whether the keys are a Mac's: ⌘C, ⌘V through the webview's Edit menu (Tauri's default on macOS), Ctrl+C always ^C. */
const onMac = () => typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

/* A link in the pane, a URL it prints or a program's OSC 8 hyperlink (tmux passes them on): ⌘-clicked (Ctrl-clicked
   off a Mac), as iTerm2 and Ghostty open one, it opens in the system browser. A plain click is the pane's, so a drag
   over a URL copies it; only http(s) links open. A session's brindle://visual/<id> opens that visual in the chat's
   place, the same way. */
export function linkOpener(openUrl: () => OpenUrl, mac: boolean, openVisual?: () => ((id: string) => void) | null): (e: MouseEvent, uri: string) => void {
  return (e, uri) => {
    if (!(mac ? e.metaKey : e.ctrlKey)) return;
    const visual = visualLink(uri);
    const show = openVisual?.();
    if (visual && show) return show(visual);
    if (!/^https?:\/\//i.test(uri)) return;
    openUrl()(uri).catch((err) => logDebug("pty_view", "error", `couldn't open a link: ${String(err)}`));
  };
}

export function PtyTerminal({ at, open, copy, paste, attachments, openUrl, onView, onSize, theme, minimumContrastRatio = 1, cropRows = 0, cropFrom, typeElsewhere, onRows }: PtyTerminalProps) {
  const footFrom = useRef(cropFrom);
  footFrom.current = cropFrom;
  const elsewhere = useRef(typeElsewhere);
  elsewhere.current = typeElsewhere;
  const rowsRead = useRef(onRows);
  rowsRead.current = onRows;
  const host = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const mask = useRef<HTMLDivElement>(null);
  const foot = useRef<HTMLDivElement>(null);
  const themed = useRef(theme);
  themed.current = theme;
  const contrasted = useRef(minimumContrastRatio);
  contrasted.current = minimumContrastRatio;
  const footRows = useRef(cropRows);
  footRows.current = cropRows;
  // The open view's xterm.js and its layout, for a theme or row crop changing while it's open.
  const live = useRef<{ term: XTerm; layout(): void } | null>(null);
  const [renderer, setRenderer] = useState<Renderer | null>(null);
  const [view, setView] = useState<TerminalView>({ kind: "connecting" });
  const [copiedShown, setCopiedShown] = useState(false);
  const [dropShown, setDropShown] = useState(false);
  const [fileNotice, setFileNotice] = useState<{ error: boolean; text: string } | null>(null);
  const attaching = useRef(attachments);
  attaching.current = attachments;
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
  const linked = useContext(VisualLinkContext);
  const visuals = useRef(linked);
  visuals.current = linked;
  useEffect(() => {
    viewed.current?.(view);
  }, [view]);
  const { server, target } = at;

  useEffect(() => {
    const el = host.current;
    const inner = box.current;
    if (!el || !inner) return;
    const mac = onMac();
    const follow = linkOpener(() => browsed.current, mac, () => visuals.current);
    const term = new XTerm({
      cursorBlink: true,
      fontFamily: '"Geist Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 13,
      scrollback: 0,
      macOptionClickForcesSelection: true,
      theme: terminalTheme(themed.current),
      minimumContrastRatio: contrasted.current,
      linkHandler: { activate: follow },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new WebLinksAddon(follow));
    term.loadAddon(new WebLinksAddon(follow, { urlRegex: VISUAL_LINK }));
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
    // A resume or reconnect replaces the view (its key), and the keyboard goes with the textarea it had, to nothing:
    // the next ⌘V and keys would reach no pane. Nothing is taken from a field or dialog in use.
    const noteFocus = () => void (lastFocused = el);
    term.textarea?.addEventListener("focus", noteFocus);
    if ((!document.activeElement || document.activeElement === document.body) && !document.querySelector(DIALOG)) term.textarea?.focus({ preventScroll: true });
    let crop: Crop = { cols: 0, left: 0 };
    // The client's columns shown, from the crop's left on: the view's, or fewer where the agent's pane is narrower.
    const shownCols = () => Math.min(term.cols - crop.cols, crop.width ?? Infinity);
    // Fitted to the box, and the crop's columns wider; shifted left by the client's columns before the agent's; masked
    // past those shown, where xterm.js goes on: the box's last fraction of a cell would show the next column's.
    // Rows likewise: the cropped ones under the view's foot, the client that much taller; masked from the last whole
    // row shown, where the box's last fraction of a row would show the first cropped one's top.
    let cropped = 0;
    const layout = () => {
      const fits = fit.proposeDimensions();
      if (fits && Number.isFinite(fits.cols) && Number.isFinite(fits.rows)) {
        cropped = Math.max(0, footRows.current);
        const cols = fits.cols + crop.cols;
        const rows = fits.rows + cropped;
        if (cols !== term.cols || rows !== term.rows) term.resize(cols, rows);
      }
      if (term.element) term.element.style.marginLeft = crop.left ? `${-crop.left * cellWidth(term)}px` : "";
      if (mask.current) {
        mask.current.hidden = !crop.cols;
        mask.current.style.left = crop.cols ? `${shownCols() * cellWidth(term)}px` : "";
      }
      // The mask's first row: the view's foot, or the crop's own start above it.
      const from = Math.min(term.rows - cropped, Math.max(0, footFrom.current ?? Infinity));
      if (foot.current) {
        foot.current.hidden = !cropped;
        foot.current.style.top = cropped ? `${from * cell(term).height}px` : "";
      }
      el.dataset.crop = `${crop.cols},${crop.left}`;
      el.dataset.cropRows = String(cropped);
      el.dataset.cropFrom = cropped ? String(from) : "";
      el.dataset.shown = String(shownCols());
    };
    layout();
    live.current = { term, layout };
    // Whether tmux has taken the client's terminal (a paste waits for it, toPane): it switches to the alternate screen
    // first thing, its pty raw by then, however the bytes were split. Also woken by the view ending, whose paste waits
    // no more.
    let attached = false;
    let ended: string | null = null;
    let wake = () => {};
    const woken = new Promise<void>((ok) => (wake = ok));
    const lookAttached = () => {
      if (gone || attached || term.buffer.active.type !== "alternate") return;
      attached = true;
      startupStep("terminal_attached");
      wake();
    };
    const readRows = () => {
      const read = rowsRead.current;
      const buffer = term.buffer.active;
      // Only tmux's screen (the alternate one) is the pane's; the client's own lines before it aren't.
      if (gone || !read || buffer.type !== "alternate") return;
      const rows: string[] = [];
      for (let y = 0; y < term.rows; y++) rows.push(buffer.getLine(buffer.viewportY + y)?.translateToString(true, crop.left, crop.left + shownCols()) ?? "");
      read(rows);
    };
    const parsed = term.onWriteParsed(readRows);
    const pty = open({ server, target }, { cols: term.cols, rows: term.rows }, {
      onData: (bytes) => {
        if (gone) return;
        if (attached) term.write(bytes);
        else term.write(bytes, lookAttached);
      },
      onCrop: (c) => {
        if (gone) return;
        crop = c;
        layout();
      },
      onExit: () => {
        if (gone) return;
        ended = "the view's client has exited";
        wake();
        setView({ kind: "closed" });
      },
      onError: (message) => {
        if (gone) return;
        ended = "the view couldn't open";
        wake();
        setView({ kind: "error", message });
      },
    });
    setView({ kind: "live" });
    sized.current?.(term.cols, term.rows - cropped);
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
    const typed = term.onData(send);
    // onData includes protocol replies, which cannot complete a dead key.
    // Retain it across keyup until xterm handles a key or input completes.
    let deadKey = false;
    const completedKey = term.onKey(() => { deadKey = false; });
    const completedInput = (event: Event) => {
      const e = event as InputEvent;
      if (e.data && e.inputType === "insertText" && !e.isComposing) deadKey = false;
    };
    term.textarea?.addEventListener("input", completedInput);
    // A composition (IME) starting while typing goes elsewhere: xterm.js would send its text at the end, past the key
    // handler, so the keyboard is handed over before it can.
    const composing = () => {
      const away = elsewhere.current;
      if (!away) return;
      term.blur();
      away("");
    };
    term.textarea?.addEventListener("compositionstart", composing);
    // Text the textarea takes with no key behind it (an emoji or character picker, dictation): xterm.js sends it from
    // its input listener, past the key handler. Captured on the view before it gets there, and sent elsewhere.
    const inserted = (event: Event) => {
      const e = event as InputEvent;
      const away = elsewhere.current;
      if (!away || e.target !== term.textarea || e.isComposing || !e.data || !e.inputType.startsWith("insert")) return;
      e.stopPropagation();
      if (term.textarea) term.textarea.value = "";
      away(e.data);
    };
    el.addEventListener("input", inserted, true);
    const binary = term.onBinary(send);
    // The wheel's reports as a native terminal sends them (terminalScroll.ts), not xterm.js's one an event.
    const wheel = scrollByWheel(term, send);
    const resized = term.onResize(({ cols, rows }) => {
      // The view's own size: the crop's columns are the client's to add. Its cropped rows are the view's own: the
      // client asks tmux for them, and they aren't the view's size.
      const shown = cols - crop.cols;
      pty.resize(shown, rows);
      sized.current?.(shown, rows - cropped);
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
    // A paste that won't reach the pane, to the debug log while it's on: why, and its size, never its text.
    const dropPaste = (why: string, bytes: number) => logDebug("pty_view", "warning", `paste dropped: ${why}`, { bytes, target });
    // What's pasted goes as xterm.js's paste would (its line ends a terminal's), but in its place in the line, not
    // through term.paste (which would put it behind what was typed after). Always bracketed, whatever xterm.js has
    // parsed so far: tmux reads the markers, and passes them on only to a pane that asked (a_paste_reaches_...), so its
    // Enters are never keys at the client. Once tmux has taken the client's terminal (ATTACH_WAIT), or a cooked pty
    // would change them. What's typed waits behind it, in the line.
    // `wentAway`: whether typing went elsewhere (typeElsewhere) as the paste was made. It goes where it was made for,
    // or nowhere: where that changed while the text was read (a permission question came, or went), it is dropped,
    // never sent to the other side.
    const toPane = async (pasting: string, wentAway: boolean) => {
      const bytes = pasting.length;
      const text = wellFormed(unbracketed(pasting)).replace(/\r?\n/g, "\r");
      if (!text) return dropPaste("the clipboard holds no text", bytes);
      const away = elsewhere.current;
      if (!!away !== wentAway) return dropPaste("where typing goes changed while it was read", bytes);
      if (away) {
        // Typing goes elsewhere: so does a paste, as the person's text, line ends theirs.
        if (!gone) away(wellFormed(unbracketed(pasting)));
        return;
      }
      if (!attached && !gone && !ended) {
        await new Promise<void>((ok) => {
          const patience = setTimeout(ok, ATTACH_WAIT);
          void woken.then(() => {
            clearTimeout(patience);
            ok();
          });
        });
      }
      if (gone) return dropPaste("the view closed first", bytes);
      if (ended) return dropPaste(ended, bytes);
      if (!attached) return dropPaste(`tmux hadn't taken the view's terminal within ${ATTACH_WAIT} ms`, bytes);
      pty.write(`\x1b[200~${text}\x1b[201~`);
      logDebug("pty_view", "info", "paste sent to the view", { bytes, target });
    };
    // The app's clipboard, for a paste with no webview paste to go by. Null: nothing to paste, and why is said.
    const attachFiles = async (files: Attachment[]): Promise<string | null> => {
      const destination = attaching.current;
      if (gone || !destination || !files.length) return null;
      if (!destination.acceptsFiles) {
        setFileNotice({ error: false, text: "This agent can't take files." });
        return null;
      }
      setFileNotice(null);
      const texts: string[] = [];
      const errors: string[] = [];
      for (const file of files) {
        if (gone || attaching.current !== destination) break;
        try {
          const result = await destination.attach(file);
          if (gone || attaching.current !== destination) return null;
          texts.push(result.text);
        } catch (err) {
          if (!gone && attaching.current === destination) {
            errors.push(message(err));
            setFileNotice({ error: true, text: errors.join(" ") });
          }
        }
      }
      return texts.join(" ") || null;
    };
    const fromApp = async (onRead?: (text: string) => void, webviewText?: string, latestText?: () => string): Promise<string | null> => {
      const pasteFrom = pasted.current;
      const destination = attaching.current;
      if (gone) return null;
      const text = webviewText || await pasteFrom?.().catch((err) => {
        dropPaste(`couldn't read the app's clipboard: ${String(err)}`, 0);
        return null;
      });
      onRead?.(text ?? "");
      const fallback = () => latestText?.() || (text ?? null);
      if (gone || !destination) return fallback();
      try {
        const files = await clipboardAttachments();
        if (gone || attaching.current !== destination) return null;
        return files.length ? await attachFiles(files) : fallback();
      } catch (err) {
        if (!gone && attaching.current === destination) setFileNotice({ error: true, text: `Couldn't read clipboard files: ${String(err)}` });
        return fallback();
      }
    };
    // Its place in the line kept from now, however long the text takes to come, and its destination too.
    const pasteInLine = (reading: Promise<string | null>) => {
      const wentAway = !!elsewhere.current;
      queued(async () => {
        const text = await reading;
        if (text !== null) await toPane(text, wentAway);
      });
    };
    let unlisten: (() => void) | undefined;
    if (attaching.current) {
      void listenForDrops(event => {
        if (gone) return;
        const underCursor = event.type === "leave" ? null : document.elementFromPoint(event.x, event.y);
        const inside = !!underCursor && el.contains(underCursor);
        setDropShown(inside && event.type !== "drop" && !!attaching.current?.acceptsFiles);
        if (!inside || event.type !== "drop") return;
        // The drop is this view's, for the agent it shows as the drop lands: its place in the line is
        // reserved from here, before a byte is read or any upload starts.
        const destination = attaching.current;
        const wentAway = !!elsewhere.current;
        queued(async () => {
          const files = await event.read();
          // Read while the view closed or moved on to another agent: dropped, never attached there.
          if (gone || attaching.current !== destination) return;
          if (!files.length) {
            // A drag with no readable file behind it (a folder, a promised file that never came): said, not silently lost.
            setFileNotice({ error: true, text: "That drag didn't include a file. Drag the saved file instead." });
            return;
          }
          const text = await attachFiles(files);
          if (text) await toPane(text, wentAway);
        });
      }).then(stop => { if (gone) stop(); else unlisten = stop; }, () => {
        if (!gone) setFileNotice({ error: true, text: "Couldn't enable file drops." });
      });
    }
    // The webview's own pastes, taken from xterm.js: each fills the ⌘V waiting longest for it, or (Edit > Paste
    // clicked, or a ⌘V's that came late) is pasted as it comes. Every paste the webview sends is pasted once.
    const awaiting: ((text: string) => void)[] = [];
    // ⌘Vs past their wait: match their original clipboard text (empty for files),
    // not the attachment text generated for the terminal, until their late answer arrives.
    const owed: { clipboardText: string; until: number }[] = [];
    const sameLines = (text: string) => text.replace(/\r\n?/g, "\n");
    const pasting = (e: ClipboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const text = e.clipboardData?.getData("text/plain") ?? "";
      const lateAnswer = owed.findIndex((o) => o.until > Date.now() && o.clipboardText === sameLines(text));
      if (lateAnswer >= 0) {
        owed.splice(lateAnswer, 1);
        return dropPaste("a late webview paste of what the app's clipboard already pasted", text.length);
      }
      const waiter = awaiting.shift();
      if (waiter) return waiter(text);
      pasteInLine(fromApp(undefined, text));
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
    // Typing going elsewhere (typeElsewhere): text keys go there as their text, editing keys go nowhere, composition
    // and dead keys hand the keyboard over (""). Their keypress is taken too, xterm.js's other way to send a key.
    // Escape and Ctrl/⌘ keys (interrupt, copy, paste) carry on below.
    const typedAway = (e: KeyboardEvent): boolean => {
      const away = elsewhere.current;
      if (!away || e.ctrlKey || e.metaKey || e.key === "Escape") return false;
      if (e.type === "keypress") return true;
      if (e.type !== "keydown") return false;
      if (e.isComposing || e.keyCode === 229 || e.key === "Dead") away("");
      else if (e.key.length === 1) away(e.key);
      else if (!EDITING_KEYS.has(e.key)) return false;
      e.preventDefault();
      return true;
    };
    term.attachCustomKeyEventHandler((e) => {
      if (typedAway(e)) return false;
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
      if (!mac && key === "v" && e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && pasted.current) {
        e.preventDefault();
        pasteInLine(fromApp());
        return false;
      }
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
      if (key === "v" && pasted.current) {
        if (!mac) {
          e.preventDefault();
          pasteInLine(fromApp());
          return false;
        }
        // The webview's paste (its Edit menu's): its text, or the app's clipboard where it has none. None in a
        // moment: the app's clipboard in this ⌘V's place, so what's typed behind it stays behind it, and its paste,
        // coming later with that text, isn't pasted again (`owed`). One coming during that read still answers it.
        pasteInLine(new Promise<string | null>((ok) => {
          let reading: Promise<string | null> | undefined;
          let clipboardText = "";
          let webviewText = "";
          const read = () => reading ??= fromApp(text => { clipboardText = text; }, undefined, () => webviewText);
          const waiter = (text: string) => {
            clearTimeout(timer);
            webviewText = text;
            ok(attaching.current ? reading ?? fromApp(undefined, text) : text || read());
          };
          const timer = setTimeout(() => {
            if (!gone) logDebug("pty_view", "warning", `⌘V: the webview's paste didn't come within ${PASTE_WAIT} ms: reading the app's clipboard`, { target });
            void read().then((text) => {
              const unanswered = awaiting.indexOf(waiter);
              if (unanswered < 0) return;
              awaiting.splice(unanswered, 1);
              if (text) owed.push({ clipboardText: sameLines(clipboardText), until: Date.now() + LATE_PASTE_WINDOW });
              ok(text);
            });
          }, PASTE_WAIT);
          awaiting.push(waiter);
        }));
        return false;
      }
      return true;
    });
    // ⌘V with the keyboard on nothing (a click on the app's own chrome, a row of the sidebar): xterm.js doesn't hear
    // it, and the webview has nowhere to paste. Taken for the view last used, once: not for a text field or dialog,
    // which have their own use for it. Its default prevented, no webview paste follows; the app's clipboard is read.
    const offView = (e: KeyboardEvent) => {
      const pasteKey = (e.metaKey && !e.ctrlKey) || (!mac && e.ctrlKey && !e.metaKey);
      if (e.defaultPrevented || !pasteKey || e.altKey || e.shiftKey || e.key.toLowerCase() !== "v") return;
      if (!pasted.current || el.contains(e.target as Node) || !onScreen(el)) return;
      if (lastFocused && lastFocused !== el && onScreen(lastFocused)) return;
      if (document.activeElement?.closest(TEXT_FIELD) || document.querySelector(DIALOG)) return;
      e.preventDefault();
      logDebug("pty_view", "info", "⌘V with the keyboard off the view: taken for it", { target });
      term.focus();
      pasteInLine(fromApp());
    };
    window.addEventListener("keydown", offView);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(layout);
    observer?.observe(el);
    return () => {
      gone = true;
      unlisten?.();
      wake();
      if (lastFocused === el) lastFocused = null;
      clearTimeout(hide);
      setCopiedShown(false);
      setDropShown(false);
      setFileNotice(null);
      window.removeEventListener("keydown", offView);
      term.textarea?.removeEventListener("focus", noteFocus);
      term.textarea?.removeEventListener("focus", focused);
      term.textarea?.removeEventListener("input", completedInput);
      term.textarea?.removeEventListener("compositionstart", composing);
      el.removeEventListener("input", inserted, true);
      el.removeEventListener("mousedown", focused, true);
      el.removeEventListener("mousedown", keyboard, true);
      window.removeEventListener("focus", atApp);
      document.removeEventListener("visibilitychange", atApp);
      el.removeEventListener("copy", copying, true);
      el.removeEventListener("paste", pasting, true);
      el.removeEventListener("mousedown", pressing, true);
      window.removeEventListener("mouseup", letGo);
      observer?.disconnect();
      parsed.dispose();
      typed.dispose();
      completedKey.dispose();
      binary.dispose();
      wheel.dispose();
      resized.dispose();
      live.current = null;
      pty.close();
      term.dispose();
    };
  }, [server, target, open]);

  // The theme switched, or the rows to crop changed, with the view open: xterm.js redraws in the new colours; the
  // client is resized to the new crop, and the pane redraws itself to fit as on any resize. The app's own theme
  // (useAppTheme) counts too: the shell's ANSI tokens change with it even where the caller passes no theme.
  const appTheme = useAppTheme();
  useEffect(() => {
    if (live.current) live.current.term.options.theme = terminalTheme(theme);
  }, [theme, appTheme]);
  useEffect(() => {
    if (live.current) live.current.term.options.minimumContrastRatio = minimumContrastRatio;
  }, [minimumContrastRatio]);
  useEffect(() => {
    live.current?.layout();
  }, [cropRows, cropFrom]);

  // The view's ground: the theme's background where one is given, the shell block's otherwise.
  const ground = theme?.background ? { background: theme.background } : undefined;
  return (
    <div ref={host} className="terminal-host relative min-h-0 min-w-0 flex-1 overflow-hidden bg-shell-bg" style={ground} data-testid="terminal" data-renderer={renderer ?? undefined} data-view={view.kind} data-pty="1" data-target={target}>
      {/* The view's box: xterm.js fits it, and is clipped to it where it is wider or taller (a crop). */}
      <div ref={box} className="absolute inset-1 overflow-hidden">
        {/* Over the columns past those shown, with columns cropped: the box's last fraction of a cell, and a right
            sidebar a narrower terminal's window puts in the view. */}
        <div ref={mask} hidden className="absolute inset-y-0 right-0 z-10 bg-shell-bg" style={ground} data-testid="terminal-mask" />
        {/* Under the last whole row shown, with rows cropped: the box's last fraction of a row. */}
        <div ref={foot} hidden className="absolute inset-x-0 bottom-0 z-10 bg-shell-bg" style={ground} data-testid="terminal-foot-mask" />
      </div>
      {/* A copy reached the app's clipboard: said for a moment, out of the way of the pane's text and clicks. */}
      {copiedShown && (
        <div role="status" className="pointer-events-none absolute right-s2 top-s2 z-20 rounded-md bg-shell-ink px-s2 py-1 text-sm font-medium text-shell-bg shadow-md" data-testid="terminal-copied">
          Copied
        </div>
      )}
      {dropShown && <div data-testid="terminal-drop" className="pointer-events-none absolute inset-0 z-20 rounded-md border-2 border-shell-acc bg-shell-bg/60 p-s3 text-shell-acc">Drop files to attach</div>}
      {fileNotice && <div role={fileNotice.error ? "alert" : "status"} className="absolute bottom-s2 left-s2 right-s2 z-20 rounded-md border border-shell-line bg-shell-bg p-s2 text-xs text-shell-ink" onClick={() => setFileNotice(null)}>{fileNotice.text}</div>}
    </div>
  );
}
