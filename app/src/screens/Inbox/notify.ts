/* System notifications and the tray badge for the inbox (plan 1.1: a
   notification for each new inbox item; the tray shows the "needs you" count),
   and a calm one when brindle judges a session ready for a PR.

   The inbox reaches the store as snapshots, fetched whenever the event stream
   says something changed; an item whose key wasn't in the previous one is new.
   The first snapshot only records what is already waiting: nothing is
   announced for items that were there before the app opened. A session's
   readiness is the bus's session.pr_ready row (daemon/session_review.py
   ReadinessWatch), announced once, as it arrives. */

import { invoke } from "@tauri-apps/api/core";
import { isPermissionGranted, requestPermission, sendNotification } from "@tauri-apps/plugin-notification";
import type { InboxItemDTO } from "../../api/schema";
import type { AppStore } from "../../store/store";
import { itemKey, kindLabel } from "./items";

export interface InboxSink {
  notify(title: string, body: string): void;
  /* The tray / menu-bar badge (src-tauri set_inbox_count). */
  count(n: number): void;
}

export const READY_TITLE = "Ready for a pull request";

export function watchInbox(store: AppStore, sink: InboxSink): () => void {
  let seen: Set<string> | null = null;
  let last: InboxItemDTO[] | null = null;
  let lastEvent = store.getState().lastEventId;

  const check = () => {
    const { inbox, synced, recent, lastEventId } = store.getState();
    if (lastEventId !== lastEvent) {
      for (const e of recent.filter((e) => e.id > lastEvent && e.kind === "session.pr_ready")) {
        sink.notify(READY_TITLE, `${e.root_id ?? e.agent_id ?? "a session"}: every milestone passed and nothing is running. Review its changes and open the PR.`);
      }
      lastEvent = lastEventId;
    }
    if (!synced || inbox === last) return;
    last = inbox;
    const keys = new Set(inbox.map(itemKey));
    if (seen) {
      for (const item of inbox) {
        if (!seen.has(itemKey(item))) sink.notify(kindLabel(item), describe(item));
      }
    }
    seen = keys;
    sink.count(inbox.length);
  };

  check();
  return store.subscribe(check);
}

function describe(item: InboxItemDTO): string {
  const who = item.agent_id ? `${item.agent_id}: ` : "";
  return `${who}${item.reason}`;
}

/* The real sink inside the Tauri shell. Notification permission is asked for
   once, the first time there is something to say. */
export function tauriSink(): InboxSink {
  let allowed: Promise<boolean> | null = null;
  const permitted = () =>
    (allowed ??= isPermissionGranted().then(async (ok) => ok || (await requestPermission()) === "granted"));
  return {
    notify(title, body) {
      void permitted().then((ok) => ok && sendNotification({ title, body }));
    },
    count(n) {
      invoke("set_inbox_count", { count: n }).catch(() => {
        // A badge that didn't update is not worth an error.
      });
    },
  };
}
