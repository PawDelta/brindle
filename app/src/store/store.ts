/* The app's one store: a pure reducer over daemon snapshots and stream
   messages, and a minimal subscribe / dispatch container for it.

   Snapshots are authoritative (plan 2.4): the store takes the snapshot and
   inbox whole on connect, and an event it can patch in place (an agent's
   status, a removed workspace, a git sweep, liveness, org status) it patches.
   Any other event marks the store stale, and sync.ts fetches a fresh snapshot. */

import type { AccountDTO, InboxItemDTO, WorkspaceDTO } from "../api/schema";
import type { AppUpdateStatus } from "../api/tauri";
import type { BusEvent, OrgNotice, StreamMessage, StreamState } from "../api/events";

export const RECENT_EVENTS = 50;

export interface AppState {
  connection: StreamState | "idle";
  /* Shared identity, refreshed by the sidebar and Account page. */
  account: AccountDTO | null;
  /* The newest bus event id applied: what a reconnect resumes from. */
  lastEventId: number;
  workspaces: WorkspaceDTO[];
  inbox: InboxItemDTO[];
  /* Agent id → whether its process is alive, from the daemon's liveness sweep. */
  alive: Record<string, boolean>;
  org: { bar: string[] | null; notices: OrgNotice[] };
  /* Something changed that only a fresh snapshot shows. */
  stale: boolean;
  /* A snapshot has arrived: the inbox is the daemon's, not the empty start. */
  synced: boolean;
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
  | { type: "snapshot"; workspaces: WorkspaceDTO[]; inbox: InboxItemDTO[] }
  | { type: "stream"; message: StreamMessage }
  | { type: "error"; message: string };

export const initialState: AppState = {
  connection: "idle",
  account: null,
  lastEventId: 0,
  workspaces: [],
  inbox: [],
  alive: {},
  org: { bar: null, notices: [] },
  stale: false,
  synced: false,
  recent: [],
  error: null,
  appUpdate: null,
};

export function reduce(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "account":
      return { ...state, account: action.account };
    case "app_update":
      return { ...state, appUpdate: action.status };
    case "connection":
      return { ...state, connection: action.state };
    case "snapshot":
      return { ...state, workspaces: action.workspaces, inbox: action.inbox, stale: false, synced: true, error: null };
    case "error":
      return { ...state, error: action.message };
    case "stream":
      return applyMessage(state, action.message);
  }
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
    case "agent.status": {
      const status = e.payload.status;
      if (typeof status !== "string" || !e.agent_id) return { ...state, stale: true };
      // An agent the snapshot doesn't have yet: a session just started says so only with its status (the
      // engine has no event of its own for a new agent). Patching nothing would leave it out for good.
      if (!state.workspaces.some((ws) => ws.agents?.some((a) => a.id === e.agent_id))) return { ...state, stale: true };
      return {
        ...state,
        workspaces: state.workspaces.map((ws) => ({
          ...ws,
          agents: ws.agents?.map((a) => (a.id === e.agent_id ? { ...a, status, status_since: e.ts } : a)),
        })),
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
