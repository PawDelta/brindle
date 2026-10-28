/* The daemon through the app's shell (src-tauri/src/daemon.rs). The shell
   reads ~/.brindle/daemon.json, starts `brindle daemon` when none runs, and
   talks to its Unix socket; the webview never sees the token. So a client
   made here has a placeholder base URL and an empty token: `tauriFetch` and
   `tauriSocket` carry the path to the shell, which adds the real ones. */

import { Channel, invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { DaemonClient } from "./client";
import type { WebSocketFactory, WebSocketLike } from "./events";

/* Never fetched: only its path and query reach the shell. */
const SHELL_URL = "http://brindle-daemon.invalid";

/* The running daemon, as the shell reports it. */
export interface EngineInfo {
  version: string;
  pid: number;
}

/* Find the daemon, starting it first when none runs. Rejects with the
   shell's reason (brindle isn't installed, the daemon didn't start ...). */
export function connectDaemon(): Promise<EngineInfo> {
  return invoke<EngineInfo>("daemon_connect");
}

interface Reply {
  status: number;
  body: string;
}

/* `fetch` for DaemonClient. A rejection (no answer at all) surfaces as the
   client's "unreachable" error, as a refused fetch would. */
export const tauriFetch: typeof fetch = async (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input));
  const reply = await invoke<Reply>("daemon_request", {
    method: init?.method ?? "GET",
    path: url.pathname + url.search,
    body: typeof init?.body === "string" ? init.body : null,
  });
  return new Response(reply.body === "" ? null : reply.body, { status: reply.status });
};

/* What the shell sends for a socket: opened, a text frame, a binary frame
   (base64: a terminal's bytes), or the close code. */
type Frame = { kind: "open" } | { kind: "text"; data: string } | { kind: "binary"; data: string } | { kind: "close"; code: number };

/* A base64 frame as the bytes a WebSocket with binaryType "arraybuffer" delivers. */
export function decodeBinary(data: string): ArrayBuffer {
  const text = atob(data);
  const bytes = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i++) bytes[i] = text.charCodeAt(i);
  return bytes.buffer;
}

/* A WebSocket for EventStream (api/events.ts), opened by the shell
   (daemon_stream_open), which sends the token as the first frame itself:
   the client's own first frame, its token, is dropped. Every frame after it
   goes to the daemon through daemon_stream_send, one after another in the
   order sent: each waits for the one before it (and the first for the
   stream's id), since two invokes in flight at once could reach the socket
   in either order. A close takes no more frames, and closes the stream once
   the ones sent before it have gone. */
export const tauriSocket: WebSocketFactory = (url) => {
  const { pathname, search } = new URL(url);
  let id: number | null = null;
  let closed = false;
  let tokenSent = false;
  const channel = new Channel<Frame>();
  const opened = invoke<number>("daemon_stream_open", { path: pathname + search, channel });
  // Sends queue on the open, then on each other.
  let sends: Promise<unknown> = opened.catch(() => {});
  const socket: WebSocketLike = {
    send(data) {
      if (!tokenSent) {
        tokenSent = true;
        return;
      }
      if (closed) return;
      sends = sends
        .then(() => (id === null ? undefined : invoke("daemon_stream_send", { id, data })))
        .catch(() => {
          // The stream is gone; its close frame says so.
        });
    },
    close() {
      if (closed) return;
      closed = true;
      sends = sends.then(() => (id === null ? undefined : invoke("daemon_stream_close", { id }))).catch(() => {});
    },
    onopen: null,
    onmessage: null,
    onclose: null,
    onerror: null,
  };
  channel.onmessage = (frame) => {
    if (frame.kind === "open") socket.onopen?.({});
    else if (frame.kind === "text") socket.onmessage?.({ data: frame.data });
    else if (frame.kind === "binary") socket.onmessage?.({ data: decodeBinary(frame.data) });
    else socket.onclose?.({ code: frame.code });
  };
  opened.then(
    (found) => {
      // Set before the queued sends (and a close) run: they wait on this same open.
      id = found;
    },
    // Refused outright (a route the shell won't carry); one that can't connect
    // closes 1006 on the channel instead.
    () => {
      socket.onerror?.({});
      socket.onclose?.({ code: 1006 });
    },
  );
  return socket;
};

/* What the shell's `install_cli` did (src-tauri/src/engine.rs CliLink): the
   link it made, `<dir>/brindle`, and the bundled engine's launcher it points at. */
export interface CliLink {
  path: string;
  target: string;
}

/* Put the `brindle` command on the person's PATH: the shell links its
   bundled engine's brindle into ~/.local/bin (the `install_cli` command of
   src-tauri/src/engine.rs). Rejects with the shell's reason: a build with no
   engine, an app run from the .dmg, something else already there. */
export function installCli(): Promise<CliLink> {
  return invoke<CliLink>("install_cli");
}

/* Settings' staff-only Update (owner decision; the only way a release
   candidate updates, src-tauri/src/updates.rs): `install` checks this build's
   feed and installs an update found, answering its version, or null when
   this is the latest; `restart` starts the app again on it. */
export interface AppUpdater {
  install: () => Promise<string | null>;
  restart: () => Promise<void>;
}

export const appUpdater: AppUpdater = {
  install: () => invoke<string | null>("app_update_install"),
  restart: () => invoke<void>("app_restart"),
};

/* App updates as the shell has them (src-tauri/src/updates.rs UpdateStatus):
   this build's version; whether it installs updates by itself (a release;
   not an rc or a staff build); an update installed and waiting for the
   restart; the version this launch was updated to, until dismissed; when
   this machine last checked (seconds since the epoch); a check under way. */
export interface AppUpdateStatus {
  version: string;
  automatic: boolean;
  pending: string | null;
  updated: string | null;
  last_checked: number | null;
  checking: boolean;
}

/* The shell's own updates, for the Inbox and Settings' About: `status` now,
   `check` (Check now: check and install, answering the status after),
   `restart` into an installed update, `dismiss` "Updated to brindle X", and
   `watch` each change the shell announces (an install in the background),
   answering a function that stops watching. */
export interface AppUpdates {
  status: () => Promise<AppUpdateStatus>;
  check: () => Promise<AppUpdateStatus>;
  restart: () => Promise<void>;
  dismiss: () => Promise<void>;
  watch: (on: (status: AppUpdateStatus) => void) => () => void;
}

export const appUpdates: AppUpdates = {
  status: () => invoke<AppUpdateStatus>("app_update_status"),
  check: () => invoke<AppUpdateStatus>("app_update_check"),
  restart: () => invoke<void>("app_restart"),
  dismiss: () => invoke<void>("app_update_dismiss"),
  watch: (on) => {
    const stop = listen<AppUpdateStatus>("app-update", (e) => on(e.payload));
    return () => void stop.then((unlisten) => unlisten());
  },
};

/* What's new in brindle `version`: its release notes on the site. */
export const releaseNotesUrl = (version: string) => `https://pawdelta.com/brindle/changelog/#${encodeURIComponent(version)}`;

/* How the shell's release-branch install goes (src-tauri/src/updates.rs
   ReleaseProgress): the build found, the bytes downloaded so far (of
   `total`, when known), installing. */
export type ReleaseProgress =
  | { kind: "found"; version: string }
  | { kind: "downloading"; downloaded: number; total: number | null }
  | { kind: "installing" };

/* Settings' staff-only "Install latest release-branch build"
   (app_release_build_install, src-tauri/src/updates.rs): `install` installs
   the release/desktop pipeline's latest build, telling `progress` how it
   goes, and answers its version, or null when this build is it already or
   newer; `restart` starts the app again on it. The shell picks the feed and
   checks staff itself; nothing here names a URL. */
export interface ReleaseBuildUpdater {
  install: (progress: (p: ReleaseProgress) => void) => Promise<string | null>;
  restart: () => Promise<void>;
}

export const releaseBuildUpdater: ReleaseBuildUpdater = {
  install: (progress) => {
    const onProgress = new Channel<ReleaseProgress>();
    onProgress.onmessage = progress;
    return invoke<string | null>("app_release_build_install", { onProgress });
  },
  restart: () => invoke<void>("app_restart"),
};

/* Settings' About (src-tauri/src/about.rs): the app's version and the license
   files the bundle carries, each null when this build has none (a dev build). */
export interface About {
  version: string;
  files: { name: string; text: string | null }[];
}

export function readAbout(): Promise<About> {
  return invoke<About>("about_read");
}

export function tauriClient(): DaemonClient {
  return new DaemonClient({ baseUrl: SHELL_URL, token: "", fetch: tauriFetch });
}
