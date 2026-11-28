import { connectDaemon, inTestContext } from "../../test/harness";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DaemonClient } from "../../api/client";
import { scopesFor } from "../../api/permissions";
import type { AskGroupDTO, InboxItemDTO, SessionDTO, SharedDesignDTO } from "../../api/schema";
import { FakeDaemon } from "../../test/fakeDaemon";
import { REPO, permission, question, root, worker } from "../../test/fixtures";
import { InboxScreen } from "./InboxScreen";
import { resetTriage } from "./queue";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
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

/* The Inbox as everyone sees it by default: the inbox_queue_v2 switch on. */
function mount(inbox: InboxItemDTO[], data: ConstructorParameters<typeof FakeDaemon>[0] = {}, wrap?: (daemon: FakeDaemon, client: DaemonClient) => DaemonClient) {
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox, ...data });
  const { client, store, sync } = connectDaemon(daemon, wrap ? wrap(daemon, daemon.client()) : daemon.client());
  const onOpen = vi.fn();
  const openUrl = vi.fn(async () => {});
  const view = () => render(inTestContext(<InboxScreen client={client} refresh={sync.refresh} onOpen={onOpen} onNavigate={vi.fn()} openUrl={openUrl} />, { client, store }, { daemon: true, sessions: true }));
  view();
  return { daemon, client, sync, view, onOpen, openUrl };
}

const row = (kind: string) => document.querySelector<HTMLElement>(`[data-testid="inbox-row"][data-kind="${kind}"]`)!;
/* The row open, where its answers are (the first waiting row opens by itself). */
const opened = async (kind: string) => {
  await waitFor(() => expect(row(kind)).toBeTruthy());
  if (!row(kind).dataset.open) fireEvent.click(within(row(kind)).getByTestId("row-summary"));
  return row(kind);
};
const posts = (daemon: FakeDaemon, path: string) => daemon.requests.filter((r) => r.method === "POST" && r.path === path);

describe("the Inbox queue", () => {
  it("gives the title its own line before the action in a narrow queue", async () => {
    let resize: ((width: number) => void) | undefined;
    vi.stubGlobal("ResizeObserver", class {
      constructor(private callback: ResizeObserverCallback) {}

      observe(target: Element) {
        resize = (width) => this.callback(
          [{ target, contentRect: { width } } as ResizeObserverEntry],
          this as unknown as ResizeObserver,
        );
      }

      disconnect() {}
    });
    mount([comment(11, "u_chen", "c:11")]);
    const item = await screen.findByTestId("inbox-row");

    act(() => resize?.(408));

    expect(item.dataset.layout).toBe("stacked");
    const summary = within(item).getByTestId("row-summary");
    const title = summary.querySelector('[data-field="title"]')!;
    const action = within(summary).getByTestId("row-go");
    expect(title.compareDocumentPosition(action)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);

    act(() => resize?.(1088));

    expect(item.dataset.layout).toBe("inline");
  });

  it.each(["grant", "decline"])("resolves a reader's requested access from the author Inbox: %s", async (action) => {
    let resolution: string | null = null;
    mount([{ kind: "access_requested", reason: "Asked to edit", doc: "ad_1", detail: "Plan",
      item_key: "access:ad_1:reader", access_role: "edit", from_person: { sub: "reader", name: "Reader" } }], {}, (_daemon, client) => {
      client.writeSharedDesign = async (_id, tail, body) => {
        if (body.role === "edit") resolution = tail.endsWith("/grant") ? "grant" : "decline";
        return {} as SharedDesignDTO;
      };
      return client;
    });
    const request = await opened("access_requested");
    await act(async () => fireEvent.click(within(request).getByRole("button", { name: action === "grant" ? "Grant edit access" : "Decline" })));
    expect(resolution).toBe(action);
  });

  it.each(["passed", "failed", "running", null])("renders the PR's actual checks state: %s", async (checks) => {
    const pr = { kind: "pr_ready", reason: "PR ready", agent_id: "sup1", root_id: "sup1", workspace_id: "proj",
      file_count: 14, checks_state: checks } as InboxItemDTO;
    mount([pr]);
    const ready = await opened("pr_ready");
    expect(ready.querySelectorAll(".iq-ok")).toHaveLength(checks === "passed" ? 2 : 0);
    const states = ready.querySelectorAll<HTMLElement>("[data-checks]");
    expect([...states].map((s) => s.dataset.checks)).toEqual(checks ? [checks, checks] : []);
    expect(ready.querySelector<HTMLElement>("[data-file-count]")?.dataset.fileCount).toBe("14");
  });

  it("shows the review's decision count and the notice's sender", async () => {
    mount([{ ...review, decision_count: 3 } as InboxItemDTO,
      { kind: "org_notice", reason: "Policy updated", notice_id: "n1", from_name: "Priya Nair" } as InboxItemDTO]);
    await opened("review_requested");
    expect(row("review_requested").querySelector<HTMLElement>("[data-decision-count]")?.dataset.decisionCount).toBe("3");
    expect(row("org_notice").querySelector("[data-notice-sender]")?.textContent).toBe("Priya Nair");
  });

  it("puts each row in its part, threads on one design in one row, sign-off progress as dots", async () => {
    mount([{ ...progress, from_person: { sub: "u_chen", name: null }, signoff: { ...progress.signoff!, waiting: [{ sub: "u_sam", name: null }, { sub: "unknown", name: null }] } },
      { ...comment(11, "u_chen", "c:11"), from_person: { sub: "u_chen", name: null } }, review, asked, comment(12, "u_sam", "c:12"),
      { kind: "shared_with_you", reason: "Shared with you", doc: "ad_feedback", share_role: "comment" }], {}, (_daemon, client) => {
      vi.spyOn(client, "libraryPeople").mockResolvedValue({ people: [{ sub: "u_chen", first_name: "Chen", last_name: "Wei", email: "chen@example.test" }, { sub: "u_sam", email: "sam@example.test" }] });
      return client;
    });
    const waiting = await screen.findByTestId("inbox-waiting");
    const team = screen.getByTestId("inbox-team");
    // What's stopped comes first: the agent's question before the review request.
    expect(within(waiting).getAllByTestId("inbox-row").map((r) => r.dataset.kind)).toEqual(["question", "review_requested", "shared_with_you"]);
    expect(within(team).getAllByTestId("inbox-row").map((r) => r.dataset.kind)).toEqual(["signoff_progress", "shared_doc_comment"]);
    const pips = within(row("signoff_progress")).getByTestId("signoff-pips");
    expect([pips.dataset.signed, pips.dataset.required]).toEqual(["2", "3"]);
    expect(pips.querySelectorAll("i.on")).toHaveLength(2);
    await waitFor(() => expect(row("signoff_progress").textContent).toContain("Chen Wei"));
    expect(row("signoff_progress").textContent).toContain("sam@example.test");
    expect(row("signoff_progress").textContent).toContain("A teammate");
    expect(row("shared_doc_comment").textContent).toContain("Chen Wei");
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
    await waitFor(() => expect(write).toHaveBeenCalledWith("ad_7", "comments", { rev: 3, parent: 11, text: "A separate cap.", repo: REPO }));
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

  it("an answer the engine refuses keeps the row open with the text and one line why", async () => {
    const { client } = mount([asked]);
    vi.spyOn(client, "inboxAction").mockRejectedValue(new Error("the session closed meanwhile"));
    const box = within(await opened("question")).getByTestId("answer-box");
    fireEvent.change(box, { target: { value: "Use Redis." } });
    fireEvent.click(within(row("question")).getByTestId("answer-send"));
    await waitFor(() => expect(within(row("question")).getByRole("alert").textContent).toContain("the session closed meanwhile"));
    expect(row("question").dataset.open).toBe("true");
    expect((within(row("question")).getByTestId("answer-box") as HTMLTextAreaElement).value).toBe("Use Redis.");
    expect(document.querySelector("[data-testid=inbox-line]")).toBeNull();
  });

  // Deciding on the cards themselves: InboxScreen.test.tsx's permission tests.
  it("permission cards lead Waiting on you, above the rows", async () => {
    const limits = { max_scope: "destination", life: "time", minutes: 30 };
    const pushes: AskGroupDTO = {
      key: `${REPO}|push|* → main @ origin`, repo: REPO, kind: "push", title: "Allow pushes into main?", general: scopesFor("push", "fix/login → main @ origin")[1],
      scopes: scopesFor("push", "fix/login → main @ origin"), blocked: false, rule: "d-push-default", limits, modes: ["once", "deny", "always-deny", "timed"],
      items: [{ agent_id: "w1", root_id: "sup1", request_id: "req-9", digest: "d-9", repo: REPO, kind: "push", tool: "Bash", exact: "fix/login → main @ origin", summary: "git push",
        waiting_since: Date.now() / 1000 - 60, worker: "developer", rule: "d-push-default", blocked: false, limits }],
    };
    mount([permission, asked], { asks: [pushes] });
    const held = await screen.findByRole("article", { name: "Allow pushes into main?" });
    expect(screen.getByTestId("inbox-waiting").contains(held)).toBe(true);
    await waitFor(() => expect(row("question")).toBeTruthy());
    expect(held.compareDocumentPosition(row("question")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("Enter on a focused control is that control's, never the open row's action (no PR opened from Later)", async () => {
    const prReady: InboxItemDTO = { kind: "pr_ready", reason: "PR ready for your approval", agent_id: "sup1", workspace_id: "proj", root_id: "sup1", detail: "Ship ls --json" };
    const { client } = mount([prReady], { workspaces: [root] });
    const draft = vi.spyOn(client, "prDraft");
    const later = within(await opened("pr_ready")).getByTestId("row-later");
    later.focus();
    expect(fireEvent.keyDown(later, { key: "Enter" })).toBe(true);
    expect(draft).not.toHaveBeenCalled();
  });

  it("a new question from the agent just answered shows on the same screen", async () => {
    const { daemon, sync } = mount([asked]);
    const box = within(await opened("question")).getByTestId("answer-box");
    fireEvent.change(box, { target: { value: "Use Redis." } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(document.querySelector<HTMLElement>("[data-testid=inbox-line]")?.dataset.what).toBe("Answered"));
    expect(row("question")).toBeNull();
    // The same worker asks again (same agent, so the same row key): a row again, not hidden behind the line.
    daemon.data.inbox = [{ ...asked, detail: "And how long should entries live?", request_id: "2" }];
    await act(async () => sync.refresh());
    await waitFor(() => expect(row("question")?.textContent).toContain("And how long should entries live?"));
    sync.stop();
  });

  it("a row set aside comes back when its time is up", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mount([progress]);
      await waitFor(() => expect(row("signoff_progress")).toBeTruthy());
      fireEvent.click(within(row("signoff_progress")).getByTestId("row-later"));
      fireEvent.click(within(screen.getByTestId("later-menu")).getAllByRole("menuitem")[0]);   // In an hour
      expect(row("signoff_progress")).toBeNull();
      await act(async () => {
        vi.advanceTimersByTime(61 * 60 * 1000);
      });
      await waitFor(() => expect(row("signoff_progress")).toBeTruthy());
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["row", "all"])("Done on a team row the Library refuses keeps its error until marked read: %s", async (scope) => {
    const { client } = mount([progress, comment(11, "u_chen", "c:11")]);
    vi.spyOn(client, "markInboxRead").mockRejectedValueOnce(new Error("Library refused the read mark"));
    await waitFor(() => expect(row("signoff_progress")).toBeTruthy());
    fireEvent.click(within(row("signoff_progress")).getByTestId("row-done"));
    await waitFor(() => expect(within(row("signoff_progress")).getByRole("alert")).toBeTruthy());
    expect(within(row("shared_doc_comment")).queryByRole("alert")).toBeNull();
    expect(document.querySelector("[data-testid=inbox-line]")).toBeNull();
    await opened("shared_doc_comment");
    await waitFor(() => expect(row("shared_doc_comment").dataset.read).toBe("true"));
    expect(within(row("signoff_progress")).getByRole("alert")).toBeTruthy();
    if (scope === "row") await opened("signoff_progress");
    else fireEvent.click(screen.getByTestId("mark-all-read"));
    await waitFor(() => expect(row("signoff_progress").dataset.read).toBe("true"));
    expect(within(row("signoff_progress")).queryByRole("alert")).toBeNull();
  });

  it("a team row's Done is sent once however often it is pressed, and a late answer never hides a row put back since", async () => {
    const { client } = mount([progress]);
    const answers: ((v: { read: string[] }) => void)[] = [];
    const read = vi.spyOn(client, "markInboxRead").mockImplementation(() => new Promise((resolve) => answers.push(resolve)));
    await waitFor(() => expect(row("signoff_progress")).toBeTruthy());
    const press = within(row("signoff_progress")).getByTestId("row-done");
    // Both presses land before React commits anything: only the guard can stop the second.
    act(() => {
      fireEvent.click(press);
      fireEvent.click(press);
    });
    expect(read).toHaveBeenCalledTimes(1);
    await act(async () => answers[0]({ read: ["signoffs:ad_5"] }));
    fireEvent.click(await screen.findByTestId("put-back"));
    expect(row("signoff_progress")).toBeTruthy();

    // Done again, then set aside and put back before the Library answers: its late answer clears nothing.
    fireEvent.click(within(row("signoff_progress")).getByTestId("row-done"));
    expect(read).toHaveBeenCalledTimes(2);
    fireEvent.click(within(row("signoff_progress")).getByTestId("row-later"));
    fireEvent.click(within(screen.getByTestId("later-menu")).getAllByRole("menuitem")[0]);
    fireEvent.click(await screen.findByTestId("put-back"));
    await act(async () => answers[1]({ read: ["signoffs:ad_5"] }));
    expect(row("signoff_progress")).toBeTruthy();
    expect(document.querySelector("[data-testid=inbox-line]")).toBeNull();
  });

  it("Done on one teammate's requests settles only when every dismissal is taken; a refused one stays, with its reason", async () => {
    const second: InboxItemDTO = { ...review, doc: "ad_3", detail: "Audit log retention", item_key: "review:ad_3" };
    const { client } = mount([review, second]);
    const real = client.inboxAction.bind(client);
    vi.spyOn(client, "inboxAction").mockImplementation((id, action, body) => (id === "ad_3" ? Promise.reject(new Error("refused by the engine")) : real(id, action, body)));
    fireEvent.click(within(await opened("review_requested")).getByTestId("row-done"));
    await waitFor(() => expect(within(row("review_requested")).getByRole("alert").textContent).toContain("refused by the engine"));
    expect(row("review_requested").textContent).toContain("Audit log retention");
    expect(document.querySelector("[data-testid=inbox-line]")).toBeNull();
  });

});
