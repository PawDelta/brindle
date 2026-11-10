/* The app's one store: a pure reducer over daemon snapshots and stream
   messages, and a minimal subscribe / dispatch container for it.

   Snapshots are authoritative (plan 2.4): the store takes the snapshot and
   inbox whole on connect, and an event it can patch in place (an agent's
   status, a removed workspace, a git sweep, liveness, org status) it patches.
   Any other event marks the store stale, and sync.ts fetches a fresh snapshot. */

import type { AccountDTO, InboxItemDTO, WorkspaceDTO } from "../api/schema";
import type { AppUpdateStatus } from "../api/tauri";
import type { BusEvent, OrgNotice, StreamMessage, StreamState } from "../api/events";
import { isPastWorker } from "./overview";

export const RECENT_EVENTS = 50;

export interface AppState {
  connection: StreamState | "idle";
  /* Changes monotonically even when a reconnect returns to the same state. */
  connectionGeneration: number;
  /* Invalidates pending reads that could restore a closed or rewound agent. */
  lifecycleVersion: number;
  /* Shared identity, refreshed by the sidebar and Account page. */
  account: AccountDTO | null;
  /* The newest bus event id applied: what a reconnect resumes from. */
  lastEventId: number;
  workspaces: WorkspaceDTO[];
  inbox: InboxItemDTO[];
  /* Past ids also suppress independently fetched permission groups. A live
     snapshot of the same id clears it when a session is continued. */
  pastWorkers: string[];
  /* Agent id → whether its process is alive, from the daemon's liveness sweep. */
  alive: Record<string, boolean>;
  org: { bar: string[] | null; notices: OrgNotice[] };
  /* Something changed that only a fresh snapshot shows. */
  stale: boolean;
  /* A snapshot has arrived: the inbox is the daemon's, not the empty start. */
  synced: boolean;
  /* Advances only when a successful snapshot is applied, not when a refresh settles. */
  snapshotVersion: number;
  /* When the read whose rows are applied began (snapshotTicket): a read begun before it,
     ending later (the panes gate's, overtaken by sync's), never puts older rows over them. */
  snapshotBegan: number;
  /* Visual id → the bus event that took it out of the Inbox (read, answered, dismissed): an inbox read
     begun before that event still has it, and mustn't put it back. Gone once a later read is applied. */
  settledVisuals: Record<string, number>;
  /* The newest bus events, oldest first (for notifications). */
  recent: BusEvent[];
  error: string | null;
  /* The app's own updates, as the shell last announced them (appUpdates.ts); null without a shell. */
  appUpdate: AppUpdateStatus | null;
}

export type Action =
  | { type: "account"; account: AccountDTO }
  | { type: "app_update"; status: AppUpdateStatus }
  | { type: "connection"; state: StreamState }
  /* `since`: the newest bus event applied when the reads began (sync.ts); the store's own when absent. */
  | { type: "snapshot"; workspaces: WorkspaceDTO[]; inbox?: InboxItemDTO[]; began?: number; since?: number }
  | { type: "stream"; message: StreamMessage }
  | { type: "error"; message: string };

export const initialState: AppState = {
  connection: "idle",
  connectionGeneration: 0,
  lifecycleVersion: 0,
  account: null,
  lastEventId: 0,
  workspaces: [],
  inbox: [],
  pastWorkers: [],
  alive: {},
  org: { bar: null, notices: [] },
  stale: false,
  synced: false,
  snapshotVersion: 0,
  snapshotBegan: 0,
  settledVisuals: {},
  recent: [],
  error: null,
  appUpdate: null,
};

let tickets = 0;

/* A snapshot read's place among all of them, taken as it begins (sync's and the panes gate's alike). */
export function snapshotTicket(): number {
  return ++tickets;
}

export function reduce(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "account":
      return { ...state, account: action.account };
    case "app_update":
      return { ...state, appUpdate: action.status };
    case "connection":
      return { ...state, connection: action.state,
        connectionGeneration: state.connectionGeneration + Number(state.connection !== action.state) };
    case "snapshot": {
      if (action.began !== undefined && action.began < state.snapshotBegan) return state;
      // The pane gate can apply ready rows before inbox answers. Keep the
      // inbox's contents and initial-sync status until that read arrives.
      let { inbox = state.inbox } = action;
      let settledVisuals = state.settledVisuals;
      if (action.inbox !== undefined) {
        // A visual settled after these reads began stays out; one settled before is the engine's to say.
        const since = action.since ?? state.lastEventId;
        inbox = inbox.filter((i) => !i.visual_id || !((state.settledVisuals[i.visual_id] ?? 0) > since));
        settledVisuals = Object.fromEntries(Object.entries(state.settledVisuals).filter(([, id]) => id > since));
      }
      return { ...state, ...liveSnapshot(action.workspaces, inbox, state.pastWorkers), stale: false,
        synced: state.synced || action.inbox !== undefined, error: null,
        snapshotVersion: state.snapshotVersion + 1, snapshotBegan: Math.max(state.snapshotBegan, action.began ?? 0),
        settledVisuals };
    }
    case "error":
      return { ...state, error: action.message };
    case "stream": {
      const message = action.message;
      const ended = message.type === "event" && (message.kind === "agent.closed"
        || (message.kind === "agent.status" && message.payload.status === "rewound"));
      return applyMessage(ended ? { ...state, lifecycleVersion: state.lifecycleVersion + 1 } : state, message);
    }
  }
}

/* One boundary for every live surface, including Inbox badges. Do not drop
   items merely because their agent is absent: paused sessions keep requests. */
function liveSnapshot(workspaces: WorkspaceDTO[], inbox: InboxItemDTO[], previous: string[]) {
  const past = new Set(previous);
  for (const a of workspaces.flatMap((ws) => ws.agents ?? [])) {
    if (isPastWorker(a)) past.add(a.id);
    else past.delete(a.id);
  }
  const pastWorkers = [...past];
  if (past.size === 0) return { workspaces, inbox, pastWorkers };
  return {
    pastWorkers,
    workspaces: workspaces.map((ws) => ({ ...ws, agents: ws.agents?.filter((a) => !past.has(a.id)) })),
    inbox: inbox.filter((i) => !i.agent_id || !past.has(i.agent_id)),
  };
}

function applyMessage(state: AppState, m: StreamMessage): AppState {
  switch (m.type) {
    case "event":
      return applyEvent(
        { ...state, lastEventId: Math.max(state.lastEventId, m.id), recent: [...state.recent, m].slice(-RECENT_EVENTS) },
        m,
      );
    case "resnapshot":
      return { ...state, stale: true };
    case "liveness": {
      if (m.gone === undefined) return { ...state, alive: { ...m.agents } };
      const alive = { ...state.alive, ...m.agents };
      for (const id of m.gone) delete alive[id];
      return { ...state, alive };
    }
    case "git":
      return {
        ...state,
        workspaces: state.workspaces.map((ws) =>
          ws.id === m.workspace_id
            ? { ...ws, ahead: m.status.ahead, behind: m.status.behind, dirty: m.status.dirty_files.length }
            : ws,
        ),
      };
    case "status":
      return { ...state, org: { bar: m.bar, notices: m.notices } };
  }
}

function applyEvent(state: AppState, e: BusEvent): AppState {
  switch (e.kind) {
    case "session.changed":
      return state;
    case "agent.closed":
      if (!e.agent_id) return { ...state, stale: true };
      return {
        ...state,
        stale: true,
        pastWorkers: e.agent_id ? [...new Set([...state.pastWorkers, e.agent_id])] : state.pastWorkers,
        workspaces: state.workspaces.map((ws) => ({ ...ws, agents: ws.agents?.filter((a) => a.id !== e.agent_id) })),
        inbox: state.inbox.filter((i) => i.agent_id !== e.agent_id),
      };
    case "agent.status": {
      const status = e.payload.status;
      if (typeof status !== "string" || !e.agent_id) return { ...state, stale: true };
      // An agent the snapshot doesn't have yet: a session just started says so only with its status (the
      // engine has no event of its own for a new agent). Patching nothing would leave it out for good.
      if (!state.workspaces.some((ws) => ws.agents?.some((a) => a.id === e.agent_id))) return { ...state, stale: true };
      return {
        ...state,
        // Status determines waiting/failed cards as well as the agent row.
        // Keep the row responsive while fetching the authoritative inbox.
        stale: true,
        ...liveSnapshot(state.workspaces.map((ws) => ({
          ...ws,
          agents: ws.agents?.map((a) => (a.id === e.agent_id ? { ...a, status, status_since: e.ts } : a)),
        })), state.inbox, state.pastWorkers),
      };
    }
    case "visual.read":
    case "visual.answered":
    case "visual.dismissed": {
      // Answered or dismissed, a visual asks nothing more; read, only when the engine says it no longer
      // waits (a question waits for its answer). Out of the Inbox and its badge at once, then read afresh.
      const id = e.payload.visual;
      if (typeof id !== "string" || (e.kind === "visual.read" && e.payload.pending !== false)) return { ...state, stale: true };
      return {
        ...state,
        stale: true,
        inbox: state.inbox.filter((i) => i.visual_id !== id),
        settledVisuals: { ...state.settledVisuals, [id]: e.id },
      };
    }
    case "workspace.removed":
      return {
        ...state,
        workspaces: state.workspaces.filter((ws) => ws.id !== e.workspace_id),
        inbox: state.inbox.filter((i) => i.workspace_id !== e.workspace_id),
      };
    default:
      return { ...state, stale: true };
  }
}

export interface Store<S, A> {
  getState(): S;
  dispatch(action: A): void;
  subscribe(listener: () => void): () => void;
}

export function createStore<S, A>(reducer: (state: S, action: A) => S, initial: S): Store<S, A> {
  let state = initial;
  const listeners = new Set<() => void>();
  return {
    getState: () => state,
    dispatch(action) {
      const next = reducer(state, action);
      if (next === state) return;
      state = next;
      for (const l of [...listeners]) l();
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

export type AppStore = Store<AppState, Action>;

export const createAppStore = (initial: AppState = initialState): AppStore => createStore(reduce, initial);
