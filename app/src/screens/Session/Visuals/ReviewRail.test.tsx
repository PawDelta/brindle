import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { toBase64, type Plan, type VisualDTO } from "../../../api/client";
import { FakeDaemon } from "../../../test/fakeDaemon";
import { PlanReader } from "./PlanReader";
import { SharedDesignReader } from "./SharedDesignReader";

afterEach(cleanup);
const visual: VisualDTO = { id: "plan", kind: "plan", session: "boss", by: "supervisor", title: "Delivery", rev: 1, created_at: 1, unread: false, kept: false };
function read(extra: Partial<Plan> = {}, reviewer = false, hold?: Promise<void>) {
  const plan: Plan = { ...visual, state: "open", markdown: "# Delivery", lede: "", queued: 0, sections: [{ id: "context", title: "Context", markdown: "Notes" }], decisions: [], split: [], threads: [], ...extra };
  const daemon = new FakeDaemon();
  const doc = daemon.seedLive(plan.id, { context: "Notes" }, { plan, author: "sam", me: reviewer ? "alice" : "sam" });
  daemon.setLiveState(plan.id, { lock: "sign_off", version: { id: plan.rev, snapshot: toBase64(doc.export({ mode: "snapshot" })), writers: [], signOffs: [] } });
  const client = daemon.client();
  if (hold) {
    const decide = client.decidePlan.bind(client);
    vi.spyOn(client, "decidePlan").mockImplementation(async (...args) => { await hold; return decide(...args); });
  }
  if (reviewer) return render(<SharedDesignReader docId={plan.id} client={client} onBack={() => {}} />);
  return render(<PlanReader client={client} visual={visual} onBack={() => {}} onChanged={() => {}} />);
}

const gate: Plan["gate"] = { state: "waiting", required: 1, signed: 0, waiting_on: ["alice"], reviewers: [
  { sub: "alice", required: true, status: "waiting" }, { sub: "bob", required: false, status: "waiting" },
] };

it("shows the author's gate and groups its signers by their effect on approval", async () => {
  read({ gate, decisions: [{ id: "transport", title: "Transport", confirmed: "http", options: [{ id: "http", label: "HTTP", pros: [], cons: [] }] }] });
  const rail = await screen.findByTestId("review-rail");
  expect(within(rail).getByTestId("plan-build")).toHaveProperty("disabled", true);
  expect(within(rail).getByTestId("signoff-alice").closest("[data-group]")?.getAttribute("data-group")).toBe("required");
  expect(within(rail).getByTestId("signoff-bob").closest("[data-group]")?.getAttribute("data-group")).toBe("optional");
});

it("gives a reviewer the sign-off action in their rail", async () => {
  read({ gate, outbox: [{ client_id: "feedback", tail: "/comments", state: "waiting", body: { rev: 1, text: "Check the delivery order." } }] }, true);
  const rail = within(await screen.findByTestId("review-rail"));
  expect(rail.getByTestId("rail-queue").querySelector("article[data-state=waiting] [data-state=waiting]")).toBeTruthy();
  fireEvent.click(rail.getByRole("button", { name: "Comment" }));
  expect(within(rail.getByTestId("thread-draft")).getByRole("textbox")).toBeTruthy();
  await waitFor(() => expect(rail.getByTestId("plan-sign-off")).toHaveProperty("disabled", false));
  fireEvent.click(rail.getByTestId("plan-sign-off"));
  await waitFor(() => expect(rail.getByTestId("signoff-alice").dataset.state).toBe("signed"));
});

it("keeps the approval label when the verdict is in flight", async () => {
  let finish!: () => void;
  read({ gate: { state: "ready", required: 0, signed: 0, reviewers: [], waiting_on: [] } }, false, new Promise<void>(resolve => { finish = resolve; }));
  const approve = await screen.findByTestId("plan-build");
  const label = approve.textContent;
  await waitFor(() => expect(approve.dataset.state).toBe("ready"));
  fireEvent.click(approve);
  expect(approve.dataset.state).toBe("pressed");
  expect(approve.textContent).toBe(label);
  await act(async () => finish());
});

it("folds the rail into the document when its container narrows", async () => {
  let resize!: (width: number) => void;
  vi.stubGlobal("ResizeObserver", class {
    constructor(private callback: ResizeObserverCallback) {}
    observe(el: HTMLElement) { if (el.dataset.testid === "plan-design-doc") resize = width => this.callback([{ contentRect: { width } } as ResizeObserverEntry], this as unknown as ResizeObserver); }
    disconnect() {}
  });
  try {
    read();
    const rail = await screen.findByTestId("review-rail");
    act(() => resize(760));
    expect(rail.dataset.folded).toBe("true");
    expect(within(screen.getByTestId("plan-doc")).getByTestId("review-summary")).toBeTruthy();
    expect(within(rail).getByTestId("plan-build")).toBeTruthy();
  } finally { vi.unstubAllGlobals(); }
});

it("places library actions in the planning toolbar and switches source from its mode menu", async () => {
  read();
  const toolbar = (await screen.findByTestId("plan-outline")).closest("[data-testid=plan-reader]")!.querySelector("header")!;
  expect(within(toolbar).getByTestId("visual-keep")).toBeTruthy();
  expect(within(toolbar).getByTestId("visual-export")).toBeTruthy();
  fireEvent.click(within(toolbar).getByText("Viewing", { exact: false }));
  await act(async () => fireEvent.click(within(screen.getByTestId("plan-mode-menu")).getByTestId("doc-markdown-switch")));
  expect(screen.getByTestId("doc-markdown")).toBeTruthy();
});
