/* The wheel over a pane view, as a native terminal turns it into mouse
   reports for tmux (docs/scroll-perf.md). xterm.js's own sends at most one
   report a wheel event, whatever distance the event covers, and takes any
   event under 50 px for a trackpad's and scales it by 0.3: a trackpad swipe
   moved tmux's copy mode (5 lines a report) once every 50 px or so of the
   finger, a few times a second, and a Mac mouse's notch (WebKit's 40 px)
   moved it on three notches in four. A native terminal (iTerm2's
   proportional reporting, Ghostty's, Terminal.app's) sends one report for
   each line of text the gesture covers, and one at least for each notch:
   so does this, a frame's reports in one write (tmux redraws once for
   them), the first of a gesture's at once.

   Only while the terminal reports the mouse (tmux's mouse, always on in the
   view): then in tmux's encoding, SGR (tmux asks for it with the mouse). */

import type { Terminal as XTerm } from "@xterm/xterm";

/* A mouse notch, in the pixels a wheel event gives it: macOS's line delta
   times WebKit's line step (40) in WKWebView and WebKitGTK, 100 in
   Chromium's (WebView2, on Windows). Trackpads give pixels as the finger
   moves. */
export const notchPx = (windows = typeof navigator !== "undefined" && /Win/.test(navigator.platform)) => (windows ? 100 : 40);

/* A pause this long (ms) between wheel events starts a new gesture: what
   was left of a line from the last one is let go. */
export const GESTURE_GAP = 250;

/* A cell's height, where xterm.js has none measured yet (a page with no layout). */
const FALLBACK_CELL_PX = 16;

/* The reports for wheel events, as a native terminal counts them: a notch's
   lines (one at least), a trackpad's pixels a line of text at a time, what's
   left of a line kept for the next event of the same gesture and the same
   way. Each event's count is signed: up (deltaY < 0) negative.

   A gesture is a mouse's while each of its pixel events is a whole number of
   notches; its first event that isn't makes it a trackpad's to its end, so a
   trackpad's event that happens to be 40 px (or 80) is pixels like the rest,
   its fraction of a line kept. */
export class WheelLines {
  private rest = 0;
  private last = -Infinity;
  private trackpad = false;

  constructor(private readonly notch = notchPx()) {}

  /* The reports `ev` makes, over cells `cellPx` high on a screen of `rows`. */
  add(ev: Pick<WheelEvent, "deltaY" | "deltaMode" | "timeStamp">, cellPx: number, rows: number): number {
    const dy = ev.deltaY;
    const gap = ev.timeStamp - this.last;
    this.last = ev.timeStamp;
    if (!dy) return 0;
    if (gap > GESTURE_GAP) {
      this.rest = 0;
      this.trackpad = false;
    }
    if (Math.sign(this.rest) === -Math.sign(dy)) this.rest = 0;
    const sign = Math.sign(dy);
    if (ev.deltaMode === 2) return sign * Math.max(1, Math.round(Math.abs(dy) * rows));
    if (ev.deltaMode === 1) return sign * Math.max(1, Math.round(Math.abs(dy)));
    if (!this.trackpad && Math.abs(dy) % this.notch === 0) return sign * Math.abs(dy / this.notch);
    this.trackpad = true;
    this.rest += dy / (cellPx > 0 ? cellPx : FALLBACK_CELL_PX);
    const whole = Math.trunc(this.rest) || 0;
    this.rest -= whole;
    return whole;
  }
}

/* `count` wheel reports at cell `col`, `row` (from 1), SGR-encoded as
   xterm.js encodes its own: up (64) or down (65), and the modifiers held. */
export function wheelReports(up: boolean, count: number, col: number, row: number, mods: { shiftKey?: boolean; altKey?: boolean; ctrlKey?: boolean } = {}): string {
  const button = (up ? 64 : 65) + (mods.shiftKey ? 4 : 0) + (mods.altKey ? 8 : 0) + (mods.ctrlKey ? 16 : 0);
  return `\x1b[<${button};${col};${row}M`.repeat(count);
}

/* The cell under the pointer, from 1, and the cells' height, as xterm.js
   reads them off its screen (shifted left with columns cropped: the cell
   under the pointer is still the one xterm.js shows there). */
function cellAt(term: XTerm, ev: Pick<MouseEvent, "clientX" | "clientY">): { col: number; row: number; cellPx: number } {
  const screen = term.element?.querySelector(".xterm-screen");
  const rect = screen?.getBoundingClientRect();
  if (!rect || !rect.width || !rect.height) return { col: 1, row: 1, cellPx: 0 };
  const w = rect.width / term.cols;
  const h = rect.height / term.rows;
  const clamp = (n: number, max: number) => Math.min(max, Math.max(1, n));
  return { col: clamp(Math.floor((ev.clientX - rect.left) / w) + 1, term.cols), row: clamp(Math.floor((ev.clientY - rect.top) / h) + 1, term.rows), cellPx: h };
}

export interface WheelScroll {
  dispose(): void;
}

/* The wheel over `term` (open) sent to `write` as a native terminal sends
   it, in place of xterm.js's own reports: a gesture's first reports at once,
   then at most one write a frame (`frame`: the page's next paint), with what
   came since; never more than a screen's rows of reports in one, and none
   left over to go on scrolling once the wheel has stopped. Taken on its way
   in to xterm.js's element (captured), before xterm.js or its viewport sees
   it. */
export function scrollByWheel(term: XTerm, write: (data: string) => void, frame: (step: () => void) => unknown = paintFrame): WheelScroll {
  const lines = new WheelLines();
  let pending = 0;
  let at = { col: 1, row: 1, mods: {} as Pick<WheelEvent, "shiftKey" | "altKey" | "ctrlKey"> };
  let booked = false;
  let gone = false;
  const flush = () => {
    if (!pending || gone) return false;
    const count = Math.min(Math.abs(pending), term.rows);
    write(wheelReports(pending < 0, count, at.col, at.row, at.mods));
    pending = 0;
    return true;
  };
  const step = () => {
    booked = flush();
    if (booked) frame(step);
  };
  const el = term.element;
  // Not open: no wheel to take.
  if (!el) return { dispose() {} };
  const wheeled = (ev: WheelEvent) => {
    if (term.modes.mouseTrackingMode === "none" || !ev.deltaY) return;
    ev.preventDefault();
    ev.stopImmediatePropagation();
    const cell = cellAt(term, ev);
    const n = lines.add(ev, cell.cellPx, term.rows);
    // A turn the other way (by the event's own direction, though it makes no whole line yet) drops what hadn't gone
    // yet of the last.
    pending = Math.sign(pending) === -Math.sign(ev.deltaY) ? n : pending + n;
    at = { col: cell.col, row: cell.row, mods: { shiftKey: ev.shiftKey, altKey: ev.altKey, ctrlKey: ev.ctrlKey } };
    if (!booked && flush()) {
      booked = true;
      frame(step);
    }
  };
  el.addEventListener("wheel", wheeled, { capture: true, passive: false });
  return {
    dispose() {
      gone = true;
      el.removeEventListener("wheel", wheeled, { capture: true });
    },
  };
}

const paintFrame = (step: () => void) => (typeof requestAnimationFrame === "function" ? requestAnimationFrame(step) : setTimeout(step, 16));
