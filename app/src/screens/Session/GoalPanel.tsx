/* The goal panel, above a session's agent tree: the session's goal and its
   milestones with their check commands and last results. The
   supervisor runs the checks; the panel only shows when they last ran.

   The goal is not part of the store's snapshot, so the panel fetches
   GET /v1/goal for its session (`root`) itself. It refetches after an action is
   acknowledged, when a goal check's job ends (job.done / job.failed), and when
   the event stream reports goal activity or a resnapshot. Completion comes
   from the milestones' statuses: GoalDTO.progress is an activity counter.

   The panel shows the session's one lifecycle, the engine's goal_phase: In
   progress, In review, Ready for PR (with the panel's own Review & open PR,
   the one way to the review sheet), PR open (a link to it), Done. Done keeps
   the goal and every milestone, all checked, as the engine serves them
   (it keeps a reached goal until a new one replaces it), and beside it a
   button Close session (POST /sessions/{root}/close, one click, nothing
   asked: the session goes, its branches stay; in the sidebar it sits under
   the milestones). An older engine sends no
   goal_phase: the PR readiness (review.ts) and the milestones stand in for
   it. Nothing here ever says "failed".

   Only the newest GET answers: a slower, older response is dropped, and so is
   any response that arrives after the panel has moved to another session. */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import type { BusEvent } from "../../api/events";
import type { DaemonClient } from "../../api/client";
import { openExternal, type OpenUrl } from "../../api/open";
import { usePtyViews } from "../../api/ptyView";
import type { GoalDTO, MilestoneDTO, WorkspaceDTO } from "../../api/schema";
import { alert, button, card, mainTone, mono, pill, quiet } from "../../components/ui";
import { rootOf } from "../../store/overview";
import type { AppStore } from "../../store/store";
import { isGoalActivity, usePrReadiness, type Readiness } from "./review";

const GOAL_JOBS = new Set(["goal.check", "session.pr"]);

/* Copy the full startup request from the goal title. Keyed by session and text
   by the caller, so pending copies and feedback cannot cross sessions. */
export function SessionGoal({ text, children, className, onOpen, noun = "Goal", title, enterCopies = true, current, treeAgent, row }: {
  text?: string | null;
  children: ReactNode;
  className: string;
  onOpen(): void;
  /* What the toast says was copied; a tree row copies its handoff info and keeps Enter for opening. */
  noun?: string;
  title?: string;
  enterCopies?: boolean;
  current?: "page";
  treeAgent?: string;
  row?: string;
}) {
  const views = usePtyViews();
  const [feedback, setFeedback] = useState<"copied" | "error" | null>(null);
  const live = useRef(false);
  const copying = useRef(false);
  useEffect(() => {
    live.current = true;
    return () => { live.current = false; };
  }, []);
  useEffect(() => {
    if (!feedback) return;
    const timer = window.setTimeout(() => setFeedback(null), 1600);
    return () => window.clearTimeout(timer);
  }, [feedback]);
  const copy = async () => {
    if (!text || copying.current) return;
    copying.current = true;
    setFeedback(null);
    try {
      if (views) await views.copy(text);
      else await navigator.clipboard.writeText(text);
      if (live.current) setFeedback("copied");
    } catch {
      if (live.current) setFeedback("error");
    } finally {
      copying.current = false;
    }
  };
  return (
    <div className="relative min-w-0 flex-1">
      <button type="button" className={className} title={title ?? (text ? "Double-click to copy the goal" : "Open the session")}
        onClick={onOpen} onDoubleClick={() => void copy()} aria-current={current}
        data-tree-agent={treeAgent} data-row={row}
        onKeyDown={(e) => {
          if (e.key === "Enter" && text && enterCopies) {
            e.preventDefault();
            e.stopPropagation();
            void copy();
          }
        }}>
        {children}
      </button>
      {feedback && <span role="status" className="pointer-events-none absolute left-0 top-full z-10 mt-1 whitespace-nowrap rounded border border-line bg-surface px-2 py-1 text-xs font-normal text-ink shadow-sm">
        {feedback === "copied" ? `${noun} copied to clipboard` : "Couldn't copy"}
      </span>}
    </div>
  );
}

/* The last few lines of a check's output: enough to see why it stopped. */
export function outputTail(output: string | null | undefined, lines = 6): string | null {
  if (!output) return null;
  return output.trimEnd().split("\n").slice(-lines).join("\n");
}

/* "20 min ago", "2h ago", "3d ago": when a check last ran (bus seconds). */
export function formatAgo(at: number, now = Date.now() / 1000): string {
  const s = Math.max(0, now - at);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/* How many milestones have passed, out of how many. */
export function completion(milestones: MilestoneDTO[] | undefined): { passed: number; total: number } {
  const all = milestones ?? [];
  return { passed: all.filter((m) => m.status === "passed").length, total: all.length };
}

/* A goal check's or the session PR's job has ended (succeeded or did not). */
function isGoalJobDone(e: BusEvent): boolean {
  return (e.kind === "job.done" || e.kind === "job.failed") && GOAL_JOBS.has(String(e.payload.job_kind));
}

/* A bus event that means the goal may have changed: goal activity for this
   session (or any session's, when the event names none), its PR's readiness or
   job, or a finished check. */
function concernsGoal(e: BusEvent, root: string): boolean {
  if (e.root_id !== null && e.root_id !== root) return false;
  return isGoalActivity(e) || e.kind.startsWith("session.pr") || isGoalJobDone(e);
}

/* Where the session's worktrees stand by git (commits ahead, dirty files), as the store's git sweeps update it:
   a change means commits the activity line should count. */
function sessionGitMark(workspaces: WorkspaceDTO[], root: string): string {
  const agents = workspaces.flatMap((ws) => ws.agents ?? []);
  return workspaces
    .filter((ws) => (ws.agents ?? []).some((a) => rootOf(agents, a.id) === root))
    .map((ws) => `${ws.id}:${ws.ahead ?? ""}:${ws.dirty ?? ""}`)
    .join("|");
}

const STOPPED_STATUS = new Set(["paused", "done", "failed", "exited", "rewound"]);
const BUSY_STATUS = new Set(["processing", "starting", "waiting"]);

/* The stage a branch's review_state says it is at (checks, reviewer, merge), or null when it names none. */
function pipelineStage(reviewState: string | null | undefined): Stage | null {
  const s = reviewState ?? "";
  if (/^(waiting for checks|checks running)/.test(s)) return "checks";
  if (/^reviewer running/.test(s)) return "review";
  if (/^approved, merging/.test(s)) return "merging";
  return null;
}

/* The stage of each milestone (by title) a worker of this session is linked to (the engine's agent `milestone`),
   from the worker itself and its branch's checks/review: a milestone with an entry is active, however many others
   are too. A worker that exited (liveness) or stopped counts only while its branch is in checks or review; one that
   reported counts while it is busy again (fixing review findings). Several workers on one milestone: the earliest stage. */
export function activeMilestoneStages(workspaces: WorkspaceDTO[], alive: Record<string, boolean>, root: string): Map<string, Stage> {
  const agents = workspaces.flatMap((ws) => ws.agents ?? []);
  const stages = new Map<string, Stage>();
  for (const ws of workspaces) {
    for (const a of ws.agents ?? []) {
      if (!a.milestone || a.mode === "interactive" || a.mode === "review" || a.dismissed_at != null || rootOf(agents, a.id) !== root) continue;
      const gone = STOPPED_STATUS.has(a.status) || alive[a.id] === false;
      const piped = pipelineStage(ws.review_state);
      const stage = piped ?? (!gone && (BUSY_STATUS.has(a.status) || !a.reported) ? "working" : null);
      if (!stage) continue;
      const before = stages.get(a.milestone);
      if (!before || STAGES.indexOf(stage) < STAGES.indexOf(before)) stages.set(a.milestone, stage);
    }
  }
  return stages;
}

/* How often the sidebar refetches its goal while open,for the activity line's rolling hour, ages and estimate. */
const ACTIVITY_REFRESH_MS = 60_000;

export type GoalPhase ="in_progress" | "in_review" | "ready_for_pr" | "pr_open" | "done";
export const GOAL_PHASE_LABEL: Record<GoalPhase, string> = {
  in_progress: "In progress",
  in_review: "In review",
  ready_for_pr: "Ready for PR",
  pr_open: "PR open",
  done: "Done",
};
const GOAL_PHASE_STYLE: Record<GoalPhase, string> = {
  in_progress: "text-branch",
  in_review: "text-branch",
  ready_for_pr: "text-pass",
  pr_open: "text-branch",
  done: "text-pass",
};
const isGoalPhase = (p: string | null | undefined): p is GoalPhase => !!p && p in GOAL_PHASE_LABEL;

/* Where the session stands, and its PR's page when one is open: the engine's
   goal_phase when it sends one the app knows. An older engine sends none: a PR
   it opened reads PR open, its word that the session is ready reads Ready for
   PR, every milestone passed reads In review (brindle is still judging it), or
   Done when the engine can't judge PRs at all and calls the goal done; else In
   progress. */
export function goalPhase(goal: GoalDTO, readiness: Pick<Readiness, "state" | "pr">): { phase: GoalPhase; prUrl: string | null } {
  const prUrl = goal.pr_url ?? readiness.pr?.url ?? null;
  if (isGoalPhase(goal.goal_phase)) return { phase: goal.goal_phase, prUrl };
  if (readiness.pr) return { phase: "pr_open", prUrl };
  if (readiness.state === "ready") return { phase: "ready_for_pr", prUrl };
  const { passed, total } = completion(goal.milestones);
  if (total > 0 && passed === total) return { phase: readiness.state === "unknown" && goal.state === "done" ? "done" : "in_review", prUrl };
  return { phase: "in_progress", prUrl };
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/* A milestone's check state, in the app's words: the panel only ever says Done or In
   progress. A milestone that passed and now doesn't reads In progress too, like one that
   hasn't passed yet or couldn't run (the supervisor fixes those itself). The engine's
   reason (state_why) is a tooltip, nothing more. */
const CHECK_LABEL: Record<string, string> = {
  passed: "Done",
  not_done: "In progress",
  failed: "In progress",
  regressed: "In progress",
};
const PHASE_LABEL: Record<string, string> = { done: "Done", in_review: "In review", in_progress: "In progress" };
const LABEL_STYLE: Record<string, string> = {
  Done: "text-pass",
  "In review": "text-branch",
  "In progress": "text-branch",
};

/* The label the panel shows: the engine's phase when it sends one, else the check state's. */
export function checkLabel(m: MilestoneDTO): string | undefined {
  return m.phase ? PHASE_LABEL[m.phase] : CHECK_LABEL[checkState(m) ?? ""];
}

/* The check state the panel shows: the engine's `state`. A milestone the engine
   hasn't checked but whose status is passed (an older snapshot) reads as done. */
export function checkState(m: MilestoneDTO): string | null {
  return m.state ?? (m.status === "passed" ? "passed" : null);
}

/* A goal's phases (each milestone's optional phase_name, from goals.md's `phase:` or set_goal's "phase"),
   as folds over the list: consecutive milestones of one phase, a milestone naming none staying in the phase
   above it (milestones before the first phase sit unfolded on top). Null when no milestone names a phase:
   the list is drawn as it always was. A fold is done once all its milestones are; the first unfinished one,
   and any later one with work done or running in it, is active (open until folded); the rest are upcoming. */
export interface PhaseFold {
  key: number;
  name: string | null;
  milestones: MilestoneDTO[];
  state: "done" | "active" | "todo";
}

export function phaseFolds(milestones: MilestoneDTO[], isDone: (m: MilestoneDTO) => boolean): PhaseFold[] | null {
  if (!milestones.some((m) => m.phase_name)) return null;
  const folds: PhaseFold[] = [];
  for (const m of milestones) {
    const last = folds[folds.length - 1];
    if (last && (!m.phase_name || m.phase_name === last.name)) last.milestones.push(m);
    else folds.push({ key: m.position, name: m.phase_name ?? null, milestones: [m], state: "todo" });
  }
  let seenUnfinished = false;
  for (const f of folds) {
    if (f.milestones.every(isDone)) f.state = "done";
    else {
      const moving = f.milestones.some((m) => isDone(m) || m.status === "running");
      f.state = !seenUnfinished || moving ? "active" : "todo";
      seenUnfinished = true;
    }
  }
  return folds;
}

/* The stage the active step is at (the engine's goal_activity): each has its colour in the sidebar, a
   `st-<stage>` class (styles.css), and its word as a tag. */
export type Stage = "working" | "checks" | "review" | "merging";
const STAGES: readonly string[] = ["working", "checks", "review", "merging"];
const isStage = (s: string | null | undefined): s is Stage => !!s && STAGES.includes(s);

/* A milestone's name as a sidebar step: its title up to the first ": " or " — ", or the whole title. */
export function shortName(title: string): string {
  const cut = title.search(/: | — /);
  return cut > 0 ? title.slice(0, cut) : title;
}

/* "15m", "2h", "3d": how long ago, as the activity line says it (bus seconds). */
function shortAgo(at: number, now: number): string {
  const s = Math.max(0, now - at);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

/* The sidebar's activity line: commits in the last hour, else when the last one landed, and how long is left
   when brindle can say and something runs. Null when there is nothing to say (an older engine sends none of it). */
export function activityLine(goal: GoalDTO, now = Date.now() / 1000): { what: string | null; eta: string | null } | null {
  const commits = goal.commits_last_hour ?? 0;
  const what = commits > 0
    ? `${commits} ${commits === 1 ? "commit" : "commits"} in the last hour`
    : goal.last_commit_at != null ? `last commit ${shortAgo(goal.last_commit_at, now)} ago` : null;
  const minutes = isStage(goal.stage) && goal.eta_seconds != null ? Math.max(1, Math.round(goal.eta_seconds / 60)) : null;
  const eta = minutes === null ? null : minutes < 90 ? `~${minutes}m left` : `~${Math.round(minutes / 60)}h left`;
  return what || eta ? { what, eta } : null;
}

/* "phase 3 of 4": where the goal is, by its named phases; empty with none, or once nothing is left. */
function phaseWord(folds: PhaseFold[] | null): string {
  const named = (folds ?? []).filter((f) => f.name !== null);
  const at = named.findIndex((f) => f.state !== "done");
  return at < 0 ? "" : `phase ${at + 1} of ${named.length}`;
}

const CHEV = (
  <svg className="chev" viewBox="0 0 12 12" aria-hidden>
    <path d="M4.5 3 7.5 6 4.5 9" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

/* The milestones under their phase folds: a done phase is one line with its green count, the active one lists
   every row (passed ones too, as they look today), an upcoming one is folded with its count. Any fold opens or
   closes on a click. The full panel's fold line carries a tiny bar, one segment per milestone. */
function PhaseFoldList({ folds, open, onToggle, isDone, large, rowsClass, row }: {
  folds: PhaseFold[];
  open: Record<number, boolean>;
  onToggle(key: number, next: boolean): void;
  isDone(m: MilestoneDTO): boolean;
  large: boolean;
  rowsClass: string;
  row(m: MilestoneDTO): ReactNode;
}) {
  return (
    <ul className={`ms-folds${large ? " lg" : ""}`} aria-label="Milestones">
      {folds.map((f) => {
        if (f.name === null) return <li key={f.key}><ul className={rowsClass}>{f.milestones.map(row)}</ul></li>;
        const isOpen = open[f.key] ?? f.state === "active";
        const passed = f.milestones.filter(isDone).length;
        const n = f.milestones.length;
        const count = f.state === "done" ? `✓ ${n} of ${n}` : f.state === "todo" ? `${n} upcoming` : `${passed} of ${n}`;
        return (
          <li key={f.key} data-st={f.state}>
            <button type="button" className="ms-fold" data-st={f.state} data-testid="phase-fold" aria-expanded={isOpen} onClick={() => onToggle(f.key, !isOpen)}>
              {CHEV}
              <span className="n">{f.name}</span>
              {large && (
                <span className="bar-s" aria-hidden>
                  {f.milestones.map((m) => (
                    <i key={m.id} data-s={isDone(m) ? "passed" : m.status === "running" ? "running" : "todo"} />
                  ))}
                </span>
              )}
              <span className="k">{count}</span>
            </button>
            {isOpen && <ul className={`${rowsClass} ms-kids`}>{f.milestones.map(row)}</ul>}
          </li>
        );
      })}
    </ul>
  );
}

/* finished: the goal is Done, so every milestone reads done whatever its last check said. */
function Milestone({ milestone, finished }: { milestone: MilestoneDTO; finished: boolean }) {
  const label = finished ? "Done" : checkLabel(milestone);
  const done = label === "Done";
  return (
    <li className="flex gap-s1 py-1" data-testid="milestone">
      <span
        className={`mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold ${
          done ? "bg-pass text-on-branch" : "border border-line-strong text-ink-3"
        }`}
        aria-hidden
      >
        {done ? "✓" : milestone.position}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-s2">
          <span className={`text-sm ${done ? "text-ink-2" : "font-medium"}`}>
            {milestone.position}. {milestone.title}
          </span>
          {label && (
            <span className={`${pill} ${LABEL_STYLE[label]}`} title={milestone.state_why ?? undefined}>
              {label}
            </span>
          )}
        </div>
        {milestone.detail && <p className="mt-0.5 text-sm text-ink-2" data-testid="milestone-detail">{milestone.detail}</p>}
        {milestone.check_cmd && <code className={`mt-0.5 block ${mono} text-ink-3`}>{milestone.check_cmd}</code>}
      </div>
    </li>
  );
}

/* The goal as the session bar reads it: fetched on mount and again after
   any event that may have changed it (an action's ack, a check's end, a
   resnapshot); null for a session without one, undefined while unknown
   (or with no engine to ask). The panel above keeps its own, finer, copy. */
export function useGoal(client: DaemonClient | null, repo: string, root: string, store: AppStore): GoalDTO | null | undefined {
  const [goal, setGoal] = useState<GoalDTO | null | undefined>(undefined);
  useEffect(() => {
    if (!client) return;
    let live = true;
    let seq = 0;
    const load = () => {
      const mine = ++seq;
      client.goal(repo, root).then(
        (got) => live && mine === seq && setGoal(got),
        () => undefined,
      );
    };
    setGoal(undefined);
    load();
    let seen = store.getState().lastEventId;
    let stale = store.getState().stale;
    const unsubscribe = store.subscribe(() => {
      const s = store.getState();
      if (s.lastEventId !== seen) {
        const fresh = s.recent.filter((e) => e.id > seen);
        seen = s.lastEventId;
        if (fresh.some((e) => concernsGoal(e, root))) load();
      }
      if (s.stale && !stale) load();
      stale = s.stale;
    });
    return () => {
      live = false;
      unsubscribe();
    };
  }, [client, repo, root, store]);
  return goal;
}

export interface GoalPanelProps {
  client: DaemonClient;
  /* The repo the session belongs to, and the session's root agent id. */
  repo: string;
  root: string;
  store: AppStore;
  /* A paused session still shows its goal. */
  paused?: boolean;
  /* In the sidebar's context pane: no card frame, tighter, and the
     milestones open from the start (the pane is theirs). */
  compact?: boolean;
  /* Opens the session's Review & open PR sheet: the Ready for PR step's action. */
  onReview?(): void;
  /* Opens the PR's page in the system browser; the shell's by default. */
  openUrl?: OpenUrl;
  /* Closes the whole session, at once and unasked: the Done step's action. */
  onCloseSession?(): void;
  /* Opens Knowledge › Docs filtered to this session: the docs line's action. */
  onOpenDocs?(): void;
}

export function GoalPanel({ client, repo, root, store, compact = false, onReview, openUrl = openExternal, onCloseSession, onOpenDocs }: GoalPanelProps) {
  const [goal, setGoal] = useState<GoalDTO | null>(null);
  // The docs this session wrote or used; null while unknown or unreachable (the line stays hidden).
  const [docSummary, setDocSummary] = useState<{ total: number; writing: number; updated: number; using: number } | null>(null);
  // An older engine's stand-in for goal_phase: its word on the session's PR.
  const readiness = usePrReadiness(client, root, store);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // Beside a terminal the milestones fold away by default (it wants the room); in the sidebar they show.
  const [open, setOpen] = useState(compact);
  // The phase folds the person opened or closed, by fold key; the rest keep their default (only the active phase open).
  const [foldOpen, setFoldOpen] = useState<Record<number, boolean>>({});
  const toggleFold = useCallback((key: number, next: boolean) => setFoldOpen((o) => ({ ...o, [key]: next })), []);
  const frame = compact ? "flex flex-col gap-1.5 px-1.5 pb-1.5 pt-1" : `${card} flex flex-col gap-1 px-s2 py-s1`;
  /* This session: its client, repo and root. Every continuation (a GET, an action's
     ack, its error or end) applies only while it is still the session on screen, so
     one started for an earlier session can't touch this one. */
  const session = useMemo(() => ({ client, repo, root }), [client, repo, root]);
  const activeSession = useRef<typeof session | null>(null);
  /* The newest GET /goal asked for; only the newest response applies. */
  const requestSeq = useRef(0);

  const refresh = useCallback(() => {
    // A refresh from an earlier session's closure does nothing; it must not bump the counter.
    if (activeSession.current !== session) return Promise.resolve();
    const seq = ++requestSeq.current;
    const current = () => activeSession.current === session && seq === requestSeq.current;
    void client.docSessionSummary(root).then(
      (got) => current() && setDocSummary(got),
      () => current() && setDocSummary(null),
    );
    return client
      .goal(repo, root)
      .then((next) => {
        if (!current()) return;
        setGoal(next);
        setLoaded(true);
        setLoadError(null);
      })
      .catch((e: unknown) => {
        if (current()) setLoadError(message(e));
      });
  }, [session, client, repo, root]);

  useEffect(() => {
    activeSession.current = session;
    // Whatever was on screen belongs to an earlier session: this one starts afresh.
    setGoal(null);
    setLoaded(false);
    setLoadError(null);
    setActionError(null);
    setDocSummary(null);
    setFoldOpen({});
    void refresh();
    let seen = store.getState().lastEventId;
    let stale = store.getState().stale;
    let workspaces = store.getState().workspaces;
    let gitMark = sessionGitMark(workspaces, root);
    const unsubscribe = store.subscribe(() => {
      const s = store.getState();
      let again = false;
      if (s.lastEventId !== seen) {
        const fresh = s.recent.filter((e) => e.id > seen);
        seen = s.lastEventId;
        again ||= fresh.some((e) => concernsGoal(e, root));
      }
      again ||= s.stale && !stale;
      stale = s.stale;
      // The sidebar counts the session's commits: a git sweep of one of its worktrees may have new ones.
      if (compact && s.workspaces !== workspaces) {
        workspaces = s.workspaces;
        const mark = sessionGitMark(workspaces, root);
        again ||= mark !== gitMark;
        gitMark = mark;
      }
      if (again) void refresh();
    });
    // The activity line's last hour, ages and estimate move with the clock.
    const tick = compact ? window.setInterval(() => void refresh(), ACTIVITY_REFRESH_MS) : undefined;
    return () => {
      activeSession.current = null;
      unsubscribe();
      window.clearInterval(tick);
    };
  }, [session, store, refresh, root, compact]);

  const storeWorkspaces = useSyncExternalStore(store.subscribe, () => store.getState().workspaces);
  const storeAlive = useSyncExternalStore(store.subscribe, () => store.getState().alive);
  const workerStages = useMemo(() => activeMilestoneStages(storeWorkspaces, storeAlive, root), [storeWorkspaces, storeAlive, root]);
  if (!loaded && loadError) {
    return (
      <section className={`${compact ? "px-s1 py-1" : `${card} px-s2 py-s1`} flex items-center gap-s2`} aria-label="Goal">
        <p className={`${alert} flex-1`}>Could not load the goal: {loadError}</p>
        <button
          type="button"
          className={button}
          onClick={() => {
            setLoadError(null);
            void refresh();
          }}
        >
          Retry
        </button>
      </section>
    );
  }
  if (!loaded) {
    return (
      <section className={`${compact ? "px-s1 py-1" : `${card} px-s2 py-s1`} text-sm text-ink-3`} aria-label="Goal">
        Loading goal…
      </section>
    );
  }
  if (!goal) {
    return (
      <section className={`${compact ? "px-s1 py-1" : `${card} px-s2 py-s1`} text-sm text-ink-3`} aria-label="Goal">
        No goal set for this session.
      </section>
    );
  }

  const { phase, prUrl } = goalPhase(goal, readiness);
  const finished = phase === "done";
  // The engine keeps a reached goal, its title and every milestone, until a new goal replaces it: Done lists them all, checked.
  const milestones = goal.milestones ?? [];
  const total = milestones.length;
  const passed = finished ? total : completion(milestones).passed;
  const pct = total === 0 ? (finished ? 100 : 0) : Math.round((passed / total) * 100);
  const shownError = actionError ?? loadError;
  // The milestone being worked on: the running one, else the first not yet passed; none once the goal is done.
  const current = finished ? undefined : (milestones.find((m) => m.status === "running") ?? milestones.find((m) => m.status !== "passed"));
  const isDone = (m: MilestoneDTO) => finished || checkLabel(m) === "Done";
  // Every unfinished milestone a live worker is linked to is active; with none linked, the current one stands alone.
  const linked = finished ? [] : milestones.filter((m) => workerStages.has(m.title) && !isDone(m));
  // Each active milestone's own stage: its workers' (and their branches'); the lone current one reads the goal's.
  const activeStage = new Map<number, Stage | null>(
    linked.length > 0
      ? linked.map((m) => [m.id, workerStages.get(m.title)!] as const)
      : current ? [[current.id, isStage(goal.stage) ? goal.stage : null] as const] : [],
  );
  // The goal's phases as folds; null without any (the list as it always was).
  const folds = phaseFolds(milestones, isDone);
  const where = finished ? "" : phaseWord(folds);
  const phaseLabel = GOAL_PHASE_LABEL[phase];
  // The sidebar's count line names only the steps after In progress; Done is the green count itself.
  const showPhase = phase !== "in_progress" && phase !== "done";
  /* The lifecycle's own step, where it has one: Ready for PR's Review & open PR, PR open's link and the
     way back into Review, where the session's commits not on the PR yet wait for the person's Push. */
  const unpushed = readiness.pr?.unpushed ?? 0;
  const step =
    phase === "pr_open" && prUrl ? (
      <div className={`flex flex-col gap-0.5 ${compact ? "mx-0.5" : ""}`}>
        <button
          type="button"
          className="flex items-center gap-s1 rounded-md bg-pass-soft px-s1 py-1 text-left text-xs font-medium text-pass hover:opacity-90"
          data-testid="goal-pr"
          title={prUrl}
          onClick={() => void openUrl(prUrl).catch((e: unknown) => setActionError(message(e)))}
        >
          <span className="min-w-0 truncate">{prUrl.replace(/^https?:\/\//, "")}</span>
          <span className="flex-1" />
          Open the PR ↗
        </button>
        {onReview && (
          <button
            type="button"
            className="flex items-center gap-s1 rounded-md px-s1 py-0.5 text-left text-xs text-ink-2 hover:bg-surface-2"
            data-testid="goal-pr-review"
            onClick={onReview}
          >
            Review changes
            {unpushed > 0 && <span className="text-sap">· {unpushed} not on the PR</span>}
            <span className="flex-1" />›
          </button>
        )}
      </div>
    ) : null;
  /* The session's docs, one quiet line under the milestones; hidden with none. */
  const docsLine =
    docSummary && docSummary.total > 0 ? (
      <button type="button" className={`${quiet} self-start text-xs`} data-testid="goal-docs" title="Knowledge › Docs for this session" onClick={onOpenDocs}>
        {docSummary.total} {docSummary.total === 1 ? "doc" : "docs"} · writing {docSummary.writing}, using {docSummary.using} →
      </button>
    ) : null;
  /* Done's step: close the session, one click, nothing asked. Beside the status in the full view; in the sidebar
     a quiet text button under the milestones, aligned with their names. */
  const closeSession =
    finished && onCloseSession ? (
      <button
        type="button"
        className={
          compact
            ? "self-start pl-[30px] text-[11.5px] text-ink-3 hover:text-ink hover:underline"
            : `inline-flex shrink-0 items-center whitespace-nowrap rounded-md border px-s1 py-0.5 ${mainTone}`
        }
        data-testid="goal-close"
        title="Stop and hide the whole session; its branches stay"
        onClick={onCloseSession}
      >
        Close session
      </button>
    ) : null;
  if (compact) {
    // The sidebar's goal: the completed count, a thin bar (done in green, the active step's slice in its stage's
    // colour), one activity line, then the milestones as short steps (✓ done, the active one marked and tagged).
    const donePct = total === 0 ? (finished ? 100 : 0) : (passed / total) * 100;
    // The stage the active step is at; none once the goal is finished or while nothing runs.
    const barStages = [...activeStage.values()].filter((s): s is Stage => s !== null);
    const activity = finished ? null : activityLine(goal);
    // Every unfinished milestone is in progress (the engine works them in parallel), so each one is lit.
    const stateOf = (m: MilestoneDTO): "done" | "now" => (isDone(m) ? "done" : "now");
    const sideRow = (m: MilestoneDTO) => {
      const state = stateOf(m);
      const active = activeStage.has(m.id);
      const stage = active ? activeStage.get(m.id) : null;
      const label = finished ? "Done" : checkLabel(m);
      const tip = [m.title, label, m.state_why ?? (active ? m.detail || m.check_cmd : null)].filter(Boolean).join(" · ");
      return (
        <li key={m.id} className={`m${active && stage ? ` st-${stage}` : ""}`} data-state={state} data-active={active || undefined} data-testid="milestone" title={tip}>
          <span className="i" aria-hidden>
            {state === "done" ? "✓" : active ? "●" : "·"}
          </span>
          <span className="t">{shortName(m.title)}</span>
          {active && stage && <span className="tag" data-testid="goal-stage">{stage}</span>}
        </li>
      );
    };
    return (
      <section className={`${frame} min-h-0`} aria-label="Goal" data-phase={phase}>
        {/* Pinned: the count and the bar stay put; only the milestones below scroll. */}
        <div className="flex shrink-0 flex-col gap-1.5" data-testid="goal-header">
        <div className="flex items-center gap-s1 px-0.5 text-xs font-medium text-ink">
          {/* The count is green once the goal is finished; In progress and Done need no label of their own. */}
          <span data-testid="goal-line" className={finished ? "text-pass" : undefined}>
            {total > 0 ? `${passed} of ${total} milestones complete` : ""}
            {showPhase && (
              <>
                {total > 0 && " · "}
                <span data-testid="goal-phase">
                  {phaseLabel}
                </span>
              </>
            )}
          </span>
          <span className="flex-1" />
          {where && <span className="whitespace-nowrap font-normal text-ink-3" data-testid="goal-where">{where}</span>}
        </div>
        <div className="side-bar" role="progressbar" aria-label="Progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(donePct)} title={goal.detail ?? undefined}>
          <span className="d" style={{ width: `${donePct}%` }} />
          {total > 0 && barStages.map((s, i) => <span key={i} className={`n st-${s}`} data-testid="goal-bar-now" style={{ width: `${100 / total}%` }} />)}
        </div>
        {activity && (
          <div className="side-act" data-testid="goal-activity">
            <span className="min-w-0 truncate" data-testid="goal-activity-what">{activity.what}</span>
            {activity.eta && <span data-testid="goal-eta">{activity.eta}</span>}
          </div>
        )}
        </div>
        <div className="flex min-h-0 flex-col gap-1.5 overflow-auto" data-testid="goal-scroll">
        {total > 0 && folds ? (
          <PhaseFoldList folds={folds} open={foldOpen} onToggle={toggleFold} isDone={isDone} large={false} rowsClass="side-ms" row={sideRow} />
        ) : total > 0 ? (
          <ul className="side-ms" aria-label="Milestones">
            {milestones.map(sideRow)}
          </ul>
        ) : (
          <p className="px-0.5 text-[11px] text-ink-3">{finished ? "brindle finished this goal." : "No milestones yet."}</p>
        )}
        {docsLine}
        {closeSession}
        {step}
        {shownError && <p className={`${alert} text-xs`}>{shownError}</p>}
        {goal.note && <p className="px-0.5 text-[11px] text-ink-3">{goal.note}</p>}
        </div>
      </section>
    );
  }
  return (
    <section className={frame} aria-label="Goal" data-phase={phase}>
      <div className="flex items-center gap-s1">
        <span className="text-xs font-medium uppercase tracking-wider text-ink-3">Goal</span>
        <span className={`${pill} ${GOAL_PHASE_STYLE[phase]}`} data-testid="goal-phase">
          {phaseLabel}
        </span>
        {closeSession}
        <span className="flex-1" />
        <span className="text-xs text-ink-2">
          {passed} of {total} milestones complete
        </span>
      </div>
      {/* In the sidebar the pane's own title line names the goal already. */}
      {!compact && <h2 className="text-sm font-semibold leading-snug">{goal.goal ?? "Goal"}</h2>}
      {goal.detail && <p className="text-xs text-ink-2">{goal.detail}</p>}
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-canvas-2" role="progressbar" aria-label="Progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
        <div className="h-full rounded-full bg-pass" style={{ width: `${pct}%` }} />
      </div>
      {!open && current && (
        <p className="text-xs text-ink-3" data-testid="milestone-now">
          now: {current.position}. {current.title}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-1">
        {where && <span className="text-xs text-ink-3" data-testid="goal-where">{where}</span>}
        <span className="flex-1" />
        {/* A finished goal shows its milestones, unfolded: nothing to fold them for. */}
        {total > 0 && !finished && (
          <button type="button" className={`${quiet} whitespace-nowrap text-xs`} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
            {open ? "Hide milestones" : `Milestones (${total})`}
          </button>
        )}
      </div>
      {step}
      {shownError && <p className={alert}>{shownError}</p>}
      {goal.note && <p className="text-sm text-ink-3">{goal.note}</p>}
      {(open || finished) &&
        (total > 0 && folds ? (
          <div className="border-t border-line pt-1">
            <PhaseFoldList
              folds={folds}
              open={foldOpen}
              onToggle={toggleFold}
              isDone={isDone}
              large
              rowsClass="flex flex-col divide-y divide-line"
              row={(m) => <Milestone key={m.id} milestone={m} finished={finished} />}
            />
          </div>
        ) : total > 0 ? (
          <ul className="flex flex-col divide-y divide-line border-t border-line" aria-label="Milestones">
            {milestones.map((m) => (
              <Milestone key={m.id} milestone={m} finished={finished} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-ink-3">{finished ? "brindle finished this goal." : "No milestones yet."}</p>
        ))}
      {docsLine}
    </section>
  );
}
