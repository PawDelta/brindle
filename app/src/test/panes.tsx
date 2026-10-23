/* For tests that render the app over the fake daemon without drawing its
   panes (jsdom has no canvas; SessionScreen.test covers the real
   PtyTerminal). In such a test file:

     vi.mock("./components/PtyTerminal", () => import("./test/panes"));

   and the app rendered under <PtyViewsProvider value={standInViews}> (or
   the fake daemon's own ptyViews(), where what a pane shows is read, as a
   sign-in reads its CLI's). */

import { useEffect, useRef } from "react";
import type { PtyViews } from "../api/ptyView";
import type { PtyTerminalProps } from "../components/PtyTerminal";

/* The size the stand-in says it has (as the real one does once open): a card's or a pane's, narrower than a sign-in's reader. */
export const STAND_IN_SIZE = { cols: 80, rows: 24 };

/* PtyTerminal's stand-in: it names the window it shows by its last part, the agent's id as fixtures.windowOf names it. */
export function PtyTerminal({ at, onSize }: PtyTerminalProps) {
  const sized = useRef(onSize);
  sized.current = onSize;
  useEffect(() => sized.current?.(STAND_IN_SIZE.cols, STAND_IN_SIZE.rows), []);
  return (
    <div data-testid="terminal" data-target={at.target}>
      {at.target.slice(at.target.lastIndexOf(":") + 1)}
    </div>
  );
}

/* Pty views that can show panes, as the shell's say once tmux is found; the stand-in opens nothing. */
export const standInViews: PtyViews = {
  status: { kind: "ready", tmux: "tmux 3.8" },
  open: () => ({ write() {}, resize() {}, focus() {}, close() {} }),
  copy: () => Promise.resolve(),
  paste: () => Promise.resolve(""),
  recheck() {},
};
