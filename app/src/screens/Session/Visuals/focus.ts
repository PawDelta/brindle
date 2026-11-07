/* Focus while a visual is open: the sidebar hides down to an edge handle instead of slimming to a
   rail (design/visualizer-build-plan.html §2). Remembered per device, so a person who reads with
   Focus on gets it next time. One store, so the reader's Focus and the app's sidebar agree. */

import { useSyncExternalStore } from "react";

export const FOCUS_KEY = "brindle.visuals.focus";

/* How the sidebar draws: full, the 52px rail, or hidden behind an edge handle. */
export type SidebarMode = "full" | "rail" | "hidden";

function read(): boolean {
  try {
    return localStorage.getItem(FOCUS_KEY) === "1";
  } catch {
    return false;
  }
}

let focus = read();
const listeners = new Set<() => void>();

export function setFocus(next: boolean): void {
  focus = next;
  try {
    localStorage.setItem(FOCUS_KEY, next ? "1" : "0");
  } catch {
    // No storage: Focus lasts this launch.
  }
  for (const l of listeners) l();
}

export function useFocus(): boolean {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => focus,
  );
}

/* The sidebar's mode for what's on screen: full unless a visual is open. */
export const sidebarMode = (reading: boolean, focused: boolean): SidebarMode => (!reading ? "full" : focused ? "hidden" : "rail");
