/* The engine's upgrade, in the background: POST /v1/engine/upgrade runs
   `uv tool upgrade brindle` on the engine itself and answers a job
   (routes_actions.upgrade_engine: 202 {job, kind "engine.upgrade"}; a press
   while one runs gets that job back). The card follows the job on the bus:
   job.progress (stage "upgrading") while uv runs, then job.done, or
   job.failed with uv's last lines in the error's `tail` (the only time any
   of its output goes on the bus). No terminal is ever shown or read: how
   it went comes from those events alone, and until one arrives the card
   says it is running. Done, the engine restarts itself and the app
   reconnects to the new one. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { DaemonClient } from "../api/client";
import type { BusEvent } from "../api/events";
import type { AppStore } from "../store/store";
import { alert, button, mono, notice, quiet } from "./ui";

export type UpgradeState =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "running"; job: string; stage: string | null }
  | { kind: "done" }
  | { kind: "failed"; lines: string[]; message: string };

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/* The upgrade's state, started with `start()` and followed on the store's bus events. */
export function useEngineUpgrade(client: DaemonClient, store: AppStore) {
  const [state, setState] = useState<UpgradeState>({ kind: "idle" });
  /* The newest bus event id already looked at, taken before the request. */
  const seen = useRef(0);

  const start = useCallback(async () => {
    seen.current = store.getState().lastEventId;
    setState({ kind: "starting" });
    let job: string;
    try {
      job = (await client.upgradeEngine()).job;
    } catch (e) {
      setState({ kind: "failed", lines: [], message: message(e) });
      return;
    }
    setState({ kind: "running", job, stage: null });
  }, [client, store]);

  // The job's events, from the ones that arrived since the request: a stage, the end, or the failure with uv's tail.
  const job = state.kind === "running" ? state.job : null;
  useEffect(() => {
    if (!job) return;
    const apply = (e: BusEvent) => {
      if (e.payload.job !== job) return;
      if (e.kind === "job.progress") {
        const stage = typeof e.payload.stage === "string" ? e.payload.stage : typeof e.payload.text === "string" ? e.payload.text : null;
        setState((s) => (s.kind === "running" ? { ...s, stage } : s));
      } else if (e.kind === "job.done") setState({ kind: "done" });
      else if (e.kind === "job.failed") {
        const error = (e.payload.error ?? {}) as { message?: string; tail?: unknown };
        const lines = Array.isArray(error.tail) ? error.tail.map(String) : [];
        setState({ kind: "failed", lines, message: error.message ?? "uv tool upgrade brindle didn't finish" });
      }
    };
    const check = () => {
      const fresh = store.getState().recent.filter((e) => e.id > seen.current);
      if (fresh.length === 0) return;
      seen.current = fresh.at(-1)!.id;
      fresh.forEach(apply);
    };
    check();
    return store.subscribe(check);
  }, [job, store]);

  const dismiss = useCallback(() => setState({ kind: "idle" }), []);
  return { state, start, dismiss };
}

/* The card: nothing while idle. */
export function UpgradeCard({ state, onDismiss }: { state: UpgradeState; onDismiss(): void }) {
  if (state.kind === "idle") return null;
  const running = state.kind === "starting" || state.kind === "running";
  return (
    <div className={`flex flex-col gap-1 rounded-md px-s2 py-s1 text-sm ${state.kind === "failed" ? alert : state.kind === "done" ? notice : "bg-canvas-2"}`} role="status" aria-label="Engine update" data-testid="upgrade-card" data-state={state.kind}>
      <div className="flex flex-wrap items-center gap-s1">
        {running && <span className="size-3 shrink-0 animate-spin rounded-full border-2 border-branch border-t-transparent" aria-hidden />}
        {state.kind === "starting" && <span>Starting the update…</span>}
        {state.kind === "running" && <span>Updating brindle…{state.stage && <span className="ml-s1 text-ink-3">{state.stage}</span>}</span>}
        {state.kind === "done" && <span>Updated. The engine restarts itself; the app reconnects to the new one in a moment.</span>}
        {state.kind === "failed" && <span>The update didn't finish: {state.message}</span>}
        <span className="flex-1" />
        {!running && (
          <button type="button" className={state.kind === "failed" ? button : quiet} onClick={onDismiss}>
            Dismiss
          </button>
        )}
      </div>
      {state.kind === "failed" && state.lines.length > 0 && (
        <pre className={`max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-canvas-2 px-s2 py-1 ${mono} text-ink-2`} data-testid="upgrade-log">
          {state.lines.join("\n")}
        </pre>
      )}
    </div>
  );
}
