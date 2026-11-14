import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../../App";
import { ONBOARDED_KEY } from "../../api/providers";
import { connectDaemon, renderApp } from "../../test/harness";
import { FakeDaemon } from "../../test/fakeDaemon";
import { REPO, root } from "../../test/fixtures";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(ONBOARDED_KEY, "1");
});
afterEach(cleanup);

it.each(["inbox", "sidebar"] as const)("opens the ready session's diff from the %s", async (entry) => {
  const daemon = new FakeDaemon({
    repos: [{ repo_root: REPO, name: "proj" }],
    sessions: [{ id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 1, goal: "Ship the change" }],
    workspaces: [root],
    goal: { root_id: "sup1", enabled: true, goal: "Ship the change", state: "done", goal_phase: "ready_for_pr", progress: 1, milestones: [] },
    inbox: [{ kind: "pr_ready", reason: "Ready", root_id: "sup1", agent_id: "sup1", workspace_id: "proj" }],
    sessionReview: { sup1: { ready: true, reasons: [], branch: "dev/change", base: "main", diff: "" } },
    sessionBase: { sup1: { "change.ts": "export const value = 1;\n" } },
    sessionFiles: { sup1: { "change.ts": "export const value = 2;\n" } },
  });
  const { client, store, sync } = connectDaemon(daemon);
  renderApp(<App client={client} store={store} refresh={sync.refresh} initialRoute={entry === "inbox" ? { screen: "inbox" } : { screen: "session", root: "sup1", repo: REPO, agent: "sup1" }} />);

  if (entry === "sidebar") await screen.findByTestId("goal-phase");
  const source = entry === "inbox"
    ? await screen.findByTestId("inbox-card")
    : await screen.findByRole("region", { name: "Session context" });
  fireEvent.click(await within(source).findByRole("button", { name: /Review & open PR/ }));
  const sheet = await screen.findByRole("dialog", { name: "Review & open PR" });
  fireEvent.click(within(sheet).getByTestId("review-changes"));
  await within(sheet).findByRole("navigation", { name: "Changed files" });
  expect(within(sheet).getAllByTestId("hunk").length).toBeGreaterThan(0);
  expect(daemon.requests.some((r) => r.method === "POST" && r.path === "/sessions/sup1/pr")).toBe(false);
});
