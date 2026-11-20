import { connectDaemon, inTestContext } from "../../test/harness";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DaemonClient } from "../../api/client";
import { scopesFor } from "../../api/permissions";
import type { AskGroupDTO, GoalDTO, InboxItemDTO, SessionDTO, SharedDesignDTO } from "../../api/schema";
import { FakeDaemon } from "../../test/fakeDaemon";
import { REPO, permission, question, root, worker } from "../../test/fixtures";
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
const sent = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "POST").map((r) => r.path);

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

  it("Done dismisses through the daemon, and the row stays gone after another snapshot", async () => {
    const { daemon, sync } = mount([asked]);
    fireEvent.click(within(await opened("question")).getByTestId("row-done"));
    await waitFor(() => expect(row("question")).toBeNull());
    expect(daemon.data.inbox).toEqual([]);
    await act(async () => sync.refresh());
    expect(row("question")).toBeNull();
    sync.stop();
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

  it("permission cards lead Waiting on you: Allow once decides the held group, and a refused Deny shows on its card", async () => {
    const limits = { max_scope: "destination", life: "time", minutes: 30 };
    const pushes: AskGroupDTO = {
      key: `${REPO}|push|* → main @ origin`, repo: REPO, kind: "push", title: "Allow pushes into main?", general: scopesFor("push", "fix/login → main @ origin")[1],
      scopes: scopesFor("push", "fix/login → main @ origin"), blocked: false, rule: "d-push-default", limits, modes: ["once", "deny", "always-deny", "timed"],
      items: [{ agent_id: "w1", root_id: "sup1", request_id: "req-9", digest: "d-9", repo: REPO, kind: "push", tool: "Bash", exact: "fix/login → main @ origin", summary: "git push",
        waiting_since: Date.now() / 1000 - 60, worker: "developer", rule: "d-push-default", blocked: false, limits }],
    };
    const { daemon } = mount([permission, asked], { asks: [pushes] });
    const held = await screen.findByRole("article", { name: "Allow pushes into main?" });
    const waitingPart = screen.getByTestId("inbox-waiting");
    expect(waitingPart.contains(held)).toBe(true);
    // Permissions first, then the rows.
    expect(held.compareDocumentPosition(row("question")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(within(held).getByRole("button", { name: "Allow once" }));
    await waitFor(() => expect(posts(daemon, "/permissions/asks/decide")[0]?.body).toEqual({ key: pushes.key, scope: "general", mode: "once" }));
    // The request the item card shows is gone from the engine: Deny's refusal is the line on that card.
    const card = (await screen.findAllByTestId("permission-card")).find((c) => c.dataset.source === "hook" && c !== held)!;
    daemon.data.inbox = daemon.data.inbox.filter((i) => i.kind !== "permission");
    fireEvent.click(within(card).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(within(card).getByRole("alert")).toBeTruthy());
  });

  it("Open PR opens the prepared PR in one press; when gh can't, the row stays with the compare page", async () => {
    const prReady: InboxItemDTO = { kind: "pr_ready", reason: "PR ready for your approval", agent_id: "sup1", workspace_id: "proj", root_id: "sup1", detail: "Ship ls --json" };
    const { daemon, openUrl } = mount([prReady], { workspaces: [root] });
    daemon.readyForPr("sup1");
    fireEvent.click(within(await opened("pr_ready")).getByTestId("open-pr"));
    await waitFor(() => expect(daemon.prs.map((p) => p.title)).toEqual(["Ship ls --json"]));
    await waitFor(() => expect(row("pr_opened")).toBeTruthy());
    expect(row("pr_ready")).toBeNull();
    cleanup();

    const again = mount([prReady], { workspaces: [root] });
    again.daemon.readyForPr("sup1");
    vi.spyOn(again.client, "openSessionPr").mockResolvedValue({ job: "j9", kind: "session.pr" } as never);
    fireEvent.click(within(await opened("pr_ready")).getByTestId("open-pr"));
    await waitFor(() => expect(again.client.openSessionPr).toHaveBeenCalled());
    const compare = "https://github.com/o/r/compare/main...sess?expand=1";
    act(() => again.daemon.publish("job.done", { root_id: "sup1", agent_id: "sup1", payload: { job: "j9", job_kind: "session.pr", result: { url: compare } } }));
    await waitFor(() => expect(within(row("pr_ready")).getByRole("alert")).toBeTruthy());
    fireEvent.click(within(row("pr_ready")).getByRole("button", { name: "Open on GitHub" }));
    expect(again.openUrl).toHaveBeenCalledWith(compare);
    expect(openUrl).not.toHaveBeenCalled();
  });

  it("a plan offer the engine refuses keeps its row with the refusal and opens nothing", async () => {
    const context = { session_name: "Command tools", goal: "Ship ls --json", detail: null, workers: 3, repos: 2, milestones: [{ title: "Parse arguments", verified: true }], reasons: ["This work spans 2 repositories."] };
    const offer: InboxItemDTO = { kind: "plan_offer", agent_id: "sup1", root_id: "sup1", workspace_id: "proj", request_id: "o1", reason: "This looks big: plan it first?", detail: context.goal, plan_offer: context };
    const { onOpen } = mount([offer], { goal: { root_id: "sup1", plan_offer: "o1" } as GoalDTO }, (daemon, client) => new DaemonClient({ baseUrl: client.baseUrl, token: client.token, fetch: async (input, init) => {
      if (String(input).endsWith("/goal/plan-offer")) throw new Error("offer unavailable");
      return daemon.fetch(input, init);
    } }));
    const yes = within(await opened("plan_offer")).getByRole("button", { name: "Yes" });
    act(() => {
      fireEvent.click(yes);
      fireEvent.click(yes);
    });
    expect(await within(row("plan_offer")).findByText("offer unavailable")).toBeTruthy();
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("a setup Fix that fails shows the job's error on its row and can be pressed again; done, the row goes", async () => {
    const setupItem: InboxItemDTO = { kind: "setup", fix: "codex_hook_trust", reason: "Codex's trust of brindle's permission hook was removed", action: { method: "POST", path: "/v1/setup/codex-hook" } };
    const { daemon } = mount([setupItem]);
    daemon.deferFixes = true;
    daemon.fixFails = true;
    fireEvent.click(within(await opened("setup")).getByTestId("setup-fix"));
    await waitFor(() => expect(within(row("setup")).getByTestId("setup-fix")).toHaveProperty("disabled", true));
    daemon.completeFixes();
    await waitFor(() => expect(within(row("setup")).getByRole("alert")).toBeTruthy());
    expect(within(row("setup")).getByTestId("setup-fix")).toHaveProperty("disabled", false);
    daemon.fixFails = false;
    fireEvent.click(within(row("setup")).getByTestId("setup-fix"));
    await waitFor(() => expect(daemon.fixes).toHaveLength(2));
    daemon.completeFixes();
    await waitFor(() => expect(row("setup")).toBeNull());
    expect(sent(daemon)).toEqual(["/setup/codex-hook", "/setup/codex-hook"]);
  });

  it("proposed checks: Decline at once, and the engine's refusal comes back as one line on the row", async () => {
    const checks: InboxItemDTO = { kind: "proposed_checks", reason: "checks proposed", agent_id: "sup1", workspace_id: "proj", root_id: "sup1", detail: "npm test\nnpm run build" };
    const { daemon } = mount([checks]);
    const open = await opened("proposed_checks");
    expect(within(open).getByTestId("card-detail").textContent).toBe("npm test\nnpm run build");
    daemon.data.inbox = [];
    fireEvent.click(within(open).getByRole("button", { name: "Decline" }));
    await waitFor(() => expect(within(row("proposed_checks")).getByRole("alert")).toBeTruthy());
  });

  it("a lapsed sign-in starts the provider's own login from its row", async () => {
    const codexWorker = { ...worker, agents: [{ ...worker.agents![0], provider: "codex", profile: "developer-codex" }] };
    const credentials: InboxItemDTO = { kind: "credentials", reason: "credentials need attention", agent_id: "w1", workspace_id: "proj/feat-ls", root_id: "sup1", detail: null, action: { method: "POST", path: "/v1/providers/codex/login" } };
    const { daemon } = mount([credentials], { workspaces: [root, codexWorker] });
    const open = await opened("credentials");
    expect(within(open).queryByRole("button", { name: "Dismiss" })).toBeNull();
    fireEvent.click(within(open).getByRole("button", { name: "Sign in to Codex" }));
    await waitFor(() => expect(daemon.actions.map((a) => a.kind)).toEqual(["login codex"]));
  });
});
