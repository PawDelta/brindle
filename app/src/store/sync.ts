/* Keeps the store in step with one daemon: the event stream feeds it, and a
   fresh snapshot and inbox are fetched whenever the stream (re)opens or the
   store goes stale. Fetches never overlap: one asked for during another runs
   once it ends. */

import type { DaemonClient } from "../api/client";
import { EventStream, type EventStreamOptions } from "../api/events";
import { snapshotTicket, type AppStore } from "./store";

export interface SyncOptions {
  client: DaemonClient;
  store: AppStore;
  /* Only this repo's workspaces, inbox and events; all repos when omitted. */
  repo?: string;
  connect?: EventStreamOptions["connect"];
  backoff?: EventStreamOptions["backoff"];
}

export interface Sync {
  stop(): void;
  /* Fetch the snapshot and inbox now (a retry, or after an action's ack). */
  refresh(): Promise<void>;
  /* Settles when no snapshot fetch is in flight or queued (for tests). */
  idle(): Promise<void>;
}

export function startSync({ client, store, repo, connect, backoff }: SyncOptions): Sync {
  let running: Promise<void> | null = null;
  let again = false;

  const fetchOnce = async () => {
    // `since`: what the bus had said when these reads began (a visual settled after it stays out).
    const { lifecycleVersion: version, snapshotVersion: applied, lastEventId: since } = store.getState();
    const acknowledged = client.inboxGeneration;
    const began = snapshotTicket();
    try {
      const [workspaces, inbox] = await Promise.all([client.snapshot(repo), client.inbox(repo)]);
      // Only action completions invalidate reads: ordinary queued refreshes
      // must still apply, even when stream traffic never becomes quiet.
      if (acknowledged !== client.inboxGeneration) {
        again = true;
        return;
      }
      // A close/rewind applied while these reads were pending is newer than
      // their rows. The queued fetch may resume an agent; this one cannot.
      if (version !== store.getState().lifecycleVersion) return;
      // Likewise a snapshot someone else applied meanwhile (the panes gate's, read after
      // recovery): this read began before it and could put older windows back.
      // Its inbox is dropped with it, so read again rather than leave the inbox unsynced.
      if (applied !== store.getState().snapshotVersion) {
        again = true;
        return;
      }
      store.dispatch({ type: "snapshot", workspaces, inbox, began, since });
    } catch (e) {
      store.dispatch({ type: "error", message: e instanceof Error ? e.message : String(e) });
    }
  };

  const refresh = (): Promise<void> => {
    if (running) {
      again = true;
      return running;
    }
    running = (async () => {
      do {
        again = false;
        await fetchOnce();
      } while (again);
      running = null;
    })();
    return running;
  };

  const stream = new EventStream({
    url: (since) => client.eventsUrl(since, repo),
    token: client.token,
    since: store.getState().lastEventId,
    connect,
    backoff,
    onState(state) {
      store.dispatch({ type: "connection", state });
      if (state === "open") void refresh();
    },
    onMessage(message) {
      store.dispatch({ type: "stream", message });
      if (store.getState().stale) void refresh();
    },
  });
  stream.start();

  return {
    stop: () => stream.stop(),
    refresh,
    idle: async () => {
      while (running) await running;
    },
  };
}
