/* The first run's setup, through the app's shell (src-tauri/src/setup.rs):
   what brindle needs on this machine and what is here, and the install (uv,
   Homebrew's tmux, `uv tool install brindle`) run on a pty of the shell's,
   whose lines the first launch's engine step reads into its progress card
   (screens/FirstRun). Nothing here signs in to
   anything: Claude Code's sign-in is only read. */

import { Channel, invoke } from "@tauri-apps/api/core";
import { connectDaemon, decodeBinary } from "./tauri";

/* The tools setup.rs looks for, in its order: brindle's own needs, then the optional agent CLIs. */
export type ToolName = "uv" | "tmux" | "brindle" | "claude" | "codex" | "agy";

export interface Tool {
  name: ToolName;
  required: boolean;
  /* Where it is; null when it isn't. */
  path: string | null;
  version: string | null;
}

/* Claude Code's sign-in, as `brindle doctor` reads it: its own login, a key
   from the environment, signed out, or no answer from `claude auth status`. */
export type SignIn = "signed_in" | "environment_key" | "signed_out" | "unknown";

export interface Detection {
  os: "mac" | "linux" | "other";
  tools: Tool[];
  /* Homebrew, which installs tmux on macOS. */
  homebrew: string | null;
  /* null when Claude Code isn't installed. */
  claude_signin: SignIn | null;
}

/* What the install's pty sends: its bytes (base64), then the script's exit code. */
export type InstallEvent = { kind: "data"; data: string } | { kind: "exit"; code: number };

/* Who watches the install: its bytes, the script's exit code, or why it couldn't be watched. */
export interface InstallPane {
  onData(bytes: Uint8Array): void;
  onExit(code: number): void;
  onError(message: string): void;
}

export interface Setup {
  detect(): Promise<Detection>;
  /* Find the daemon, starting it: rejects with why it can't. */
  connect(): Promise<unknown>;
  /* Start the install; rejects when one is already running. */
  install(): Promise<void>;
  /* Watch the install: what it printed so far, then live, then its exit, until closed. */
  attach(pane: InstallPane): { close(): void };
}

export function tauriSetup(): Setup {
  return {
    detect: () => invoke<Detection>("setup_detect"),
    connect: connectDaemon,
    install: () => invoke<void>("setup_install_start"),
    attach(pane) {
      let closed = false;
      const channel = new Channel<InstallEvent>();
      channel.onmessage = (event) => {
        if (closed) return;
        if (event.kind === "data") pane.onData(new Uint8Array(decodeBinary(event.data)));
        else pane.onExit(event.code);
      };
      invoke("setup_install_attach", { channel }).catch((e) => closed || pane.onError(String(e)));
      return {
        close() {
          closed = true;
        },
      };
    },
  };
}
