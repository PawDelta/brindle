/* The library doc open in the Docs reader right now (its id), or null: DocsView keeps it
   current as the person opens a row or a connected doc and goes back, and the Library rail
   reads it to know which design "Move here" moves. */

import { useSyncExternalStore } from "react";

let current: string | null = null;
const listeners = new Set<() => void>();

export function setReaderDoc(id: string | null): void {
  if (id === current) return;
  current = id;
  listeners.forEach((l) => l());
}

export const useReaderDoc = (): string | null =>
  useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => current,
  );
