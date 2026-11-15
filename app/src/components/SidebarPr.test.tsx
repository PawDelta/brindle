import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DaemonClient } from "../api/client";
import type { GoalDTO } from "../api/schema";
import type { AppUpdates } from "../api/tauri";
import { SessionsProvider } from "../store/sessions";
import { UsageProvider } from "../store/usage";
import { BASE_URL, FakeDaemon, TOKEN } from "../test/fakeDaemon";
import { REPO, root } from "../test/fixtures";
import { connectDaemon, inTestContext } from "../test/harness";
import { AppUpdateProvider } from "./AppUpdateBanner";
import { Sidebar } from "./Sidebar";

afterEach(cleanup);

it("renders the goal DTO's externally opened PR under its supervisor", async () => {
  // Same goal contract exercised by test_session_pr_tracking.py.
  const goal: GoalDTO = {
    root_id: "sup1", enabled: true, state: "running", progress: 0, milestones: [],
    goal_phase: "pr_open", pr_url: "https://github.com/o/r/pull/458",
    pr_state: { number: 458, state: "ready", done: 1, total: 1 },
  };
  const daemon = new FakeDaemon({
    repos: [{ repo_root: REPO, name: "proj" }], workspaces: [root], goal,
    sessions: [{ id: "sup1", workspace_id: root.id, profile: "supervisor", status: "idle", mode: "interactive", created_at: 0 }],
  });
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: daemon.fetch });
  const { store, sync } = connectDaemon(daemon, client);
  const openUrl = vi.fn(async () => undefined);
  render(inTestContext(
    <AppUpdateProvider updates={{} as AppUpdates}>
      <SessionsProvider client={client}>
        <UsageProvider client={client}>
          <Sidebar route={{ screen: "session", root: "sup1", repo: REPO }} navigate={vi.fn()} refresh={sync.refresh} openUrl={openUrl} />
        </UsageProvider>
      </SessionsProvider>
    </AppUpdateProvider>, { client, store }, { daemon: true, providers: true },
  ));
  const line = await screen.findByTestId("session-pr");
  expect(line.textContent).toBe("PR #458 · Ready to merge");
  expect(line.closest('[data-testid="tree-session-sup1"]')).toBeTruthy();
  fireEvent.click(line);
  await waitFor(() => expect(openUrl).toHaveBeenCalledWith(goal.pr_url));
  sync.stop();
});
