import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../../App";
import { DaemonClient, type AnalysisDoc } from "../../api/client";
import { PtyViewsProvider } from "../../api/ptyView";
import type { GoalDTO, MilestoneDTO, SessionDTO } from "../../api/schema";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { BASE_URL, FakeDaemon, TOKEN, flush } from "../../test/fakeDaemon";
import { REPO, root } from "../../test/fixtures";
import { standInViews } from "../../test/panes";
import { GoalPanel, completion, goalPhase, outputTail } from "./GoalPanel";
import { UNKNOWN, type Readiness } from "./review";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

afterEach(cleanup);

const ROOT = "proj";
/* The App-level test's session: the App routes to it by its root agent id. */
const APP_ROOT = "sup1";
const OTHER_REPO = "/home/u/other";
const appSession: SessionDTO = { id: APP_ROOT, workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: Date.now() / 1000 - 600, goal: "Ship the Goal screen" };
const libDoc = (id: string): AnalysisDoc => ({ id, title: `Doc ${id}`, kind: "investigation", topics: [], findings: [], open_questions: [], related: [], supersedes: [] });
/* The app as the shell shows it: its panes through pty views (stand-ins here). */
const renderApp = (app: ReactElement) => render(<PtyViewsProvider value={standInViews}>{app}</PtyViewsProvider>);

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
  it("sends the session root with each request", async () => {
    const daemon = new FakeDaemon({ goal: structuredClone(goal) });
    const store = createAppStore();
    const sync = startSync({ client: daemon.client(), store, connect: daemon.connect });
    render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={store} />);

    await waitFor(() => expect(screen.getByText("Ship the Goal screen")).toBeTruthy());
    // Folded by default, with the milestone being worked on in a line; unfolded on demand.
    expect(screen.getByTestId("milestone-now").textContent).toBe("now: 2. Screen renders");
    expect(screen.queryByText("npm run build")).toBeNull();
    unfold();
    expect(screen.getByText("2. Screen renders")).toBeTruthy();
    expect(screen.getByText("npm run build")).toBeTruthy();
    expect(daemon.requests.find((r) => r.path === "/goal")?.query).toMatchObject({ repo: REPO, root: ROOT });

    expect(screen.queryByRole("switch")).toBeNull();
    sync.stop();
  });

  it("computes completion from the milestones' statuses, not the activity counter", () => {
    expect(completion(goal.milestones)).toEqual({ passed: 1, total: 2 });
    expect(completion(undefined)).toEqual({ passed: 0, total: 0 });
  });

  it("shows the milestone completion, not progress, in the bar", async () => {
    const daemon = new FakeDaemon({ goal: structuredClone(goal) });
    const store = createAppStore();
    render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={store} />);
    await waitFor(() => expect(screen.getByText("1 of 2 milestones complete")).toBeTruthy());
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("50");
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
    const sync = startSync({ client, store: createAppStore(), connect: daemon.connect });
    renderApp(<App store={createAppStore()} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: APP_ROOT, repo: OTHER_REPO }} />);
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
    const again = startSync({ client, store: createAppStore(), connect: daemon.connect });
    renderApp(<App store={createAppStore()} client={client} refresh={again.refresh} initialRoute={{ screen: "session", root: APP_ROOT, repo: OTHER_REPO }} />);
    await waitFor(() => expect(daemon.requests.some((r) => r.path === "/goal")).toBe(true));
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

  it("refetches the goal when the event stream reports autopilot activity", async () => {
    const daemon = new FakeDaemon({ goal: structuredClone(goal) });
    const store = createAppStore();
    const sync = startSync({ client: daemon.client(), store, connect: daemon.connect });
    render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={store} />);
    await waitFor(() => expect(screen.getByText("Ship the Goal screen")).toBeTruthy());
    unfold();

    daemon.data.goal = {
      ...goal,
      state: "blocked",
      goal_phase: "in_review",
      milestones: [goal.milestones![0], { ...goal.milestones![1], status: "running", state: "regressed", output: "tsc: 3 errors\nbuild failed" }],
    };
    daemon.publish("autopilot.progress", { repo_root: REPO });
    // The goal's one status is its lifecycle phase, not the engine's state word.
    await waitFor(() => expect(screen.getByTestId("goal-phase").textContent).toBe("In review"));
    expect(screen.getByText("2. Screen renders").closest("li")!.textContent).toContain("In progress");
    expect(screen.queryByText("blocked")).toBeNull();
    // The check's own output stays out of the panel: it would say "failed".
    expect(screen.queryByText(/build failed/)).toBeNull();
    expect(screen.queryByText(/tsc: 3 errors/)).toBeNull();
    sync.stop();
  });

  it("does not let an older goal response overwrite a newer one", async () => {
    const daemon = new FakeDaemon({ goal: structuredClone(goal) });
    const { client, state, held } = holdingClient(daemon, "/v1/goal");
    const store = createAppStore();
    const sync = startSync({ client: daemon.client(), store, connect: daemon.connect });
    render(<GoalPanel client={client} repo={REPO} root={ROOT} store={store} />);
    await waitFor(() => expect(screen.getByText("1 of 2 milestones complete")).toBeTruthy());

    // An autopilot event starts a refresh (an event also marks the store stale, so there
    // can be a second); their responses are held, so they carry the old goal.
    state.hold = true;
    daemon.publish("autopilot.progress", { repo_root: REPO, root_id: ROOT });
    await waitFor(() => expect(held.length).toBeGreaterThan(0));

    // A newer refresh, after the goal finished, answers first.
    state.hold = false;
    const finished = structuredClone(daemon.data.goal!);
    finished.milestones!.forEach((m) => (m.status = "passed"));
    daemon.data.goal = finished;
    daemon.publish("autopilot.progress", { repo_root: REPO, root_id: ROOT });
    await waitFor(() => expect(screen.getByText("2 of 2 milestones complete")).toBeTruthy());

    // The older response arrives last; it must not win.
    held.forEach((release) => release());
    await flush();
    expect(screen.getByText("2 of 2 milestones complete")).toBeTruthy();
    sync.stop();
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
    await waitFor(() => expect(sidebar.getAllByTitle(why)).toHaveLength(1));
    expect(sidebar.getByTitle(why).textContent).toContain(label);
    expect(sidebar.getByText("Ship it", { exact: false }).closest("li")!.textContent).toContain("In progress");
    sidebar.unmount();

    const full = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={store} />);
    await waitFor(() => expect(full.getByText("Ship the Goal screen")).toBeTruthy());
    fireEvent.click(within(full.container).getByRole("button", { name: /^Milestones \(/ }));
    expect(within(full.container).getByTitle(why).textContent).toBe(label);
    expect(within(full.container).getByText("3. Ship it").closest("li")!.textContent).toContain("In progress");
    expect(document.body.textContent).not.toMatch(/failed|broke/i);
  });

  /* The engine works milestones in parallel: every unfinished one is lit as in progress in the
     sidebar, not only the first; only the first carries its detail. */
  it("lights every unfinished milestone as in progress, not only the first", async () => {
    const milestones: MilestoneDTO[] = [
      { ...goal.milestones![0], state: "passed", status: "passed" },
      { ...goal.milestones![1], state: "not_done", status: "pending" },
      { ...goal.milestones![1], id: 3, position: 3, title: "Ship it", state: null, status: "pending" },
    ];
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), milestones } });
    const sidebar = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} compact />);
    await waitFor(() => expect(sidebar.getAllByTestId("milestone")).toHaveLength(3));
    expect(sidebar.getAllByTestId("milestone").map((li) => li.dataset.state)).toEqual(["done", "now", "now"]);
    expect(sidebar.queryByRole("progressbar")).toBeNull();
    expect(sidebar.getByTestId("goal-line").textContent).toContain("1 of 3 milestones complete");
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
    expect(sidebar.container.textContent!.match(/Ready for PR/g)?.length ?? 0).toBe(phase === "ready_for_pr" ? 1 : 0);
    expect(sidebar.container.textContent).not.toMatch(/failed/i);
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

  it("offers no Close session at Done when there is nothing to close it with", async () => {
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), goal_phase: "done" } });
    render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} compact />);
    await waitFor(() => expect(screen.getByRole("region", { name: "Goal" }).dataset.phase).toBe("done"));
    expect(screen.queryByTestId("goal-close")).toBeNull();
  });

  /* A Done goal as the panel draws it, from the engine's DTO alone: Done, the title (the full view's; the
     sidebar's pane names it above), every milestone listed and checked, nothing of a PR or of no milestones. */
  async function expectDone(view: ReturnType<typeof render>, compact: boolean) {
    await waitFor(() => expect(view.getByRole("region", { name: "Goal" }).dataset.phase).toBe("done"));
    const items = [...within(view.getByRole("list", { name: "Milestones" })).getAllByTestId("milestone")];
    if (compact) {
      expect(items.map((li) => li.dataset.state)).toEqual(["done", "done"]);
      expect(items.map((li) => li.textContent)).toEqual(["✓Client wiredDone", "✓Screen rendersDone"]);
      expect(view.getByTestId("goal-line").textContent).toBe("2 of 2 milestones complete");
      expect(view.getByTestId("goal-line").className).toContain("text-pass");
      expect(view.queryByTestId("goal-phase")).toBeNull();
      expect(view.queryByRole("progressbar")).toBeNull();
    } else {
      expect(view.getByTestId("goal-phase").textContent).toBe("Done");
      expect(view.getByRole("heading", { name: "Ship the Goal screen" })).toBeTruthy();
      expect(items.map((li) => li.textContent)).toEqual([expect.stringContaining("✓1. Client wiredDone"), expect.stringContaining("✓2. Screen rendersDone")]);
      expect(view.queryByTestId("milestone-now")).toBeNull();
      expect(view.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("100");
    }
    expect(view.container.textContent).not.toContain("No milestones yet");
    expect(view.queryByTestId("goal-review")).toBeNull();
    expect(view.queryByTestId("goal-pr")).toBeNull();
  }

  it("shows a session reaching its goal as Done, keeping its title and every milestone, checked", async () => {
    const daemon = new FakeDaemon({ goal: structuredClone(goal) });
    const store = createAppStore();
    const sync = startSync({ client: daemon.client(), store, connect: daemon.connect });
    const sidebar = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={store} compact />);
    await waitFor(() => expect(sidebar.getByRole("region", { name: "Goal" }).dataset.phase).toBe("in_progress"));
    // The engine keeps the reached goal: its title and milestones, all passed, goal_phase done.
    daemon.completeGoal(ROOT);
    expect(daemon.data.goal).toMatchObject({ goal: "Ship the Goal screen", goal_phase: "done" });
    await expectDone(sidebar, true);
    sync.stop();
  });

  it.each([true, false])("shows a Done goal opened fresh with its title and checked milestones (compact: %s)", async (compact) => {
    const daemon = new FakeDaemon({ goal: structuredClone(goal) });
    daemon.completeGoal(ROOT);
    const view = render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} compact={compact} />);
    await expectDone(view, compact);
  });

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

  it("says a finished goal is finished, not that it has no milestones, when it never listed any", async () => {
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), goal_phase: "done", milestones: [] } });
    render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} compact />);
    await waitFor(() => expect(screen.getByRole("region", { name: "Goal" }).dataset.phase).toBe("done"));
    expect(document.body.textContent).not.toContain("No milestones yet");
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

  it("never shows 'failed' for an older engine's failed goal", async () => {
    const daemon = new FakeDaemon({ goal: { ...structuredClone(goal), goal_phase: undefined, state: "failed" } });
    render(<GoalPanel client={daemon.client()} repo={REPO} root={ROOT} store={createAppStore()} />);
    await waitFor(() => expect(screen.getByTestId("goal-phase").textContent).toBe("In progress"));
    expect(document.body.textContent).not.toMatch(/failed/i);
  });

  it("keeps the last lines of a check's output", () => {
    expect(outputTail("a\nb\nc\nd\ne\nf\ng\nh\n", 3)).toBe("f\ng\nh");
    expect(outputTail(null)).toBeNull();
  });
});
