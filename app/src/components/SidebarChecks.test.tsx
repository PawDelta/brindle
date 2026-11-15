import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SessionsProvider } from "../store/sessions";
import { UsageProvider } from "../store/usage";
import { FakeDaemon } from "../test/fakeDaemon";
import { REPO, root, worker } from "../test/fixtures";
import { connectDaemon, inTestContext } from "../test/harness";
import { Sidebar } from "./Sidebar";

afterEach(cleanup);

it("distinguishes branch checks from coding and review in worker and session rows", async () => {
  localStorage.clear();
  const daemon = new FakeDaemon({
    repos: [{ repo_root: REPO, name: "proj" }],
    sessions: [{ id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0 }],
    workspaces: [root, { ...worker, review_state: "checks running" }],
    goal: { root_id: "sup1", enabled: true, goal: "Ship the branch", state: "running", progress: 0, milestones: [], stage: "checks" },
  });
  const client = daemon.client();
  const { store, sync } = connectDaemon(daemon, client);
  try {
    render(inTestContext(
      <SessionsProvider client={client}><UsageProvider client={client}>
        <Sidebar route={{ screen: "session", root: "sup1", repo: REPO }} navigate={vi.fn()} />
      </UsageProvider></SessionsProvider>,
      { client, store }, { daemon: true, providers: true },
    ));
    const session = await screen.findByTestId("tree-session-sup1");
    await act(async () => {});
    const workerStatus = () => within(session.querySelector<HTMLElement>('[data-tree-agent="w1"]')!).getByTestId("agent-status");
    const sessionStatus = () => within(session).getByTestId("session-status");
    expect.soft(workerStatus().dataset.state).toBe("checks");
    expect.soft(sessionStatus().dataset.state).toBe("checking");
    expect.soft(workerStatus().classList.contains("st-checks")).toBe(true);
    expect.soft(sessionStatus().classList.contains("st-checks")).toBe(true);

    // Both queue formats retain the checks stage while waiting for a slot.
    for (const review_state of ["waiting for checks", "checks waiting for room: 2 running"]) {
      act(() => store.dispatch({ type: "snapshot", workspaces: [root, { ...worker, review_state }] }));
      expect(workerStatus().dataset.state).toBe("checks");
    }

    // The same mounted rows follow the pipeline out of checks into review.
    daemon.data.goal = { ...daemon.data.goal!, stage: "review" };
    daemon.data.workspaces = [root, { ...worker, review_state: "reviewer running (r1)" }];
    daemon.publish("autopilot.checking", { repo_root: REPO });
    await waitFor(() => expect(sessionStatus().dataset.state).toBe("reviewing"));
    expect(workerStatus().dataset.state).toBe("review");
  } finally {
    sync.stop();
  }
});
