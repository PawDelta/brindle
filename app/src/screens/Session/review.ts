/* A supervisor session's readiness for a PR and its diff, as the engine
   serves them (daemon/session_review.py), for the session's Review & open
   PR sheet (ReviewSheet.tsx) and, on an engine older than the goal's
   goal_phase, the goal panel's lifecycle (GoalPanel.goalPhase).

   Readiness is the engine's word alone: the app never judges a session
   ready from its own view of the milestones. GET /sessions/{root}/pr/
   readiness is read on mount and again whenever the bus says something of
   the session's changed (session.pr_ready when it flips, an edit from the
   sheet, autopilot activity, the PR job's end, or anything that leaves the
   store stale). An engine without the route, or a session the engine
   doesn't know, reads as unknown: the app then shows nothing of the flow.

   The diff comes with each file and hunk attributed to whoever wrote it;
   the app drops that (the person never sees which agent made what) and
   reshapes the files into screens/Branch/parseDiff's FileDiff, so the hunk
   logic (hunksOf, newSide, splice) is the one the review sheet has. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { type DaemonClient, type SessionDiff, type SessionFileDiff } from "../../api/client";
import type { BusEvent } from "../../api/events";
import type { AppStore } from "../../store/store";
import { newSide, type FileDiff, type Hunk } from "../Branch/parseDiff";

export interface Readiness {
  /* unknown: not read yet, or the engine can't say (no route, no session). */
  state: "unknown" | "ready" | "not_ready";
  reasons: string[];
  /* The PR the person opened, and how many session commits aren't pushed to it yet. */
  pr: { url: string; unpushed: number } | null;
}

export const UNKNOWN: Readiness = { state: "unknown", reasons: [], pr: null };

export const isGoalActivity = (e: BusEvent): boolean => e.kind.startsWith("autopilot.");

/* A bus event after which the session's readiness is read again. */
export function concernsReadiness(e: BusEvent, root: string): boolean {
  if (e.root_id !== null && e.root_id !== root && e.agent_id !== root) return false;
  if (e.kind.startsWith("session.pr") || e.kind === "session.edited" || e.kind.startsWith("autopilot.")) return true;
  return (e.kind === "job.done" || e.kind === "job.failed") && (e.payload.job_kind === "session.pr" || e.payload.job_kind === "goal.check");
}

/* The event that says the session just became ready: the calm notice's cue. */
export const isReadyEvent = (e: BusEvent, root: string): boolean => e.kind === "session.pr_ready" && (e.root_id === root || e.agent_id === root);

export function usePrReadiness(client: DaemonClient, root: string, store: AppStore): Readiness & { reload(): void } {
  const [readiness, setReadiness] = useState<Readiness>(UNKNOWN);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    let seq = 0;
    const load = () => {
      const mine = ++seq;
      client.prReadiness(root).then(
        (got) => live && mine === seq && setReadiness({ state: got.ready ? "ready" : "not_ready", reasons: got.reasons ?? [], pr: got.pr ?? null }),
        () => live && mine === seq && setReadiness(UNKNOWN),
      );
    };
    load();
    let seen = store.getState().lastEventId;
    let stale = store.getState().stale;
    let workspaces = store.getState().workspaces;
    const unsubscribe = store.subscribe(() => {
      const s = store.getState();
      // A git sweep (a commit in the checkout: one more not on an open PR) replaces a workspace's
      // metadata and keeps its agents, without a readiness event.
      const swept = s.workspaces.some((ws) => {
        const before = workspaces.find((old) => old.id === ws.id);
        return before && ws !== before && ws.agents === before.agents;
      });
      workspaces = s.workspaces;
      let changed = swept || (s.stale && !stale);
      if (s.lastEventId !== seen) {
        const fresh = s.recent.filter((e) => e.id > seen);
        seen = s.lastEventId;
        changed ||= fresh.some((e) => concernsReadiness(e, root));
      }
      stale = s.stale;
      if (changed) load();
    });
    return () => {
      live = false;
      unsubscribe();
    };
  }, [client, root, store, attempt]);
  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  return useMemo(() => ({ ...readiness, reload }), [readiness, reload]);
}

const STATUS: Record<string, FileDiff["status"]> = { A: "added", D: "deleted", R: "renamed" };

/* The engine's file, as the review sheet's hunk logic reads it (no blob:
   an edit is pinned to the file as read from the session's checkout). */
export function toFileDiff(f: SessionFileDiff): FileDiff {
  const lines = f.hunks.flatMap((h) => h.text.split("\n"));
  while (lines.at(-1) === "") lines.pop();
  return {
    path: f.path,
    oldPath: f.old_path ?? null,
    status: STATUS[f.status?.[0] ?? ""] ?? "modified",
    added: lines.filter((l) => l.startsWith("+")).length,
    removed: lines.filter((l) => l.startsWith("-")).length,
    binary: f.binary,
    lines,
    blob: null,
  };
}

/* Whether the diff's hunk still describes the file as the checkout has it:
   the hunk's new side is the file's lines from its new start. A hunk that
   doesn't match (the file moved on between the diff and the file being
   read) must not be spliced into the file: the diff is read again. */
export function hunkMatches(content: string, file: FileDiff, hunk: Hunk): boolean {
  const lines = (content.endsWith("\n") ? content.slice(0, -1) : content).split("\n");
  const expected = newSide(file, hunk);
  if (hunk.newStart - 1 + expected.length > lines.length && expected.length > 0) return false;
  return expected.every((line, i) => lines[hunk.newStart - 1 + i] === line);
}

export interface SessionDiffState {
  diff: SessionDiff | null;
  files: FileDiff[];
  error: string | null;
  /* The engine said the session isn't ready after all: its reasons. */
  notReady: string[] | null;
  reload(): void;
}

export { useSessionDiff } from "./sessionDiff";
