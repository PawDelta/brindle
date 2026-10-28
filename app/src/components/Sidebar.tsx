/* The merged sidebar (design/options-v2/M3b-multi's layout, with
   design/options-v3/C2b-inbox-only's one entry for what needs a person):
   brindle's whole layer in one column beside the provider's session. From
   the top: the logo (and New session); "Inbox · N", the one place anything
   about attention shows in the sidebar (the Inbox page, in the main area,
   is where it is read and answered); the selected session's context
   (SessionContext: its goal with milestones, progress and checks, and the
   one lifecycle that carries its PR step); a handle that drags the split between them (kept in
   localStorage); then "Sessions": every open session grouped by repo, each
   row its goal with its id and mode under it and a caret that folds its
   agents, and a close ("✕", on hover or focus; "Done · Close", always
   there, once its goal is Done or, with no goal, all in it is at rest)
   that closes the session in one click, nothing asked, the row just
   going; its agents nested in compact rows (the mockup's dot vocabulary
   for the CLI's statuses: branch at work, sap waiting, pass done, quiet
   idle, fail red), each "id · branch" with its state in plain words at the
   right, never a count or a needs-you mark; paused and failed sessions
   out of the list, into one collapsed "Paused · N" row at the foot of their
   repo's group (open per viewer, kept in localStorage; the selected paused
   session stays listed under it). Clicking a session opens its
   provider session on the supervisor; clicking an agent opens the session
   on that agent. Keys, as in the CLI: ↑ ↓ (or j k) move, Enter opens, ⌘1-9
   jump to a session, ⌘N starts one. Knowledge, Agents,
   Settings and Account as small links at the foot, on one line, with the
   terminal and theme switches under them, and beside those,
   only while a provider is near its limit, a few warm words that open
   Account › Usage (design/usage/A4), and beside them only while a provider
   has a problem (signed out, an install failed), a few more that open
   Settings › Providers (design/connect/); no providers block anywhere else
   in the sidebar. There is no right-hand panel: this is
   where brindle's layer lives. The site's surfaces, purple (branch) only
   as the accent (styles.css .side-*). */

import { useCallback, useContext, useEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent as ReactMouseEvent, type RefObject } from "react";
import type { DaemonClient } from "../api/client";
import { DaemonContext } from "../api/context";
import type { OpenUrl } from "../api/open";
import type { GoalDTO, SessionDTO } from "../api/schema";
import { readLastSession, routeSession, sameRoute, type Route, type SessionRef } from "../routes";
import { goalPhase, useGoal } from "../screens/Session/GoalPanel";
import { UNKNOWN } from "../screens/Session/review";
import { agentIds, sessionTree, type AgentNode } from "../screens/Session/tree";
import { useContinue } from "../screens/Session/useContinue";
import { cliStatus, formatAge, isPaused, type CliStatus } from "../store/overview";
import { nearReading, nowSeconds } from "../screens/Account/usage";
import { useSessions } from "../store/sessions";
import { providerProblems, type ProviderProblem } from "../api/providers";
import { useProviders } from "../store/providers";
import { useUsage } from "../store/usage";
import { useAppState, useStore } from "../store/useStore";
import Logo from "./Logo";
import { Preflight } from "./Preflight";
import { SessionContext } from "./SessionContext";
import { TerminalToggle } from "./TerminalPanel";
import { ThemeToggle } from "./ThemeToggle";

export interface SidebarProps {
  route: Route;
  navigate(route: Route): void;
  /* Fetch the snapshot and inbox again (Sync.refresh), after a decision made here. */
  refresh?: () => Promise<void>;
  /* Opens a session's PR page in the system browser (the goal's PR open step). */
  openUrl?: OpenUrl;
}

/* The status line's reading while a provider is near its limit, or null. */
function useNearReading() {
  const { usage } = useUsage();
  return usage ? nearReading(usage.near, nowSeconds()) : null;
}

/* A few warm words in the status line while a provider is near its limit
   (design/usage/A4): the worst one in the fewest words, "Claude 88% ·
   5:10pm" or "Antigravity limit · 7:18pm", "+N" in the line's dim colour
   when others are near too, opening Account › Usage. Nothing otherwise.
   An over-long reading ends in an ellipsis
   rather than wrapping. */
/* Each click is its own request, so a second one with Account open scrolls back to Usage. */
let usageAsks = 0;
function UsageReading({ reading, navigate }: { reading: ReturnType<typeof useNearReading> } & Pick<SidebarProps, "navigate">) {
  if (!reading) return null;
  return (
    <button
      type="button"
      className="group ml-auto inline-flex min-w-0 flex-[0_1_auto] items-baseline gap-1 font-mono text-[10.5px] font-medium text-sap"
      aria-label="Open Account › Usage"
      data-testid="usage-reading"
      data-provider={reading.provider}
      onClick={() => navigate({ screen: "account", section: "usage", ask: ++usageAsks })}
    >
      <span className="min-w-0 overflow-hidden text-ellipsis group-hover:underline group-hover:underline-offset-2">{reading.text}</span>
      {reading.more > 0 && (
        <span className="flex-none font-normal text-ink-3" data-testid="usage-more">
          +{reading.more}
        </span>
      )}
    </button>
  );
}

/* The only thing the sidebar says about providers (design/connect/ step
   9): a problem, in a few warm words beside the usage reading, "Codex
   signed out" or "OpenCode install failed", signed out first, "+N" when
   there are others, opening Settings › Providers. Nothing otherwise: the
   sidebar has no providers block. */
let providerAsks = 0;
function ProviderProblemLine({ problems, navigate }: { problems: ProviderProblem[] } & Pick<SidebarProps, "navigate">) {
  const first = problems[0];
  if (!first) return null;
  return (
    <button
      type="button"
      className="group ml-auto inline-flex min-w-0 flex-[0_1_auto] items-baseline gap-1 font-mono text-[10.5px] font-medium text-sap"
      aria-label={`${first.text}: open Settings › Providers`}
      data-testid="provider-problem"
      data-provider={first.provider}
      data-kind={first.kind}
      onClick={() => navigate({ screen: "settings", section: "providers", ask: ++providerAsks })}
    >
      <span className="min-w-0 overflow-hidden text-ellipsis group-hover:underline group-hover:underline-offset-2">{first.text}</span>
      {problems.length > 1 && (
        <span className="flex-none font-normal text-ink-3" data-testid="provider-problem-more">
          +{problems.length - 1}
        </span>
      )}
    </button>
  );
}

/* The mockup's rows: the keyboard's cursor, the repo heading, the foot's links. */
const CURSOR = "ring-1 ring-inset ring-branch";
const REPO_HEADING = "px-s1 pb-1 text-[10.5px] font-semibold uppercase tracking-[0.08em] text-ink-3";
const FOOT_LINK = "text-[11.5px] text-ink-3 hover:text-ink";

/* The split: how tall the session's context is, kept for next time. */
export const SPLIT_KEY = "brindle.sidebar.split";
const SPLIT_DEFAULT = 330;
const SPLIT_MIN = 120;
/* What the tree keeps below the context, at least. */
const SPLIT_RESERVE = 280;

export function readSplit(): number {
  try {
    const n = Number(localStorage.getItem(SPLIT_KEY));
    return Number.isFinite(n) && n >= SPLIT_MIN ? n : SPLIT_DEFAULT;
  } catch {
    return SPLIT_DEFAULT;
  }
}

function writeSplit(height: number): void {
  try {
    localStorage.setItem(SPLIT_KEY, String(Math.round(height)));
  } catch {
    // No storage: the split lasts for this window.
  }
}

/* The repos whose "Paused · N" row is open, kept for next time. Collapsed by default. */
export const PAUSED_KEY = "brindle.sidebar.pausedOpen";

export function readPausedOpen(): Set<string> {
  try {
    const v: unknown = JSON.parse(localStorage.getItem(PAUSED_KEY) ?? "[]");
    return new Set(Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

function writePausedOpen(open: Set<string>): void {
  try {
    localStorage.setItem(PAUSED_KEY, JSON.stringify([...open]));
  } catch {
    // No storage: the row's state lasts for this window.
  }
}

const clampSplit = (h: number) => Math.max(SPLIT_MIN, Math.min(h, Math.max(SPLIT_MIN, (typeof window === "undefined" ? 900 : window.innerHeight) - SPLIT_RESERVE)));

/* The count of inbox items waiting on an agent, as a badge. */
export function NeedsYou({ count, className = "" }: { count: number; className?: string }) {
  if (count === 0) return null;
  return (
    <span className={`side-nb ${className}`} aria-label={`${count} needing you`} data-testid="needs-you">
      {count}
    </span>
  );
}

/* The mockup's dot for a CLI status: work, wait, done, idle, fail, paused. */
export type DotTone = "work" | "wait" | "done" | "idle" | "fail" | "paused";

export function dotTone(status: CliStatus, agentStatus: string): DotTone {
  if (status.style === "busy") return "work";
  if (status.style === "alert") return agentStatus === "failed" ? "fail" : "wait";
  if (status.style === "bad") return "fail";
  if (status.glyph === "✓") return "done";
  if (status.glyph === "‖") return "paused";
  return "idle";
}

export const Dot = ({ tone, className = "" }: { tone: DotTone; className?: string }) => <span className={`side-dot ${className}`} data-tone={tone} aria-hidden />;

/* The agent's state in plain words (C2b: the tree says what the agent is
   doing, never that it needs a person; that is the Inbox's): a worker
   stopped at a prompt is "waiting", one whose sign-in lapsed "stopped". */
export function plainLabel(status: CliStatus, agentStatus: string): string {
  if (status.style !== "alert") return status.label;
  if (agentStatus === "waiting") return "waiting";
  if (agentStatus === "failed") return "stopped";
  return status.label;
}

/* One row the keyboard can land on: a session or an agent, in reading order. */
interface RowRef {
  key: string;
  route: Route;
}

/* One agent in a session's tree, and its children under it: a dot, "id ·
   branch" (the supervisor by its role), its state at the right. */
function AgentRow({ node, depth, route, navigate, repo, root, cursor, setCursor }: { node: AgentNode; depth: number; repo: string; root: string; cursor: string | null; setCursor(key: string): void } & Pick<SidebarProps, "route" | "navigate">) {
  const a = node.agent;
  const alive = useAppState((s) => s.alive[a.id]);
  const active = route.screen === "session" && route.root === root && (route.agent ?? root) === a.id;
  const status = cliStatus(a, node.workspace, alive);
  const key = `agent:${a.id}`;
  const label = a.id === root ? "supervisor" : node.workspace.branch;
  return (
    <li>
      <button
        type="button"
        className={`flex w-full items-center gap-s1 rounded-r-md py-[3px] pl-[10px] pr-s1 text-left hover:bg-surface ${active ? "bg-branch-soft" : ""} ${cursor === key ? CURSOR : ""}`}
        style={{ paddingLeft: `${10 + (depth - 1) * 12}px` }}
        aria-current={active ? "page" : undefined}
        data-tree-agent={a.id}
        data-row={key}
        title={`${a.id} · ${status.glyph} ${status.label} · ${node.workspace.branch}`}
        onClick={() => {
          setCursor(key);
          navigate({ screen: "session", root, repo, agent: a.id });
        }}
      >
        <Dot tone={dotTone(status, a.status)} />
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink">
          {depth > 2 && <span className="text-ink-3">↳ </span>}
          {a.id} · {label}
        </span>
        <span className="max-w-[50%] shrink truncate text-[11px] text-ink-3" data-testid="agent-status">
          {plainLabel(status, a.status)}
        </span>
      </button>
      {node.children.length > 0 && (
        <ul className="flex flex-col gap-px">
          {node.children.map((child) => (
            <AgentRow key={child.agent.id} node={child} depth={depth + 1} route={route} navigate={navigate} repo={repo} root={root} cursor={cursor} setCursor={setCursor} />
          ))}
        </ul>
      )}
    </li>
  );
}

/* Closes a session row's session (POST /sessions/{root}/close), one click
   and nothing asked: the row goes, its branches stay. */
type CloseRow = (root: string) => void;

/* Every session row's close, at its right: a "✕" shown on hover or keyboard
   focus, or, once the session is through (`done`: its goal Done, or no goal
   and nothing at work), "Done · Close" shown without hovering. Closed from
   the keyboard, focus goes back to the list. */
function RowClose({ root, done, onClose, list }: { root: string; done: boolean; onClose: CloseRow; list: RefObject<HTMLElement | null> }) {
  return (
    <button
      type="button"
      className={
        done
          ? "mt-[4px] shrink-0 whitespace-nowrap rounded-full bg-pass-soft px-1.5 py-px text-[10.5px] font-medium text-pass hover:opacity-80"
          : "mt-[4px] w-[18px] shrink-0 rounded-sm text-center text-[12px] leading-none text-ink-3 opacity-0 hover:text-ink focus-visible:opacity-100 group-hover:opacity-100"
      }
      aria-label={done ? `Done: close session ${root}` : `Close session ${root}`}
      title="Close the session: it stops and leaves the list; its branches stay"
      data-testid={done ? `session-done-${root}` : `session-close-${root}`}
      onClick={(e) => {
        if (e.currentTarget === document.activeElement) list.current?.focus();
        onClose(root);
      }}
    >
      {done ? "Done · Close" : "✕"}
    </button>
  );
}

/* What an agent at rest is: through with its turn, stopped, or gone. */
const AT_REST = new Set(["idle", "done", "paused", "exited"]);

/* A row's session's goal (GET /goal): undefined while unknown, null when it has none. */
function useRowGoal(repo: string, root: string) {
  return useGoal(useContext(DaemonContext), repo, root, useStore());
}

/* The goal is reached: Done, whatever the session's process is doing (live or paused). */
const goalDone = (goal: GoalDTO | null | undefined) => !!goal && goalPhase(goal, UNKNOWN).phase === "done";

/* A live session: its goal with its id and mode under it, a needs-you
   count, the caret that folds its agents; the supervisor and its workers
   nested under it. A close on hover or focus, and "Done · Close" in its
   place once the session is through. */
function SessionRows({ session, repo, index, route, navigate, cursor, setCursor, folded, toggle, onClose, list }: { session: SessionDTO; repo: string; index: number; cursor: string | null; setCursor(key: string): void; folded: boolean; toggle(): void; onClose: CloseRow; list: RefObject<HTMLElement | null> } & Pick<SidebarProps, "route" | "navigate">) {
  const root = session.id;
  const workspaces = useAppState((s) => s.workspaces);
  const alive = useAppState((s) => s.alive);
  const { hidden } = useSessions();
  const goal = useRowGoal(repo, root);
  const tree = useMemo(() => sessionTree(workspaces, root, hidden), [workspaces, root, hidden]);
  const members = useMemo(() => agentIds(tree), [tree]);
  const open = route.screen === "session" && route.root === root;
  const key = `session:${root}`;
  const agents = members.size;
  const busy = workspaces.flatMap((ws) => (ws.agents ?? []).filter((a) => members.has(a.id) && cliStatus(a, ws, alive[a.id]).style === "busy")).length;
  // Through: its goal Done, or no goal and every agent in it at rest (the supervisor among them).
  const mine = workspaces.flatMap((ws) => ws.agents ?? []).filter((a) => members.has(a.id));
  const idle = mine.some((a) => a.id === root) && mine.every((a) => AT_REST.has(a.status) || alive[a.id] === false);
  const done = goalDone(goal) || (goal === null && idle);
  return (
    <li className="flex flex-col gap-px" data-testid={`tree-session-${root}`} data-session={root}>
      <div className={`group flex items-start gap-0.5 rounded-md ${open ? "bg-branch-soft" : "hover:bg-surface/60"}`}>
        <button
          type="button"
          className={`flex min-w-0 flex-1 items-start gap-s1 rounded-md px-s1 py-[5px] text-left ${cursor === key ? CURSOR : ""}`}
          data-tree-session={root}
          data-row={key}
          title={`${root} · ${session.status}${index < 9 ? ` · ⌘${index + 1}` : ""}`}
          onClick={() => {
            setCursor(key);
            navigate({ screen: "session", root, repo, agent: root });
          }}
        >
          <Dot tone={busy > 0 ? "work" : "idle"} className="mt-[6px]" />
          <span className="min-w-0 flex-1">
            <span className="line-clamp-2 text-[13px] leading-[1.35] text-ink">{session.name || session.goal || <span className="italic text-ink-3">No goal set</span>}</span>
            <span className="block truncate text-[11.5px] text-ink-3">
              <span className="font-mono text-[11px]">{root}</span> · {session.mode} · {agents} agent{agents === 1 ? "" : "s"}
              {busy > 0 && ` · ${busy} working`}
            </span>
          </span>
        </button>
        <RowClose root={root} done={done} onClose={onClose} list={list} />
        {agents > 0 && (
          <button
            type="button"
            className="mt-[5px] w-[18px] shrink-0 pr-1 text-center text-[11px] leading-none text-ink-3 hover:text-ink"
            aria-label={folded ? `Show ${root}'s agents` : `Hide ${root}'s agents`}
            aria-expanded={!folded}
            onClick={toggle}
          >
            {folded ? "▸" : "▾"}
          </button>
        )}
      </div>
      {folded ? null : tree ? (
        <ul className="mb-1 ml-s1 flex flex-col gap-px border-l border-line">
          <AgentRow node={tree} depth={1} route={route} navigate={navigate} repo={repo} root={root} cursor={cursor} setCursor={setCursor} />
        </ul>
      ) : (
        <p className="pl-s3 text-xs text-ink-3">no agents yet</p>
      )}
    </li>
  );
}

/* A paused session's Resume (POST /sessions/{root}/continue), which opens
   the session once its chat is back; the engine's refusal, or the
   provider's setup items (Preflight), show under the row. */
function useResume(client: DaemonClient, root: string, repo: string, navigate: SidebarProps["navigate"], refresh: () => Promise<void>) {
  const feed = useSessions();
  const reload = useCallback(async () => {
    await Promise.all([feed.reload(), refresh()]).catch(() => undefined);
  }, [feed, refresh]);
  const opened = useCallback(() => navigate({ screen: "session", root, repo, agent: root }), [navigate, root, repo]);
  return useContinue(client, root, reload, opened);
}

function ResumeButton({ client, root, repo, navigate, refresh }: { client: DaemonClient; root: string; repo: string; refresh: () => Promise<void> } & Pick<SidebarProps, "navigate">) {
  const recovery = useResume(client, root, repo, navigate, refresh);
  return (
    <>
      <button
        type="button"
        className="mt-[4px] shrink-0 whitespace-nowrap rounded-full border border-line-strong px-1.5 py-px text-[10.5px] font-medium text-ink-2 hover:text-ink disabled:opacity-50"
        title="Continue the session and open its chat"
        data-testid={`session-resume-${root}`}
        disabled={recovery.busy}
        onClick={() => void recovery.resume()}
      >
        Resume
      </button>
      {(recovery.notice?.failed || recovery.preflight) && (
        <div className="order-last basis-full pl-s3">
          {recovery.notice?.failed && (
            <p role="alert" className="text-xs text-sap">
              {recovery.notice.text}
            </p>
          )}
          {recovery.preflight && <Preflight preflight={recovery.preflight} onRefresh={recovery.setPreflight} onLaunch={() => void recovery.resume()} launchLabel="Resume" repo={repo} />}
        </div>
      )}
    </>
  );
}

/* A paused or failed session, dimmed at the foot of its repo's group: its goal, and how long ago it stopped; Resume; a close on hover or focus, "Done · Close" once its goal is Done. */
function PausedRow({ session, repo, route, navigate, refresh, cursor, setCursor, onClose, list }: { session: SessionDTO; repo: string; refresh: () => Promise<void>; cursor: string | null; setCursor(key: string): void; onClose: CloseRow; list: RefObject<HTMLElement | null> } & Pick<SidebarProps, "route" | "navigate">) {
  const root = session.id;
  const client = useContext(DaemonContext);
  const open = route.screen === "session" && route.root === root;
  const key = `session:${root}`;
  const age = formatAge(Date.now() / 1000 - (session.status_since ?? session.created_at));
  const done = goalDone(useRowGoal(repo, root));
  return (
    <li className="group flex flex-wrap items-start gap-0.5 pr-[18px]">
      <button
        type="button"
        className={`flex min-w-0 flex-1 items-start gap-s1 rounded-md px-s1 py-[5px] text-left opacity-70 hover:opacity-100 ${open ? "bg-branch-soft opacity-100" : ""} ${cursor === key ? CURSOR : ""}`}
        data-tree-session={root}
        data-row={key}
        title={`${root} · ${session.status}`}
        onClick={() => {
          setCursor(key);
          navigate({ screen: "session", root, repo, agent: root });
        }}
      >
        <Dot tone={session.status === "failed" ? "fail" : "paused"} className="mt-[6px]" />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] leading-[1.35] text-ink-3">{session.name || session.goal || <span className="italic">No goal set</span>}</span>
          <span className="block truncate text-[11.5px] text-ink-3">
            <span className="font-mono text-[11px]">{root}</span> · {session.status}
            {age !== "just now" && ` ${age}`}
          </span>
        </span>
      </button>
      {client && <ResumeButton client={client} root={root} repo={repo} navigate={navigate} refresh={refresh} />}
      <RowClose root={root} done={done} onClose={onClose} list={list} />
    </li>
  );
}

/* "Inbox · N" at the very top (design/options-v3/C2b-inbox-only): the one
   entry about what needs a person, everything else about attention being
   the Inbox page's. "empty" when nothing waits. */
function InboxEntry({ route, navigate }: Pick<SidebarProps, "route" | "navigate">) {
  const total = useAppState((s) => s.inbox.length);
  const on = route.screen === "inbox";
  return (
    <button
      type="button"
      className={`mx-1 flex items-center gap-s1 rounded-[7px] border bg-surface px-[10px] py-[7px] text-left text-[12.5px] ${on ? "border-branch" : "border-line hover:border-line-strong"} ${total > 0 ? "font-semibold text-ink" : "font-medium text-ink-3"}`}
      aria-current={on ? "page" : undefined}
      data-testid="inbox-link"
      onClick={() => navigate({ screen: "inbox" })}
    >
      <span className="flex-1">Inbox</span>
      {total > 0 ? (
        <span className="side-nb" data-testid="inbox-badge" aria-label={`${total} needing you`}>
          {total}
        </span>
      ) : (
        <span className="font-mono text-[11.5px] font-medium text-ink-3">empty</span>
      )}
    </button>
  );
}

/* The handle between the context and the tree: drags with the mouse, and
   moves with ↑ ↓ for the keyboard. */
function Handle({ height, onChange }: { height: number; onChange(next: number): void }) {
  const [dragging, setDragging] = useState(false);
  const start = useRef<{ y: number; h: number } | null>(null);
  const down = (e: ReactMouseEvent) => {
    start.current = { y: e.clientY, h: height };
    setDragging(true);
    e.preventDefault();
  };
  useEffect(() => {
    if (!dragging) return;
    const move = (e: MouseEvent) => {
      if (start.current) onChange(clampSplit(start.current.h + e.clientY - start.current.y));
    };
    const up = () => {
      start.current = null;
      setDragging(false);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, [dragging, onChange]);
  const keys = (e: KeyboardEvent) => {
    if (e.key === "ArrowUp") onChange(clampSplit(height - 24));
    else if (e.key === "ArrowDown") onChange(clampSplit(height + 24));
    else return;
    e.preventDefault();
  };
  return (
    <div
      role="separator"
      aria-orientation="horizontal"
      aria-label="Resize the session's context"
      aria-valuenow={Math.round(height)}
      aria-valuemin={SPLIT_MIN}
      tabIndex={0}
      className="side-handle relative flex h-3.5 shrink-0 items-center justify-center outline-none before:absolute before:inset-x-2 before:top-1/2 before:h-px before:bg-line"
      data-testid="split-handle"
      data-dragging={dragging}
      onMouseDown={down}
      onKeyDown={keys}
    >
      <i className="relative z-10" />
    </div>
  );
}

export function Sidebar({ route, navigate, refresh, openUrl }: SidebarProps) {
  const { repos, sessions, error: feedError, closeError, close, after } = useSessions();
  const [cursor, setCursor] = useState<string | null>(null);
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [split, setSplit] = useState(readSplit);
  const [pausedOpen, setPausedOpen] = useState(readPausedOpen);
  const list = useRef<HTMLElement>(null);
  const noRefresh = useCallback(() => Promise.resolve(), []);
  const reading = useNearReading();
  const { rows: providerRows, installs } = useProviders();
  const problems = useMemo(() => providerProblems(providerRows, installs), [providerRows, installs]);

  // The live sessions in reading order (⌘1-9), and the paused ones after them.
  const live = useMemo(() => (repos ?? []).flatMap((repo) => (sessions[repo.repo_root] ?? []).filter((s) => !isPaused(s)).map((session) => ({ session, repo: repo.repo_root }))), [repos, sessions]);
  const paused = useMemo(() => (repos ?? []).flatMap((repo) => (sessions[repo.repo_root] ?? []).filter(isPaused).map((session) => ({ session, repo: repo.repo_root }))), [repos, sessions]);
  const all = useMemo(() => [...live, ...paused], [live, paused]);

  // The context pane's session: the one on screen, else the one kept from last time, while it is listed.
  const selected = useMemo<SessionRef | null>(() => {
    const here = routeSession(route);
    const listed = (ref: SessionRef | null) => (ref && all.some((r) => r.session.id === ref.root && r.repo === ref.repo) ? ref : null);
    return listed(here) ?? listed(readLastSession()) ?? (all[0] ? { root: all[0].session.id, repo: all[0].repo } : null);
  }, [route, all]);

  // An open session's agents show.
  useEffect(() => {
    if (route.screen === "session") setFolded((f) => (f.has(route.root) ? new Set([...f].filter((id) => id !== route.root)) : f));
  }, [route]);

  const resize = useCallback((next: number) => {
    setSplit(next);
    writeSplit(next);
  }, []);

  // A row's close: the row goes at once; the view moves on only when that session was on screen.
  const closeRow = useCallback(
    (root: string) => {
      if (route.screen === "session" && route.root === root) navigate(after(root));
      void close(root);
    },
    [route, navigate, after, close],
  );

  const togglePaused = useCallback((repo: string) => {
    setPausedOpen((open) => {
      const next = new Set(open);
      if (next.has(repo)) next.delete(repo);
      else next.add(repo);
      writePausedOpen(next);
      return next;
    });
  }, []);

  /* The rows as drawn, in order: what ↑ ↓ move through. */
  const rows = useCallback((): RowRef[] => {
    const out: RowRef[] = [];
    for (const el of list.current?.querySelectorAll<HTMLButtonElement>("button[data-row]") ?? []) {
      const key = el.dataset.row!;
      const root = el.dataset.treeSession ?? el.closest<HTMLElement>("[data-session]")?.dataset.session ?? "";
      const repo = all.find((r) => r.session.id === root)?.repo ?? "";
      out.push({ key, route: { screen: "session", root, repo, agent: el.dataset.treeAgent ?? root } });
    }
    return out;
  }, [all]);

  // The keyboard's row follows the route: the row on screen is where keys start.
  useEffect(() => {
    if (route.screen === "session") setCursor(route.agent && route.agent !== route.root ? `agent:${route.agent}` : `session:${route.root}`);
  }, [route]);

  const keys = (e: KeyboardEvent<HTMLElement>) => {
    const target = e.target as HTMLElement;
    if (target !== e.currentTarget && target.closest("button, a, input, textarea, select")) return;
    const rowsNow = rows();
    if (rowsNow.length === 0) return;
    const at = rowsNow.findIndex((r) => r.key === cursor);
    switch (e.key) {
      case "ArrowDown":
      case "j":
        setCursor(rowsNow[Math.min(at + 1, rowsNow.length - 1)].key);
        break;
      case "ArrowUp":
      case "k":
        setCursor(rowsNow[Math.max(at - 1, 0)].key);
        break;
      case "Enter": {
        const row = rowsNow[at] ?? rowsNow[0];
        if (row) navigate(row.route);
        break;
      }
      default:
        return;
    }
    e.preventDefault();
  };

  // ⌘1-9 (Ctrl on Linux and Windows) jump to a live session; ⌘N starts one.
  useEffect(() => {
    const jump = (e: globalThis.KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      if (e.key === "n") {
        e.preventDefault();
        navigate({ screen: "new" });
        return;
      }
      const n = Number(e.key);
      if (!Number.isInteger(n) || n < 1 || n > 9) return;
      const target = live[n - 1];
      if (!target) return;
      e.preventDefault();
      navigate({ screen: "session", root: target.session.id, repo: target.repo, agent: target.session.id });
    };
    window.addEventListener("keydown", jump);
    return () => window.removeEventListener("keydown", jump);
  }, [live, navigate]);

  const page = (to: Route, label: string) => (
    <button type="button" className={`${FOOT_LINK} ${sameRoute(route, to) ? "font-medium text-ink" : ""}`} aria-current={sameRoute(route, to) ? "page" : undefined} onClick={() => navigate(to)}>
      {label}
    </button>
  );

  let index = 0;
  return (
    <aside className="side flex w-[352px] shrink-0 flex-col gap-[10px] border-r border-line bg-canvas-2px-[12px] pb-[12px] pt-[14px] text-[13px] text-ink" aria-label="Sidebar">
      <div className="flex items-center gap-s1 px-1.5 pb-1">
        <Logo className="text-[17px]" />
        <span className="flex-1" />
        <button
          type="button"
          className={`flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11.5px] text-ink-3 hover:bg-surface hover:text-ink ${route.screen === "new" ? "bg-branch-soft text-ink" : ""}`}
          aria-current={route.screen === "new" ? "page" : undefined}
          title="New session (⌘N)"
          onClick={() => navigate({ screen: "new" })}
        >
          <span className="text-sm leading-none" aria-hidden>
            +
          </span>
          New session
        </button>
      </div>
      <InboxEntry route={route} navigate={navigate} />
      <SessionContext session={selected} route={route} navigate={navigate} refresh={refresh ?? noRefresh} openUrl={openUrl} style={{ height: split }} />
      <Handle height={split} onChange={resize} />
      <div className="side-h">Sessions</div>
      <nav ref={list} aria-label="Sessions" className="flex min-h-0 flex-1 flex-col gap-[14px] overflow-auto outline-none focus-visible:ring-1 focus-visible:ring-branch" tabIndex={0} onKeyDown={keys}>
        {/* A close the engine refused: the session is back in its place, and this says why. */}
        {closeError && (
          <p className="px-s1 text-xs text-sap" data-testid="close-error">
            {closeError}
          </p>
        )}
        {repos === null ? (
          <p className={`px-s1 text-xs ${feedError ? "text-sap" : "text-ink-3"}`}>{feedError ? `Couldn't load the sessions: ${feedError}` : "Loading…"}</p>
        ) : repos.length === 0 ? (
          <p className="px-s1 text-xs text-ink-3">No repo yet. Run brindle once in a git repository.</p>
        ) : (
          repos.map((repo) => {
            const here = live.filter((r) => r.repo === repo.repo_root);
            const rested = paused.filter((r) => r.repo === repo.repo_root);
            if (here.length === 0 && rested.length === 0) return null;
            return (
              <div key={repo.repo_root} className="flex flex-col" data-testid={`repo-${repo.name}`}>
                <div className={`${REPO_HEADING} truncate`} title={repo.repo_root}>
                  {repo.name}
                </div>
                <ul className="flex flex-col gap-px">
                  {here.map(({ session }) => (
                    <SessionRows
                      key={session.id}
                      session={session}
                      repo={repo.repo_root}
                      index={index++}
                      route={route}
                      navigate={navigate}
                      cursor={cursor}
                      setCursor={setCursor}
                      folded={folded.has(session.id)}
                      toggle={() => setFolded((f) => (f.has(session.id) ? new Set([...f].filter((id) => id !== session.id)) : new Set([...f, session.id])))}
                      onClose={closeRow}
                      list={list}
                    />
                  ))}
                  {rested.length > 0 && (
                    <li>
                      <button
                        type="button"
                        className="flex w-full items-center gap-s1 rounded-md px-s1 py-[5px] text-left text-[11.5px] text-ink-3 hover:bg-surface/60 hover:text-ink"
                        aria-expanded={pausedOpen.has(repo.repo_root)}
                        data-testid={`paused-toggle-${repo.name}`}
                        onClick={() => togglePaused(repo.repo_root)}
                      >
                        <span className="w-[18px] shrink-0 text-center text-[11px] leading-none" aria-hidden>
                          {pausedOpen.has(repo.repo_root) ? "▾" : "▸"}
                        </span>
                        Paused · {rested.length}
                      </button>
                    </li>
                  )}
                  {/* Collapsed, only the selected paused session stays listed under the row. */}
                  {rested
                    .filter(({ session }) => pausedOpen.has(repo.repo_root) || session.id === selected?.root)
                    .map(({ session }) => (
                      <PausedRow key={session.id} session={session} repo={repo.repo_root} route={route} navigate={navigate} refresh={refresh ?? noRefresh} cursor={cursor} setCursor={setCursor} onClose={closeRow} list={list} />
                    ))}
                </ul>
              </div>
            );
          })
        )}
        {repos && repos.length > 0 && live.length === 0 && <p className="px-s1 text-xs text-ink-3">Nothing running. Start a session, or continue a recent one.</p>}
      </nav>
      <nav aria-label="Pages" className="flex flex-nowrap items-center gap-x-[10px] whitespace-nowrap border-t border-line px-s1 pt-s1" data-testid="pages">
        {page({ screen: "knowledge" }, "Knowledge")}
        {page({ screen: "agents" }, "Agents")}
        {page({ screen: "settings" }, "Settings")}
        {page({ screen: "account" }, "Account")}
      </nav>
      <div className="flex items-center justify-between gap-[10px] whitespace-nowrap px-s1 pt-0.5" data-testid="status-line">
        {(problems.length > 0 || reading) && (
          <span className="ml-auto flex min-w-0 flex-[0_1_auto] items-baseline gap-[10px]">
            <ProviderProblemLine problems={problems} navigate={navigate} />
            <UsageReading reading={reading} navigate={navigate} />
          </span>
        )}
        <TerminalToggle />
        <ThemeToggle className="flex flex-none items-center gap-1 rounded-md text-[11.5px] lowercase text-ink-3 hover:text-ink" />
      </div>
    </aside>
  );
}
