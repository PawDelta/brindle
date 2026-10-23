/* Staff-only debug logging (owner decision), the app's side. The daemon's
   GET /v1/debug says whether the account is staff and whether logging is
   on; Settings shows its one toggle to staff only (PUT /v1/debug). While it
   is on, the app's own events (a pane view that couldn't open or attach)
   go to POST /v1/debug/log, uploaded to brindle with the engine's.
   logDebug is fire-and-forget: it never throws, never waits, and does
   nothing while logging is off or before the app knows. */

import type { DaemonClient, DebugLevel, DebugState } from "./client";

let sink: DaemonClient | null = null;
let enabled = false;

/* The client logs go through, and what the daemon says now: at start (main.tsx), and on each toggle in Settings. */
export function debugState(client: DaemonClient, state: DebugState): void {
  sink = client;
  enabled = state.staff && state.enabled;
}

/* Asks the daemon once whether logging is on; one without the route, or not answering, leaves it off. */
export function watchDebug(client: DaemonClient): void {
  client.debug().then(
    (state) => debugState(client, state),
    () => {},
  );
}

export function logDebug(source: string, level: DebugLevel, message: string, detail?: Record<string, unknown>): void {
  if (!sink || !enabled) return;
  try {
    void sink.debugLog({ source, level, message, ...(detail ? { detail } : {}) }).catch(() => {});
  } catch {
    // Never the caller's problem.
  }
}
