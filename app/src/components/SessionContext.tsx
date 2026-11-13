/* The selected session's short goal and progress, above its milestones
   and the Sessions tree (card B in calm.html). GoalPanel
   compact carries the PR step: at Ready for PR its
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
import type { AppStore } from "../store/store";
import { useGoal } from "../screens/Session/GoalPanel";
import { useSessionDiff } from "../screens/Session/review";
import { GoalPanel, SessionGoal } from "../screens/Session/GoalPanel";
import { isPaused } from "../store/overview";
import { useCloseSession, useSessions } from "../store/sessions";
import { useStore } from "../store/useStore";
import { useRef, useState } from "react";

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
  const { root, repo } = session;
  const row = feed.find(root);
  const goal = useGoal(client, repo, root, store);
  const paused = row ? isPaused(row) : false;
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
  const heading = row?.name || goal?.summary || row?.goal || goal?.goal;
  return (
    <section className="side-context flex min-h-0 min-w-0 flex-col overflow-hidden" aria-label="Session context" data-testid="session-context" style={style}>
      <GoalPanel
        client={client}
        repo={repo}
        root={root}
        store={store}
        paused={paused}
        compact
        loadingTitle={row?.name || row?.goal}
        request={row?.request}
        title={(onOpen) => (
          <div className="flex min-w-0 flex-1 items-center gap-1">
            {renaming ? (
              <input
                autoFocus
                aria-label="Session name"
                className="min-w-0 w-full rounded border border-line bg-surface px-1 text-[12.5px] font-semibold text-ink"
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
              <>
                <SessionGoal key={`${repo}:${root}:${row?.request}`} text={row?.request || goal?.goal || row?.goal} title={row?.name || goal?.goal || row?.goal || undefined} className={`w-full ${row?.name ? "block truncate" : "line-clamp-2 break-words"} text-left text-[13px] font-medium ${here ? "text-ink" : "text-ink/90 hover:text-ink"}`} onOpen={onOpen}>
                  {heading || <span className="italic text-ink-3">No goal set</span>}
                </SessionGoal>
                <button type="button" className="shrink-0 text-[11px] text-ink-3 hover:text-ink" aria-label="Rename session" title="Rename session" onClick={() => setRenaming(true)}>
                  ✎
                </button>
              </>
            )}
          </div>
        )}
        onReview={() => navigate({ screen: "session", root, repo, agent: root, sheet: { kind: "review" } })}
        openUrl={openUrl}
        onCloseSession={closeSession && (() => closeSession(root))}
        onOpenDocs={() => navigate({ screen: "knowledge", view: "docs", session: root, from: { root, repo } })}
      />
      {renameError && (
        <p role="alert" className="shrink-0 text-[11px] text-sap">
          {renameError}
        </p>
      )}
      {sidebarChanges.shown && (
        <ChangesRow
          client={client}
          root={root}
          store={store}
          open={here && route.screen === "session" && route.sheet?.kind === "changes"}
          onOpen={(open) => navigate({ screen: "session", root, repo, agent: here && route.screen === "session" ? route.agent : undefined, sheet: open ? { kind: "changes" } : undefined })}
        />
      )}
    </section>
  );
}

/* Hidden for now: the row, its sheet and its tests stay, below the milestones;
   set `shown` to bring it back. */
export const sidebarChanges = { shown: false };

/* The session's changes against its PR base, in one line: the files and lines,
   coloured. Clicking it puts the diff in the chat's place, and again (or ← Chat)
   takes it away. */
function ChangesRow({ client, root, store, open, onOpen }: { client: DaemonClient; root: string; store: AppStore; open: boolean; onOpen(open: boolean): void }) {
  const { diff, files } = useSessionDiff(client, root, store);
  const added = files.reduce((n, f) => n + f.added, 0);
  const removed = files.reduce((n, f) => n + f.removed, 0);
  return (
    <div className="mx-s1 flex items-center gap-1">
      <button
        type="button"
        aria-pressed={open}
        className="flex min-w-0 flex-1 items-baseline gap-s1 rounded-md px-s1 py-1 text-left text-[13px] hover:bg-surface-2 aria-pressed:bg-surface-2"
        onClick={() => onOpen(!open)}
        data-testid="changes-row"
      >
        <span className="font-medium text-ink">Changes</span>
        {!diff ? (
          <span className="text-xs text-ink-3">…</span>
        ) : files.length === 0 ? (
          <span className="text-xs text-ink-3">none yet</span>
        ) : (
          <span className="truncate font-mono text-xs" data-testid="changes-count">
            <span className="text-ink-3">
              {files.length} {files.length === 1 ? "file" : "files"}
            </span>{" "}
            <span className="text-pass">+{added}</span> <span className="text-sap">−{removed}</span>
          </span>
        )}
        <span className="flex-1" />
        <span className="text-ink-3" aria-hidden>
          {open ? "‹" : "›"}
        </span>
      </button>
    </div>
  );
}
