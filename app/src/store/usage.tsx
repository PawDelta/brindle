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

const scopedProviders = ["claude", "codex"] as const;
type ScopedProvider = typeof scopedProviders[number];
type UsageIdentities = Record<ScopedProvider, string | null>;
type UsageScopeFields = Pick<Usage, "claude_usage_scope" | "codex_usage_scope">;

const identitiesOf = (data?: UsageScopeFields | null): UsageIdentities => ({
  claude: data?.claude_usage_scope || null, codex: data?.codex_usage_scope || null,
});
const sameIdentities = (a: UsageIdentities, b: UsageIdentities) =>
  scopedProviders.every((provider) => a[provider] === b[provider]);

const weeklyKey = (accountKey: string | null, provider: ScopedProvider, identity: string | null) =>
  accountKey && identity ? `brindle.usage.${provider}.weekly.${JSON.stringify([accountKey, identity])}` : null;

function readWeekly(key: string | null, provider: ScopedProvider): WeeklyReading | undefined {
  if (!key) return;
  try {
    const saved = JSON.parse(localStorage.getItem(key) ?? "null");
    if (saved?.provider === provider && Number.isFinite(saved.used)
      && (saved.resetsAt === null || Number.isFinite(saved.resetsAt))) return saved;
  } catch {
    // The feed still works when local storage is unavailable.
  }
}

const readSaved = (accountKey: string | null, identities: UsageIdentities) =>
  scopedProviders.flatMap((provider) => readWeekly(weeklyKey(accountKey, provider, identities[provider]), provider) ?? []);

export interface UsageFeed {
  /* null until the first load answers. */
  usage: Usage | null;
  saved: WeeklyReading[];
  checking: boolean;
  /* The last failed load's message; the last usage stays, once loaded. */
  error: string | null;
  reload(): Promise<void>;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

const NONE: UsageFeed = { usage: null, saved: [], checking: false, error: null, reload: () => Promise.resolve() };

export const UsageContext = createContext<UsageFeed | null>(null);

/* Outside a <UsageProvider> there is nothing to read: no usage, nothing near. */
export function useUsage(): UsageFeed {
  return useContext(UsageContext) ?? NONE;
}

interface UsageScope {
  loading: Promise<void> | null;
  again: boolean;
  checking: Promise<void> | null;
  generation: number;
  identities: UsageIdentities;
}

type ScopedFeed = Omit<UsageFeed, "reload"> & { scope: UsageScope; identities: UsageIdentities };

const scopedFeed = (scope: UsageScope, accountKey: string | null): ScopedFeed => ({
  scope, identities: scope.identities, usage: null, saved: readSaved(accountKey, scope.identities), checking: false, error: null,
});

const filterUsage = (usage: Usage | null, show: (provider: string) => boolean): Usage | null => usage && {
  ...usage,
  providers: usage.providers.filter((p) => show(p.provider)),
  near: usage.near.filter((p) => show(p.provider)),
};
const isScoped = (provider: string): provider is ScopedProvider => provider === "claude" || provider === "codex";

/* `client` null: no engine to ask (a plain browser tab); the feed stays empty. */
export function UsageProvider({ client, children, pollMs = USAGE_POLL_MS }: { client: DaemonClient | null; children: ReactNode; pollMs?: number }) {
  const subject = useAppState((s) => s.account?.signed_in ? s.account.subject : null);
  const claude = useAppState((s) => s.account?.signed_in ? s.account.claude_usage_scope ?? null : null);
  const codex = useAppState((s) => s.account?.signed_in ? s.account.codex_usage_scope ?? null : null);
  const accountKey = client && subject ? JSON.stringify([client.baseUrl, subject]) : null;
  const scope = useMemo<UsageScope>(() => ({
    loading: null, again: false, checking: null, generation: 0, identities: { claude, codex },
  }), [client, accountKey, claude, codex]);
  const [state, setState] = useState(() => scopedFeed(scope, accountKey));
  // A new account gets its own feed without remounting the active screen.
  if (state.scope !== scope) setState(scopedFeed(scope, accountKey));
  const { usage, saved, checking, identities, error } = state;
  const started = useRef<UsageScope | null>(null);

  useEffect(() => {
    try {
      for (const reading of saved) {
        if (!isScoped(reading.provider)) continue;
        const key = weeklyKey(accountKey, reading.provider, identities[reading.provider]);
        if (key) localStorage.setItem(key, JSON.stringify(reading));
      }
    } catch {
      // This launch can still display the reading from memory.
    }
  }, [accountKey, identities, saved]);

  const update = useCallback((generation: number, identities: UsageIdentities, next?: Usage, error: string | null = null) => {
    if (generation !== scope.generation) return;
    scope.identities = identities;
    const stored = readSaved(accountKey, identities);
    setState((previous) => {
      if (previous.scope !== scope || generation !== scope.generation) return previous;
      const saved = scopedProviders.flatMap((provider) => {
        if (!identities[provider]) return [];
        const current = next?.providers.find((p) => p.provider === provider);
        return (current && weeklyReading(current)) ??
          (previous.identities[provider] === identities[provider] ? previous.saved.find((p) => p.provider === provider) : undefined) ??
          stored.find((p) => p.provider === provider) ?? [];
      });
      const usage = next ?? filterUsage(previous.usage, (provider) =>
        !isScoped(provider) || previous.identities[provider] === identities[provider]);
      return { ...previous, usage, identities, saved, checking: false, error };
    });
  }, [accountKey, scope]);

  const load = useCallback(async () => {
    if (!client) return;
    const generation = scope.generation;
    const refresh = async (requested: UsageIdentities) => {
      const next = await client.usage(true, requested.claude);
      while (scope.checking) await scope.checking;
      if (generation !== scope.generation) return false;
      const received = identitiesOf(next);
      if (requested !== scope.identities && !sameIdentities(received, scope.identities)) {
        scope.again = true;
        return false;
      }
      update(generation, received, next);
      return true;
    };
    let requested = scope.identities;
    try {
      while (scope.checking) await scope.checking;
      if (generation !== scope.generation) return;
      requested = scope.identities;
      if (!await refresh(requested)) return;
      // A fresh request for an old identity returns current data without
      // refreshing under the new login. Update the chip before asking again.
      if (scope.identities.claude && scope.identities.claude !== requested.claude) {
        requested = scope.identities;
        await refresh(requested);
      }
    } catch (e) {
      while (scope.checking) await scope.checking;
      if (generation !== scope.generation) return;
      if (!sameIdentities(requested, scope.identities)) {
        scope.again = true;
        return;
      }
      setState((previous) => previous.scope === scope ? { ...previous, error: message(e) } : previous);
    }
  }, [client, scope, update]);

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
    scope.generation++;
    started.current = null;
  }, [scope]);

  useEffect(() => {
    if (!client) return;
    const wake = () => {
      if (document.hidden || scope.checking) return;
      const generation = scope.generation;
      setState((previous) => previous.scope === scope ? { ...previous, checking: true } : previous);
      // Identity is a cheap read that can finish independently of the CLI.
      scope.checking = client.usage(false).then(
        (current) => update(generation, identitiesOf(current)),
        (error: unknown) => update(generation, identitiesOf(), undefined, message(error)),
      ).finally(() => { scope.checking = null; });
      if (!scope.loading) void reload();
    };
    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      window.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [client, reload, scope, update]);

  useEffect(() => {
    if (!client) return;
    const timer = setInterval(() => void reload(), pollMs);
    return () => clearInterval(timer);
  }, [client, reload, pollMs]);

  const feed = useMemo<UsageFeed>(() => ({
    usage: checking ? filterUsage(usage, (provider) => !isScoped(provider)) : usage,
    saved: checking ? [] : saved, checking, error, reload,
  }), [usage, saved, checking, error, reload]);
  return <UsageContext.Provider value={feed}>{children}</UsageContext.Provider>;
}
