import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { Plan, SharedDesign, VisualDTO } from "../../../api/client";
import { createAppStore } from "../../../store/store";
import { inTestStore } from "../../../test/harness";
import { PlanReader } from "./PlanReader";
import { SharedDesignReader } from "./SharedDesignReader";
import { SharedLinkReader } from "./SharedLinkReader";

afterEach(cleanup);

const plan: Plan = {
  id: "readers-plan", session: "boss", title: "Webhook delivery", rev: 1, state: "open",
  markdown: "", lede: "", queued: 0, created_at: 1, split: [],
  sections: [{ id: "rollout", title: "Rollout", markdown: "- Deliver signed events." }],
  decisions: [{ id: "store", title: "Where should delivery state live?", mine: "a", options: [{ id: "a", label: "A table" }, { id: "b", label: "A queue" }] }],
  threads: [{ id: 1, kind: "comment", rev: 1, author: "person", text: "Which queue?", status: "open", at: 1, anchor: { kind: "decision", decision: "store", state: "current" } }],
};
const visual: VisualDTO = { id: plan.id, session: plan.session, title: plan.title, by: "supervisor", kind: "plan", rev: 1, kept: false, unread: false, created_at: 1 };
const readerClient = {
  plan: async (id: string) => ({ ...plan, id }), decidePlan: async () => plan, commentPlan: async () => plan, replyPlan: async () => plan,
  resolvePlanThread: async () => plan, suggestPlan: async () => plan, acceptPlanSuggestion: async () => plan,
  rejectPlanSuggestion: async () => plan, editPlan: async () => plan, confirmPlan: async () => plan,
  addPlanAnswer: async () => plan, changePlanAnswer: async () => plan,
  visuals: async () => [visual], answerVisual: async () => visual, dismissVisual: async () => visual,
  keepVisual: async () => visual, unkeepVisual: async () => visual, markVisualRead: async () => {},
};
const teammateClient = (doc: SharedDesign) => ({
  sharedDesign: async () => doc, writeSharedDesign: async () => doc,
  libraryPeople: async () => ({ people: [{ sub: "u_author", display_name: "Dana Okafor", email: "dana@example.test" }] }),
});

it.each(["view access", "share link"])("shows an open decision without selecting its recommendation for %s", async (access) => {
  const doc = { ...plan, me: "u_reader", author: "teammate:u_author", can_comment: false };
  const open = (shown: SharedDesign) => render(access === "view access"
    ? <SharedDesignReader client={teammateClient(shown)} docId={shown.id} onBack={() => {}} />
    : <SharedLinkReader client={{ shareLinkDesign: async () => ({ ...shown, id: "link_reader" }) }}
        link={{ kind: "plan", token: "reader-token", org: null, team: null, plan: null }} onBack={() => {}} />);
  const radios = (decision: HTMLElement) => within(decision).getAllByRole<HTMLInputElement>("radio").map(({ checked, disabled }) => [checked, disabled]);
  const first = open(doc);
  let decision = await screen.findByTestId("plan-decision");
  expect(decision.dataset.state).toBe("open");
  expect(screen.getByTestId("outline-count").dataset.decided).toBe("0");
  expect(within(decision).getByTestId("plan-recommended")).toBeTruthy();
  expect(radios(decision)).toEqual([[false, true], [false, true]]);

  first.unmount();
  const teammateVote = open({ ...doc, decisions: doc.decisions.map((d) => ({ ...d, confirmed: "a", votes: { a: ["person"] } })) });
  decision = await screen.findByTestId("plan-decision");
  expect(decision.dataset.state).toBe("open");
  expect(radios(decision)).toEqual([[false, true], [false, true]]);
  expect(within(decision).getByTestId("plan-decision-status").dataset.state).toBe("waiting-on-author");
  expect(screen.getByTestId("outline-decision-marker").dataset.state).toBe("waiting-on-author");
  expect(within(decision).getAllByTestId("plan-option")[0].querySelector("[data-votes]")).toBeTruthy();

  teammateVote.unmount();
  open({ ...doc, decisions: doc.decisions.map((d) => ({ ...d, confirmed: "a", votes: { a: ["person"], b: [doc.author] } })) });
  decision = await screen.findByTestId("plan-decision");
  expect(decision.dataset.state).toBe("decided");
  expect(within(decision).getByTestId("plan-decision-status").dataset.state).toBe("author-chose");
  expect(radios(decision)).toEqual([[false, true], [true, true]]);
});

it("renders the author's approval for a teammate and omits session guidance", async () => {
  const doc = { ...plan, me: "u_reader", author: "teammate:u_author", can_comment: true,
    decisions: plan.decisions.map((d) => ({ ...d, confirmed: "b" })) };
  render(<SharedDesignReader client={teammateClient(doc)} docId={doc.id} onBack={() => {}} />);
  expect((await screen.findByTestId("plan-decision-approval")).dataset.approver).toBe("Dana");
  expect(screen.getByTestId("plan-rollout-approval").dataset.approver).toBe("Dana");
  expect(screen.queryByTestId("plan-rollout-session")).toBeNull();
});

it.each(["comment", "reply", "own answer", "request changes"] as const)("restores an unsent %s after leaving and reopening the plan", async (kind) => {
  let reader: ReturnType<typeof render>;
  const store = createAppStore();
  const open = (entry = visual, app = store) => render(inTestStore(<PlanReader client={readerClient} visual={entry} onBack={() => reader.unmount()} onChanged={() => {}} />, app));
  reader = open();
  const decision = await screen.findByTestId("plan-decision");
  const start = {
    comment: () => within(decision).getByRole("button", { name: "Comment" }),
    reply: () => within(screen.getByTestId("thread-1")).getByRole("button", { name: "Reply" }),
    "own answer": () => screen.getByTestId("plan-add-answer"),
    "request changes": () => screen.getByRole("button", { name: "Request changes" }),
  } as const;
  const box = {
    comment: () => within(screen.getByTestId("thread-draft")).getByRole("textbox") as HTMLTextAreaElement,
    reply: () => within(screen.getByTestId("thread-1")).getByRole<HTMLInputElement>("textbox"),
    "own answer": () => within(screen.getByTestId("plan-answer-field")).getByRole<HTMLInputElement>("textbox"),
    "request changes": () => screen.getByRole("textbox", { name: "Note for the supervisor" }) as HTMLTextAreaElement,
  } as const;
  fireEvent.click(start[kind]());
  const input = box[kind]();
  const text = `Unsent ${kind}`;
  fireEvent.change(input, { target: { value: text } });
  if (kind === "request changes") {
    fireEvent.click(screen.getByTestId("visual-back"));
  } else {
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.getByTestId("plan-reader")).toBeTruthy();
    expect(document.activeElement).not.toBe(input);
    fireEvent.keyDown(document.body, { key: "Escape" });
  }
  expect(screen.queryByTestId("plan-reader")).toBeNull();

  if (kind === "comment") {
    const other = open({ ...visual, id: "other-readers-plan" });
    await screen.findByTestId("plan-decision");
    expect(screen.queryByTestId("thread-draft")).toBeNull();
    other.unmount();

    const otherApp = open(visual, createAppStore());
    await screen.findByTestId("plan-decision");
    expect(screen.queryByTestId("thread-draft")).toBeNull();
    otherApp.unmount();
  }

  reader = open();
  await screen.findByTestId("plan-decision");
  expect(box[kind]().value).toBe(text);
});
