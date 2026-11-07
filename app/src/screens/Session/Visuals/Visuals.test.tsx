import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../../../App";
import type { VisualDTO } from "../../../api/client";
import { ONBOARDED_KEY } from "../../../api/providers";
import { PtyViewsProvider } from "../../../api/ptyView";
import type { SessionDTO } from "../../../api/schema";
import { createAppStore } from "../../../store/store";
import { startSync } from "../../../store/sync";
import { FakeDaemon } from "../../../test/fakeDaemon";
import { REPO, root } from "../../../test/fixtures";
import { standInViews } from "../../../test/panes";
import type { VisualsClient } from "./client";
import { FOCUS_KEY, setFocus } from "./focus";
import type { VisualFrameProps, VisualSource } from "./frame";
import { VisualReader } from "./VisualReader";

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
  it("opens from the header in the chat's place with the sidebar as a rail, Focus hides it and is remembered, and Esc returns to the same terminal", async () => {
    const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: now - 600, goal: "Ship it" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const client = daemon.client();
    vi.spyOn(client, "visuals").mockResolvedValue([visual, answered]);
    const read = vi.spyOn(client, "markVisualRead").mockResolvedValue({});
    const store = createAppStore();
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
      await waitFor(() => expect(read).toHaveBeenCalledWith("v_8f2c"));
      // Keep: the engine's kept row shows at once.
      vi.spyOn(client, "keepVisual").mockResolvedValue({ ...visual, kept: true, unread: false });
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
    const fake: VisualsClient = {
      visuals: async () => [visual],
      answerVisual: async (_id, rev, option, note) => (sent.push({ rev, option, note }), { ...visual, unread: false, answer: { option, note, at: now } }),
      keepVisual: async () => ({ ...visual, kept: true }),
      unkeepVisual: async () => visual,
      markVisualRead: async () => ({}),
    };
    const back = vi.fn();
    function Reader() {
      const [shown, setShown] = useState(visual);
      return <VisualReader client={fake} visual={shown} onBack={back} onAnswered={setShown} Frame={Frame} />;
    }
    render(<Reader />);
    act(() => post(data, from));
    expect(back).toHaveBeenCalledTimes(want.back ? 1 : 0);
    const send = screen.getByTestId("visual-send") as HTMLButtonElement;
    expect(screen.getByRole("radio", { name: "B · Gate on CI" }).getAttribute("aria-checked")).toBe(String(want.picked));
    expect(send.disabled).toBe(!want.picked);
    if (!want.picked) return;
    // Only the bar's Send answers, for the revision on screen.
    fireEvent.change(screen.getByLabelText("Add a note"), { target: { value: "ship it" } });
    fireEvent.click(send);
    await waitFor(() => expect(screen.getByTestId("visual-answered").textContent).toContain("B · Gate on CI"));
    expect(sent).toEqual([{ rev: 2, option: "B", note: "ship it" }]);
  });
});
