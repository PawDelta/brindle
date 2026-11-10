import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { Plan, VisualDTO } from "../../../api/client";
import { createAppStore } from "../../../store/store";
import { StoreContext } from "../../../store/useStore";
import { PlanReader } from "./PlanReader";
import type { PlansClient, VisualsClient } from "./client";

afterEach(cleanup);

it("keeps an approved plan board live from recorded worker events and drops stale reads", async () => {
  const visual: VisualDTO = { id: "v_plan", session: "boss", title: "Presets", by: "supervisor", kind: "plan", rev: 1, kept: false, unread: false, created_at: 1 };
  const plan: Plan = { id: visual.id, session: "boss", title: visual.title, rev: 1, state: "open", markdown: "## Goal\nPresets", lede: "Presets", sections: [], decisions: [], split: [
    { id: "schema", title: "Schema", test: "pytest schema", task_id: "t_schema", state: "queued", test_state: "not run" },
    { id: "ui", title: "UI", test: "vitest presets", task_id: "t_ui", state: "queued", test_state: "not run" },
    { id: "docs", title: "Docs", state: "queued", test_state: "not run" },
  ], queued: 2, created_at: 1 };
  const reads: ((p: Plan) => void)[] = [];
  let decided!: (p: Plan) => void;
  const client: PlansClient & VisualsClient = {
    visuals: async () => [visual], answerVisual: async () => visual, dismissVisual: async () => visual,
    keepVisual: async () => visual, unkeepVisual: async () => visual, markVisualRead: async () => {},
    plan: () => new Promise<Plan>((resolve) => reads.push(resolve)),
    decidePlan: () => new Promise<Plan>((resolve) => { decided = resolve; }),
  };
  const store = createAppStore();
  const view = render(<StoreContext.Provider value={store}><PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} /></StoreContext.Provider>);
  await act(async () => reads.shift()!(plan));
  expect(screen.queryByRole("region", { name: "Plan board" })).toBeNull();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Approve" })));
  const event = (id: number) => act(() => store.dispatch({ type: "stream", message: { type: "event", id, ts: id, kind: "agent.status", root_id: "boss", repo_root: null, agent_id: "a_schema", workspace_id: null, task_id: "t_schema", payload: {} } }));
  event(1);
  await act(async () => reads.shift()!({ ...plan, state: "approved" }));
  const board = () => screen.getByRole("region", { name: "Plan board" });
  expect(within(board()).getAllByText(/queued/).length).toBe(3);
  expect(within(board()).getByTestId("task-schema").querySelector("[data-test-state]")?.getAttribute("data-test-state")).toBe("not run");
  event(2);
  const older = reads.shift()!;
  event(3);
  const approved: Plan = { ...plan, state: "approved", split: plan.split.map((w, i) => ({ ...w, agent_id: i < 2 ? `a_${w.id}` : null, state: i === 0 ? "working" : i === 1 ? "in review" : "queued", test_state: i === 0 ? "failing" : i === 1 ? "passing" : "not run" })) };
  await act(async () => reads.shift()!(approved));
  expect(within(board()).getByTestId("task-schema").textContent).toContain("a_schema");
  expect(within(board()).getByTestId("task-ui").textContent).toContain("in review");
  expect(within(board()).getByTestId("task-schema").querySelector("[data-test-state]")?.className).toContain("text-fail");
  expect(screen.getByTestId("plan-board-progress").textContent).toContain("1 of 3 green");
  await act(async () => older({ ...plan, state: "approved" }));
  await act(async () => decided({ ...plan, state: "approved" }));
  expect(screen.getByTestId("plan-board-progress").textContent).toContain("1 of 3 green");
  event(4);
  await act(async () => reads.shift()!({ ...approved, split: approved.split.map((w) => ({ ...w, state: "merged", test_state: "passing" })) }));
  expect(screen.getByTestId("plan-board-progress").textContent).toContain("3 of 3 green");
  expect(within(board()).getByTestId("task-schema").textContent).toContain("merged");
  view.unmount();
});
