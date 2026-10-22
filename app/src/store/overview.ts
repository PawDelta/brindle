/* How a session and its agents read at a glance, from what the daemon
   serves: the session rows (GET /sessions), the snapshot's agents and the
   liveness sweep (the store), and the inbox. Shared by the sidebar's tree,
   the Sessions screen and the session view, so a status means the same
   thing everywhere. */

import type { BusEvent } from "../api/events";
import type { AgentDTO, InboxItemDTO, SessionDTO, WorkspaceDTO } from "../api/schema";

/* GET /sessions lists a failed root with the paused ones (brindle/sessions.py);
   the app tells them apart. */
export const isPaused = (s: SessionDTO) => s.status === "paused" || s.status === "failed";
export const isFailed = (s: SessionDTO) => s.status === "failed";

const STOPPED = new Set(["paused", "done", "failed"]);

/* Every agent a session started, at any depth (by parent_id). */
export function descendants(agents: AgentDTO[], rootId: string): AgentDTO[] {
  const out: AgentDTO[] = [];
  const seen = new Set([rootId]);
  for (let frontier = [rootId]; frontier.length > 0; ) {
    const next = agents.filter((a) => a.parent_id && frontier.includes(a.parent_id) && !seen.has(a.id));
    for (const a of next) seen.add(a.id);
    out.push(...next);
    frontier = next.map((a) => a.id);
  }
  return out;
}

/* The workers still at work under a session, as brindle/sessions.py live()
   counts them: not interactive, not stopped, and alive. */
export function liveWorkers(members: AgentDTO[], alive: Record<string, boolean>): number {
  return members.filter((a) => a.mode !== "interactive" && !STOPPED.has(a.status) && alive[a.id] !== false).length;
}

/* The session an agent belongs to: the top of its parent chain in the
   snapshot (itself when it has no parent), or null when it isn't in it.
   `hidden` (store/sessions.tsx) carries the parents of agents the snapshot
   leaves out (closed ones), so the chain crosses them, as the sidebar's
   tree does. */
export function rootOf(agents: AgentDTO[], id: string, hidden: Record<string, string | null> = {}): string | null {
  const byId = new Map<string, AgentDTO>(agents.map((a) => [a.id, a]));
  const seen = new Set<string>();
  const start = byId.get(id);
  if (!start) return null;
  let current: AgentDTO = start;
  while (current.parent_id && !seen.has(current.id)) {
    seen.add(current.id);
    let next: string | null = current.parent_id;
    while (next !== null && !byId.has(next) && next in hidden && !seen.has(next)) {
      seen.add(next);
      next = hidden[next];
    }
    const parent: AgentDTO | undefined = next === null ? undefined : byId.get(next);
    if (!parent) break;
    current = parent;
  }
  return current.id;
}

/* The inbox items that concern any of `ids`. */
export const needsYou = (inbox: InboxItemDTO[], ids: Set<string>): InboxItemDTO[] =>
  inbox.filter((i) => i.agent_id != null && ids.has(i.agent_id));

/* What to call an agent's state: locked, exited, or its status. */
export function agentState(agent: AgentDTO, alive: boolean | undefined): string {
  if (agent.locked) return `locked by ${agent.locked}`;
  if (alive === false) return "exited";
  return agent.status;
}

/* The colour a status reads as: attention (waiting on a person, failed),
   busy (at work), idle (ready for a message), off (stopped or gone). */
export type Tone = "attention" | "busy" | "idle" | "off";

export function tone(status: string, alive?: boolean): Tone {
  if (alive === false) return "off";
  if (status === "waiting" || status === "failed") return "attention";
  if (status === "processing" || status === "starting") return "busy";
  if (STOPPED.has(status) || status === "exited") return "off";
  return "idle";
}

export const DOT: Record<Tone, string> = {
  attention: "bg-sap",
  busy: "bg-pass",
  idle: "bg-fawn",
  off: "bg-ink-3",
};

/* The CLI sidebar's vocabulary for an agent's row (brindle/watch.py
   STATUS_LABEL and STATUS_STYLE, service/inbox.py needs_you): a glyph, a
   label and a style, the same here so a row reads the same in both. */
export type CliStyle = "busy" | "ok" | "alert" | "bad" | "dim" | "normal";

export interface CliStatus {
  glyph: string;
  label: string;
  style: CliStyle;
  /* The row needs a person (a "◆" row in the CLI). */
  needs: boolean;
}

const STATUS_LABEL: Record<string, [string, string, CliStyle]> = {
  processing: ["●", "working", "busy"],
  starting: ["◌", "starting up", "busy"],
  idle: ["○", "idle", "ok"],
  waiting: ["◆", "needs you", "alert"],
  failed: ["◆", "credentials need attention", "alert"],
  exited: ["✕", "stopped", "bad"],
  paused: ["‖", "paused", "dim"],
  done: ["✓", "done", "ok"],
};

const isWorker = (a: AgentDTO) => !!a.parent_id && a.mode !== "review";

type ReviewFields = Pick<WorkspaceDTO, "review" | "review_state" | "asking" | "autopilot">;

/* service/inbox.py awaiting_review: how a worker that reported and hasn't
   been approved yet reads, or null. */
export function awaitingReview(a: AgentDTO, ws: ReviewFields): string | null {
  if (!isWorker(a) || !a.reported || a.status === "processing" || a.status === "starting") return null;
  if (ws.review_state && !ws.review_state.startsWith("approved")) return ws.review_state;
  if (ws.review === "approved") return null;
  return ws.review === "changes" ? "changes requested" : "to review";
}

/* service/inbox.py needs_you: why a person must act on the agent's row, or null. */
export function needsYouReason(a: AgentDTO, ws: ReviewFields): string | null {
  if (a.status === "waiting") return "needs you";
  if (a.status === "failed") return "credentials need attention";
  if (ws.asking === a.id) return "has a question";
  return ws.autopilot ? null : awaitingReview(a, ws);
}

/* The agent's row as the CLI draws it: "● working", "◆ needs you", "◇ to review" ... */
export function cliStatus(a: AgentDTO, ws: ReviewFields, alive?: boolean): CliStatus {
  if (a.locked) return { glyph: "·", label: `locked by ${a.locked}`, style: "dim", needs: false };
  const status = alive === false && !STOPPED.has(a.status) ? "exited" : a.status;
  let [glyph, label, style] = STATUS_LABEL[status] ?? ["·", status, "normal" as CliStyle];
  if (status === "idle" && a.reported) [glyph, label] = ["✓", "done"];
  const reason = needsYouReason(a, ws);
  if (reason) return { glyph: "◆", label: reason, style: "alert", needs: true };
  const review = awaitingReview(a, ws);
  if (review) return { glyph: "◇", label: review, style: "dim", needs: false };
  return { glyph, label, style, needs: false };
}

/* The CLI's colours as the app's tokens: busy is the braid's purple, ok the
   green thread, alert sap orange, bad the fail red, dim the quiet ink. */
export const CLI_INK: Record<CliStyle, string> = {
  busy: "text-branch",
  ok: "text-pass",
  alert: "text-sap",
  bad: "text-fail",
  dim: "text-ink-3",
  normal: "text-ink-2",
};

/* The provider's name as the session bar reads it: the CLI, not the model. */
export const PROVIDER_NAME: Record<string, string> = {
  claude: "Claude Code",
  codex: "Codex",
  antigravity: "Antigravity",
  opencode: "OpenCode",
  native: "Local model",
  shell: "Shell",
  subagent: "Subagent",
};
export const providerName = (provider: string): string => PROVIDER_NAME[provider] ?? provider;

/* What an agent is called where there is no room for its id: what it works
   on, the tail of its branch ("desktop/ux-revamp" → "ux revamp"), or, for
   an agent in the same workspace as its parent (a reviewer, a helper), its
   profile. The supervisor is "supervisor". The id stays for hover and detail. */
export function agentName(workspaces: WorkspaceDTO[], id: string): string {
  const entries = workspaces.flatMap((ws) => (ws.agents ?? []).map((agent) => ({ agent, ws })));
  const mine = entries.find((e) => e.agent.id === id);
  if (!mine) return id;
  if (!mine.agent.parent_id) return "supervisor";
  const parent = entries.find((e) => e.agent.id === mine.agent.parent_id);
  if (parent && parent.ws.id === mine.ws.id) return mine.agent.profile;
  return branchName(mine.ws.branch);
}

export const branchName = (branch: string) => (branch.split("/").pop() ?? branch).replace(/[-_]+/g, " ");

/* A bus event about an agent, in words (without the agent's name). */
export function eventPhrase(e: BusEvent): string {
  const p = e.payload;
  switch (e.kind) {
    case "agent.status":
      return typeof p.status === "string" ? (p.status === "waiting" ? "waiting on you" : `is ${p.status}`) : "changed status";
    case "agent.result":
      return "reported its result";
    case "message.queued":
      return "got your message";
    case "review.submitted":
      return "review submitted";
    case "job.progress":
      return typeof p.text === "string" ? p.text : "working";
    case "job.done":
      return typeof p.job_kind === "string" ? `${p.job_kind} done` : "job done";
    case "job.failed":
      return typeof p.job_kind === "string" ? `${p.job_kind} failed` : "job failed";
    default:
      return e.kind.replace(/\./g, " ");
  }
}

/* The newest thing the stream reported about one agent, with its age; null
   when nothing has come in since the app opened. */
export function agentActivity(recent: BusEvent[], id: string, now = Date.now() / 1000): string | null {
  const last = [...recent].reverse().find((e) => e.agent_id === id);
  if (!last) return null;
  const ago = formatAge(now - last.ts);
  return `${eventPhrase(last)} · ${ago === "just now" ? ago : `${ago} ago`}`;
}

/* The latest thing that happened in a session, in a line: the newest bus
   event about one of its agents, or failing that what its busiest agent is
   doing now. `name` says what to call an agent. */
export function activityLine(recent: BusEvent[], members: AgentDTO[], name: (id: string) => string = (id) => id, now = Date.now() / 1000): string {
  const ids = new Set(members.map((a) => a.id));
  const last = [...recent].reverse().find((e) => e.agent_id && ids.has(e.agent_id));
  if (last) {
    const ago = formatAge(now - last.ts);
    return `${name(last.agent_id!)} ${eventPhrase(last)} · ${ago === "just now" ? ago : `${ago} ago`}`;
  }
  const busy = members.find((a) => a.status === "processing" || a.status === "starting") ?? members.find((a) => a.status === "waiting");
  if (busy) {
    const forHow = busy.status_since != null ? ` for ${formatAge(now - busy.status_since).replace("just now", "a moment")}` : "";
    return `${name(busy.id)} ${busy.status === "waiting" ? "waiting on you" : busy.status}${forHow}`;
  }
  return "all quiet";
}

/* "just now", "4m", "3h", "2d" for an age in seconds. */
export function formatAge(seconds: number): string {
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

/* A session's age line: how long since it started, or since it paused. */
export function sessionAge(session: SessionDTO, now = Date.now() / 1000): string {
  const paused = isPaused(session);
  const age = formatAge(now - (paused ? (session.status_since ?? session.created_at) : session.created_at));
  if (paused) return `${session.status} ${age} ago`;
  return age === "just now" ? "started just now" : `started ${age} ago`;
}
