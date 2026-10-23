import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

/* The shell's IPC, recorded: each invoke, and what pty_view_status answers. */
const ipc = vi.hoisted(() => {
  const calls: Array<{ cmd: string; args: Record<string, unknown> }> = [];
  class Channel<T> {
    onmessage: ((message: T) => void) | null = null;
  }
  const state ={ status: { kind: "ready", tmux: "tmux 3.8" } as unknown, tauri: true, clipboard: null as string | null };
  const invoke = vi.fn((cmd: string, args: Record<string, unknown> = {}) => {
    calls.push({ cmd, args });
    if (cmd === "pty_view_status") return Promise.resolve(state.status);
    // No clipboard (null): the shell's copy and paste fail, as the plugin's would.
    if (cmd === "pty_view_copy" || cmd === "pty_view_paste") return state.clipboard === null ? Promise.reject("no clipboard here") : Promise.resolve(cmd === "pty_view_paste" ? state.clipboard : undefined);
    if (cmd === "pty_view_open") return Promise.resolve(7);
    return Promise.resolve(undefined);
  });
  return { calls, Channel, invoke, state };
});

vi.mock("@tauri-apps/api/core", () => ({ Channel: ipc.Channel, invoke: ipc.invoke, isTauri: () => ipc.state.tauri }));

/* What goes to the debug log. */
const logs = vi.hoisted(() => [] as unknown[][]);
vi.mock("./debug", () => ({ logDebug: (...args: unknown[]) => void logs.push(args) }));

import { Launch } from "../screens/FirstRun/Launch";
import { AppPtyViews, tauriCopy, tauriPaste, tauriPtyView, usePtyViews, type PtyViews } from "./ptyView";
import type { Setup } from "./setup";

beforeEach(() => {
  ipc.calls.length = 0;
  ipc.state.status = { kind: "ready", tmux: "tmux 3.8" };
  ipc.state.tauri = true;
  ipc.state.clipboard = "";
  logs.length = 0;
});
afterEach(cleanup);

function seen() {
  const got: { views: PtyViews | null } = { views: null };
  function Probe() {
    got.views = usePtyViews();
    return null;
  }
  render(
    <AppPtyViews>
      <Probe />
    </AppPtyViews>,
  );
  return got;
}

const settle = () => act(() => new Promise((r) => setTimeout(r, 0)));

it("in the app the pty view is the default: the shell's status is asked once, and panes open through its pty_view commands", async () => {
  const got = seen();
  expect(got.views?.status).toEqual({ kind: "pending" });
  await settle();
  expect(got.views?.status).toEqual({ kind: "ready", tmux: "tmux 3.8" });
  expect(got.views?.open).toBe(tauriPtyView);
  expect(got.views?.copy).toBe(tauriCopy);
  expect(ipc.calls.filter((c) => c.cmd === "pty_view_status")).toHaveLength(1);
});

it("takes the shell's word: unusable, with why", async () => {
  ipc.state.status = { kind: "unusable", message: "The app's tmux is tmux 3.0a, …" };
  const got = seen();
  await settle();
  expect(got.views?.status).toEqual({ kind: "unusable", message: "The app's tmux is tmux 3.0a, …" });
});

it("asks the shell again when setup reaches ready, without a reload: a tmux installed meanwhile turns unusable into ready", async () => {
  ipc.state.status = { kind: "unusable", message: "couldn't run tmux" };
  // The first run's setup, until it says ready; then the app (a probe of the pty views).
  let ready: () => void = () => {};
  const setup = {
    connect: () => new Promise<void>((ok) => (ready = ok)),
  } as unknown as Setup;
  const got: { views: PtyViews | null } = { views: null };
  function Probe() {
    got.views = usePtyViews();
    return null;
  }
  render(
    <AppPtyViews>
      <Launch setup={setup}>{() => <Probe />}</Launch>
    </AppPtyViews>,
  );
  await settle();
  expect(ipc.calls.filter((c) => c.cmd === "pty_view_status")).toHaveLength(1);
  // Setup installs tmux, then reaches ready: asked again, and the answer taken.
  ipc.state.status = { kind: "ready", tmux: "tmux 3.8" };
  await act(async () => ready());
  await settle();
  expect(ipc.calls.filter((c) => c.cmd === "pty_view_status")).toHaveLength(2);
  expect(got.views?.status).toEqual({ kind: "ready", tmux: "tmux 3.8" });
});

it("takes the latest answer when asked again before the last came back", async () => {
  const answers: ((s: unknown) => void)[] = [];
  const held = (cmd: string, args: Record<string, unknown> = {}) => {
    ipc.calls.push({ cmd, args });
    return new Promise((ok) => answers.push(ok));
  };
  ipc.invoke.mockImplementationOnce(held).mockImplementationOnce(held);
  const got = seen();
  act(() => got.views!.recheck());
  // The second (newer) answer comes first, the stale first one after: the newer stays.
  await act(async () => answers[1]({ kind: "ready", tmux: "tmux 3.8" }));
  await act(async () => answers[0]({ kind: "unusable", message: "old" }));
  expect(got.views?.status).toEqual({ kind: "ready", tmux: "tmux 3.8" });
});

it("in a browser tab, with no shell, there are no pty views", () => {
  ipc.state.tauri = false;
  expect(seen().views).toBeNull();
  expect(ipc.calls).toEqual([]);
});

/* What pty_view_open's channel says to the page, as the shell would. */
const said = (event: unknown) => (ipc.calls.find((c) => c.cmd === "pty_view_open")!.args.channel as { onmessage(e: unknown): void }).onmessage(event);
const commands = () => ipc.calls.map((c) => c.cmd).filter((c) => c !== "pty_view_copy");

it("opens, types, resizes and closes a view in order once it says it opened, and copies through the shell", async () => {
  const data: number[][] = [];
  const view = tauriPtyView({ server: "brindle", target: "s:@1" }, { cols: 80, rows: 24 }, { onData: (b) => data.push([...b]), onExit() {}, onError() {} });
  view.write("a");
  view.resize(100, 30);
  view.focus();
  tauriCopy("copied");
  await settle();
  // The id is in, the view still opening: nothing is sent to it yet.
  expect(commands()).toEqual(["pty_view_open"]);
  expect(ipc.calls[0].args).toMatchObject({ server: "brindle", target: "s:@1", cols: 80, rows: 24 });
  act(() => said({ kind: "open" }));
  act(() => said({ kind: "data", data: btoa("hi") }));
  view.close();
  await settle();
  expect(commands()).toEqual(["pty_view_open", "pty_view_write", "pty_view_resize", "pty_view_focus", "pty_view_close"]);
  expect(ipc.calls.find((c) => c.cmd === "pty_view_write")!.args).toEqual({ id: 7, data: "a" });
  expect(ipc.calls.find((c) => c.cmd === "pty_view_focus")!.args).toEqual({ id: 7 });
  expect(ipc.calls.find((c) => c.cmd === "pty_view_copy")!.args).toEqual({ text: "copied" });
  expect(data).toEqual([[104, 105]]);
});

it("logs each copy and paste through the shell's clipboard, and why one failed, by size, never the text; a copy says whether it landed", async () => {
  await expect(tauriCopy("secret ✓")).resolves.toBeUndefined();
  expect(logs).toEqual([["pty_view", "info", "copied onto the clipboard", { bytes: 8 }]]);
  ipc.state.clipboard = "pasted";
  expect(await tauriPaste()).toBe("pasted");
  expect(logs.at(-1)).toEqual(["pty_view", "info", "read the clipboard for a paste", { bytes: 6 }]);
  // The plugin refused: the copy fails (so no "Copied" is shown for it), the paste goes quietly; the reason to the log.
  ipc.state.clipboard = null;
  await expect(tauriCopy("secret ✓")).rejects.toBe("no clipboard here");
  expect(logs.at(-1)).toEqual(["pty_view", "error", "couldn't copy onto the clipboard: no clipboard here", { bytes: 8 }]);
  expect(await tauriPaste()).toBe("");
  expect(logs.at(-1)).toEqual(["pty_view", "error", "couldn't read the clipboard for a paste: no clipboard here"]);
  expect(JSON.stringify(logs)).not.toContain("secret");
});

it("closes a view still opening at once, so the shell drops it as it opens; and says why one couldn't open", async () => {
  const errors: string[] = [];
  const view = tauriPtyView({ server: "brindle", target: "s:@1" }, { cols: 80, rows: 24 }, { onData() {}, onExit() {}, onError: (m) => errors.push(m) });
  view.write("typed before it opened");
  view.close();
  await settle();
  expect(commands()).toEqual(["pty_view_open", "pty_view_close"]);
  expect(ipc.calls.at(-1)!.args).toEqual({ id: 7 });
  ipc.calls.length = 0;
  tauriPtyView({ server: "brindle", target: "s:@1" }, { cols: 80, rows: 24 }, { onData() {}, onExit() {}, onError: (m) => errors.push(m) });
  act(() => said({ kind: "error", message: "The agents' tmux server is tmux 3.0a, …" }));
  expect(errors).toEqual(["The agents' tmux server is tmux 3.0a, …"]);
});
