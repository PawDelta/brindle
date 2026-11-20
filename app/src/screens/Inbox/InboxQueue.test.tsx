import { connectDaemon, inTestContext } from "../../test/harness";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { InboxItemDTO, SessionDTO, SharedDesignDTO } from "../../api/schema";
import { FakeDaemon } from "../../test/fakeDaemon";
import { question, root, worker } from "../../test/fixtures";
import { InboxScreen } from "./InboxScreen";
import { resetTriage } from "./queue";

afterEach(cleanup);
beforeEach(() => resetTriage());

const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };
const asked: InboxItemDTO = { ...question, detail: "Which database should the cache use?", inbox_group: "needs_you", request_id: "1" };
const review: InboxItemDTO = {
  kind: "review_requested", reason: "Review requested", inbox_group: "needs_you", doc: "ad_2", detail: "CSV export",
  from_person: { sub: "u_ana", name: "Ana Ruiz" }, item_key: "review:ad_2", read: false,
};
const progress: InboxItemDTO = {
  kind: "signoff_progress", reason: "2 of 3 signed off", inbox_group: "from_team", doc: "ad_5", detail: "Rate limits",
  from_person: { sub: "u_chen", name: "Chen Wei" }, item_key: "signoffs:ad_5", read: false,
  signoff: { rev: 1, signed: 2, required: 3, waiting: [{ sub: "u_sam", name: "Sam Ortiz" }] },
};
const comment = (thread: number, sub: string, key: string): InboxItemDTO => ({
  kind: "shared_doc_comment", reason: "Commented", inbox_group: "from_team", doc: "ad_7", detail: "Webhook delivery",
  from_person: { sub, name: sub }, item_key: key, read: false, thread_id: thread, excerpt: "Then we need a separate cap.",
});

function mount(inbox: InboxItemDTO[]) {
  const daemon = new FakeDaemon({ sessions: [session], workspaces: [root, worker], inbox });
  const { client, store, sync } = connectDaemon(daemon);
  const view = () => render(inTestContext(<InboxScreen client={client} refresh={sync.refresh} onOpen={vi.fn()} onNavigate={vi.fn()} />, { client, store }, { daemon: true, sessions: true }));
  view();
  return { daemon, client, sync, view };
}

const row = (kind: string) => document.querySelector<HTMLElement>(`[data-testid="inbox-row"][data-kind="${kind}"]`)!;
const posts = (daemon: FakeDaemon, path: string) => daemon.requests.filter((r) => r.method === "POST" && r.path === path);

describe("the Inbox queue", () => {
  it("puts each row in its part, threads on one design in one row, sign-off progress as dots", async () => {
    mount([progress, comment(11, "u_chen", "c:11"), review, asked, comment(12, "u_sam", "c:12")]);
    const waiting = await screen.findByTestId("inbox-waiting");
    const team = screen.getByTestId("inbox-team");
    // What's stopped comes first: the agent's question before the review request.
    expect(within(waiting).getAllByTestId("inbox-row").map((r) => r.dataset.kind)).toEqual(["question", "review_requested"]);
    expect(within(team).getAllByTestId("inbox-row").map((r) => r.dataset.kind)).toEqual(["signoff_progress", "shared_doc_comment"]);
    const pips = within(row("signoff_progress")).getByTestId("signoff-pips");
    expect([pips.dataset.signed, pips.dataset.required]).toEqual(["2", "3"]);
    expect(pips.querySelectorAll("i.on")).toHaveLength(2);
  });

  it("opens the first waiting row with its answer box, and Enter sends the answer to the agent", async () => {
    const { daemon } = mount([review, asked]);
    await waitFor(() => expect(row("question").dataset.open).toBe("true"));
    expect(row("review_requested").dataset.open).toBeUndefined();
    const box = within(row("question")).getByTestId("answer-box");
    fireEvent.change(box, { target: { value: "Use Redis." } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(posts(daemon, "/inbox/sup1/answer").map((r) => r.body)).toEqual([{ text: "Use Redis." }]));
    // The row settles to its line where it stood, and the next row opens.
    await waitFor(() => expect(document.querySelector<HTMLElement>("[data-testid=inbox-line]")?.dataset.what).toBe("Answered"));
    expect(row("question")).toBeNull();
    expect(row("review_requested").dataset.open).toBe("true");
  });

  it("marks a team row read through the daemon when it is opened", async () => {
    const { daemon } = mount([progress]);
    fireEvent.click(within(await screen.findByTestId("inbox-team")).getByTestId("row-summary"));
    await waitFor(() => expect(posts(daemon, "/inbox/read").map((r) => r.body)).toEqual([{ item_keys: ["signoffs:ad_5"] }]));
    await waitFor(() => expect(row("signoff_progress").dataset.read).toBe("true"));
  });

  it("opens a thread row inline with the Library's thread and posts a reply to that thread", async () => {
    const { client } = mount([comment(11, "u_chen", "c:11")]);
    const design = {
      id: "ad_7", rev: 3,
      threads: [{ id: 11, rev: 3, kind: "comment", status: "open", at: 0, author: "Chen Wei", text: "Count retries against the limit?", anchor: { kind: "text", quote: "Retry up to 5 times" },
        replies: [{ id: 1, at: 0, author: "Sam Ortiz", text: "No." }] }],
    } as unknown as SharedDesignDTO;
    vi.spyOn(client, "sharedDesign").mockResolvedValue(design);
    const write = vi.spyOn(client, "writeSharedDesign").mockResolvedValue(design);
    fireEvent.click(within(await screen.findByTestId("inbox-team")).getByTestId("row-summary"));
    const body = await within(row("shared_doc_comment")).findByTestId("thread-form");
    expect(within(body).getAllByTestId("thread-msg")).toHaveLength(2);
    fireEvent.change(within(body).getByTestId("reply-box"), { target: { value: "A separate cap." } });
    fireEvent.click(within(body).getByTestId("reply-send"));
    await waitFor(() => expect(write).toHaveBeenCalledWith("ad_7", "comments", { rev: 3, parent: 11, text: "A separate cap." }));
  });

  it("Done (E) clears the open row and keeps it under Done today", async () => {
    const { daemon, view } = mount([asked, review]);
    await waitFor(() => expect(row("question").dataset.open).toBe("true"));
    fireEvent.keyDown(window, { key: "j" });
    expect(row("review_requested").dataset.open).toBe("true");
    fireEvent.keyDown(window, { key: "e" });
    await waitFor(() => expect(posts(daemon, "/inbox/ad_2/dismiss")).toHaveLength(1));
    await waitFor(() => expect(row("review_requested")).toBeNull());
    // Leaving the Inbox and coming back: the line is under Done today.
    cleanup();
    view();
    const done = await screen.findByTestId("done-today");
    expect(within(done).getAllByTestId("done-entry").map((d) => [d.dataset.key, d.dataset.what])).toEqual([[expect.stringContaining("review_requested"), "Done"]]);
  });
});
