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
import { DaemonError, type DaemonClient, type SessionDiff, type SessionFileDiff } from "../../api/client";
import type { BusEvent } from "../../api/events";
import type { AppStore } from "../../store/store";
import { newSide, type FileDiff, type Hunk } from "../Branch/parseDiff";

export interface Readiness {
  /* unknown: not read yet, or the engine can't say (no route, no session). */
  state: "unknown" | "ready" | "not_ready";
  reasons: string[];
  /* The PR brindle opened itself, when the engine's auto-PR is on. */
  pr: { url: string } | null;
}

export const UNKNOWN: Readiness = { state: "unknown", reasons: [], pr: null };

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
    const unsubscribe = store.subscribe(() => {
      const s = store.getState();
      if (s.lastEventId !== seen) {
        const fresh = s.recent.filter((e) => e.id > seen);
        seen = s.lastEventId;
        if (fresh.some((e) => concernsReadiness(e, root))) load();
      }
      if (s.stale && !stale) load();
      stale = s.stale;
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

export function useSessionDiff(client: DaemonClient, root: string, store: AppStore): SessionDiffState {
  const [snapshot, setSnapshot] = useState<{ root: string; diff: SessionDiff | null; error: string | null; notReady: string[] | null } | null>(null);
  const current = snapshot?.root === root ? snapshot : null;
  const diff = current?.diff ?? null;
  const error = current?.error ?? null;
  const notReady = current?.notReady ?? null;
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  useEffect(() => {
    let previous = store.getState();
    return store.subscribe(() => {
      const next = store.getState();
      const fresh = next.recent.filter((e) => e.id > previous.lastEventId);
      // A git sweep replaces workspace metadata while retaining its agents.
      // Snapshot updates following an event have already been covered by that event.
      const swept = next.workspaces.some((ws) => {
        const before = previous.workspaces.find((old) => old.id === ws.id);
        return before && ws !== before && ws.agents === before.agents;
      });
      const changed = swept ||
        (next.stale && !previous.stale) ||
        fresh.some((e) => concernsReadiness(e, root));
      previous = next;
      if (changed) reload();
    });
  }, [store, root, reload]);
  useEffect(() => {
    let live = true;
    client.sessionDiff(root).then(
      (d) => {
        if (!live) return;
        setSnapshot({ root, diff: d, error: null, notReady: null });
      },
      (e: unknown) => {
        if (!live) return;
        // A diff already shown stays shown: the engine judging the session again (after an edit made here, say)
        // is said quietly beside it, not by taking it away.
        const refused = e instanceof DaemonError && e.code === "not_ready";
        setSnapshot((previous) => ({
          root,
          diff: previous?.root === root ? previous.diff : null,
          error: refused ? null : e instanceof Error ? e.message : String(e),
          notReady: refused ? e.reasons ?? [e.message] : null,
        }));
      },
    );
    return () => {
      live = false;
    };
  }, [client, root, attempt]);
  const files = useMemo(() => (diff?.files ?? []).map(toFileDiff), [diff]);
  return { diff, files, error, notReady, reload };
}

/* Whether the person's settings let brindle open the session's PR on its
   own: the repo's `auto_pr` (engine branch desktop/auto-pr; read from GET
   /config's repo settings, false until it lands). */
export const autoPrOn = (config: { repo?: Record<string, unknown> } | null | undefined): boolean => config?.repo?.auto_pr === true;

export function useAutoPr(client: DaemonClient, repo: string): boolean {
  const [on, setOn] = useState(false);
  useEffect(() => {
    let live = true;
    client.config(repo).then(
      (c) => live && setOn(autoPrOn(c)),
      () => live && setOn(false),
    );
    return () => {
      live = false;
    };
  }, [client, repo]);
  return on;
}
