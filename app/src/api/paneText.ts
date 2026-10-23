/* What a tmux pane shows, read as text: an action terminal's (a provider
   sign-in: the link its CLI printed, the question it asks, its end). The
   pane comes through a tmux client of its own on a pty (a PtyView no one
   sees), whose bytes are redraws, not the CLI's own output: they are
   parsed by an xterm.js that is never opened (no renderer, no DOM), and
   its screen read back. A row the client drew to its last column runs on
   into the next, as a wrapped line does (a long link): that holds only
   read at the pane's own width, so while another client shows the pane
   (the sign-in's details, which tmux then sizes the window by) the reader
   takes that client's size (`resize`). The CLI's command
   exiting leaves its pane (remain-on-exit) with tmux's "Pane is dead" on
   it: that, or the client exiting, is the end. */

import { Terminal as XTerm } from "@xterm/xterm";
import type { PtyViewOpener, PtyViewTarget } from "./ptyView";

/* The reading client's size: wide, so a link the CLI prints fits a row or two. */
export const READ_SIZE = { cols: 200, rows: 50 };

/* What tmux draws on a pane whose command has exited (remain-on-exit: "Pane is dead (status 0, …)"). */
export const PANE_DEAD = /Pane is dead\b/;

/* The screen's text: a line per row, a row drawn to its last column joined
   to the next, the blank rows at its foot left out. */
export function screenText(term: XTerm): string {
  const buffer = term.buffer.active;
  const lines: string[] = [];
  let runsOn = false;
  for (let y = 0; y < buffer.length; y++) {
    const line = buffer.getLine(y);
    if (!line) continue;
    const row = line.translateToString(true);
    if ((line.isWrapped || runsOn) && lines.length > 0) lines[lines.length - 1] += row;
    else lines.push(row);
    const last = line.getCell(term.cols - 1)?.getChars() ?? "";
    runsOn = last !== "" && last !== " ";
  }
  return lines.join("\n").replace(/\n+$/, "");
}

export interface PaneReading {
  /* Read at this size, the reader's client sized to match: the pane's own
     width while another client shares it (tmux wraps the pane at the size
     of the client used last; read at any other width, a long link's rows
     would read as separate lines). */
  resize(cols: number, rows: number): void;
  close(): void;
}

/* Reads the pane at `at`: `onText` with its screen's text each time it
   changes, then `onEnd` once, when its command has exited or the client
   has (with why, when it couldn't open). */
export function readPane(open: PtyViewOpener, at: PtyViewTarget, onText: (text: string) => void, onEnd: (error?: string) => void): PaneReading {
  const term = new XTerm({ ...READ_SIZE, scrollback: 0 });
  let closed = false;
  let last = "";
  // Ended before the view was even back (an opener that answers at once): closed as it comes.
  let view: { resize(cols: number, rows: number): void; close(): void } | null = null;
  function close() {
    if (closed) return;
    closed = true;
    view?.close();
    term.dispose();
  }
  function end(error?: string) {
    if (closed) return;
    close();
    onEnd(error);
  }
  const opened = open(at, READ_SIZE, {
    onData(bytes) {
      if (closed) return;
      term.write(bytes, () => {
        if (closed) return;
        const text = screenText(term);
        if (text !== last) {
          last = text;
          onText(text);
        }
        if (PANE_DEAD.test(text)) end();
      });
    },
    onExit: () => end(),
    onError: (message) => end(message),
  });
  if (closed) opened.close();
  else view = opened;
  function resize(cols: number, rows: number) {
    if (closed || (cols === term.cols && rows === term.rows)) return;
    term.resize(cols, rows);
    view?.resize(cols, rows);
  }
  return { resize, close };
}

/* The link to keep when the screen now shows `found`: a fragment of the one
   already read (its first rows, the rest gone with a redraw) doesn't replace it. */
export const keptLink = (had: string | null, found: string): string => (had && had !== found && had.startsWith(found) ? had : found);
