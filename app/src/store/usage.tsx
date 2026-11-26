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

const weeklyKey = (accountKey: string | null, claudeScope: string | null) =>
  accountKey && claudeScope ? `brindle.usage.claude.weekly.${JSON.stringify([accountKey, claudeScope])}` : null;

function readWeekly(key: string | null): WeeklyReading | undefined {
  if (!key) return;
  try {
    const saved = JSON.parse(localStorage.getItem(key) ?? "null");
    if (saved?.provider === "claude" && Number.isFinite(saved.used)
      && (saved.resetsAt === null || Number.isFinite(saved.resetsAt))) return saved;
  } catch {
    // The feed still works when local storage is unavailable.
  }
}

export interface UsageFeed {
  /* null until the first load answers. */
  usage: Usage | null;
  savedClaude?: WeeklyReading;
  checkingClaude: boolean;
  /* The last failed load's message; the last usage stays, once loaded. */
  error: string | null;
  reload(): Promise<void>;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

const NONE: UsageFeed = { usage: null, checkingClaude: false, error: null, reload: () => Promise.resolve() };

export const UsageContext = createContext<UsageFeed | null>(null);

/* Outside a <UsageProvider> there is nothing to read: no usage, nothing near. */
export function useUsage(): UsageFeed {
  return useContext(UsageContext) ?? NONE;
}

interface UsageScope {
  loading: Promise<void> | null;
  again: boolean;
  verifyClaude: boolean;
  generation: number;
  claudeIdentity: string | null;
}

type ScopedFeed = Omit<UsageFeed, "reload"> & { scope: UsageScope; claudeScope: string | null };

const scopedFeed = (scope: UsageScope, accountKey: string | null, claudeScope: string | null): ScopedFeed => ({
  scope, claudeScope, usage: null, savedClaude: readWeekly(weeklyKey(accountKey, claudeScope)), checkingClaude: false, error: null,
});

const withoutClaude = (usage: Usage | null): Usage | null => usage && {
  ...usage,
  providers: usage.providers.filter((p) => p.provider !== "claude"),
  near: usage.near.filter((p) => p.provider !== "claude"),
};

/* `client` null: no engine to ask (a plain browser tab); the feed stays empty. */
export function UsageProvider({ client, children, pollMs = USAGE_POLL_MS }: { client: DaemonClient | null; children: ReactNode; pollMs?: number }) {
  const subject = useAppState((s) => s.account?.signed_in ? s.account.subject : null);
  const initialClaudeScope = useAppState((s) => s.account?.signed_in ? s.account.claude_usage_scope ?? null : null);
  const accountKey = client && subject ? JSON.stringify([client.baseUrl, subject]) : null;
  const scope = useMemo<UsageScope>(() => ({
    loading: null, again: false, verifyClaude: false, generation: 0, claudeIdentity: initialClaudeScope,
  }), [client, accountKey, initialClaudeScope]);
  const [state, setState] = useState(() => scopedFeed(scope, accountKey, initialClaudeScope));
  // A new account gets its own feed without remounting the active screen.
  if (state.scope !== scope) setState(scopedFeed(scope, accountKey, initialClaudeScope));
  const { usage, savedClaude, checkingClaude, claudeScope, error } = state;
  const cacheKey = weeklyKey(accountKey, claudeScope);
  const started = useRef<UsageScope | null>(null);

  useEffect(() => {
    if (!cacheKey || !savedClaude) return;
    try {
      localStorage.setItem(cacheKey, JSON.stringify(savedClaude));
    } catch {
      // This launch can still display the reading from memory.
    }
  }, [cacheKey, savedClaude]);

  const load = useCallback(async () => {
    if (!client) return;
    const generation = scope.generation;
    const apply = (next: Usage) => {
      if (generation !== scope.generation) return false;
      const identity = next.claude_usage_scope ?? null;
      scope.claudeIdentity = identity;
      const provider = next.providers.find((p) => p.provider === "claude");
      const reading = identity && provider ? weeklyReading(provider) : undefined;
      const saved = readWeekly(weeklyKey(accountKey, identity));
      setState((previous) => {
        if (previous.scope !== scope || generation !== scope.generation) return previous;
        const savedClaude = reading ?? (identity === previous.claudeScope ? previous.savedClaude : saved);
        return { ...previous, usage: next, claudeScope: identity, savedClaude, checkingClaude: false, error: null };
      });
      return true;
    };
    try {
      if (scope.verifyClaude) {
        const current = await client.usage(false);
        if (generation !== scope.generation) return;
        scope.verifyClaude = false;
        apply(current);
      }
      const expectedIdentity = scope.claudeIdentity;
      const next = await client.usage(true, expectedIdentity);
      if (!apply(next)) return;
      // A fresh request for an old identity returns current data without
      // refreshing under the new login. Update the chip before asking again.
      if (scope.claudeIdentity && scope.claudeIdentity !== expectedIdentity) {
        apply(await client.usage(true, scope.claudeIdentity));
      }
    } catch (e) {
      if (generation !== scope.generation) return;
      if (scope.verifyClaude) scope.claudeIdentity = null;
      scope.verifyClaude = false;
      setState((previous) => previous.scope === scope && generation === scope.generation ? {
        ...previous,
        ...(previous.checkingClaude && { claudeScope: null, savedClaude: undefined, checkingClaude: false }),
        error: message(e),
      } : previous);
    }
  }, [accountKey, client, scope]);

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
      if (document.hidden || scope.verifyClaude) return;
      // A login may have changed while the window was away. Confirm the
      // subscription with a fast read before starting the slow CLI refresh.
      scope.verifyClaude = true;
      scope.generation++;
      setState((previous) => previous.scope === scope ? {
        ...previous, usage: withoutClaude(previous.usage), checkingClaude: true,
      } : previous);
      void reload();
    };
    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      window.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [client, reload, scope]);

  useEffect(() => {
    if (!client) return;
    const timer = setInterval(() => void reload(), pollMs);
    return () => clearInterval(timer);
  }, [client, reload, pollMs]);

  const feed = useMemo<UsageFeed>(() => ({
    usage, savedClaude: checkingClaude ? undefined : savedClaude, checkingClaude, error, reload,
  }), [usage, savedClaude, checkingClaude, error, reload]);
  return <UsageContext.Provider value={feed}>{children}</UsageContext.Provider>;
}
