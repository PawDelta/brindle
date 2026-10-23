import { describe, expect, it, vi } from "vitest";
import { flush } from "../test/fakeDaemon";
import type { InstallEvent } from "./setup";

/* The shell's IPC, recorded: each invoke, and the channel the attach got. */
const ipc = vi.hoisted(() => {
  const calls: Array<{ cmd: string; args: Record<string, unknown> }> = [];
  class Channel<T> {
    onmessage: ((message: T) => void) | null = null;
  }
  const state = { refuse: null as string | null };
  const invoke = vi.fn((cmd: string, args: Record<string, unknown> = {}) => {
    calls.push({ cmd, args });
    return state.refuse ? Promise.reject(state.refuse) : Promise.resolve(undefined);
  });
  return { calls, Channel, invoke, state };
});

vi.mock("@tauri-apps/api/core", () => ({ Channel: ipc.Channel, invoke: ipc.invoke, isTauri: () => true }));

import { tauriSetup } from "./setup";

describe("tauriSetup", () => {
  it("watches the install's pty: its bytes in, its exit once, nothing once closed", async () => {
    const data: string[] = [];
    const exits: number[] = [];
    const errors: string[] = [];
    const pane = tauriSetup().attach({
      onData: (bytes) => data.push(new TextDecoder().decode(bytes)),
      onExit: (code) => exits.push(code),
      onError: (why) => errors.push(why),
    });
    const attach = ipc.calls.find((c) => c.cmd === "setup_install_attach")!;
    const channel = attach.args.channel as { onmessage: (e: InstallEvent) => void };
    await flush();
    channel.onmessage({ kind: "data", data: btoa("==> installing uv\r\n") });
    expect(data).toEqual(["==> installing uv\r\n"]);
    channel.onmessage({ kind: "exit", code: 0 });
    expect(exits).toEqual([0]);
    expect(errors).toEqual([]);

    // Closed (the step unmounted): nothing more reaches it.
    pane.close();
    channel.onmessage({ kind: "exit", code: 0 });
    expect(exits).toEqual([0]);
  });

  it("says why the install couldn't be watched", async () => {
    ipc.state.refuse = "no install is running";
    const errors: string[] = [];
    tauriSetup().attach({ onData() {}, onExit() {}, onError: (why) => errors.push(why) });
    await flush();
    expect(errors).toEqual(["no install is running"]);
    ipc.state.refuse = null;
  });
});
