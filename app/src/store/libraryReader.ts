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

/* How many times a row of the Library's designs was picked in its sidebar: each one closes a
   design open in the reader, so what was picked shows. */
let picks = 0;
const pickListeners = new Set<() => void>();

export function libraryPicked(): void {
  picks++;
  pickListeners.forEach((l) => l());
}

export const useLibraryPicks = (): number =>
  useSyncExternalStore(
    (l) => {
      pickListeners.add(l);
      return () => pickListeners.delete(l);
    },
    () => picks,
  );

/* How many times the Library's folders or a design's place changed here (a folder made, a design
   moved): everything that read them (the sidebar's counts, the list and board) reads them again. */
let changes = 0;
const changeListeners = new Set<() => void>();

export function libraryChanged(): void {
  changes++;
  changeListeners.forEach((l) => l());
}

export const useLibraryChanges = (): number =>
  useSyncExternalStore(
    (l) => {
      changeListeners.add(l);
      return () => changeListeners.delete(l);
    },
    () => changes,
  );
