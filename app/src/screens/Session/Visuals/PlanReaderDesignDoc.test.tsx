import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DaemonError, type DocRevisionComparison, type Plan, type SharedDesign, type VisualDTO } from "../../../api/client";
import { Annotated, MarginNote } from "./PlanDesignDoc";
import { PlanReader } from "./PlanReader";
import { SharedDesignReader } from "./SharedDesignReader";
import type { PlanDocClient, PlansClient, SharedDesignClient, VisualsClient } from "./client";

afterEach(cleanup);

/* mermaid draws in the browser; here it returns a fixed drawing. On a source it can't parse it works as
   mermaid does: its temporary container is left in the page body, and an error drawing is added to it
   unless error rendering is suppressed. */
vi.mock("mermaid", () => {
  let suppressed = false;
  return {
    default: {
      initialize: (config: { suppressErrorRendering?: boolean }) => { suppressed = !!config.suppressErrorRendering; },
      render: async (id: string, source: string) => {
        if (!source.includes("broken")) return { svg: `<svg data-mermaid="1"><g></g></svg>` };
        const temp = document.createElement("div");
        temp.id = `d${id}`;
        temp.innerHTML = suppressed ? "" : `<svg data-error="1"></svg>`;
        document.body.append(temp);
        throw new Error("Parse error");
      },
    },
  };
});

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
    { id: "context", title: "Context", markdown: "Teams poll today.\n\n- Polling misses short states." },
    { id: "goals", title: "Goals and non-goals", markdown: "**Goals**\n\n- No event is lost.\n\n**Non-goals**\n\n- Exactly-once delivery." },
    { id: "proposed-design", title: "Proposed design", markdown: "An outbox row per event." },
    { id: "architecture", title: "Architecture", markdown: "The dispatcher signs each payload." },
    { id: "alternatives", title: "Alternatives considered", markdown: "- **Keep polling.** Every team still runs a cron job.\n- **A relay.** Payloads leave the machine." },
    { id: "risks", title: "Risks and what's hard to undo", markdown: "**Hard to undo:** the payload shape.\n\n- Dead endpoints flood the Inbox." },
    { id: "rollout", title: "Rollout and tasks", markdown: "1. **Lands dark.** Events are written, never sent.\n2. **Reaches teams.** The dispatcher ships.\n\n### What gets built\n\n- The outbox" },
    { id: "open-questions", title: "Open questions", markdown: "None." },
  ],
  decisions: [
    { id: "store", title: "Where should delivery state live?", section: "proposed-design", mine: "a", challenge: "Write locks could stall delivery.", options },
    { id: "loud", title: "How should failures show up?", section: "architecture", mine: "a", options },
    { id: "reach", title: "How should it reach teams?", options },
  ],
  split: [{ id: "outbox", title: "Outbox", files: ["src/outbox.py"], test: "pytest outbox", state: "queued", test_state: "not run" }],
  threads: [
    { id: 1, kind: "comment", rev: 1, author: "person", text: "Retry-After?", status: "open", at: 1, anchor: { kind: "text", section: "proposed-design", start: 3, end: 9, quote: "outbox", state: "current" } },
    { id: 2, kind: "comment", rev: 1, author: "person", text: "Which relay?", status: "open", at: 1, anchor: { kind: "text", section: "alternatives", start: 88, end: 95, quote: "machine", state: "current" } },
    { id: 3, kind: "suggestion", rev: 1, author: "person", text: "", status: "open", at: 1, before: "Dead", after: "Failing", anchor: { kind: "text", section: "risks", start: 40, end: 44, quote: "Dead", state: "current" } },
    { id: 4, kind: "suggestion", rev: 1, author: "person", text: "", status: "open", at: 1, before: "Teams", after: "Admins", anchor: { kind: "text", section: "context", start: 0, end: 5, quote: "Teams", state: "current" } },
  ],
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
  expect(within(outline).getAllByTestId("outline-section").map((a) => a.dataset.section)).toEqual(["context", "goals", "proposed-design", "alternatives", "risks", "rollout", "open-questions"]);
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
  await act(async () => fireEvent.click(within(card("store")).getAllByRole("radio")[1]));
  await act(async () => fireEvent.click(within(card("loud")).getAllByRole("radio")[0]));
  expect(card("store").dataset.state).toBe("decided");
  expect(rows().map((a) => a.dataset.state)).toEqual(["decided", "decided", "open"]);
  expect(approve().disabled).toBe(true);
  // Each pick is saved as it is made: Approve waits for the last save to come back.
  await act(async () => fireEvent.click(within(card("reach")).getAllByRole("radio")[0]));
  expect(screen.getByTestId("plan-next").dataset.state).toBe("ready");
  expect(approve().disabled).toBe(false);
  await act(async () => fireEvent.click(approve()));
  expect(verdicts).toEqual([expect.objectContaining({ approved: true, picks: { store: "b", loud: "a", reach: "a" } })]);
  expect(screen.queryByTestId("task-outbox")).toBeNull();
});

it("an Approve the engine refuses for a reason other than a newer revision shows that reason", async () => {
  const plan = spine({ decisions: [], threads: [] });
  const why = "Risks and what's hard to undo is required by the Design review template: fill it in before this is signed off";
  const client: PlansClient & PlanDocClient & VisualsClient = { ...visuals, ...writes(plan), plan: async () => plan,
    decidePlan: async () => { throw new DaemonError("required_sections_missing", why, 409); } };
  render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  const approve = (await screen.findByTestId("plan-build")) as HTMLButtonElement;
  expect(approve.disabled).toBe(false);
  await act(async () => fireEvent.click(approve));
  expect(within(screen.getByTestId("plan-verdict")).getByRole("alert").textContent).toContain(why);
});

/* The engine as it keeps picks: each voter's one pick per decision, in `library` by account as a
   shared plan's reach the Library (the author is u_author). `hold` keeps the next confirm's
   answer back until `release`. */
function pickEngine(more: Partial<Plan> = {}, library: Record<string, Record<string, string[]>> = {}) {
  let plan = spine({ threads: [], ...more });
  const confirms: unknown[] = [];
  const verdicts: unknown[] = [];
  let held: (() => void) | null = null;
  const state = { hold: false, release: () => held?.() };
  const client: PlansClient & PlanDocClient & VisualsClient = {
    ...visuals, ...writes(plan), plan: async () => plan, decidePlan: async (_id, verdict) => { verdicts.push(verdict); return plan; },
    confirmPlan: async (_id, rev, decision, option) => {
      confirms.push([rev, decision, option]);
      const cast = Object.fromEntries(Object.entries(library[decision] ?? {}).map(([o, who]) => [o, who.filter((w) => w !== "u_author")]));
      library[decision] = { ...cast, [option]: [...(cast[option] ?? []), "u_author"] };
      plan = { ...plan, decisions: plan.decisions.map((d) => {
        if (d.id !== decision) return d;
        const votes = Object.fromEntries(Object.entries(d.votes ?? {}).map(([o, who]) => [o, who.filter((w) => w !== "person")]));
        return { ...d, confirmed: option, votes: { ...votes, [option]: [...(votes[option] ?? []), "person"] } };
      }) };
      if (state.hold) await new Promise<void>((resolve) => { held = resolve; });
      return plan;
    },
  };
  return { client, confirms, verdicts, state };
}

const radios = (card: HTMLElement) => within(card).getAllByRole("radio") as HTMLInputElement[];

async function openPlan(client: PlansClient & PlanDocClient & VisualsClient) {
  render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  await screen.findByTestId("plan-outline");
  const doc = screen.getByTestId("plan-doc");
  return (id: string) => doc.querySelector<HTMLElement>(`[data-testid=plan-decision][data-decision=${id}]`)!;
}

it("saves each pick as it is made, a change back included: reopened, the doc keeps it, counts it decided, says who picked what, and Approve sends it", async () => {
  const known = "64680428-2b70-4a42-839b-1b1e05b51ca4";
  const unknown = "81790428-2b70-4a42-839b-1b1e05b51ca4";
  const { client, confirms, verdicts } = pickEngine({ teammate_names: { [known]: "Dana Okafor" }, decisions: spine().decisions.map((d) => (d.id === "store" ? { ...d, votes: { a: [`teammate:${known}`, `teammate:${unknown}`] } } : d)) });
  let card = await openPlan(client);
  expect(within(card("store")).getByTitle("Dana Okafor").textContent).toBe("DO");
  expect(within(card("store")).getByTitle("A teammate").textContent).toBe("AT");
  expect(card("store").innerHTML).not.toContain(known);
  expect(card("store").innerHTML).not.toContain(unknown);
  for (const option of [0, 1, 0]) await act(async () => fireEvent.click(radios(card("store"))[option]));
  expect(confirms).toEqual([[1, "store", "a"], [1, "store", "b"], [1, "store", "a"]]);

  // Closed before Approve and opened again: the last pick is still made, and counts.
  cleanup();
  card = await openPlan(client);
  expect(card("store").dataset.state).toBe("decided");
  expect(radios(card("store")).map((r) => r.checked)).toEqual([true, false]);
  expect(screen.getByTestId("plan-next").dataset.left).toBe("2");
  expect(within(card("store")).getByTestId("plan-picks").textContent).toBe("You: A table · Dana Okafor: A table · A teammate: A table");

  await act(async () => fireEvent.click(radios(card("loud"))[0]));
  await act(async () => fireEvent.click(radios(card("reach"))[1]));
  const approve = screen.getByTestId("plan-build") as HTMLButtonElement;
  expect(approve.disabled).toBe(false);
  await act(async () => fireEvent.click(approve));
  expect(verdicts).toEqual([expect.objectContaining({ approved: true, picks: { store: "a", loud: "a", reach: "b" } })]);
});

it("takes no second pick while one is being saved, so closing before the answer loses none it took", async () => {
  const { client, confirms, state } = pickEngine();
  state.hold = true;
  let card = await openPlan(client);
  await act(async () => fireEvent.click(radios(card("store"))[1]));
  // The first save is still out: the cards take no pick meanwhile.
  expect(radios(card("loud")).map((r) => r.disabled)).toEqual([true, true]);
  await act(async () => fireEvent.click(radios(card("loud"))[1]));
  expect(confirms).toEqual([[1, "store", "b"]]);

  // Closed before its answer; it comes back afterwards, and reopened the pick is there.
  cleanup();
  state.hold = false;
  await act(async () => state.release());
  card = await openPlan(client);
  expect(radios(card("store")).map((r) => r.checked)).toEqual([false, true]);
  expect([card("store").dataset.state, card("loud").dataset.state]).toEqual(["decided", "open"]);
  expect(screen.getByTestId("plan-next").dataset.left).toBe("2");
});

it("an author's pick on a shared plan, still waiting on a reviewer, reaches a teammate's reader, beside their own saved pick", async () => {
  // The teammate (u_me) voted already. Their reader reads what the Library holds, its own pick as "person".
  const library: Record<string, Record<string, string[]>> = { store: { a: ["u_me"] } };
  const { client } = pickEngine({ gate: inReview }, library);
  const card = await openPlan(client);
  await act(async () => fireEvent.click(radios(card("store"))[1]));
  expect((screen.getByTestId("plan-build") as HTMLButtonElement).disabled).toBe(true);
  cleanup();

  const { design } = teammateDoc(async () => {});
  const read = (): SharedDesign => ({ ...design, decisions: design.decisions.map((d) => {
    const cast = library[d.id] ?? {};
    return { ...d, votes: Object.fromEntries(Object.entries(cast).map(([o, who]) => [o, who.map((w) => (w === "u_me" ? "person" : w))])),
      confirmed: Object.entries(cast).find(([, who]) => who.includes("u_me"))?.[0] ?? null };
  }) });
  const teammate: SharedDesignClient = {
    sharedDesign: async () => read(), writeSharedDesign: async () => read(),
    libraryPeople: async () => ({ people: [{ sub: "u_author", display_name: "Dana Okafor", email: "dana.okafor@example.test" }] }),
  };
  render(<SharedDesignReader client={teammate} docId={design.id} onBack={() => {}} />);
  const doc = await screen.findByTestId("plan-doc");
  const store = doc.querySelector<HTMLElement>("[data-testid=plan-decision][data-decision=store]")!;
  await waitFor(() => expect(within(store).getByTestId("plan-picks").textContent).toBe("You: A table · Dana Okafor: A queue"));
  expect(within(store).getByTitle("Dana Okafor").textContent).toBe("DO");
  expect(store.innerHTML).not.toContain("u_author");
  expect(store.dataset.state).toBe("decided");
  expect(screen.getByTestId("plan-next").dataset.left).toBe("2");
});

/* Shared for team review, one required reviewer still to sign. */
const inReview = { state: "waiting" as const, required: 1, signed: 0, waiting_on: ["u_dana"], reviewers: [{ sub: "u_dana", required: true, status: "waiting" as const }] };

/* The spine plan as a teammate who must sign it reads it from the Library; `answer` settles each write. */
function teammateDoc(answer: (tail: string) => Promise<void>) {
  const design: SharedDesign = { ...spine({ gate: { ...inReview, waiting_on: ["u_me"], reviewers: [{ sub: "u_me", required: true, status: "waiting", you: true }] }, threads: [] }),
    me: "u_me", author: "u_author", can_comment: true, can_suggest: true, can_sign_off: true, signed_off: false };
  const sent: [string, Record<string, unknown>][] = [];
  const client: SharedDesignClient = {
    sharedDesign: async () => design,
    writeSharedDesign: async (_doc, tail, body) => { sent.push([tail, body]); await answer(tail); return design; },
    libraryPeople: async () => ({ people: [] }),
  };
  return { design, sent, client };
}

it.each([
  ["the author", async () => {
    const plan = spine({ gate: inReview, threads: [] });
    const client: PlansClient & PlanDocClient & VisualsClient = { ...visuals, ...writes(plan), plan: async () => plan, decidePlan: async () => plan };
    render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
    // Every pick made, Approve still waits on the reviewer's sign-off, and the sheet says who.
    return async () => {
      expect(within(screen.getByTestId("plan-outline")).getByTestId("plan-signoffs")).toBeTruthy();
      const approve = screen.getByTestId("plan-build") as HTMLButtonElement;
      expect([approve.dataset.gate, approve.disabled]).toEqual(["waiting", true]);
    };
  }],
  ["a teammate", async () => {
    const { design, sent, client } = teammateDoc(async () => {});
    render(<SharedDesignReader client={client} docId={design.id} onBack={() => {}} />);
    // Each pick went out as the teammate's vote, and their Sign off is the bar's action.
    return async () => {
      expect(sent.map(([tail, body]) => [tail, body.decision])).toEqual([["votes", "store"], ["votes", "loud"], ["votes", "reach"]]);
      expect((screen.getByTestId("plan-sign-off") as HTMLButtonElement).disabled).toBe(false);
    };
  }],
])("reads a shared spine plan, as %s, in the planning doc's layout", async (_who, open) => {
  const finish = await open();
  const outline = await screen.findByTestId("plan-outline");
  expect(within(outline).getAllByTestId("outline-decision").map((a) => a.dataset.state)).toEqual(["open", "open", "open"]);
  const doc = screen.getByTestId("plan-doc");
  expect([...within(doc).getByRole("region", { name: "Open questions" }).querySelectorAll<HTMLElement>("[data-testid=plan-decision]")].map((d) => d.dataset.decision)).toEqual(["reach"]);
  // Not the older reader: no review column, collapsed or open.
  expect(screen.queryByTestId("plan-review")).toBeNull();
  expect(screen.queryByTestId("plan-review-strip")).toBeNull();
  for (const id of ["store", "loud", "reach"]) {
    await act(async () => fireEvent.click(within(doc.querySelector<HTMLElement>(`[data-testid=plan-decision][data-decision=${id}]`)!).getAllByRole("radio")[0]));
  }
  expect(within(outline).getAllByTestId("outline-decision").map((a) => a.dataset.state)).toEqual(["decided", "decided", "decided"]);
  await finish();
});

it("counts a teammate's pick as decided only once its vote is saved, and takes no second pick while one is out", async () => {
  let settle!: () => void;
  let answer: () => Promise<void> = () => new Promise<void>((resolve) => { settle = resolve; });
  const { design, sent, client } = teammateDoc(() => answer());
  render(<SharedDesignReader client={client} docId={design.id} onBack={() => {}} />);
  const doc = await screen.findByTestId("plan-doc");
  const card = (id: string) => doc.querySelector<HTMLElement>(`[data-testid=plan-decision][data-decision=${id}]`)!;
  const pick = (id: string) => act(async () => fireEvent.click(within(card(id)).getAllByRole("radio")[0]));
  const signOff = () => screen.getByTestId("plan-sign-off") as HTMLButtonElement;

  // The first vote is still out: it isn't decided yet, and a second pick isn't taken (nor lost as a decided one).
  await pick("store");
  await pick("loud");
  expect(sent.map(([, body]) => body.decision)).toEqual(["store"]);
  expect([card("store").dataset.state, card("loud").dataset.state]).toEqual(["open", "open"]);
  await act(async () => settle());
  expect(card("store").dataset.state).toBe("decided");

  // A vote the Library refuses leaves its decision open, and Sign off waits for it.
  answer = async () => { throw new DaemonError("upstream", "offline", 502); };
  await pick("loud");
  expect(card("loud").dataset.state).toBe("open");
  expect(screen.getByRole("alert")).toBeTruthy();
  answer = async () => {};
  await pick("reach");
  expect(signOff().disabled).toBe(true);
});

it("lays out the spine sections as the design does, and their words stay anchorable", async () => {
  const plan = spine();
  const client: PlansClient & PlanDocClient & VisualsClient = { ...visuals, ...writes(plan), plan: async () => plan, decidePlan: async () => plan };
  render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  const doc = await screen.findByTestId("plan-doc");
  const region = (name: string) => within(doc).getByRole("region", { name });
  expect(region("Goals and non-goals").querySelector("[data-layout=two]")?.children.length).toBe(2);
  expect(region("Alternatives considered").querySelectorAll("[data-layout=rows] dt").length).toBe(2);
  expect(region("Risks and what's hard to undo").querySelector("[data-layout=undo]")).toBeTruthy();
  expect(region("Rollout and tasks").querySelectorAll("[data-layout=steps] li").length).toBe(2);
  expect(region("Rollout and tasks").querySelectorAll("[data-layout=tasks] li").length).toBe(1);
  // A suggestion in a laid-out list still shows its new words beside the struck ones.
  expect(region("Risks and what's hard to undo").querySelector('del[data-thread="3"] + ins')?.textContent).toBe("Failing");
  // A suggestion in a paragraph, with a laid-out list after it: its new words once, at its range.
  const context = region("Context");
  expect([...context.querySelectorAll('ins[data-thread="4"]')].map((el) => [el.previousElementSibling?.getAttribute("data-thread"), el.textContent])).toEqual([["4", "Admins"]]);
  // A comment on an alternative's words is drawn on them inside its row.
  expect(region("Alternatives considered").querySelector('[data-layout=rows] dd [data-thread="2"]')?.textContent).toBe("machine");
});

it("keeps a past revision on the page once the history menu closes, with Approve off until back at the latest", async () => {
  const plan = spine({ decisions: [], threads: [], rev: 2 });
  const comparison: DocRevisionComparison = {
    revision: { ...plan, rev: 1, sections: [{ id: "context", title: "Context", markdown: "Teams polled." }] },
    previous: { ...plan, rev: 1, sections: [{ id: "context", title: "Context", markdown: "Teams polled." }] },
    changes: [],
  };
  const client = {
    ...visuals, ...writes(plan), plan: async () => plan, decidePlan: async () => plan,
    planHistory: async () => ({ events: [{ id: "r1", kind: "revision" as const, actor: "supervisor", at: 1, rev: 1, version: null }] }),
    planRevisionChanges: async () => comparison,
  };
  render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  await screen.findByTestId("plan-design-doc");
  fireEvent.click(screen.getByTestId("plan-rev").closest("button")!);
  await act(async () => {});
  await act(async () => fireEvent.click(within(screen.getByTestId("doc-history")).getByRole("button")));
  const pane = screen.getByTestId("plan-doc");
  expect(screen.getByTestId("plan-history-menu").hidden).toBe(true);
  const shown = within(pane).getByRole("region", { name: "Revision comparison" });
  expect((screen.getByTestId("plan-build") as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(within(shown).getByRole("button", { name: "Back to the latest" }));
  expect(within(pane).queryByRole("region", { name: "Revision comparison" })).toBeNull();
  expect((screen.getByTestId("plan-build") as HTMLButtonElement).disabled).toBe(false);
});

it("moves the notes below a margin note that grows when a reply opens", () => {
  const observers: ResizeObserverCallback[] = [];
  vi.stubGlobal("ResizeObserver", class {
    constructor(cb: ResizeObserverCallback) { observers.push(cb); }
    observe() {}
    disconnect() {}
  });
  const height = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight")!;
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, get(this: HTMLElement) { return this.querySelector("form") ? 100 : 40; } });
  try {
    const actions = { reply: async () => true, resolve: async () => true, accept: async () => true, reject: async () => true };
    const note = (id: number) => ({ id, kind: "comment" as const, rev: 1, author: "person", text: "Why?", status: "open" as const, at: 1, anchor: { kind: "section" as const, section: "context", state: "current" as const } });
    const notes = [1, 2].map((id) => ({ key: `t${id}`, anchor: null, node: <MarginNote thread={note(id)} busy={false} canJudge canReply canResolve actions={actions} lit={false} onHover={() => {}} /> }));
    render(<Annotated notes={notes}><p>Text</p></Annotated>);
    const second = () => screen.getByTestId("thread-2").parentElement!;
    expect(second().style.top).toBe("52px");
    fireEvent.click(within(screen.getByTestId("thread-1")).getByRole("button", { name: "Reply" }));
    act(() => observers.forEach((cb) => cb([], {} as ResizeObserver)));
    expect(second().style.top).toBe("112px");
  } finally {
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", height);
    vi.unstubAllGlobals();
  }
});

it("draws a mermaid section as an svg, and shows its source when it can't be drawn", async () => {
  const withFigure = (markdown: string) => spine({ sections: spine().sections.map((s) => (s.id === "architecture" ? { ...s, kind: "mermaid" as const, markdown, caption: "Flow" } : s)) });
  const drawn = withFigure("flowchart LR\n  A-->B");
  const client: PlansClient & PlanDocClient & VisualsClient = { ...visuals, ...writes(drawn), plan: async () => drawn, decidePlan: async () => drawn };
  const view = render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  const figure = (doc: HTMLElement) => doc.querySelector<HTMLElement>("[data-diagram=architecture]")!;
  const doc = await screen.findByTestId("plan-doc");
  await waitFor(() => expect(figure(doc).querySelector("svg")).toBeTruthy());
  expect(figure(doc).querySelector("pre, code")).toBeNull();
  expect(figure(doc).querySelector("figcaption")?.textContent).toContain("Flow");
  view.unmount();

  const broken = withFigure("flowchart LR\n  broken -->");
  const failing: PlansClient & PlanDocClient & VisualsClient = { ...visuals, ...writes(broken), plan: async () => broken, decidePlan: async () => broken };
  render(<PlanReader client={failing} visual={visual} onBack={() => {}} onChanged={() => {}} />);
  const failedDoc = await screen.findByTestId("plan-doc");
  await waitFor(() => expect(figure(failedDoc).querySelector("pre")?.textContent).toContain("broken -->"));
  expect(figure(failedDoc).querySelector("svg")).toBeNull();
  // Nothing mermaid left in the page outside the figure.
  expect(document.querySelector("[data-error]")).toBeNull();
  expect(document.querySelector('[id*="doc-mermaid"]')).toBeNull();
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
