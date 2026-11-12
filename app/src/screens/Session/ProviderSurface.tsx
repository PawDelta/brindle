/* The provider's own session, front and centre: what Claude Code, Codex or
   OpenCode draws, edge to edge, and nothing of the shell or tmux beneath
   it. While the agent's process runs its pane shows here; the moment it
   doesn't (exited, failed, paused, done, never
   started) a native state card takes the surface instead, so a shell
   prompt is never what the person is left looking at: what ended or
   failed and why, with Continue / Restart (a provider whose credentials
   lapsed says so; the sign-in itself is the Inbox card's: nothing pops up
   over a session), and the engine's preflight items as native UI when a
   launch is refused
   with 409 {preflight}. Continue here and Continue in the session's bar are
   the same handler (useContinue, owned by the session view): its preflight
   shows here whichever button was pressed, and each success opens the
   pane afresh. What the dead pane left (its last screen: tmux keeps a
   dead pane's window) stays behind "Show what it left".

   The pane is a real tmux client on a pty (PtyTerminal, through the app's
   PtyViews) on the agent's window, for the session's supervisor and each
   worker alike, its last screen included. A pane that can't be shown here
   (a tmux too old for the pty view, no window named for it, a browser tab
   with no shell) says so on the card instead. */

import { useContext, useEffect, useMemo, useRef, useState } from "react";
import type { AgentDTO, InboxItemDTO } from "../../api/schema";
import { DaemonContext } from "../../api/context";
import { usePtyViews } from "../../api/ptyView";
import type { Attachment } from "../../api/attachments";
import { LoadingButton } from "../../components/LoadingButton";
import { Preflight } from "../../components/Preflight";
import { PtyTerminal, type TerminalView } from "../../components/PtyTerminal";
import { button, card as cardClass, main, quiet } from "../../components/ui";
import { providerName } from "../../store/overview";
import { PanesReadyContext, panesReadyFor } from "../../store/panes";
import type { Recovery } from "./useContinue";
import { visualsGate, withTypedWords, withVisualInput } from "./Visuals/terminalInput";

export interface ProviderSurfaceProps {
  /* The session's repo (a preflight sign-in checks the provider there). */
  repo: string;
  /* The session's root: whether its panes may show is asked of it alone (store/panes). */
  root?: string;
  /* The agent shown; null for a session with no agents in the snapshot (paused). */
  agent: AgentDTO | null;
  /* Whether its process is alive, from the liveness sweep (undefined: not known yet). */
  alive?: boolean;
  /* The root's listed state; omitted when showing a worker. */
  sessionStatus?: string | null;
  /* The session's name or goal: with no agent to show, what tells one stopped session's card from another's. */
  sessionName?: string | null;
  /* A credentials item for this agent: its card says what lapsed. */
  credentials?: InboxItemDTO | null;
  /* The session's Continue / Restart: shared with the bar (useContinue). */
  recovery: Recovery;
}

const STOPPED = new Set(["paused", "done", "failed", "exited"]);

type Card =
  | { kind: "starting" }
  | { kind: "ended"; title: string; why: string | null; locked?: boolean }
  /* `unshowable`: the pane runs but can't be shown here (tmux too old): no Restart. */
  | { kind: "failed"; title: string; why: string; unshowable?: boolean }
  | { kind: "disconnected" }
  /* The engine is still bringing its sessions back (store/panes.ts): the pane follows. */
  | { kind: "returning" };

/* The card the surface shows instead of a pane, or null for a live pane.
   `unshowable`: why a live pane can't be shown here (the pty view's status). */
export function surfaceCard(agent: AgentDTO | null, alive: boolean | undefined, sessionStatus: string | null | undefined, view: TerminalView | null, credentials: InboxItemDTO | null | undefined, unshowable?: string | null): Card | null {
  if (sessionStatus === "resuming" && !agent?.locked) return { kind: "returning" };
  const name = agent ? providerName(agent.provider) : "The session";
  if (!agent) {
    if (sessionStatus === "failed") return { kind: "failed", title: `${name} failed`, why: "Its supervisor stopped with an error." };
    return { kind: "ended", title: sessionStatus === "paused" ? `${name} is paused` : `${name} isn't running`, why: null };
  }
  const lockedBy = agent.locked;
  if (lockedBy) return { kind: "ended", title: `Locked by ${lockedBy}`, why: "Another identity holds this session; its conversation isn't shown here.", locked: true };
  if (agent.status === "failed") return { kind: "failed", title: `${name} stopped`, why: credentials?.detail ? `Its credentials need attention: ${credentials.detail}` : "Its credentials need attention." };
  if (agent.status === "starting" && (!view || view.kind === "connecting")) return { kind: "starting" };
  if (STOPPED.has(agent.status) || alive === false) {
    const how = agent.status === "paused" ? "is paused" : agent.status === "done" ? "finished" : "ended";
    return { kind: "ended", title: `${name} ${how}`, why: agent.status === "done" ? "It reported its result." : null };
  }
  if (unshowable) return { kind: "failed", title: `${name} can't be shown`, why: unshowable, unshowable: true };
  if (view?.kind === "error") return { kind: "failed", title: `${name} can't be shown`, why: view.message };
  if (view?.kind === "closed") return { kind: "disconnected" };
  return null;
}

/* Why no pane can be shown in a browser tab: the pty view is the shell's. */
export const NO_SHELL = "Sessions show in the brindle app, through its own tmux: a browser tab can't show this pane.";
/* Why an agent's pane can't be shown when the engine names no window for it. */
export const NO_WINDOW = "The engine names no tmux window for it.";

interface PaneCheck {
  agent: AgentDTO;
  view: TerminalView | null;
  attempt: number;
  alive: boolean | undefined;
  sessionStatus: string | null | undefined;
  found: AgentDTO;
}

export function ProviderSurface({ repo, root, agent: listedAgent, alive, sessionStatus, sessionName, credentials, recovery }: ProviderSurfaceProps) {
  const [view, setView] = useState<TerminalView | null>(null);
  const client = useContext(DaemonContext);
  const visualsGateRef = useRef<ReturnType<typeof visualsGate> | null>(null);
  useEffect(() => {
    const gate = visualsGate(client);
    visualsGateRef.current = gate;
    return () => { gate.close(); visualsGateRef.current = null; };
  }, [client]);
  const attempt = recovery.attempt;
  const [checked, setChecked] = useState<PaneCheck | null>(null);
  useEffect(() => {
    // Check on opening, on state changes, and after a view fails or exits:
    // a view's exit/error alone cannot distinguish a lost client from a missing pane.
    // GET agent checks the actual pane; the sessions list only carries the recorded status.
    if (!client || !listedAgent) return;
    let current = true;
    const at = { agent: listedAgent, view, attempt, alive, sessionStatus };
    void client.agent(listedAgent.id).then((found) => {
      if (current) setChecked({ ...at, found });
    }, () => {});
    return () => { current = false; };
  }, [client, listedAgent, view, attempt, alive, sessionStatus]);
  const currentCheck = checked?.agent === listedAgent && checked?.attempt === attempt
    && checked?.alive === alive && checked?.sessionStatus === sessionStatus ? checked : null;
  // Keep the complete refreshed row: a resumed session can have a different pane
  // even when its status is unchanged. A view failure must not pin the old target.
  // Once applied, the row also belongs to the fresh view it opens; cleanup above
  // still discards responses from requests superseded by another view or state.
  const agent = currentCheck ? currentCheck.found
    : listedAgent && sessionStatus && STOPPED.has(sessionStatus)
      ? { ...listedAgent, status: sessionStatus }
      : listedAgent;
  // What the view of a dead pane's last screen says: why it couldn't open, say.
  const [leftView, setLeftView] = useState<TerminalView | null>(null);
  const [reconnects, setReconnects] = useState(0);
  // A worker that finished (done, or idle having reported) shows what it left, its final summary, from the
  // start; a pane that merely died (a shell prompt by now) stays behind the card's disclosure.
  const finished = !!agent && (agent.status === "done" || (agent.status === "idle" && !!agent.reported));
  const [showLeft, setShowLeft] = useState(finished);
  /* The continue the surface has seen: a newer one (from either button) is a
     fresh view, and what the last one said is forgotten, so a closed view
     doesn't keep the card up over the chat that came back. */
  // A pane can return outside Continue too (another app window or the CLI).
  // Forget an old stream's failure when its lifecycle changes, without reopening
  // a healthy terminal on every snapshot refresh.
  // Panes ready again after the engine was away (store/panes.ts): whatever the old view said is
  // forgotten, and the pane opens afresh on the rows read since.
  const panes = useContext(PanesReadyContext);
  // A session released ahead of the rest is a fresh view of its own too, without touching the others'.
  const epoch = `${panes.epoch}:${root ? panes.back?.indexOf(root) ?? -1 : -1}`;
  const lifecycle = JSON.stringify([agent?.id, agent?.status, agent?.window, agent?.tmux_server, alive, recovery.attempt, epoch]);
  const [seen, setSeen] = useState({ lifecycle, attempt, epoch });
  if (seen.lifecycle !== lifecycle) {
    setSeen({ lifecycle, attempt, epoch });
    if (view?.kind === "error" || view?.kind === "closed" || (checked && STOPPED.has(checked.found.status)) || attempt !== seen.attempt || epoch !== seen.epoch) setView(null);
    setLeftView(null);
    setShowLeft(finished);
  }

  const pty = usePtyViews();
  // The pty view can't run here (no shell, the app's tmux), or couldn't open on this pane (the agents' tmux
  // too old, its server gone, no window): the view's problem, not the agent's, so the card offers to look
  // again, not a restart.
  const unshowable = !pty ? NO_SHELL : pty.status.kind === "unusable" ? pty.status.message : agent && !agent.window ? NO_WINDOW : view?.kind === "error" ? view.message : null;
  // Opened (or reopened, a surface per agent) on a pane that couldn't be shown: ask the shell again.
  const recheck = pty?.recheck;
  const opened = useRef(false);
  useEffect(() => {
    if (opened.current) return;
    opened.current = true;
    if (unshowable) recheck?.();
  }, [unshowable, recheck]);
  // The engine is still bringing its sessions back (a launch, or a new daemon after a restart): the
  // rows may name windows from before, so no pane yet, and none of the cards a missing window or a
  // closed view would bring. What it finished or failed with still shows.
  const returning = !panesReadyFor(panes, root) && !!agent && !agent.locked && agent.status !== "done" && agent.status !== "failed";
  const card: Card | null = returning ? { kind: "returning" } : surfaceCard(agent, alive, sessionStatus, view, credentials, unshowable);
  const name = agent ? providerName(agent.provider) : "the session";
  const { resume, busy, notice, preflight, setPreflight } = recovery;

  /* A fresh socket to the pane after a dropped stream. */
  const reconnect = () => {
    setView(null);
    setLeftView(null);
    setShowLeft(finished);
    setReconnects((n) => n + 1);
  };

  // The pane shows only while the process runs; a stopped agent's pane is
  // what it left (tmux keeps the dead pane), shown behind a disclosure of
  // the card, not as a surface, where the pty view can show it.
  const live = agent && !card;
  const ready = pty?.status.kind === "ready" ? pty : null;
  // Claude and OpenCode have authenticated prompt-submit hooks. The other
  // CLIs need the shell's input observed before their native PTY write. The
  // hooks can't say what was typed rather than pasted, so those two report that.
  const observedOpen = useMemo(() => ready && client && agent
    ? (["claude", "opencode"].includes(agent.provider) ? withTypedWords : withVisualInput)(client, agent.id, ready.open, () => visualsGateRef.current?.enabled() ?? true)
    : ready?.open, [ready?.open, client, agent?.id, agent?.provider]);
  const target = agent?.window ?? null;
  const attachments = useMemo(() => client && agent ? {
    acceptsFiles: agent.accepts_files === true,
    attach: (file: Attachment) => client.attach(agent.id, file),
  } : undefined, [client, agent?.id, agent?.accepts_files]);
  const left = agent && card && card.kind !== "starting" && card.kind !== "returning" && !(card.kind === "ended" && card.locked) && !!target && !!ready && (alive === false || STOPPED.has(agent.status));

  return (
    <div className="relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-md bg-shell-bg" data-testid="provider-surface" data-card={card?.kind ?? "live"}>
      {/* A real tmux client on a pty; nothing until the shell has said it can (its status). */}
      {/* Mounted under the "starting" card too: the card leaves once this view is live, and an agent
          that stays "starting" while it waits on a startup prompt (trust, permission) is shown by it. */}
      {agent && (live || card?.kind === "starting") && ready && target && (
        <PtyTerminal key={`${agent.id}:${recovery.attempt}:${reconnects}:${epoch}`} at={{ server: agent.tmux_server ?? null, target }} open={observedOpen!} copy={ready.copy} paste={ready.paste} attachments={attachments} onView={setView} />
      )}
      {/* The engine's preflight items, from either Continue, whatever this pane shows: over a live
          worker's pane (the supervisor's chat is what the continue brings back) as a strip, else as the surface. */}
      {preflight && (
        <div className={`${live ? "shrink-0 border-b border-shell-line" : "flex-1"} flex items-start justify-center overflow-auto bg-canvas p-s4`}>
          <Preflight preflight={preflight} onRefresh={setPreflight} onLaunch={() => void resume()} launchLabel="Continue" repo={repo} />
        </div>
      )}
      {card && !preflight && (
        <div className={`${card.kind === "starting" && ready && target ? "absolute inset-0 z-10" : left && showLeft ? "shrink-0" : "flex-1"} flex items-start justify-center overflow-auto bg-canvas p-s4`}>
            <section className={`${cardClass} flex w-full max-w-xl flex-col gap-s2 px-s3 py-s2`} aria-label={card.kind === "starting" ? `Starting ${name}` : card.kind === "returning" ? `Bringing ${name} back` : card.kind === "disconnected" ? "Disconnected" : card.title} data-testid="state-card" data-kind={card.kind}>
              {card.kind === "starting" && (
                <div className="flex items-center gap-s1">
                  <span className="size-3 shrink-0 animate-spin rounded-full border-2 border-branch border-t-transparent" aria-hidden />
                  <h2 className="text-base font-semibold">Starting {name}…</h2>
                </div>
              )}
              {card.kind === "returning" && (
                <>
                  <div className="flex items-center gap-s1">
                    <span className="size-3 shrink-0 animate-spin rounded-full border-2 border-branch border-t-transparent" aria-hidden />
                    <h2 className="text-base font-semibold">Bringing {name} back…</h2>
                  </div>
                  {panes.recovery && <p className="text-xs text-ink-3" data-testid="recovery-progress">Reopening sessions, {panes.recovery.done} of {panes.recovery.total}</p>}
                  <p className="text-sm text-ink-2">brindle is reopening your sessions. The conversation shows here in a moment; everything else already works.</p>
                </>
              )}
              {card.kind === "disconnected" && (
                <>
                  <h2 className="text-base font-semibold">Disconnected from {name}</h2>
                  <p className="text-sm text-ink-2">The app's view of its pane closed. {name} itself may well still be running.</p>
                  <div>
                    <button type="button" className={main} onClick={reconnect}>
                      Reconnect
                    </button>
                  </div>
                </>
              )}
              {(card.kind === "ended" || card.kind === "failed") && (
                <>
                  <h2 className={`text-base font-semibold ${card.kind === "failed" ? "text-sap" : ""}`}>{card.title}</h2>
                  {!agent && sessionName && <p className="truncate text-sm font-medium text-ink" data-testid="state-card-session" title={sessionName}>{sessionName}</p>}
                  {card.why && <p className="text-sm text-ink-2">{card.why}</p>}
                  {!card.why && <p className="text-sm text-ink-2">Nothing of {name} is on screen until it runs again.</p>}
                  <div className="flex flex-wrap items-center gap-s1">
                    {credentials && card.kind === "failed" && <span className="text-sm text-ink-3">Sign in from the Inbox, then restart it.</span>}
                    {card.kind === "failed" && card.unshowable && (
                      <button
                        type="button"
                        className={button}
                        onClick={() => {
                          recheck?.();
                          reconnect();
                        }}
                      >
                        Check again
                      </button>
                    )}
                    {!(card.kind === "ended" && card.locked) && !(card.kind === "failed" && card.unshowable) && (
                      <LoadingButton className={credentials && card.kind === "failed" ? button : main} busy={busy} disabled={busy} onClick={() => resume()}>
                        {card.kind === "failed" ? "Restart" : "Continue"}
                      </LoadingButton>
                    )}
                    {left && (
                      <button type="button" className={quiet} aria-expanded={showLeft} onClick={() => setShowLeft((s) => !s)}>
                        {showLeft ? "Hide what it left" : "Show what it left"}
                      </button>
                    )}
                  </div>
                  {notice && <p className={`text-sm ${notice.failed ? "text-sap" : "text-ink-2"}`}>{notice.text}</p>}
                </>
              )}
            </section>
        </div>
      )}
      {agent && left && showLeft && target && ready && (
        <>
          {leftView?.kind === "error" && (
            <p role="status" className="shrink-0 border-b border-shell-line px-s2 py-1 text-sm text-shell-dim">
              Nothing of it is left to show: {leftView.message}
            </p>
          )}
          {leftView?.kind !== "error" && leftView?.kind !== "closed" && (
            <PtyTerminal key={`${agent.id}:left:${reconnects}`} at={{ server: agent.tmux_server ?? null, target }} open={ready.open} copy={ready.copy} paste={ready.paste} onView={setLeftView} />
          )}
        </>
      )}
    </div>
  );
}
