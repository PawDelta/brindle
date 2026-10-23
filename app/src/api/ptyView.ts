/* A tmux pane as a terminal shows it: a real tmux client on a pty
   (docs/terminal-architecture.md). Every pane the app shows comes this
   way: an agent's window, and the daemon's action terminals (a sign-in,
   which the app also reads through one no one sees: api/paneText.ts). One
   seam, "a terminal stream for this pane": PtyViewOpener (open, write,
   resize, and its bytes, exit and error back, then close). On macOS and
   Linux it is the shell's pty view (src-tauri/src/pty_view.rs,
   tauriPtyView); the measuring rig and the e2e tests reach the same Rust
   over a local WebSocket (wsPtyView, src-tauri/examples/pty_ws.rs);
   Windows's will be the engine's WSL bridge. Bytes are bytes both ways:
   what the client prints goes to xterm.js as is, what xterm.js says the
   person typed (keys, pastes, mouse reports) goes to the client as is. */

import { Channel, invoke, isTauri } from "@tauri-apps/api/core";
import { createContext, createElement, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { logDebug } from "./debug";
import type { WebSocketFactory, WebSocketLike } from "./events";
import { decodeBinary } from "./tauri";

/* Where the pane is: an agent's `tmux_server` and `window`, or an action
   terminal's `server` and `target`. */
export interface PtyViewTarget {
  server: string | null;
  target: string;
}

/* Columns of the pane's window the view doesn't show (pty_view.rs Crop):
   brindle's CLI sidebar pane and its border, at the left or right. The
   view's client is `cols` columns wider than the view; the terminal shows
   the view's width of it from the client's column `left` on (where the
   agent's pane starts on the client, or as far on as keeps its cursor in
   view, a wider terminal's window panned on the client). `width`: the
   agent's columns from `left` on, where fewer than the view's (a narrower
   terminal having the window puts a right sidebar inside the view): the
   terminal masks the rest. */
export interface Crop {
  cols: number;
  left: number;
  width?: number | null;
}

export interface PtyViewHandlers {
  onData(bytes: Uint8Array): void;
  /* The window's crop: before the client's first bytes, and on each change. */
  onCrop?(crop: Crop): void;
  /* The tmux client exited (its view session went, or the agent's window). */
  onExit(code: number): void;
  onError(message: string): void;
}

export interface PtyView {
  write(data: string): void;
  /* The view's size in cells, the crop's columns not counted. */
  resize(cols: number, rows: number): void;
  /* The person is at the view (it opened, took focus or was clicked, or the
     app came to the front with it on screen): the window
     takes its size, as typing in it would, with nothing sent to the pane. */
  focus(): void;
  close(): void;
}

export type PtyViewOpener = (at: PtyViewTarget, size: { cols: number; rows: number }, handlers: PtyViewHandlers) => PtyView;

/* pty_view_status: whether panes can show through the pty view. "unusable":
   it can't work here (tmux older than 3.2, or none; Windows, until its
   bridge), in words to show. */
export type PtyStatus = { kind: "pending" } | { kind: "ready"; tmux: string } | { kind: "unusable"; message: string };

/* How the app shows panes: the pty view's status, the seam, and where what
   tmux copies (OSC 52) goes. No PtyViews (a browser tab, with no shell):
   no pane can be shown. */
export interface PtyViews {
  status: PtyStatus;
  open: PtyViewOpener;
  /* Settled once the clipboard holds `text`; failed where it couldn't be written. */
  copy(text: string): Promise<void>;
  /* What the clipboard holds, for a view's ⌘V (the shell's: the webview can't read it). */
  paste(): Promise<string>;
  /* Ask the shell again (tmux installed or updated since): setup reached
     ready, a pane that couldn't be shown is opened again or retried. */
  recheck(): void;
}

const PtyViewContext = createContext<PtyViews | null>(null);

export function PtyViewsProvider({ value, children }: { value: PtyViews | null; children?: ReactNode }) {
  return createElement(PtyViewContext.Provider, { value }, children);
}

export const usePtyViews = (): PtyViews | null => useContext(PtyViewContext);

/* What pty_view_open's channel says (PtyViewEvent): opened, the window's crop, the client's bytes, its exit, or why it couldn't open. */
type PtyViewEvent = { kind: "open" } | ({ kind: "crop" } & Crop) | { kind: "data"; data: string } | { kind: "exit"; code: number } | { kind: "error"; message: string };

/* The handlers with a view's error (it couldn't open, attach or crop) also
   sent to the debug log, while staff have it on (api/debug.ts). */
function logged(at: PtyViewTarget, handlers: PtyViewHandlers): PtyViewHandlers {
  return {
    ...handlers,
    onError(message) {
      logDebug("pty_view", "error", message, { server: at.server, target: at.target });
      handlers.onError(message);
    },
  };
}

/* Through the shell's pty_view_* commands. The id comes at once; writes and
   resizes wait for the view to say it opened. */
export const tauriPtyView: PtyViewOpener = (at, size, given) => {
  const handlers = logged(at, given);
  let closed = false;
  let isOpen = false;
  let ready: () => void = () => {};
  const live = new Promise<void>((ok) => (ready = ok));
  const channel = new Channel<PtyViewEvent>();
  channel.onmessage = (event) => {
    if (event.kind === "open") {
      isOpen = true;
      ready();
    }
    if (closed) return;
    if (event.kind === "crop") handlers.onCrop?.({ cols: event.cols, left: event.left, width: event.width });
    else if (event.kind === "data") handlers.onData(new Uint8Array(decodeBinary(event.data)));
    else if (event.kind === "exit") handlers.onExit(event.code);
    else if (event.kind === "error") handlers.onError(event.message);
  };
  const asked = invoke<number>("pty_view_open", { server: at.server, target: at.target, cols: size.cols, rows: size.rows, channel });
  asked.catch((e) => closed || handlers.onError(String(e)));
  const opened = asked.then(async (id) => {
    await live;
    return id;
  });
  // In order: two invokes in flight could land either way round.
  let sends: Promise<unknown> = opened;
  const after = (command: string, args: Record<string, unknown>) => {
    sends = sends.then((id) => invoke(command, { id, ...args }).then(() => id)).catch(() => opened);
  };
  return {
    write: (data) => closed || after("pty_view_write", { data }),
    resize: (cols, rows) => closed || after("pty_view_resize", { cols, rows }),
    focus: () => closed || after("pty_view_focus", {}),
    close() {
      if (closed) return;
      closed = true;
      // Open: after what was typed before it. Still opening (or never to open): at once, so the
      // shell drops its claim and closes it as it opens.
      void (isOpen ? sends.then(() => opened) : asked).then((id) => invoke("pty_view_close", { id })).catch(() => {});
    },
  };
};

/* What tmux copied onto the clipboard, through the shell (the webview has no clipboard permission): settled once the
   clipboard holds it, failed where it couldn't be written. Each copy, and why one failed, goes to the debug log while
   it is on (its size, never its text): which layer a lost copy stopped at. */
export const tauriCopy = (text: string): Promise<void> =>
  invoke("pty_view_copy", { text }).then(
    () => logDebug("pty_view", "info", "copied onto the clipboard", { bytes: text.length }),
    (e) => {
      logDebug("pty_view", "error", `couldn't copy onto the clipboard: ${String(e)}`, { bytes: text.length });
      throw e;
    },
  );

/* What the clipboard holds, for a view's ⌘V, through the shell; nothing where it can't be read (why, to the debug log). */
export const tauriPaste = (): Promise<string> =>
  invoke<string>("pty_view_paste").then(
    (text) => {
      logDebug("pty_view", "info", "read the clipboard for a paste", { bytes: text.length });
      return text;
    },
    (e) => {
      logDebug("pty_view", "error", `couldn't read the clipboard for a paste: ${String(e)}`);
      return "";
    },
  );

export async function tauriPtyStatus(): Promise<PtyStatus> {
  try {
    return await invoke<PtyStatus>("pty_view_status");
  } catch (e) {
    return { kind: "unusable", message: String(e) };
  }
}

/* The app's PtyViews in the shell: the status asked at start and on each
   recheck (the latest answer wins), panes waiting on the first. */
export function TauriPtyViews({ children }: { children?: ReactNode }) {
  const [status, setStatus] = useState<PtyStatus>({ kind: "pending" });
  const asked = useRef(0);
  const recheck = useCallback(() => {
    const n = ++asked.current;
    void tauriPtyStatus().then((s) => n === asked.current && setStatus(s));
  }, []);
  useEffect(() => {
    recheck();
    return () => {
      asked.current++;
    };
  }, [recheck]);
  const value = useMemo(() => ({ status, open: tauriPtyView, copy: tauriCopy, paste: tauriPaste, recheck }), [status, recheck]);
  return createElement(PtyViewsProvider, { value }, children);
}

/* PtyViews for where the app runs: the shell's, or none in a browser tab. */
export function AppPtyViews({ children }: { children?: ReactNode }) {
  return isTauri() ? createElement(TauriPtyViews, null, children) : createElement(PtyViewsProvider, { value: null }, children);
}

/* The same over app/src-tauri/examples/pty_ws.rs (the measuring rig's
   and the e2e tests' bridge: a browser has no Tauri), with the token it printed. */
export function wsPtyView(url: string, token: string, connect?: WebSocketFactory): PtyViewOpener {
  return (at, size, given) => {
    const handlers = logged(at, given);
    let socket: WebSocketLike;
    if (connect) socket = connect(url);
    else {
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      socket = ws as unknown as WebSocketLike;
    }
    let open = false;
    let closed = false;
    const queued: string[] = [];
    const send = (frame: object) => {
      const text = JSON.stringify(frame);
      if (open) socket.send(text);
      else queued.push(text);
    };
    socket.onopen = () => {
      socket.send(JSON.stringify({ token, server: at.server, target: at.target, cols: size.cols, rows: size.rows }));
      open = true;
      for (const text of queued.splice(0)) socket.send(text);
    };
    socket.onmessage = (ev) => {
      if (closed) return;
      const data = ev.data;
      if (typeof data === "string") {
        const said = JSON.parse(data) as { exit?: number; error?: string; crop?: Crop };
        if (said.crop !== undefined) handlers.onCrop?.(said.crop);
        else if (said.error !== undefined) handlers.onError(said.error);
        else if (said.exit !== undefined) handlers.onExit(said.exit);
      } else handlers.onData(new Uint8Array(data as ArrayBuffer));
    };
    socket.onclose = () => {
      if (!closed) handlers.onExit(-1);
      closed = true;
    };
    socket.onerror = () => {};
    return {
      write: (data) => closed || send({ type: "input", data }),
      resize: (cols, rows) => closed || send({ type: "resize", cols, rows }),
      focus: () => closed || send({ type: "focus" }),
      close() {
        closed = true;
        socket.close(1000);
      },
    };
  };
}
