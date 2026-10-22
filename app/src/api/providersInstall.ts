/* Follows provider installs (POST /providers/{p}/install, a job each: the
   maker's official installer, run as-is): the route answers a job id, then
   job.progress with each line the installer prints (`text`; `line` from
   older fakes) and finally job.done or job.failed (payload.job = that id)
   arrive on the event stream, which the store keeps in `recent` (only the
   last few). One state per provider; several installs can run at once.
   Every new event is looked at as it lands; while an install's 202 is
   still on its way, the job events no install claims are held here, outside
   the store's short history, and replayed for the install whose job they
   turn out to be: one that ends before its 202 arrives still ends. */

import { useCallback, useContext, useEffect, useRef, useState } from "react";
import type { DaemonClient } from "./client";
import type { BusEvent } from "./events";
import { StoreContext } from "../store/useStore";

export interface Install {
  status: "starting" | "running" | "done" | "failed";
  job?: string;
  lines: string[];
  error?: string;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));
/* What a progress event says: an installer's line (or a job's text). */
export const lineOf = (payload: Record<string, unknown>): string | null =>
  typeof payload.line === "string" ? payload.line : typeof payload.text === "string" ? payload.text : null;

/* An install being followed: its job, once the 202 has said it. */
interface Follow {
  job?: string;
}

export function useInstalls(client: DaemonClient, onDone?: (provider: string) => void) {
  const store = useContext(StoreContext);
  if (!store) throw new Error("useInstalls needs a <StoreContext.Provider>");
  const [installs, setInstalls] = useState<Record<string, Install>>({});
  const follows = useRef<Record<string, Follow>>({});
  /* The newest event already looked at, and the job events held for an install whose 202 is on its way. */
  const seen = useRef(0);
  const held = useRef<BusEvent[]>([]);
  const done = useRef(onDone);
  done.current = onDone;

  const set = (provider: string, change: (i: Install) => Install) => setInstalls((all) => (all[provider] ? { ...all, [provider]: change(all[provider]) } : all));

  /* One event of `provider`'s job, applied to its install. */
  const apply = useCallback((provider: string, e: BusEvent) => {
    if (e.kind === "job.progress") {
      const line = lineOf(e.payload);
      if (line) set(provider, (i) => ({ ...i, lines: [...i.lines, line] }));
    } else if (e.kind === "job.done") {
      delete follows.current[provider];
      set(provider, (i) => ({ ...i, status: "done" }));
      done.current?.(provider);
    } else if (e.kind === "job.failed") {
      delete follows.current[provider];
      const err = e.payload.error as { message?: string } | undefined;
      set(provider, (i) => ({ ...i, status: "failed", error: err?.message ?? "the installer failed" }));
    }
  }, []);

  const waiting = () => Object.values(follows.current).some((f) => !f.job);

  // Every event since the last look (the store calls this on each one), to the install whose job it is.
  const check = useCallback(() => {
    if (Object.keys(follows.current).length === 0) return;
    const fresh = store.getState().recent.filter((e) => e.id > seen.current);
    if (fresh.length === 0) return;
    seen.current = fresh.at(-1)!.id;
    for (const e of fresh) {
      if (!e.kind.startsWith("job.") || typeof e.payload.job !== "string") continue;
      const provider = Object.keys(follows.current).find((p) => follows.current[p].job === e.payload.job);
      if (provider) apply(provider, e);
      // Nobody's job yet, while a 202 is on its way: it may be that install's.
      else if (waiting()) held.current.push(e);
    }
  }, [store, apply]);

  useEffect(() => store.subscribe(check), [store, check]);

  /* A start ended (its 202, or its refusal): what was held for it is replayed; nothing is held once no 202 is on its way. */
  const settled = (provider: string, job: string | null) => {
    if (job) {
      const mine = held.current.filter((e) => e.payload.job === job);
      held.current = held.current.filter((e) => e.payload.job !== job);
      for (const e of mine) if (follows.current[provider]?.job === job) apply(provider, e);
    }
    if (!waiting()) held.current = [];
  };

  const start = useCallback(
    async (provider: string) => {
      // Looked at from here on: a quick install can end before its 202 arrives.
      if (Object.keys(follows.current).length === 0) seen.current = store.getState().lastEventId;
      const follow: Follow = {};
      follows.current[provider] = follow;
      setInstalls((all) => ({ ...all, [provider]: { status: "starting", lines: [] } }));
      try {
        const { job } = await client.installProvider(provider);
        if (follows.current[provider] !== follow) return settled(provider, null);
        // Anything not looked at yet first, while this install still counts as waiting.
        check();
        follow.job = job;
        setInstalls((all) => ({ ...all, [provider]: { ...all[provider], status: "running", job } }));
        settled(provider, job);
      } catch (e) {
        if (follows.current[provider] !== follow) return settled(provider, null);
        delete follows.current[provider];
        setInstalls((all) => ({ ...all, [provider]: { ...all[provider], status: "failed", error: message(e) } }));
        settled(provider, null);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, store, check],
  );

  const forget = useCallback((provider: string) => {
    delete follows.current[provider];
    if (!waiting()) held.current = [];
    setInstalls((all) => {
      const rest = { ...all };
      delete rest[provider];
      return rest;
    });
  }, []);

  /* Lets every finished install go (its CLI is in the rows now). */
  const settle = useCallback(() => {
    setInstalls((all) => (Object.values(all).some((i) => i.status === "done") ? Object.fromEntries(Object.entries(all).filter(([, i]) => i.status !== "done")) : all));
  }, []);

  return { installs, start, forget, settle };
}
