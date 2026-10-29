/* Whether this app and the engine it found can work together. GET /v1/meta
   reports the engine's api_version; the app supports a pinned range of it
   (>=1.0,<2). An older engine gets an upgrade offer and the app waits for
   it; a newer one still runs, with an "update the app" banner. */

import { DaemonError, type DaemonClient, type Meta } from "./client";

/* The API versions this app speaks: from MIN_API (inclusive) up to, not
   including, the next major. */
export const MIN_API: readonly [number, number] = [1, 0];
export const BELOW_API_MAJOR = 2;

export type Compatibility = "ok" | "engine-too-old" | "app-too-old";

/* An api_version that isn't "<major>.<minor>" (or none: an engine from before
   the daemon reported one) counts as too old. */
export function compatibility(apiVersion: string | undefined): Compatibility {
  const m = /^(\d+)\.(\d+)$/.exec(apiVersion?.trim() ?? "");
  if (!m) return "engine-too-old";
  const [major, minor] = [Number(m[1]), Number(m[2])];
  if (major < MIN_API[0] || (major === MIN_API[0] && minor < MIN_API[1])) return "engine-too-old";
  if (major >= BELOW_API_MAJOR) return "app-too-old";
  return "ok";
}

/* What the app knows about its engine, for the EngineBanner. */
export type EngineStatus =
  | { state: "connecting" }
  | { state: "ok"; meta: Meta }
  | { state: "upgrade-engine"; meta: Meta }
  | { state: "update-app"; meta: Meta }
  /* The daemon refused this app as one of an agent's processes (403 agent_caller). */
  | { state: "refused"; message: string }
  /* No daemon: not installed, didn't start, or gone. */
  | { state: "unreachable"; message: string };

const BY_COMPATIBILITY = {
  ok: "ok",
  "engine-too-old": "upgrade-engine",
  "app-too-old": "update-app",
} as const;

export function engineStatus(meta: Meta): EngineStatus {
  return { state: BY_COMPATIBILITY[compatibility(meta.api_version)], meta };
}

/* How the shell's start failure begins when the installed brindle predates
   `brindle daemon` (src-tauri/src/daemon.rs ENGINE_TOO_OLD). */
export const ENGINE_TOO_OLD = "engine too old";

/* How the shell's start failure begins while the daemon it started still runs
   but doesn't listen yet (src-tauri/src/daemon.rs ENGINE_STARTING): an
   engine's first start is slow, and that isn't a failure. */
export const ENGINE_STARTING = "engine starting";

const reasonOf = (e: unknown) => (e instanceof Error ? e.message : String(e));

/* `connect` (the shell's daemon_connect), asked again for as long as the shell
   answers that the engine is still starting; rejects only with a real failure
   (the engine exited, or none could be started), or once `live` says no one
   waits any more. */
export async function connectOnceStarted<T>(connect: () => Promise<T>, live: () => boolean = () => true, pause = 250): Promise<T> {
  for (;;) {
    try {
      return await connect();
    } catch (e) {
      if (!reasonOf(e).startsWith(`${ENGINE_STARTING}:`) || !live()) throw e;
    }
    await new Promise((resolve) => setTimeout(resolve, pause));
  }
}

/* What the shell's engine start is doing while connect waits
   (src-tauri/src/daemon.rs daemon_start_step), and for how many seconds:
   stopping the daemon of the build from before an update (or another
   install's), or waiting for the one it started to answer. */
export type StartStep = { step: "stopping" | "starting"; pid: number; secs: number };

/* "Starting brindle…" in the words of what the start is doing. */
export function startingLine(step: StartStep | null | undefined): string {
  if (!step) return "Starting brindle…";
  const took = step.secs >= 1 ? ` ${step.secs}s` : "";
  if (step.step === "stopping") return `Stopping the engine that ran before (pid ${step.pid})…${took}`;
  return `Starting the engine…${took}`;
}

/* The banner's upgrade for a daemon that couldn't start because its brindle
   is too old to have one, with the version the shell named; null for any
   other reason. No daemon answered, so nothing of its meta but the version. */
export function tooOldToStart(reason: string): EngineStatus | null {
  if (!reason.startsWith(`${ENGINE_TOO_OLD}:`)) return null;
  const version = /^engine too old: brindle (\S+) /.exec(reason)?.[1] ?? "(version unknown)";
  return { state: "upgrade-engine", meta: { engine_version: version, api_version: "", pid: 0, db_path: "", tmux: false, airgap: false } };
}

/* Ask the daemon for its meta and judge it. Never rejects. */
export async function checkEngine(client: DaemonClient): Promise<EngineStatus> {
  try {
    return engineStatus(await client.meta());
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    if (e instanceof DaemonError && e.code === "agent_caller") return { state: "refused", message };
    return { state: "unreachable", message };
  }
}
