/* A practice run's session (the demo's, in brindle's own practice repo:
   RepoDTO.demo): a step strip over the supervisor's terminal (Plan, Build,
   Review, Merge, read from the session's goal and its agents), and in the
   session's bar "End practice run" (DELETE /demo, which ends the run and
   removes its repo) until it is done; done, "Start your own" with Open a
   folder… and Clone a repository…, which take the person to New session
   with that way of adding a project open. */

import { useContext, useState } from "react";
import type { DaemonClient } from "../../api/client";
import { LoadingButton } from "../../components/LoadingButton";
import { button, label, main, quiet } from "../../components/ui";
import { SessionsContext } from "../../store/sessions";
import { useStore } from "../../store/useStore";
import { completion, goalPhase, useGoal } from "./GoalPanel";
import { UNKNOWN } from "./review";

const STEPS = [
  { name: "Plan", detail: "The supervisor reads the goal and splits it" },
  { name: "Build", detail: "Two developers, a branch each, side by side" },
  { name: "Review", detail: "A reviewer checks each branch" },
  { name: "Merge", detail: "Milestones turn green as tests pass" },
];

/* Where a practice run is: `step` 0-3 (Plan to Merge), `done` once its goal is reached; null for any other session. */
export function usePractice(client: DaemonClient, repo: string, root: string, workers: number): { step: number; done: boolean } | null {
  const repos = useContext(SessionsContext)?.repos;
  const practice = !!repos?.find((r) => r.repo_root === repo)?.demo;
  // Only a practice run's goal is read here (the sidebar reads every session's anyway, through the same cache).
  const goal = useGoal(practice ? client : null, repo, root, useStore());
  if (!practice) return null;
  const { passed, total } = completion(goal?.milestones ?? undefined);
  const done = !!goal && (goalPhase(goal, UNKNOWN).phase === "done" || (total > 0 && passed === total));
  if (done) return { step: STEPS.length, done };
  switch (goal?.stage) {
    case "merging":
      return { step: 3, done };
    case "review":
      return { step: 2, done };
  }
  // The engine says "working" for a supervisor at work before any worker exists (autopilot's stage):
  // that is still Plan; Build starts with the first worker handed a milestone, or a milestone passing.
  return { step: workers > 0 || passed > 0 ? 1 : 0, done };
}

/* The strip over the terminal: the step under way lit, those behind it ticked. */
export function PracticeSteps({ step, done }: { step: number; done: boolean }) {
  return (
    <div className={`grid shrink-0 grid-cols-[210px_repeat(4,minmax(0,1fr))] border-b border-line ${done ? "bg-pass/5" : "bg-branch/4"}`} data-testid="practice-steps" data-state={done ? "done" : "running"}>
      <div className="flex flex-col justify-center gap-0.5 border-r border-line px-4 py-3">
        <span className={`${label} !text-pass`}>{done ? "Practice run · done" : "Practice run"}</span>
        <span className="text-xs text-ink-3">Sample repo · your projects {done ? "weren't" : "aren't"} touched</span>
      </div>
      <ol className="contents" aria-label="Practice run steps">
        {STEPS.map((s, i) => {
          const state = i < step ? "ok" : i === step ? "on" : "todo";
          return (
            <li
              key={s.name}
              className={`grid grid-cols-[22px_minmax(0,1fr)] content-center gap-x-2.5 border-r border-line px-4 py-3 last:border-r-0 ${state === "on" ? "shadow-[inset_0_-2px_0_var(--branch)]" : ""}`}
              data-state={state}
              aria-current={state === "on" ? "step" : undefined}
            >
              <span
                className={`row-span-2 grid size-[22px] place-items-center rounded-full border-[1.5px] font-mono text-[10.5px] font-semibold ${
                  state === "ok" ? "border-pass bg-pass-soft text-pass" : state === "on" ? "border-branch text-branch" : "border-line-strong text-ink-3"
                }`}
                aria-hidden
              >
                {state === "ok" ? "✓" : i + 1}
              </span>
              <span className={`text-[13px] font-medium ${state === "todo" ? "text-ink-2" : "text-ink"}`}>{s.name}</span>
              <span className="text-[11.5px] leading-snug text-ink-3">{s.detail}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/* The bar's practice-run actions: End practice run while it runs; done, Start your own. */
export function PracticeActions({ client, done, onEnded, onStartOwn }: { client: DaemonClient; done: boolean; onEnded(): Promise<void> | void; onStartOwn?(add: "open" | "clone"): void }) {
  const [failed, setFailed] = useState<string | null>(null);
  if (done) {
    return (
      <span className="flex items-center gap-s1">
        <span className="text-xs text-ink-3">Start your own:</span>
        <button type="button" className={main} onClick={() => onStartOwn?.("open")}>
          Open a folder…
        </button>
        <button type="button" className={button} onClick={() => onStartOwn?.("clone")}>
          Clone a repository…
        </button>
      </span>
    );
  }
  const end = async () => {
    setFailed(null);
    try {
      await client.cancelDemo();
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
      return;
    }
    await onEnded();
  };
  return (
    <>
      <LoadingButton className={quiet} onClick={end}>
        End practice run
      </LoadingButton>
      {failed && (
        <span role="alert" className="text-xs text-sap">
          {failed}
        </span>
      )}
    </>
  );
}
