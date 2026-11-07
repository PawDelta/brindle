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

import { useEffect, useState } from "react";
import type { AppState } from "../store/store";
import { button } from "./ui";

/* How long a dropped stream goes unremarked; how long before a next step is offered. */
export const RECONNECT_GRACE_MS = 1000;
export const RECONNECT_LONG_MS = 30000;

export type Reconnect = "none" | "quiet" | "shown" | "long";

/* Where the reconnect is: "none" while connected (or before the engine
   ever answered), "quiet" in the grace after a drop, then "shown", then "long". */
export function useReconnect(connection: AppState["connection"]): Reconnect {
  const up = connection === "open";
  const refused = connection === "unauthorized";
  const [answered, setAnswered] = useState(up);
  const [phase, setPhase] = useState<Reconnect>("none");
  useEffect(() => {
    if (up) setAnswered(true);
  }, [up]);
  useEffect(() => {
    if (up || refused || !answered) {
      setPhase("none");
      return;
    }
    setPhase("quiet");
    const shown = setTimeout(() => setPhase("shown"), RECONNECT_GRACE_MS);
    const long = setTimeout(() => setPhase("long"), RECONNECT_LONG_MS);
    return () => {
      clearTimeout(shown);
      clearTimeout(long);
    };
  }, [up, refused, answered]);
  return phase;
}

export function ReconnectOverlay({ phase, onRetry }: { phase: Reconnect; /* Ask the engine now (the shell starts it again when it can). */ onRetry(): void }) {
  if (phase !== "shown" && phase !== "long") return null;
  return (
    <div className="absolute inset-0 z-50 flex items-center justify-center bg-canvas/70 backdrop-blur-[2px]" data-testid="reconnecting" data-phase={phase}>
      <section className="flex max-w-sm flex-col items-center gap-s2 rounded-lg border border-line-strong bg-surface px-s4 py-s3 text-center shadow-lg" role="status" aria-live="polite" aria-label="Reconnecting to brindle">
        <div className="flex items-center gap-s1">
          <span className="size-3 shrink-0 animate-spin rounded-full border-2 border-branch border-t-transparent" aria-hidden />
          <h2 className="text-base font-semibold">Reconnecting to brindle…</h2>
        </div>
        {phase === "shown" ? (
          <p className="text-sm text-ink-2">The engine is restarting. Your sessions keep their place, and this screen comes back as soon as it answers.</p>
        ) : (
          <>
            <p className="text-sm text-ink-2">The engine is taking longer than usual to come back. brindle keeps trying on its own.</p>
            <p className="text-sm text-ink-2">If it doesn't come back, quit brindle and open it again.</p>
            <button type="button" className={button} onClick={onRetry}>
              Try again now
            </button>
          </>
        )}
      </section>
    </div>
  );
}
