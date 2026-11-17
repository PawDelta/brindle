import { connectDaemon, inTestContext } from "../test/harness";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DaemonClient } from "../api/client";
import { SessionsProvider } from "../store/sessions";
import { UsageProvider } from "../store/usage";
import { BASE_URL, FakeDaemon, TOKEN } from "../test/fakeDaemon";
import { REPO, root } from "../test/fixtures";
import { Sidebar } from "./Sidebar";
import { GoalPanel } from "../screens/Session/GoalPanel";
import { createAppStore } from "../store/store";

afterEach(cleanup);

it("clamps notes and action failures below milestones with their full text on hover", async () => {
  const note = "The audit found additional details about the implementation and its checks. ".repeat(20);
  const failure = "Opening the pull request failed with additional diagnostic details. ".repeat(20);
  const daemon = new FakeDaemon({ goal: { root_id: "sup1", enabled: true, goal: "Improve the sidebar", note, goal_phase: "pr_open", pr_url: "https://github.com/example/project/pull/1", state: "running", progress: 0, milestones: [] } });
  render(<GoalPanel client={daemon.client()} repo={REPO} root="sup1" store={createAppStore()} compact openUrl={async () => { throw new Error(failure); }} />);
  const rendered = await screen.findByText(note.trim());
  expect(rendered.classList.contains("line-clamp-2")).toBe(true);
  expect(rendered.getAttribute("title")).toBe(note);
  fireEvent.click(screen.getByTestId("goal-pr"));
  const error = await screen.findByText(failure.trim());
  expect(error.classList.contains("line-clamp-2")).toBe(true);
  expect(error.getAttribute("title")).toBe(failure);
});

it.each([false, true])("keeps completed sidebar milestones visible with ticks (phases: %s)", async (phased) => {
  const daemon = new FakeDaemon({ goal: {
    root_id: "sup1", enabled: true, goal: "Improve the sidebar", state: "running", progress: 50,
    milestones: [
      { id: 1, position: 1, title: "Completed work", status: "passed", state: "passed", phase_name: phased ? "Build" : null },
      { id: 2, position: 2, title: "Current work", status: "running", state: "not_done", phase_name: phased ? "Review" : null },
    ],
  } });
  render(<GoalPanel client={daemon.client()} repo={REPO} root="sup1" store={createAppStore()} compact />);
  await screen.findByTestId("goal-line");
  const rows = screen.getAllByTestId("milestone");
  expect(rows.map((row) => row.dataset.state)).toEqual(["done", "now"]);
  expect(rows[0].querySelector(".i")?.textContent).toBe("✓");
  expect(screen.getByTestId("goal-count").tagName).toBe("SPAN");
});

it("bounds goal headings and keeps live and paused session names on one line", async () => {
  localStorage.clear();
  const goalText = "Deliver a detailed desktop improvement with milestones, project constraints, review requirements, and verification steps. ".repeat(8);
  const name = "A descriptive session name that must stay on one line ".repeat(8).trim();
  const live = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: goalText };
  const daemon = new FakeDaemon({
    repos: [{ repo_root: REPO, name: "proj" }],
    workspaces: [root],
    sessions: [live, { ...live, id: "named", name }, { ...live, id: "paused", name, status: "paused" }],
    goal: { root_id: "sup1", enabled: true, goal: goalText, state: "running", progress: 0, milestones: [] },
  });
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: daemon.fetch });
  const { store, sync } = connectDaemon(daemon, client);
  try {
    render(inTestContext(
      <SessionsProvider client={client}><UsageProvider client={client}>
        <Sidebar route={{ screen: "session", root: "sup1", repo: REPO }} navigate={vi.fn()} />
      </UsageProvider></SessionsProvider>,
      { client, store }, { daemon: true, providers: true },
    ));
    const heading = await screen.findByTestId("goal-line");
    const title = within(heading).getAllByRole("button")[0];
    expect(title.classList.contains("line-clamp-2")).toBe(true);
    expect(title.getAttribute("title")).toBe(goalText);
    const rows = await screen.findAllByTestId("session-title");
    for (const row of rows) {
      expect(row.classList.contains("truncate")).toBe(true);
      expect(row.getAttribute("title")).toBe(row.textContent);
    }
    fireEvent.click(screen.getByTestId("paused-toggle-proj"));
    await waitFor(() => expect(document.querySelector('[data-tree-session="paused"] .truncate')).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Rename session" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Session name" }), { target: { value: name } });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Session name" }), { key: "Enter" });
    await waitFor(() => {
      const renamed = within(heading).getAllByRole("button")[0];
      expect(renamed.classList.contains("truncate")).toBe(true);
      expect(renamed.getAttribute("title")).toBe(name);
    });
  } finally {
    sync.stop();
  }
});
