/* The app's own updates in the store, so everything that counts what waits
   on the person (the Inbox, the sidebar's "Inbox · N", the tray badge) sees
   the same item: an update the shell installed by itself waits for a restart,
   then, once, "Updated to brindle X" until dismissed (src-tauri/src/updates.rs).
   Only a build that updates by itself (a release, not an rc or a staff build:
   `automatic`) has one. */

import type { AppUpdateStatus, AppUpdates } from "../api/tauri";
import type { AppState, AppStore } from "./store";

export interface AppUpdateItem {
  kind: "pending" | "updated";
  version: string;
}

/* The item the status calls for, if any. */
export function appUpdateItem(status: AppUpdateStatus | null): AppUpdateItem | null {
  if (!status?.automatic) return null;
  if (status.pending) return { kind: "pending", version: status.pending };
  if (status.updated) return { kind: "updated", version: status.updated };
  return null;
}

/* Everything waiting on the person: the engine's inbox and the app's own item. */
export const waitingCount = (state: AppState): number => state.inbox.length + (appUpdateItem(state.appUpdate) ? 1 : 0);

/* Keep the store's status current. The shell is listened to first, and only
   then asked for the status now, which counts only when no announcement came
   meanwhile: one that did is newer. Answers a function that stops. */
export function watchAppUpdates(store: AppStore, updates: AppUpdates): () => void {
  let live = true;
  let heard = false;
  let stop = () => {};
  updates
    .watch((status) => {
      heard = true;
      if (live) store.dispatch({ type: "app_update", status });
    })
    .then((unlisten) => {
      if (!live) return unlisten();
      stop = unlisten;
      return updates.status().then((status) => {
        if (live && !heard) store.dispatch({ type: "app_update", status });
      });
    })
    .catch(() => {
      // No status to show is no item: the Inbox stays as the engine has it.
    });
  return () => {
    live = false;
    stop();
  };
}
