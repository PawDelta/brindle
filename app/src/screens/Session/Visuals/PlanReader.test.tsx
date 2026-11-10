import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { Plan, VisualDTO } from "../../../api/client";
import { createAppStore } from "../../../store/store";
import { StoreContext } from "../../../store/useStore";
import { changeBetween } from "./DocText";
import { PlanReader, REVIEW_COLLAPSED_KEY, UNSENT_REPLIES_KEY } from "./PlanReader";
import type { PlanDocClient, PlansClient, VisualsClient } from "./client";

afterEach(() => {
  cleanup();
  localStorage.removeItem(REVIEW_COLLAPSED_KEY);
  localStorage.removeItem(UNSENT_REPLIES_KEY);
});

const visual: VisualDTO = { id: "v_plan", session: "boss", title: "Presets", by: "supervisor", kind: "plan", rev: 1, kept: false, unread: false, created_at: 1 };
const visuals: VisualsClient = {
  visuals: async () => [visual], answerVisual: async () => visual, dismissVisual: async () => visual,
  keepVisual: async () => visual, unkeepVisual: async () => visual, markVisualRead: async () => {},
};
/* The doc's writes, each answering with the plan `now` gives. */
const docWrites = (now: () => Plan): PlanDocClient => ({
  commentPlan: async () => now(), replyPlan: async () => now(), resolvePlanThread: async () => now(), suggestPlan: async () => now(),
  acceptPlanSuggestion: async () => now(), rejectPlanSuggestion: async () => now(), editPlan: async () => now(), confirmPlan: async () => now(),
});

it("keeps an approved plan board live from recorded worker events and drops stale reads", async () => {
  const plan: Plan = { id: visual.id, session: "boss", title: visual.title, rev: 1, state: "open", markdown: "## Goal\nPresets", lede: "Presets", sections: [], decisions: [], split: [
    { id: "schema", title: "Schema", test: "pytest schema", task_id: "t_schema", state: "queued", test_state: "not run" },
    { id: "ui", title: "UI", test: "vitest presets", task_id: "t_ui", state: "queued", test_state: "not run" },
    { id: "docs", title: "Docs", state: "queued", test_state: "not run" },
  ], queued: 2, created_at: 1 };
  const reads: ((p: Plan) => void)[] = [];
  let decided!: (p: Plan) => void;
  const client: PlansClient & PlanDocClient & VisualsClient = {
    ...visuals, ...docWrites(() => plan),
    plan: () => new Promise<Plan>((resolve) => reads.push(resolve)),
    decidePlan: () => new Promise<Plan>((resolve) => { decided = resolve; }),
  };
  const store = createAppStore();
  const view = render(<StoreContext.Provider value={store}><PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} /></StoreContext.Provider>);
  await act(async () => reads.shift()!(plan));
  expect(screen.queryByRole("region", { name: "Plan board" })).toBeNull();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Sign off" })));
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
  // The verdict's own reply is older than what the events brought: it changes nothing, and the
  // plan is read once more.
  await act(async () => decided({ ...plan, state: "approved" }));
  expect(screen.getByTestId("plan-board-progress").textContent).toContain("1 of 3 green");
  await act(async () => reads.shift()!({ ...approved, split: approved.split.map((w) => ({ ...w, state: "merged", test_state: "passing" })) }));
  expect(screen.getByTestId("plan-board-progress").textContent).toContain("3 of 3 green");
  expect(within(board()).getByTestId("task-schema").textContent).toContain("merged");
  view.unmount();
});

it("design doc reader", async () => {
  // An emoji leads each section: the engine's offsets count it as one character, the screen as two.
  const summary = "🚀 Workers run on a **machine** the customer controls.";
  let plan: Plan = { id: visual.id, session: "boss", title: "Cloud workers", rev: 1, state: "open", markdown: "…", lede: "", queued: 0, created_at: 1, split: [],
    sections: [{ id: "summary", title: "Summary", markdown: summary }, { id: "api", title: "API sketch", markdown: 'POST 🚀 with a "token" in the body.' }],
    decisions: [{ id: "where", title: "Where cloud workers run", mine: "own", options: [{ id: "own", label: "A · Your own server" }, { id: "hosted", label: "B · PawDelta-hosted" }] }],
    threads: [{ id: 1, kind: "suggestion", rev: 1, author: "supervisor", text: "Tokens end up in logs.", status: "open", at: 1, before: '"token"', after: '"lease"',
      anchor: { kind: "text", section: "api", start: 14, end: 21, quote: '"token"', state: "current" } }] };
  const calls: unknown[][] = [];
  const client: PlansClient & PlanDocClient & VisualsClient = {
    ...visuals, ...docWrites(() => plan),
    plan: async () => plan, decidePlan: async () => plan,
    commentPlan: async (_id, rev, anchor, text) => {
      calls.push(["comment", rev, anchor, text]);
      const end = anchor.kind === "text" ? anchor.start + anchor.quote.length : undefined;
      return (plan = { ...plan, threads: [...plan.threads!, { id: 2, kind: "comment", rev, author: "person", text, status: "open", at: 2, anchor: { ...anchor, end, state: "current" } }] });
    },
    acceptPlanSuggestion: async (_id, thread, rev) => {
      calls.push(["accept", thread, rev]);
      return (plan = { ...plan, rev: rev + 1, sections: plan.sections.map((s) => (s.id === "api" ? { ...s, markdown: s.markdown.replace('"token"', '"lease"') } : s)),
        threads: plan.threads!.map((t) => (t.id === thread ? { ...t, status: "accepted" } : t)) });
    },
    editPlan: async (_id, rev, change) => {
      calls.push(["edit", rev, change]);
      return (plan = { ...plan, rev: rev + 1, sections: plan.sections.map((s) => (s.id === change.section ? { ...s, markdown: s.markdown.replace(change.before, change.after) } : s)) });
    },
    confirmPlan: async (_id, rev, decision, option) => {
      calls.push(["confirm", rev, decision, option]);
      return (plan = { ...plan, decisions: plan.decisions.map((d) => ({ ...d, confirmed: option, votes: { [option]: ["person"] } })) });
    },
  };
  const open = () => render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  const view = open();
  // One scrolling doc: every section and the decision are on the page at once.
  const doc = await screen.findByTestId("plan-doc");
  const api = within(doc).getByRole("region", { name: "API sketch" });
  const decision = within(doc).getByRole("region", { name: /Where cloud workers run/ });
  expect(within(doc).getByRole("region", { name: "Summary" })).toBeTruthy();

  // A comment on a highlighted range: the highlight runs into bold text, and the comment is
  // anchored on that range of the section's own markdown.
  const range = document.createRange();
  range.setStart(within(doc).getByText(/Workers run on a/).firstChild!, 3);
  range.setEnd(within(doc).getByText("machine").firstChild!, 7);
  window.getSelection()!.removeAllRanges();
  window.getSelection()!.addRange(range);
  fireEvent.mouseUp(doc);
  fireEvent.click(within(screen.getByRole("toolbar")).getByRole("button", { name: "Comment" }));
  const draft = screen.getByTestId("thread-draft");
  fireEvent.change(within(draft).getByRole("textbox"), { target: { value: "Does this include a rented VM?" } });
  await act(async () => fireEvent.click(within(draft).getByRole("button", { name: "Comment" })));
  expect(calls.pop()).toEqual(["comment", 1, { kind: "text", section: "summary", start: 2, quote: "Workers run on a **machine" }, "Does this include a rented VM?"]);
  expect(screen.queryByTestId("thread-draft")).toBeNull();
  expect(screen.getByTestId("thread-2").getAttribute("data-kind")).toBe("comment");
  expect(doc.querySelectorAll('[data-thread="2"]').length).toBe(2);

  // A suggestion shows its before and after, on its card and in the text; accepted, the text
  // is the new revision's and the card moves to Resolved.
  expect(within(screen.getByTestId("thread-1")).getByTestId("suggestion-change").querySelectorAll("del, ins").length).toBe(2);
  expect(api.querySelector("del")?.textContent).toBe('"token"');
  expect(api.querySelector("ins")?.textContent).toBe('"lease"');
  await act(async () => fireEvent.click(within(screen.getByTestId("thread-1")).getByRole("button", { name: "Accept" })));
  expect(calls.pop()).toEqual(["accept", 1, 1]);
  expect(api.querySelector("del, ins")).toBeNull();
  expect(api.textContent).toContain('"lease"');
  expect(screen.queryByTestId("thread-1")).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: /Resolved/ }));
  expect(screen.getByTestId("thread-1").getAttribute("data-status")).toBe("accepted");

  // Editing: a section's text opens in its box, and saving sends only what changed, as a direct edit.
  fireEvent.click(screen.getByRole("radio", { name: "Editing" }));
  fireEvent.click(screen.getByRole("button", { name: "Edit Summary" }));
  fireEvent.change(screen.getByLabelText("Text of Summary"), { target: { value: summary.replace("customer", "team") } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
  expect(calls.pop()).toEqual(["edit", 2, { section: "summary", start: 35, before: "customer", after: "team" }]);
  expect(screen.queryByLabelText("Text of Summary")).toBeNull();

  // The column collapses to a strip that keeps both counts; the choice outlives the reader.
  fireEvent.click(screen.getByTestId("plan-review-collapse"));
  expect(screen.queryByTestId("plan-review")).toBeNull();
  view.unmount();
  open();
  const strip = await screen.findByTestId("plan-review-strip");
  expect(strip.querySelector('[data-count="comments"]')?.textContent).toBe("1");
  expect(strip.querySelector('[data-count="decisions"]')?.textContent).toBe("1");
  fireEvent.click(strip);
  expect(screen.queryByTestId("plan-review-strip")).toBeNull();
  expect(localStorage.getItem(REVIEW_COLLAPSED_KEY)).toBe("0");

  // The decision: the bottom bar leads to it, and confirms the option picked in its table.
  const section = () => within(screen.getByTestId("plan-doc")).getByRole("region", { name: /Where cloud workers run/ });
  expect(decision.getAttribute("data-next")).toBe("true");
  expect(within(screen.getByTestId("plan-todo")).getByRole("button").getAttribute("data-state")).toBe("current");
  fireEvent.click(within(section()).getByRole("radio", { name: "B · PawDelta-hosted" }));
  await act(async () => fireEvent.click(within(screen.getByTestId("plan-verdict")).getByRole("button", { name: "Confirm B" })));
  expect(calls.pop()).toEqual(["confirm", 3, "where", "hosted"]);
  expect(section().getAttribute("data-next")).toBeNull();
  expect(within(section()).getByRole("radio", { name: "B · PawDelta-hosted" }).querySelector("[data-votes]")?.getAttribute("data-votes")).toBe("1");
  expect(within(screen.getByTestId("plan-todo")).getByRole("button").getAttribute("data-state")).toBe("done");
  expect(within(screen.getByTestId("plan-verdict")).getByRole("button", { name: "Sign off" })).toBeTruthy();
});

const doc = (rev: number, text: string, more: Partial<Plan> = {}): Plan => ({ id: visual.id, session: "boss", title: "Cloud workers", rev, state: "open", markdown: "…", lede: "", queued: 0, created_at: 1, split: [],
  sections: [{ id: "summary", title: "Summary", markdown: text }], decisions: [], threads: [], ...more });

it("design doc writes go one at a time, a read sent before one came back can't undo it, and a reply is feedback to send", async () => {
  const base = doc(1, "Workers run on your machine.", {
    decisions: [{ id: "where", title: "Where", mine: "own", options: [{ id: "own", label: "A · Own" }, { id: "hosted", label: "B · Hosted" }] }],
    revisions: [{ rev: 1, author: "supervisor", source: "proposal", at: 10 }],
    threads: [{ id: 1, kind: "comment", rev: 1, author: "supervisor", text: "Is a rented VM fine?", status: "open", at: 11, anchor: { kind: "section", section: "summary", state: "current" } }] });
  const replied: Plan = { ...base, threads: [{ ...base.threads![0], replies: [{ id: 2, author: "person", text: "Yes, rented too.", at: 12 }] }] };
  const reads: ((p: Plan) => void)[] = [];
  let reply!: (p: Plan) => void;
  const confirmed: string[] = [];
  const sent: boolean[] = [];
  const redirected: Plan = { ...replied, state: "redirected", verdict: { approved: false, at: 13, rev: 1 } };
  const client: PlansClient & PlanDocClient & VisualsClient = {
    ...visuals, ...docWrites(() => replied),
    plan: () => new Promise<Plan>((resolve) => reads.push(resolve)),
    decidePlan: async (_id, verdict) => { sent.push(verdict.approved); return redirected; },
    replyPlan: () => new Promise<Plan>((resolve) => { reply = resolve; }),
    confirmPlan: async (_id, _rev, decision) => { confirmed.push(decision); return replied; },
  };
  const reader = (v: VisualDTO) => <PlanReader client={client} visual={v} onBack={() => {}} onChanged={() => {}} />;
  const view = render(reader(visual));
  await act(async () => reads[0](base));
  const bar = () => within(screen.getByTestId("plan-verdict"));
  expect((bar().getByRole("button", { name: "Request changes" }) as HTMLButtonElement).disabled).toBe(true);
  // A read goes out (the session's row changed), then the person's reply.
  view.rerender(reader({ ...visual, answer: { option: "x", note: "", at: 5 } }));
  fireEvent.click(within(screen.getByTestId("thread-1")).getByRole("button", { name: "Reply" }));
  fireEvent.change(within(screen.getByTestId("thread-1")).getByRole("textbox"), { target: { value: "Yes, rented too." } });
  fireEvent.click(within(screen.getByTestId("thread-1")).getByRole("button", { name: "Send" }));
  // While the reply is out no other write goes: the engine's two answers could cross.
  fireEvent.click(bar().getByRole("button", { name: "Confirm A" }));
  expect(confirmed).toEqual([]);
  await act(async () => reply(replied));
  // The read sent before the reply came back lands after it, without the reply: it is dropped.
  await act(async () => reads[1](base));
  expect(screen.getByTestId("thread-1").textContent).toContain("Yes, rented too.");
  // The reply alone is something to send back to the supervisor.
  await act(async () => fireEvent.click(bar().getByRole("button", { name: "Request changes" })));
  expect(sent).toEqual([false]);
  // That verdict delivered the reply. One more, written while the supervisor revises, is not
  // delivered by the revision that follows: after the reader is closed and opened again on
  // revision 2, it is still feedback to send.
  const unsent = () => (JSON.parse(localStorage.getItem(UNSENT_REPLIES_KEY) ?? "{}") as Record<string, number[]>)[visual.id];
  expect(unsent()).toBeUndefined();
  const again = [...replied.threads![0].replies!, { id: 3, author: "person", text: "And spot instances.", at: 14 }];
  fireEvent.click(within(screen.getByTestId("thread-1")).getByRole("button", { name: "Reply" }));
  fireEvent.change(within(screen.getByTestId("thread-1")).getByRole("textbox"), { target: { value: "And spot instances." } });
  fireEvent.click(within(screen.getByTestId("thread-1")).getByRole("button", { name: "Send" }));
  await act(async () => reply({ ...redirected, threads: [{ ...redirected.threads![0], replies: again }] }));
  view.unmount();
  render(reader({ ...visual, rev: 2 }));
  await act(async () => reads[reads.length - 1]({ ...base, rev: 2, revisions: [...base.revisions!, { rev: 2, author: "supervisor", source: "proposal", at: 20 }], threads: [{ ...base.threads![0], replies: again }] }));
  await act(async () => fireEvent.click(bar().getByRole("button", { name: "Request changes" })));
  expect(sent).toEqual([false, false]);
});

it("a design doc edit or comment begun on older text isn't sent over a newer revision's", async () => {
  let now = doc(1, "Workers run on your machine.");
  const calls: string[] = [];
  const client: PlansClient & PlanDocClient & VisualsClient = {
    ...visuals, ...docWrites(() => now), plan: async () => now, decidePlan: async () => now,
    editPlan: async () => { calls.push("edit"); return now; },
    commentPlan: async () => { calls.push("comment"); return now; },
  };
  const reader = (v: VisualDTO) => <PlanReader client={client} visual={v} onBack={() => {}} onChanged={() => {}} />;
  const view = render(reader(visual));
  // On revision 1: a comment begun on a highlight, and the section opened for editing.
  const text = (await screen.findByText("Workers run on your machine.")).firstChild!;
  const range = document.createRange();
  range.setStart(text, 0);
  range.setEnd(text, 7);
  window.getSelection()!.removeAllRanges();
  window.getSelection()!.addRange(range);
  fireEvent.mouseUp(screen.getByTestId("plan-doc"));
  fireEvent.click(within(screen.getByRole("toolbar")).getByRole("button", { name: "Comment" }));
  fireEvent.change(within(screen.getByTestId("thread-draft")).getByRole("textbox"), { target: { value: "Whose?" } });
  fireEvent.click(screen.getByRole("radio", { name: "Editing" }));
  fireEvent.click(screen.getByRole("button", { name: "Edit Summary" }));
  fireEvent.change(screen.getByLabelText("Text of Summary"), { target: { value: "Workers run on my machine." } });
  // The supervisor's revision 2 rewrites the section under both.
  now = doc(2, "Workers run on a machine the customer controls.");
  await act(async () => view.rerender(reader({ ...visual, rev: 2 })));
  expect(screen.getByTestId("plan-rev").textContent).toBe("rev 2");
  expect(screen.getByTestId("plan-edit-box").getAttribute("data-stale")).toBe("true");
  expect(screen.getByTestId("thread-draft").getAttribute("data-stale")).toBe("true");
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
  await act(async () => fireEvent.submit(screen.getByTestId("thread-draft")));
  expect(calls).toEqual([]);
});

it.each([
  ["an insertion after an emoji is placed in the engine's code points", "🚀 abc", "🚀X abc", { start: 1, before: "", after: "X" }],
  ["two emoji alike in their first half are swapped whole", "a🚀b", "a🚁b", { start: 1, before: "🚀", after: "🚁" }],
  ["two emoji alike in their second half are swapped whole", "a😀", "a🈀", { start: 1, before: "😀", after: "🈀" }],
])("a design doc edit sends only what changed: %s", (_case, source, next, change) => {
  expect(changeBetween(source, next)).toEqual(change);
});
