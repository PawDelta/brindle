import { inTestStore } from "../../../test/harness";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { EditorView } from "@codemirror/view";
import { afterEach, expect, it } from "vitest";
import { DaemonError, type DaemonClient, type LibraryPerson, type Plan, type VisualDTO } from "../../../api/client";
import { DaemonContext } from "../../../api/context";
import { createAppStore } from "../../../store/store";
import { StoreContext } from "../../../store/useStore";
import { changeBetween } from "./DocText";
import { PlanReader, REVIEW_COLLAPSED_KEY } from "./PlanReader";
import type { PlanDocClient, PlansClient, VisualsClient } from "./client";

afterEach(() => {
  cleanup();
  localStorage.removeItem(REVIEW_COLLAPSED_KEY);
});

/* Who reviews the doc, and sharing it, open over the page from the toolbar's Share. */
const openShare = async () => fireEvent.click(await screen.findByRole("button", { name: "Share" }));

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
  const view = render(inTestStore(<><PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} /></>, store));
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

it("asks immediately in the thread without turning the question into a change request", async () => {
  let plan = doc(1, "Workers run on your machine.");
  const client: PlansClient & PlanDocClient & VisualsClient = {
    ...visuals, ...docWrites(() => plan), plan: async () => plan, decidePlan: async () => plan,
    commentPlan: async (_id, rev, anchor, text, question) => (plan = { ...plan, threads: [
      { id: 1, kind: "comment", rev, anchor: { ...anchor, state: "current" }, author: "person", text,
        question, status: "open", at: 1, replies: [
          { id: 2, author: "person", text: "On your own machine.", at: 2, from_supervisor: true },
        ] },
    ] }),
  };
  render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  const text = (await screen.findByText("Workers run on your machine.")).firstChild!;
  const range = document.createRange();
  range.setStart(text, 0);
  range.setEnd(text, 7);
  window.getSelection()!.removeAllRanges();
  window.getSelection()!.addRange(range);
  fireEvent.mouseUp(screen.getByTestId("plan-doc"));
  fireEvent.click(within(screen.getByRole("toolbar")).getByRole("button", { name: "Ask the supervisor" }));
  fireEvent.change(within(screen.getByTestId("thread-draft")).getByRole("textbox"), { target: { value: "Where?" } });
  await act(async () => fireEvent.submit(screen.getByTestId("thread-draft")));
  const thread = screen.getByTestId("thread-1");
  expect(thread.dataset.question).toBe("true");
  expect(thread.querySelector('[data-from-supervisor="true"]')).toBeTruthy();
  expect((screen.getByRole("button", { name: "Request changes" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Sign off" }) as HTMLButtonElement).disabled).toBe(false);
});

it("design doc reader", async () => {
  // An emoji leads each section: the engine's offsets count it as one character, the screen as two.
  const summary = "🚀 Workers run on a **machine** the customer controls.";
  let plan: Plan = { id: visual.id, session: "boss", title: "Cloud workers", rev: 1, state: "open", markdown: "…", lede: "", queued: 0, created_at: 1, split: [], reference: "DES-0042",
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
  // Its reference sits beside the title.
  expect(within(screen.getByRole("heading", { level: 1 })).getByTestId("doc-reference").textContent).toBe("DES-0042");
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
  // is the new revision's and the card moves under Resolved.
  expect(within(screen.getByTestId("thread-1")).getByTestId("suggestion-change").querySelectorAll("del, ins").length).toBe(2);
  expect(api.querySelector("del")?.textContent).toBe('"token"');
  expect(api.querySelector("ins")?.textContent).toBe('"lease"');
  await act(async () => fireEvent.click(within(screen.getByTestId("thread-1")).getByRole("button", { name: "Accept" })));
  expect(calls.pop()).toEqual(["accept", 1, 1]);
  expect(api.querySelector("del, ins")).toBeNull();
  expect(api.textContent).toContain('"lease"');
  expect(within(screen.getByTestId("plan-resolved")).getByTestId("thread-1").getAttribute("data-status")).toBe("accepted");

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
  fireEvent.click(screen.getByRole("tab", { name: /Decisions/ }));
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
  // The engine marks a reply `sent` once a verdict has carried it.
  const delivered = (p: Plan): Plan => ({ ...p, threads: p.threads!.map((t) => ({ ...t, replies: t.replies?.map((r) => ({ ...r, sent: true })) })) });
  const redirected: Plan = { ...delivered(replied), state: "redirected", verdict: { approved: false, at: 13, rev: 1 } };
  let decided!: (p: Plan) => void;
  const client: PlansClient & PlanDocClient & VisualsClient = {
    ...visuals, ...docWrites(() => replied),
    plan: () => new Promise<Plan>((resolve) => reads.push(resolve)),
    // The first verdict is answered at once; the second, when the test says so.
    decidePlan: (_id, verdict) => (sent.push(verdict.approved) === 1 ? Promise.resolve(redirected) : new Promise<Plan>((resolve) => { decided = resolve; })),
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
  const again = [...redirected.threads![0].replies!, { id: 3, author: "person", text: "And spot instances.", at: 14 }];
  fireEvent.click(within(screen.getByTestId("thread-1")).getByRole("button", { name: "Reply" }));
  fireEvent.change(within(screen.getByTestId("thread-1")).getByRole("textbox"), { target: { value: "And spot instances." } });
  fireEvent.click(within(screen.getByTestId("thread-1")).getByRole("button", { name: "Send" }));
  await act(async () => reply({ ...redirected, threads: [{ ...redirected.threads![0], replies: again }] }));
  view.unmount();
  const second: Plan = { ...base, rev: 2, revisions: [...base.revisions!, { rev: 2, author: "supervisor", source: "proposal", at: 20 }], threads: [{ ...base.threads![0], replies: again }] };
  const reopened = render(reader({ ...visual, rev: 2 }));
  await act(async () => reads[reads.length - 1](second));
  await act(async () => fireEvent.click(bar().getByRole("button", { name: "Request changes" })));
  expect(sent).toEqual([false, false]);
  // The reader is closed before that verdict's answer arrives. The verdict still delivered the
  // reply, which the engine now reports as sent: opened again on revision 3, nothing is left to send.
  reopened.unmount();
  await act(async () => decided({ ...delivered(second), state: "redirected", verdict: { approved: false, at: 21, rev: 2 } }));
  const third = render(reader({ ...visual, rev: 3 }));
  const revised: Plan = { ...delivered(second), rev: 3, revisions: [...second.revisions!, { rev: 3, author: "supervisor" as const, source: "proposal", at: 30 }] };
  await act(async () => reads[reads.length - 1](revised));
  expect((bar().getByRole("button", { name: "Request changes" }) as HTMLButtonElement).disabled).toBe(true);
  // Another device wrote a reply this one never saw, older than the supervisor's latest revision:
  // still feedback to send here, until a delivery from elsewhere marks it sent.
  third.unmount();
  const elsewhere = (sent: boolean): Plan => ({ ...revised, threads: [{ ...revised.threads![0], replies: [{ id: 9, author: "person", text: "From my laptop.", at: 5, sent }] }] });
  const fresh = render(reader({ ...visual, rev: 3 }));
  await act(async () => reads[reads.length - 1](elsewhere(false)));
  expect((bar().getByRole("button", { name: "Request changes" }) as HTMLButtonElement).disabled).toBe(false);
  fresh.rerender(reader({ ...visual, rev: 3, answer: { option: "x", note: "", at: 6 } }));
  await act(async () => reads[reads.length - 1](elsewhere(true)));
  expect((bar().getByRole("button", { name: "Request changes" }) as HTMLButtonElement).disabled).toBe(true);
});

it("a design doc Request changes the engine refuses as nothing to send shows the refusal, stops counting the reply it was delivered elsewhere, and keeps a newer one", async () => {
  const at = (replies: { id: number; at: number; sent: boolean }[]): Plan => doc(2, "Workers run on your machine.", {
    revisions: [{ rev: 1, author: "supervisor", source: "proposal", at: 10 }],
    threads: [{ id: 1, kind: "comment", rev: 1, author: "supervisor", text: "Is a rented VM fine?", status: "open", at: 11, anchor: { kind: "section", section: "summary", state: "current" },
      replies: replies.map((r) => ({ ...r, author: "person", text: `reply ${r.id}` })) }] });
  // This device sees reply 2 as unsent; a verdict from another window had already delivered it.
  let plan = at([{ id: 2, at: 12, sent: false }]);
  const client: PlansClient & PlanDocClient & VisualsClient = {
    ...visuals, ...docWrites(() => plan), plan: async () => plan,
    decidePlan: async () => {
      plan = at([{ id: 2, at: 12, sent: true }]);
      throw new DaemonError("invalid", "say what to change: a comment, a note or a different pick", 400);
    },
    replyPlan: async () => (plan = at([{ id: 2, at: 12, sent: true }, { id: 3, at: 25, sent: false }])),
  };
  render(<PlanReader client={client} visual={{ ...visual, rev: 2 }} onBack={() => {}} onChanged={() => {}} />);
  const request = async () => (await screen.findByRole("button", { name: "Request changes" })) as HTMLButtonElement;
  expect((await request()).disabled).toBe(false);
  await act(async () => fireEvent.click(await request()));
  // The refusal is shown, and the read that follows reports reply 2 delivered: nothing left to send.
  expect(screen.getByRole("alert")).toBeTruthy();
  expect((await request()).disabled).toBe(true);
  // A reply written after that is still feedback to send.
  fireEvent.click(within(await screen.findByTestId("thread-1")).getByRole("button", { name: "Reply" }));
  fireEvent.change(within(screen.getByTestId("thread-1")).getByRole("textbox"), { target: { value: "reply 3" } });
  await act(async () => fireEvent.click(within(screen.getByTestId("thread-1")).getByRole("button", { name: "Send" })));
  expect((await request()).disabled).toBe(false);
});

it("a design doc's build button follows the gate: reviewers listed, disabled with the reason, enabled when ready", async () => {
  const row = (sub: string, status: "signed_off" | "waiting", required = true) => ({ sub, required, status });
  let plan = doc(1, "Workers run on your machine.", { gate: { state: "waiting", reason: "Waiting on u_a (1 of 2 required sign-offs).", required: 2, signed: 1, waiting_on: ["u_a"],
    reviewers: [row("u_b", "signed_off"), row("u_a", "waiting"), row("u_c", "waiting", false)] } });
  const decided: boolean[] = [];
  const client: PlansClient & PlanDocClient & VisualsClient = { ...visuals, ...docWrites(() => plan), plan: async () => plan,
    decidePlan: async (_id, verdict) => { decided.push(verdict.approved); return plan; } };
  const reader = (v: VisualDTO) => <PlanReader client={client} visual={v} onBack={() => {}} onChanged={() => {}} />;
  const view = render(reader(visual));
  const build = (await screen.findByTestId("plan-build")) as HTMLButtonElement;
  expect(build.disabled).toBe(true);
  expect(build.title).toContain("u_a");
  await openShare();
  expect(screen.getByTestId("plan-reviewer-u_a").dataset.status).toBe("waiting");
  expect(screen.getByTestId("plan-reviewer-u_c").dataset.required).toBe("false");

  plan = { ...plan, gate: { ...plan.gate!, state: "ready", reason: null, signed: 2, waiting_on: [] } };
  await act(async () => view.rerender(reader({ ...visual, answer: { option: "x", note: "", at: 5 } })));
  expect(screen.getByTestId("plan-build").dataset.gate).toBe("ready");
  await act(async () => fireEvent.click(screen.getByTestId("plan-build")));
  expect(decided).toEqual([true]);
});

it("a design doc whose Team plan ended says so, once, and only then", async () => {
  const open = (plan: Plan) => render(<PlanReader client={{ ...visuals, ...docWrites(() => plan), plan: async () => plan, decidePlan: async () => plan }} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  const shared = open(doc(1, "Workers run on your machine."));
  await screen.findByTestId("plan-build");
  expect(screen.queryByTestId("plan-team-ended")).toBeNull();
  shared.unmount();
  open(doc(1, "Workers run on your machine.", { team_ended: true }));
  expect(await screen.findAllByTestId("plan-team-ended")).toHaveLength(1);
});

it("a doc the Library can't confirm says why in plain words, with Sign in, and Stop sharing goes on to build", async () => {
  const said = "The engine's plain words for why.";
  const plan = doc(1, "Workers run on your machine.", { gate: { state: "ready", reason: null, required: 0, signed: 0, waiting_on: [], reviewers: [],
    unconfirmed: { reason: "sign_in", message: said, approval_held: true, can_stop_sharing: true } } });
  const calls: string[] = [];
  const client = { ...visuals, ...docWrites(() => plan), plan: async () => plan, decidePlan: async (_id: string, verdict: { approved: boolean }) => { calls.push(`decide ${verdict.approved}`); return plan; },
    sharePlan: async () => plan, setPlanReviewers: async () => plan, syncPlan: async () => plan, signOffPlan: async () => plan, overridePlanPolicy: async () => plan, docScopes: async () => ({ scopes: [] }), libraryPeople: async () => ({ people: [] }),
    stopSharingPlan: async () => { calls.push("stop"); return plan; } };
  const daemon = { accountLogin: async () => { calls.push("login"); return { job: "j1" }; } } as unknown as DaemonClient;
  render(<DaemonContext.Provider value={daemon}><StoreContext.Provider value={createAppStore()}><PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} /></StoreContext.Provider></DaemonContext.Provider>);
  const why = await screen.findByTestId("plan-unconfirmed");
  expect(why.dataset.reason).toBe("sign_in");
  expect(why.dataset.held).toBe("true");
  expect(why.textContent).toContain(said);
  await act(async () => fireEvent.click(within(why).getByRole("button", { name: "Sign in" })));
  await act(async () => fireEvent.click(within(why).getByRole("button", { name: "Stop sharing and build" })));
  expect(calls).toEqual(["login", "stop", "decide true"]);
});

/* A design doc held by one org rule, "security", and a fake Library whose answers carry what was written. */
type Override = { policy: string; reason: string; stand_in?: string; until?: number };
const policyRow = (name: string, more = {}) => ({ org_id: "org_1", policy: name, name, approvals: 1, eligible: 6, needs_kind: "team", needs_name: "Security", when: "paths", matched: ["billing/meter.py"], status: "waiting" as "waiting" | "signed_off" | "waived", ...more });
const policyDoc = (rev: number, security = {}, others: ReturnType<typeof policyRow>[] = []) => doc(rev, "Workers run on your machine.", { gate: { state: "waiting", reason: "Waiting on security (1 of 2 required sign-offs).", required: 2, signed: 1, waiting_on: ["security"],
  reviewers: [{ sub: "u_b", required: true, status: "signed_off" }, { sub: null, name: "security", required: true, status: "waiting" }], policies: [policyRow("security", security), ...others] } });
const policyReader = (now: () => Plan, library: { admin_org: string | null; me?: string; people?: LibraryPerson[] }, override: (body: Override) => Plan = now, signOff: () => Plan = now) => {
  const client = { ...visuals, ...docWrites(now), plan: async () => now(), decidePlan: async () => now(),
    sharePlan: async () => now(), setPlanReviewers: async () => now(), syncPlan: async () => now(), docScopes: async () => ({ scopes: [] }),
    libraryPeople: async () => ({ people: [], ...library }), signOffPlan: async () => signOff(), overridePlanPolicy: async (_id: string, body: Override) => override(body) };
  return (v: VisualDTO) => <PlanReader client={client} visual={v} onBack={() => {}} onChanged={() => {}} />;
};

it("org policies are locked rows that show a waiver, and only an admin of that org is offered an override", async () => {
  const waived = policyRow("staff", { status: "waived", waiver: { by: "dana", reason: "Offsite" } });
  let plan = policyDoc(1, {}, [waived]);
  const waive = (body: Override) => (plan = policyDoc(1, { status: "waived", waiver: { by: "you", reason: body.reason } }, [waived]));

  const member = render(policyReader(() => plan, { admin_org: null, people: [{ sub: "sam", email: "sam@x" }] }, waive)(visual));
  await openShare();
  const row = await screen.findByTestId("plan-policy-security");
  expect(screen.getByTestId("plan-status").dataset.held).toBe("policy");
  expect(screen.queryByTestId("plan-reviewer-security")).toBeNull();
  expect(screen.getByTestId("plan-policy-staff").dataset.status).toBe("waived");
  expect(screen.getByTestId("plan-policy-staff").textContent).toContain("Offsite");
  await act(async () => {});
  expect(within(row).queryByRole("button", { name: "Waive once" })).toBeNull();
  member.unmount();

  render(policyReader(() => plan, { admin_org: "org_1", people: [{ sub: "sam", email: "sam@x" }] }, waive)(visual));
  await openShare();
  const adminRow = await screen.findByTestId("plan-policy-security");
  await within(adminRow).findByRole("button", { name: "Waive once" });
  expect(within(screen.getByTestId("plan-policy-staff")).queryByRole("button", { name: "Waive once" })).toBeNull();
  fireEvent.click(within(adminRow).getByRole("button", { name: "Waive once" }));
  fireEvent.change(within(adminRow).getByRole("textbox", { name: "Reason" }), { target: { value: "Pen test on hold" } });
  await act(async () => fireEvent.click(within(screen.getByTestId("plan-policy-override-security")).getByRole("button", { name: "Waive once" })));
  expect(screen.getByTestId("plan-policy-security").dataset.status).toBe("waived");
  expect(screen.getByTestId("plan-policy-security").textContent).toContain("Pen test on hold");
});

it("a waiver begun on one revision can't be sent over a newer one", async () => {
  let plan = policyDoc(1);
  let sent = 0;
  const reader = policyReader(() => plan, { admin_org: "org_1" }, () => { sent += 1; return plan; });
  const view = render(reader(visual));
  await openShare();
  const row = await screen.findByTestId("plan-policy-security");
  fireEvent.click(await within(row).findByRole("button", { name: "Waive once" }));
  fireEvent.change(within(row).getByRole("textbox", { name: "Reason" }), { target: { value: "Fine as it stood" } });

  plan = policyDoc(2);
  await act(async () => view.rerender(reader({ ...visual, rev: 2 })));
  expect(screen.getByTestId("plan-rev").textContent).toBe("rev 2");
  const draft = screen.getByTestId("plan-policy-override-security");
  expect(draft.dataset.stale).toBe("true");
  await act(async () => fireEvent.click(within(draft).getByRole("button", { name: "Waive once" })));
  expect(sent).toBe(0);
});

it("an admin can name themself as the only stand-in, and the rule waits until they sign", async () => {
  const me = "user_1";
  let plan = policyDoc(1);
  const reader = policyReader(() => plan, { admin_org: "org_1", me },
    (body) => (plan = policyDoc(1, { stand_ins: [{ stand_in: body.stand_in!, until: body.until! }] })),
    () => (plan = policyDoc(1, { status: "signed_off", met_by: [me], stand_ins: [{ stand_in: me, until: 4_000_000_000 }] })));
  render(reader(visual));
  await openShare();
  const row = await screen.findByTestId("plan-policy-security");
  fireEvent.click(await within(row).findByRole("button", { name: "Name a stand-in" }));
  fireEvent.change(within(row).getByRole("combobox", { name: "Stand-in" }), { target: { value: me } });
  fireEvent.change(within(row).getByLabelText("Stands in until"), { target: { value: "2096-01-01" } });
  fireEvent.change(within(row).getByRole("textbox", { name: "Reason" }), { target: { value: "Alex is away" } });
  await act(async () => fireEvent.click(within(screen.getByTestId("plan-policy-override-security")).getByRole("button", { name: "Name stand-in" })));
  expect(screen.getByTestId("plan-policy-security").dataset.status).toBe("waiting");

  await act(async () => fireEvent.click(within(screen.getByTestId("plan-policy-security")).getByRole("button", { name: "Sign off" })));
  expect(screen.getByTestId("plan-policy-security").dataset.status).toBe("signed_off");
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

it("the policies shown before a first share are those of the revision on screen, whatever answers late", async () => {
  let now = doc(1, "Workers run on your machine.");
  const asked: { rev: number; reply(rows: string[]): void }[] = [];
  const row = (policy: string) => ({ org_id: "org_1", policy, name: policy, note: null, when: "paths", label: null, paths: [], matched: ["x"], needs_kind: "team", needs_name: "Security", approvals: 1, eligible: 6 });
  const client = { ...visuals, ...docWrites(() => now), plan: async () => now, decidePlan: async () => now,
    sharePlan: async () => now, setPlanReviewers: async () => now, syncPlan: async () => now, signOffPlan: async () => now, overridePlanPolicy: async () => now,
    docScopes: async () => ({ scopes: [{ kind: "group", id: "g", name: "Core", can_share: true }] }), libraryPeople: async () => ({ people: [] }),
    previewPlanPolicies: () => new Promise<{ policies: ReturnType<typeof row>[] }>((resolve) => { asked.push({ rev: now.rev, reply: (names) => resolve({ policies: names.map(row) }) }); }) };
  const reader = (v: VisualDTO) => <PlanReader client={client} visual={v} onBack={() => {}} onChanged={() => {}} />;
  const view = render(reader(visual));
  await openShare();
  fireEvent.click(await screen.findByRole("button", { name: "Share for review" }));
  await waitFor(() => expect(asked).toHaveLength(1));

  now = doc(2, "Workers run on a machine the customer controls.");
  await act(async () => view.rerender(reader({ ...visual, rev: 2 })));
  await waitFor(() => expect(asked).toHaveLength(2));
  await act(async () => asked[0].reply(["stale-rule"]));
  expect(screen.queryByTestId("plan-policy-preview")).toBeNull();
  await act(async () => asked[1].reply(["fresh-rule"]));
  expect(screen.getByTestId("plan-policy-preview-fresh-rule")).toBeTruthy();
  expect(screen.queryByTestId("plan-policy-preview-stale-rule")).toBeNull();
});

it("types a suggestion on the page as a tracked change, formats it there, and sends it with the card's reason", async () => {
  const plan = doc(1, "Workers run on your machine.");
  const sent: unknown[] = [];
  const client: PlansClient & PlanDocClient & VisualsClient = { ...visuals, ...docWrites(() => plan), plan: async () => plan, decidePlan: async () => plan,
    suggestPlan: async (_id, rev, change, text) => { sent.push([rev, change, text]); return plan; } };
  render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  const words = (await screen.findByText("Workers run on your machine.")).firstChild!;
  const range = document.createRange();
  range.setStart(words, 0);
  range.setEnd(words, 7);
  window.getSelection()!.removeAllRanges();
  window.getSelection()!.addRange(range);
  fireEvent.mouseUp(screen.getByTestId("plan-doc"));
  fireEvent.click(within(screen.getByRole("toolbar", { name: "For the highlighted text" })).getByRole("button", { name: "Suggest edit" }));
  // The section opens where it reads, the highlighted word selected: typing replaces it, tracked.
  const box = await screen.findByLabelText("Suggestion to Summary");
  const editor = EditorView.findFromDOM(box.closest<HTMLElement>(".cm-editor")!)!;
  await act(async () => editor.dispatch(editor.state.replaceSelection("Agents")));
  // Only what differs is tracked: the two words share their "s".
  expect(box.querySelector(".cm-sg-del")?.textContent).toBe("Worker");
  expect(box.querySelector(".cm-sg-ins")?.textContent).toBe("Agent");
  // The formatting toolbar writes into it: the new word made bold.
  await act(async () => editor.dispatch({ selection: { anchor: 0, head: 6 } }));
  fireEvent.focus(editor.contentDOM);
  await act(async () => fireEvent.click(within(screen.getByRole("toolbar", { name: "Formatting" })).getByRole("button", { name: "Bold" })));
  const card = screen.getByTestId("thread-draft");
  expect(within(card).getByTestId("suggestion-change").textContent).toBe("Workers → **Agents**");
  fireEvent.change(within(card).getByRole("textbox", { name: "Why (optional)" }), { target: { value: "They are agents now." } });
  await act(async () => fireEvent.click(within(screen.getByTestId("plan-verdict")).getByRole("button", { name: "Send 1 suggestion" })));
  expect(sent).toEqual([[1, { section: "summary", start: 0, before: "Workers", after: "**Agents**" }, "They are agents now."]]);
});
