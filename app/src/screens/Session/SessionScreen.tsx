/* A session's main area is the selected agent's chat (ProviderSurface), or
   its changes in the chat's place when the sidebar's Changes row asks
   (sheet "changes").
   Continue in the bar and on the state card share useContinue.
   The goal and PR lifecycle stay in the sidebar; Review & open PR opens
   its sheet from there and uses the same editor as Changes.

   The tree is the store's snapshot (tree.ts, with the sessions feed's
   lookup of closed agents' parents). Which agent is shown and which sheet
   is open are the route's (App.tsx), so the sidebar and this view agree;
   alone (tests) the view keeps that itself. */

import { useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { DaemonClient } from "../../api/client";
import { logDebug } from "../../api/debug";
import type { OpenUrl } from "../../api/open";
import type { InboxItemDTO, QueuedChatMessageDTO } from "../../api/schema";
import { seenKey } from "../Inbox/items";
import { LoadingButton } from "../../components/LoadingButton";
import { button, main, mono } from "../../components/ui";
import type { SheetRef } from "../../routes";
import { CLI_INK, agentName, cliStatus, isPaused, liveWorkers, needsYou, providerName } from "../../store/overview";
import { OpenSessionContext, PanesReadyContext, panesReadyFor } from "../../store/panes";
import { SessionsContext } from "../../store/sessions";
import type { AppStore } from "../../store/store";
import { useAppState } from "../../store/useStore";
import { AgentMenu, SessionMenu, closeSessionLabel, type CloseSessionItem } from "./AgentMenu";
import { ChatBox, type ChatBoxTyping } from "./Chat/ChatBox";
import { ChatDrop } from "./Chat/ChatDrop";
import { useChatSupport } from "./Chat/support";
import { TerminalFrame, chatTheme } from "./Chat/ChatPane";
import { useAppTheme } from "../../components/useAppTheme";
import { failure, type Notice } from "./notice";
import { ProviderSurface } from "./ProviderSurface";
import { usePrReadiness, useSessionDiff } from "./review";
import { ReviewSheet } from "./ReviewSheet";
import { SessionDiffEditor } from "./SessionDiffEditor";
import { agentIds, sessionTree, sessionWork, since, type AgentNode } from "./tree";
import { useContinue, type Recovery } from "./useContinue";
import { VisualLinkContext } from "./Visuals/link";
import { VisualReader } from "./Visuals/VisualReader";
import { VisualsButton } from "./Visuals/VisualsButton";
import { OfferChip } from "./Visuals/OfferChip";
import { PracticeActions, PracticeSteps, usePractice } from "./PracticeRun";
import { useVisuals } from "./Visuals/useVisuals";
import { useSpareVisual } from "./Visuals/NativeFrame";

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
  /* To New session: where a practice run's End practice run goes. */
  onHome?(): void;
  /* A finished practice run's Start your own: New session, adding a project that way. */
  onStartOwn?(add: "open" | "clone"): void;
}


/* An agent's states with a live CLI to message: at its prompt, working (Send queues), or waiting on the person. */
const CHAT_STATES = new Set(["idle", "processing", "waiting"]);
const NO_QUEUE: QueuedChatMessageDTO[] = [];
/* An agent's states, read from the engine, whose pane isn't running (GET agent says exited for a dead pane). */
const PANE_DOWN = new Set(["paused", "done", "failed", "exited"]);

/* An ask as the Back badge tells them apart: its inbox identity, and what it says (an identity a
   new question at the same agent reuses still reads as new). */
const askIdentity = (item: InboxItemDTO) => `${seenKey(item)}|${item.reason}|${item.detail ?? ""}`;

export function findNode(node: AgentNode | null, id: string): AgentNode | null {
  if (!node) return null;
  if (node.agent.id === id) return node;
  for (const child of node.children) {
    const found = findNode(child, id);
    if (found) return found;
  }
  return null;
}

export function SessionScreen({ client, root, repo, refresh, store, agent, sheet, onSelect, openUrl, onOpen, onCloseSession, onHome, onStartOwn }: SessionScreenProps) {
  const appTheme = useAppTheme();
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

  // After a restart (an update), the last daemon paused every session on its way out and the new one is
  // bringing them back: a row still paused that the resuming engine names pending is restarting, not paused.
  // Nor is one the recovery named, until its pane may show: back or not, the rows read before then (a
  // snapshot without its root, a feed row from before its release or none) can't say so. Nor, after that,
  // until the feed's row has been read since its return (the gate's `stale`, kept past recovery's end), nor
  // while the gate hasn't heard from the engine since the app (re)connected (`unsure`): a paused row, or no
  // root in the snapshot, is then a row from before, never the engine's word. Only a row read after its
  // return says paused (the person's pause); while unsure, the live terminal or Restarting… stays.
  const panes = useContext(PanesReadyContext);
  // Likewise the liveness sweep's "not alive" for a session recovery brought back: it may be a look taken
  // before its return, arriving after it. It stands once the engine, asked after it came (GET agent looks
  // at the pane itself), says the pane is down too; till then the pane stays as it is.
  const nodeId = node?.agent.id ?? null;
  const said = nodeId ? alive[nodeId] : undefined;
  const saidAt = useAppState((s) => (nodeId ? s.aliveAt[nodeId] : undefined));
  const recovered = !!nodeId && !!panes.recovered?.includes(nodeId);
  const [downCheck, setDownCheck] = useState<{ id: string; at: number | undefined; down: boolean } | null>(null);
  useEffect(() => {
    if (!nodeId || !recovered || said !== false) return;
    let current = true;
    void client.agent(nodeId).then((found) => {
      if (current) setDownCheck({ id: nodeId, at: saidAt, down: PANE_DOWN.has(found.status) });
    }, () => {});
    return () => { current = false; };
  }, [client, nodeId, recovered, said, saidAt]);
  const nodeAlive = said === false && recovered && !(downCheck?.id === nodeId && downCheck.at === saidAt && downCheck.down) ? undefined : said;
  const pendingHere = panes.pending?.includes(root) === true;
  const returning = !panesReadyFor(panes, root) && (pendingHere || !!panes.returning?.includes(root));
  // Recovery named it and it isn't known back, its pane shown anyway (the gate's startup deadline): the same.
  const unread = pendingHere ? "pending"
    : panes.returning?.includes(root) && !panes.back?.includes(root) ? "recovery named it, not back yet"
    : panes.stale?.includes(root) ? "row not read since its return"
    : panes.unsure ? "engine not heard since connecting" : null;
  const looksStopped = session ? session.status === "paused" || (tree === null && synced) : tree === null && synced;
  const restarting = returning || (!!unread && looksStopped);
  const sessionStatus = restarting ? "resuming" : session?.status ?? null;
  const paused = !restarting && (session ? isPaused(session) : tree === null && synced);
  // A session seen live here that pauses under the view (a fresh start in its checkout, a pause from
  // elsewhere) stays on screen and says so, with Reopen; one opened already paused doesn't.
  const live = session ? !isPaused(session) : tree !== null;
  const [seenLive, setSeenLive] = useState<string | null>(null);
  useEffect(() => {
    if (live) setSeenLive(root);
  }, [live, root]);
  const pausedUnder = paused && seenLive === root;
  // Each switch between live, restarting and paused, with why, to the debug log while it's on: the next
  // restart's log then shows what the view went by.
  const view = restarting ? "restarting" : paused ? "paused" : "live";
  const why = restarting
    ? returning ? "recovery named it and its pane can't show yet" : `rows can't say paused: ${unread}`
    : paused ? (session ? `feed row says ${session.status}` : "no feed row and no root in the snapshot") : session ? `feed row says ${session.status}` : tree ? "root in the snapshot" : "nothing read yet";
  const logged = useRef<{ root: string; view: string } | null>(null);
  useEffect(() => {
    const before = logged.current?.root === root ? logged.current.view : null;
    if (before === view) return;
    logged.current = { root, view };
    logDebug("session_view", "info", `${root}: ${before ?? "opened"} → ${view} (${why})`, {
      root, from: before, to: view, reason: why, row: session?.status ?? null, tree: tree !== null, synced,
      ready: panes.ready, pending: panes.pending?.includes(root) ?? null, stale: panes.stale?.includes(root) ?? false, unsure: !!panes.unsure, seen_live: seenLive === root,
    });
  });

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
  // A practice run (the demo's practice repo): its steps over the terminal, and its own actions in the bar.
  const practice = usePractice(client, repo, root, tree?.children.length ?? 0);
  // Opening a session the engine hasn't brought back yet asks it to do this one next; no answer matters.
  const comingBack = pendingHere;
  useEffect(() => {
    if (comingBack) client.prioritize(root).catch(() => {});
  }, [client, root, comingBack]);
  // The engine keeps the session on screen, so the next one brings it back first; no answer matters.
  useEffect(() => {
    client.viewed(root).catch(() => {});
  }, [client, root]);
  // The app's gate shows this one's pane as soon as it is back after a restart (OpenSessionContext).
  const opened = useContext(OpenSessionContext);
  useEffect(() => {
    opened(root);
    return () => opened(null);
  }, [opened, root]);
  // The engine's word on a PR: the Review & open PR entry exists only once it says ready.
  const readiness = usePrReadiness(client, root, store);
  const changes = useSessionDiff(client, root, store);
  // The session's changes in the chat's place, opened from the sidebar's Changes row; read afresh each time.
  const showChanges = shownSheet?.kind === "changes";
  const reloadChanges = changes.reload;
  const [changesOpened, setChangesOpened] = useState<string | null>(null);
  useEffect(() => {
    if (!showChanges) return;
    setChangesOpened(root);
    reloadChanges();
  }, [showChanges, reloadChanges, root]);
  useEffect(() => {
    if (!showChanges) return;
    // Esc goes back to the chat, unless an edit box took it first.
    const back = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) select(selected, null);
    };
    window.addEventListener("keydown", back);
    return () => window.removeEventListener("keydown", back);
  });
  // The session's visuals; one open replaces the chat (the terminal stays mounted under it, hidden).
  const visuals = useVisuals(client, root);
  // The newest one not opened yet is built ahead, so opening it is at once.
  useSpareVisual(visuals.visuals);
  const showVisual = shownSheet?.kind === "visual";
  const visualId = shownSheet?.kind === "visual" ? shownSheet.id : null;
  // Stable, for the chat's link handler: it opens on the agent and route as they are when clicked.
  const latest = useRef({ select, selected });
  latest.current = { select, selected };
  const openVisual = useCallback((id: string) => latest.current.select(latest.current.selected, { kind: "visual", id }), []);
  // The person asked for a plan: it opens in the planning view at once, even while still being written.
  // Events from before this screen showed are not replayed.
  const planAsked = useAppState((s) => s.recent.filter((e) => e.kind === "plan.requested" && e.root_id === root).at(-1));
  const seenPlanAsk = useRef(planAsked?.id ?? 0);
  useEffect(() => {
    const visual = planAsked?.payload.visual;    if (!planAsked || planAsked.id <= seenPlanAsk.current || typeof visual !== "string") return;
    seenPlanAsk.current = planAsked.id;
    openVisual(visual);
  }, [planAsked, openVisual]);
  // What waits on the person when the visual opened, by identity and wording: one that wasn't there
  // (or left and came back) while it's open, and Back carries a sap badge until the next open. A
  // supervisor's question keeps one inbox row ("has a question") however often it asks, so each new
  // autopilot.need_user on the bus (the question itself, its own event id) counts too.
  const asked = useRef<{ items: Set<string>; question: number }>({ items: new Set(), question: 0 });
  const [asking, setAsking] = useState(false);
  const question = useAppState((s) => s.recent.filter((e) => e.kind === "autopilot.need_user" && (e.root_id === root || (!!e.agent_id && members.has(e.agent_id)))).at(-1)?.id ?? 0);
  useEffect(() => {
    if (!visualId) return;
    asked.current = { items: new Set(waiting.map(askIdentity)), question };
    setAsking(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visualId]);
  useEffect(() => {
    if (!visualId) return;
    const live = new Set(waiting.map(askIdentity));
    if ([...live].some((k) => !asked.current.items.has(k)) || question > asked.current.question) setAsking(true);
    asked.current = { items: new Set([...asked.current.items].filter((k) => live.has(k))), question: Math.max(question, asked.current.question) };
  }, [waiting, visualId, question]);
  // A new HEAD the diff's own refresh found: readiness (an open PR's unpushed count) is read again.
  const head = changes.diff?.head;
  const seenHead = useRef(head);
  const reloadReadiness = readiness.reload;
  useEffect(() => {
    if (seenHead.current && head && head !== seenHead.current) reloadReadiness();
    if (head) seenHead.current = head;
  }, [head, reloadReadiness]);

  // The message box, under a Claude Code agent's live pane: the supervisor's or a worker's, while it runs
  // here, on an engine that says it takes the box's messages. Files dropped anywhere on the session wait
  // in the box as chips: the box of the agent they were dropped on, and no other's.
  const chatSupported = useChatSupport(client);
  const shown = node?.agent ?? null;
  const chatAgent = chatSupported && shown && shown.provider === "claude" && !shown.locked && !paused && sessionStatus !== "resuming" && CHAT_STATES.has(shown.status) && nodeAlive !== false ? shown : null;
  const [dropped, setDropped] = useState<{ agent: string; files: File[] }>();
  const [dropping, setDropping] = useState(false);
  const chatId = chatAgent?.id;
  const dropFiles = useCallback((files: File[]) => setDropped(chatId ? { agent: chatId, files } : undefined), [chatId]);
  // Typed in the agent's pane while the box stands in for its hidden prompt: into the box, the keyboard with it.
  const typing = useRef<ChatBoxTyping>(null);
  const composeText = useCallback((text: string) => typing.current?.type(text), []);

  const host = useRef<HTMLElement>(null);
  useEffect(() => {
    const section = host.current;
    if (!section || shownSheet) return;
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
  }, [root, repo, selected, shownSheet]);

  return (
    <VisualLinkContext.Provider value={openVisual}>
    <section ref={host} className="relative flex min-h-0 flex-1 flex-col" aria-label="Session">
      {/* An open visual takes the whole window; the session's own chrome waits, mounted, for Back. */}
      <div className="contents" hidden={showVisual}>
      <SessionBar
        visuals={<><OfferChip client={client} root={root} /><VisualsButton visuals={visuals.visuals} onOpen={openVisual} /></>}
        client={client}
        root={root}
        repo={repo}
        node={node}
        alive={nodeAlive}
        sessionStatus={sessionStatus}
        restarting={restarting}
        paused={paused}
        reload={reload}
        refresh={refresh}
        recovery={recovery}
        onAgent={(id) => select(id, null)}
        onOpen={onOpen}
        closeSession={closeSession}
        onChat={showChanges ? closeSheet : undefined}
        practice={practice && (
          <PracticeActions
            client={client}
            done={practice.done}
            onStartOwn={onStartOwn}
            onEnded={async () => {
              await reload();
              onHome?.();
            }}
          />
        )}
        hidePause={practice?.done}
      />
      {practice && !showChanges && <PracticeSteps step={practice.step} done={practice.done} />}
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
      </div>
      <div className={showChanges || showVisual ? "hidden" : "flex min-h-0 flex-1 p-s2"} data-testid="chat-area">
        {!tree && !paused && !synced ? (
          <p className="flex-1 text-ink-3">Loading…</p>
        ) : !tree && !paused && !session ? (
          <p className="flex-1 text-ink-3">No session {root}.</p>
        ) : (
          // Files dragged over a Claude Code session land in its message box; any other session's drags stay the terminal's.
          <ChatDrop enabled={!!chatAgent} onFiles={dropFiles} onDragging={setDropping} className="flex min-w-0 flex-1 flex-col gap-3">
            <TerminalFrame ground={appTheme === "light" ? chatTheme("light").background : undefined} data-testid="terminal-surface">
              <ProviderSurface
                // A sidebar Resume also needs a fresh pane when this session was already selected.
                key={`${selected}:${paused}`}
                repo={repo}
                root={root}
                agent={node?.agent ?? null}
                alive={nodeAlive}
                sessionStatus={sessionStatus === "resuming" || !node || node.agent.id === root ? sessionStatus : null}
                restarting={restarting}
                sessionName={session?.name || session?.goal}
                credentials={credentials}
                recovery={recovery}
                hidePrompt={!!chatAgent && chatAgent.id === node?.agent.id}
                onComposeText={composeText}
              />
            </TerminalFrame>
            {chatAgent && (
              <ChatBox
                key={chatAgent.id}
                client={client}
                agentId={chatAgent.id}
                busy={chatAgent.status === "processing"}
                queue={chatAgent.chat_queue ?? NO_QUEUE}
                droppedFiles={dropped?.agent === chatAgent.id ? dropped.files : undefined}
                onFilesConsumed={() => setDropped(undefined)}
                onChanged={refresh}
                recipient={chatAgent.id === root ? "the supervisor" : agentName(workspaces, chatAgent.id)}
                dropping={dropping}
                typing={typing}
              />
            )}
          </ChatDrop>
        )}
      </div>
      {/* Kept once opened, so an unsaved edit outlives a look back at the chat. */}
      {changesOpened === root && (
        <div className={showChanges ? "flex min-h-0 min-w-0 flex-1 flex-col p-s2" : "hidden"} hidden={!showChanges} data-testid="session-changes">
          <SessionDiffEditor key={root} client={client} root={root} state={changes} />
        </div>
      )}
      {/* The one thing that ever lies over a session, opened on purpose: an open PR's Review changes.
          Open, it stays through readiness being read again (an edit here, a sweep): it says so quietly instead. */}
      {shownSheet?.kind === "review" && <ReviewSheet key={root} client={client} root={root} readiness={readiness} state={changes} onClose={closeSheet} openUrl={openUrl} />}
      {visualId && (
        <VisualReader
          client={client}
          repo={repo}
          visual={visuals.visuals?.find((v) => v.id === visualId) ?? null}
          loading={visuals.visuals === null}
          asking={asking}
          onBack={closeSheet}
          onAnswered={visuals.put}
          onRead={visuals.read}
        />
      )}
    </section>
    </VisualLinkContext.Provider>
  );
}

/* The selected agent's status and controls. */
function SessionBar({ client, root, repo, node, alive, sessionStatus, restarting = false, paused, reload, refresh, recovery, onAgent, onOpen, closeSession, onChat, visuals, practice, hidePause = false }: {
  client: DaemonClient;
  /* A practice run's own actions (End practice run; done, Start your own), before Pause. */
  practice?: ReactNode;
  /* A finished practice run has nothing left to pause. */
  hidePause?: boolean;
  /* The header's Visuals N (nothing while the session has none). */
  visuals?: ReactNode;
  root: string;
  repo: string;
  node: AgentNode | null;
  alive: boolean | undefined;
  sessionStatus: string | null;
  /* Paused by the last daemon on its way out, and being brought back by this one (a restart). */
  restarting?: boolean;
  paused: boolean;
  reload: () => Promise<void>;
  refresh: () => Promise<void>;
  recovery: Recovery;
  onAgent(id: string): void;
  onOpen?(root: string, repo: string): void;
  closeSession?: CloseSessionItem;
  /* The changes are showing in the chat's place: the way back. */
  onChat?(): void;
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
  const resuming = sessionStatus === "resuming";
  const status = resuming ? { glyph: "◌", label: restarting ? "Restarting…" : "Resuming", style: "dim" as const, needs: false }
    : node ? cliStatus(node.agent, node.workspace, alive) : { glyph: "‖", label: sessionStatus ?? "paused", style: "dim" as const, needs: false };
  const named = useContext(SessionsContext)?.find(root)?.name;
  const who = node ? (node.agent.id === root ? (named || "supervisor") : agentName(workspaces, node.agent.id)) : named || "supervisor";
  const provider = node ? providerName(node.agent.provider) : "Session";
  const ago = node ? since(node.agent.status_since) : null;
  // The bar's own notice is the pause's; a continue's shows on the surface's card, where it was asked for.
  return (
    <header className="flex flex-wrap items-center gap-x-s2 gap-y-1 border-b border-line px-s2 py-1" data-testid="session-bar">
      <div className="flex min-w-0 items-center gap-s1">
        <span className="truncate text-sm font-semibold" data-testid="surface-title">
          {provider} · {who}
        </span>
        {/* No agent shown (a paused root is in no snapshot): the session's own id, so each one still reads as itself. */}
        <span className={`${mono} hidden text-xs text-ink-3 sm:inline`} data-testid={node ? "agent-id" : "session-id"} title={node?.workspace.branch}>
          {node?.agent.id ?? root}
        </span>
        <span className={`flex items-center gap-1 text-xs ${resuming ? "rounded-full bg-branch-soft px-2 py-0.5 text-branch" : status.style === "alert" ? "text-sap" : "text-ink-2"}`} data-testid="session-status" data-state={restarting ? "restarting" : resuming ? "resuming" : node?.agent.status ?? sessionStatus ?? "paused"} data-tone={resuming ? "work" : undefined}>
          <span className={`font-mono ${CLI_INK[status.style]}`} aria-hidden>
            {status.glyph}
          </span>
          {status.label}
          {ago && status.style !== "dim" && <span className="text-ink-3">· {ago}</span>}
        </span>
      </div>
      <span className="flex-1" />
      {visuals}
      {onChat && (
        <button type="button" className={button} onClick={onChat} data-testid="back-to-chat" title="Back to the chat (esc)">
          ← Chat
        </button>
      )}
      {practice}
      {paused ? (
        <LoadingButton className={main} data-session-start={root} busy={recovery.busy} disabled={recovery.busy} onClick={() => recovery.resume()}>
          Continue
        </LoadingButton>
      ) : !resuming && !hidePause && (
        <LoadingButton className={button} busy={busy} disabled={busy} onClick={() => pause()}>
          Pause
        </LoadingButton>
      )}
      {/* Not keyed by the agent, and kept when no agent is left to show (a transfer pauses the scratch
          session's root, which leaves the snapshot): a card it opened stays while its job runs and says how it ended. */}
      {!resuming && menuNode && (
        <AgentMenu client={client} agent={menuNode.agent} workspace={menuNode.workspace} gone={!node} root={root} repo={repo} refresh={refresh} onAgent={onAgent} onSession={onOpen} closeSession={closeSession} />
      )}
      {/* No agent shown (a paused session's root is in no snapshot): the session's own ⋯, so it can still be closed. */}
      {!resuming && !node && closeSession && <SessionMenu closeSession={closeSession} />}
      {notice && <p className={`w-full text-xs ${notice.failed ? "text-sap" : "text-ink-2"}`}>{notice.text}</p>}
    </header>
  );
}
