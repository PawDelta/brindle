/* A session's main area has Terminal and Changes tabs. Terminal keeps
   the selected agent's ProviderSurface; Changes shows the whole session
   branch against its PR base, even before readiness. Both remain mounted
   after opening so switching tabs keeps the pane and unsaved drafts.
   The bar always names the session branch, even when a worker is selected.
   Continue in the bar and on the state card share useContinue.
   The goal and PR lifecycle stay in the sidebar; Review & open PR opens
   its sheet from there and uses the same editor as Changes.

   The tree is the store's snapshot (tree.ts, with the sessions feed's
   lookup of closed agents' parents). Which agent is shown and which sheet
   is open are the route's (App.tsx), so the sidebar and this view agree;
   alone (tests) the view keeps that itself. */

import { useContext, useEffect, useMemo, useRef, useState } from "react";
import type { DaemonClient } from "../../api/client";
import type { OpenUrl } from "../../api/open";
import { button, mono, primary } from "../../components/ui";
import type { SheetRef } from "../../routes";
import { CLI_INK, agentName, cliStatus, isPaused, liveWorkers, needsYou, providerName } from "../../store/overview";
import { SessionsContext } from "../../store/sessions";
import type { AppStore } from "../../store/store";
import { useAppState } from "../../store/useStore";
import { AgentMenu, SessionMenu, closeSessionLabel, type CloseSessionItem } from "./AgentMenu";
import { failure, type Notice } from "./notice";
import { ProviderSurface } from "./ProviderSurface";
import { usePrReadiness, useSessionDiff } from "./review";
import { ReviewSheet } from "./ReviewSheet";
import { SessionDiffEditor } from "./SessionDiffEditor";
import { agentIds, sessionTree, sessionWork, since, type AgentNode } from "./tree";
import { useContinue, type Recovery } from "./useContinue";

export interface SessionScreenProps {
  client: DaemonClient;
  /* The session's root agent (its supervisor), and the repo it belongs to. */
  root: string;
  repo: string;
  /* Fetch the snapshot and inbox again (Sync.refresh). */
  refresh: () => Promise<void>;
  store: AppStore;
  /* The agent shown and the sheet over it; the root's session, bare, when omitted. */
  agent?: string;
  sheet?: SheetRef | null;
  /* Called when the person picks another agent or sheet here; the app puts it in the route. */
  onSelect?(agent: string, sheet: SheetRef | null): void;
  /* Opens the PR's page in the system browser; the shell's by default. */
  openUrl?: OpenUrl;
  /* Opens another session: the one a handover starts. */
  onOpen?(root: string, repo: string): void;
  /* Closes this whole session and moves the view on (the ⋯'s Close session). */
  onCloseSession?(root: string): void;
}

export function findNode(node: AgentNode | null, id: string): AgentNode | null {
  if (!node) return null;
  if (node.agent.id === id) return node;
  for (const child of node.children) {
    const found = findNode(child, id);
    if (found) return found;
  }
  return null;
}

export function SessionScreen({ client, root, repo, refresh, store, agent, sheet, onSelect, openUrl, onOpen, onCloseSession }: SessionScreenProps) {
  const workspaces = useAppState((s) => s.workspaces);
  const inbox = useAppState((s) => s.inbox);
  const alive = useAppState((s) => s.alive);
  const synced = useAppState((s) => s.synced);
  const feed = useContext(SessionsContext);
  const hidden = feed?.hidden ?? {};
  const session = feed?.find(root) ?? null;
  const tree = useMemo(() => sessionTree(workspaces, root, hidden), [workspaces, root, hidden]);

  // Alone, the view keeps the selection itself; under the app the route does.
  const [own, setOwn] = useState<{ agent: string; sheet: SheetRef | null }>({ agent: root, sheet: null });
  const picked = agent ?? own.agent;
  const shownSheet = sheet === undefined ? own.sheet : sheet;
  const select = (next: string, nextSheet: SheetRef | null) => {
    setOwn({ agent: next, sheet: nextSheet });
    onSelect?.(next, nextSheet);
  };
  // A closed or killed agent leaves the tree: fall back to the root.
  const selected = agentIds(tree).has(picked) ? picked : root;
  const node = useMemo(() => findNode(tree, selected), [tree, selected]);
  const members = useMemo(() => agentIds(tree), [tree]);
  const waiting = useMemo(() => needsYou(inbox, members), [inbox, members]);
  // The ⋯'s Close session, with what it still stops: workers running, and every workspace of the
  // session's (the supervisor's own too) with commits ahead of its base or uncommitted changes. Read
  // from the snapshot without the tree: a paused session's root is in none, its workers and work may be.
  const rootWorkspace = session?.workspace_id ?? tree?.workspace.id ?? null;
  const closeSession = useMemo<CloseSessionItem | undefined>(() => {
    if (!onCloseSession) return undefined;
    const work = sessionWork(workspaces, root, rootWorkspace, hidden);
    const workers = work.agents.filter((a) => a.id !== root);
    const unmerged = work.workspaces.filter((ws) => (ws.ahead ?? 0) > 0 || (ws.dirty ?? 0) > 0);
    return { label: closeSessionLabel(liveWorkers(workers, alive), unmerged.length), close: () => onCloseSession(root) };
  }, [onCloseSession, workspaces, root, rootWorkspace, hidden, alive]);

  const paused = session ? isPaused(session) : tree === null && synced;
  // A session seen live here that pauses under the view (a fresh start in its checkout, a pause from
  // elsewhere) stays on screen and says so, with Reopen; one opened already paused doesn't.
  const live = session ? !isPaused(session) : tree !== null;
  const [seenLive, setSeenLive] = useState<string | null>(null);
  useEffect(() => {
    if (live) setSeenLive(root);
  }, [live, root]);
  const pausedUnder = paused && seenLive === root;

  const closeSheet = () => select(selected, null);
  // A lapsed sign-in of the shown agent: its card says so; the sign-in itself is the Inbox's.
  const credentials = node ? (waiting.find((i) => i.agent_id === node.agent.id && i.kind === "credentials") ?? null) : null;
  const reload = useMemo(
    () => async () => {
      await Promise.all([feed?.reload(), refresh()]).catch(() => undefined);
    },
    [feed, refresh],
  );
  const recovery = useContinue(client, root, reload);
  // The engine's word on a PR: the Review & open PR entry exists only once it says ready.
  const readiness = usePrReadiness(client, root, store);
  const changes = useSessionDiff(client, root, store);
  const [view, setView] = useState<{ root: string; tab: "terminal" | "changes" }>({ root, tab: "terminal" });
  const tab = view.root === root ? view.tab : "terminal";
  const [visited, setVisited] = useState<string | null>(null);
  const branch = changes.diff?.branch || workspaces.find((ws) => ws.id === rootWorkspace)?.branch || "…";

  const host = useRef<HTMLElement>(null);
  useEffect(() => {
    const section = host.current;
    if (!section || shownSheet || tab !== "terminal") return;
    // xterm loads asynchronously and adds its textarea after the pane mounts.
    // Focus once per navigation, never on later terminal output or store updates.
    const focus = () => {
      const input = section.querySelector<HTMLTextAreaElement>(".xterm-helper-textarea");
      if (!input) return false;
      if (!document.querySelector('[role="dialog"], [role="alertdialog"], dialog[open]')) input.focus({ preventScroll: true });
      return true;
    };
    if (focus()) return;
    const observer = new MutationObserver(() => {
      if (focus()) observer.disconnect();
    });
    observer.observe(section, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [root, repo, selected, shownSheet, tab]);

  return (
    <section ref={host} className="relative flex min-h-0 flex-1 flex-col" aria-label="Session">
      <SessionBar
        client={client}
        root={root}
        repo={repo}
        node={node}
        alive={node ? alive[node.agent.id] : undefined}
        sessionStatus={session?.status ?? null}
        paused={paused}
        reload={reload}
        refresh={refresh}
        recovery={recovery}
        onAgent={(id) => select(id, null)}
        onOpen={onOpen}
        closeSession={closeSession}
        branch={branch}
        tab={tab}
        onTab={(tab) => {
          setView({ root, tab });
          if (tab === "changes") setVisited(root);
        }}
        added={changes.files.reduce((n, f) => n + f.added, 0)}
        removed={changes.files.reduce((n, f) => n + f.removed, 0)}
      />
      {pausedUnder && (
        <div role="status" className="flex items-center gap-s2 border-b border-line px-s2 py-1 text-sm text-ink-2">
          <span>This session was paused</span>
          <button
            type="button"
            className={button}
            disabled={recovery.busy}
            onClick={() => {
              select(root, null);
              void recovery.resume();
            }}
          >
            Reopen
          </button>
        </div>
      )}
      <div role="tabpanel" id="session-terminal" aria-labelledby="session-tab-terminal" hidden={tab !== "terminal"} className={tab === "terminal" ? "flex min-h-0 flex-1 p-s2" : "hidden"}>
        {!tree && !paused && !synced ? (
          <p className="flex-1 text-ink-3">Loading…</p>
        ) : !tree && !paused && !session ? (
          <p className="flex-1 text-ink-3">No session {root}.</p>
        ) : (
          <div className="flex min-w-0 flex-1 flex-col" data-testid="terminal-surface">
            <ProviderSurface
              // A sidebar Resume also needs a fresh pane when this session was already selected.
              key={`${selected}:${paused}`}
              repo={repo}
              agent={node?.agent ?? null}
              alive={node ? alive[node.agent.id] : undefined}
              sessionStatus={session?.status ?? null}
              credentials={credentials}
              recovery={recovery}
            />
          </div>
        )}
      </div>
      <div role="tabpanel" id="session-changes" aria-labelledby="session-tab-changes" hidden={tab !== "changes"} className={tab === "changes" ? "flex min-h-0 min-w-0 flex-1 flex-col p-s2" : "hidden"}>
        {visited === root && <SessionDiffEditor key={root} client={client} root={root} state={changes} />}
      </div>
      {/* The one thing that ever lies over a session, opened on purpose from the goal panel's Ready for PR step.
          Open, it stays through readiness being read again (an edit here, a sweep): it says so quietly instead. */}
      {shownSheet?.kind === "review" && <ReviewSheet key={root} client={client} root={root} repo={repo} readiness={readiness} state={changes} onClose={closeSheet} openUrl={openUrl} />}
    </section>
  );
}

/* Tabs, session branch, and the selected agent's status and controls. */
function SessionBar({ client, root, repo, node, alive, sessionStatus, paused, reload, refresh, recovery, onAgent, onOpen, closeSession, branch, tab, onTab, added, removed }: {
  client: DaemonClient;
  root: string;
  repo: string;
  node: AgentNode | null;
  alive: boolean | undefined;
  sessionStatus: string | null;
  paused: boolean;
  reload: () => Promise<void>;
  refresh: () => Promise<void>;
  recovery: Recovery;
  onAgent(id: string): void;
  onOpen?(root: string, repo: string): void;
  closeSession?: CloseSessionItem;
  branch: string;
  tab: "terminal" | "changes";
  onTab(tab: "terminal" | "changes"): void;
  added: number;
  removed: number;
}) {
  const workspaces = useAppState((s) => s.workspaces);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  // The last agent shown: the ⋯ menu stays mounted on it when the tree empties under a running action.
  const last = useRef<AgentNode | null>(null);
  if (node) last.current = node;
  const menuNode = node ?? last.current;
  const pause = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const got = await client.pauseSession(root);
      setNotice({ text: got.paused.length > 0 ? `Paused ${got.paused.join(", ")}.` : "Nothing was running.", failed: false });
    } catch (e) {
      setNotice(failure(e));
    } finally {
      setBusy(false);
      await reload();
    }
  };
  const status = node ? cliStatus(node.agent, node.workspace, alive) : { glyph: "‖", label: sessionStatus ?? "paused", style: "dim" as const, needs: false };
  const named = useContext(SessionsContext)?.find(root)?.name;
  const who = node ? (node.agent.id === root ? (named || "supervisor") : agentName(workspaces, node.agent.id)) : named || "supervisor";
  const provider = node ? providerName(node.agent.provider) : "Session";
  const ago = node ? since(node.agent.status_since) : null;
  // The bar's own notice is the pause's; a continue's shows on the surface's card, where it was asked for.
  return (
    <header className="flex flex-wrap items-center gap-x-s2 gap-y-1 border-b border-line bg-surface px-s2 py-1" data-testid="session-bar">
      <div role="tablist" aria-label="Session views" data-testid="session-tabs" className="flex shrink-0 items-center self-stretch gap-s1 pr-s2">
        {(["terminal", "changes"] as const).map((item) => (
          <button key={item} id={`session-tab-${item}`} role="tab" type="button" aria-selected={tab === item} aria-controls={`session-${item}`} tabIndex={tab === item ? 0 : -1}
            className={`border-b-2 px-s2 py-2 text-sm ${tab === item ? "border-branch text-ink" : "border-transparent text-ink-3 hover:text-ink"}`}
            onClick={() => onTab(item)}
            onKeyDown={(e) => {
              if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
              e.preventDefault();
              const next = e.key === "Home" ? "terminal" : e.key === "End" ? "changes" : item === "terminal" ? "changes" : "terminal";
              onTab(next);
              document.getElementById(`session-tab-${next}`)?.focus();
            }}>
            {item === "terminal" ? "Terminal" : <>Changes <span className={`${mono} ml-1 text-xs`}><span className="text-pass">+{added}</span> <span className="text-sap">−{removed}</span></span></>}
          </button>
        ))}
      </div>
      <span data-testid="session-branch" title={branch} className={`${mono} min-w-0 max-w-64 truncate rounded border border-line bg-surface-2 px-s1 py-0.5 text-xs text-ink-2`}>{branch}</span>
      <div className="flex min-w-0 items-center gap-s1">
        <span className="truncate text-sm font-semibold" data-testid="surface-title">
          {provider} · {who}
        </span>
        {node && (
          <span className={`${mono} hidden text-xs text-ink-3 sm:inline`} data-testid="agent-id" title={node.workspace.branch}>
            {node.agent.id}
          </span>
        )}
        <span className={`flex items-center gap-1 text-xs ${status.style === "alert" ? "text-sap" : "text-ink-2"}`} data-testid="session-status">
          <span className={`font-mono ${CLI_INK[status.style]}`} aria-hidden>
            {status.glyph}
          </span>
          {status.label}
          {ago && status.style !== "dim" && <span className="text-ink-3">· {ago}</span>}
        </span>
      </div>
      <span className="flex-1" />
      {paused ? (
        <button type="button" className={primary} disabled={recovery.busy} onClick={() => void recovery.resume()}>
          Continue
        </button>
      ) : (
        <button type="button" className={button} disabled={busy} onClick={() => void pause()}>
          Pause
        </button>
      )}
      {/* Not keyed by the agent, and kept when no agent is left to show (a transfer pauses the scratch
          session's root, which leaves the snapshot): a card it opened stays while its job runs and says how it ended. */}
      {menuNode && (
        <AgentMenu client={client} agent={menuNode.agent} workspace={menuNode.workspace} gone={!node} root={root} repo={repo} refresh={refresh} onAgent={onAgent} onSession={onOpen} closeSession={closeSession} />
      )}
      {/* No agent shown (a paused session's root is in no snapshot): the session's own ⋯, so it can still be closed. */}
      {!node && closeSession && <SessionMenu closeSession={closeSession} />}
      {notice && <p className={`w-full text-xs ${notice.failed ? "text-sap" : "text-ink-2"}`}>{notice.text}</p>}
    </header>
  );
}
