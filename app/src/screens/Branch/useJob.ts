/* Follows one long action: the route answers a job id at once, then
   job.progress lines and finally job.done or job.failed (payload.job = that
   id) arrive on the event stream, which the store keeps in `recent`. */

import { useContext, useEffect, useRef, useState } from "react";
import type { Job } from "../../api/client";
import { StoreContext } from "../../store/useStore";

export interface JobState {
  label: string;
  status: "starting" | "running" | "done" | "failed";
  id?: string;
  progress: string[];
  /* job.done's payload.result, or the failure's message. */
  result?: unknown;
  error?: string;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function useJob() {
  const store = useContext(StoreContext);
  if (!store) throw new Error("useJob needs a <StoreContext.Provider>");
  const [job, setJob] = useState<JobState | null>(null);
  /* The newest event id already looked at, and what to do when the job ends. */
  const seen = useRef(0);
  const onDone = useRef<((result: unknown) => void) | undefined>(undefined);

  const start = async (label: string, run: () => Promise<Job>, done?: (result: unknown) => void) => {
    // Taken before the request: a quick job can end before its 202 arrives.
    seen.current = store.getState().lastEventId;
    onDone.current = done;
    setJob({ label, status: "starting", progress: [] });
    try {
      const started = await run();
      setJob({ label, status: "running", id: started.job, progress: [] });
    } catch (e) {
      setJob({ label, status: "failed", progress: [], error: message(e) });
    }
  };

  const id = job?.status === "running" ? job.id : undefined;
  useEffect(() => {
    if (!id) return;
    const check = () => {
      const fresh = store.getState().recent.filter((e) => e.id > seen.current);
      if (fresh.length === 0) return;
      seen.current = fresh.at(-1)!.id;
      for (const e of fresh) {
        if (e.payload.job !== id) continue;
        if (e.kind === "job.progress") {
          // A job's text, a URL to open, or a line a command printed (git's, an installer's).
          const line = typeof e.payload.text === "string" ? e.payload.text : typeof e.payload.url === "string" ? e.payload.url : typeof e.payload.line === "string" ? e.payload.line : null;
          if (line) setJob((j) => (j && j.id === id ? { ...j, progress: [...j.progress, line] } : j));
        } else if (e.kind === "job.done") {
          setJob((j) => (j && j.id === id ? { ...j, status: "done", result: e.payload.result } : j));
          onDone.current?.(e.payload.result);
        } else if (e.kind === "job.failed") {
          const err = e.payload.error as { message?: string } | undefined;
          setJob((j) => (j && j.id === id ? { ...j, status: "failed", error: err?.message ?? "the job failed" } : j));
        }
      }
    };
    check();
    return store.subscribe(check);
  }, [id, store]);

  const busy = job?.status === "starting" || job?.status === "running";
  return { job, busy, start, clear: () => setJob(null) };
}
