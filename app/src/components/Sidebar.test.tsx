import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DaemonClient } from "../api/client";
import { DaemonContext } from "../api/context";
import type { GoalDTO, InboxItemDTO, SessionDTO } from "../api/schema";
import type { Route } from "../routes";
import { SessionsProvider } from "../store/sessions";
import { createAppStore } from "../store/store";
import { startSync } from "../store/sync";
import { StoreContext } from "../store/useStore";
import { BASE_URL, FakeDaemon, TOKEN, type DaemonData } from "../test/fakeDaemon";
import { REPO, permission, permissionInTerminal, question, root, waiting, worker } from "../test/fixtures";
import { PAUSED_KEY, SPLIT_KEY, Sidebar } from "./Sidebar";

beforeEach(() => localStorage.clear());
afterEach(cleanup);

const live: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };
const paused: SessionDTO = { ...live, id: "sup0", status: "paused", status_since: Date.now() / 1000 - 2 * 86400, goal: "Refactor the cache" };
const child = { ...worker.agents![0], id: "c1", parent_id: "w1" };
const goal: GoalDTO = { root_id: "sup1", enabled: true, goal: "Ship ls --json", state: "running", progress: 3, milestones: [{ id: 1, position: 1, title: "Parse flags", status: "passed", check_cmd: "npm test" }, { id: 2, position: 2, title: "Print JSON", status: "running", check_cmd: "npm run build" }] };

function mount(data: Partial<DaemonData>, route: Route = { screen: "new" }, fetch?: (daemon: FakeDaemon) => typeof globalThis.fetch) {
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [live, paused], ...data });
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: fetch?.(daemon) ?? daemon.fetch });
  const store = createAppStore();
  const sync = startSync({ client, store, connect: daemon.connect });
  const navigate = vi.fn();
  render(
    <DaemonContext.Provider value={client}>
      <StoreContext.Provider value={store}>
        <SessionsProvider client={client}>
          <Sidebar route={route} navigate={navigate} refresh={sync.refresh} />
        </SessionsProvider>
      </StoreContext.Provider>
    </DaemonContext.Provider>,
  );
  return { daemon, sync, navigate };
}

const agentRow = (id: string) => document.querySelector<HTMLButtonElement>(`button[data-tree-agent="${id}"]`);
const sessionRow = (id: string) => document.querySelector<HTMLButtonElement>(`button[data-tree-session="${id}"]`);
const list = () => screen.getByRole("navigation", { name: "Sessions" });

describe("Sidebar", () => {
  it("lists every open session by repo with its agents nested in the CLI's rows, recent ones folded, and opens them", async () => {
    const { sync, navigate } = mount({ workspaces: [root, { ...worker, agents: [...worker.agents!, child] }], inbox: [waiting] }, { screen: "session", root: "sup1", repo: REPO, agent: "w1" });
    expect(list().textContent).toContain("Loading");
    expect(within(screen.getByTestId("status-line")).getByRole("button", { name: "Open Account › Usage" })).toBeTruthy();
    await waitFor(() => expect(agentRow("w1")).toBeTruthy());
    expect(screen.getByTestId("repo-proj")).toBeTruthy();
    // Under "Sessions": the session (its goal, its id and mode under it) with the supervisor and its workers
    // nested under it, as the mockup draws them; nothing in the tree about what needs a person (C2b).
    const session = screen.getByTestId("tree-session-sup1");
    expect(sessionRow("sup1")!.textContent).toContain("Ship ls --json");
    expect(sessionRow("sup1")!.textContent).toContain("sup1 · interactive · 3 agents · 2 working");
    expect(sessionRow("sup1")!.title).toContain("⌘1");
    expect(session.contains(agentRow("sup1"))).toBe(true);
    expect(session.contains(agentRow("w1"))).toBe(true);
    expect(agentRow("w1")!.closest("li")!.contains(agentRow("c1"))).toBe(true);
    // The rows read as the mockup's: a dot for the CLI's status, "id · branch" (the supervisor by its role), the label.
    expect(agentRow("sup1")!.querySelector(".side-dot")!.getAttribute("data-tone")).toBe("idle");
    expect(agentRow("sup1")!.textContent).toContain("sup1 · supervisor");
    expect(agentRow("w1")!.querySelector(".side-dot")!.getAttribute("data-tone")).toBe("work");
    expect(agentRow("w1")!.textContent).toContain("w1 · feat/ls");
    expect(within(agentRow("w1")!).getByTestId("agent-status").textContent).toBe("working");
    expect(within(session).queryByTestId("needs-you")).toBeNull();
    expect(screen.queryByTestId("summary")).toBeNull();
    // The route's agent is the current one.
    expect(agentRow("w1")!.getAttribute("aria-current")).toBe("page");
    expect(agentRow("sup1")!.getAttribute("aria-current")).toBeNull();
    // A paused session sits behind the group's collapsed "Paused · 1" row, at its foot.
    expect(sessionRow("sup0")).toBeNull();
    expect(screen.getByTestId("repo-proj").lastElementChild!.textContent).toContain("Paused · 1");
    expect(screen.queryByText(/Recent/)).toBeNull();

    // Clicking opens the provider session on that agent (or a session on its supervisor).
    fireEvent.click(agentRow("c1")!);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "session", root: "sup1", repo: REPO, agent: "c1" });
    fireEvent.click(screen.getByRole("button", { name: /Paused · 1/ }));
    expect(sessionRow("sup0")!.textContent).toContain("Refactor the cache");
    expect(sessionRow("sup0")!.textContent).toContain("paused 2d");
    fireEvent.click(sessionRow("sup0")!);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "session", root: "sup0", repo: REPO, agent: "sup0" });
    fireEvent.click(screen.getByRole("button", { name: "New session" }));
    expect(navigate).toHaveBeenLastCalledWith({ screen: "new" });
    sync.stop();
  });

  it("folds a repo's paused sessions into one collapsed Paused · N row, remembered per viewer, and keeps the selected paused session listed", async () => {
    const { sync } = mount({ workspaces: [root, worker] }, { screen: "new" });
    await waitFor(() => expect(screen.getByTestId("repo-proj")).toBeTruthy());
    const toggle = () => screen.getByRole("button", { name: /Paused · 1/ });
    // Collapsed by default: the count shows, the paused session does not.
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    expect(sessionRow("sup0")).toBeNull();
    // Expanding shows it under the row, and the state is kept.
    fireEvent.click(toggle());
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    expect(sessionRow("sup0")).toBeTruthy();
    expect(JSON.parse(localStorage.getItem(PAUSED_KEY)!)).toEqual([REPO]);
    fireEvent.click(toggle());
    expect(sessionRow("sup0")).toBeNull();
    expect(JSON.parse(localStorage.getItem(PAUSED_KEY)!)).toEqual([]);
    sync.stop();
    cleanup();
    // The selected paused session stays listed, collapsed or not.
    const again = mount({ workspaces: [root, worker] }, { screen: "session", root: "sup0", repo: REPO, agent: "sup0" });
    await waitFor(() => expect(sessionRow("sup0")).toBeTruthy());
    expect(screen.getByRole("button", { name: /Paused · 1/ }).getAttribute("aria-expanded")).toBe("false");
    again.sync.stop();
  });

  it("moves with the keyboard: ↑ ↓ and Enter through the rows, ⌘1-9 to a session, ⌘N to a new one", async () => {
    const { sync, navigate } = mount({ workspaces: [root, worker] }, { screen: "session", root: "sup1", repo: REPO, agent: "sup1" });
    await waitFor(() => expect(agentRow("w1")).toBeTruthy());
    // The row on screen (the session's) is where the keys start: down is its supervisor, then its worker.
    fireEvent.keyDown(list(), { key: "ArrowDown" });
    fireEvent.keyDown(list(), { key: "ArrowDown" });
    fireEvent.keyDown(list(), { key: "Enter" });
    expect(navigate).toHaveBeenLastCalledWith({ screen: "session", root: "sup1", repo: REPO, agent: "w1" });
    fireEvent.keyDown(list(), { key: "k" });
    fireEvent.keyDown(list(), { key: "Enter" });
    expect(navigate).toHaveBeenLastCalledWith({ screen: "session", root: "sup1", repo: REPO, agent: "sup1" });
    fireEvent.keyDown(window, { key: "1", metaKey: true });
    expect(navigate).toHaveBeenLastCalledWith({ screen: "session", root: "sup1", repo: REPO, agent: "sup1" });
    navigate.mockClear();
    fireEvent.keyDown(window, { key: "2", metaKey: true }); // no second live session
    expect(navigate).not.toHaveBeenCalled();
    fireEvent.keyDown(window, { key: "n", ctrlKey: true });
    expect(navigate).toHaveBeenLastCalledWith({ screen: "new" });
    sync.stop();
  });

  it("leads with one Inbox · N entry at the very top, the only thing about attention in the sidebar, which opens the Inbox page", async () => {
    const { daemon, sync, navigate } = mount({ workspaces: [root, worker], inbox: [question, waiting, permission] }, { screen: "new" });
    await waitFor(() => expect(screen.getByTestId("inbox-badge").textContent).toBe("3"));
    const inbox = screen.getByTestId("inbox-link");
    expect(inbox.textContent).toBe("Inbox3");
    // As C2b has it: the entry right under the logo, above the session's context, the handle and the tree.
    const side = screen.getByRole("complementary", { name: "Sidebar" });
    expect(side.compareDocumentPosition(inbox) & Node.DOCUMENT_POSITION_CONTAINED_BY).toBeTruthy();
    expect(inbox.compareDocumentPosition(screen.getByTestId("session-context")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByTestId("session-context").compareDocumentPosition(screen.getByTestId("split-handle")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByTestId("split-handle").compareDocumentPosition(screen.getByRole("navigation", { name: "Sessions" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // No needs-you section, cards, chips or badges anywhere else: the agent's row says what it does, in plain words.
    expect(screen.queryByTestId("context-needs")).toBeNull();
    expect(screen.queryByTestId("context-card")).toBeNull();
    expect(screen.queryAllByTestId("needs-you")).toHaveLength(0);
    await waitFor(() => expect(agentRow("w1")).toBeTruthy());
    expect(within(agentRow("w1")!).getByTestId("agent-status").textContent).toBe("working");
    fireEvent.click(inbox);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "inbox" });
    // Nothing waiting: the entry says so, quietly.
    daemon.data.inbox = [];
    daemon.publish("message.queued", { agent_id: "sup1" });
    await waitFor(() => expect(screen.queryByTestId("inbox-badge")).toBeNull());
    expect(inbox.textContent).toBe("Inboxempty");
    sync.stop();
  });

  it("shows the session's changes in one coloured line that toggles them in the chat's place", async () => {
    const onSession: Route = { screen: "session", root: "sup1", repo: REPO, agent: "w1" };
    const changed = {
      workspaces: [root, worker],
      sessionReview: { sup1: { ready: false, reasons: ["work in progress"], branch: "dev/ls", base: "main", diff: "" } },
      sessionBase: { sup1: { "src/a.ts": "old\ncontext\n" } },
      sessionFiles: { sup1: { "src/a.ts": "new\ncontext\n", "docs/readme.md": "hello\nworld\n" } },
    };
    const first = mount(changed, onSession);
    const row = () => within(screen.getByTestId("session-context")).getByTestId("changes-row");
    await waitFor(() => expect(within(row()).getByTestId("changes-count").textContent).toBe("2 files +3 −1"));
    expect(within(row()).getByText("+3").className).toContain("text-pass");
    expect(within(row()).getByText("−1").className).toContain("text-sap");
    expect(row().getAttribute("aria-pressed")).toBe("false");
    // Clicking puts the changes in the chat's place, on the agent shown.
    fireEvent.click(row());
    expect(first.navigate).toHaveBeenLastCalledWith({ screen: "session", root: "sup1", repo: REPO, agent: "w1", sheet: { kind: "changes" } });
    first.sync.stop();
    cleanup();

    // Shown: pressed, and clicking again takes them away.
    const second = mount(changed, { ...onSession, sheet: { kind: "changes" } });
    await waitFor(() => expect(row().getAttribute("aria-pressed")).toBe("true"));
    fireEvent.click(row());
    expect(second.navigate).toHaveBeenLastCalledWith({ screen: "session", root: "sup1", repo: REPO, agent: "w1", sheet: undefined });
    second.sync.stop();
  });

  it("shows the selected session's context above the tree: goal and milestones and the PR status, nothing of what waits; the split drags and is kept", async () => {
    const credentials: InboxItemDTO = { kind: "credentials", reason: "credentials need attention", agent_id: "c1", workspace_id: "proj/feat-ls", detail: "claude auth login" };
    const { sync } = mount(
      { workspaces: [root, { ...worker, agents: [...worker.agents!, child] }], inbox: [permission, waiting, permissionInTerminal, credentials], goal: structuredClone(goal) },
      { screen: "session", root: "sup1", repo: REPO, agent: "sup1" },
    );
    const context = () => screen.getByTestId("session-context");
    // Completion stays visible alongside the phase; the milestone list carries the detail.
    await waitFor(() => expect(within(context()).getByTestId("goal-line").textContent).toBe("1 of 2 milestones complete · In progress"));
    expect(context().textContent).toContain("Ship ls --json");
    expect(context().textContent).toContain("sup1 · proj · interactive · autopilot");
    const milestones = within(context()).getByRole("list", { name: "Milestones" });
    expect(milestones.textContent).toContain("Print JSON");
    expect([...milestones.querySelectorAll("li")].map((li) => li.dataset.state)).toEqual(["done", "now"]);
    expect(within(context()).queryByRole("progressbar")).toBeNull();
    expect(screen.queryByTestId("connection")).toBeNull();
    expect(context().textContent).toContain("Checked never");
    expect(within(context()).queryByRole("button", { name: /Check milestone|Run checks/ })).toBeNull();
    expect(within(context()).getByRole("switch").getAttribute("aria-checked")).toBe("true");
    expect(within(context()).getByRole("switch").textContent).toContain("autopilot on");
    // Nothing about a PR until the engine says the session is ready (the fake's never does here).
    expect(within(context()).queryByTestId("goal-review")).toBeNull();
    expect(context().textContent).not.toMatch(/PR/);
    // Four things wait in this session; the context says nothing of them (the Inbox does), the entry counts them.
    expect(screen.getByTestId("inbox-badge").textContent).toBe("4");
    expect(within(context()).queryByTestId("context-needs")).toBeNull();
    expect(within(context()).queryByTestId("context-card")).toBeNull();
    expect(within(context()).queryByRole("group", { name: "Permission choices" })).toBeNull();
    expect(within(context()).queryByRole("button", { name: /Sign in/ })).toBeNull();

    // The split: the handle drags the context's height, and the keyboard moves it; both are kept for next time.
    const handle = screen.getByTestId("split-handle");
    expect(context().style.height).toBe("330px");
    fireEvent.mouseDown(handle, { clientY: 100 });
    fireEvent.mouseMove(window, { clientY: 160 });
    fireEvent.mouseUp(window);
    expect(context().style.height).toBe("390px");
    expect(localStorage.getItem(SPLIT_KEY)).toBe("390");
    fireEvent.keyDown(handle, { key: "ArrowUp" });
    expect(context().style.height).toBe("366px");
    expect(localStorage.getItem(SPLIT_KEY)).toBe("366");
    sync.stop();

    // Mounted again, the split is where it was left.
    cleanup();
    const again = mount({ workspaces: [root, worker] }, { screen: "session", root: "sup1", repo: REPO, agent: "sup1" });
    expect(screen.getByTestId("session-context").style.height).toBe("366px");
    again.sync.stop();
  });

  it("offers no autopilot for a paused session's context, and says nothing about a PR", async () => {
    const { sync } = mount({ workspaces: [root, worker], goal: { ...structuredClone(goal), root_id: "sup0", state: "paused", enabled: false } }, { screen: "session", root: "sup0", repo: REPO, agent: "sup0" });
    const context = () => screen.getByTestId("session-context");
    await waitFor(() => expect(within(context()).getByTestId("goal-line").textContent).toBe("1 of 2 milestones complete · In progress"));
    expect(context().textContent).toContain("Refactor the cache");
    expect(within(context()).queryByRole("switch")).toBeNull();
    expect(context().textContent).not.toMatch(/PR/);
    sync.stop();
  });

  it("renames a session from its context title, then lists it by that name in its row and context; an empty name goes back to the goal", async () => {
    let refuse = true;
    const { daemon, sync } = mount({ workspaces: [root, worker], goal: structuredClone(goal) }, { screen: "session", root: "sup1", repo: REPO, agent: "sup1" }, (d) => (input, init) =>
      refuse && String(input).endsWith("/rename") ? Promise.resolve(new Response(JSON.stringify({ error: { code: "invalid", message: "nope" } }), { status: 400 })) : d.fetch(input, init),
    );
    await waitFor(() => expect(sessionRow("sup1")).toBeTruthy());
    const rename = async (name: string) => {
      fireEvent.click(screen.getByRole("button", { name: "Rename session" }));
      const input = screen.getByLabelText("Session name");
      fireEvent.change(input, { target: { value: name } });
      fireEvent.keyDown(input, { key: "Enter" });
    };
    // A refused rename keeps the editor and what was typed, and says so.
    await rename("  Login fix ");
    await screen.findByRole("alert");
    expect((screen.getByLabelText("Session name") as HTMLInputElement).value).toBe("  Login fix ");
    expect(sessionRow("sup1")!.textContent).toContain("Ship ls --json");
    refuse = false;
    fireEvent.keyDown(screen.getByLabelText("Session name"), { key: "Enter" });
    await waitFor(() => expect(sessionRow("sup1")!.textContent).toContain("Login fix"));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(daemon.requests).toContainEqual(expect.objectContaining({ method: "POST", path: "/sessions/sup1/rename", body: { name: "  Login fix " } }));
    expect(screen.getByTestId("session-context").textContent).toContain("Login fix");
    await rename("");
    await waitFor(() => expect(sessionRow("sup1")!.textContent).toContain("Ship ls --json"));
    sync.stop();
  });

  it("closes a session from its row's close, shown on hover or focus: one click, nothing asked, the row just goes", async () => {
    const { daemon, sync, navigate } = mount({ workspaces: [root, worker], goal: structuredClone(goal) }, { screen: "session", root: "sup1", repo: REPO, agent: "sup1" });
    await waitFor(() => expect(sessionRow("sup1")).toBeTruthy());
    // Still at work (its goal in progress, a worker busy): no "Done · Close", only the quiet ✕, a button the keyboard reaches.
    expect(screen.queryByTestId("session-done-sup1")).toBeNull();
    const close = screen.getByRole("button", { name: "Close session sup1" });
    expect(close.className).toContain("group-hover:opacity-100");
    expect(close.className).toContain("focus-visible:opacity-100");
    fireEvent.click(close);
    // The row goes at once; the session on screen moves on (no other live session: the Inbox).
    expect(screen.queryByTestId("tree-session-sup1")).toBeNull();
    expect(navigate).toHaveBeenLastCalledWith({ screen: "inbox" });
    await waitFor(() => expect(daemon.requests).toContainEqual(expect.objectContaining({ method: "POST", path: "/sessions/sup1/close" })));
    expect(screen.queryByRole("dialog")).toBeNull();
    sync.stop();
  });

  it("marks a session that is through with Done · Close on its row, without hovering: its goal Done, or no goal and nothing at work", async () => {
    const idle: SessionDTO = { ...live, id: "sup2", goal: null };
    const resting = { ...root, id: "proj/rest", agents: [{ ...root.agents![0], id: "sup2" }] };
    const { daemon, sync } = mount({ workspaces: [root, worker, resting], sessions: [live, idle], goal: { ...structuredClone(goal), goal_phase: "done" } });
    await waitFor(() => expect(screen.getByTestId("session-done-sup1").textContent).toBe("Done · Close"));
    expect(screen.getByTestId("session-done-sup2").textContent).toBe("Done · Close");
    fireEvent.click(screen.getByTestId("session-done-sup1"));
    expect(screen.queryByTestId("tree-session-sup1")).toBeNull();
    await waitFor(() => expect(daemon.requests).toContainEqual(expect.objectContaining({ method: "POST", path: "/sessions/sup1/close" })));
    sync.stop();
    cleanup();

    // A paused session whose goal is Done says so too, on its row under the open Paused group.
    localStorage.setItem(PAUSED_KEY, JSON.stringify([REPO]));
    const again = mount({ workspaces: [root, worker], goal: { ...structuredClone(goal), root_id: "sup0", goal_phase: "done" } });
    await waitFor(() => expect(screen.getByTestId("session-done-sup0").textContent).toBe("Done · Close"));
    fireEvent.click(screen.getByTestId("session-done-sup0"));
    expect(sessionRow("sup0")).toBeNull();
    await waitFor(() => expect(again.daemon.requests).toContainEqual(expect.objectContaining({ method: "POST", path: "/sessions/sup0/close" })));
    again.sync.stop();
  });

  it("keeps a closed agent's running child under the session", async () => {
    // The snapshot leaves w1 out (closed) but keeps c1; w1's record says where c1 belongs.
    const { daemon, sync } = mount({ workspaces: [root, { ...worker, agents: [child] }], closed: [worker.agents![0]] });
    await waitFor(() => expect(agentRow("c1")).toBeTruthy());
    expect(agentRow("w1")).toBeNull();
    expect(screen.getByTestId("tree-session-sup1").contains(agentRow("c1"))).toBe(true);
    expect(daemon.requests).toContainEqual({ method: "GET", path: "/agents/w1", query: {}, body: undefined });
    sync.stop();
  });

  it("looks a closed parent up again with the next snapshot when the lookup failed", async () => {
    let failed = 0;
    const { daemon, sync } = mount(
      { workspaces: [root, { ...worker, agents: [child] }], closed: [worker.agents![0]] },
      { screen: "new" },
      (daemon) => (input, init) => {
        if (String(input).endsWith("/agents/w1") && failed++ === 0) return Promise.reject(new TypeError("connection refused"));
        return daemon.fetch(input, init);
      },
    );
    await waitFor(() => expect(failed).toBe(1));
    expect(agentRow("c1")).toBeNull();
    daemon.publish("agent.result", { agent_id: "c1" }); // any event the store can't patch: a fresh snapshot
    await waitFor(() => expect(agentRow("c1")).toBeTruthy());
    expect(failed).toBe(2);
    sync.stop();
  });
});
