/* The sessions the daemon knows, for the whole app: every repo (GET /repos)
   and each repo's live, paused and failed sessions (GET /sessions), fetched
   whole and refetched after every bus event, on (re)connect, after a
   resnapshot, when a listed root's liveness changes, and on demand after an
   action's ack (a reply is an acknowledgement, not the new state). A live
   session's status, branch, workers and needs-you count come from the
   store's snapshot, which the event stream keeps current; this feed is only
   the list of sessions and which repo each belongs to. */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { DaemonError, type DaemonClient, type Repo } from "../api/client";
import type { SessionDTO } from "../api/schema";
import type { Route } from "../routes";
import { unknownAncestors, type Hidden } from "../screens/Session/tree";
import { isPaused } from "./overview";
import { useAppState } from "./useStore";

export interface SessionsFeed {
  /* null until the first load answers. */
  repos: Repo[] | null;
  /* Repo root → its sessions, live ones first (routes_read.list_sessions). */
  sessions: Record<string, SessionDTO[]>;
  /* The last failed load's message; the last lists stay, once loaded. */
  error: string | null;
  reload(): Promise<void>;
  /* The repo a session root belongs to, as the list gave it. */
  repoOf(root: string): string | null;
  /* The session row for a root, from any repo. */
  find(root: string): SessionDTO | null;
  /* Agents the snapshot leaves out (closed) whose running children stay in
     it: each one's own parent, so they hang under the session still
     (screens/Session/tree.ts sessionTree). */
  hidden: Hidden;
  /* Closes a whole session (POST /sessions/{root}/close): it leaves the
     lists at once, before the engine answers; a refusal brings it back
     and says so in closeError. */
  close(root: string): Promise<void>;
  /* The last close the engine refused, in words; null once another goes. */
  closeError: string | null;
  /* Where the view goes once `root` is closed: the next live session in
     the sidebar's order (else the one before it), else the Inbox. */
  after(root: string): Route;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export const SessionsContext = createContext<SessionsFeed | null>(null);

export function useSessions(): SessionsFeed {
  const feed = useContext(SessionsContext);
  if (!feed) throw new Error("useSessions needs a <SessionsProvider>");
  return feed;
}

/* Closing a session, one click and nothing asked: the view moves on (the
   next session, else the Inbox) and the session leaves the sidebar at once.
   Without a feed (a screen alone in a test) there is nothing to close with. */
export function useCloseSession(navigate: ((route: Route) => void) | undefined): ((root: string) => void) | undefined {
  const feed = useContext(SessionsContext);
  const close = useCallback(
    (root: string) => {
      if (!feed || !navigate) return;
      navigate(feed.after(root));
      void feed.close(root);
    },
    [feed, navigate],
  );
  return feed && navigate ? close : undefined;
}

/* `client` null: no engine to ask (a plain browser tab); the feed stays empty. */
export function SessionsProvider({ client, children }: { client: DaemonClient | null; children: ReactNode }) {
  const [repos, setRepos] = useState<Repo[] | null>(null);
  const [sessions, setSessions] = useState<Record<string, SessionDTO[]>>({});
  const [error, setError] = useState<string | null>(null);
  // Only the newest load applies its result: an older one finishing late is dropped.
  const latest = useRef(0);
  // One load out at a time: the stream replays its backlog an event at a time
  // (at launch, after a reconnect), and a load per event, all at once, once
  // took every file descriptor the app's shell had (each request is a
  // connection to the daemon). Asked again while one is out, it runs once more after.
  const loading = useRef<Promise<void> | null>(null);
  const again = useRef(false);
  // Sessions being closed: out of the lists from the click until the engine's
  // own list, asked for after the close succeeded, has answered (a failed
  // refresh keeps the old lists, which still have them). A refused close
  // brings its session back.
  const [closing, setClosing] = useState<ReadonlySet<string>>(new Set());
  // Closed roots → the newest load started before the engine said so: only a later load settles them.
  const closedAt = useRef(new Map<string, number>());

  const load = useCallback(async () => {
    if (!client) return;
    const seq = ++latest.current;
    try {
      const found = await client.repos();
      const lists = await Promise.all(found.map((r) => client.sessions(r.repo_root)));
      if (seq !== latest.current) return;
      setRepos(found);
      setSessions(Object.fromEntries(found.map((r, i) => [r.repo_root, lists[i]])));
      setError(null);
      const settled = [...closedAt.current].filter(([, at]) => seq > at).map(([root]) => root);
      if (settled.length > 0) {
        for (const root of settled) closedAt.current.delete(root);
        setClosing((c) => new Set([...c].filter((root) => !settled.includes(root))));
      }
    } catch (e) {
      if (seq === latest.current) setError(message(e));
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

  // Sessions start, pause and resume outside the app too (the CLI, other
  // windows): refetch on every bus event, on (re)connect and after a resnapshot.
  const lastEventId = useAppState((s) => s.lastEventId);
  const connection = useAppState((s) => s.connection);
  const stale = useAppState((s) => s.stale);
  // A session's chat can also exit with no bus event, only a liveness change
  // (sessions.live drops a root whose process is gone): watch the listed roots'.
  const roots = Object.values(sessions).flatMap((list) => list.map((s) => s.id));
  const rootsAlive = useAppState((s) => roots.map((id) => `${id}:${s.alive[id] ?? "?"}`).join(","));
  useEffect(() => {
    void reload();
  }, [reload, lastEventId, connection, stale, rootsAlive]);

  // A closed agent leaves the snapshot but its running children stay: look up
  // its own parent so they hang under the session still. A lookup that fails
  // for any reason but not_found is left unresolved and tried again with the
  // next snapshot (a refresh, a reconnect, the next event).
  const workspaces = useAppState((s) => s.workspaces);
  const [hidden, setHidden] = useState<Hidden>({});
  useEffect(() => {
    if (!client) return;
    const missing = unknownAncestors(workspaces, hidden);
    if (missing.length === 0) return;
    let current = true;
    void Promise.all(
      missing.map((id) =>
        client.agent(id).then(
          (a): [string, string | null] => [id, a.parent_id ?? null],
          // Killed: no record left to follow.
          (e): [string, null] | null => (e instanceof DaemonError && e.code === "not_found" ? [id, null] : null),
        ),
      ),
    ).then((found) => {
      const known = found.filter((f) => f !== null);
      if (current && known.length > 0) setHidden((h) => ({ ...h, ...Object.fromEntries(known) }));
    });
    return () => {
      current = false;
    };
  }, [client, workspaces, hidden]);

  const [closeError, setCloseError] = useState<string | null>(null);
  const shown = useMemo(
    () => (closing.size === 0 ? sessions : Object.fromEntries(Object.entries(sessions).map(([repo, list]) => [repo, list.filter((s) => !closing.has(s.id))]))),
    [sessions, closing],
  );
  const close = useCallback(
    async (root: string) => {
      if (!client) return;
      setCloseError(null);
      setClosing((c) => new Set([...c, root]));
      try {
        await client.closeSession(root);
      } catch (e) {
        setClosing((c) => new Set([...c].filter((id) => id !== root)));
        setCloseError(`Couldn't close ${root}: ${message(e)}`);
        return;
      }
      // Closed: hidden until a list asked for from now on answers (load settles it).
      closedAt.current.set(root, latest.current);
      await reload();
    },
    [client, reload],
  );

  const feed = useMemo<SessionsFeed>(
    () => ({
      repos,
      sessions: shown,
      error,
      reload,
      repoOf: (root) => Object.entries(shown).find(([, list]) => list.some((s) => s.id === root))?.[0] ?? null,
      find: (root) => Object.values(shown).flat().find((s) => s.id === root) ?? null,
      hidden,
      close,
      closeError,
      after: (root) => {
        // The sidebar's order: repo by repo, each repo's live sessions.
        const live = (repos ?? []).flatMap((r) => (shown[r.repo_root] ?? []).filter((s) => !isPaused(s)).map((s) => ({ root: s.id, repo: r.repo_root })));
        const at = live.findIndex((s) => s.root === root);
        const rest = live.filter((s) => s.root !== root);
        const next = at < 0 ? rest[0] : (rest[at] ?? rest[at - 1]);
        return next ? { screen: "session", root: next.root, repo: next.repo, agent: next.root } : { screen: "inbox" };
      },
    }),
    [repos, shown, error, reload, hidden, close, closeError],
  );
  return <SessionsContext.Provider value={feed}>{children}</SessionsContext.Provider>;
}
