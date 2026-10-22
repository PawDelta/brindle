/* Each provider's usage (GET /usage), for the whole app: the Account page's
   Usage section and the sidebar's status-line reading near a limit. The
   numbers move with the providers' own CLIs, not with brindle's bus, so
   they are asked for gently: on start and on every (re)connect, then once a
   minute, and when the Account page opens; never per event. As in the
   sessions feed (store/sessions.tsx), one load is out at a time, and asked
   again while one is out it runs once more after. */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { DaemonClient, Usage } from "../api/client";
import { useAppState } from "./useStore";

/* How often the usage is asked for again while the app is open. */
export const USAGE_POLL_MS = 60_000;

export interface UsageFeed {
  /* null until the first load answers. */
  usage: Usage | null;
  /* The last failed load's message; the last usage stays, once loaded. */
  error: string | null;
  reload(): Promise<void>;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

const NONE: UsageFeed = { usage: null, error: null, reload: () => Promise.resolve() };

export const UsageContext = createContext<UsageFeed | null>(null);

/* Outside a <UsageProvider> there is nothing to read: no usage, nothing near. */
export function useUsage(): UsageFeed {
  return useContext(UsageContext) ?? NONE;
}

/* `client` null: no engine to ask (a plain browser tab); the feed stays empty. */
export function UsageProvider({ client, children, pollMs = USAGE_POLL_MS }: { client: DaemonClient | null; children: ReactNode; pollMs?: number }) {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loading = useRef<Promise<void> | null>(null);
  const again = useRef(false);

  const load = useCallback(async () => {
    if (!client) return;
    try {
      setUsage(await client.usage());
      setError(null);
    } catch (e) {
      setError(message(e));
    }
  }, [client]);

  const reload = useCallback((): Promise<void> => {
    if (loading.current) {
      again.current = true;
      return loading.current;
    }
    const run = (async () => {
      try {
        do {
          again.current = false;
          await load();
        } while (again.current);
      } finally {
        loading.current = null;
      }
    })();
    loading.current = run;
    return run;
  }, [load]);

  // On start and on every (re)connect: an engine that restarted may know more.
  const connection = useAppState((s) => s.connection);
  useEffect(() => {
    if (connection === "open" || connection === "idle") void reload();
  }, [reload, connection]);

  useEffect(() => {
    if (!client) return;
    const timer = setInterval(() => void reload(), pollMs);
    return () => clearInterval(timer);
  }, [client, reload, pollMs]);

  const feed = useMemo<UsageFeed>(() => ({ usage, error, reload }), [usage, error, reload]);
  return <UsageContext.Provider value={feed}>{children}</UsageContext.Provider>;
}
