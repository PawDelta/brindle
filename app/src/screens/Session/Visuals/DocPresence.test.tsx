import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { LoroDoc } from "loro-crdt";
import { afterEach, expect, it, vi } from "vitest";
import { fromBase64, type Plan, type SharedDesign, type VisualDTO } from "../../../api/client";
import { FakeDaemon } from "../../../test/fakeDaemon";
import { DocEditor } from "./DocEditor";
import { DocPresence } from "./DocPresence";
import { SharedDesignReader } from "./SharedDesignReader";
import { PlanReader } from "./PlanReader";

afterEach(() => { cleanup(); vi.useRealTimers(); });

const settle = async () => {
  for (let i = 0; i < 10; i++) await act(async () => {});
};
const editor = (label: string) => EditorView.findFromDOM(screen.getByLabelText(label).closest<HTMLElement>(".cm-editor")!)!;

it("requires the author to freeze a teammate's document", async () => {
  const daemon = new FakeDaemon();
  daemon.seedLive("doc", { lede: "Text" }, { canFreeze: false });
  render(<DocPresence client={daemon.client()} visual="doc" isAuthor={false} name="Bo" />);
  await settle();
  const panel = screen.getByRole("region", { name: "Live document" });
  expect(panel.querySelector('[role="status"][data-state="author-required"]')).toBeTruthy();
  expect(within(panel).queryByRole("button")).toBeNull();
});

it("shows the author a Freeze the daemon refused and leaves the document editable", async () => {
  const daemon = new FakeDaemon();
  daemon.seedLive("doc", { lede: "Text" }, { canFreeze: false });
  render(<DocPresence client={daemon.client()} visual="doc" isAuthor name="Ann" />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "Freeze for sign-off" }));
  await settle();
  const panel = screen.getByRole("region", { name: "Live document" });
  expect(within(panel).getByRole("alert").dataset.state).toBe("freeze-refused");
  expect(panel.dataset.state).toBe("editing");
  expect(daemon.liveState("doc").lock).toBeNull();
});

it("preserves HTTP review and build controls when the document has no live text", async () => {
  const daemon = new FakeDaemon();
  const live = daemon.client();
  const visual: VisualDTO = { id: "http-doc", session: "boss", title: "Review", by: "supervisor", kind: "plan", rev: 7, kept: false, unread: false, created_at: 1 };
  const plan: Plan = { id: "http-doc", session: "boss", title: "Review", rev: 7, state: "open", markdown: "Text", lede: "Text", sections: [], decisions: [], split: [], queued: 0, created_at: 1,
    gate: { state: "ready", required: 1, signed: 1, waiting_on: [], reviewers: [{ sub: "Bo", you: true, required: true, status: "signed_off" }] } };
  const client = {
    visualLive: live.visualLive.bind(live), plan: async () => plan, decidePlan: async () => plan,
    visuals: async () => [visual], answerVisual: async () => visual, dismissVisual: async () => visual, keepVisual: async () => visual, unkeepVisual: async () => visual, markVisualRead: async () => {},
    commentPlan: async () => plan, replyPlan: async () => plan, resolvePlanThread: async () => plan, suggestPlan: async () => plan, acceptPlanSuggestion: async () => plan, rejectPlanSuggestion: async () => plan, editPlan: async () => plan, confirmPlan: async () => plan,
    sharePlan: async () => plan, setPlanReviewers: async () => plan, syncPlan: async () => plan, overridePlanPolicy: async () => plan, signOffPlan: async () => plan,
    docScopes: async () => ({ scopes: [] }), libraryPeople: async () => ({ people: [], me: "Bo" }),
  };
  const author = render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  let signedOff = true;
  const design = (): SharedDesign => ({ ...plan, me: "Bo", author: "Ann", signed_off: signedOff, can_sign_off: true });
  const reviewerClient = { visualLive: client.visualLive, libraryPeople: client.libraryPeople, sharedDesign: async () => design(), writeSharedDesign: async () => { signedOff = true; return design(); } };
  const reviewer = render(<SharedDesignReader client={reviewerClient} docId="http-doc" onBack={() => {}} />);
  await settle();
  fireEvent.click(within(author.container).getByRole("button", { name: "Share" }));
  expect(within(author.container).getByTestId("plan-reviewer-Bo").dataset.status).toBe("signed_off");
  expect((within(author.container).getByTestId("plan-build") as HTMLButtonElement).disabled).toBe(false);
  expect(within(reviewer.container).queryByTestId("plan-sign-off")).toBeNull();
  expect(screen.queryByRole("region", { name: "Live document" })).toBeNull();
  reviewer.unmount();
  signedOff = false;
  const unsigned = render(<SharedDesignReader client={reviewerClient} docId="http-doc" onBack={() => {}} />);
  await settle();
  const signOff = within(unsigned.container).getByTestId("plan-sign-off") as HTMLButtonElement;
  expect(signOff.disabled).toBe(false);
  fireEvent.click(signOff);
  await settle();
  expect(within(unsigned.container).queryByTestId("plan-sign-off")).toBeNull();
});

it("keeps two readers present without section editors and removes a departing reader", async () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  const daemon = new FakeDaemon();
  daemon.seedLive("doc", { lede: "Text" });
  const live = daemon.client();
  const reader = (me: string) => {
    const design: SharedDesign = { id: "doc", session: "", title: "Readers", rev: 1, state: "open", markdown: "Text", lede: "Text", sections: [], decisions: [], split: [], queued: 0, created_at: 1, me, author: "Ann" };
    const client = { visualLive: live.visualLive.bind(live), libraryPeople: async () => ({ people: [] }), sharedDesign: async () => design, writeSharedDesign: async () => design };
    return render(<SharedDesignReader client={client} docId="doc" onBack={() => {}} />);
  };
  const bo = reader("Bo");
  const cy = reader("Cy");
  await settle();
  const boPeers = within(bo.container).getByRole("list", { name: "Others in this document" });
  const cyPeers = within(cy.container).getByRole("list", { name: "Others in this document" });
  expect(boPeers.querySelector('[data-peer="Cy"]')).toBeTruthy();
  expect(cyPeers.querySelector('[data-peer="Bo"]')).toBeTruthy();
  expect(document.querySelector(".cm-editor")).toBeNull();
  await act(async () => void vi.advanceTimersByTime(45_000));
  await settle();
  const dee = reader("Dee");
  await settle();
  const deePeers = within(dee.container).getByRole("list", { name: "Others in this document" });
  expect(deePeers.querySelector('[data-peer="Bo"]')).toBeTruthy();
  expect(deePeers.querySelector('[data-peer="Cy"]')).toBeTruthy();
  await act(async () => void vi.advanceTimersByTime(1_000));
  dee.unmount();
  cy.unmount();
  await settle();
  expect(within(boPeers).queryByRole("listitem")).toBeNull();
});

it("withdraws the rendered review and build gate when a signed live version is reopened", async () => {
  const daemon = new FakeDaemon();
  const live = daemon.client();
  const visual: VisualDTO = { id: "doc", session: "boss", title: "Review", by: "supervisor", kind: "plan", rev: 7, kept: false, unread: false, created_at: 1 };
  const plan: Plan = { id: "doc", session: "boss", title: "Review", rev: 7, state: "open", markdown: "Text", lede: "Text", sections: [], decisions: [], split: [], queued: 0, created_at: 1,
    gate: { state: "waiting", required: 1, signed: 0, waiting_on: ["Bo"], reviewers: [{ sub: "Bo", you: true, required: true, status: "waiting" }] } };
  daemon.seedLive("doc", { lede: "Text" }, { plan, me: "Bo", author: "Ann" });
  const client = {
    visualLive: live.visualLive.bind(live), plan: live.plan.bind(live), decidePlan: live.decidePlan.bind(live),
    visuals: async () => [visual], answerVisual: async () => visual, dismissVisual: async () => visual, keepVisual: async () => visual, unkeepVisual: async () => visual, markVisualRead: async () => {},
    commentPlan: async () => plan, replyPlan: async () => plan, resolvePlanThread: async () => plan, suggestPlan: async () => plan, acceptPlanSuggestion: async () => plan, rejectPlanSuggestion: async () => plan, editPlan: async () => plan, confirmPlan: async () => plan,
    sharePlan: async () => plan, setPlanReviewers: async () => plan, syncPlan: live.syncPlan.bind(live), overridePlanPolicy: async () => plan,
    docScopes: async () => ({ scopes: [] }), libraryPeople: async () => ({ people: [], me: "Bo" }),
    signOffPlan: live.signOffPlan.bind(live),
  };
  const author = render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  const authorUI = within(author.container);
  await settle();
  fireEvent.click(authorUI.getByRole("button", { name: "Share" }));
  fireEvent.click(authorUI.getByRole("button", { name: "Freeze for sign-off" }));
  await settle();
  const firstVersion = daemon.liveState("doc").version!.id;
  fireEvent.click(within(authorUI.getByTestId("plan-reviewer-Bo")).getByRole("button", { name: "Sign off" }));
  await settle();
  expect(authorUI.getByTestId("plan-reviewer-Bo").dataset.status).toBe("signed_off");
  expect((authorUI.getByTestId("plan-build") as HTMLButtonElement).disabled).toBe(false);
  // Losing a confirmed live channel cannot restore the HTTP gate, even for a 4404 close.
  const beforeDisconnect = daemon.liveSockets.length;
  const disconnected = render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  await settle();
  expect((within(disconnected.container).getByTestId("plan-build") as HTMLButtonElement).disabled).toBe(false);
  await act(async () => { for (const socket of daemon.liveSockets.slice(beforeDisconnect)) socket.close(4404); });
  await settle();
  expect((within(disconnected.container).getByTestId("plan-build") as HTMLButtonElement).disabled).toBe(true);
  disconnected.unmount();
  const reviewer = render(<SharedDesignReader client={live} docId="doc" onBack={() => {}} />);
  const reviewerUI = within(reviewer.container);
  await settle();
  expect(reviewerUI.queryByTestId("plan-sign-off")).toBeNull();
  fireEvent.click(authorUI.getByRole("button", { name: "Reopen" }));
  await settle();
  expect(authorUI.getByTestId("plan-reviewer-Bo").dataset.status).toBe("waiting");
  expect((authorUI.getByTestId("plan-build") as HTMLButtonElement).disabled).toBe(true);
  expect((reviewerUI.getByTestId("plan-sign-off") as HTMLButtonElement).disabled).toBe(true);
  expect((await live.plan("doc")).gate?.reviewers?.[0].status).toBe("waiting");
  expect((await live.sharedDesign("doc")).signed_off).toBe(false);
  fireEvent.click(authorUI.getByRole("button", { name: "Freeze for sign-off" }));
  await settle();
  // A delayed request for the withdrawn version cannot sign the replacement or build it.
  await expect(live.signOffPlan("doc", 7, firstVersion)).rejects.toMatchObject({ status: 409 });
  await expect(live.decidePlan("doc", { rev: 7, version: firstVersion, approved: true, picks: {}, comments: [], note: "" })).rejects.toMatchObject({ status: 409 });
  const asked = daemon.liveRefreshes;
  fireEvent.click(reviewerUI.getByTestId("plan-sign-off"));
  await settle();
  expect(reviewerUI.queryByTestId("plan-sign-off")).toBeNull();
  expect(daemon.liveRefreshes).toBe(asked + 1);   // the frozen version asked for again, its sign-offs the review's now
  expect(authorUI.getByTestId("plan-reviewer-Bo").dataset.status).toBe("signed_off");
  expect((authorUI.getByTestId("plan-build") as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(authorUI.getByTestId("plan-build"));
  await settle();
  expect((await live.plan("doc")).state).toBe("approved");
});

it("shows awareness colours, freezes credited text for everyone and withdraws sign-offs on reopen", async () => {
  const daemon = new FakeDaemon();
  daemon.seedLive("doc", { lede: "Text" }, { author: "Ann" });
  const client = daemon.client();
  render(<>
    <DocPresence client={client} visual="doc" isAuthor name="Ann" />
    <DocEditor client={client} visual="doc" section="lede" name="Ann" label="Ann's text" isAuthor />
    <DocEditor client={client} visual="doc" section="lede" name="Bo" label="Bo's text" />
  </>);
  await settle();
  const peers = screen.getByRole("list", { name: "Others in this document" });
  expect(within(peers).getAllByRole("listitem")).toHaveLength(1);
  expect(peers.querySelector('[data-peer="Bo"] [data-colour^="loro-peer-"]')).toBeTruthy();
  const ann = editor("Ann's text");
  const bo = editor("Bo's text");
  await act(async () => ann.dispatch({ changes: { from: 0, insert: "New " } }));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "Freeze for sign-off" }));
  await settle();
  expect(screen.getByRole("region", { name: "Live document" }).dataset.state).toBe("sign_off");
  expect(within(screen.getByRole("list", { name: "Credited writers" })).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Ann"]);
  const frozen = daemon.liveState("doc").version!;
  const snapshot = new LoroDoc();
  snapshot.import(fromBase64(frozen.snapshot));
  expect(snapshot.getText("lede").toString()).toBe("New Text");
  render(<DocEditor client={client} visual="doc" section="lede" name="Cy" label="Cy's text" />);
  await settle();
  expect(document.querySelector('[contenteditable="true"]')).toBeNull();
  for (const view of [ann, bo, editor("Cy's text")]) {
    expect(view.state.facet(EditorState.readOnly)).toBe(true);
    await act(async () => view.dispatch({ changes: { from: 0, insert: "Blocked " } }));
    expect(view.state.doc.toString()).toBe("New Text");
  }
  await act(async () => daemon.setLiveState("doc", { version: { ...frozen, signOffs: ["Bo"] } }));
  fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
  await settle();
  expect(daemon.liveState("doc").version).toBeNull();
  expect(screen.queryByTestId("doc-version")).toBeNull();
  expect(ann.state.facet(EditorState.readOnly)).toBe(false);
  await act(async () => bo.dispatch({ changes: { from: 0, insert: "Again " } }));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "Freeze for sign-off" }));
  await settle();
  expect(daemon.liveState("doc").version?.id).toBeGreaterThan(frozen.id);
  expect(daemon.liveState("doc").version?.signOffs).toEqual([]);
  expect(daemon.liveState("doc").version?.writers).toContain("Bo");
});

it("locks others' text while the author is offline and still sends comments and suggestions", async () => {
  const daemon = new FakeDaemon();
  daemon.seedLive("doc", { lede: "Workers run here." }, { author: "Ann", canFreeze: false });
  const live = daemon.client();
  const design: SharedDesign = { id: "doc", session: "", title: "Workers", rev: 1, state: "open", markdown: "Workers run here.", lede: "Workers run here.", sections: [], decisions: [], split: [], queued: 0, created_at: 1, can_comment: true, can_suggest: true, author: "Ann", me: "Bo" };
  const sent: string[] = [];
  const client = {
    visualLive: live.visualLive.bind(live),
    libraryPeople: async () => ({ people: [] }),
    sharedDesign: async () => design,
    writeSharedDesign: async (_id: string, tail: string) => { sent.push(tail); return design; },
  };
  render(<>
    <SharedDesignReader client={client} docId="doc" onBack={() => {}} />
    <DocEditor client={live} visual="doc" section="lede" name="Bo" label="Bo's text" />
  </>);
  await settle();
  await act(async () => daemon.setLiveState("doc", { lock: "author_offline" }));
  const panel = screen.getByRole("region", { name: "Live document" });
  expect(panel.dataset.state).toBe("author_offline");
  expect(within(panel).getAllByRole("status").length).toBeGreaterThan(0);
  const bo = editor("Bo's text");
  expect(bo.state.facet(EditorState.readOnly)).toBe(true);
  expect(document.querySelector('[contenteditable="true"]')).toBeNull();
  await act(async () => bo.dispatch({ changes: { from: 0, insert: "Blocked " } }));
  expect(daemon.liveText("doc", "lede")).toBe("Workers run here.");
  const highlight = () => {
    const text = within(screen.getByTestId("plan-doc")).getByText("Workers run here.").firstChild!;
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, 7);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    fireEvent.mouseUp(screen.getByTestId("plan-doc"));
  };
  highlight();
  fireEvent.click(within(screen.getByRole("toolbar")).getByRole("button", { name: "Comment" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Comment" }), { target: { value: "Where?" } });
  await act(async () => fireEvent.submit(screen.getByTestId("thread-draft")));
  highlight();
  fireEvent.click(within(screen.getByRole("toolbar")).getByRole("button", { name: "Suggest edit" }));
  await settle();
  const suggestion = editor("Suggestion to the overview");
  await act(async () => suggestion.dispatch(suggestion.state.replaceSelection("Agents")));
  await act(async () => fireEvent.submit(screen.getByTestId("thread-draft")));
  expect(sent).toEqual(["comments", "suggestions"]);
});

it("shows the live text, not the revision's, after Done editing, once frozen and once reopened, on the page and in the Markdown view", async () => {
  const daemon = new FakeDaemon();
  const live = daemon.client();
  const visual: VisualDTO = { id: "doc", session: "boss", title: "Workers", by: "supervisor", kind: "plan", rev: 1, kept: false, unread: false, created_at: 1 };
  const plan: Plan = { id: "doc", session: "boss", title: "Workers", rev: 1, state: "open", markdown: "", lede: "", decisions: [], split: [], queued: 0, created_at: 1,
    sections: [{ id: "summary", title: "Summary", markdown: "Workers run here." }],
    gate: { state: "waiting", required: 1, signed: 0, waiting_on: ["Bo"], reviewers: [{ sub: "Bo", required: true, status: "waiting" }] } };
  daemon.seedLive("doc", { summary: "Workers run here." }, { plan, me: "Ann", author: "Ann" });
  const client = {
    visualLive: live.visualLive.bind(live), plan: async () => plan, decidePlan: live.decidePlan.bind(live),
    visuals: async () => [visual], answerVisual: async () => visual, dismissVisual: async () => visual, keepVisual: async () => visual, unkeepVisual: async () => visual, markVisualRead: async () => {},
    commentPlan: async () => plan, replyPlan: async () => plan, resolvePlanThread: async () => plan, suggestPlan: async () => plan, acceptPlanSuggestion: async () => plan, rejectPlanSuggestion: async () => plan, editPlan: async () => plan, confirmPlan: async () => plan,
  };
  render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  await settle();
  fireEvent.click(screen.getByRole("radio", { name: "Editing" }));
  fireEvent.click(screen.getByRole("button", { name: "Edit Summary" }));
  await settle();
  const text = editor("Text of Summary");
  await act(async () => text.dispatch({ changes: { from: text.state.doc.length - 1, insert: ", on the customer's machine" } }));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "Done editing" }));
  await settle();
  // The revision still says "Workers run here."; the doc reads as it was typed, at every step.
  const typed = "Workers run here, on the customer's machine.";
  const showsTyped = () => {
    expect(within(screen.getByRole("region", { name: "Summary" })).getByText(typed)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Markdown" }));
    expect(screen.getByTestId("doc-markdown").textContent).toContain(typed);
    fireEvent.click(screen.getByRole("button", { name: "Markdown" }));
  };
  showsTyped();
  fireEvent.click(screen.getByRole("button", { name: "Freeze for sign-off" }));
  await settle();
  showsTyped();
  fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
  await settle();
  showsTyped();
});

it("types a suggestion only in Suggesting on unlocked text: in Viewing or once frozen it is shown read only", async () => {
  const daemon = new FakeDaemon();
  const live = daemon.client();
  const visual: VisualDTO = { id: "doc", session: "boss", title: "Workers", by: "supervisor", kind: "plan", rev: 1, kept: false, unread: false, created_at: 1 };
  const plan: Plan = { id: "doc", session: "boss", title: "Workers", rev: 1, state: "open", markdown: "", lede: "", decisions: [], split: [], queued: 0, created_at: 1,
    sections: [{ id: "summary", title: "Summary", markdown: "Workers run here." }],
    gate: { state: "waiting", required: 1, signed: 0, waiting_on: ["Bo"], reviewers: [{ sub: "Bo", required: true, status: "waiting" }] } };
  daemon.seedLive("doc", { summary: "Workers run here." }, { plan, me: "Ann", author: "Ann" });
  const client = {
    visualLive: live.visualLive.bind(live), plan: async () => plan, decidePlan: live.decidePlan.bind(live),
    visuals: async () => [visual], answerVisual: async () => visual, dismissVisual: async () => visual, keepVisual: async () => visual, unkeepVisual: async () => visual, markVisualRead: async () => {},
    commentPlan: async () => plan, replyPlan: async () => plan, resolvePlanThread: async () => plan, suggestPlan: async () => plan, acceptPlanSuggestion: async () => plan, rejectPlanSuggestion: async () => plan, editPlan: async () => plan, confirmPlan: async () => plan,
  };
  render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  await settle();
  const words = within(screen.getByRole("region", { name: "Summary" })).getByText("Workers run here.").firstChild!;
  const range = document.createRange();
  range.setStart(words, 0);
  range.setEnd(words, 7);
  window.getSelection()!.removeAllRanges();
  window.getSelection()!.addRange(range);
  fireEvent.mouseUp(screen.getByTestId("plan-doc"));
  fireEvent.click(within(screen.getByRole("toolbar", { name: "For the highlighted text" })).getByRole("button", { name: "Suggest edit" }));
  await settle();
  const typing = editor("Suggestion to Summary");
  await act(async () => typing.dispatch(typing.state.replaceSelection("Agents")));
  const editable = () => screen.getByRole("region", { name: "Summary" }).querySelector('[contenteditable="true"]');
  const preview = () => screen.getByTestId("plan-suggest-preview");
  // Viewing: the page reads, the change still shown; Suggesting again picks up what was typed.
  fireEvent.click(screen.getByRole("radio", { name: "Viewing" }));
  expect(editable()).toBeNull();
  expect([preview().querySelector("del")?.textContent, preview().querySelector("ins")?.textContent]).toEqual(["Worker", "Agent"]);
  fireEvent.click(screen.getByRole("radio", { name: "Suggesting" }));
  await settle();
  expect(editor("Suggestion to Summary").state.doc.toString()).toBe("Agents run here.");
  // Frozen with its text as it was: nothing can be typed, and the suggestion can't be sent.
  fireEvent.click(screen.getByRole("button", { name: "Freeze for sign-off" }));
  await settle();
  expect(editable()).toBeNull();
  expect(preview().querySelector("ins")?.textContent).toBe("Agent");
  expect((within(screen.getByTestId("plan-verdict")).getByRole("button", { name: "Send 1 suggestion" }) as HTMLButtonElement).disabled).toBe(true);
});

/* The Markdown switch changes the view, never the mode: read-only while Viewing, the live text while
   Editing (where the formatting toolbar writes the syntax into the draft), read-only and marked while
   a version is locked for sign-off. */
it.each([
  ["viewing", () => {}],
  ["editing", () => fireEvent.click(screen.getByRole("radio", { name: "Editing" }))],
  ["locked", () => fireEvent.click(screen.getByRole("button", { name: "Freeze for sign-off" }))],
] as const)("shows the doc's Markdown %s", async (state, enter) => {
  const daemon = new FakeDaemon();
  const live = daemon.client();
  const visual: VisualDTO = { id: "doc", session: "boss", title: "Workers", by: "supervisor", kind: "plan", rev: 1, kept: false, unread: false, created_at: 1 };
  const plan: Plan = { id: "doc", session: "boss", title: "Workers", rev: 1, state: "open", markdown: "", lede: "", decisions: [], split: [], queued: 0, created_at: 1,
    sections: [{ id: "summary", title: "Summary", markdown: "Workers run here." }],
    gate: { state: "waiting", required: 1, signed: 0, waiting_on: ["Bo"], reviewers: [{ sub: "Bo", required: true, status: "waiting" }] } };
  daemon.seedLive("doc", { summary: "Workers run here." }, { plan, me: "Ann", author: "Ann" });
  const client = {
    visualLive: live.visualLive.bind(live), plan: live.plan.bind(live), decidePlan: live.decidePlan.bind(live),
    visuals: async () => [visual], answerVisual: async () => visual, dismissVisual: async () => visual, keepVisual: async () => visual, unkeepVisual: async () => visual, markVisualRead: async () => {},
    commentPlan: async () => plan, replyPlan: async () => plan, resolvePlanThread: async () => plan, suggestPlan: async () => plan, acceptPlanSuggestion: async () => plan, rejectPlanSuggestion: async () => plan, editPlan: async () => plan, confirmPlan: async () => plan,
  };
  render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  await settle();
  enter();
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "Markdown" }));
  await settle();
  const source = screen.getByTestId("doc-markdown");
  expect(source.dataset.state).toBe(state);
  expect(screen.queryByRole("toolbar", { name: "Formatting" }) !== null).toBe(state === "editing");
  expect(source.querySelector('[contenteditable="true"]') !== null).toBe(state === "editing");
  expect(within(source).queryByTestId("doc-markdown-readonly") !== null).toBe(state === "locked");
  if (state !== "editing") return;
  const text = EditorView.findFromDOM(source.querySelector<HTMLElement>(".cm-editor")!)!;
  expect(text.state.doc.toString()).toBe("Workers run here.");
  text.dispatch({ selection: { anchor: 0, head: 7 } });
  fireEvent.focus(text.contentDOM);
  fireEvent.click(screen.getByRole("button", { name: "Bold" }));
  await settle();
  expect(daemon.liveText("doc", "summary")).toBe("**Workers** run here.");
});
