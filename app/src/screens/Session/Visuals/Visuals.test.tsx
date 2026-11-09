import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import App from "../../../App";
import { DaemonClient, type Plan, type PlanVerdict, type VisualDTO } from "../../../api/client";
import { ONBOARDED_KEY } from "../../../api/providers";
import { PtyViewsProvider } from "../../../api/ptyView";
import type { InboxItemDTO, SessionDTO } from "../../../api/schema";
import { createAppStore } from "../../../store/store";
import { startSync } from "../../../store/sync";
import { FakeDaemon } from "../../../test/fakeDaemon";
import { REPO, question, root, waiting } from "../../../test/fixtures";
import { standInViews } from "../../../test/panes";
import type { PlansClient, VisualsClient } from "./client";
import { VisualFrame, type VisualFrameProps, type VisualSource } from "./frame";
import { StoreContext } from "../../../store/useStore";
import { VisualReader } from "./VisualReader";
import { useVisuals } from "./useVisuals";
import { OfferChip } from "./OfferChip";
import { VisualsSetting } from "../../Settings/VisualsSetting";
import { withTypedWords, withVisualInput } from "./terminalInput";

vi.mock("../../../components/PtyTerminal", () => import("../../../test/panes"));
const native = vi.hoisted(() => ({
  enabled: false,
  invoke: vi.fn(),
  events: new Map<string, (event: { payload: unknown }) => void>(),
}));
vi.mock("@tauri-apps/api/core", async (original) => ({ ...await original<typeof import("@tauri-apps/api/core")>(), isTauri: () => native.enabled, invoke: native.invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async (name: string, handler: (event: { payload: unknown }) => void) => {
  native.events.set(name, handler);
  return () => { if (native.events.get(name) === handler) native.events.delete(name); };
}) }));

const drops = vi.hoisted(() => ({ handle: (_event: unknown) => {}, clipboard: (): Promise<unknown[]> => Promise.resolve([]) }));
vi.mock("../../../api/attachments", async (original) => ({
  ...await original<typeof import("../../../api/attachments")>(),
  clipboardAttachments: () => drops.clipboard(),
  listenForDrops: async (handle: typeof drops.handle) => { drops.handle = handle; return () => {}; },
}));

const now = Date.now() / 1000;
const visual: VisualDTO = {
  id: "v_8f2c",
  session: "sup1",
  title: "Release flow",
  by: "supervisor",
  kind: "visual",
  rev: 2,
  question: "Which flow should 0.6.2 use?",
  options: [
    { id: "A", label: "A · Tag first" },
    { id: "B", label: "B · Gate on CI" },
  ],
  answer: null,
  kept: false,
  unread: true,
  created_at: now - 60,
};
const answered: VisualDTO = { ...visual, id: "v_old", title: "Old pick", answer: { option: "A", note: "", at: now }, unread: false };

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(ONBOARDED_KEY, "1");
  native.invoke.mockReset();
});
afterEach(() => {
  cleanup();
  native.enabled = false;
  native.events.clear();
  document.documentElement.style.removeProperty("--canvas");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("visuals", () => {
  it("refreshes context after direct messages without replaying a superseded submission", async () => {
    vi.useFakeTimers();
    let context = 1, lastSequence = 0, registrations = 0;
    let invalidatedAt = 0;
    let granted = false, hold = false, holdBeforeSnapshot = false;
    let resolveSnapshot: (() => void) | undefined;
    const seen: string[] = [];
    const open = withVisualInput({
      beginVisualInput: async () => { registrations++; return { generation: 1 }; },
      refreshVisualInput: async (_id, _observer, _expires, _signal, submittedAt) => {
        if (hold && holdBeforeSnapshot) { hold = false; await new Promise<void>(ok => { resolveSnapshot = ok; }); }
        const snapshot = context;
        if (hold) { hold = false; await new Promise<void>(ok => { resolveSnapshot = ok; }); }
        if (submittedAt <= invalidatedAt) throw new Error("superseded submission");
        return { generation: snapshot };
      },
      visualInput: async (_id, text, _expires, _signal, observation) => {
        seen.push(text);
        if (observation?.generation !== context || observation.sequence <= lastSequence || observation.submitted_at <= invalidatedAt) throw new Error("superseded");
        lastSequence = observation.sequence;
        granted = text === "Make a visual";
        return { observed: true };
      },
    }, "codex", () => ({ write() {}, resize() {}, focus() {}, close() {} }));
    const view = open({ server: null, target: "%1" }, { cols: 80, rows: 24 }, { onData() {}, onExit() {}, onError() {} });
    try {
      view.write("Make a visual\r");
      await act(async () => undefined);
      expect(granted).toBe(true);
      context++; // Direct person message: same terminal, new message context.
      view.write("Don't make a visual\r");
      await act(async () => undefined);
      expect(granted).toBe(false);
      expect(registrations).toBe(1);
      view.write("Make a visual\r");
      await act(async () => undefined);
      hold = true;
      view.write("Don't make a visual\r");
      await act(async () => undefined);
      view.write("Make a visual\r");
      await act(async () => undefined);
      await act(async () => resolveSnapshot?.());
      expect(granted).toBe(true);
      expect(seen).toHaveLength(4);
      hold = holdBeforeSnapshot = true;
      view.write("Make a visual\r");
      await act(async () => undefined);
      await act(async () => vi.advanceTimersByTime(10));
      invalidatedAt = Date.now() / 1000;
      context++; granted = false; // A later direct cancellation precedes refresh processing.
      await act(async () => resolveSnapshot?.());
      expect(granted).toBe(false);
      expect(seen).toHaveLength(4);
      await act(async () => vi.advanceTimersByTime(10));
      view.write("Make a visual\r");
      await act(async () => undefined);
      expect(granted).toBe(true);
    } finally { view.close(); vi.useRealTimers(); }
  });

  it.each(["interrupt", "deadline", "cancellation"])("recovers the PTY from pending consent recording on %s", async (how) => {
    vi.useFakeTimers();
    const bytes: string[] = [];
    let signal: AbortSignal | undefined;
    const observations: { generation: number; sequence: number }[] = [];
    const open = withVisualInput({ beginVisualInput: async () => ({ generation: 1 }), refreshVisualInput: async () => ({ generation: 1 }), visualInput: (_id, _text, _expiry, given, observation) => {
      signal ??= given; observations.push(observation!); return new Promise(() => {});
    } }, "codex",
      () => ({ write(data) { bytes.push(data); }, resize() {}, focus() {}, close() {} }));
    const view = open({ server: null, target: "%1" }, { cols: 80, rows: 24 }, { onData() {}, onExit() {}, onError() {} });
    try {
      view.write("hello"); view.write("\r");
      await act(async () => undefined);
      if (how === "interrupt") {
        view.write("\x03"); view.write("more");
        await act(async () => undefined);
        expect(bytes.join("")).toBe("hello\r\x03more");
      } else if (how === "deadline") {
        await act(async () => vi.advanceTimersByTime(300));
        expect(bytes.join("")).toBe("hello\r");
      } else {
        view.write("Don't make a visual\r");
        await act(async () => undefined);
        expect(observations.map(o => o.sequence)).toEqual([1, 2]);
        expect(observations[1].generation).toBe(observations[0].generation);
        expect(bytes.join("")).toBe("hello\r");
      }
      expect(signal?.aborted).toBe(true);
    } finally { view.close(); vi.useRealTimers(); }
  });

  it("asked_quote: only wholly typed input counts as typed, and a hook CLI's typed words are vouched for before Enter", async () => {
    const typed: (boolean | undefined)[] = [];
    const observed = withVisualInput({ beginVisualInput: async () => ({ generation: 1 }), refreshVisualInput: async () => ({ generation: 1 }), visualInput: async (_id, _text, _expiry, _signal, observation) => {
      typed.push(observation?.typed);
      return { observed: true };
    } }, "codex", () => ({ write() {}, resize() {}, focus() {}, close() {} }))({ server: null, target: "%1" }, { cols: 80, rows: 24 }, { onData() {}, onExit() {}, onError() {} });
    const send = async (...writes: string[]) => { writes.forEach(data => observed.write(data)); await act(async () => undefined); };
    await send(..."hi!", "\r");
    await send("\x1b[200~hi there\x1b[201~", "\r");
    await send("hi there, pasted\r");
    await send("render first", "\r");   // an unbracketed paste, its Enter a separate write
    await send(..."ok", "\r");
    expect(typed).toEqual([true, false, false, false, true]);

    const bytes: string[] = [], words: string[] = [];
    let answer: { ok(): void; fail(): void } | undefined;
    const hooked = withTypedWords({ visualTypedWords: (_id, text) => { words.push(text); return new Promise((ok, no) => {
      answer = { ok: () => ok({ observed: true }), fail: () => no(new Error("refused")) };
    }); } }, "claude", () => ({ write(data) { bytes.push(data); }, resize() {}, focus() {}, close() {} }))({ server: null, target: "%1" }, { cols: 80, rows: 24 }, { onData() {}, onExit() {}, onError() {} });
    [..."hi", "\r"].forEach(data => hooked.write(data));
    expect(words).toEqual(["hi"]);
    expect(bytes).toEqual(["h", "i"]);          // Enter waits for the report
    await act(async () => answer?.ok());
    expect(bytes.at(-1)).toBe("\r");
    [..."no", "\r"].forEach(data => hooked.write(data));
    await act(async () => answer?.fail());      // a refused report still lets the prompt go, unvouched
    expect(bytes.at(-1)).toBe("\r");
    words.length = 0;
    hooked.write("\x1b[200~pasted words\x1b[201~");
    hooked.write("\r");
    await act(async () => undefined);
    expect(words).toEqual([]);                  // a paste is never vouched for
    expect(bytes.at(-1)).toBe("\r");
    hooked.close();
  });

  it("observes Codex PTY submissions before forwarding Enter, including edits, pastes and uncertain history", async () => {
    const submitted: string[] = [], bytes: string[] = [];
    let recorded: (() => void) | undefined;
    const open = withVisualInput({ beginVisualInput: async () => ({ generation: 1 }), refreshVisualInput: async () => ({ generation: 1 }), visualInput: async (_id, text) => {
      submitted.push(text);
      await new Promise<void>(ok => { recorded = ok; });
      return { observed: true };
    } }, "codex", () => ({ write(data) { bytes.push(data); }, resize() {}, focus() {}, close() {} }));
    const view = open({ server: null, target: "%1" }, { cols: 80, rows: 24 }, { onData() {}, onExit() {}, onError() {} });
    view.write("Make a visualx");
    view.write("\x7f\r");
    await act(async () => undefined);
    expect(submitted).toEqual(["Make a visual"]);
    expect(bytes.join("")).not.toContain("\r");
    await act(async () => recorded?.());
    view.write("\x1b[200~Compare choices.\nPlease make a visual.\x1b[201~\r");
    await act(async () => undefined);
    expect(submitted.at(-1)).toBe("Compare choices.\nPlease make a visual.");
    await act(async () => recorded?.());
    view.write("\x1b[A\r");
    await act(async () => undefined);
    view.write("\r"); view.write("y\r");
    await act(async () => undefined);
    expect(submitted).toHaveLength(3);
    expect(submitted.at(-1)).toBe("y");
    await act(async () => recorded?.());
    view.write("2\r");
    await act(async () => undefined);
    expect(submitted.at(-1)).toBe("2");
    await act(async () => recorded?.());
    expect(bytes.join("")).toContain("\x1b[A\r");
    view.write("Make a visual");
    view.write("\n"); // Ctrl+J inserts a draft newline rather than submitting.
    await act(async () => undefined);
    expect(submitted).toHaveLength(4);
    expect(bytes.at(-1)).toBe("\n");
    view.write("only if I ask later\r");
    await act(async () => undefined);
    expect(submitted).toHaveLength(5);
    expect(submitted.at(-1)).toBe("Make a visual\nonly if I ask later");
    await act(async () => recorded?.());
    for (const edit of ["\x1b[H", "\x05"]) {
      view.write(`Make a visual\n${edit}only if I ask later\r`);
      await act(async () => undefined);
      expect(submitted).toHaveLength(5);
    }
    view.close();
  });

  it("drops stale offer reads across sessions, dismisses and expires offers", async () => {
    vi.useFakeTimers();
    const store = createAppStore();
    const offer = { id: "o_1", kind: "layout", counts: {}, expires_at: Date.now() / 1000 + 5 };
    let oldRead: (value: typeof offer) => void = () => undefined;
    const decisions: boolean[] = [];
    const client = {
      visualOffer: async (root: string) => root === "old" ? new Promise<typeof offer>(ok => { oldRead = ok; }) : root === "empty" ? null : root === "confirm" ? { ...offer, id: "r_1", confirmation: "plan" as const, expires_at: Date.now() / 1000 + 1800 } : offer,
      resolveVisualOffer: async (_root: string, _id: string, accept: boolean) => { decisions.push(accept); return { accepted: accept }; },
    };
    const chip = (root: string) => <StoreContext.Provider value={store}><OfferChip client={client} root={root} /></StoreContext.Provider>;
    try {
      const view = render(chip("old"));
      view.rerender(chip("empty"));
      await act(async () => { oldRead(offer); });
      await act(async () => vi.advanceTimersByTime(2000));
      expect(screen.queryByRole("button")).toBeNull();
      view.rerender(chip("current"));
      await act(async () => undefined);
      await act(async () => vi.advanceTimersByTime(2000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Not now" })));
      expect(decisions).toEqual([false]);
      view.rerender(chip("expiry"));
      await act(async () => undefined);
      await act(async () => vi.advanceTimersByTime(2000));
      expect(screen.queryByRole("button")).toBeNull();
      view.rerender(chip("confirm"));
      await act(async () => undefined);
      await act(async () => vi.advanceTimersByTime(2000));
      await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create the planning document now?" })));
      expect(decisions).toEqual([false, true]);
      expect(screen.queryByRole("button")).toBeNull();
    } finally { vi.useRealTimers(); }
  });

  it("delays offers while typing, preserves focus and permits keyboard acceptance; setting saves independently", async () => {
    vi.useFakeTimers();
    const store = createAppStore();
    const accepted: boolean[] = [];
    const offer = { id: "o_1", kind: "options", counts: { options: 3, attributes: 4 }, expires_at: Date.now() / 1000 + 1800 };
    let mode: "on" | "off" = "off";
    const client = {
      visualOffer: async () => offer,
      resolveVisualOffer: async (_root: string, _offer: string, accept: boolean) => { accepted.push(accept); return { accepted: accept }; },
      userSettings: async () => ({ claude_permission_mode: "default" as const, visuals: mode }),
      patchUserSettings: async (body: { visuals?: "on" | "off" }) => { mode = body.visuals!; return client.userSettings(); },
    };
    try {
      render(<StoreContext.Provider value={store}><textarea aria-label="chat" /><OfferChip client={client} root="sup1" /><VisualsSetting client={client} /></StoreContext.Provider>);
      await act(async () => undefined);
      const chat = screen.getByRole("textbox");
      chat.focus();
      await act(async () => vi.advanceTimersByTime(1500));
      fireEvent.keyDown(chat, { key: "a" });
      await act(async () => vi.advanceTimersByTime(1500));
      expect(screen.queryByRole("button", { name: "Make a visual?" })).toBeNull();
      await act(async () => vi.advanceTimersByTime(500));
      expect(document.activeElement).toBe(chat);
      const chip = screen.getByRole("button", { name: "Make a visual?" });
      chip.focus();
      fireEvent.keyDown(chip, { key: "Tab" });
      expect(document.activeElement).toBe(chip);
      await act(async () => fireEvent.click(chip));
      expect(accepted).toEqual([true]);
      expect(screen.queryByRole("button", { name: "Make a visual?" })).toBeNull();
      await act(async () => fireEvent.click(screen.getByRole("switch", { name: "Visuals" })));
      expect(mode).toBe("on");
      expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
    } finally { vi.useRealTimers(); }
  });

  it("hosts the native page at the content bounds, relays choices and theme, and closes across reader lifecycles", async () => {
    native.enabled = true;
    vi.stubGlobal("matchMedia", () => ({ addEventListener() {}, removeEventListener() {} }));
    let bounds = { x: 120, y: 90, width: 600, height: 400 };
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({ ...bounds, top: bounds.y, left: bounds.x, right: bounds.x + bounds.width, bottom: bounds.y + bounds.height, toJSON() {} }));
    const layouts: (() => void)[] = [];
    vi.stubGlobal("ResizeObserver", class {
      constructor(layout: () => void) { layouts.push(layout); }
      observe() {}
      disconnect() {}
    });
    native.invoke.mockReset().mockImplementation(async (command) => command === "visual_open" ? "visual:v_8f2c:r2" : undefined);
    const client = new FakeDaemon().client();
    const answer = vi.spyOn(client, "answerVisual").mockResolvedValue({ ...visual, answer: { option: "B", note: "", at: now } });
    function Reader({ shown = true, rev = 2 }) {
      return shown ? <VisualReader client={client} visual={{ ...visual, unread: false, rev }} onBack={() => view.rerender(<Reader shown={false} />)} onAnswered={() => {}} /> : <div>Chat</div>;
    }
    const view = render(<Reader />);
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_open", { id: visual.id, rev: 2, bounds }));
    bounds = { x: 40, y: 100, width: 780, height: 300 };
    await act(async () => layouts.forEach((layout) => layout()));
    expect(native.invoke).toHaveBeenCalledWith("visual_set_bounds", { label: "visual:v_8f2c:r2", bounds });
    const deliver = (name: string, payload: unknown) => act(() => native.events.get(name)!({ payload }));
    const delivery = { label: "visual:v_8f2c:r2", visual: { kind: "revision", id: visual.id, rev: 2 }, message: { type: "choice", option: "B" } };
    deliver("visual:message", { ...delivery, visual: { ...delivery.visual, rev: 1 } });
    expect((screen.getByTestId("visual-send") as HTMLButtonElement).disabled).toBe(true);
    deliver("visual:message", delivery);
    expect(screen.getByRole("radio", { name: "B · Gate on CI" }).getAttribute("aria-checked")).toBe("true");
    expect(answer).not.toHaveBeenCalled();
    document.documentElement.style.setProperty("--canvas", "#123456");
    deliver("visual:loaded", { ...delivery, event: "loaded" });
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_post", { label: delivery.label, message: expect.objectContaining({ type: "brindle.theme", vars: expect.objectContaining({ "--canvas": "#123456" }) }) }));
    document.documentElement.style.setProperty("--canvas", "#abcdef");
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_post", { label: delivery.label, message: expect.objectContaining({ vars: expect.objectContaining({ "--canvas": "#abcdef" }) }) }));
    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_snapshot", { label: delivery.label }));
    deliver("visual:message", { ...delivery, message: { type: "brindle.snapshot", html: "<html><body>Flow</body></html>" } });
    const menu = await screen.findByRole("group", { name: "Export as" });
    // The menu is an overlay: its wrapper is absolutely positioned under the Export button. The layout box is checked in the browser (app/shots/visuals-shots.mjs), since jsdom has no layout.
    expect(menu.closest(".absolute")).not.toBeNull();
    // The native page steps aside so the menu shows.
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_set_bounds", { label: delivery.label, bounds: { ...bounds, width: 0, height: 0 } }));
    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    await waitFor(() => expect(native.invoke).toHaveBeenLastCalledWith("visual_set_bounds", { label: delivery.label, bounds }));
    fireEvent.click(screen.getByTestId("visual-send"));
    await waitFor(() => expect(answer).toHaveBeenCalledWith(visual.id, 2, "B", ""));
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_close", { label: delivery.label }));
    expect(native.events.size).toBe(0);
    // A revision change and a screen switch while the native open is still pending.
    let opened!: (label: string) => void;
    native.invoke.mockImplementation((command) => command === "visual_open" ? new Promise<string>((resolve) => { opened = resolve; }) : Promise.resolve());
    view.rerender(<Reader rev={3} />);
    await waitFor(() => expect(opened).toBeTypeOf("function"));
    view.rerender(<Reader shown={false} />);
    await act(async () => opened("visual:v_8f2c:r3"));
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_close", { label: "visual:v_8f2c:r3" }));
    native.invoke.mockImplementation(async (command) => command === "visual_open" ? "visual:kept:k_1" : undefined);
    const kept = { kind: "kept", id: "k_1" } as const;
    view.rerender(<VisualFrame source={kept} title="Kept" onMessage={() => {}} />);
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_open", { id: "k_1", rev: null, bounds }));
    view.rerender(<VisualFrame source={kept} title="Kept" onMessage={() => {}} snapshotRequest={1} />);
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_snapshot", { label: "visual:kept:k_1" }));
    native.invoke.mockClear();
    let closed!: () => void;
    native.invoke.mockImplementation((command) => command === "visual_close" ? new Promise<void>((resolve) => { closed = resolve; }) : Promise.resolve());
    deliver("visual:failed", { label: "visual:kept:k_1", visual: kept, why: "Blocker unavailable" });
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_close", { label: "visual:kept:k_1" }));
    expect(screen.queryByRole("alert")).toBeNull();
    await act(async () => closed());
    expect(screen.getByRole("alert")).toBeTruthy();
    await act(async () => layouts.forEach((layout) => layout()));
    expect(native.invoke.mock.calls.map(([command]) => command)).toEqual(["visual_close"]);
    // Blocker failure can arrive before the open command returns its child's label.
    native.invoke.mockImplementation((command) => command === "visual_open" ? new Promise<string>((resolve) => { opened = resolve; }) : Promise.resolve());
    opened = undefined!;
    view.rerender(<Reader rev={4} />);
    await waitFor(() => expect(opened).toBeTypeOf("function"));
    native.invoke.mockClear();
    deliver("visual:failed", { ...delivery, label: "visual:v_8f2c:r4", visual: { ...delivery.visual, rev: 4 }, why: "Blocker unavailable" });
    expect(screen.queryByRole("alert")).toBeNull();
    await act(async () => opened("visual:v_8f2c:r4"));
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(native.invoke.mock.calls.map(([command]) => command)).toEqual(["visual_close"]);
    view.unmount();
  });

  it("an open native visual steps aside under the reconnect overlay and comes back, the same page, once the engine answers", async () => {
    native.enabled = true;
    vi.stubGlobal("matchMedia", () => ({ addEventListener() {}, removeEventListener() {} }));
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    const bounds = { x: 0, y: 40, width: 900, height: 600 };
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({ ...bounds, top: bounds.y, left: bounds.x, right: bounds.width, bottom: bounds.y + bounds.height, toJSON() {} }));
    native.invoke.mockReset().mockImplementation(async (command) => command === "visual_open" ? "visual:v_8f2c:r2" : undefined);
    const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: now - 600, goal: "Ship it" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const client = daemon.client();
    vi.spyOn(client, "visuals").mockResolvedValue([visual]);
    vi.spyOn(client, "markVisualRead").mockResolvedValue({ ...visual, unread: false });
    vi.useFakeTimers();
    const store = createAppStore();
    // The stream comes back on the first try after a drop, a second later (the overlay's grace).
    const sync = startSync({ client, store, connect: daemon.connect, backoff: () => 1000 });
    const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
    const label = "visual:v_8f2c:r2";
    const lastBounds = () => native.invoke.mock.calls.filter(([command]) => command === "visual_set_bounds").at(-1)?.[1];
    try {
      render(
        <PtyViewsProvider value={standInViews}>
          <App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO, sheet: { kind: "visual", id: visual.id } }} />
        </PtyViewsProvider>,
      );
      await advance(1000);
      expect(native.invoke).toHaveBeenCalledWith("visual_open", { id: visual.id, rev: 2, bounds });
      expect(lastBounds()).toEqual({ label, bounds });

      // The engine drops: under the overlay the native page takes no room, so it can't show or take input over it.
      daemon.die();
      await advance(0);
      expect(screen.queryByTestId("reconnecting")).toBeNull();
      expect(lastBounds()).toEqual({ label, bounds });
      await advance(1000);
      expect(screen.getByTestId("reconnecting")).toBeTruthy();
      expect(lastBounds()).toEqual({ label, bounds: { ...bounds, width: 0, height: 0 } });

      // Back: the same page takes its room again on the next attempt; it was never closed or opened afresh.
      daemon.revive();
      await advance(1000);
      expect(store.getState().connection).toBe("open");
      expect(screen.queryByTestId("reconnecting")).toBeNull();
      expect(lastBounds()).toEqual({ label, bounds });
      expect(native.invoke.mock.calls.filter(([command]) => command === "visual_open")).toHaveLength(1);
      expect(native.invoke).not.toHaveBeenCalledWith("visual_close", expect.anything());
    } finally {
      sync.stop();
      vi.useRealTimers();
    }
  });

  it("opens from the header over the whole window with the sidebar hidden, and Esc or Back returns to the same sidebar and terminal", async () => {
    const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: now - 600, goal: "Ship it" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const client = daemon.client();
    // The engine's rows: marking read and keeping change them, as the daemon's store would (no bus event for a read).
    let rows = [visual, answered];
    const edit = (id: string, fields: Partial<VisualDTO>) => (rows = rows.map((v) => (v.id === id ? { ...v, ...fields } : v))).find((v) => v.id === id)!;
    vi.spyOn(client, "visuals").mockImplementation(async () => rows);
    const read = vi.spyOn(client, "markVisualRead").mockImplementation(async (id) => edit(id, { unread: false }));
    vi.spyOn(client, "keepVisual").mockImplementation(async (id) => edit(id, { kept: true }));
    const store = createAppStore();
    // The inbox as the engine has it: a blocked supervisor's question is one row, the same however often it asks.
    const supervisorAsks = { ...question, reason: "has a question", detail: null };
    const ask = (item: InboxItemDTO) => act(() => store.dispatch({ type: "snapshot", workspaces: store.getState().workspaces, inbox: [item] }));
    const needUser = (id: number, text: string) =>
      act(() => store.dispatch({ type: "stream", message: { type: "event", id, ts: now, kind: "autopilot.need_user", repo_root: REPO, root_id: "sup1", agent_id: "sup1", workspace_id: "proj", task_id: null, payload: { question: text } } }));
    const sync = startSync({ client, store, connect: daemon.connect });
    try {
      render(
        <PtyViewsProvider value={standInViews}>
          {/* No agent, as Inbox's "Open the session" opens it: the reader's open then names the root. */}
          <App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO }} />
        </PtyViewsProvider>,
      );
      const terminal = await screen.findByTestId("terminal", {}, { timeout: 4000 });
      const sidebar = () => screen.getByRole("complementary", { name: "Sidebar" });
      const aside = sidebar();
      // The person folds the open session's agents away; Back must find them still folded.
      fireEvent.click(await within(aside).findByRole("button", { name: "Hide sup1's agents" }));
      // The header uses an icon without exposing the old count text or an unread dot.
      const entry = await screen.findByTestId("visuals-button");
      expect(entry.querySelector("svg")).toBeTruthy();
      expect(entry.textContent).toBe("");
      expect(within(entry).queryByLabelText("unread")).toBeNull();
      needUser(1, "Which database should the cache use?");
      ask(supervisorAsks);
      fireEvent.click(entry);
      const list = screen.getByTestId("visuals-panel");
      expect(within(list).queryByTestId("visual-row-v_old")).toBeNull();
      fireEvent.click(within(list).getByTestId("visuals-decided"));
      expect(within(list).getByTestId("visual-row-v_old")).toBeTruthy();
      fireEvent.click(within(list).getByTestId("visual-row-v_8f2c"));

      // The reader takes the window; the terminal still mounted under it, hidden; the sidebar and the session's bar out of sight.
      expect(screen.getByTestId("visual-reader")).toBeTruthy();
      // Browser development keeps the frame placeholder without calling the desktop shell.
      expect(screen.getByTestId("visual-frame").dataset.src).toBe("brindle-visual://v_8f2c/r2");
      expect(native.invoke).not.toHaveBeenCalled();
      expect(screen.getByTestId("chat-area").className).toContain("hidden");
      expect(screen.getByTestId("terminal")).toBe(terminal);
      expect(screen.queryByRole("complementary", { name: "Sidebar" })).toBeNull();
      expect(screen.queryByRole("button", { name: /Visuals/ })).toBeNull();
      // Opening marks it read, though the header remains icon-only either way.
      await waitFor(() => expect(within(screen.getByTestId("visuals-button")).queryByLabelText("unread")).toBeNull());
      expect(screen.getByTestId("visuals-button").querySelector("svg")).toBeTruthy();
      expect(screen.getByTestId("visuals-button").textContent).toBe("");
      expect(read).toHaveBeenCalledTimes(1);
      expect(read).toHaveBeenCalledWith("v_8f2c", 2);
      // The ask there at opening badges nothing, read again or not; the supervisor asking anew (the same row) does.
      expect(screen.queryByTestId("visual-back-asking")).toBeNull();
      ask(supervisorAsks);
      expect(screen.queryByTestId("visual-back-asking")).toBeNull();
      needUser(2, "Should the cache be per user?");
      expect(screen.getByTestId("visual-back-asking")).toBeTruthy();
      // Keep: the engine's kept row shows at once.
      fireEvent.click(screen.getByTestId("visual-keep"));
      await waitFor(() => expect(screen.getByTestId("visual-keep").getAttribute("aria-pressed")).toBe("true"));

      // Esc: the chat back, the same terminal, the very sidebar it left (never unmounted, so as it was).
      act(() => void window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })));
      expect(screen.queryByTestId("visual-reader")).toBeNull();
      expect(screen.getByTestId("chat-area").className).not.toContain("hidden");
      expect(screen.getByTestId("terminal")).toBe(terminal);
      expect(sidebar()).toBe(aside);
      expect(within(sidebar()).getByTestId("tree-session-sup1")).toBeTruthy();
      expect(within(sidebar()).getByRole("button", { name: "Show sup1's agents" })).toBeTruthy();

      // Opened again, the sidebar hides again.
      fireEvent.click(screen.getByTestId("visuals-button"));
      fireEvent.click(within(screen.getByTestId("visuals-panel")).getByTestId("visual-row-v_8f2c"));
      expect(screen.queryByRole("complementary", { name: "Sidebar" })).toBeNull();
      // A fresh open starts the badge over; another ask taking the open one's place (the same count) badges it.
      expect(screen.queryByTestId("visual-back-asking")).toBeNull();
      ask({ ...waiting, agent_id: "sup1", workspace_id: "proj" });
      expect(screen.getByTestId("visual-back-asking")).toBeTruthy();

      // Back, the button: the same sidebar and terminal again.
      vi.spyOn(client, "keptVisuals").mockResolvedValue([{ id: "k_1", title: "Release flow", from_session_name: "Ship it", repo: REPO, answer: { option: "B", note: "", at: now }, kept_at: now }]);
      fireEvent.click(screen.getByTestId("visual-back"));
      expect(sidebar()).toBe(aside);
      expect(within(sidebar()).getByRole("button", { name: "Show sup1's agents" })).toBeTruthy();
      expect(screen.getByTestId("terminal")).toBe(terminal);

      // Knowledge › Kept visuals: the kept copy opens in the same reader, read-only, on its own scheme path.
      fireEvent.click(within(sidebar()).getByRole("button", { name: "Open Account › Usage" }));
      fireEvent.click(within(sidebar()).getByRole("button", { name: "Knowledge" }));
      fireEvent.click(await screen.findByRole("tab", { name: /Kept visuals/ }));
      fireEvent.click(await screen.findByTestId("kept-k_1"));
      const kept = screen.getByTestId("kept-reader");
      expect(within(kept).getByTestId("visual-frame").dataset.src).toBe("brindle-visual://kept/k_1");
      expect(within(kept).queryByTestId("visual-send")).toBeNull();
      // Esc closes the reader and stays in Knowledge; the next Esc, with nothing nested open, leaves it.
      fireEvent.keyDown(window, { key: "Escape" });
      expect(await screen.findByTestId("kept-k_1")).toBeTruthy();
      expect(screen.queryByTestId("kept-reader")).toBeNull();
      fireEvent.keyDown(window, { key: "Escape" });
      await waitFor(() => expect(screen.queryByTestId("kept-k_1")).toBeNull());
    } finally {
      sync.stop();
    }
  });

  it.each(["Escape", "Back", "Answer"])("returns to the session from an Inbox visual after %s", async (leave) => {
    const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: now, goal: "Ship it" };
    const item: InboxItemDTO = { kind: "visual", agent_id: "sup1", root_id: "sup1", workspace_id: "proj", visual_id: visual.id, reason: "asks you to choose" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], inbox: [item] });
    const client = daemon.client();
    let row = visual;
    vi.spyOn(client, "visuals").mockImplementation(async () => [answered, row]);
    vi.spyOn(client, "markVisualRead").mockImplementation(async () => (row = { ...row, unread: false }));
    vi.spyOn(client, "answerVisual").mockImplementation(async (_id, _rev, option, note) => {
      row = { ...row, answer: { option: option ?? null, note, at: now } };
      daemon.data.inbox = [];
      daemon.publish("visual.answered", { root_id: "sup1" });
      return row;
    });
    const store = createAppStore();
    const sync = startSync({ client, store, connect: daemon.connect });
    try {
      render(<PtyViewsProvider value={standInViews}><App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "inbox", session: "sup1" }} /></PtyViewsProvider>);
      const card = (await screen.findAllByTestId("inbox-card")).find((candidate) => candidate.dataset.kind === "visual")!;
      await waitFor(() => expect((within(card).getByRole("button", { name: "Open visual" }) as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(within(card).getByRole("button", { name: "Open visual" }));
      await waitFor(() => expect(screen.getByTestId("visual-frame").dataset.src).toBe("brindle-visual://v_8f2c/r2"));
      expect(screen.queryByRole("complementary", { name: "Sidebar" })).toBeNull();
      if (leave === "Answer") {
        fireEvent.click(screen.getByRole("radio", { name: "B · Gate on CI" }));
        fireEvent.click(screen.getByTestId("visual-send"));
        await waitFor(() => expect(store.getState().inbox).toEqual([]));
      }
      if (leave === "Escape") fireEvent.keyDown(window, { key: "Escape" });
      else if (leave === "Back") fireEvent.click(screen.getByTestId("visual-back"));
      expect((await screen.findByTestId("terminal")).textContent).toBe("sup1");
      expect(screen.getByTestId("chat-area").className).not.toContain("hidden");
      expect(screen.queryByTestId("inbox-session")).toBeNull();
      expect(screen.queryByTestId("visual-reader")).toBeNull();
      expect(screen.queryByTestId("inbox-card")).toBeNull();
    } finally { sync.stop(); }
  });

  it("keeps the logo, a Home button, clear of Back over an Inbox visual, and it goes Home", async () => {
    const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: now, goal: "Ship it" };
    const item: InboxItemDTO = { kind: "visual", agent_id: "sup1", root_id: "sup1", workspace_id: "proj", visual_id: visual.id, reason: "asks you to choose" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], inbox: [item] });
    const client = daemon.client();
    vi.spyOn(client, "visuals").mockResolvedValue([visual]);
    vi.spyOn(client, "markVisualRead").mockResolvedValue({ ...visual, unread: false });
    const store = createAppStore();
    const sync = startSync({ client, store, connect: daemon.connect });
    try {
      render(<PtyViewsProvider value={standInViews}><App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "inbox", session: "sup1" }} /></PtyViewsProvider>);
      const card = (await screen.findAllByTestId("inbox-card")).find((candidate) => candidate.dataset.kind === "visual")!;
      await waitFor(() => expect((within(card).getByRole("button", { name: "Open visual" }) as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(within(card).getByRole("button", { name: "Open visual" }));
      await screen.findByTestId("visual-reader");
      // The logo's own row: not inside the reader's toolbar, so Back is still its own control.
      const home = screen.getByRole("button", { name: "Home" });
      expect(screen.getByTestId("visual-back").parentElement?.contains(home)).toBe(false);
      fireEvent.click(home);
      expect(await screen.findByRole("region", { name: "New session" })).toBeTruthy();
      expect(screen.queryByTestId("visual-reader")).toBeNull();
    } finally { sync.stop(); }
  });

  it("sends a note-only answer from the free-text choice", async () => {
    let sent: unknown;
    const client = new DaemonClient({ baseUrl: "http://daemon/v1", token: "test", fetch: async (url, init) => {
      if (String(url).endsWith("/answer")) sent = JSON.parse(init!.body as string);
      return new Response(JSON.stringify({ ...visual, answer: { option: null, note: "Stage it first", at: now } }));
    } });
    function Reader() {
      const [row, setRow] = useState(visual);
      return <VisualReader client={client} visual={row} onBack={() => {}} onAnswered={setRow} Frame={() => <div />} />;
    }
    render(<Reader />);
    fireEvent.click(screen.getByRole("radio", { name: "B · Gate on CI" }));
    fireEvent.click(screen.getByRole("radio", { name: "Something else…" }));
    const send = screen.getByTestId("visual-send") as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    fireEvent.change(screen.getByRole("textbox", { name: "Your answer" }), { target: { value: "  Stage it first  " } });
    fireEvent.click(send);
    await screen.findByTestId("visual-answered");
    expect(sent).toEqual({ rev: 2, note: "Stage it first" });
    expect(screen.getByTestId("visual-answered").textContent).toContain("Stage it first");
  });

  it("submits the answer on Enter from the single-line note, the free-text box or a chosen option", async () => {
    const sent: unknown[] = [];
    const client = new DaemonClient({ baseUrl: "http://daemon/v1", token: "test", fetch: async (url, init) => {
      if (String(url).endsWith("/answer")) sent.push(JSON.parse(init!.body as string));
      return new Response(JSON.stringify({ ...visual, answer: { option: "B", note: "", at: now } }));
    } });
    function Reader() {
      const [row, setRow] = useState(visual);
      return <VisualReader client={client} visual={row} onBack={() => {}} onAnswered={setRow} Frame={() => <div />} />;
    }
    // A chosen option A with focus moved to an unchosen B: Enter leaves the answer to the native pick, so nothing is sent yet.
    render(<Reader />);
    fireEvent.click(screen.getByRole("radio", { name: "A · Tag first" }));
    const other = screen.getByRole("radio", { name: "B · Gate on CI" });
    other.focus();
    // fireEvent.keyDown returns false when the handler cancels the event; a browser then skips the native pick.
    expect(fireEvent.keyDown(other, { key: "Enter" })).toBe(true);
    fireEvent.click(other); // the native activation an uncanceled Enter produces on a focused radio
    expect(other.getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("radio", { name: "A · Tag first" }).getAttribute("aria-checked")).toBe("false");
    expect(sent).toEqual([]);
    cleanup();

    // A chosen option, Enter from its focused button with no note: exactly one answer, the option stays chosen.
    render(<Reader />);
    const option = screen.getByRole("radio", { name: "B · Gate on CI" });
    fireEvent.click(option);
    option.focus();
    fireEvent.keyDown(option, { key: "Enter" });
    await screen.findByTestId("visual-answered");
    expect(sent).toEqual([{ rev: 2, option: "B", note: "" }]);
    cleanup();

    sent.length = 0;
    render(<Reader />);
    fireEvent.click(screen.getByRole("radio", { name: "B · Gate on CI" }));
    const note = screen.getByRole("textbox", { name: "Add a note" });
    expect(note).toBeInstanceOf(HTMLInputElement);
    fireEvent.change(note, { target: { value: "Line one\nLine two" } });
    expect((note as HTMLInputElement).value).toBe("Line oneLine two");
    fireEvent.keyDown(note, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(note, { key: "Enter", isComposing: true });
    expect(sent).toEqual([]);
    fireEvent.keyDown(note, { key: "Enter" });
    await waitFor(() => expect(sent).toEqual([{ rev: 2, option: "B", note: "Line oneLine two" }]));
    cleanup();

    // Enter from the free-text box with no option chosen sends nothing.
    sent.length = 0;
    render(<Reader />);
    fireEvent.click(screen.getByRole("radio", { name: "Something else…" }));
    const own = screen.getByRole("textbox", { name: "Your answer" });
    fireEvent.keyDown(own, { key: "Enter" });
    expect(sent).toEqual([]);
    fireEvent.change(own, { target: { value: "Stage it" } });
    fireEvent.keyDown(own, { key: "Enter" });
    await waitFor(() => expect(sent).toEqual([{ rev: 2, option: undefined, note: "Stage it" }]));
  });

  it.each([null, { ...visual, id: "v_other" }, { ...visual, rev: 3 }])("ignores a pending answer after replacing its reader with %j", async (replacement) => {
    let resolve!: (row: VisualDTO) => void;
    const client = new FakeDaemon().client();
    vi.spyOn(client, "answerVisual").mockImplementation(() => new Promise((done) => { resolve = done; }));
    const onAnswered = vi.fn();
    const reader = (row: VisualDTO) => <VisualReader client={client} visual={row} onAnswered={onAnswered} onBack={() => view.rerender(<div>Old session</div>)} Frame={() => <div />} />;
    const view = render(reader(visual));
    fireEvent.click(screen.getByRole("radio", { name: "B · Gate on CI" }));
    fireEvent.click(screen.getByTestId("visual-send"));
    view.rerender(replacement ? reader(replacement) : <div>Another session</div>);
    await act(async () => resolve({ ...visual, answer: { option: "B", note: "", at: now } }));
    expect(replacement ? screen.getByTestId("visual-reader") : screen.getByText("Another session")).toBeTruthy();
    expect(onAnswered).not.toHaveBeenCalled();
  });

  it("can dismiss a visual prompt without answering, closing the reader like Back", async () => {
    const client = new FakeDaemon().client();
    const dismiss = vi.spyOn(client, "dismissVisual").mockResolvedValue(visual);
    const answer = vi.spyOn(client, "answerVisual");
    const onAnswered = vi.fn();
    const onBack = vi.fn();
    render(<VisualReader client={client} visual={visual} onAnswered={onAnswered} onBack={onBack} Frame={() => <div />} />);
    fireEvent.click(screen.getByTestId("visual-dismiss"));
    await waitFor(() => expect(onBack).toHaveBeenCalled());
    expect(dismiss).toHaveBeenCalledWith(visual.id, visual.rev);
    expect(onAnswered).toHaveBeenCalledWith(visual);
    expect(answer).not.toHaveBeenCalled();
  });

  const takesFiles: VisualDTO ={ ...visual, answer_agent_id: "a_sup", accepts_files: true };
  it("shows attach controls only when the provider accepts files, and sends uploaded paths after the uploads finish", async () => {
    const client = new FakeDaemon().client();
    let finish!: (r: { paths: string[]; text: string }) => void;
    const attach = vi.spyOn(client, "attach").mockImplementation(() => new Promise((done) => { finish = done; }));
    const answer = vi.spyOn(client, "answerVisual").mockResolvedValue({ ...takesFiles, answer: { option: "A", note: "", at: now, attachments: ["/up/a.png"] } });
    const view = render(<VisualReader client={client} visual={visual} onAnswered={() => {}} onBack={() => {}} Frame={() => <div />} />);
    expect(screen.queryByRole("button", { name: "Attach" })).toBeNull();
    view.rerender(<VisualReader client={client} visual={takesFiles} onAnswered={() => {}} onBack={() => {}} Frame={() => <div />} />);
    fireEvent.click(screen.getByRole("radio", { name: "A · Tag first" }));
    // Enter on Attach opens the picker; it doesn't submit the answer.
    expect(fireEvent.keyDown(screen.getByRole("button", { name: "Attach" }), { key: "Enter" })).toBe(true);
    expect(answer).not.toHaveBeenCalled();
    fireEvent.change(screen.getByTestId("visual-attach-input"), { target: { files: [new File(["x"], "a.png", { type: "image/png" })] } });
    await waitFor(() => expect(attach).toHaveBeenCalledWith("a_sup", { name: "a.png", data_base64: "eA==" }));
    expect((screen.getByTestId("visual-send") as HTMLButtonElement).disabled).toBe(true);
    await act(async () => finish({ paths: ["/up/a.png"], text: "" }));
    const send = screen.getByTestId("visual-send") as HTMLButtonElement;
    expect(send.disabled).toBe(false);
    fireEvent.click(send);
    await waitFor(() => expect(answer).toHaveBeenCalledWith(visual.id, visual.rev, "A", "", ["/up/a.png"]));
    view.rerender(<VisualReader client={client} visual={{ ...takesFiles, answer: { option: "A", note: "", at: now, attachments: ["/up/a.png"] } }} onAnswered={() => {}} onBack={() => {}} Frame={() => <div />} />);
    expect(screen.getByTestId("visual-answered").textContent).toContain("attached a.png");
  });

  it("attach: a copied file with filename text and drops upload, chips are removable, failures show, and a stale upload is dropped", async () => {
    const client = new FakeDaemon().client();
    const finish: ((r: { paths: string[]; text: string }) => void)[] = [];
    const attach = vi.spyOn(client, "attach").mockImplementation(() => new Promise((done) => { finish.push(done); }));
    const reader = (row: VisualDTO) => <VisualReader client={client} visual={row} onAnswered={() => {}} onBack={() => {}} Frame={() => <div />} />;
    const view = render(reader(takesFiles));
    Object.defineProperty(document, "elementFromPoint", { configurable: true, value: (x: number) => x < 100 ? screen.getByTestId("visual-answer") : document.body });
    // A drop elsewhere is ignored; one over the bar uploads by path.
    await act(async () => drops.handle({ type: "drop", x: 500, y: 1, paths: ["/elsewhere"] }));
    expect(attach).not.toHaveBeenCalled();
    await act(async () => drops.handle({ type: "drop", x: 5, y: 1, paths: ["/Users/me/notes.txt"] }));
    expect(attach).toHaveBeenCalledWith("a_sup", { path: "/Users/me/notes.txt" });
    await act(async () => finish[0]({ paths: ["/up/notes.txt"], text: "" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove notes.txt" }));
    expect(screen.queryByText("notes.txt")).toBeNull();
    // A paste with files uploads them and is kept out of the note.
    fireEvent.paste(screen.getByLabelText("Add a note"), { clipboardData: { files: [new File(["y"], "shot.png")] } });
    await waitFor(() => expect(attach).toHaveBeenCalledTimes(2));
    await act(async () => finish[1]({ paths: ["/up/shot.png"], text: "" }));
    expect(screen.getByText("shot.png")).toBeTruthy();
    attach.mockRejectedValueOnce(new Error("too big"));
    await act(async () => drops.handle({ type: "drop", x: 5, y: 1, paths: ["/big"] }));
    expect(screen.getByRole("alert").textContent).toContain("too big");
    // Text suppresses image bytes; Finder's filename text still allows native files, across revision changes.
    native.enabled = true;
    await act(async () => { fireEvent.paste(screen.getByLabelText("Add a note"), { clipboardData: { files: [new File(["z"], "image.png", { type: "image/png" })], getData: () => "plain" } }); });
    expect((screen.getByLabelText("Add a note") as HTMLInputElement).value).toBe("plain");
    expect(attach).toHaveBeenCalledTimes(3);
    const note = screen.getByLabelText("Add a note") as HTMLInputElement;
    fireEvent.change(note, { target: { value: "" } });
    const reads: ((files: unknown[]) => void)[] = [];
    drops.clipboard = () => new Promise(done => reads.push(done));
    fireEvent.paste(note, { clipboardData: { files: [], getData: () => "pasted" } });
    fireEvent.change(note, { target: { value: `${note.value}x` } });
    note.setSelectionRange(note.value.length, note.value.length);
    fireEvent.paste(note, { clipboardData: { files: [], getData: () => "two" } });
    await act(async () => reads[1]([]));
    await act(async () => reads[0]([]));
    const ordered = note.value;
    drops.clipboard = () => Promise.reject(new Error("clipboard unavailable"));
    note.setSelectionRange(note.value.length, note.value.length);
    await act(async () => { fireEvent.paste(note, { clipboardData: { files: [], getData: () => "fallback" } }); });
    expect([ordered, note.value]).toEqual(["pastedxtwo", "pastedxtwofallback"]);
    expect(screen.getByRole("alert").textContent).toContain("clipboard unavailable");
    fireEvent.change(note, { target: { value: "" } });
    let clip!: (files: unknown[]) => void;
    drops.clipboard = () => new Promise((done) => { clip = done; });
    fireEvent.click(screen.getAllByRole("radio")[0]);
    const send = screen.getByTestId("visual-send") as HTMLButtonElement;
    expect(send.disabled).toBe(false);
    fireEvent.paste(screen.getByLabelText("Add a note"), { clipboardData: { files: [], getData: () => "old.png" } });
    expect(send.disabled).toBe(true);
    view.rerender(reader({ ...takesFiles, rev: 3 }));
    await act(async () => clip([{ path: "/clip/old.png" }]));
    expect(attach).toHaveBeenCalledTimes(3);
    const revisedNote = screen.getByLabelText("Add a note") as HTMLInputElement;
    fireEvent.change(revisedNote, { target: { value: "keep" } });
    revisedNote.setSelectionRange(1, 3);
    fireEvent.paste(revisedNote, { clipboardData: { files: [], getData: () => "new.png" } });
    fireEvent.change(revisedNote, { target: { value: `${revisedNote.value}x` } });
    await act(async () => clip([{ path: "/clip/new.png" }]));
    await waitFor(() => expect(attach).toHaveBeenLastCalledWith("a_sup", { path: "/clip/new.png" }));
    expect(revisedNote.value).toBe("keepx");
    // A result that lands after the revision changed is dropped.
    await act(async () => drops.handle({ type: "drop", x: 5, y: 1, paths: ["/late"] }));
    view.rerender(reader({ ...takesFiles, rev: 4 }));
    await act(async () => finish[3]({ paths: ["/up/late.txt"], text: "" }));
    expect(screen.queryByText("late.txt")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    const currentNote = screen.getByLabelText("Add a note") as HTMLInputElement;
    drops.clipboard = () => Promise.resolve([]);
    await act(async () => { fireEvent.paste(currentNote, { clipboardData: { files: [], getData: () => "a\r\nb" } }); });
    currentNote.setSelectionRange(currentNote.value.length, currentNote.value.length);
    await act(async () => { fireEvent.paste(currentNote, { clipboardData: { files: [], getData: () => "x" } }); });
    const outcomes = [currentNote.value];
    attach.mockResolvedValue({ paths: [], text: "" });
    for (const [end, order, secondFile] of [[null, [0, 1], true], [null, [1, 0], true], [3, [0, 1], true], [3, [1, 0], true], [3, [0, 1], false], [3, [1, 0], false]] as const) {
      fireEvent.change(currentNote, { target: { value: "keep" } });
      const pending: ((files: unknown[]) => void)[] = [];
      drops.clipboard = () => new Promise(done => pending.push(done));
      for (const name of ["one.png", secondFile ? "two.png" : "two"]) {
        currentNote.setSelectionRange(0, name === "one.png" ? currentNote.value.length : end ?? currentNote.value.length);
        fireEvent.paste(currentNote, { clipboardData: { files: [], getData: () => name } });
      }
      for (const index of order) await act(async () => pending[index](index && !secondFile ? [] : [{ path: index ? "/two.png" : "/one.png" }]));
      outcomes.push(currentNote.value);
    }
    expect(outcomes).toEqual(["abx", "keep", "keep", "keep", "keep", "two", "two"]);
  });

  const open: VisualSource = { kind: "session", id: visual.id, rev: visual.rev };
  it.each([
    ["another visual's choice", { ...open, id: "v_other" }, { type: "choice", option: "B" }, { picked: false, back: false }],
    ["an older revision's choice", { ...open, rev: 1 }, { type: "choice", option: "B" }, { picked: false, back: false }],
    ["an option the tool call didn't give", open, { type: "choice", option: "Z" }, { picked: false, back: false }],
    ["the open visual's Esc", open, { type: "brindle.esc" }, { picked: false, back: true }],
    ["the open visual's choice, sent", open, { type: "choice", option: "B" }, { picked: true, back: false }],
  ] as const)("%s", async (_name, from, data, want) => {
    let post: VisualFrameProps["onMessage"] = () => undefined;
    const Frame = ({ onMessage }: VisualFrameProps) => ((post = onMessage), <div data-testid="visual-frame" />);
    const sent: { rev: number; option: string | undefined; note: string }[] = [];
    // The engine's rows, and list reads it can hold: each answers with the rows as they were when asked.
    let rows: VisualDTO[] = [{ ...visual, unread: false }];
    let hold = false;
    const held: (() => void)[] = [];
    const fake: VisualsClient = {
      dismissVisual: async () => visual,
      visuals: () => {
        const asked = rows;
        return new Promise((ok) => (hold ? held.push(() => ok(asked)) : ok(asked)));
      },
      answerVisual: async (_id, rev, option, note) => {
        sent.push({ rev, option, note });
        rows = rows.map((v) => ({ ...v, answer: { option: option ?? null, note, at: now } }));
        return rows[0];
      },
      keepVisual: async () => rows[0],
      unkeepVisual: async () => rows[0],
      markVisualRead: async () => ({}),
    };
    const back = vi.fn();
    const store = createAppStore();
    function Reader() {
      const { visuals, put, read } = useVisuals(fake, visual.session);
      return <VisualReader client={fake} visual={visuals?.find((v) => v.id === visual.id) ?? null} loading={visuals === null} onBack={back} onAnswered={put} onRead={read} Frame={Frame} />;
    }
    render(
      <StoreContext.Provider value={store}>
        <Reader />
      </StoreContext.Provider>,
    );
    await screen.findByTestId("visual-frame");
    act(() => post(data, from));
    expect(back).toHaveBeenCalledTimes(want.back ? 1 : 0);
    const send = screen.getByTestId("visual-send") as HTMLButtonElement;
    expect(screen.getByRole("radio", { name: "B · Gate on CI" }).getAttribute("aria-checked")).toBe(String(want.picked));
    expect(send.disabled).toBe(!want.picked);
    if (!want.picked) return;
    const option = screen.getByRole("radio", { name: "B · Gate on CI" });
    fireEvent.click(option);
    expect(option.getAttribute("aria-checked")).toBe("false");
    expect(send.disabled).toBe(true);
    fireEvent.click(option);
    // A list read begun on the bus before the answer, still out when the answer lands.
    hold = true;
    await act(async () => store.dispatch({ type: "stream", message: { type: "event", id: 1, ts: now, kind: "visual.published", repo_root: null, root_id: visual.session, agent_id: null, workspace_id: null, task_id: null, payload: {} } }));
    hold = false;
    expect(held).toHaveLength(1);
    // Only the bar's Send answers, for the revision on screen.
    fireEvent.change(screen.getByLabelText("Add a note"), { target: { value: "ship it" } });
    fireEvent.click(send);
    await waitFor(() => expect(screen.getByTestId("visual-answered").textContent).toContain("B · Gate on CI"));
    expect(sent).toEqual([{ rev: 2, option: "B", note: "ship it" }]);
    // The older read's unanswered rows come in last, and don't bring the answer bar back.
    await act(async () => held.forEach((answer) => answer()));
    expect(screen.getByTestId("visual-answered")).toBeTruthy();
    expect(screen.queryByTestId("visual-send")).toBeNull();
  });

  it("renders a plan itself, sends per-section comments and picks on Redirect, and drops a read older than the verdict", async () => {
    const plan: Plan = {
      id: "v_p1", session: "sup1", title: "Org presets", rev: 1, state: "open", markdown: "…", created_at: now, queued: 2,
      lede: "One page. <img src=x onerror=alert(1)>", split: [{ id: "w1", title: "Schema", after: [] }],
      sections: [{ id: "s1", title: "Goal", markdown: "Presets per org." }, { id: "s2", title: "Risks", markdown: "A migration." }],
      decisions: [{ id: "d1", title: "Where presets live", mine: "org", options: [{ id: "org", label: "Org policy" }, { id: "repo", label: "A file in each repo" }] }],
    };
    const reads: ((p: Plan) => void)[] = [];
    const replies: ((p: Plan) => void)[] = [];
    const sent: PlanVerdict[] = [];
    const fake: VisualsClient & PlansClient = {
      visuals: async () => [], answerVisual: async () => visual, dismissVisual: async () => visual, keepVisual: async () => visual, unkeepVisual: async () => visual, markVisualRead: async () => ({}),
      plan: () => new Promise<Plan>((ok) => { reads.push(ok); }),
      decidePlan: (_id, verdict) => { sent.push(verdict); return new Promise<Plan>((ok) => { replies.push(ok); }); },
    };
    const redirected: Plan = { ...plan, state: "redirected", verdict: { approved: false, picks: { d1: "repo" }, comments: [{ section: "s1", text: "Repo file first." }], at: now, rev: 1 } };
    const row: VisualDTO = { ...visual, id: plan.id, kind: "plan", title: plan.title, rev: 1, question: undefined, options: undefined, unread: false };
    const reader = (v: VisualDTO) => <VisualReader client={fake} visual={v} onBack={() => {}} onAnswered={() => {}} />;
    const view = render(reader(row));
    await act(async () => reads[0](plan));
    // brindle's own rendering: the agent's raw HTML stays text, and there's no frame.
    expect(screen.getByText(/onerror=alert/)).toBeTruthy();
    expect(document.querySelector("img, iframe")).toBeNull();
    const redirect = screen.getByRole("button", { name: "Redirect" }) as HTMLButtonElement;
    expect(redirect.disabled).toBe(true);
    fireEvent.click(screen.getByRole("radio", { name: "A file in each repo" }));
    fireEvent.click(within(screen.getByRole("region", { name: "Goal" })).getByRole("button", { name: "Comment" }));
    fireEvent.change(screen.getByLabelText("Comment on Goal"), { target: { value: "Repo file first." } });
    // Another change to the row starts a read that is still out when the verdict lands.
    view.rerender(reader({ ...row, answer: { option: "x", note: "", at: now } }));
    await act(async () => fireEvent.click(redirect));
    expect(sent).toEqual([{ rev: 1, approved: false, picks: { d1: "repo" }, comments: [{ section: "s1", text: "Repo file first." }], note: "" }]);
    await act(async () => replies[0](redirected));
    await act(async () => reads[1](plan));
    expect(screen.getByTestId("plan-decided").textContent).toContain("Repo file first.");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    // The other order: a newer revision is read while an older verdict's reply is still out.
    view.rerender(reader({ ...row, rev: 2 }));
    await act(async () => reads[2]({ ...plan, rev: 2 }));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Approve" })));
    view.rerender(reader({ ...row, rev: 3 }));
    await act(async () => reads[3]({ ...plan, rev: 3 }));
    await act(async () => replies[1]({ ...plan, rev: 2, state: "approved", verdict: { approved: true, at: now, rev: 2 } }));
    expect(screen.getByText("plan · rev 3")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
    // Reopened later, a decided plan shows the alternative the person sent, not the supervisor's.
    view.unmount();
    render(reader(row));
    await act(async () => reads[4](redirected));
    expect(screen.getByRole("radio", { name: "A file in each repo" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("radio", { name: /Org policy/ }).getAttribute("aria-checked")).toBe("false");
  });
});
