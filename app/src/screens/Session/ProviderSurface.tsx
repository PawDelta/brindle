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

import { useEffect, useRef, useState } from "react";
import type { AgentDTO, InboxItemDTO } from "../../api/schema";
import { usePtyViews } from "../../api/ptyView";
import { Preflight } from "../../components/Preflight";
import { PtyTerminal, type TerminalView } from "../../components/PtyTerminal";
import { button, card as cardClass, main, quiet } from "../../components/ui";
import { providerName } from "../../store/overview";
import type { Recovery } from "./useContinue";

export interface ProviderSurfaceProps {
  /* The session's repo (a preflight sign-in checks the provider there). */
  repo: string;
  /* The agent shown; null for a session with no agents in the snapshot (paused). */
  agent: AgentDTO | null;
  /* Whether its process is alive, from the liveness sweep (undefined: not known yet). */
  alive?: boolean;
  /* The session's listed state, for a session with no agents to show. */
  sessionStatus?: string | null;
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
  | { kind: "disconnected" };

/* The card the surface shows instead of a pane, or null for a live pane.
   `unshowable`: why a live pane can't be shown here (the pty view's status). */
export function surfaceCard(agent: AgentDTO | null, alive: boolean | undefined, sessionStatus: string | null | undefined, view: TerminalView | null, credentials: InboxItemDTO | null | undefined, unshowable?: string | null): Card | null {
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

export function ProviderSurface({ repo, agent, alive, sessionStatus, credentials, recovery }: ProviderSurfaceProps) {
  const [view, setView] = useState<TerminalView | null>(null);
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
  const [seen, setSeen] = useState(recovery.attempt);
  if (seen !== recovery.attempt) {
    setSeen(recovery.attempt);
    setView(null);
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
  const card = surfaceCard(agent, alive, sessionStatus, view, credentials, unshowable);
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
  const target = agent?.window ?? null;
  const left = agent && card && card.kind !== "starting" && !(card.kind === "ended" && card.locked) && !!target && !!ready && (alive === false || STOPPED.has(agent.status));

  return (
    <div className="relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-md bg-shell-bg" data-testid="provider-surface" data-card={card?.kind ?? "live"}>
      {/* A real tmux client on a pty; nothing until the shell has said it can (its status). */}
      {agent && live && ready && target && (
        <PtyTerminal key={`${agent.id}:${recovery.attempt}:${reconnects}`} at={{ server: agent.tmux_server ?? null, target }} open={ready.open} copy={ready.copy} paste={ready.paste} onView={setView} />
      )}
      {/* The engine's preflight items, from either Continue, whatever this pane shows: over a live
          worker's pane (the supervisor's chat is what the continue brings back) as a strip, else as the surface. */}
      {preflight && (
        <div className={`${live ? "shrink-0 border-b border-shell-line" : "flex-1"} flex items-start justify-center overflow-auto bg-canvas p-s4`}>
          <Preflight preflight={preflight} onRefresh={setPreflight} onLaunch={() => void resume()} launchLabel="Continue" repo={repo} />
        </div>
      )}
      {card && !preflight && (
        <div className={`${left && showLeft ? "shrink-0" : "flex-1"} flex items-start justify-center overflow-auto bg-canvas p-s4`}>
            <section className={`${cardClass} flex w-full max-w-xl flex-col gap-s2 px-s3 py-s2`} aria-label={card.kind === "starting" ? `Starting ${name}` : card.kind === "disconnected" ? "Disconnected" : card.title} data-testid="state-card" data-kind={card.kind}>
              {card.kind === "starting" && (
                <div className="flex items-center gap-s1">
                  <span className="size-3 shrink-0 animate-spin rounded-full border-2 border-branch border-t-transparent" aria-hidden />
                  <h2 className="text-base font-semibold">Starting {name}…</h2>
                </div>
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
                      <button type="button" className={credentials && card.kind === "failed" ? button : main} disabled={busy} onClick={() => void resume()}>
                        {card.kind === "failed" ? "Restart" : "Continue"}
                      </button>
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
          <PtyTerminal key={`${agent.id}:left:${reconnects}`} at={{ server: agent.tmux_server ?? null, target }} open={ready.open} copy={ready.copy} paste={ready.paste} onView={setLeftView} />
        </>
      )}
    </div>
  );
}
