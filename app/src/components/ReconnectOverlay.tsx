/* What the app shows while its engine is away after having answered (the
   daemon restarted, crashed, or is being replaced by an update): the last
   good screen stays where it was, dimmed under a calm "Reconnecting to
   brindle…", rather than the raw state of every screen that just lost its
   engine (failed reads, closed panes, the stream's error). The app keeps
   reconnecting on its own (store/sync.ts) and the overlay leaves the moment
   the engine answers. A short drop shows nothing: the overlay waits a
   moment before it appears. After a long wait it says what to do next. A
   first connection that never opens, or a refused token, is the engine
   banner's (components/EngineBanner), not this. */

import { createContext, useEffect, useRef, useState, type RefObject } from "react";
import type { AppState } from "../store/store";
import { button } from "./ui";

/* Whether the whole app is under the overlay. What the page's own layers can't cover (a
   visual's native webview, which sits above the page: screens/Session/Visuals/NativeFrame)
   reads it and steps aside, keeping its state, until the overlay goes. */
export const AppCovered = createContext(false);

/* How long a dropped stream goes unremarked; how long before a next step is offered. */
export const RECONNECT_GRACE_MS = 1000;
export const RECONNECT_LONG_MS = 30000;

export type Reconnect = "none" | "quiet" | "shown" | "long";

/* Where the reconnect is: "none" while connected (or before the engine
   ever answered), "quiet" in the grace after a drop, then "shown", then "long".
   `focusBefore`: what had the focus as the overlay was about to show, read
   before the update that shows it, since that update makes the app inert
   and the browser then moves the focus off it (to the body) before any
   effect of the overlay's could read it. */
export function useReconnect(connection: AppState["connection"]): { phase: Reconnect; focusBefore: RefObject<HTMLElement | null> } {
  const up = connection === "open";
  const refused = connection === "unauthorized";
  const [answered, setAnswered] = useState(up);
  const [phase, setPhase] = useState<Reconnect>("none");
  const focusBefore = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (up) setAnswered(true);
  }, [up]);
  useEffect(() => {
    if (up || refused || !answered) {
      setPhase("none");
      return;
    }
    setPhase("quiet");
    const shown = setTimeout(() => {
      const now = document.activeElement;
      focusBefore.current = now instanceof HTMLElement && now !== document.body ? now : null;
      setPhase("shown");
    }, RECONNECT_GRACE_MS);
    const long = setTimeout(() => setPhase("long"), RECONNECT_LONG_MS);
    return () => {
      clearTimeout(shown);
      clearTimeout(long);
    };
  }, [up, refused, answered]);
  return { phase, focusBefore };
}

/* The overlay while it shows: a global shortcut of the app's own checks for it and stands down. */
export const RECONNECTING = '[data-testid="reconnecting"]';

/* What an overlaid app must not hear: keys and pastes aimed anywhere but the overlay. */
const HELD = ["keydown", "keypress", "keyup", "beforeinput", "paste"] as const;

/* Shown, the overlay is modal: the app under it is inert (the Shell's), the focus moves into it
   (onto Try again once that's offered), Tab stays there, and keys or pastes aimed at the app under
   it (a terminal that had the focus, ⌘N's, ⌘V's window handlers) are held at the window before
   they reach anything. Gone, the focus goes back where it was. */
export function ReconnectOverlay({ phase, focusBefore, onRetry }: {
  phase: Reconnect;
  /* Where the focus goes back to (useReconnect's, read before the app went inert). */
  focusBefore?: RefObject<HTMLElement | null>;
  /* Ask the engine now (the shell starts it again when it can). */
  onRetry(): void;
}) {
  const visible = phase === "shown" || phase === "long";
  const layer = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLElement>(null);
  const tryAgain = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!visible) return;
    const before = focusBefore?.current ?? null;
    const hold = (e: Event) => {
      if (e.target instanceof Node && layer.current?.contains(e.target)) return;
      e.stopPropagation();
      // The shell's own menu shortcuts (⌘Q) keep their default; nothing reaches the page under the
      // overlay either way, and a paste, held separately, has its default stopped.
      if (!(e instanceof KeyboardEvent && e.metaKey)) e.preventDefault();
    };
    for (const kind of HELD) window.addEventListener(kind, hold, true);
    return () => {
      for (const kind of HELD) window.removeEventListener(kind, hold, true);
      const now = document.activeElement;
      if (before?.isConnected && (!now || now === document.body || !now.isConnected)) before.focus();
    };
  }, [visible, focusBefore]);
  useEffect(() => {
    if (phase === "long") tryAgain.current?.focus();
    else if (phase === "shown") box.current?.focus();
  }, [phase]);
  if (!visible) return null;
  return (
    <div
      ref={layer}
      className="absolute inset-0 z-50 flex items-center justify-center bg-canvas/70 backdrop-blur-[2px]"
      data-testid="reconnecting"
      data-phase={phase}
      // Inside the overlay too, nothing goes on to the window's shortcuts; Tab has nowhere else to go.
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === "Tab") e.preventDefault();
      }}
    >
      <section
        ref={box}
        tabIndex={-1}
        className="flex max-w-sm flex-col items-center gap-s2 rounded-lg border border-line-strong bg-surface px-s4 py-s3 text-center shadow-lg outline-none"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="reconnecting-title"
        aria-describedby="reconnecting-why"
      >
        <div className="flex items-center gap-s1">
          <span className="size-3 shrink-0 animate-spin rounded-full border-2 border-branch border-t-transparent" aria-hidden />
          <h2 id="reconnecting-title" className="text-base font-semibold">Reconnecting to brindle…</h2>
        </div>
        {phase === "shown" ? (
          <p id="reconnecting-why" className="text-sm text-ink-2">The engine is restarting. Your sessions keep their place, and this screen comes back as soon as it answers.</p>
        ) : (
          <>
            <div id="reconnecting-why" className="flex flex-col gap-s1">
              <p className="text-sm text-ink-2">The engine is taking longer than usual to come back. brindle keeps trying on its own.</p>
              <p className="text-sm text-ink-2">If it doesn't come back, quit brindle and open it again.</p>
            </div>
            <button ref={tryAgain} type="button" className={button} onClick={onRetry}>
              Try again now
            </button>
          </>
        )}
      </section>
    </div>
  );
}
