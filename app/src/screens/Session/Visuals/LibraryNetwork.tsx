/* Library calls over a slow or missing network (brindle/service/errors.py network_error): the
   daemon answers `library_slow` or `library_offline` instead of socket text, and these show a calm
   note with Try again. A call still out after SLOW_NOTE_AFTER_MS says so, so it doesn't look hung. */

import { useEffect, useState } from "react";
import { DaemonError } from "../../../api/client";

export type LibraryNetwork = "slow" | "offline";

const CODES: Record<string, LibraryNetwork> = { library_slow: "slow", library_offline: "offline" };
const SAY: Record<LibraryNetwork, string> = {
  slow: "The connection to the Library is slow right now.",
  offline: "This Mac looks offline, so the Library can't be reached.",
};
export const SLOW_NOTE_AFTER_MS = 3000;

export function libraryNetwork(e: unknown): LibraryNetwork | null {
  return e instanceof DaemonError ? CODES[e.code] ?? null : null;
}

/* True once `pending` has stayed true for SLOW_NOTE_AFTER_MS; false again as soon as it ends. */
export function useSlowPending(pending: boolean): boolean {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    setSlow(false);
    if (!pending) return;
    const id = window.setTimeout(() => setSlow(true), SLOW_NOTE_AFTER_MS);
    return () => window.clearTimeout(id);
  }, [pending]);
  return slow;
}

export function LibraryNetworkNote({ state, onTryAgain, hint, className = "msg", buttonClassName = "sp-btn q end" }: {
  state: LibraryNetwork; onTryAgain?(): void; hint?: string; className?: string; buttonClassName?: string;
}) {
  return <div className={className} role="alert" data-testid="library-network" data-state={state}>
    <span className="grow">{SAY[state]}{hint && ` ${hint}`}</span>
    {onTryAgain && <button type="button" className={buttonClassName} onClick={onTryAgain}>Try again</button>}
  </div>;
}

/* The same, inline on a row whose write failed: the row keeps its own Try again. */
const SHORT: Record<LibraryNetwork, string> = { slow: "Slow connection", offline: "Offline" };
export function LibraryNetworkLabel({ state }: { state: LibraryNetwork }) {
  return <span className="st" data-testid="library-network" data-state={state} title={SAY[state]}>{SHORT[state]}</span>;
}

export function SlowConnectionNote({ className = "msg" }: { className?: string }) {
  return <div className={className} role="status" data-testid="library-slow" data-state="slow">Slow connection, still working…</div>;
}
