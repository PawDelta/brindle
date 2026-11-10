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

it("plan reader layout", async () => {
  const visual: VisualDTO = { id: "v_plan", session: "boss", title: "Presets", by: "supervisor", kind: "plan", rev: 1, kept: false, unread: false, created_at: 1 };
  const plan: Plan = { id: visual.id, session: "boss", title: visual.title, rev: 1, state: "open", markdown: "…", ask: "Presets per org.", lede: "One worker.", sections: [], queued: 0, created_at: 1,
    decisions: [{ id: "where", title: "Where presets live", mine: "org", options: [
      { id: "org", label: "Org policy", pros: ["One place"], cons: ["Needs a migration"], touches: ["policy.py"] },
      { id: "repo", label: "A file in each repo", pros: ["No server change"], cons: ["Drifts"], touches: ["config.py"] },
    ] }],
    split: [{ id: "schema", title: "Schema", test: "pytest schema" }] };
  const client: PlansClient & VisualsClient = {
    visuals: async () => [visual], answerVisual: async () => visual, dismissVisual: async () => visual,
    keepVisual: async () => visual, unkeepVisual: async () => visual, markVisualRead: async () => {},
    plan: async () => plan, decidePlan: async () => plan,
  };
  render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  // A steps column: every step, and how many are settled as a bar at its foot.
  const steps = await screen.findByRole("navigation", { name: "Plan steps" });
  expect(within(steps).getAllByRole("button").length).toBe(4);
  const settled = () => within(steps).getByRole("progressbar").getAttribute("aria-valuenow");
  expect(within(steps).getByRole("progressbar").getAttribute("aria-valuemax")).toBe("3");
  expect(settled()).toBe("0");
  // A bottom answer bar holds the way on, outside the pane that scrolls.
  const bar = screen.getByTestId("plan-verdict");
  fireEvent.click(within(bar).getByRole("button", { name: /^Next/ }));
  expect(settled()).toBe("1");
  expect(within(screen.getByTestId("plan-step-pane")).queryByRole("button", { name: /Next|Back/ })).toBeNull();
  expect(within(bar).getByRole("button", { name: /Back/ })).toBeTruthy();
  expect(within(bar).getByRole("textbox")).toBeTruthy();
  // Option cards carry their trade-offs; what the picked one touches is a row under the cards.
  const cards = screen.getByRole("radiogroup");
  const picked = within(cards).getByRole("radio", { checked: true });
  expect(picked.querySelectorAll("[data-tradeoff='pro']").length).toBe(1);
  expect(picked.querySelectorAll("[data-tradeoff='con']").length).toBe(1);
  const touches = screen.getByTestId("touches-org");
  expect(cards.contains(touches)).toBe(false);
  expect(cards.compareDocumentPosition(touches) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(within(bar).getByTestId("plan-picked").textContent).toContain("Org policy");
  fireEvent.click(within(cards).getByRole("radio", { checked: false }));
  expect(within(bar).getByTestId("plan-picked").textContent).toContain("A file in each repo");
  expect(within(screen.getByTestId("touches-repo")).getByText("config.py")).toBeTruthy();
  expect(screen.queryByTestId("touches-org")).toBeNull();
  // Correct the ask, from the bar, opens the correction on the restated ask.
  fireEvent.click(within(bar).getByRole("button", { name: "Correct the ask" }));
  expect(within(screen.getByTestId("plan-ask")).getByRole("textbox")).toBeTruthy();
});
