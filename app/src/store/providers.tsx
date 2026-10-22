/* Each provider CLI as brindle detects it (GET /providers/installed), for
   the whole app: Settings › Providers' rows and the sidebar's status line,
   which says only when one has a problem (api/providers.ts). Installs
   started from Settings (POST /providers/{p}/install, followed on the bus by
   api/providersInstall.ts) live here too, so a failed one still shows in
   the status line once Settings is closed. The rows are asked for on start
   and on every (re)connect, then once a minute (a sign-in can lapse on its
   own), and again after an install, a removal or a sign-in. */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { DaemonClient, ProviderInstalled } from "../api/client";
import { useInstalls, type Install } from "../api/providersInstall";
import { useAppState } from "./useStore";

/* How often the rows are asked for again while the app is open. */
export const PROVIDERS_POLL_MS = 60_000;

export interface ProvidersFeed {
  /* null until the first load answers (or with no engine, or one without the route). */
  rows: ProviderInstalled[] | null;
  /* The last failed load's message; the last rows stay, once loaded. */
  error: string | null;
  /* Asks again; answers the rows (null when the ask failed). */
  reload(): Promise<ProviderInstalled[] | null>;
  /* The installs started here, by provider: running; failed until retried or
     dismissed; done until the rows have been read again after it. */
  installs: Record<string, Install>;
  /* Runs the maker's official installer as-is; done, the rows are read again. */
  install(provider: string): Promise<void>;
  /* Turns a provider that is off back on, running the copy still found (POST
     /providers/{p}/use-mine): no installer runs. */
  turnOn(provider: string, path: string): Promise<void>;
  /* Lets a failed install go. */
  dismiss(provider: string): void;
  /* Takes the provider out of brindle (DELETE /providers/{p}); the CLI stays installed. */
  remove(provider: string): Promise<void>;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

const NONE: ProvidersFeed = {
  rows: null,
  error: null,
  reload: () => Promise.resolve(null),
  installs: {},
  install: () => Promise.resolve(),
  turnOn: () => Promise.resolve(),
  dismiss: () => undefined,
  remove: () => Promise.resolve(),
};

export const ProvidersContext = createContext<ProvidersFeed | null>(null);

/* Outside a <ProvidersProvider> there is nothing to read: no rows, no problems. */
export function useProviders(): ProvidersFeed {
  return useContext(ProvidersContext) ?? NONE;
}

/* `client` null: no engine to ask (a plain browser tab); the feed stays empty. */
export function ProvidersProvider({ client, children, pollMs = PROVIDERS_POLL_MS }: { client: DaemonClient | null; children: ReactNode; pollMs?: number }) {
  if (!client) return <>{children}</>;
  return (
    <Feed client={client} pollMs={pollMs}>
      {children}
    </Feed>
  );
}

function Feed({ client, children, pollMs }: { client: DaemonClient; children: ReactNode; pollMs: number }) {
  const [rows, setRows] = useState<ProviderInstalled[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  /* The newest ask: an older answer arriving late never replaces it. */
  const seq = useRef(0);

  // An install done: the rows read again say where the installer put the CLI.
  const reloadRef = useRef<() => Promise<unknown>>(() => Promise.resolve());
  const { installs, start, forget, settle } = useInstalls(client, () => void reloadRef.current());

  const reload = useCallback(async () => {
    const mine = ++seq.current;
    try {
      const got = await client.providersInstalled();
      if (mine === seq.current) {
        setRows(got);
        setError(null);
        // Only rows read after an install finished let its card go (an older
        // answer is dropped above); until then the card stays, saying it's done.
        settle();
      }
      return got;
    } catch (e) {
      if (mine === seq.current) setError(message(e));
      return null;
    }
  }, [client, settle]);
  reloadRef.current = reload;

  const remove = useCallback(
    async (provider: string) => {
      await client.removeProvider(provider);
      await reload();
    },
    [client, reload],
  );

  const turnOn = useCallback(
    async (provider: string, path: string) => {
      await client.pickProviderInstall(provider, path);
      await reload();
    },
    [client, reload],
  );

  // On start and on every (re)connect: an engine that restarted may find more.
  const connection = useAppState((s) => s.connection);
  useEffect(() => {
    if (connection === "open" || connection === "idle") void reload();
  }, [reload, connection]);

  useEffect(() => {
    const timer = setInterval(() => void reload(), pollMs);
    return () => clearInterval(timer);
  }, [reload, pollMs]);

  const feed = useMemo<ProvidersFeed>(() => ({ rows, error, reload, installs, install: start, turnOn, dismiss: forget, remove }), [rows, error, reload, installs, start, turnOn, forget, remove]);
  return <ProvidersContext.Provider value={feed}>{children}</ProvidersContext.Provider>;
}
