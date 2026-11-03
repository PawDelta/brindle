/* What brindle could give back on disk (GET /storage's `reclaimable`), shared
   by Settings › Storage and the Inbox's nudge. The report is slow (the daemon
   measures every worktree), so the Inbox reads it at most once an hour and
   Settings, which reads it anyway, keeps the figure current: a Clean there
   lowers the nudge at once. An engine without the route leaves it null. */

import { useEffect, useState } from "react";
import type { DaemonClient } from "../api/client";

/* Above this the Inbox suggests a clean. */
export const NUDGE_BYTES = 5 * 1024 ** 3;
const STALE_MS = 60 * 60 * 1000;

const GB = 1024 ** 3;
const MB = 1024 ** 2;

/* Bytes as the engine's disk.human writes them: "4.2 GB", "310 MB". */
export function bytes(n: number): string {
  return n >= GB / 10 ? `${(n / GB).toFixed(1)} GB` : `${Math.round(n / MB)} MB`;
}

let last: { at: number; reclaimable: number } | null = null;
let reading: Promise<void> | null = null;
const listeners = new Set<() => void>();

/* A report just read (or a clean just done) sets the shared figure. */
export function noteReclaimable(reclaimable: number): void {
  last = { at: Date.now(), reclaimable };
  for (const l of listeners) l();
}

/* For tests: forget the figure. */
export function forgetReclaimable(): void {
  last = null;
  reading = null;
}

/* The figure, reading the report when there is none or it is over an hour old. */
export function useReclaimable(client: Pick<DaemonClient, "storage">): number | null {
  const [value, setValue] = useState(last?.reclaimable ?? null);
  useEffect(() => {
    const update = () => setValue(last?.reclaimable ?? null);
    listeners.add(update);
    if (!reading && (!last || Date.now() - last.at > STALE_MS)) {
      reading = client.storage().then(
        (r) => noteReclaimable(r.reclaimable),
        () => undefined,
      ).finally(() => {
        reading = null;
      });
    }
    update();
    return () => {
      listeners.delete(update);
    };
  }, [client]);
  return value;
}
