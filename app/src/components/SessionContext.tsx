/* brindle's layer for the selected session, in the sidebar above the tree
   (design/options-v2/M3b-multi .ctx, as design/options-v3/C2b-inbox-only
   keeps it): the goal's title with the session's id, repo and mode under
   it; the goal with its milestones, progress and checks (GoalPanel
   compact: the check button, autopilot on / off, none of that for a paused
   session), whose one lifecycle carries the PR step: at Ready for PR its
   Review & open PR opens the session's sheet, at PR open it links to the
   PR, and at Done its Close session closes the session and moves on. There
   is no separate PR entry, so a Done goal never sits beside a
   PR banner. Nothing here about what waits
   on the person either: that is the Inbox's (the entry at the sidebar's
   top, the page in the main area). */

import { useContext, type CSSProperties } from "react";
import type { DaemonClient } from "../api/client";
import { DaemonContext } from "../api/context";
import type { OpenUrl } from "../api/open";
import type { Route, SessionRef } from "../routes";
import { useGoal } from "../screens/Session/GoalPanel";
import { GoalPanel } from "../screens/Session/GoalPanel";
import { agentIds, sessionTree } from "../screens/Session/tree";
import { isPaused, liveWorkers, sessionAge } from "../store/overview";
import { useCloseSession, useSessions } from "../store/sessions";
import { useAppState, useStore } from "../store/useStore";
import { useMemo, useRef, useState } from "react";

export interface SessionContextProps {
  /* The session whose context shows; null when none is selected. */
  session: SessionRef | null;
  route: Route;
  navigate(route: Route): void;
  /* Fetch the snapshot and inbox again (Sync.refresh). */
  refresh: () => Promise<void>;
  /* Opens the session's PR page in the system browser. */
  openUrl?: OpenUrl;
  style?: CSSProperties;
}

export function SessionContext({ session, route, navigate, openUrl, style }: SessionContextProps) {
  // No engine (a plain browser tab), or nothing selected: a quiet pane, and nothing asked for.
  const client = useContext(DaemonContext);
  if (!session || !client) {
    return (
      <section className="flex min-h-0 flex-col overflow-auto px-s1 text-xs text-ink-3" aria-label="Session context" data-testid="session-context" style={style}>
        <p className="py-1">{client ? "Pick a session below to see its goal and where it stands." : "No engine to show a session from."}</p>
      </section>
    );
  }
  return <Context key={session.root} client={client} session={session} route={route} navigate={navigate} openUrl={openUrl} style={style} />;
}

function Context({ client, session, route, navigate, openUrl, style }: Pick<SessionContextProps, "route" | "navigate" | "openUrl" | "style"> & { client: DaemonClient; session: SessionRef }) {
  const store = useStore();
  const feed = useSessions();
  const workspaces = useAppState((s) => s.workspaces);
  const alive = useAppState((s) => s.alive);
  const { root, repo } = session;
  const row = feed.find(root);
  const tree = useMemo(() => sessionTree(workspaces, root, feed.hidden), [workspaces, root, feed.hidden]);
  const members = useMemo(() => agentIds(tree), [tree]);
  const agents = useMemo(() => workspaces.flatMap((ws) => ws.agents ?? []).filter((a) => members.has(a.id)), [workspaces, members]);
  const goal = useGoal(client, repo, root, store);
  const paused = row ? isPaused(row) : false;
  const repoName = feed.repos?.find((r) => r.repo_root === repo)?.name ?? repo;
  const closeSession = useCloseSession(navigate);
  const [renaming, setRenaming] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  const saving = useRef(false);
  // A refused rename keeps the editor and what was typed, and says why.
  const commit = (name: string) => {
    if (saving.current) return;
    if (name.trim() === (row?.name ?? "")) {
      setRenaming(false);
      return;
    }
    saving.current = true;
    feed
      .rename(root, name)
      .then(
        () => {
          setRenaming(false);
          setRenameError(null);
        },
        (err: unknown) => setRenameError(`Couldn't rename: ${err instanceof Error ? err.message : String(err)}`),
      )
      .finally(() => {
        saving.current = false;
      });
  };

  const here = route.screen === "session" && route.root === root;
  return (
    <section className="flex min-h-0 flex-col gap-[10px] overflow-auto" aria-label="Session context" data-testid="session-context" style={style}>
      <div className="px-s1">
        {renaming ? (
          <input
            autoFocus
            aria-label="Session name"
            className="w-full rounded border border-line bg-surface px-1 text-[12.5px] font-semibold text-ink"
            defaultValue={row?.name ?? ""}
            placeholder={row?.goal || goal?.goal || "Name this session"}
            onFocus={(e) => e.currentTarget.select()}
            onKeyDown={(e) => {
              if (e.key === "Enter") commit(e.currentTarget.value);
              if (e.key === "Escape") {
                setRenaming(false);
                setRenameError(null);
              }
            }}
            onBlur={(e) => commit(e.currentTarget.value)}
          />
        ) : (
          <div className="flex items-start gap-1">
            <button type="button" className={`block min-w-0 flex-1 text-left text-[12.5px] font-semibold leading-[1.35] ${here ? "text-ink" : "text-ink/90 hover:text-ink"}`} title={here ? undefined : "Open the session"} onClick={() => navigate({ screen: "session", root, repo, agent: root })}>
              {row?.name || row?.goal || goal?.goal || <span className="italic text-ink-3">No goal set</span>}
            </button>
            <button type="button" className="text-[11px] text-ink-3 hover:text-ink" aria-label="Rename session" title="Rename session" onClick={() => setRenaming(true)}>
              ✎
            </button>
          </div>
        )}
        {renameError && (
          <p role="alert" className="text-[11px] text-sap">
            {renameError}
          </p>
        )}
        <div className="truncate text-[11px] text-ink-3">
          <span className="font-mono text-[11px]">{root}</span> · {repoName} · {row ? (paused ? sessionAge(row) : `${row.mode}${goal?.enabled ? " · autopilot" : ""} · ${liveWorkers(agents, alive)} working`) : "…"}
        </div>
      </div>
      <GoalPanel
        client={client}
        repo={repo}
        root={root}
        store={store}
        paused={paused}
        compact
        onReview={() => navigate({ screen: "session", root, repo, agent: root, sheet: { kind: "review" } })}
        openUrl={openUrl}
        onCloseSession={closeSession && (() => closeSession(root))}
      />
    </section>
  );
}
