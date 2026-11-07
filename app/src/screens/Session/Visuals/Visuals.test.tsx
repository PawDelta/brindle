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
import type { VisualFrameProps, VisualSource } from "./frame";
import { StoreContext } from "../../../store/useStore";
import { VisualReader } from "./VisualReader";
import { useVisuals } from "./useVisuals";
import { OfferChip } from "./OfferChip";
import { VisualsSetting } from "../../Settings/VisualsSetting";
import { withVisualInput } from "./terminalInput";

vi.mock("../../../components/PtyTerminal", () => import("../../../test/panes"));

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
});
afterEach(() => {
  cleanup();
  setFocus(false);
});

describe("visuals", () => {
  it.each(["interrupt", "deadline", "cancellation"])("recovers the PTY from pending consent recording on %s", async (how) => {
    vi.useFakeTimers();
    const bytes: string[] = [];
    let signal: AbortSignal | undefined;
    const observations: { generation: number; sequence: number }[] = [];
    const open = withVisualInput({ beginVisualInput: async () => ({ generation: 1 }), visualInput: (_id, _text, _expiry, given, observation) => {
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

  it("observes Codex PTY submissions before forwarding Enter, including edits, pastes and uncertain history", async () => {
    const submitted: string[] = [], bytes: string[] = [];
    let recorded: (() => void) | undefined;
    const open = withVisualInput({ beginVisualInput: async () => ({ generation: 1 }), visualInput: async (_id, text) => {
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
