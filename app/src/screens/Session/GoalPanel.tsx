/* The goal panel, above a session's agent tree: the session's goal and its
   milestones with their check commands and last results, and autopilot
   on / off (not offered for a paused session: nothing is driving it). The
   supervisor runs the checks; the panel only shows when they last ran.

   The goal is not part of the store's snapshot, so the panel fetches
   GET /v1/goal for its session (`root`) itself. It refetches after an action is
   acknowledged, when a goal check's job ends (job.done / job.failed), and when
   the event stream reports autopilot activity or a resnapshot. Completion comes
   from the milestones' statuses: GoalDTO.progress is an activity counter.

   The panel shows the session's one lifecycle, the engine's goal_phase: In
   progress, In review, Ready for PR (with the panel's own Review & open PR,
   the one way to the review sheet), PR open (a link to it), Done. Done keeps
   the goal and every milestone, all checked, as the engine serves them
   (it keeps a reached goal until a new one replaces it), and beside it a
   button Close session (POST /sessions/{root}/close, one click, nothing
   asked: the session goes, its branches stay). An older engine sends no
   goal_phase: the PR readiness (review.ts) and the milestones stand in for
   it. Nothing here ever says "failed".

   Only the newest GET answers: a slower, older response is dropped, and so is
   any response that arrives after the panel has moved to another session. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { BusEvent } from "../../api/events";
import type { DaemonClient } from "../../api/client";
import { openExternal, type OpenUrl } from "../../api/open";
import type { GoalDTO, MilestoneDTO } from "../../api/schema";
import { alert, button, card, mainTone, mono, pill, quiet } from "../../components/ui";
import type { AppStore } from "../../store/store";
import { usePrReadiness, type Readiness } from "./review";

const AUTOPILOT_EVENT = /^autopilot\./;
const GOAL_JOBS = new Set(["goal.check", "session.pr"]);

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

/* A bus event that means the goal may have changed: autopilot activity for this
   session (or any session's, when the event names none), its PR's readiness or
   job, or a finished check. */
function concernsGoal(e: BusEvent, root: string): boolean {
  if (e.root_id !== null && e.root_id !== root) return false;
  return AUTOPILOT_EVENT.test(e.kind) || e.kind.startsWith("session.pr") || isGoalJobDone(e);
}

export type GoalPhase = "in_progress" | "in_review" | "ready_for_pr" | "pr_open" | "done";
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
  /* A paused session: its goal is shown, but autopilot isn't offered. */
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
}

export function GoalPanel({ client, repo, root, store, paused = false, compact = false, onReview, openUrl = openExternal, onCloseSession }: GoalPanelProps) {
  const [goal, setGoal] = useState<GoalDTO | null>(null);
  // An older engine's stand-in for goal_phase: its word on the session's PR.
  const readiness = usePrReadiness(client, root, store);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [enabling, setEnabling] = useState(false);
  // Beside a terminal the milestones fold away by default (it wants the room); in the sidebar they show.
  const [open, setOpen] = useState(compact);
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
    setEnabling(false);
    void refresh();
    let seen = store.getState().lastEventId;
    let stale = store.getState().stale;
    const unsubscribe = store.subscribe(() => {
      const s = store.getState();
      if (s.lastEventId !== seen) {
        const fresh = s.recent.filter((e) => e.id > seen);
        seen = s.lastEventId;
        if (fresh.some((e) => concernsGoal(e, root))) void refresh();
      }
      if (s.stale && !stale) void refresh();
      stale = s.stale;
    });
    return () => {
      activeSession.current = null;
      unsubscribe();
    };
  }, [session, store, refresh, root]);

  const toggleAutopilot = () => {
    if (!goal) return;
    setActionError(null);
    setEnabling(true);
    client
      .setGoalEnabled(root, !goal.enabled)
      // The switch stays disabled until the enabled state is reloaded.
      .then(() => (activeSession.current === session ? refresh() : undefined))
      .catch((e: unknown) => {
        if (activeSession.current === session) setActionError(message(e));
      })
      .finally(() => {
        if (activeSession.current === session) setEnabling(false);
      });
  };

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
  const phaseLabel = GOAL_PHASE_LABEL[phase];
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
  /* Done's step, beside the Done status: close the session, one click, nothing asked. */
  const closeSession =
    finished && onCloseSession ? (
      <button
        type="button"
        className={`inline-flex shrink-0 items-center whitespace-nowrap rounded-md border px-s1 py-0.5 ${mainTone}`}
        data-testid="goal-close"
        title="Stop and hide the whole session; its branches stay"
        onClick={onCloseSession}
      >
        Close session
      </button>
    ) : null;
  if (compact) {
    // The sidebar's goal: a ring and completed count, the milestones as a list (✓ done, every unfinished one lit),
    // then when the supervisor last checked, with the autopilot switch beside it.
    const lastCheck = Math.max(0, ...milestones.map((m) => m.checked_at ?? 0));
    const donePct = finished ? 100 : total === 0 ? 0 : (passed / total) * 100;
    const nowPct = finished ? 100 : total === 0 ? 0 : (Math.min(passed + 1, total) / total) * 100;
    // Every unfinished milestone is in progress (the engine works them in parallel), so each one is lit.
    const stateOf = (m: MilestoneDTO): "done" | "now" => (finished || checkLabel(m) === "Done" ? "done" : "now");
    return (
      <section className={frame} aria-label="Goal" data-phase={phase}>
        <div className="flex items-center gap-s1 px-0.5 text-xs font-medium text-ink">
          <span className="side-ring" style={{ background: `conic-gradient(var(--pass) 0 ${donePct}%, var(--branch) ${donePct}% ${nowPct}%, var(--line) ${nowPct}% 100%)` }} aria-hidden />
          <span data-testid="goal-line">
            {total > 0 ? `${passed} of ${total} milestones complete · ` : ""}
            <span data-testid="goal-phase" className={finished || phase === "ready_for_pr" ? "text-pass" : undefined}>
              {phaseLabel}
            </span>
          </span>
          {closeSession}
          <span className="flex-1" />
        </div>
        {total > 0 ? (
          <ul className="side-ms" aria-label="Milestones">
            {milestones.map((m) => {
              const state = stateOf(m);
              const label = finished ? "Done" : checkLabel(m);
              return (
                <li key={m.id} className="m" data-state={state} data-testid="milestone" title={m.state_why ?? m.check_cmd ?? undefined}>
                  <span className="i" aria-hidden>
                    {state === "done" ? "✓" : m.position}
                  </span>
                  <span className="t">
                    {m.title}
                    {(label || (m.id === current?.id && (m.detail || m.check_cmd))) && (
                      <small>{[label, m.id === current?.id ? m.detail || m.check_cmd : null].filter(Boolean).join(" · ")}</small>
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="px-0.5 text-[11px] text-ink-3">{finished ? "brindle finished this goal." : "No milestones yet."}</p>
        )}
        {step}
        <div className="flex items-center gap-1.5 whitespace-nowrap px-1.5 pt-0.5 text-[11px] text-ink-3" title={goal.detail ?? undefined}>
          <span className="min-w-0 truncate">Checked {lastCheck > 0 ? formatAgo(lastCheck) : "never"}</span>
          {!paused && (
            <button type="button" role="switch" aria-checked={goal.enabled} className="shrink-0 rounded-sm hover:text-ink disabled:opacity-50" disabled={enabling} title={goal.enabled ? "Autopilot is on: brindle drives the session. Click to turn it off." : "Autopilot is off. Click to let brindle drive the session."} onClick={toggleAutopilot}>
              · autopilot {goal.enabled ? "on" : "off"}
            </button>
          )}
        </div>
        {shownError && <p className={`${alert} text-xs`}>{shownError}</p>}
        {goal.note && <p className="px-0.5 text-[11px] text-ink-3">{goal.note}</p>}
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
        {!paused && (
          <button
            type="button"
            role="switch"
            aria-checked={goal.enabled}
            className={`${button} whitespace-nowrap px-s1 py-0.5 text-xs`}
            disabled={enabling}
            onClick={toggleAutopilot}
          >
            Autopilot {goal.enabled ? "on" : "off"}
          </button>
        )}
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
        (total > 0 ? (
          <ul className="flex flex-col divide-y divide-line border-t border-line" aria-label="Milestones">
            {milestones.map((m) => (
              <Milestone key={m.id} milestone={m} finished={finished} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-ink-3">{finished ? "brindle finished this goal." : "No milestones yet."}</p>
        ))}
    </section>
  );
}
