import { connectDaemon, inTestContext } from "../../test/harness";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FakeDaemon, flush } from "../../test/fakeDaemon";
import { REPO, root, worker } from "../../test/fixtures";
import { standInViews } from "../../test/panes";
import { SessionScreen } from "./SessionScreen";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

afterEach(cleanup);

describe("a practice run's session", () => {
  it("shows its steps over the terminal (Plan until a worker is handed work, then Build), and End practice run asks the engine to end it, then goes home", async () => {
    // The session's repo is brindle's practice repo: the demo's. The engine says "working" while the
    // supervisor reads and splits the goal, before any worker exists.
    const milestone = (id: number, title: string) => ({ id, position: id, title, status: "pending", state: "not_done" });
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "textkit-1", demo: true }],
      sessions: [{ id: "sup1", workspace_id: "proj", profile: "supervisor", status: "processing", mode: "interactive", created_at: 0, goal: "Finish the textkit helpers" }],
      workspaces: [root],
      goal: { root_id: "sup1", enabled: true, progress: 0, state: "running", stage: "working", goal: "Finish the textkit helpers", milestones: [milestone(1, "slugify works"), milestone(2, "wrap works")] },
    });
    const { client, store, sync } = connectDaemon(daemon);
    const onHome = vi.fn();
    render(inTestContext(<SessionScreen client={client} root="sup1" repo={REPO} refresh={sync.refresh} store={store} onHome={onHome} />, { client, store }, { daemon: true, sessions: true, views: standInViews }));
    const steps = await screen.findByRole("list", { name: "Practice run steps" });
    const states = () => within(steps).getAllByRole("listitem").map((s) => s.dataset.state);
    // Once the goal's answer is in, the strip still says Plan.
    await waitFor(() => expect(daemon.requests.some((r) => r.path === "/goal")).toBe(true));
    await act(() => flush());
    expect(states()).toEqual(["on", "todo", "todo", "todo"]);
    // The supervisor hands out the first milestone: a worker on its own branch.
    daemon.data.workspaces = [root, worker];
    await act(() => sync.refresh());
    await waitFor(() => expect(states()).toEqual(["ok", "on", "todo", "todo"]));
    fireEvent.click(within(screen.getByTestId("session-bar")).getByRole("button", { name: "End practice run" }));
    await waitFor(() => expect(onHome).toHaveBeenCalled());
    expect(daemon.requests.filter((r) => r.method === "DELETE").map((r) => r.path)).toEqual(["/demo"]);
    sync.stop();
  });
});
