import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../../../App";
import type { VisualDTO } from "../../../api/client";
import { ONBOARDED_KEY } from "../../../api/providers";
import { PtyViewsProvider } from "../../../api/ptyView";
import type { InboxItemDTO, SessionDTO } from "../../../api/schema";
import { createAppStore } from "../../../store/store";
import { startSync } from "../../../store/sync";
import { FakeDaemon } from "../../../test/fakeDaemon";
import { REPO, question, root, waiting } from "../../../test/fixtures";
import { standInViews } from "../../../test/panes";
import type { VisualsClient } from "./client";
import { FOCUS_KEY, setFocus } from "./focus";
import { VisualFrame, type VisualFrameProps, type VisualSource } from "./frame";
import { StoreContext } from "../../../store/useStore";
import { VisualReader } from "./VisualReader";
import { useVisuals } from "./useVisuals";

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
  setFocus(false);
  native.enabled = false;
  native.events.clear();
  document.documentElement.style.removeProperty("--canvas");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("visuals", () => {
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
    fireEvent.click(screen.getByTestId("visual-send"));
    await waitFor(() => expect(answer).toHaveBeenCalledWith(visual.id, 2, "B", ""));
    document.documentElement.style.setProperty("--canvas", "#123456");
    deliver("visual:loaded", { ...delivery, event: "loaded" });
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_post", { label: delivery.label, message: expect.objectContaining({ type: "brindle.theme", vars: expect.objectContaining({ "--canvas": "#123456" }) }) }));
    document.documentElement.style.setProperty("--canvas", "#abcdef");
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_post", { label: delivery.label, message: expect.objectContaining({ vars: expect.objectContaining({ "--canvas": "#abcdef" }) }) }));
    fireEvent.click(screen.getByRole("button", { name: "Export ▾" }));
    await waitFor(() => expect(native.invoke).toHaveBeenCalledWith("visual_snapshot", { label: delivery.label }));
    deliver("visual:message", { ...delivery, message: { type: "brindle.snapshot", html: "<html><body>Flow</body></html>" } });
    const menu = await screen.findByRole("group", { name: "Export as" });
    expect(menu.closest("header")).toBeTruthy();
    // Native children cover DOM overlays: destinations must participate in header layout.
    expect(menu.closest(".absolute, .fixed")).toBeNull();
    bounds = { ...bounds, y: bounds.y + 80, height: bounds.height - 80 };
    await act(async () => layouts.forEach((layout) => layout()));
    expect(native.invoke).toHaveBeenCalledWith("visual_set_bounds", { label: delivery.label, bounds });
    fireEvent.click(screen.getByRole("button", { name: "Export ▾" }));
    fireEvent.click(screen.getByTestId("visual-back"));
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

  it("opens from the header in the chat's place with the sidebar as a rail, Focus hides it and is remembered, and Esc returns to the same terminal", async () => {
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
          <App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO, agent: "sup1" }} />
        </PtyViewsProvider>,
      );
      const terminal = await screen.findByTestId("terminal", {}, { timeout: 4000 });
      const sidebar = () => screen.getByRole("complementary", { name: "Sidebar" });
      // The header's count is the visuals still open; the decided one is folded away in the list.
      const entry = await screen.findByTestId("visuals-button");
      expect(entry.textContent).toContain("1");
      expect(within(entry).getByLabelText("unread")).toBeTruthy();
      needUser(1, "Which database should the cache use?");
      ask(supervisorAsks);
      fireEvent.click(entry);
      const list = screen.getByTestId("visuals-panel");
      expect(within(list).queryByTestId("visual-row-v_old")).toBeNull();
      fireEvent.click(within(list).getByTestId("visuals-decided"));
      expect(within(list).getByTestId("visual-row-v_old")).toBeTruthy();
      fireEvent.click(within(list).getByTestId("visual-row-v_8f2c"));

      // The reader in the chat's place; the terminal still mounted under it, hidden; the sidebar a rail.
      expect(screen.getByTestId("visual-reader")).toBeTruthy();
      // Browser development keeps the frame placeholder without calling the desktop shell.
      expect(screen.getByTestId("visual-frame").dataset.src).toBe("brindle-visual://v_8f2c/r2");
      expect(native.invoke).not.toHaveBeenCalled();
      expect(screen.getByTestId("chat-area").className).toContain("hidden");
      expect(screen.getByTestId("terminal")).toBe(terminal);
      expect(sidebar().dataset.mode).toBe("rail");
      // Opened, it's read: the header's dot goes, with no event on the bus to say so.
      await waitFor(() => expect(within(screen.getByTestId("visuals-button")).queryByLabelText("unread")).toBeNull());
      expect(read).toHaveBeenCalledTimes(1);
      // The ask there at opening badges nothing, read again or not; the supervisor asking anew (the same row) does.
      expect(screen.queryByTestId("visual-back-asking")).toBeNull();
      ask(supervisorAsks);
      expect(screen.queryByTestId("visual-back-asking")).toBeNull();
      needUser(2, "Should the cache be per user?");
      expect(screen.getByTestId("visual-back-asking")).toBeTruthy();
      // Keep: the engine's kept row shows at once.
      fireEvent.click(screen.getByTestId("visual-keep"));
      await waitFor(() => expect(screen.getByTestId("visual-keep").getAttribute("aria-pressed")).toBe("true"));

      // Focus hides the sidebar down to its edge handle, and the device remembers it.
      fireEvent.click(screen.getByTestId("visual-focus"));
      expect(screen.getByTestId("sidebar-handle")).toBeTruthy();
      expect(screen.queryByRole("complementary", { name: "Sidebar" })).toBeNull();
      expect(localStorage.getItem(FOCUS_KEY)).toBe("1");

      // Esc: the chat back, the same terminal, the full sidebar.
      act(() => void window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })));
      expect(screen.queryByTestId("visual-reader")).toBeNull();
      expect(screen.getByTestId("chat-area").className).not.toContain("hidden");
      expect(screen.getByTestId("terminal")).toBe(terminal);
      expect(within(sidebar()).getByTestId("tree-session-sup1")).toBeTruthy();

      // Opened again, Focus is still on; the handle brings the rail back.
      fireEvent.click(screen.getByTestId("visuals-button"));
      fireEvent.click(within(screen.getByTestId("visuals-panel")).getByTestId("visual-row-v_8f2c"));
      fireEvent.click(screen.getByTestId("sidebar-handle"));
      expect(sidebar().dataset.mode).toBe("rail");
      // A fresh open starts the badge over; another ask taking the open one's place (the same count) badges it.
      expect(screen.queryByTestId("visual-back-asking")).toBeNull();
      ask({ ...waiting, agent_id: "sup1", workspace_id: "proj" });
      expect(screen.getByTestId("visual-back-asking")).toBeTruthy();

      // Knowledge › Kept visuals: the kept copy opens in the same reader, read-only, on its own scheme path.
      vi.spyOn(client, "keptVisuals").mockResolvedValue([{ id: "k_1", title: "Release flow", from_session_name: "Ship it", repo: REPO, answer: { option: "B", note: "", at: now }, kept_at: now }]);
      act(() => void window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })));
      fireEvent.click(within(sidebar()).getByRole("button", { name: "Knowledge" }));
      fireEvent.click(await screen.findByRole("tab", { name: /Kept visuals/ }));
      fireEvent.click(await screen.findByTestId("kept-k_1"));
      const kept = screen.getByTestId("kept-reader");
      expect(within(kept).getByTestId("visual-frame").dataset.src).toBe("brindle-visual://kept/k_1");
      expect(within(kept).queryByTestId("visual-send")).toBeNull();
    } finally {
      sync.stop();
    }
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
    const sent: { rev: number; option: string; note: string }[] = [];
    // The engine's rows, and list reads it can hold: each answers with the rows as they were when asked.
    let rows: VisualDTO[] = [{ ...visual, unread: false }];
    let hold = false;
    const held: (() => void)[] = [];
    const fake: VisualsClient = {
      visuals: () => {
        const asked = rows;
        return new Promise((ok) => (hold ? held.push(() => ok(asked)) : ok(asked)));
      },
      answerVisual: async (_id, rev, option, note) => {
        sent.push({ rev, option, note });
        rows = rows.map((v) => ({ ...v, answer: { option, note, at: now } }));
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
});
