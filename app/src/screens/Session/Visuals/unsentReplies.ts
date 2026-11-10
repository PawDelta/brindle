/* The replies a person wrote on a design doc that no verdict has carried to the supervisor yet,
   by plan id (PlanReader.tsx). The engine's reply carries no "sent" of its own, and only a
   verdict delivers one (a new revision does not), so the reader keeps their ids itself: on this
   device, across its own closing and the supervisor's revisions.

   The record is changed from answers that arrive whenever they arrive, to readers that may have
   been closed and opened again since they asked. So there is one record, never a reader's copy
   of it: every change is made to the record as it is at that moment (`changeUnsentReplies` adds
   or takes away, it never puts back what a reader once saw), and every open reader follows it. */

import { useMemo, useSyncExternalStore } from "react";

export const UNSENT_REPLIES_KEY = "brindle.plan.unsentReplies";

// The record, when storage can't hold it (off, or full): kept for as long as the app is open.
let memory: string | null = null;
const listeners = new Set<() => void>();

function read(): string {
  if (memory !== null) return memory;
  try {
    return localStorage.getItem(UNSENT_REPLIES_KEY) ?? "{}";
  } catch {
    return (memory = "{}");
  }
}

function parse(raw: string): Record<string, number[]> {
  const out: Record<string, number[]> = {};
  try {
    const all: unknown = JSON.parse(raw);
    if (all && typeof all === "object" && !Array.isArray(all)) {
      for (const [plan, ids] of Object.entries(all)) if (Array.isArray(ids)) out[plan] = ids.filter((id): id is number => typeof id === "number");
    }
  } catch {
    // Unreadable: nothing is recorded.
  }
  return out;
}

/* Change one plan's record, from what it holds now. */
export function changeUnsentReplies(plan: string, change: (ids: number[]) => number[]) {
  const all = parse(read());
  const next = [...new Set(change(all[plan] ?? []))];
  if (next.length) all[plan] = next;
  else delete all[plan];
  const raw = JSON.stringify(all);
  if (memory === null) {
    try {
      localStorage.setItem(UNSENT_REPLIES_KEY, raw);
    } catch {
      memory = raw;
    }
  } else memory = raw;
  listeners.forEach((changed) => changed());
}

function subscribe(changed: () => void) {
  listeners.add(changed);
  return () => {
    listeners.delete(changed);
  };
}

/* One plan's record, followed as it changes. */
export function useUnsentReplies(plan: string): number[] {
  const raw = useSyncExternalStore(subscribe, read);
  return useMemo(() => parse(raw)[plan] ?? [], [raw, plan]);
}
