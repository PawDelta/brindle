/* Each provider's usage (GET /usage), for the whole app: the Account page's
   Usage section and the sidebar's status-line reading near a limit. The
   numbers move with the providers' own CLIs, not with brindle's bus, so
   they are asked for gently: on start, reconnect, and wake, then once a
   minute, and when the Account page opens; never per event. As in the
   sessions feed (store/sessions.tsx), one load is out at a time, and asked
   again while one is out it runs once more after. */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { DaemonClient, ProviderUsage, Usage } from "../api/client";
import { planWindows } from "../screens/Account/usage";
import { useAppState } from "./useStore";

/* How often the usage is asked for again while the app is open. */
export const USAGE_POLL_MS = 60_000;

export interface WeeklyReading {
  provider: string;
  used: number;
  resetsAt: number | null;
}

export function weeklyReading(provider: ProviderUsage): WeeklyReading | undefined {
  const window = provider.known && planWindows(provider).find((window) => window.minutes === 10080);
  if (window) return { provider: provider.provider, used: window.used, resetsAt: window.resets_at };
}

function readWeekly(key: string | null): WeeklyReading[] {
  if (!key) return [];
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(key) ?? "[]");
    if (Array.isArray(saved)) {
      return saved.filter((reading) => reading && typeof reading.provider === "string" && Number.isFinite(reading.used)
        && (reading.resetsAt === null || Number.isFinite(reading.resetsAt)));
    }
  } catch {
    // The feed still works when local storage is unavailable.
  }
  return [];
}

export interface UsageFeed {
  /* null until the first load answers. */
  usage: Usage | null;
  weekly: WeeklyReading[];
  /* The last failed load's message; the last usage stays, once loaded. */
  error: string | null;
  reload(): Promise<void>;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

const NONE: UsageFeed = { usage: null, weekly: [], error: null, reload: () => Promise.resolve() };

export const UsageContext = createContext<UsageFeed | null>(null);

/* Outside a <UsageProvider> there is nothing to read: no usage, nothing near. */
export function useUsage(): UsageFeed {
  return useContext(UsageContext) ?? NONE;
}

interface UsageScope {
  loading: Promise<void> | null;
  again: boolean;
}

const scopedFeed = (scope: UsageScope, cacheKey: string | null): Omit<UsageFeed, "reload"> & { scope: UsageScope } => ({
  scope, usage: null, weekly: readWeekly(cacheKey), error: null,
});

/* `client` null: no engine to ask (a plain browser tab); the feed stays empty. */
export function UsageProvider({ client, children, pollMs = USAGE_POLL_MS }: { client: DaemonClient | null; children: ReactNode; pollMs?: number }) {
  const subject = useAppState((s) => s.account?.signed_in ? s.account.subject : null);
  const cacheKey = client && subject ? `brindle.usage.weekly.${JSON.stringify([client.baseUrl, subject])}` : null;
  const scope = useMemo<UsageScope>(() => ({ loading: null, again: false }), [client, cacheKey]);
  const [state, setState] = useState(() => scopedFeed(scope, cacheKey));
  // A new account gets its own feed without remounting the active screen.
  if (state.scope !== scope) setState(scopedFeed(scope, cacheKey));
  const { usage, weekly, error } = state;
  const started = useRef<UsageScope | null>(null);

  useEffect(() => {
    if (!cacheKey || !weekly.length) return;
    try {
      localStorage.setItem(cacheKey, JSON.stringify(weekly));
    } catch {
      // This launch can still display the reading from memory.
    }
  }, [cacheKey, weekly]);

  const load = useCallback(async () => {
    if (!client) return;
    try {
      const next = await client.usage();
      const readings = next.providers.flatMap((provider) => weeklyReading(provider) ?? []);
      setState((previous) => {
        if (previous.scope !== scope) return previous;
        const byProvider = new Map(previous.weekly.map((reading) => [reading.provider, reading]));
        for (const reading of readings) byProvider.set(reading.provider, reading);
        return { ...previous, usage: next, weekly: [...byProvider.values()], error: null };
      });
    } catch (e) {
      setState((previous) => previous.scope === scope ? { ...previous, error: message(e) } : previous);
    }
  }, [client, scope]);

  const reload = useCallback((): Promise<void> => {
    if (scope.loading) {
      scope.again = true;
      return scope.loading;
    }
    const run = (async () => {
      try {
        do {
          scope.again = false;
          await load();
        } while (scope.again);
      } finally {
        scope.loading = null;
      }
    })();
    scope.loading = run;
    return run;
  }, [load, scope]);

  // On start and on every (re)connect: an engine that restarted may know more.
  const connection = useAppState((s) => s.connection);
  useEffect(() => {
    if (started.current !== scope || connection === "open") void reload();
    started.current = scope;
  }, [reload, connection, scope]);

  useEffect(() => () => {
    scope.again = false;
  }, [scope]);

  useEffect(() => {
    if (!client) return;
    const wake = () => {
      if (!document.hidden) void reload();
    };
    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      window.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [client, reload]);

  useEffect(() => {
    if (!client) return;
    const timer = setInterval(() => void reload(), pollMs);
    return () => clearInterval(timer);
  }, [client, reload, pollMs]);

  const feed = useMemo<UsageFeed>(() => ({ usage, weekly, error, reload }), [usage, weekly, error, reload]);
  return <UsageContext.Provider value={feed}>{children}</UsageContext.Provider>;
}
