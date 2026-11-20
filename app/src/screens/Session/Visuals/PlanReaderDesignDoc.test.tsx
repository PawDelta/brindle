import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { Plan, VisualDTO } from "../../../api/client";
import { PlanReader } from "./PlanReader";
import type { PlanDocClient, PlansClient, VisualsClient } from "./client";

afterEach(cleanup);

const visual: VisualDTO = { id: "v_plan", session: "boss", title: "Webhooks", by: "supervisor", kind: "plan", rev: 1, kept: false, unread: false, created_at: 1 };
const visuals: VisualsClient = {
  visuals: async () => [visual], answerVisual: async () => visual, dismissVisual: async () => visual,
  keepVisual: async () => visual, unkeepVisual: async () => visual, markVisualRead: async () => {},
};
const options = [{ id: "a", label: "A table", pros: ["No new service"], cons: ["Tops out early"] }, { id: "b", label: "A queue", pros: ["Fans out"], cons: ["One more service"] }];
/* A plan on the design-doc spine: Architecture sits inside Proposed design, and the third
   decision names no section, so it goes to Open questions. The engine sends no split; one is
   here anyway, to show the reader never draws it. */
const spine = (more: Partial<Plan> = {}): Plan => ({
  id: visual.id, session: "boss", title: "Webhook delivery", rev: 1, state: "open", markdown: "…", lede: "Deliver signed events.", queued: 0, created_at: 1,
  sections: [
    { id: "context", title: "Context", markdown: "Teams poll today." },
    { id: "goals", title: "Goals and non-goals", markdown: "No event is lost." },
    { id: "proposed-design", title: "Proposed design", markdown: "An outbox row per event." },
    { id: "architecture", title: "Architecture", markdown: "The dispatcher signs each payload." },
    { id: "risks", title: "Risks and what's hard to undo", markdown: "The payload shape." },
    { id: "open-questions", title: "Open questions", markdown: "None." },
  ],
  decisions: [
    { id: "store", title: "Where should delivery state live?", section: "proposed-design", mine: "a", challenge: "Write locks could stall delivery.", options },
    { id: "loud", title: "How should failures show up?", section: "architecture", mine: "a", options },
    { id: "reach", title: "How should it reach teams?", options },
  ],
  split: [{ id: "outbox", title: "Outbox", files: ["src/outbox.py"], test: "pytest outbox", state: "queued", test_state: "not run" }],
  threads: [{ id: 1, kind: "comment", rev: 1, author: "person", text: "Retry-After?", status: "open", at: 1, anchor: { kind: "text", section: "proposed-design", start: 3, end: 9, quote: "outbox", state: "current" } }],
  ...more,
});
const writes = (plan: Plan): PlanDocClient => ({
  commentPlan: async () => plan, replyPlan: async () => plan, resolvePlanThread: async () => plan, suggestPlan: async () => plan,
  acceptPlanSuggestion: async () => plan, rejectPlanSuggestion: async () => plan, editPlan: async () => plan, confirmPlan: async () => plan,
});

it("reads a spine plan as the planning doc: the outline marks open decisions, each decision sits in its section, and Approve waits for every pick", async () => {
  const plan = spine();
  const verdicts: unknown[] = [];
  const client: PlansClient & PlanDocClient & VisualsClient = { ...visuals, ...writes(plan), plan: async () => plan, decidePlan: async (_id, verdict) => { verdicts.push(verdict); return plan; } };
  render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  const outline = await screen.findByTestId("plan-outline");

  // Numbered spine sections only (Architecture is inside Proposed design), each decision under its section.
  expect(within(outline).getAllByTestId("outline-section").map((a) => a.dataset.section)).toEqual(["context", "goals", "proposed-design", "risks", "open-questions"]);
  const rows = () => within(outline).getAllByTestId("outline-decision");
  expect(rows().map((a) => [a.dataset.decision, a.dataset.state])).toEqual([["store", "open"], ["loud", "open"], ["reach", "open"]]);

  // Inline at its anchored section, a sub-section's inside that sub-section, an unanchored one in Open questions.
  const doc = screen.getByTestId("plan-doc");
  const inRegion = (name: string) => within(doc).getByRole("region", { name }).querySelectorAll<HTMLElement>("[data-testid=plan-decision]");
  expect([...inRegion("Proposed design")].map((d) => d.dataset.decision)).toEqual(["store", "loud"]);
  expect([...inRegion("Architecture")].map((d) => d.dataset.decision)).toEqual(["loud"]);
  expect([...inRegion("Open questions")].map((d) => d.dataset.decision)).toEqual(["reach"]);
  // A comment sits in the margin of the section it is on.
  expect(within(within(doc).getByRole("region", { name: "Proposed design" })).getByTestId("thread-1")).toBeTruthy();

  // The recommendation is not a pick: Approve stays off until every decision has the person's.
  const approve = () => screen.getByTestId("plan-build") as HTMLButtonElement;
  const card = (id: string) => doc.querySelector<HTMLElement>(`[data-testid=plan-decision][data-decision=${id}]`)!;
  expect(approve().disabled).toBe(true);
  expect(screen.getByTestId("plan-next").dataset.left).toBe("3");
  fireEvent.click(within(card("store")).getAllByRole("radio")[1]);
  fireEvent.click(within(card("loud")).getAllByRole("radio")[0]);
  expect(card("store").dataset.state).toBe("decided");
  expect(rows().map((a) => a.dataset.state)).toEqual(["decided", "decided", "open"]);
  expect(approve().disabled).toBe(true);
  fireEvent.click(within(card("reach")).getAllByRole("radio")[0]);
  expect(screen.getByTestId("plan-next").dataset.state).toBe("ready");
  expect(approve().disabled).toBe(false);
  await act(async () => fireEvent.click(approve()));
  expect(verdicts).toEqual([expect.objectContaining({ approved: true, picks: { store: "b", loud: "a", reach: "a" } })]);
  expect(screen.queryByTestId("task-outbox")).toBeNull();
});

it("shows no worker split to the person once the plan is approved", async () => {
  const plan = spine({ state: "approved", verdict: { approved: true, at: 2, rev: 1, picks: { store: "a", loud: "a", reach: "b" } } });
  const client: PlansClient & PlanDocClient & VisualsClient = { ...visuals, ...writes(plan), plan: async () => plan, decidePlan: async () => plan };
  render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  expect((await screen.findByTestId("plan-next")).dataset.state).toBe("approved");
  expect(screen.queryByRole("region", { name: "Plan board" })).toBeNull();
  expect(screen.queryByTestId("task-outbox")).toBeNull();
  expect(screen.queryByTestId("plan-board-progress")).toBeNull();
});
