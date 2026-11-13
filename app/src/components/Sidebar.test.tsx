import { connectDaemon, inTestContext } from "../test/harness";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import { DaemonClient } from "../api/client";
import type { GoalDTO, InboxItemDTO, SessionDTO } from "../api/schema";
import type { AppUpdates } from "../api/tauri";
import type { Route } from "../routes";
import { SessionsProvider } from "../store/sessions";
import { UsageProvider } from "../store/usage";
import { BASE_URL, FakeDaemon, TOKEN, freshProviders, type DaemonData } from "../test/fakeDaemon";
import { REPO, permission, permissionInTerminal, question, root, waiting, worker } from "../test/fixtures";
import { usageScenario } from "../test/usage";
import { AppUpdateProvider } from "./AppUpdateBanner";
import { PAUSED_KEY, SPLIT_KEY, Sidebar, roleOf } from "./Sidebar";
import { sidebarChanges } from "./SessionContext";
import { sessionTree } from "../screens/Session/tree";

beforeEach(() => localStorage.clear());
afterEach(cleanup);

const live: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };
const paused: SessionDTO = { ...live, id: "sup0", status: "paused", status_since: Date.now() / 1000 - 2 * 86400, goal: "Refactor the cache" };
const child = { ...worker.agents![0], id: "c1", parent_id: "w1" };
const goal: GoalDTO = { root_id: "sup1", enabled: true, goal: "Ship ls --json", state: "running", progress: 3, milestones: [{ id: 1, position: 1, title: "Parse flags", status: "passed", check_cmd: "npm test" }, { id: 2, position: 2, title: "Print JSON", status: "running", check_cmd: "npm run build" }] };

function mount(data: Partial<DaemonData>, route: Route = { screen: "new" }, fetch?: (daemon: FakeDaemon) => typeof globalThis.fetch, openUrl?: (url: string) => Promise<void>) {
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [live, paused], ...data });
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: fetch?.(daemon) ?? daemon.fetch });
  const { store, sync } = connectDaemon(daemon, client);
  const navigate = vi.fn();
  render(
    inTestContext(<><AppUpdateProvider updates={{} as AppUpdates}>
            <SessionsProvider client={client}>
              <UsageProvider client={client}>
                <Sidebar route={route} navigate={navigate} refresh={sync.refresh} openUrl={openUrl} />
              </UsageProvider>
            </SessionsProvider>
          </AppUpdateProvider></>, { client, store }, { daemon: true, providers: true }),
  );
  return { daemon, sync, navigate, store };
}

const agentRow = (id: string) => document.querySelector<HTMLButtonElement>(`button[data-tree-agent="${id}"]`);
const sessionRow = (id: string) => document.querySelector<HTMLButtonElement>(`button[data-tree-session="${id}"]`);
const list = () => screen.getByRole("navigation", { name: "Sessions" });

describe("Sidebar", () => {
  it("uses the open session supervisor's provider even before its model is known", async () => {
    const { sync } = mount({
      workspaces: [{ ...root, agents: [{ ...root.agents![0], provider: "codex", model: null }] }, worker],
      usage: usageScenario("near"),
    }, { screen: "session", root: "sup1", repo: REPO, agent: "w1" });
    await waitFor(() => expect(screen.getByTestId("profile-usage").dataset.state).toBe("reading"));
    expect(screen.getByTestId("profile-usage").dataset.provider).toBe("codex");
    sync.stop();
    cleanup();

    // A remembered context session does not choose usage outside a session route.
    mount({
      workspaces: [{ ...root, agents: [{ ...root.agents![0], provider: "codex", model: "gpt-5" }] }],
      usage: usageScenario("near"),
    });
    await waitFor(() => expect(screen.getByTestId("profile-usage").dataset.state).toBe("reading"));
    expect(screen.getByTestId("profile-usage").dataset.provider).toBe("claude");
  });

  it("uses the neutral usage state when the session provider has no weekly reading", async () => {
    const usage = usageScenario("near");
    usage.providers = usage.providers.map((provider) => provider.provider === "claude"
      ? { ...provider, windows: provider.windows.filter((window) => window.minutes !== 10080) }
      : provider);
    mount({
      workspaces: [{ ...root, agents: [{ ...root.agents![0], model: "claude-opus-4-7" }] }], usage,
    }, { screen: "session", root: "sup1", repo: REPO });
    await act(async () => {});
    await waitFor(() => expect(screen.getByTestId("usage-warning-dot")).toBeTruthy());
    expect(screen.getByTestId("profile-usage").dataset.state).toBe("none");
    expect(screen.getByTestId("profile-usage").dataset.provider).toBeUndefined();
  });

  it("running agents count includes supervisors and workers, describes the count on hover, and hides at zero", async () => {
    const { store } = mount({ workspaces: [{ ...root, agents: [{ ...root.agents![0], mode: "interactive" }] }, worker] });
    await screen.findByLabelText("2 agents running");

    act(() => store.dispatch({ type: "snapshot", workspaces: [{ ...root, agents: [{ ...root.agents![0], status: "paused" }] }, worker] }));
    expect(screen.getByLabelText("1 agent running").getAttribute("title")).toBe("1 agent running");

    act(() => store.dispatch({ type: "snapshot", workspaces: [{ ...root, agents: [{ ...root.agents![0], status: "paused" }] }, { ...worker, agents: [{ ...worker.agents![0], status: "done" }] }] }));
    expect(screen.queryByLabelText(/agent[s]? running/)).toBeNull();
  });
  it("does not substitute another session's context when the selected session disappears", async () => {
    const { daemon } = mount({ sessions: [live, paused] }, { screen: "session", root: "sup1", repo: REPO });
    await act(async () => {});
    expect(screen.getByRole("button", { name: "Rename session" })).toBeTruthy();
    await act(async () => {
      daemon.data.sessions = [paused];
      daemon.publish("session.closed", { root_id: "sup1" });
    });
    expect(sessionRow("sup1")).toBeNull();
    expect(screen.getByTestId("session-context").textContent).not.toContain(paused.goal);
    expect(screen.queryByRole("button", { name: "Rename session" })).toBeNull();
  });
  it("sidebar foot is one row with the profile pill and terminal, even with provider problems", async () => {
    const { sync, store, navigate } = mount({ account: { signed_in: true, name: "Alex Brown" }, usage: usageScenario("normal"), providers: freshProviders({ claude: { path: "/bin/claude", version: "2" } }) });
    await waitFor(() => expect(store.getState().account?.name).toBe("Alex Brown"));
    const pill = screen.getByTestId("profile-pill");
    await waitFor(() => expect(within(pill).getByTestId("profile-usage").dataset.state).toBe("reading"));
    expect(pill.tagName).toBe("BUTTON");
    expect(pill.textContent).toContain("AB");
    expect(pill.querySelector("button")).toBeNull();
    expect(screen.queryByTestId("provider-problem")).toBeNull();
    expect(screen.queryByTestId("usage-warning-dot")).toBeNull();
    fireEvent.click(pill);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "account", section: "usage", ask: expect.any(Number) });
    act(() => store.dispatch({ type: "app_update", status: { automatic: true, version: "0.6.2", available: { version: "0.6.3", notes: null }, later: true, pending: null, updated: null, last_checked: null, checking: false } }));
    expect(screen.queryByTestId("app-update-pill")).toBeNull();
    sync.stop();
    cleanup();
    mount({ usage: usageScenario("several"), providers: freshProviders({ claude: { path: "/bin/claude", version: "2" }, codex: { path: "/bin/codex", version: "1", signed_in: false } }) });
    await waitFor(() => expect(screen.getByTestId("sidebar-foot").children).toHaveLength(1));
    expect(screen.queryByTestId("provider-problem")).toBeNull();
    expect(within(screen.getByTestId("sidebar-foot-row")).getByTestId("profile-pill")).toBeTruthy();
    expect(screen.getByTestId("usage-warning-dot")).toBeTruthy();
  });
  it.each([
    ["processing", null, undefined, "Working", "working"],
    ["idle", "working", undefined, "Working", "working"],
    ["idle", "checks", undefined, "Working", "working"],
    ["idle", "review", undefined, "Reviewing", "review"],
    ["idle", "merging", undefined, "Merging", "merging"],
    ["idle", null, undefined, "Idle", null],
    ["paused", "review", undefined, "Paused", null],
    ["failed", "review", undefined, "Failed", null],
    ["paused", "review", "done", "Done", "done"],
    ["idle", "merging", "done", "Done", "done"],
  ])("session status word: %s / %s / %s reads as %s", async (status, stage, goal_phase, word, colour) => {
    mount({ workspaces: [{ ...root, agents: [{ ...root.agents![0], model: "claude-sonnet-4" }] }, worker], sessions: [{ ...live, status }], goal: { ...goal, stage, goal_phase } }, { screen: "session", root: "sup1", repo: REPO });
    await waitFor(() => expect(within(sessionRow("sup1")!).getByTestId("session-status").textContent).toBe(word));
    const row = sessionRow("sup1")!;
    const label = within(row).getByTestId("session-status");
    expect(label.className).toContain(colour ? `st-${colour} stage-word` : "text-ink-3");
    expect(label.classList.contains("stage-word")).toBe(colour !== null);
    const rested = status === "paused" || status === "failed";
    const marker = rested ? row.querySelector(".side-dot")! : screen.getByRole("button", { name: "Hide sup1's agents" });
    expect(marker.className).toContain(colour ? `st-${colour}` : "text-ink-3");
    expect(row.querySelector(".supervisor-tag")).toBeNull();
    if (!rested) {
      expect(within(row).getByTestId("session-model").textContent).toBe("sonnet");
      fireEvent.click(screen.getByRole("button", { name: "Hide sup1's agents" }));
      expect(agentRow("w1")).toBeNull();
      expect(within(row).getByTestId("session-status").textContent).toBe(word);
    }
  });

  it("no drift mark: a drifting session has none with or without a PR", async () => {
    const { sync } = mount({ workspaces: [root, worker], sessions: [live], goal: { ...goal, drifting: true } as typeof goal });
    const row = await screen.findByTestId("tree-session-sup1");
    await waitFor(() => expect(row.querySelector("[data-testid='session-status']")).toBeTruthy());
    expect(row.querySelector('[data-state="drifting"]')).toBeNull();
    expect(screen.queryByTestId("session-pr")).toBeNull();

    sync.stop();
    cleanup();
    mount({ workspaces: [root, worker], sessions: [live], goal: { ...goal, drifting: true, pr_url: "https://github.com/o/r/pull/390", pr_state: { number: 390, state: "ready", done: 2, total: 2 } } as typeof goal });
    const prRow = await screen.findByTestId("tree-session-sup1");
    await screen.findByTestId("session-pr");
    expect(prRow.querySelector('[data-state="drifting"]')).toBeNull();
  });

  it.each([
    [{ number: 391, state: "ci", done: 14, total: 18 }, "PR #391 · In CI, 14 of 18", "text-sap"],
    [{ number: 387, state: "ready", done: 4, total: 4 }, "PR #387 · Ready to merge", "text-pass"],
    [{ number: 389, state: "failed", done: 5, total: 6 }, "PR #389 · CI failed", "text-fail"],
    [{ number: 391, state: "unknown", done: 0, total: 0 }, "PR #391", "text-ink-3"],
    [undefined, null, null],
  ])("pr line: %j reads %s", async (pr_state, text, tone) => {
    const openUrl = vi.fn(async () => undefined);
    const url = "https://github.com/o/r/pull/391";
    const { navigate } = mount({ workspaces: [root, { ...worker, agents: [{ ...worker.agents![0], status: "done" }] }], sessions: [live], goal: { ...goal, goal_phase: "pr_open", pr_url: url, pr_state } }, undefined, undefined, openUrl);
    await waitFor(() => expect(within(sessionRow("sup1")!).getByTestId("session-status").textContent).toBe("Idle"));
    if (text === null) {
      await waitFor(() => expect(sessionRow("sup1")).toBeTruthy());
      expect(screen.queryByTestId("session-pr")).toBeNull();
      return;
    }
    fireEvent.click(screen.getByRole("button", { name: "Hide sup1's agents" }));    // folded rows show it too
    const line = await screen.findByTestId("session-pr");
    expect(line.textContent).toBe(text);
    expect(line.className).toContain(tone!);
    fireEvent.click(line);
    expect(openUrl).toHaveBeenCalledWith(url);
    expect(navigate).not.toHaveBeenCalled();
  });

  it.each([
    ["processing", "done", true, "failed", 0, "fixing", "text-sap"],
    ["idle", "processing", true, "failed", 0, "fixing", "text-sap"],
    ["paused", "processing", true, "failed", 0, "fixing", "text-sap"],
    ["idle", "processing", false, "failed", 0, "failed", "text-fail"],
    ["processing", "done", true, "failed", 2, "pushing", "text-sap"],
    ["processing", "processing", true, "ci", 2, "ci", "text-sap"],
  ])("Fixing CI follows session activity and PR progress: %s / %s / %s / %s / %s", async (status, workerStatus, workerAlive, state, unpushed, expected, tone) => {
    const { store } = mount({
      workspaces: [{ ...root, agents: [{ ...root.agents![0], status }] }, { ...worker, agents: [{ ...worker.agents![0], status: workerStatus }] }],
      sessions: [{ ...live, status }],
      goal: { ...goal, pr_url: "https://github.com/o/r/pull/413", pr_state: { number: 413, state, done: 1, total: 2, unpushed, failed_checks: ["build"] } },
    });
    await act(async () => {});
    act(() => store.dispatch({ type: "stream", message: { type: "liveness", agents: { w1: workerAlive } } }));
    const fold = screen.queryByRole("button", { name: "Hide sup1's agents" });
    if (fold) fireEvent.click(fold);
    const line = await screen.findByTestId("session-pr");
    expect(line.dataset.state).toBe(expected);
    expect(line.className).toContain(tone);
    expect(line.querySelector("[data-pr-pulse]") !== null).toBe(expected === "fixing");
    if (expected === "fixing") {
      expect(line.querySelector("[data-pr-pulse]")!.className).toContain("motion-safe:animate-pulse");
      expect(line.title).toContain("build");
    }
  });

  it("names the selected session in its context at once, while its goal is still loading", async () => {
    mount({}, { screen: "session", root: "sup0", repo: REPO }, (daemon) => (url, init) => (String(url).includes("/goal") ? new Promise(() => {}) : daemon.fetch(url, init)));
    await waitFor(() => expect(screen.getByTestId("session-context").textContent).toContain("Refactor the cache"));
  });

  it("branches workers from the root dot, folds from it, and opens the supervisor from the row", async () => {
    const supervisor = { ...worker.agents![0], profile: "supervisor-sonnet", milestone: "Release page" };
    const { navigate } = mount({ workspaces: [root, { ...worker, agents: [supervisor, child] }] });
    await waitFor(() => expect(agentRow("c1")).toBeTruthy());
    expect(agentRow("sup1")).toBeNull();
    expect(agentRow("w1")!.closest("ul")!.parentElement!.dataset.session).toBe("sup1");
    expect(agentRow("c1")!.closest("ul")!.parentElement).toBe(agentRow("w1")!.closest("li"));
    expect(agentRow("w1")!.closest("li")!.classList.contains("session-tree-node")).toBe(true);
    expect(agentRow("c1")!.closest("li")!.classList.contains("session-tree-node")).toBe(true);
    expect(within(agentRow("w1")!).getByTestId("agent-role").textContent).toBe("supervisor · sonnet");
    expect(agentRow("w1")!.querySelector(".supervisor-tag")).toBeNull();
    const dot = screen.getByRole("button", { name: "Hide sup1's agents" });
    expect(dot.classList.contains("session-root-dot")).toBe(true);
    expect(dot.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(dot);
    expect(dot.getAttribute("aria-expanded")).toBe("false");
    expect(agentRow("w1")).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
    fireEvent.click(dot);
    expect(dot.getAttribute("aria-expanded")).toBe("true");
    expect(agentRow("c1")).toBeTruthy();
    // SessionGoal's block wrapper fills the row; its button must fill that wrapper.
    expect(sessionRow("sup1")!.classList.contains("w-full")).toBe(true);
    fireEvent.click(sessionRow("sup1")!);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "session", root: "sup1", repo: REPO, agent: "sup1" });
  });

  it("uses the short goal summary on one line and copies the full goal", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const { navigate } = mount({ workspaces: [root], goal: { ...goal, summary: "CLI polish" } as GoalDTO });
    await waitFor(() => expect(sessionRow("sup1")!.textContent).toContain("CLI polish"));
    expect(sessionRow("sup1")!.querySelector("[data-testid=session-title]")!.classList.contains("truncate")).toBe(true);
    expect(sessionRow("sup1")!.querySelector(".supervisor-tag")).toBeNull();
    fireEvent.click(sessionRow("sup1")!);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "session", root: "sup1", repo: REPO, agent: "sup1" });
    await act(async () => fireEvent.dblClick(sessionRow("sup1")!));
    expect(writeText).toHaveBeenCalledWith(goal.goal);
  });

  it.each(["rewound", "dismissed"])("omits a %s worker from the session sidebar", async (reason) => {
    const past = { ...worker.agents![0], status: reason === "rewound" ? "rewound" : "exited", dismissed_at: reason === "dismissed" ? 1 : null };
    const fresh = { ...worker.agents![0], id: "fresh" };
    mount({ workspaces: [root, { ...worker, agents: [past, fresh] }] }, { screen: "session", root: "sup1", repo: REPO });
    await waitFor(() => expect(agentRow("fresh")).toBeTruthy());
    expect(agentRow("w1")).toBeNull();
  });

  it("worker row role reads only the role, with the milestone, model and branch in the tooltip, and double-click copies its info", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const a = { ...worker.agents![0], profile: "developer-haiku", milestone: "Logo goes Home" };
    const b = { ...a, id: "w2", milestone: undefined, profile: "developer-heavy" };
    // The pipeline's reviewer is the developer's sibling (same parent), on a Codex model.
    const r = { ...a, id: "w3", mode: "review", profile: "codex", provider: "codex", model: "gpt-6-luna", milestone: undefined };
    const g = { ...a, id: "w4", profile: "developer-gemini", provider: "gemini", model: "gemini-3-pro", milestone: undefined };
    const bare = { ...a, id: "w5", profile: "claude", milestone: undefined };
    mount({ workspaces: [root, { ...worker, agents: [a, r] }, { ...worker, id: "proj/other", name: "other", branch: "feat/other", agents: [b, g, bare] }] }, { screen: "session", root: "sup1", repo: REPO });
    await waitFor(() => expect(agentRow("w1")).toBeTruthy());
    expect(within(agentRow("w4")!).getByTestId("agent-role").textContent).toBe("developer · gemini");
    expect(agentRow("w1")!.closest("li")!.contains(agentRow("w3"))).toBe(true);
    expect(within(agentRow("w3")!).getByTestId("agent-role").textContent).toBe("reviewer · codex");
    expect(within(agentRow("w2")!).getByTestId("agent-role").textContent).toBe("developer · claude");
    expect(within(agentRow("w5")!).getByTestId("agent-role").textContent).toBe("developer");
    expect(within(agentRow("w1")!).getByTestId("agent-role").textContent).toBe("developer · haiku");
    expect(agentRow("w1")!.title).toContain("haiku");
    expect(agentRow("w1")!.title).toContain("milestone: Logo goes Home");
    expect(agentRow("w3")!.title).toContain("gpt-6-luna");
    expect(agentRow("w2")!.querySelector("[data-testid=agent-title]")).toBeNull();
    vi.useFakeTimers();
    onTestFinished(() => { vi.useRealTimers(); });
    await act(async () => {
      fireEvent.dblClick(agentRow("w1")!);
      await writeText.mock.results[0].value;
    });
    expect(writeText).toHaveBeenCalledWith("id: w1\nrole: developer\nprofile: developer-haiku\nbranch: feat/ls\nmilestone: Logo goes Home");
    expect(screen.getByRole("status")).toBeTruthy();
  });

  it("nests a reviewer only under the one worker of its own session in its workspace", () => {
    const agent = (id: string, mode: string, parent: string) => ({ ...worker.agents![0], id, mode, parent_id: parent });
    const ws = (agents: ReturnType<typeof agent>[]) => ({ ...worker, agents });
    const parentsIn = (workspaces: ReturnType<typeof ws>[], of: string) => {
      const found = new Map<string, string>();
      const walk = (n: NonNullable<ReturnType<typeof sessionTree>>) => n.children.forEach((c) => { found.set(c.agent.id, n.agent.id); walk(c); });
      walk(sessionTree(workspaces, "sup1")!);
      return found.get(of);
    };
    // Sharing the workspace with its supervisor, the sole worker still wins; another session's worker or a second worker leave it be.
    expect(parentsIn([ws([agent("sup1", "interactive", ""), agent("d", "assign", "sup1"), agent("r", "review", "sup1")])], "r")).toBe("d");
    // A reviewer already under its developer stays there beside a helper; with the developer gone, it stays under its parent.
    expect(parentsIn([ws([agent("sup1", "interactive", ""), agent("d", "assign", "sup1"), agent("h", "assign", "d"), agent("r", "review", "d")])], "r")).toBe("d");
    const gone = { ...agent("d", "assign", "sup1"), dismissed_at: 1 };
    const withGone = [ws([agent("sup1", "interactive", ""), gone, agent("r", "review", "d"), agent("h", "assign", "r")])];
    expect([parentsIn(withGone, "d"), parentsIn(withGone, "r"), parentsIn(withGone, "h")]).toEqual([undefined, "sup1", "r"]);
    expect(parentsIn([ws([agent("sup1", "interactive", ""), agent("x", "assign", "sup2"), agent("r", "review", "sup1")])], "r")).toBe("sup1");
    expect(parentsIn([ws([agent("sup1", "interactive", ""), agent("d", "assign", "sup1"), agent("e", "assign", "sup1"), agent("r", "review", "sup1")])], "r")).toBe("sup1");
  });

  it("new user sees no usage failure", async () => {
    const { daemon } = mount({});
    const reading = await screen.findByTestId("profile-usage");
    await waitFor(() => expect(daemon.requests.some((r) => r.path === "/usage")).toBe(true));
    expect(reading.textContent).not.toContain("unavailable");
  });

  it("marks the usage reading failed when the load really failed", async () => {
    mount({}, { screen: "new" }, (daemon) => (input, init) =>
      String(input).endsWith("/usage") ? Promise.resolve(new Response("{}", { status: 500 })) : daemon.fetch(input, init));
    await waitFor(() => expect(screen.getByTestId("profile-usage").dataset.state).toBe("failed"));
  });

  it.each([
    { account: { signed_in: true, name: "Sam Rivera", email: "other@example.com" }, initials: "SR" },
    { account: { signed_in: true, name: "  ", email: "sam.rivera@example.com" }, initials: "S" },
    { account: { signed_in: true }, initials: "" },
    { account: { signed_in: false, name: "Sam Rivera", email: "sam@example.com" }, initials: "" },
  ])("renders the account identity fallback for $account", async ({ account, initials }) => {
    const { navigate, store } = mount({ account }, { screen: "account" });
    const button = screen.getByRole("button", { name: "Open Account › Usage" });
    await waitFor(() => {
      expect(store.getState().account).toEqual(account);
      expect(button.firstElementChild!.textContent).toBe(initials);
      expect(Boolean(button.querySelector("svg"))).toBe(!initials);
      expect(button.firstElementChild!.className.includes("text-ink-3")).toBe(!account.signed_in);
      expect(button.firstElementChild!.className.includes("border-dashed")).toBe(!account.signed_in);
      expect(button.firstElementChild!.className.includes("bg-pass-soft")).toBe(account.signed_in);
    });
    expect(screen.getByRole("tablist", { name: "Settings" })).toBeTruthy();
    expect(screen.queryByRole("navigation", { name: "Sessions" })).toBeNull();
    if (initials) expect(screen.getAllByText(initials)).toHaveLength(2);
    fireEvent.click(button);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "account", section: "usage", ask: expect.any(Number) });
  });

  it("lists every open session by repo with its agents nested in the CLI's rows, recent ones folded, and opens them", async () => {
    const { navigate, store } = mount({ workspaces: [root, { ...worker, agents: [...worker.agents!, child] }], inbox: [waiting] }, { screen: "session", root: "sup1", repo: REPO, agent: "w1" });
    expect(list().textContent).toContain("Loading");
    expect(within(screen.getByTestId("sidebar-foot")).getByRole("button", { name: "Open Account › Usage" })).toBeTruthy();
    await waitFor(() => expect(agentRow("w1")).toBeTruthy());
    expect(screen.getByTestId("repo-proj")).toBeTruthy();
    // The session is its supervisor, with the workers directly beneath it.
    const session = screen.getByTestId("tree-session-sup1");
    expect(agentRow("sup1")).toBeNull();
    expect(session.contains(agentRow("w1"))).toBe(true);
    expect(agentRow("w1")!.closest("li")!.contains(agentRow("c1"))).toBe(true);
    // The rows read: a dot for the CLI's status, "id · role", the label; the branch is in the tooltip.
    expect(agentRow("w1")!.querySelector(".side-dot")!.getAttribute("data-tone")).toBe("work");
    const workingClass = within(agentRow("w1")!).getByTestId("agent-status").className;
    const workingDot = agentRow("w1")!.querySelector(".side-dot")!.className;
    for (const review_state of ["checks running", "waiting for checks", "checks waiting for room: 2 running"]) {
      act(() => store.dispatch({ type: "snapshot", workspaces: [root, { ...worker, review_state, agents: [...worker.agents!, child] }], inbox: [waiting] }));
      expect(within(agentRow("w1")!).getByTestId("agent-status").className).toBe(workingClass);
      expect(agentRow("w1")!.querySelector(".side-dot")!.className).toBe(workingDot);
    }
    expect(within(session).queryByTestId("needs-you")).toBeNull();
    expect(screen.queryByTestId("summary")).toBeNull();
    // The route's agent is the current one.
    expect(agentRow("w1")!.getAttribute("aria-current")).toBe("page");
    expect(sessionRow("sup1")!.getAttribute("aria-current")).toBeNull();
    // A paused session sits behind the group's collapsed "Paused · 1" row, at its foot.
    expect(sessionRow("sup0")).toBeNull();
    expect(screen.queryByText(/Recent/)).toBeNull();

    // Clicking opens the provider session on that agent (or a session on its supervisor).
    fireEvent.click(agentRow("c1")!);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "session", root: "sup1", repo: REPO, agent: "c1" });
    fireEvent.click(screen.getByRole("button", { name: /Paused · 1/ }));
    fireEvent.click(sessionRow("sup0")!);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "session", root: "sup0", repo: REPO, agent: "sup0" });
    fireEvent.click(screen.getByRole("button", { name: "New session" }));
    expect(navigate).toHaveBeenLastCalledWith({ screen: "new" });
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
    const { navigate } = mount({ workspaces: [root, worker] }, { screen: "session", root: "sup1", repo: REPO, agent: "sup1" });
    await waitFor(() => expect(agentRow("w1")).toBeTruthy());
    // The row on screen (the session's) is where the keys start: down is its supervisor, then its worker.
    const reveal = vi.fn();
    agentRow("w1")!.scrollIntoView = reveal;
    fireEvent.keyDown(list(), { key: "ArrowDown" });
    fireEvent.keyDown(list(), { key: "ArrowDown" });
    expect(reveal).toHaveBeenCalled();
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
  });

  it("takes the logo, a button labelled Home, to the Home screen", async () => {
    const { navigate } = mount({}, { screen: "session", root: "sup1", repo: REPO });
    fireEvent.click(screen.getByRole("button", { name: "Home" }));
    expect(navigate).toHaveBeenLastCalledWith({ screen: "new" });
  });

  it("leads with one Inbox · N entry at the very top, the only thing about attention in the sidebar, which opens the Inbox page", async () => {
    const { daemon, navigate } = mount({ workspaces: [root, worker], inbox: [question, waiting, permission] }, { screen: "new" });
    await waitFor(() => expect(screen.getByTestId("inbox-badge").textContent).toBe("3"));
    const inbox = screen.getByTestId("inbox-link");
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
    fireEvent.click(inbox);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "inbox" });
    // Nothing waiting: no pill at all.
    daemon.data.inbox = [];
    daemon.publish("message.queued", { agent_id: "sup1" });
    await waitFor(() => expect(screen.queryByTestId("inbox-badge")).toBeNull());
  });

  it("shows the session's changes in one coloured line that toggles them in the chat's place", async () => {
    sidebarChanges.shown = true;
    onTestFinished(() => {
      sidebarChanges.shown = false;
    });
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

  it("keeps milestones on top of Sessions with the short goal and a persistent split", async () => {
    const credentials: InboxItemDTO = { kind: "credentials", reason: "credentials need attention", agent_id: "c1", workspace_id: "proj/feat-ls", detail: "claude auth login" };
    const { sync } = mount(
      { workspaces: [root, { ...worker, agents: [...worker.agents!, child] }], inbox: [permission, waiting, permissionInTerminal, credentials], goal: { ...structuredClone(goal), summary: "CLI polish" } },
      { screen: "session", root: "sup1", repo: REPO, agent: "sup1" },
    );
    const context = () => screen.getByTestId("session-context");
    // Completion stays visible alongside the phase; the milestone list carries the detail.
    await waitFor(() => expect(within(context()).getByTestId("goal-line").textContent).toContain("CLI polish"));
    expect(context().compareDocumentPosition(list()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(context()).getByTestId("goal-count").textContent).toBe("1 of 2");
    const milestones = within(context()).getByRole("list", { name: "Milestones" });
    expect(milestones.textContent).toContain("Print JSON");
    expect(within(milestones).getAllByTestId("milestone").map((li) => li.dataset.state)).toEqual(["done", "now"]);
    expect(within(context()).getByRole("progressbar").getAttribute("aria-valuenow")).toBe("50");
    expect(screen.queryByTestId("connection")).toBeNull();
    expect(within(context()).queryByRole("button", { name: /Check milestone|Run checks/ })).toBeNull();
    expect(within(context()).queryByRole("switch")).toBeNull();
    // Nothing about a PR until the engine says the session is ready (the fake's never does here).
    expect(within(context()).queryByTestId("goal-review")).toBeNull();
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

  it("renames a session from its context title, then lists it by that name in its row and context; an empty name goes back to the goal", async () => {
    let refuse = true;
    const { daemon } = mount({ workspaces: [root, worker], goal: structuredClone(goal) }, { screen: "session", root: "sup1", repo: REPO, agent: "sup1" }, (d) => (input, init) =>
      refuse && String(input).endsWith("/rename") ? Promise.resolve(new Response(JSON.stringify({ error: { code: "invalid", message: "nope" } }), { status: 400 })) : d.fetch(input, init),
    );
    await waitFor(() => expect(sessionRow("sup1")).toBeTruthy());
    const rename = async (name: string) => {
      fireEvent.click(await screen.findByRole("button", { name: "Rename session" }));
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
  });

  it("closes a session from its row's close, shown on hover or focus: one click, nothing asked, the row just goes", async () => {
    const { daemon, navigate } = mount({ workspaces: [root, worker], goal: structuredClone(goal) }, { screen: "session", root: "sup1", repo: REPO, agent: "sup1" });
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

  it("looks a closed parent up again with the next snapshot when the lookup failed", async () => {
    let failed = 0;
    const { daemon } = mount(
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
  });
});

it.each([
  ["developer", "developer"],
  ["developer-codex-heavy", "developer"],
  ["developer-or-qwen3-coder", "developer"],
  ["developer-some-new-model", "developer"],
  ["reviewer-gemini-low", "reviewer"],
  ["backend-auditor", "backend-auditor"],
  ["test-writer-sonnet", "test-writer"],
  ["qa", "qa"],
])("%s reads as the role %s", (profile, role) => {
  expect(roleOf(profile)).toBe(role);
});
