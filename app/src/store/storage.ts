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

/* `started`: when the read behind the figure began. A read that began before
   the one already noted is older news (heavy-lane reads can land out of
   order) and is dropped. `failedAt`: a failed read counts like a successful
   one for the hourly limit. */
let last: { started: number; reclaimable: number } | null = null;
let failedAt = 0;
let reading: Promise<void> | null = null;
const listeners = new Set<() => void>();
let clock = 0;

/* A strictly increasing start stamp for a read (Date.now() can repeat). */
export function readStarted(): number {
  clock = Math.max(clock + 1, Date.now());
  return clock;
}

/* A report read that began at `started` (or a clean just done) sets the
   shared figure, unless a newer read has set it already. */
export function noteReclaimable(reclaimable: number, started: number): void {
  if (last && started < last.started) return;
  last = { started, reclaimable };
  for (const l of listeners) l();
}

/* The shared figure, or null when none was read. */
export function currentReclaimable(): number | null {
  return last?.reclaimable ?? null;
}

/* For tests: forget the figure. */
export function forgetReclaimable(): void {
  last = null;
  failedAt = 0;
  reading = null;
}

/* The figure, reading the report when there is none or it is over an hour
   old, and not within an hour of a failed read. */
export function useReclaimable(client: Pick<DaemonClient, "storage">): number | null {
  const [value, setValue] = useState(currentReclaimable());
  useEffect(() => {
    const update = () => setValue(currentReclaimable());
    listeners.add(update);
    const now = Date.now();
    if (!reading && now - failedAt > STALE_MS && (!last || now - last.started > STALE_MS)) {
      const started = readStarted();
      reading = client.storage().then(
        (r) => noteReclaimable(r.reclaimable, started),
        () => {
          failedAt = Date.now();
        },
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
