import { beforeEach, describe, expect, it, vi } from "vitest";
import { flush } from "../test/fakeDaemon";

/* The shell's IPC, recorded: each invoke, the channel it was given, the id it
   answers a stream open with, and sends it can hold until released. */
const ipc = vi.hoisted(() => {
  const calls: Array<{ cmd: string; args: Record<string, unknown> }> = [];
  class Channel<T> {
    onmessage: ((message: T) => void) | null = null;
  }
  let opened: ((id: number) => void) | null = null;
  const held: Array<() => void> = [];
  const state = { hold: false };
  const invoke = vi.fn((cmd: string, args: Record<string, unknown> = {}) => {
    calls.push({ cmd, args });
    if (cmd === "daemon_stream_open") return new Promise<number>((resolve) => (opened = resolve));
    if (cmd === "daemon_stream_send" && state.hold) return new Promise<void>((resolve) => held.push(resolve));
    return Promise.resolve(undefined);
  });
  return { calls, Channel, invoke, state, open: (id: number) => opened?.(id), release: () => held.splice(0).forEach((r) => r()) };
});

vi.mock("@tauri-apps/api/core", () => ({ Channel: ipc.Channel, invoke: ipc.invoke, isTauri: () => true }));

import { decodeBinary, tauriSocket } from "./tauri";

const sends = () => ipc.calls.filter((c) => c.cmd === "daemon_stream_send").map((c) => c.args);
const URL = "ws://brindle-daemon.invalid/v1/events?since=4";

beforeEach(() => {
  ipc.calls.length = 0;
  ipc.state.hold = false;
});

/* A socket through the shell, opened, with what comes down it recorded. */
function stream() {
  const socket = tauriSocket(URL);
  const texts: unknown[] = [];
  const closes: number[] = [];
  socket.onmessage = (ev) => texts.push(ev.data);
  socket.onclose = (ev) => closes.push(ev.code);
  const channel = ipc.calls.filter((c) => c.cmd === "daemon_stream_open").at(-1)!.args.channel as InstanceType<typeof ipc.Channel>;
  return { socket, channel, texts, closes };
}

describe("tauriSocket", () => {
  it("carries a stream through the shell: its frames in, its own frames out in order, the token never", async () => {
    const { socket, channel, texts, closes } = stream();
    expect(ipc.calls.find((c) => c.cmd === "daemon_stream_open")!.args.path).toBe("/v1/events?since=4");

    // Opened before the shell has answered with the stream's id: the client's
    // token is dropped (the shell sent the real one), and frames wait for the id.
    channel.onmessage!({ kind: "open" });
    socket.send("token");
    socket.send("one");
    socket.send("two");
    await flush();
    expect(sends()).toEqual([]);
    ipc.open(7);
    await flush();
    expect(sends()).toEqual([
      { id: 7, data: "one" },
      { id: 7, data: "two" },
    ]);

    // Each send waits for the one before it: with the first held in the shell,
    // the second isn't even asked for, and goes once the first is answered.
    ipc.state.hold = true;
    socket.send("three");
    await flush();
    ipc.state.hold = false;
    socket.send("four");
    await flush();
    expect(sends()).toHaveLength(3);
    expect(sends()[2]).toEqual({ id: 7, data: "three" });
    ipc.release();
    await flush();
    expect(sends()).toHaveLength(4);
    expect(sends()[3]).toEqual({ id: 7, data: "four" });

    // Text comes as is, binary decoded.
    channel.onmessage!({ kind: "text", data: '{"type":"event"}' });
    channel.onmessage!({ kind: "binary", data: btoa("hi") });
    expect(texts[0]).toBe('{"type":"event"}');
    expect(new Uint8Array(texts[1] as ArrayBuffer)).toEqual(new Uint8Array([104, 105]));

    socket.close();
    await flush();
    expect(ipc.calls.at(-1)).toEqual({ cmd: "daemon_stream_close", args: { id: 7 } });
    channel.onmessage!({ kind: "close", code: 1000 });
    expect(closes).toEqual([1000]);
  });

  it("closes the stream once the frames sent before it have gone, with earlier sends pending or the id not yet in", async () => {
    const stream7 = () => ipc.calls.filter((c) => c.cmd === "daemon_stream_send" || c.cmd === "daemon_stream_close").map((c) => (c.cmd === "daemon_stream_close" ? "close" : String(c.args.data)));

    // A frame still in the shell when the stream is closed: the last frame and then the close wait for it.
    const { socket } = stream();
    socket.send("token");
    ipc.open(7);
    await flush();
    ipc.state.hold = true;
    socket.send("x");
    await flush();
    ipc.state.hold = false;
    socket.send("last");
    socket.close();
    // Nothing after the close is taken.
    socket.send("after");
    await flush();
    expect(stream7()).toEqual(["x"]);
    ipc.release();
    await flush();
    expect(stream7()).toEqual(["x", "last", "close"]);

    // Closed before the shell has answered with the stream's id: the last frame goes once it has, then the close.
    ipc.calls.length = 0;
    const early = stream().socket;
    early.send("token");
    early.send("last");
    early.close();
    await flush();
    expect(stream7()).toEqual([]);
    ipc.open(8);
    await flush();
    expect(ipc.calls.filter((c) => c.cmd !== "daemon_stream_open")).toEqual([
      { cmd: "daemon_stream_send", args: { id: 8, data: "last" } },
      { cmd: "daemon_stream_close", args: { id: 8 } },
    ]);
  });

  it("decodes base64 to the bytes the daemon sent", () => {
    expect(new Uint8Array(decodeBinary(btoa("\x00\x1b[2;3H\xff")))).toEqual(new Uint8Array([0, 27, 91, 50, 59, 51, 72, 255]));
  });
});
