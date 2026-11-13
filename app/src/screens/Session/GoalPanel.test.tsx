import { connectDaemon, renderApp, trackedSync as startSync } from "../../test/harness";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../../App";
import { ONBOARDED_KEY } from "../../api/providers";
import { DaemonClient, type AnalysisDoc } from "../../api/client";
import type { GoalDTO, MilestoneDTO, SessionDTO } from "../../api/schema";
import { createAppStore } from "../../store/store";
import { BASE_URL, FakeDaemon, TOKEN, flush } from "../../test/fakeDaemon";
import { REPO, root } from "../../test/fixtures";
import { GoalPanel, goalPhase, useGoal } from "./GoalPanel";
import { UNKNOWN, type Readiness } from "./review";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const ROOT = "proj";
/* The App-level test's session: the App routes to it by its root agent id. */
const APP_ROOT = "sup1";
const OTHER_REPO = "/home/u/other";
const appSession: SessionDTO = { id: APP_ROOT, workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: Date.now() / 1000 - 600, goal: "Ship the Goal screen" };
const libDoc = (id: string): AnalysisDoc => ({ id, title: `Doc ${id}`, kind: "investigation", topics: [], findings: [], open_questions: [], related: [], supersedes: [] });
/* The app as the shell shows it: its panes through pty views (stand-ins here). */

/* progress is the engine's activity counter, not the completion: 7 must not
   show as 7 of anything. */
const goal: GoalDTO = {
  root_id: ROOT,
  enabled: false,
  goal: "Ship the Goal screen",
  detail: "Milestone 5, task 5.6",
  state: "running",
  goal_phase: "in_progress",
  pr_url: null,
  note: null,
  progress: 7,
  milestones: [
    {
      id: 1,
      position: 1,
      title: "Client wired",
      status: "passed",
      state: "passed",
      check_cmd: "npm test",
      checked_at: 150,
      output: "all green",
    },
    {
      id: 2,
      position: 2,
      title: "Screen renders",
      status: "pending",
      state: "not_done",
      check_cmd: "npm run build",
      checked_at: null,
      output: null,
    },
  ],
};

/* A client whose responses can be held: while `hold` is set, requests to `path`
   are answered from the daemon's state at the time they're sent, but delivered
   only when `held`'s entries are released. */
function holdingClient(daemon: FakeDaemon, path: string) {
  const state = { hold: false };
  const held: Array<() => void> = [];
  const client = new DaemonClient({
    baseUrl: BASE_URL,
    token: TOKEN,
    fetch: async (input, init) => {
      const response = await daemon.fetch(input, init);
      if (!state.hold || new URL(String(input)).pathname !== path) return response;
      return new Promise<Response>((resolve) => held.push(() => resolve(response)));
    },
  });
  return { client, state, held };
}

const unfold = () => fireEvent.click(screen.getByRole("button", { name: /^Milestones \(/ }));

describe("GoalPanel", () => {
  it("shares one session read between rows and panels, including event refreshes", async () => {
    const daemon = new FakeDaemon({ goal: structuredClone(goal) });
    const { client, store } = connectDaemon(daemon);
    function Row() {
      const value = useGoal(client, REPO, ROOT, store);
      return <span data-testid="row-goal">{value?.goal}</span>;
    }
    const reads = () => daemon.requests.filter((r) => r.path === "/goal").length;
    const view = render(<><Row /><Row /><GoalPanel client={client} repo={REPO} root={ROOT} store={store} /></>);
    await waitFor(() => expect(screen.getAllByTestId("row-goal").map((el) => el.textContent)).toEqual([goal.goal, goal.goal]));
    expect(reads()).toBe(1);
    expect(daemon.requests.find((r) => r.path === "/goal")?.query).toMatchObject({ repo: REPO, root: ROOT });
    expect(screen.queryByText("npm run build")).toBeNull();
    unfold();
    expect(screen.getByText("npm run build")).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
    daemon.data.goal = { ...structuredClone(goal), goal: "Updated shared goal" };
    act(() => daemon.publish("autopilot.progress", { root_id: ROOT }));
    await waitFor(() => expect(screen.getAllByText("Updated shared goal").length).toBe(3));
    expect(reads()).toBe(2);
    view.unmount();
  });

  it.each([true, false])("shows the phase line only after its heading scrolls out of view (compact: %s)", async (compact) => {
    const phased = structuredClone(goal);
    phased.milestones![0].phase_name = "Planned work";
    phased.milestones![1].phase_name = "Fixes from testing";
    const view = render(<GoalPanel client={new FakeDaemon({ goal: phased }).client()} repo={REPO} root={ROOT} store={createAppStore()} compact={compact} />);
    await view.findByText(goal.goal!);
    if (!compact) unfold();
    expect(view.queryByTestId("goal-phase-line")).toBeNull();
    expect(view.queryByTestId("goal-where")).toBeNull();
    const list = view.getByTestId("goal-scroll");
    let firstTop = 90;
    let nextTop = 150;
    vi.spyOn(list, "getBoundingClientRect").mockImplementation(() => ({ top: 100 }) as DOMRect);
    const headers = view.getAllByTestId("phase-fold");
    vi.spyOn(headers[0], "getBoundingClientRect").mockImplementation(() => ({ top: firstTop, bottom: firstTop + 20 }) as DOMRect);
    vi.spyOn(headers[1], "getBoundingClientRect").mockImplementation(() => ({ top: nextTop, bottom: nextTop + 20 }) as DOMRect);
    fireEvent.scroll(list);
    expect(view.queryByTestId("goal-phase-line")).toBeNull();
    firstTop = 80;
    fireEvent.scroll(list);
    const line = view.getByTestId("goal-phase-line");
    expect(list.contains(line)).toBe(false);
    expect(line.nextElementSibling).toBe(list);
    expect(line.classList.contains("absolute")).toBe(true);
    expect(line.textContent).toBe("Planned work1 of 1");
    // The next heading under the line: the line would name the phase above and hide the one the rows below are in.
    nextTop = 115;
    fireEvent.scroll(list);
    expect(view.queryByTestId("goal-phase-line")).toBeNull();
    nextTop = 99;
    fireEvent.scroll(list);
    expect(view.queryByTestId("goal-phase-line")).toBeNull();
    nextTop = 80;
    fireEvent.scroll(list);
    expect(view.getByTestId("goal-phase-line").textContent).toBe("Fixes from testing0 of 1");
    nextTop = 150;
    fireEvent.scroll(list);
    expect(view.getByTestId("goal-phase-line").textContent).toBe("Planned work1 of 1");
    firstTop = 100;
    fireEvent.scroll(list);
    expect(view.queryByTestId("goal-phase-line")).toBeNull();
    view.unmount();
    const plain = render(<GoalPanel client={new FakeDaemon({ goal: structuredClone(goal) }).client()} repo={REPO} root={ROOT} store={createAppStore()} compact={compact} />);
    await plain.findByText(goal.goal!);
    if (!compact) unfold();
    expect(plain.queryByTestId("goal-phase-line")).toBeNull();
  });

  it.each([true, false])("opens and copies the complete request with copy feedback (recorded goal: %s)", async (recordedGoal) => {
    const request = "Build a goal screen.\n" + "Keep the original request readable and copyable. ".repeat(20);
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [{ ...appSession, request }], workspaces: [root], goal: recordedGoal ? { ...structuredClone(goal), root_id: APP_ROOT } : null });
    const { client, store, sync } = connectDaemon(daemon);
    localStorage.setItem(ONBOARDED_KEY, "1");
    const views = daemon.ptyViews();
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: APP_ROOT, repo: REPO }} />, views);
    // The shell shows before the sessions are read: the context pane is replaced once they are.
    const title = await waitFor(() => within(screen.getByTestId("session-context")).getByRole("button", { name: appSession.goal! }));
    const context = screen.getByTestId("session-context");
    fireEvent.click(title);
    expect(screen.getByRole("region", { name: "Full goal" }).textContent).toContain(request);
    fireEvent.click(screen.getByRole("button", { name: "Close full goal" }));
    vi.useFakeTimers();
    await act(async () => fireEvent.dblClick(title));
    expect(daemon.clipboard).toBe(request);
    expect(screen.queryByTestId("session-request")).toBeNull();
    expect(screen.queryByText(request)).toBeNull();
    const feedback = within(context).getByRole("status");
    expect(feedback.textContent).not.toBe("");
    act(() => vi.advanceTimersByTime(2000));
    expect(within(context).queryByRole("status")).toBeNull();
    daemon.clipboard = "";
    title.focus();
    await act(async () => fireEvent.keyDown(title, { key: "Enter" }));
    expect(daemon.clipboard).toBe(request);
    const copied = within(context).getByRole("status").textContent;
    act(() => vi.advanceTimersByTime(2000));
    let rejectCopy!: (reason: Error) => void;
    const pendingCopy = vi.fn(() => new Promise<void>((_, reject) => { rejectCopy = reject; }));
    views.copy = pendingCopy;
    await act(async () => fireEvent.dblClick(title));
    expect(within(context).queryByRole("status")).toBeNull();
    await act(async () => fireEvent.dblClick(title));
    expect(pendingCopy).toHaveBeenCalledTimes(1);
    await act(async () => rejectCopy(new Error("clipboard unavailable")));
    expect(within(context).getByRole("status").textContent).not.toBe(copied);
    act(() => vi.advanceTimersByTime(2000));
    expect(within(context).queryByRole("status")).toBeNull();
  });

  it("opens the session's docs from its goal line, filtered; Back to session returns, clearing the chip shows all; no docs hides the line", async () => {
    // Two repos: the session is in the second, the one Knowledge would open on by default is the first.
    const other = { ...root, id: "other", name: "other", path: OTHER_REPO };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER_REPO, name: "other" }], sessions: [{ ...appSession, workspace_id: "other" }], workspaces: [other], goal: { ...structuredClone(goal), root_id: APP_ROOT } });
    const client = daemon.client();
    // Docs exist only in the session's repo; the first repo has none.
    vi.spyOn(client, "docs").mockImplementation(async (query = {}) => {
      if (query.repo !== OTHER_REPO) return { docs: [] };
      return { docs: query.session ? [libDoc("d1"), libDoc("d2")] : [libDoc("d1"), libDoc("d2"), libDoc("d3")] };
    });
    const summary = vi.spyOn(client, "docSessionSummary").mockResolvedValue({ total: 2, writing: 1, updated: 0, using: 1 });
    const cards = () => screen.getByTestId("docs").querySelectorAll("li[data-doc]").length;
    const { store, sync } = connectDaemon(daemon, client);
    localStorage.setItem(ONBOARDED_KEY, "1");
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: APP_ROOT, repo: OTHER_REPO }} />);
    expect((await screen.findByTestId("goal-docs")).textContent).toContain("2 docs · writing 1, using 1 →");
    fireEvent.click(await screen.findByTestId("goal-docs"));
    await waitFor(() => expect(cards()).toBe(2));
    expect(screen.getByTestId("session-filter").textContent).toContain(APP_ROOT);

    fireEvent.click(screen.getByRole("button", { name: "Clear session filter" }));
    await waitFor(() => expect(cards()).toBe(3));
    expect(screen.queryByText(`Session ${APP_ROOT}`)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Back to session" }));
    expect(await screen.findByRole("region", { name: "Session" })).toBeTruthy();
    expect(screen.queryByTestId("docs")).toBeNull();
    sync.stop();
    cleanup();

    // With no docs for the session, the goal line is hidden.
    summary.mockResolvedValue({ total: 0, writing: 0, updated: 0, using: 0 });
    const again = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={again.refresh} initialRoute={{ screen: "session", root: APP_ROOT, repo: OTHER_REPO }} />);
    await screen.findByRole("region", { name: "Session" });
    await flush();
    expect(screen.queryByTestId("goal-docs")).toBeNull();
    again.stop();
  });

  it("shows an initial load error with a retry that loads the goal", async () => {
    const daemon = new FakeDaemon({ goal: structuredClone(goal) });
    let failFirst = true;
    const flaky = new DaemonClient({
      baseUrl: BASE_URL,
      token: TOKEN,
      fetch: (input, init) => {
        if (failFirst && new URL(String(input)).pathname === "/v1/goal") {
          failFirst = false;
          return Promise.reject(new TypeError("daemon unreachable"));
        }
        return daemon.fetch(input, init);
      },
    });
    const store = createAppStore();
    render(<GoalPanel client={flaky} repo={REPO} root={ROOT} store={store} />);

    await waitFor(() => expect(screen.getByText(/daemon unreachable/)).toBeTruthy());
    fireEvent.click(screen.getByText("Retry"));
    await waitFor(() => expect(screen.getByText("Ship the Goal screen")).toBeTruthy());
  });

  it("discards an invalidated goal response and coalesces updates during its read", async () => {
    const daemon = new FakeDaemon({ goal: structuredClone(goal) });
    const { client, state, held } = holdingClient(daemon, "/v1/goal");
    const store = createAppStore();
    startSync({ client: daemon.client(), store, connect: daemon.connect });
    render(<GoalPanel client={client} repo={REPO} root={ROOT} store={store} />);
    await waitFor(() => expect(screen.getByText("1 of 2 milestones complete")).toBeTruthy());

    const reads = () => daemon.requests.filter((r) => r.path === "/goal").length;
    const before = reads();
    // Hold one read with an obsolete goal, then change the goal during it.
    state.hold = true;
    daemon.data.goal = { ...structuredClone(goal), goal: "Obsolete goal" };
    daemon.publish("autopilot.progress", { repo_root: REPO, root_id: ROOT });
    await waitFor(() => expect(held.length).toBeGreaterThan(0));

    const finished = structuredClone(goal);
    finished.milestones!.forEach((m) => (m.status = "passed"));
    daemon.data.goal = finished;
    daemon.publish("autopilot.progress", { repo_root: REPO, root_id: ROOT });
    daemon.publish("autopilot.progress", { repo_root: REPO, root_id: ROOT });
    await flush();
    expect(reads()).toBe(before + 1);
    held[0]();
    await waitFor(() => expect(held.length).toBe(2));
    expect(screen.queryByText("Obsolete goal")).toBeNull();
    held[1]();
    await waitFor(() => expect(screen.getByText("2 of 2 milestones complete")).toBeTruthy());
    expect(reads()).toBe(before + 2);
    state.hold = false;
    daemon.data.goal = { ...goal, state: "blocked", goal_phase: "in_review", milestones: [goal.milestones![0], { ...goal.milestones![1], status: "running", state: "regressed", output: "tsc: 3 errors\\nbuild failed" }] };
    daemon.publish("autopilot.progress", { repo_root: REPO });
    await waitFor(() => expect(screen.getByRole("region", { name: "Goal" }).dataset.phase).toBe("in_review"));
    unfold();
    expect(screen.queryByText(/build failed/)).toBeNull();
  });

  /* One row per state (and the engine's phase): the milestone under test shows its label in
     the full view and in the sidebar's compact view; a later unfinished milestone shows it too. */
  it.each<[Partial<MilestoneDTO>, string]>([
    [{ state: "passed" }, "Done"],
    [{ state: "not_done" }, "In progress"],
    [{ state: "failed" }, "In progress"],
    [{ state: "regressed" }, "In progress"],
    [{ state: "not_done", phase: "in_review" }, "In review"],
  ])("labels a milestone %j '%s' in both views, its reason only as a tooltip", async (fields, label) => {
    const why = "passed earlier; a later change broke it";
    const milestones: MilestoneDTO[] = [
      goal.milestones![0],
      { ...goal.milestones![1], ...fields, state_why: why },
      { ...goal.milestones![1], id: 3, position: 3, title: "Ship it", state: "not_done", state_why: null },
    ];
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), milestones } });
    const store = createAppStore();

    const sidebar = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={store} compact />);
    // The sidebar's steps are one line each: the label and the reason are the row's tooltip.
    fireEvent.click(await sidebar.findByRole("button", { name: "1 pending" }));
    const [, row] = sidebar.getAllByTestId("milestone");
    expect(row.title).toContain(label);
    expect(row.title).toContain(why);
    sidebar.unmount();

    const full = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={store} />);
    await waitFor(() => expect(full.getByText("Ship the Goal screen")).toBeTruthy());
    fireEvent.click(within(full.container).getByRole("button", { name: /^Milestones \(/ }));
    expect(within(full.container).getByTitle(why).textContent).toBe(label);
  });

  /* Only a milestone with work on it reads as in progress; one nobody has started reads as pending. */
  it.each([false, true])("keeps completed and active rows visible independently of pending rows (phases: %s)", async (phased) => {
    const milestones: MilestoneDTO[] = [
      { ...goal.milestones![0], state: "passed", status: "passed" },
      { ...goal.milestones![1], state: "not_done", status: "pending" },
      { ...goal.milestones![1], id: 3, position: 3, title: "Ship it", state: null, status: "pending" },
    ];
    if (phased) milestones[0].phase_name = "Build";
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), milestones } });
    const sidebar = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} compact />);
    const pending = await sidebar.findByRole("button", { name: "1 pending" });
    expect(sidebar.getAllByTestId("milestone").map((li) => li.dataset.state)).toEqual(["done", "now"]);
    // Completed milestones stay listed; the count reports progress without hiding them.
    expect(sidebar.queryByRole("button", { name: /^\d+ done$/ })).toBeNull();
    expect(sidebar.getByTestId("goal-count").hasAttribute("aria-expanded")).toBe(false);
    expect(sidebar.queryByText("Ship it")).toBeNull();
    expect(pending.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(pending);
    expect(sidebar.getAllByTestId("milestone").map((li) => li.dataset.state)).toEqual(["done", "now", "pending"]);
    expect(sidebar.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("33");
    expect(sidebar.getByTestId("goal-count").textContent).toBe("1 of 3");
    fireEvent.click(pending);
    expect(sidebar.getAllByTestId("milestone")).toHaveLength(2);
    if (phased) {
      const phase = sidebar.getByTestId("phase-fold");
      fireEvent.click(phase);
      fireEvent.click(phase);
      expect(sidebar.getByRole("button", { name: "1 pending" }).getAttribute("aria-expanded")).toBe("false");
    }
    fireEvent.click(sidebar.getByRole("button", { name: "1 pending" }));
    const client = daemon.client();
    daemon.data.goal = { ...daemon.data.goal!, root_id: "other" };
    sidebar.rerender(<GoalPanel client={client} repo={REPO} root="other" store={createAppStore()} compact />);
    expect((await sidebar.findByRole("button", { name: "1 pending" })).getAttribute("aria-expanded")).toBe("false");
    expect(sidebar.getAllByTestId("milestone").map((li) => li.dataset.state)).toEqual(["done", "now"]);
  });

  /* Completed rows remain visible in the leading unnamed group and inside an open phase. */
  it("keeps leading completed rows visible while a named phase is folded", async () => {
    const base = goal.milestones![1];
    const milestones: MilestoneDTO[] = [
      { ...base, id: 1, position: 1, title: "Alpha", state: "passed", status: "passed", phase_name: null },
      { ...base, id: 2, position: 2, title: "Beta", state: "passed", status: "passed", phase_name: "Build" },
      { ...base, id: 3, position: 3, title: "Gamma", state: "not_done", status: "pending", phase_name: "Build" },
    ];
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), milestones } });
    const sidebar = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} compact />);
    await waitFor(() => expect(sidebar.getByRole("region", { name: "Goal" }).dataset.phase).toBe("in_progress"));
    const count = sidebar.getByTestId("goal-count");
    expect(count.tagName).toBe("SPAN");
    expect(sidebar.getByText("Alpha")).toBeTruthy();
    expect(sidebar.getByText("Beta")).toBeTruthy();
    // Folding a named phase hides its children without affecting the leading completed rows.
    fireEvent.click(sidebar.getByTestId("phase-fold"));
    expect(sidebar.queryByText("Beta")).toBeNull();
    expect(sidebar.getByText("Alpha")).toBeTruthy();
    expect(sidebar.getByTestId("phase-fold").getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(sidebar.getByTestId("phase-fold"));
    expect(sidebar.getByText("Beta")).toBeTruthy();
    expect(sidebar.getByTestId("phase-fold").getAttribute("aria-expanded")).toBe("true");
  });

  /* Every milestone a live worker is linked to is active, in the stage's colour and word; one nobody works on is not. */
  it("marks several active milestones and opens a later phase with linked workers", async () => {
    const milestones: MilestoneDTO[] = [
      { ...goal.milestones![1], id: 1, position: 1, title: "Alpha", state: "not_done", status: "pending" },
      { ...goal.milestones![1], id: 2, position: 2, title: "Beta", state: "not_done", status: "pending" },
      { ...goal.milestones![1], id: 3, position: 3, title: "Gamma", state: "not_done", status: "pending" },
      { ...goal.milestones![1], id: 4, position: 4, title: "Delta", state: "not_done", status: "pending" },
    ];
    milestones[0].phase_name = "Build";
    milestones[2].phase_name = "Ship";
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), root_id: APP_ROOT, stage: "working", milestones } });
    const store = createAppStore();
    const agent = (id: string, milestone: string, status = "processing", extra = {}) => ({ id, profile: "developer", provider: "claude", status, mode: "assign", parent_id: APP_ROOT, milestone, subagents: [], ...extra });
    const second = { ...structuredClone(root), id: "proj/feat-b", review_state: "checks running", agents: [agent("w4", "Delta", "idle", { reported: true })] };
    store.dispatch({
      type: "snapshot",
      workspaces: [
        { ...structuredClone(root), agents: [{ ...root.agents![0], id: APP_ROOT }, agent("w1", "Beta"), agent("w2", "Gamma", "processing", { reported: true }), agent("w3", "Alpha", "done")] },
        second,
      ],
      inbox: [],
    });
    const view = render(<GoalPanel client={daemon.client()} repo={REPO} root={APP_ROOT} store={store} compact />);
    fireEvent.click(await view.findByRole("button", { name: "1 pending" }));
    expect(view.getAllByTestId("phase-fold")[1].dataset.st).toBe("active");
    expect(view.getAllByTestId("phase-fold")[1].getAttribute("aria-expanded")).toBe("true");
    const marks = () => milestones.map((m) => {
      const li = view.getByText(m.title).closest("li")!;
      return [li.dataset.active ?? null, li.className.match(/\bst-(\w+)/)?.[1] ?? null];
    });
    // Alpha's worker is done; Beta's works; Gamma's reported but is busy again (fixing); Delta's branch is in checks.
    expect(marks()).toEqual([[null, null], ["true", "working"], ["true", "working"], ["true", "working"]]);
    expect(view.getAllByTestId("goal-stage")).toHaveLength(3);
    // Delta's reported worker keeps its marker as its branch waits for check room, then waits to merge.
    for (const [state, stage] of [["checks waiting for room: 2 running", "working"], ["approved, waiting to merge", "merging"]]) {
      act(() => store.dispatch({ type: "snapshot", workspaces: [store.getState().workspaces[0], { ...second, review_state: state }], inbox: [] }));
      expect(marks()[3]).toEqual(["true", stage]);
    }
    // Beta's worker exits (a liveness message, no new snapshot): it drops out while the others stay active.
    act(() => store.dispatch({ type: "stream", message: { type: "liveness", agents: { w1: false } } } as never));
    expect(marks()).toEqual([[null, null], [null, null], ["true", "working"], ["true", "merging"]]);
  });

  /* The goal's count and bar stay above the list; only the milestones scroll. */
  it("keeps the goal summary on one line with the count visible and the header pinned", async () => {
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), summary: "A very long summary for a narrow sidebar", stage: "working", commits_last_hour: 4, eta_seconds: 1200 } });
    const view = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} compact />);
    await view.findByTestId("goal-line");
    const header = view.getByTestId("goal-header");
    const list = view.getByTestId("goal-scroll");
    expect(header.contains(view.getByTestId("goal-line"))).toBe(true);
    expect(header.contains(view.getByRole("progressbar"))).toBe(true);
    expect(list.contains(header)).toBe(false);
    expect(list.className).toContain("overflow-auto");
    expect(header.className).toContain("shrink-0");
    expect(view.getByTestId("goal-line").className).toContain("flex-nowrap");
    expect(view.getByRole("button", { name: daemon.data.goal!.summary! }).className).toContain("truncate");
    expect(view.getByTestId("goal-count").className).toContain("shrink-0");
    expect(view.getByTestId("goal-count").className).toContain("whitespace-nowrap");
    expect(list.contains(view.getAllByTestId("milestone")[0])).toBe(true);
  });

  /* The active step and its slice of the bar take its stage's colour, with the stage's word; with nothing
     running, neither does. */
  it.each([["working"], ["checks"], ["review"], ["merging"], [null]])("colours the active step by its stage %s", async (stage) => {
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), stage } });
    const view = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} compact />);
    await view.findByTestId("goal-line");
    const [done, active] = view.getAllByTestId("milestone");
    const displayed = stage === "checks" ? "working" : stage;
    expect(done.className).not.toMatch(/\bst-/);
    expect(active.className.match(/\bst-(\w+)/)?.[1] ?? null).toBe(displayed);
    expect(view.queryByTestId("goal-bar-now")?.className.match(/\bst-(\w+)/)?.[1] ?? null).toBe(displayed);
  });

  /* One activity line: commit activity plus the estimate, including queued work
     and a fallback for older engines or incomplete snapshots. */
  const ago = (s: number) => Date.now() / 1000 - s;
  it.each<[string, Partial<GoalDTO>, string | null, string | null]>([
    ["commits, an estimate", { stage: "working", commits_last_hour: 3, last_commit_at: ago(60), eta_seconds: 1500 }, "3 commits in the last hour", "about 25m left"],
    ["one commit, no estimate", { stage: "working", commits_last_hour: 1, last_commit_at: ago(60), eta_seconds: null }, "1 commit in the last hour", "about 40m left"],
    ["no commit this hour", { stage: "checks", commits_last_hour: 0, last_commit_at: ago(900), eta_seconds: 240 }, "last commit 15m ago", "about 4m left"],
    ["nothing running", { stage: null, commits_last_hour: 0, last_commit_at: ago(900), eta_seconds: 600 }, "last commit 15m ago", "about 10m left"],
    ["an older engine", {}, null, "about 40m left"],
    ["expired estimate", { stage: "working", eta_seconds: -60 }, null, "about 40m left"],
    ["hours of queued work", { stage: null, eta_seconds: 10800 }, null, "about 3h left"],
  ])("says what happened lately: %s", async (_, fields, what, eta) => {
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), ...fields } });
    const view = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} compact />);
    await view.findByTestId("goal-header");
    expect(view.queryByTestId("goal-activity-what")?.textContent ?? null).toBe(what);
    expect(view.queryByTestId("goal-eta")?.textContent ?? null).toBe(eta);
  });

  /* Both finishing states use the same plain activity line in each view. */
  it.each<[Partial<GoalDTO>, boolean]>([
    [{ checking: true }, true],
    [{ auditing: true }, true],
    [{ checking: true }, false],
    [{ auditing: true }, false],
  ])("shows the checking line %#", async (fields, compact) => {
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), commits_last_hour: 3, ...fields } });
    const view = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} compact={compact} />);
    const line = await view.findByTestId("goal-activity-what");
    expect(line.style.color).toBe("");
    expect((compact ? view.getByTestId("goal-activity") : line).className).toBe(compact ? "side-act" : "text-xs text-ink-3");
  });

  it("shows the checking line on a mounted panel as checks start and stop", async () => {
    const daemon = new FakeDaemon({ goal: structuredClone(goal) });
    const store = createAppStore();
    startSync({ client: daemon.client(), store, connect: daemon.connect });
    render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={store} />);
    await waitFor(() => expect(screen.getByText("Ship the Goal screen")).toBeTruthy());
    expect(screen.queryByTestId("goal-activity-what")).toBeNull();
    daemon.data.goal = { ...goal, checking: true };
    daemon.publish("autopilot.checking", { repo_root: REPO });
    await waitFor(() => expect(screen.getByTestId("goal-activity-what")).toBeTruthy());
    daemon.data.goal = { ...goal, checking: false };
    daemon.publish("autopilot.checking", { repo_root: REPO });
    await waitFor(() => expect(screen.queryByTestId("goal-activity-what")).toBeNull());
  });

  /* Commits land without any goal event: a git sweep of the session's worktree refetches the line, and so does
     the clock, once a minute, for the rolling hour and the ages. */
  it("refetches the activity line on a git sweep of its session's worktree and as the clock moves", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), root_id: APP_ROOT, stage: "working", commits_last_hour: 1, last_commit_at: ago(60) } });
    const store = createAppStore();
    store.dispatch({ type: "snapshot", workspaces: [structuredClone(root)], inbox: [] });
    const view = render(<GoalPanel client={daemon.client()} repo={REPO} root={APP_ROOT} store={store} compact />);
    const what = () => view.queryByTestId("goal-activity-what")?.textContent;
    await waitFor(() => expect(what()).toBe("1 commit in the last hour"));

    daemon.data.goal = { ...daemon.data.goal!, commits_last_hour: 2 };
    act(() => store.dispatch({ type: "stream", message: { type: "git", workspace_id: root.id, status: { branch: "main", base: null, ahead: 1, behind: 0, dirty_files: [], unpushed: null } } }));
    await waitFor(() => expect(what()).toBe("2 commits in the last hour"));

    daemon.data.goal = { ...daemon.data.goal!, commits_last_hour: 0, last_commit_at: ago(1800) };
    act(() => vi.advanceTimersByTime(60_000));
    await waitFor(() => expect(what()).toBe("last commit 30m ago"));
  });

  /* Phases fold the list: a done phase is one line with its green count, the active one lists all its rows
     (its passed one too), an upcoming one folds to a count; any fold opens and closes on a click. */
  it.each([true, false])("folds the milestones by phase, the active one open (compact: %s)", async (compact) => {
    const base = goal.milestones![0];
    const ms = (n: number, phase_name: string | null, passed: boolean): MilestoneDTO => ({
      ...base, id: n, position: n, title: `M${n}: the step in full`, phase_name, status: passed ? "passed" : "pending", state: passed ? "passed" : "not_done",
    });
    const milestones = [ms(1, "Build", true), ms(2, null, true), ms(3, "Polish", true), ms(4, "Polish", false), ms(5, "Ship", false)];
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), milestones } });
    const view = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} compact={compact} />);
    await waitFor(() => expect(view.getByRole("region", { name: "Goal" }).dataset.phase).toBe("in_progress"));
    if (!compact) unfold();
    const folds = () => view.getAllByTestId("phase-fold");
    const rows = () => view.queryAllByTestId("milestone").map((li) => li.textContent!.match(/M\d/)![0]);
    const countOf = (b: HTMLElement) => b.querySelector(".k")!.textContent;
    expect(folds().map((b) => [b.dataset.st, b.querySelector(".n")!.textContent, countOf(b), b.getAttribute("aria-expanded")])).toEqual([
      ["done", "Build", "✓ 2 of 2", compact ? "true" : "false"],
      ["active", "Polish", "1 of 2", "true"],
      ["todo", "Ship", "1 upcoming", "false"],
    ]);
    expect(rows()).toEqual(compact ? ["M1", "M2", "M3", "M4"] : ["M3", "M4"]);
    // The sidebar's steps show their full titles (cut by CSS): a done one checked, the active one marked.
    expect(view.queryByTestId("goal-where")).toBeNull();
    // Only the panel's fold line carries the tiny per-phase bar, one segment per milestone.
    expect([...folds()[1].querySelectorAll(".bar-s i")].map((i) => (i as HTMLElement).dataset.s)).toEqual(compact ? [] : ["passed", "todo"]);
    if (compact) {
      fireEvent.click(folds()[0]);
      expect(rows()).toEqual(["M3", "M4"]);
    }
    fireEvent.click(folds()[0]);
    expect(rows()).toEqual(["M1", "M2", "M3", "M4"]);
    fireEvent.click(folds()[1]);
    expect(rows()).toEqual(["M1", "M2"]);
    expect(folds()[1].getAttribute("aria-expanded")).toBe("false");
  });

  /* Sidebar steps show each milestone's full title, so titles sharing a prefix before a colon stay told apart. */
  it("shows each sidebar step's full milestone title, not a prefix before the colon", async () => {
    const base = goal.milestones![0];
    const titles = ["Stress: load", "Stress: soak", "Stress: spike"];
    const milestones = titles.map((title, i): MilestoneDTO => ({ ...base, id: i + 1, position: i + 1, title, status: "pending", state: "not_done" }));
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), milestones } });
    const view = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} compact />);
    await waitFor(() => expect(view.getByRole("region", { name: "Goal" }).dataset.phase).toBe("in_progress"));
    const sidebar = within(view.getByRole("list", { name: "Milestones" }));
    fireEvent.click(sidebar.getByRole("button", { name: /2 pending/ }));
    expect(sidebar.getAllByTestId("milestone").map((li) => li.querySelector(".t")!.textContent)).toEqual(titles);
    expect(sidebar.getAllByTestId("milestone").map((li) => li.querySelector(".t")!.getAttribute("title"))).toEqual(titles);
  });

  /* One row per goal phase: the panel's one status in both views, and the step that phase has (PR open's link,
     Done's Close session), nothing of either in any other phase. Ready for PR is only its label: the session's
     ready notice offers the PR. */
  const PR = "https://github.com/example/proj/pull/7";
  it.each<[string, string, "pr" | "close" | null]>([
    ["in_progress", "In progress", null],
    ["in_review", "In review", null],
    ["ready_for_pr", "Ready for PR", null],
    ["pr_open", "PR open", "pr"],
    ["done", "Done", "close"],
  ])("shows the goal phase %s as '%s', with its own step", async (phase, label, step) => {
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), goal_phase: phase, pr_url: phase === "pr_open" || phase === "done" ? PR : null } });
    const store = createAppStore();
    const onReview = vi.fn();
    const onCloseSession = vi.fn();
    const openUrl = vi.fn(() => Promise.resolve());

    const sidebar = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={store} compact onReview={onReview} openUrl={openUrl} onCloseSession={onCloseSession} />);
    await waitFor(() => expect(sidebar.getByRole("region", { name: "Goal" }).dataset.phase).toBe(phase));
    // The sidebar's count line names only the steps after In progress; Done is the green count itself.
    expect(sidebar.queryByTestId("goal-phase")?.textContent ?? null).toBe(phase === "in_progress" || phase === "done" ? null : label);
    expect(sidebar.queryByTestId("goal-review")).toBeNull();
    expect(!!sidebar.queryByTestId("goal-pr")).toBe(step === "pr");
    expect(!!sidebar.queryByTestId("goal-close")).toBe(step === "close");
    if (step === "close") {
      // Done's step sits under the milestones, a quiet text button, and closes in one click.
      const close = sidebar.getByRole("button", { name: "Close session" });
      expect(sidebar.getByTestId("goal-line").contains(close)).toBe(false);
      expect(sidebar.getByRole("list", { name: "Milestones" }).compareDocumentPosition(close) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(close.className).toContain("text-ink-3");
      fireEvent.click(close);
      expect(onCloseSession).toHaveBeenCalledTimes(1);
      expect(sidebar.queryByRole("dialog")).toBeNull();
    }
    if (step === "pr") {
      fireEvent.click(sidebar.getByTestId("goal-pr"));
      expect(openUrl).toHaveBeenCalledWith(PR);
    }
    // One lifecycle: the PR step is said once, and nothing says the goal failed.
    sidebar.unmount();

    const full = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={store} onReview={onReview} openUrl={openUrl} onCloseSession={onCloseSession} />);
    await waitFor(() => expect(full.getByTestId("goal-phase").textContent).toBe(label));
    expect(full.queryByTestId("goal-review")).toBeNull();
    expect(!!full.queryByTestId("goal-pr")).toBe(step === "pr");
    expect(!!full.queryByTestId("goal-close")).toBe(step === "close");
    if (step === "close") {
      expect(full.getByTestId("goal-close").parentElement).toBe(full.getByTestId("goal-phase").parentElement);
      fireEvent.click(full.getByTestId("goal-close"));
      expect(onCloseSession).toHaveBeenCalledTimes(2);
    }
    full.unmount();
  });

  /* A Done goal as the panel draws it, from the engine's DTO alone: Done, the title (the full view's; the
     sidebar's pane names it above), every milestone listed and checked, nothing of a PR or of no milestones. */
  async function expectDone(view: ReturnType<typeof render>, compact: boolean) {
    await waitFor(() => expect(view.getByRole("region", { name: "Goal" }).dataset.phase).toBe("done"));
    const items = [...within(view.getByRole("list", { name: "Milestones" })).getAllByTestId("milestone")];
    if (compact) {
      expect(items.map((li) => li.dataset.state)).toEqual(["done", "done"]);
      expect(view.getByTestId("goal-count").textContent).toBe("2 of 2");
      expect(view.getByTestId("goal-line").className).toContain("text-pass");
      expect(view.queryByTestId("goal-phase")).toBeNull();
      expect(view.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("100");
    } else {
      expect(view.getByRole("heading", { name: "Ship the Goal screen" })).toBeTruthy();
      expect(view.queryByTestId("milestone-now")).toBeNull();
      expect(view.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("100");
    }
    expect(view.queryByTestId("goal-review")).toBeNull();
    expect(view.queryByTestId("goal-pr")).toBeNull();
  }

  it.each([true, false])("shows a Done goal again, title and checked milestones, when revisited after another session (compact: %s)", async (compact) => {
    const daemon = new FakeDaemon({ goal: structuredClone(goal) });
    daemon.completeGoal(ROOT);
    const store = createAppStore();
    const view = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={store} compact={compact} />);
    await expectDone(view, compact);
    // Another session, which has no goal; then back.
    view.rerender(<GoalPanel client={daemon.client()} repo={REPO} root="other" store={store} compact={compact} />);
    await waitFor(() => expect(view.getByText("No goal set for this session.")).toBeTruthy());
    view.rerender(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={store} compact={compact} />);
    await expectDone(view, compact);
  });

  /* An engine without goal_phase: its PR readiness and the milestones stand in, and "failed" is never said. */
  it.each<[string, Partial<GoalDTO>, Partial<Readiness>, string]>([
    ["work left", { state: "running" }, {}, "in_progress"],
    ["a goal the engine calls failed", { state: "failed" }, {}, "in_progress"],
    ["every milestone passed, not yet ready", { state: "done", milestones: goal.milestones!.map((m) => ({ ...m, status: "passed" })) }, { state: "not_ready" }, "in_review"],
    ["every milestone passed, no PR judging at all", { state: "done", milestones: goal.milestones!.map((m) => ({ ...m, status: "passed" })) }, {}, "done"],
    ["ready", { state: "done" }, { state: "ready" }, "ready_for_pr"],
    ["an open PR", { state: "done" }, { state: "ready", pr: { url: PR, unpushed: 0 } }, "pr_open"],
  ])("falls back without goal_phase: %s", (_, fields, readiness, phase) => {
    const older: GoalDTO = { ...structuredClone(goal), goal_phase: undefined, ...fields };
    expect(goalPhase(older, { ...UNKNOWN, ...readiness }).phase).toBe(phase);
  });

  /* The session's PR merged and its branch moved on: the engine's phase has no PR, so the merged one a readiness
     read still carries is not the session's PR any more (its next is to be opened). */
  it("gives no PR to the phase after a merged PR, when the session's next one is to be opened", () => {
    const merged = { state: "ready" as const, pr: { url: PR, unpushed: 1 } };
    expect(goalPhase({ ...structuredClone(goal), goal_phase: "ready_for_pr", pr_url: null }, merged)).toEqual({ phase: "ready_for_pr", prUrl: null });
  });

  it("never shows 'failed' for an older engine's failed goal", async () => {
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), goal_phase: undefined, state: "failed" } });
    render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} />);
    await waitFor(() => expect(screen.getByTestId("goal-phase").textContent).toBe("In progress"));
    expect(document.body.textContent).not.toMatch(/failed/i);
  });

});
