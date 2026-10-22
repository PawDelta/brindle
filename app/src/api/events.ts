/* WS /v1/events (brindle/daemon/events.py, auth.py): the bus, live.

   The token goes as the socket's first text frame, never in the URL; a wrong
   one closes the socket with 4401. Every bus row carries its id, so after a
   drop the stream reconnects with ?since=<last id> and the daemon replays
   what was missed, or answers "resnapshot" when it can't (rows pruned). */

export interface BusEvent {
  type: "event";
  id: number;
  ts: number;
  kind: string;
  repo_root: string | null;
  root_id: string | null;
  agent_id: string | null;
  workspace_id: string | null;
  task_id: string | null;
  payload: Record<string, unknown>;
}

/* The daemon couldn't honour `since`: fetch the snapshot and inbox afresh. */
export interface Resnapshot {
  type: "resnapshot";
  reason: string;
}

/* The first liveness message after (re)connecting maps every agent; later ones
   carry only the agents that changed, plus those that are gone. */
export interface Liveness {
  type: "liveness";
  agents: Record<string, boolean>;
  gone?: string[];
}

export interface GitStatus {
  branch: string | null;
  base: string | null;
  ahead: number;
  behind: number;
  dirty_files: string[];
  unpushed: number | null;
}

export interface GitSweep {
  type: "git";
  workspace_id: string;
  status: GitStatus;
}

export interface OrgNotice {
  id: string;
  [key: string]: unknown;
}

/* The Pro status poller's bar text and unread org notices. */
export interface OrgStatus {
  type: "status";
  bar: string[] | null;
  notices: OrgNotice[];
}

export type StreamMessage = BusEvent | Resnapshot | Liveness | GitSweep | OrgStatus;

export type StreamState = "connecting" | "open" | "closed" | "unauthorized";

/* Close code for a socket whose first frame wasn't the token (auth.WS_UNAUTHORIZED). */
export const WS_UNAUTHORIZED = 4401;

/* The part of the DOM WebSocket the stream uses, so tests can pass a fake. */
export interface WebSocketLike {
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code: number }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
}

export type WebSocketFactory = (url: string) => WebSocketLike;

export interface EventStreamOptions {
  /* The socket URL for a given `since` (DaemonClient.eventsUrl). */
  url: (since: number) => string;
  token: string;
  /* The last event id already seen; 0 replays everything the daemon still holds. */
  since?: number;
  onMessage: (message: StreamMessage) => void;
  onState?: (state: StreamState) => void;
  connect?: WebSocketFactory;
  /* Milliseconds before reconnect attempt `n` (0-based). */
  backoff?: (attempt: number) => number;
}

export const defaultBackoff = (attempt: number) => Math.min(250 * 2 ** attempt, 10_000);

export class EventStream {
  private socket: WebSocketLike | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private stopped = true;
  private lastId: number;
  private readonly opts: EventStreamOptions;

  constructor(opts: EventStreamOptions) {
    this.opts = opts;
    this.lastId = opts.since ?? 0;
  }

  /* The id of the newest bus event received: the next connection's `since`. */
  get since(): number {
    return this.lastId;
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    const socket = this.socket;
    this.socket = null;
    socket?.close(1000);
    this.opts.onState?.("closed");
  }

  private open(): void {
    const connect = this.opts.connect ?? ((url: string) => new WebSocket(url) as unknown as WebSocketLike);
    this.opts.onState?.("connecting");
    const socket = connect(this.opts.url(this.lastId));
    this.socket = socket;
    socket.onopen = () => {
      if (this.socket !== socket) return;
      socket.send(this.opts.token);
      this.attempt = 0;
      this.opts.onState?.("open");
    };
    socket.onmessage = (ev) => {
      if (this.socket !== socket || typeof ev.data !== "string") return;
      let message: StreamMessage;
      try {
        message = JSON.parse(ev.data) as StreamMessage;
      } catch {
        return;
      }
      if (message.type === "event") this.lastId = Math.max(this.lastId, message.id);
      this.opts.onMessage(message);
    };
    socket.onclose = (ev) => {
      if (this.socket !== socket) return;
      this.socket = null;
      if (ev.code === WS_UNAUTHORIZED) {
        this.stopped = true;
        this.opts.onState?.("unauthorized");
        return;
      }
      this.opts.onState?.("closed");
      if (!this.stopped) this.reconnect();
    };
    socket.onerror = () => {
      // A close always follows an error; reconnecting is onclose's business.
    };
  }

  private reconnect(): void {
    const delay = (this.opts.backoff ?? defaultBackoff)(this.attempt++);
    this.timer = setTimeout(() => {
      this.timer = null;
      if (!this.stopped) this.open();
    }, delay);
  }
}
