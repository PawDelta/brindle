import { useMemo, useSyncExternalStore } from "react";
import { DaemonError, type DaemonClient } from "../../api/client";
import type { AppStore } from "../../store/store";
import { toFileDiff, type SessionDiffState } from "./review";

const FALLBACK_MS = 60_000;
const MAX_DELAY_MS = 960_000;

/* Scope caches to the daemon and app store so switching connections cannot
   reuse another daemon's data. Both views subscribe to the same root entry. */
const clients = new WeakMap<DaemonClient, WeakMap<AppStore, Map<string, DiffStore>>>();

class DiffStore {
  private listeners = new Set<() => void>();
  private timer: ReturnType<typeof setTimeout> | undefined;
  private unsubscribe: (() => void) | undefined;
  private running = false;
  private queued = false;
  private generation = 0;
  private delay = FALLBACK_MS;
  private blockedUntil = 0;
  private snapshot: SessionDiffState = { diff: null, files: [], error: null, notReady: null, reload: () => void this.load() };

  constructor(private client: DaemonClient, private root: string, private store: AppStore) {}

  getSnapshot = () => this.snapshot;

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    if (this.listeners.size === 1) {
      let previous = this.store.getState();
      this.unsubscribe = this.store.subscribe(() => {
        const next = this.store.getState();
        const changed = next.recent.some((e) => e.id > previous.lastEventId &&
          (e.kind === "session.changed" || e.kind === "session.edited") && e.root_id === this.root);
        const resnapshot = next.stale && !previous.stale && next.lastEventId === previous.lastEventId;
        previous = next;
        if (changed || resnapshot) void this.load();
      });
      document.addEventListener("visibilitychange", this.visibility);
      void this.load();
    }
    return () => {
      this.listeners.delete(listener);
      if (!this.listeners.size) {
        this.generation++;
        this.queued = false;
        clearTimeout(this.timer);
        this.unsubscribe?.();
        document.removeEventListener("visibilitychange", this.visibility);
      }
    };
  };

  private visibility = () => {
    clearTimeout(this.timer);
    if (!document.hidden) void this.load();
  };

  private schedule(ms: number) {
    clearTimeout(this.timer);
    if (this.listeners.size && !document.hidden) this.timer = setTimeout(() => void this.load(), ms);
  }

  private async load() {
    if (!this.listeners.size || document.hidden) return;
    if (Date.now() < this.blockedUntil) {
      this.schedule(this.blockedUntil - Date.now());
      return;
    }
    if (this.running) {
      this.queued = true;
      return;
    }
    clearTimeout(this.timer);
    this.running = true;
    this.queued = false;
    const generation = this.generation;
    try {
      const diff = await this.client.sessionDiff(this.root);
      if (generation !== this.generation || !this.listeners.size) return;
      this.delay = FALLBACK_MS;
      this.blockedUntil = 0;
      this.snapshot = { ...this.snapshot, diff, files: diff.files.map(toFileDiff), error: null, notReady: null };
    } catch (e: unknown) {
      if (generation !== this.generation || !this.listeners.size) return;
      const refused = e instanceof DaemonError && e.code === "not_ready";
      this.delay = refused ? FALLBACK_MS : Math.min(this.delay * 2, MAX_DELAY_MS);
      // A readiness conflict can be rechecked explicitly by the review sheet.
      // Transport failures and busy answers hold off event-triggered reads too.
      this.blockedUntil = refused ? 0 : Date.now() + this.delay;
      this.snapshot = {
        ...this.snapshot,
        error: refused ? null : e instanceof Error ? e.message : String(e),
        notReady: refused ? e.reasons ?? [e.message] : null,
      };
    } finally {
      this.running = false;
      if (generation === this.generation) this.listeners.forEach((listener) => listener());
      if (this.queued) void this.load();
      else this.schedule(Math.max(this.delay, this.blockedUntil - Date.now()));
    }
  }
}

export function useSessionDiff(client: DaemonClient, root: string, store: AppStore): SessionDiffState {
  const shared = useMemo(() => {
    let stores = clients.get(client);
    if (!stores) clients.set(client, stores = new WeakMap());
    let roots = stores.get(store);
    if (!roots) stores.set(store, roots = new Map());
    let entry = roots.get(root);
    if (!entry) roots.set(root, entry = new DiffStore(client, root, store));
    return entry;
  }, [client, root, store]);
  return useSyncExternalStore(shared.subscribe, shared.getSnapshot);
}
