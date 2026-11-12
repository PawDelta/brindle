/* System notifications and the tray badge for the inbox (plan 1.1: a
   notification for each new inbox item; the tray shows the "needs you" count),
   and a calm one when brindle judges a session ready for a PR.

   The inbox reaches the store as snapshots, fetched whenever the event stream
   says something changed; an item whose key wasn't in the previous one is new.
   Open PRs are remembered by URL and kind for this watch's lifetime: pushes
   and CI reruns can temporarily remove their cards or change their kind.
   The first snapshot only records what is already waiting: nothing is
   announced for items that were there before the app opened. A session's
   readiness is the bus's session.pr_ready row (daemon/session_review.py
   ReadinessWatch), announced once, as it arrives. */

import { invoke } from "@tauri-apps/api/core";
import { isPermissionGranted, onAction, registerActionTypes, requestPermission, sendNotification } from "@tauri-apps/plugin-notification";
import { openExternal, type OpenUrl } from "../../api/open";
import type { InboxItemDTO } from "../../api/schema";
import { waitingCount } from "../../store/appUpdates";
import type { AppStore } from "../../store/store";
import { itemKey, kindLabel } from "./items";

export interface InboxSink {
  /* `link`: the page the notification is about (an opened PR), for a sink that can offer it as an action. */
  notify(title: string, body: string, link?: string): void;
  /* The tray / menu-bar badge (src-tauri set_inbox_count). */
  count(n: number): void;
}

export const READY_TITLE = "Ready for a pull request";

export function watchInbox(store: AppStore, sink: InboxSink): () => void {
  let seen: Set<string> | null = null;
  const seenPrs = new Set<string>();
  let last: InboxItemDTO[] | null = null;
  let lastEvent = store.getState().lastEventId;
  let counted: number | null = null;

  const check = () => {
    const state = store.getState();
    const { inbox, synced, recent, lastEventId } = state;
    if (lastEventId !== lastEvent) {
      // (The engine doesn't publish session.pr_ready for a session whose PR is already open.)
      for (const e of recent.filter((e) => e.id > lastEvent && e.kind === "session.pr_ready")) {
        sink.notify(READY_TITLE, `${e.root_id ?? e.agent_id ?? "a session"}: every milestone passed and nothing is running. Review its changes and open the PR.`);
      }
      lastEvent = lastEventId;
    }
    if (!synced) return;
    if (inbox !== last) {
      last = inbox;
      const keys = new Set(inbox.map(itemKey));
      for (const item of inbox) {
        // A failure is remembered with what failed, so a new failure still notifies.
        const prKey = (item.kind === "pr_opened" || item.kind === "pr_failed") && item.url
          ? JSON.stringify([item.kind, item.url, item.kind === "pr_failed" ? item.detail ?? "" : ""]) : null;
        if (seen && (prKey ? !seenPrs.has(prKey) : !seen.has(itemKey(item)))) {
          sink.notify(kindLabel(item), describe(item), item.kind === "pr_opened" ? (item.url ?? undefined) : undefined);
        }
        // Seed this history on initial sync too, without announcing old items.
        if (prKey) seenPrs.add(prKey);
      }
      seen = keys;
    }
    // The badge counts the app's own update item too (store/appUpdates.ts).
    const count = waitingCount(state);
    if (count !== counted) {
      counted = count;
      sink.count(count);
    }
  };

  check();
  return store.subscribe(check);
}

function describe(item: InboxItemDTO): string {
  const who = item.agent_id ? `${item.agent_id}: ` : "";
  // An opened PR says which one, and where (the notification's "Open on GitHub" is its link).
  if (item.kind === "pr_opened") return `${who}${item.reason}${item.detail ? `: ${item.detail}` : ""}${item.url ? ` · ${item.url}` : ""}`;
  return `${who}${item.reason}`;
}

/* The notification calls the sink uses: the plugin's, or a fake in a test. */
export interface NotificationApi {
  isPermissionGranted(): Promise<boolean>;
  requestPermission(): Promise<string>;
  sendNotification(options: { title: string; body: string; actionTypeId?: string; extra?: Record<string, unknown> }): void;
  registerActionTypes(types: { id: string; actions: { id: string; title: string }[] }[]): Promise<void>;
  onAction(cb: (event: { actionId: string; notification: { extra?: Record<string, unknown> } | null }) => void): Promise<unknown>;
}

const PR_ACTIONS = "pr-opened";
export const OPEN_ON_GITHUB = "open-github";

/* The real sink inside the Tauri shell. Notification permission is asked for
   once, the first time there is something to say. A notification with a `link`
   (an opened PR) carries an "Open on GitHub" action, and a click on it opens
   the link in the system browser (the desktop plugin reports clicks and actions
   only for notifications shown while a listener exists, so it is set up first). */
export function tauriSink(api: NotificationApi = { isPermissionGranted, requestPermission, sendNotification, registerActionTypes, onAction } as NotificationApi, open: OpenUrl = openExternal): InboxSink {
  let allowed: Promise<boolean> | null = null;
  let listening: Promise<unknown> | null = null;
  const permitted = () =>
    (allowed ??= api.isPermissionGranted().then(async (ok) => ok || (await api.requestPermission()) === "granted"));
  const listen = () =>
    (listening ??= Promise.all([
      api.registerActionTypes([{ id: PR_ACTIONS, actions: [{ id: OPEN_ON_GITHUB, title: "Open on GitHub" }] }]),
      api.onAction((event) => {
        const url = event.notification?.extra?.url;
        if ((event.actionId === OPEN_ON_GITHUB || event.actionId === "tap") && typeof url === "string") void open(url);
      }),
    ]).catch(() => {
      // Without actions the notification still shows, as text.
    }));
  return {
    notify(title, body, link) {
      void permitted().then(async (ok) => {
        if (!ok) return;
        if (link) {
          await listen();
          api.sendNotification({ title, body, actionTypeId: PR_ACTIONS, extra: { url: link } });
        } else api.sendNotification({ title, body });
      });
    },
    count(n) {
      invoke("set_inbox_count", { count: n }).catch(() => {
        // A badge that didn't update is not worth an error.
      });
    },
  };
}
