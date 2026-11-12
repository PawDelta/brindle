/* The merged sidebar (design/options-v2/M3b-multi's layout, with
   design/options-v3/C2b-inbox-only's one entry for what needs a person):
   brindle's whole layer in one column beside the provider's session. From
   the top: the logo (and New session); "Inbox · N", the one place anything
   about attention shows in the sidebar (the Inbox page, in the main area,
   is where it is read and answered); the selected session's context
   (SessionContext: its goal with milestones, progress and checks, and the
   one lifecycle that carries its PR step); a handle that drags the split between them (kept in
   localStorage); then "Sessions": every open session grouped by repo, each
   row its short goal, model and status word, a root dot that folds its
   workers, and a close ("✕", on hover or focus; "Done · Close", always
   there, once its goal is Done or, with no goal, all in it is at rest)
   that closes the session in one click, nothing asked, the row just
   going; its workers connected by tree lines in compact rows (the mockup's dot vocabulary
   for the CLI's statuses: branch at work, sap waiting, pass done, quiet
   idle, fail red), each "id · branch" with its state in plain words at the
   right, never a count or a needs-you mark; paused and failed sessions
   out of the list, into one collapsed "Paused · N" row at the foot of their
   repo's group (open per viewer, kept in localStorage; the selected paused
   session stays listed under it). Clicking a session opens its
   provider session on the supervisor; clicking an agent opens the session
   on that agent. Keys, as in the CLI: ↑ ↓ (or j k) move, Enter opens, ⌘1-9
   jump to a session, ⌘N starts one. At the foot, the profile pill combines
   initials and usage and opens Account › Usage; the terminal switch sits
   beside it. Updates live in the Inbox and Settings › About. There is no right-hand panel: this is
   where brindle's layer lives. The site's surfaces, purple (branch) only
   as the accent (styles.css .side-*). */

import { useCallback, useContext, useEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent as ReactMouseEvent, type RefObject } from "react";
import type { DaemonClient, ProviderUsage } from "../api/client";
import { DaemonContext } from "../api/context";
import { openExternal, type OpenUrl } from "../api/open";
import type { AgentDTO, GoalDTO, SessionDTO } from "../api/schema";
import { inProfileRail, railPage, readLastSession, routeSession, settingsTab, type Route, type SessionRef } from "../routes";
import { AccountAvatar, SettingsRail } from "./SettingsRail";
import { pruneShown, seenKey } from "../screens/Inbox/items";
import { goalPhase, SessionGoal, useGoal } from "../screens/Session/GoalPanel";
import { UNKNOWN } from "../screens/Session/review";
import { agentIds, sessionTree, type AgentNode } from "../screens/Session/tree";
import { useContinue } from "../screens/Session/useContinue";
import { waitingCount } from "../store/appUpdates";
import { cliStatus, formatAge, isPastWorker, isPaused, type CliStatus } from "../store/overview";
import { planWindows, providerName, WARN } from "../screens/Account/usage";
import { useSessions } from "../store/sessions";
import { useUsage } from "../store/usage";
import { useAppState, useStore } from "../store/useStore";
import Logo from "./Logo";
import { Preflight } from "./Preflight";
import { SessionContext } from "./SessionContext";
import { TerminalToggle } from "./TerminalPanel";

export interface SidebarProps {
  onBackFromSettings?(): void;
  route: Route;
  navigate(route: Route): void;
  /* Fetch the snapshot and inbox again (Sync.refresh), after a decision made here. */
  refresh?: () => Promise<void>;
  /* Opens a session's PR page in the system browser (the goal's PR open step). */
  openUrl?: OpenUrl;
}

const weeklyWindow = (provider: ProviderUsage) => (provider.known ? planWindows(provider).find((window) => window.minutes === 10080) : undefined);

/* The reset day only matters once the weekly window is close to full. */
const RESET_SHOWN_FROM = 80;

/* Each click is its own request, so a second one with Account open scrolls back to Usage. */
let usageAsks = 0;
/* No reading is not a fault: a new user has none yet, and the first load may still be out.
   Only a failed load says "unavailable"; otherwise a neutral "Usage" opens the same page. */
function UsageReading({ provider, warning, failed }: { provider?: ProviderUsage; warning: boolean; failed: boolean }) {
  const window = provider && weeklyWindow(provider);
  return (
    <span
      className={`min-w-0 truncate font-mono text-[11.5px] ${warning ? "text-sap" : "text-ink-3"}`}
      data-testid="profile-usage"
      data-provider={window ? provider?.provider : undefined}
      data-state={window ? "reading" : failed ? "failed" : "none"}
    >
      {provider && window ? (
        <>
          <span>{providerName(provider.provider)}</span>
          {" "}{Math.round(window.used)}%
          {window?.resets_at != null && window.used >= RESET_SHOWN_FROM && <> · {new Date(window.resets_at * 1000).toLocaleDateString("en-US", { weekday: "short" })}</>}
        </>
      ) : failed ? "Usage unavailable" : "Usage"}
    </span>
  );
}

function AccountButton({ route, navigate, provider, warning, failed }: Pick<SidebarProps, "route" | "navigate"> & { provider?: ProviderUsage; warning: boolean; failed: boolean }) {
  const client = useContext(DaemonContext);
  const store = useStore();
  const connection = useAppState((s) => s.connection);
  const account = useAppState((s) => s.account);
  useEffect(() => {
    let current = true;
    const previous = store.getState().account;
    if (client && (connection === "open" || connection === "idle")) {
      void client.account().then(
        (next) => {
          // An Account-page reload may have superseded this initial read.
          if (current && store.getState().account === previous) store.dispatch({ type: "account", account: next });
        },
        () => undefined,
      );
    }
    return () => { current = false; };
  }, [client, connection, store]);
  const name = account?.signed_in ? account.name?.trim() : "";
  const email = account?.signed_in ? account.email?.trim() : "";
  return (
    <button
      type="button"
      aria-label="Open Account › Usage"
      data-testid="profile-pill"
      data-walkthrough="profile"
      aria-current={settingsTab(route) ? "page" : undefined}
      title={account?.signed_in ? name || email || "Signed in" : "Sign in"}
      className="relative flex min-w-0 items-center gap-2 rounded-full border border-line py-0.5 pl-0.5 pr-3 hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-branch"
      onClick={() => navigate({ screen: "account", section: "usage", ask: ++usageAsks })}
    >
      <AccountAvatar />
      {warning && <span aria-hidden="true" data-testid="usage-warning-dot" className="absolute left-6 top-0 size-1.5 rounded-full bg-sap ring-2 ring-canvas-2" />}
      <UsageReading provider={provider} warning={warning} failed={failed} />
    </button>
  );
}

/* The mockup's rows: the keyboard's cursor (a tone, not a box; the branch
   ring comes back while the list has keyboard focus), the repo heading, the
   foot's links. */
const CURSOR = "bg-surface-2 in-focus-visible:ring-1 in-focus-visible:ring-inset in-focus-visible:ring-branch";
const REPO_HEADING = "px-s1 pb-1 text-[10.5px] font-semibold uppercase tracking-[0.08em] text-ink-3";

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

/* A profile's role: developer-* and reviewer-* are those roles whatever model
   or tier they run on; any other name keeps its words, minus a model or tier
   at its end (backend-auditor, qa, test-writer stay as they are). */
const PROFILE_VARIANT = /^(sonnet|opus|haiku|fable|codex|gemini|flash|pro|opencode|local|light|heavy|low|medium|high)$/;

export function roleOf(profile: string): string {
  const family = /^(developer|reviewer)(-|$)/.exec(profile);
  if (family) return family[1];
  const words = profile.split("-");
  while (words.length > 1 && PROFILE_VARIANT.test(words[words.length - 1])) words.pop();
  return words.join("-");
}

/* The model an agent runs, in a word: what its transcript last ran on, else the variant its profile names. */
const MODEL_WORD = /^(sonnet|opus|haiku|fable|codex|gemini|flash|opencode|local)$/;

function modelOf(a: { model?: string | null; profile: string; provider: string }): string {
  // Codex and Gemini name their models by version (gpt-6-luna), which says less than the CLI; Claude's family word says it.
  if (a.model) return /^(codex|gemini)$/.test(a.provider) ? a.provider : /(sonnet|opus|haiku|fable)/.exec(a.model)?.[1] ?? a.model;
  // No transcript yet: a model word in the profile name; a routing tier (heavy, low…) is not a model, so the provider stands in.
  const word = a.profile.split("-").find((w, i) => i > 0 && MODEL_WORD.test(w));
  return word ?? (a.profile === a.provider ? "" : a.provider);
}

/* The stage word and colour a row shares with the milestones view; none when at rest. */
type RowStage = "code" | "review" | "done";
const STAGE_CLASS: Record<RowStage, string> = { code: "working", review: "review", done: "done" };

function stageOf(tone: DotTone, a: { mode: string }, reviewState?: string | null): RowStage | null {
  if (tone === "done") return "done";
  if (a.mode === "review") return tone === "work" ? "review" : null;
  if (reviewState?.startsWith("reviewer")) return "review";
  if (reviewState?.startsWith("checks") || reviewState?.startsWith("waiting for checks")) return "code";
  return tone === "work" ? "code" : null;
}

/* One agent in a session's tree, and its children under it: a dot, its role
   in words, its stage at the right. The tooltip carries id, role, profile,
   model, branch and milestone. Double-clicked, it copies its info. */
function AgentRow({ node, route, navigate, repo, root, cursor, setCursor }: { node: AgentNode; repo: string; root: string; cursor: string | null; setCursor(key: string): void } & Pick<SidebarProps, "route" | "navigate">) {
  const a = node.agent;
  const alive = useAppState((s) => s.alive[a.id]);
  const active = route.screen === "session" && route.root === root && (route.agent ?? root) === a.id;
  const status = cliStatus(a, node.workspace, alive);
  const key = `agent:${a.id}`;
  const role = a.id === root ? "supervisor" : a.profile
    ? a.profile === a.provider ? (a.mode === "review" ? "reviewer" : "developer") : roleOf(a.profile)
    : a.mode === "review" ? "reviewer" : "developer";
  const tone = dotTone(status, a.status);
  const stage = stageOf(tone, a, node.workspace.review_state);
  const info = [`id: ${a.id}`, `role: ${role}`, `profile: ${a.profile}`, `branch: ${node.workspace.branch}`, `milestone: ${a.milestone ?? ""}`].join("\n");
  return (
    <li className="session-tree-node">
      <SessionGoal
        text={info}
        noun="Session info"
        enterCopies={false}
        current={active ? "page" : undefined}
        treeAgent={a.id}
        row={key}
        title={`${a.id} · ${status.glyph} ${status.label} · ${role} · ${a.profile} · ${a.model || modelOf(a) || "no model"} · ${node.workspace.branch}${a.milestone ? ` · milestone: ${a.milestone}` : ""} (double-click to copy its info)`}
        className={`flex w-full items-center gap-s1 rounded-r-md py-[3px] pr-s1 text-left hover:bg-surface ${active ? "bg-branch-soft" : ""} ${cursor === key ? CURSOR : ""}`}
        onOpen={() => {
          setCursor(key);
          navigate({ screen: "session", root, repo, agent: a.id });
        }}
      >
        <Dot tone={tone} className={stage ? `st-${STAGE_CLASS[stage]}` : ""} />
        <span className="min-w-0 flex-1 truncate text-xs text-ink">
          <span className="text-ink-3" data-testid="agent-role">{[role, modelOf(a)].filter(Boolean).join(" · ")}</span>
        </span>
        <span className={`max-w-[50%] shrink truncate text-[11px] ${stage ? `st-${STAGE_CLASS[stage]} stage-word` : "text-ink-3"}`} data-testid="agent-status">
          {stage ?? plainLabel(status, a.status)}
        </span>
      </SessionGoal>
      {node.children.length > 0 && (
        <ul className="session-tree">
          {node.children.map((child) => (
            <AgentRow key={child.agent.id} node={child} route={route} navigate={navigate} repo={repo} root={root} cursor={cursor} setCursor={setCursor} />
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
          ? "mt-[4px] shrink-0 whitespace-nowrap rounded-sm px-1.5 py-px text-[10.5px] font-medium text-ink-2 hover:text-ink"
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

function sessionWorking(session: SessionDTO, agents: AgentDTO[], alive: Record<string, boolean>) {
  const busy = (status: string) => status === "processing" || status === "starting";
  return (alive[session.id] !== false && busy(session.status))
    || agents.some((a) => alive[a.id] !== false && busy(a.status));
}

/* A row's session's goal (GET /goal): undefined while unknown, null when it has none. */
function useRowGoal(repo: string, root: string) {
  return useGoal(useContext(DaemonContext), repo, root, useStore());
}

/* The goal is reached: Done, whatever the session's process is doing (live or paused). */
const goalDone = (goal: GoalDTO | null | undefined) => !!goal && goalPhase(goal, UNKNOWN).phase === "done";

/* The supervisor's live activity, even when its workers are folded away.
   A finished goal wins; a paused process cannot still be doing its last stage. */
function sessionStatusWord(session: SessionDTO, goal: GoalDTO | null | undefined) {
  if (session.status === "resuming") return "Resuming";
  if (goalDone(goal)) return "Done";
  if (session.status === "failed") return "Failed";
  if (isPaused(session)) return "Paused";
  switch (goal?.stage) {
    case "working":
    case "checks": return "Working";
    case "review": return "Reviewing";
    case "merging": return "Merging";
  }
  return session.status === "processing" || session.status === "starting" ? "Working" : "Idle";
}

function sessionStageClass(word: string) {
  switch (word) {
    case "Working": return "st-working";
    case "Reviewing": return "st-review";
    case "Merging": return "st-merging";
    case "Done": return "st-done";
    default: return "text-ink-3";
  }
}

function SessionStatus({ word }: { word: string }) {
  const stageClass = sessionStageClass(word);
  return (
    <span className={`max-w-[50%] shrink truncate text-[11px] ${stageClass}${stageClass === "text-ink-3" ? "" : " stage-word"}`} data-testid="session-status" data-state={word.toLowerCase()}>
      {word}
    </span>
  );
}

const PR_LINE: Record<string, { word: string; tone: string }> = {
  ci: { word: "In CI", tone: "text-sap" },
  ready: { word: "Ready to merge", tone: "text-pass" },
  failed: { word: "CI failed", tone: "text-fail" },
  fixing: { word: "Fixing CI", tone: "text-sap" },
  pushing: { word: "Fix ready, pushing", tone: "text-sap" },
};

/* A session with an open PR: a small line under its goal, "PR #391 · In CI, 14 of 18". Unknown
   (or unread) state is just "PR #391", never a guess. Its own button: it opens the PR, not the session. */
function PrLine({ goal, working = false, openUrl }: { goal: GoalDTO | null | undefined; working?: boolean; openUrl: OpenUrl }) {
  const pr = goal?.pr_state;
  if (!pr || !goal?.pr_url) return null;
  const url = goal.pr_url;
  const state = pr.state === "failed" ? ((pr.unpushed ?? 0) > 0 ? "pushing" : working ? "fixing" : "failed") : pr.state;
  const known = PR_LINE[state];
  const text = known ? `PR #${pr.number} · ${known.word}${pr.state === "ci" && pr.total ? `, ${pr.done} of ${pr.total}` : ""}` : `PR #${pr.number}`;
  return (
    <button
      type="button"
      className={`block max-w-full truncate py-px pb-[5px] pl-[34px] pr-s1 text-left text-[11.5px] hover:underline ${known?.tone ?? "text-ink-3"}`}
      data-testid="session-pr"
      data-state={state}
      title={`${pr.state === "failed" && pr.failed_checks?.length ? `Failed: ${pr.failed_checks.join(", ")} · ` : ""}Open ${url}`}
      onClick={() => void openUrl(url).catch(() => undefined)}
    >
      {state === "fixing" && <span aria-hidden="true" data-pr-pulse className="mr-1 inline-block size-1.5 rounded-full bg-current motion-safe:animate-pulse" />}{text}
    </button>
  );
}

/* The session row is its supervisor; workers branch directly from it. */
function SessionRows({ session, repo, index, route, navigate, cursor, setCursor, folded, toggle, onClose, list, openUrl }: { session: SessionDTO; repo: string; index: number; cursor: string | null; setCursor(key: string): void; folded: boolean; toggle(): void; onClose: CloseRow; list: RefObject<HTMLElement | null>; openUrl: OpenUrl } & Pick<SidebarProps, "route" | "navigate">) {
  const root = session.id;
  const workspaces = useAppState((s) => s.workspaces);
  const alive = useAppState((s) => s.alive);
  const { hidden } = useSessions();
  const goal = useRowGoal(repo, root);
  const word = sessionStatusWord(session, goal);
  const tree = useMemo(() => sessionTree(workspaces, root, hidden), [workspaces, root, hidden]);
  const members = useMemo(() => agentIds(tree), [tree]);
  const open = route.screen === "session" && route.root === root;
  const key = `session:${root}`;
  // Through: its goal Done, or no goal and every agent in it at rest (the supervisor among them).
  const mine = workspaces.flatMap((ws) => ws.agents ?? []).filter((a) => members.has(a.id));
  const idle = mine.some((a) => a.id === root) && mine.every((a) => AT_REST.has(a.status) || alive[a.id] === false);
  const working = sessionWorking(session, mine, alive);
  const done = goalDone(goal) || (goal === null && idle);
  return (
    <li className="flex flex-col gap-px" data-testid={`tree-session-${root}`} data-session={root}>
      <div className={`session-root-row group flex items-center rounded-md ${open ? "bg-branch-soft" : "hover:bg-surface/60"}`} data-connected={!folded && !!tree?.children.length}>
        <button
          type="button"
          className={`session-root-dot ${sessionStageClass(word)}`}
          aria-label={folded ? `Show ${root}'s agents` : `Hide ${root}'s agents`}
          aria-expanded={!folded}
          onClick={toggle}
        />
        <SessionGoal
          text={goal?.goal || session.goal}
          enterCopies={false}
          treeSession={root}
          row={key}
          current={open && (route.agent ?? root) === root ? "page" : undefined}
          className={`flex w-full min-w-0 items-center gap-s1 rounded-md py-[5px] pr-s1 text-left ${cursor === key ? CURSOR : ""}`}
          title={`${root} · ${session.status}${index < 9 ? ` · ⌘${index + 1}` : ""}`}
          onOpen={() => {
            setCursor(key);
            navigate({ screen: "session", root, repo, agent: root });
          }}
        >
          <span data-testid="session-title" className="min-w-0 flex-1 truncate text-[13px] font-medium leading-[1.35] text-ink">{session.name || goal?.summary || session.goal || goal?.goal || <span className="italic text-ink-3">No goal set</span>}</span>
          <span className="shrink-0 text-[11px] text-ink-3" data-testid="session-model">{tree ? modelOf(tree.agent) : modelOf({ profile: session.profile, provider: "", model: null })}</span>
          <SessionStatus word={word} />
        </SessionGoal>
        <RowClose root={root} done={done} onClose={onClose} list={list} />
      </div>
      <PrLine goal={goal} working={working} openUrl={openUrl} />
      {folded ? null : tree ? (
        <ul className="session-tree mb-1">
          {tree.children.map((child) => <AgentRow key={child.agent.id} node={child} route={route} navigate={navigate} repo={repo} root={root} cursor={cursor} setCursor={setCursor} />)}
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
        className="mt-[4px] shrink-0 whitespace-nowrap rounded-sm border border-line-strong px-1.5 py-px text-[10.5px] font-medium text-ink-2 hover:text-ink disabled:opacity-50"
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
function PausedRow({ session, repo, route, navigate, refresh, cursor, setCursor, onClose, list, openUrl }: { session: SessionDTO; repo: string; refresh: () => Promise<void>; cursor: string | null; setCursor(key: string): void; onClose: CloseRow; list: RefObject<HTMLElement | null>; openUrl: OpenUrl } & Pick<SidebarProps, "route" | "navigate">) {
  const root = session.id;
  const workspaces = useAppState((s) => s.workspaces);
  const alive = useAppState((s) => s.alive);
  const { hidden } = useSessions();
  const members = agentIds(sessionTree(workspaces, root, hidden));
  const mine = workspaces.flatMap((ws) => ws.agents ?? []).filter((a) => members.has(a.id));
  const client = useContext(DaemonContext);
  const open = route.screen === "session" && route.root === root;
  const key = `session:${root}`;
  const age = formatAge(Date.now() / 1000 - (session.status_since ?? session.created_at));
  const goal = useRowGoal(repo, root);
  const done = goalDone(goal);
  const word = sessionStatusWord(session, goal);
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
        <Dot tone="paused" className={`mt-[6px] ${sessionStageClass(word)}`} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] leading-[1.35] text-ink-3">{session.name || session.goal || <span className="italic">No goal set</span>}</span>
          <span className="block truncate text-[11.5px] text-ink-3">
            <span className="font-mono text-[11px]">{root}</span> · {session.status}
            {age !== "just now" && ` ${age}`}
          </span>
        </span>
        <SessionStatus word={word} />
      </button>
      {client && <ResumeButton client={client} root={root} repo={repo} navigate={navigate} refresh={refresh} />}
      <RowClose root={root} done={done} onClose={onClose} list={list} />
      <div className="basis-full"><PrLine goal={goal} working={sessionWorking(session, mine, alive)} openUrl={openUrl} /></div>
    </li>
  );
}

/* "Inbox · N" at the very top (design/options-v3/C2b-inbox-only): the one
   entry about what needs a person, everything else about attention being
   the Inbox page's. "empty" when nothing waits. The app's own update item
   counts too (store/appUpdates.ts). */
function InboxEntry({ route, navigate }: Pick<SidebarProps, "route" | "navigate">) {
  const total = useAppState(waitingCount);
  const on = route.screen === "inbox";
  // A count that rose since last drawn: the pill glows once (a new key restarts the animation).
  // The sidebar is always there: an item that leaves the inbox while the page is closed is new when it returns.
  const inbox = useAppState((s) => s.inbox);
  const synced = useAppState((s) => s.synced);
  useEffect(() => {
    if (synced) pruneShown(inbox.map(seenKey));
  }, [inbox, synced]);
  const last = useRef(total);
  const [pulse, setPulse] = useState(0);
  useEffect(() => {
    if (total > last.current) setPulse((p) => p + 1);
    last.current = total;
  }, [total]);
  return (
    <button
      type="button"
      className={`mx-1 flex items-center gap-s1 rounded-md border border-transparent py-[7px] pl-2 pr-[10px] text-left text-[12.5px] ${on ? "bg-surface-2" : "hover:bg-surface-2"} ${total > 0 ? "font-semibold text-ink" : "font-medium text-ink-3"}`}
      aria-current={on ? "page" : undefined}
      data-testid="inbox-link"
      data-walkthrough="inbox"
      onClick={() => navigate({ screen: "inbox" })}
    >
      <span className="flex-1">Inbox</span>
      {total > 0 ? (
        <span key={pulse} className="side-nb" data-pulse={pulse > 0 || undefined} data-testid="inbox-badge" aria-label={`${total} needing you`}>
          {total}
        </span>
      ) : null}
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

/* Two session routes that differ only in the sheet over the chat: the same session, repo and
   selected agent (none selected is the root, as the session screen reads it), another sheet. */
const sheetOnly = (a: Extract<Route, { screen: "session" }>, b: Extract<Route, { screen: "session" }>) =>
  a.root === b.root && a.repo === b.repo && (a.agent ?? a.root) === (b.agent ?? b.root) && JSON.stringify(a.sheet ?? null) !== JSON.stringify(b.sheet ?? null);

export function Sidebar({ route, navigate, refresh, openUrl, onBackFromSettings }: SidebarProps) {
  const tab = settingsTab(route);
  const railOpen = inProfileRail(route);
  const { repos, sessions, error: feedError, closeError, close, after } = useSessions();
  const [cursor, setCursor] = useState<string | null>(null);
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [split, setSplit] = useState(readSplit);
  const [pausedOpen, setPausedOpen] = useState(readPausedOpen);
  const list = useRef<HTMLElement>(null);
  const noRefresh = useCallback(() => Promise.resolve(), []);
  const { usage, error: usageError } = useUsage();
  const mostUsed = usage?.providers.filter((provider) => weeklyWindow(provider)).sort((a, b) => weeklyWindow(b)!.used - weeklyWindow(a)!.used)[0];

  // The live sessions in reading order (⌘1-9), and the paused ones after them.
  const live = useMemo(() => (repos ?? []).flatMap((repo) => (sessions[repo.repo_root] ?? []).filter((s) => !isPaused(s)).map((session) => ({ session, repo: repo.repo_root }))), [repos, sessions]);
  const paused = useMemo(() => (repos ?? []).flatMap((repo) => (sessions[repo.repo_root] ?? []).filter(isPaused).map((session) => ({ session, repo: repo.repo_root }))), [repos, sessions]);
  const all = useMemo(() => [...live, ...paused], [live, paused]);

  // The context pane's session: the one on screen, else the one kept from last time, while it is listed.
  const selected = useMemo<SessionRef | null>(() => {
    const here = routeSession(route);
    const listed = (ref: SessionRef | null) => (ref && all.some((r) => r.session.id === ref.root && r.repo === ref.repo) ? ref : null);
    // A session route must never show another session's goal or actions when its row leaves the feed.
    if (here) return listed(here);
    return listed(readLastSession()) ?? (all[0] ? { root: all[0].session.id, repo: all[0].repo } : null);
  }, [route, all]);

  // The model the selected session's own agent last ran on, from the agents the snapshot already lists.
  const workspaces = useAppState((s) => s.workspaces);
  const alive = useAppState((s) => s.alive);
  const runningAgents = useMemo(() => workspaces.flatMap((ws) => ws.agents ?? []).filter((a) =>
    !isPastWorker(a) && !["paused", "done", "failed"].includes(a.status) && alive[a.id] !== false,
  ).length, [workspaces, alive]);
  const agent = useMemo(() => workspaces.flatMap((ws) => ws.agents ?? []).find((a) => a.id === selected?.root), [workspaces, selected]);
  // An open session follows its supervisor's provider, even before its model is known.
  const shown = route.screen === "session" ? usage?.providers.find((p) => p.provider === agent?.provider) : mostUsed;
  const warning = !!shown && (shown.limited || planWindows(shown).some((window) => window.used >= WARN));

  // An open session's agents show, on each navigation to it; a sheet opening or closing over the
  // session (a visual, Review changes) isn't one, so Back finds the tree folded as it was left.
  const lastRoute = useRef(route);
  useEffect(() => {
    const was = lastRoute.current;
    lastRoute.current = route;
    if (route.screen !== "session") return;
    if (was.screen === "session" && sheetOnly(was, route)) return;
    setFolded((f) => (f.has(route.root) ? new Set([...f].filter((id) => id !== route.root)) : f));
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
    const move = (row: RowRef) => {
      setCursor(row.key);
      // The cursor is kept on the list rather than focusing each button: bring its row into view explicitly.
      const button = [...(list.current?.querySelectorAll<HTMLButtonElement>("button[data-row]") ?? [])].find((el) => el.dataset.row === row.key);
      button?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
    };
    switch (e.key) {
      case "ArrowDown":
      case "j":
        move(rowsNow[Math.min(at + 1, rowsNow.length - 1)]);
        break;
      case "ArrowUp":
      case "k":
        move(rowsNow[Math.max(at - 1, 0)]);
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

  let index = 0;
  return (
    <aside className="side flex min-h-0 w-[352px] max-w-full shrink-0 flex-col gap-[10px] overflow-hidden border-r border-line bg-canvas-2 px-[12px] pb-[12px] pt-[14px] text-[13px] text-ink" aria-label="Sidebar">
      <div className="flex shrink-0 items-center gap-s1 px-1.5 pb-1">
        <Logo className="text-[17px]" onHome={() => navigate({ screen: "new" })} />
        <span className="flex-1" />
        <button
          type="button"
          className={`flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11.5px] font-medium ${route.screen === "new" ? "border-pass/40 bg-pass-soft text-pass" : "border-pass/30 bg-pass-soft/60 text-ink-2 hover:bg-pass-soft hover:text-pass"}`}
          aria-current={route.screen === "new" ? "page" : undefined}
          title="New session (⌘N)"
          data-walkthrough="new-session"
          onClick={() => navigate({ screen: "new" })}
        >
          <span className="text-sm leading-none" aria-hidden>
            +
          </span>
          New session
        </button>
      </div>
      {railOpen && <SettingsRail tab={tab} page={railPage(route)} onBack={onBackFromSettings ?? (() => navigate({ screen: "new" }))} onTab={(tab) => navigate({ screen: "settings", tab, repo: "repo" in route ? route.repo : undefined })} onPage={(page) => navigate({ screen: page })} />}
      <div hidden={railOpen} className={railOpen ? "hidden" : "contents"}>
      <InboxEntry route={route} navigate={navigate} />
      <div className="mx-2 h-px shrink-0 bg-line" role="separator" data-testid="inbox-divider" />
      <SessionContext session={selected} route={route} navigate={navigate} refresh={refresh ?? noRefresh} openUrl={openUrl} style={{ height: split }} />
      <Handle height={split} onChange={resize} />
      <div className="side-h shrink-0">
        Sessions
        {runningAgents > 0 && (
          <span
            className="ml-auto inline-flex h-[18px] items-center gap-1 rounded-full bg-pass-soft px-1.5 font-mono text-[11px] font-bold leading-none text-pass"
            title={`${runningAgents} agent${runningAgents === 1 ? "" : "s"} running`}
            aria-label={`${runningAgents} agent${runningAgents === 1 ? "" : "s"} running`}
          >
            <span className="size-1.5 rounded-full bg-pass" aria-hidden="true" />
            {runningAgents}
          </span>
        )}
      </div>
      <nav ref={list} aria-label="Sessions" className="side-sessions flex min-h-0 flex-1 flex-col gap-[14px] overflow-auto outline-none focus-visible:ring-1 focus-visible:ring-branch" tabIndex={0} onKeyDown={keys}>
        {/* A close the engine refused: the session is back in its place, and this says why. */}
        {closeError && (
          <p className="px-s1 text-xs text-sap" data-testid="close-error">
            {closeError}
          </p>
        )}
        {repos === null ? (
          <p className={`px-s1 text-xs ${feedError ? "text-sap" : "text-ink-3"}`}>{feedError ? `Couldn't load the sessions: ${feedError}` : "Loading…"}</p>
        ) : repos.length === 0 ? (
          <p className="px-s1 text-xs text-ink-3">No project yet. Open a folder or clone a repository to start.</p>
        ) : (
          repos.map((repo) => {
            const here = live.filter((r) => r.repo === repo.repo_root);
            const rested = paused.filter((r) => r.repo === repo.repo_root);
            if (here.length === 0 && rested.length === 0) return null;
            return (
              <div key={repo.repo_root} className="flex flex-col" data-testid={`repo-${repo.name}`}>
                <button
                  type="button"
                  className={`${REPO_HEADING} truncate text-left hover:text-ink`}
                  title={`${repo.repo_root}: project settings`}
                  aria-label={`Project settings for ${repo.name}`}
                  onClick={() => navigate({ screen: "project-settings", repo: repo.repo_root })}
                >
                  {repo.name}
                </button>
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
                      openUrl={openUrl ?? openExternal}
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
                        <span className="w-[18px] shrink-0 text-center text-[16px] leading-none" aria-hidden>
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
                      <PausedRow key={session.id} session={session} repo={repo.repo_root} route={route} navigate={navigate} refresh={refresh ?? noRefresh} cursor={cursor} setCursor={setCursor} onClose={closeRow} list={list} openUrl={openUrl ?? openExternal} />
                    ))}
                </ul>
              </div>
            );
          })
        )}
        {repos && repos.length > 0 && live.length === 0 && <p className="px-s1 text-xs text-ink-3">Nothing running. Start a session, or continue a recent one.</p>}
      </nav>
      </div>
      <div className="flex shrink-0 flex-col gap-2 border-t border-line px-s1 pt-s1" data-testid="sidebar-foot">
        <div className="flex h-10 min-w-0 shrink-0 flex-nowrap items-center gap-1 whitespace-nowrap" role="group" aria-label="Sidebar foot" data-testid="sidebar-foot-row">
          <AccountButton route={route} navigate={navigate} provider={shown} warning={warning} failed={usageError != null} />
          <span className="flex-1" />
          <TerminalToggle />
        </div>
      </div>
    </aside>
  );
}
