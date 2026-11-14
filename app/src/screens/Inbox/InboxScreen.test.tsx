import { connectDaemon, inTestContext, inTestStore } from "../../test/harness";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonClient } from "../../api/client";
import { scopesFor } from "../../api/permissions";
import type { AskDTO, AskDecisionDTO, AskGroupDTO, GoalDTO, InboxItemDTO, SessionDTO } from "../../api/schema";
import type { AppUpdateStatus, AppUpdates } from "../../api/tauri";
import { AppUpdateBanner, AppUpdateProvider } from "../../components/AppUpdateBanner";
import { BASE_URL, FakeDaemon, TOKEN } from "../../test/fakeDaemon";
import { REPO, permission, permissionInTerminal, question, root, waiting, worker } from "../../test/fixtures";
import { Permissions } from "../Settings/Permissions";
import { InboxScreen, type InboxScreenProps } from "./InboxScreen";
import { tauriSink, watchInbox } from "./notify";
import { usePermissionAsks } from "./PermissionAsks";

afterEach(cleanup);

const notice: InboxItemDTO = { kind: "org_notice", reason: "Reviewer profiles now default to reviewer-codex.", detail: "admin", notice_id: "n-18" };
const prOpened: InboxItemDTO = { kind: "pr_opened", reason: "PR opened", agent_id: "sup1", workspace_id: "proj", root_id: "sup1", detail: "#408 Ship ls --json", url: "https://github.com/o/r/pull/408" };
const prReady: InboxItemDTO ={ kind: "pr_ready", reason: "PR ready for your approval", agent_id: "sup1", workspace_id: "proj", root_id: "sup1", detail: "Ship ls --json" };
const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };

function mount(daemon: FakeDaemon, client: DaemonClient = daemon.client(), props: Partial<InboxScreenProps> = {}) {
  const { store, sync } = connectDaemon(daemon, client);
  const onOpen = vi.fn();
  const onNavigate = vi.fn();
  const onOpenRule = vi.fn();
  render(
    inTestContext(<><InboxScreen client={client} refresh={sync.refresh} onOpen={onOpen} onNavigate={onNavigate} onOpenRule={onOpenRule} {...props} /></>, { client, store }, { daemon: true, sessions: true }),
  );
  return { store, sync, onOpen, onNavigate, onOpenRule };
}

/* Held requests as GET /permissions/asks groups them: two pushes into main (w1's is the `permission` fixture's
   hold) under the push rule's 30-minute limit, and a read the credential deny blocks. */
const now = Date.now() / 1000;
const limits = { max_scope: "destination", life: "time", minutes: 30 };
const ask = (agent_id: string, request_id: string, kind: string, exact: string, since: number, rule: string, blocked = false): AskDTO => ({
  agent_id, root_id: "sup1", request_id, digest: `d-${request_id}`, repo: REPO, kind, tool: kind === "read" ? "Read" : "Bash", exact,
  summary: exact, waiting_since: now - since, worker: agent_id === "w1" ? "developer" : "supervisor", rule, blocked, limits: blocked ? null : limits,
});
const pushes: AskGroupDTO = {
  key: `${REPO}|push|* → main @ origin`, repo: REPO, kind: "push", title: "Allow pushes into main?", general: scopesFor("push", "fix/login → main @ origin")[1],
  scopes: scopesFor("push", "fix/login → main @ origin"), blocked: false, rule: "d-push-default", limits, modes: ["once", "deny", "always-deny", "timed"],
  items: [ask("w1", "req-1", "push", "fix/login → main @ origin", 600, "d-push-default"), ask("sup1", "req-2", "push", "chore/ci → main @ origin", 120, "d-push-default")],
};

const posts = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "POST");
/* The one card an item gets, whatever its kind. */
const card = (kind: string) => document.querySelector<HTMLElement>(`[data-testid="inbox-card"][data-kind="${kind}"]`)!;

describe("inbox", () => {
  it("keeps a deferred update actionable, then replaces the offer with the installed card", async () => {
    const updates = { updateNow: async () => null } as unknown as AppUpdates;
    const { store } = mount(new FakeDaemon(), undefined, { appUpdates: updates });
    render(inTestStore(<><AppUpdateProvider updates={updates}><AppUpdateBanner openUrl={async () => {}} /></AppUpdateProvider></>, store));
    const status: AppUpdateStatus = { version: "0.6.2", automatic: true, available: { version: "0.6.3", notes: null }, pending: null, updated: null, later: false, checking: false, last_checked: null };
    act(() => store.dispatch({ type: "app_update", status }));
    expect(screen.getByTestId("app-update-now")).toBeTruthy();
    act(() => store.dispatch({ type: "app_update", status: { ...status, later: true } }));
    fireEvent.click(screen.getByTestId("app-update-now"));
    expect(screen.getByTestId("app-update-banner").dataset.phase).toBe("downloading");
    act(() => store.dispatch({ type: "app_update", status: { ...status, pending: "0.6.3" } }));
    expect(screen.getByTestId("app-update-card").dataset.kind).toBe("pending");
    expect(screen.queryByTestId("app-update-banner")).toBeNull();
    await act(async () => {});
  });

  it("inbox dismiss clears the PR card while a read from before Dismiss finishes", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], inbox: [prReady, { ...prReady, kind: "pr_failed" }] });
    const client = daemon.client(), reads: ((items: InboxItemDTO[]) => void)[] = [];
    const { sync } = mount(daemon, client);
    await screen.findByRole("region", { name: "Ship ls --json" }); await act(async () => sync.idle()); vi.spyOn(client, "inbox").mockImplementation(() => new Promise((resolve) => reads.push(resolve))); act(() => { void sync.refresh(); });
    expect(within(card("pr_failed")).getByRole("button", { name: "Dismiss" })).toBeTruthy();
    fireEvent.click(within(card("pr_ready")).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(posts(daemon)).toEqual([{ method: "POST", path: "/inbox/sup1/pr-not-now", query: {}, body: {} }]));
    await waitFor(() => expect(card("pr_ready")).toBeNull());
    await act(async () => reads[0]([prReady, { ...prReady, kind: "pr_failed" }])); expect(card("pr_ready")).toBeNull(); await act(async () => { reads[1](daemon.data.inbox); await sync.idle(); });
    expect(card("pr_failed")).toBeTruthy();
  });

  it("dismisses a failed PR and opens an existing PR on GitHub", async () => {
    const failed: InboxItemDTO = { ...prOpened, kind: "pr_failed" };
    const daemon = new FakeDaemon({ sessions: [session], workspaces: [root], inbox: [failed] });
    const openUrl = vi.fn(async () => {});
    const { sync, onOpen } = mount(daemon, undefined, { openUrl });
    await waitFor(() => expect(card("pr_failed")).toBeTruthy());
    const buttons = within(card("pr_failed"));
    expect(buttons.getAllByRole("button")).toHaveLength(2);
    fireEvent.click(buttons.getByRole("button", { name: "Open on GitHub" }));
    expect(openUrl).toHaveBeenCalledWith(failed.url);
    expect(onOpen).not.toHaveBeenCalled();
    // Opening it on GitHub dismisses the card.
    await waitFor(() => expect(card("pr_failed")).toBeNull());
    sync.stop();
    cleanup();
    daemon.data.inbox = [{ ...failed, url: null }];
    const { sync: noReviewSync } = mount(daemon, undefined, { onOpen: undefined });
    await waitFor(() => expect(card("pr_failed")).toBeTruthy());
    expect(within(card("pr_failed")).getAllByRole("button")).toHaveLength(1);
    fireEvent.click(within(card("pr_failed")).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(card("pr_failed")).toBeNull());
    noReviewSync.stop();
  });

  it("dismisses an opened PR without a URL", async () => {
    const daemon = new FakeDaemon({ sessions: [session], workspaces: [root], inbox: [{ ...prOpened, url: null }] });
    mount(daemon);
    await waitFor(() => expect(card("pr_opened")).toBeTruthy());
    expect(within(card("pr_opened")).getAllByRole("button")).toHaveLength(1);
    fireEvent.click(within(card("pr_opened")).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(card("pr_opened")).toBeNull());
  });

  it.each([["rewound", "card"], ["dismissed", "card"], ["rewound", "permission group"], ["dismissed", "permission group"]])("omits stale Inbox %s worker %s", async (reason, surface) => {
    const past = { ...worker.agents![0], status: reason === "rewound" ? "rewound" : "exited", dismissed_at: reason === "dismissed" ? 1 : null };
    const daemon = new FakeDaemon({ sessions: [session], workspaces: [root, { ...worker, agents: [past] }], inbox: [question, waiting], asks: surface === "permission group" ? [{ ...pushes, items: [pushes.items[0]] }] : [] });
    const { store } = mount(daemon);
    await waitFor(() => expect(store.getState().synced).toBe(true));
    await act(async () => {});
    expect(screen.queryByTestId("permission-card")).toBeNull();
    expect(card("waiting")).toBeNull();
    expect(card("question")).toBeTruthy();
  });

  it.each(["reload", "inbox", "client"])("discards older permission groups after a newer %s response", async (trigger) => {
    const client = new FakeDaemon().client();
    const other = new FakeDaemon().client();
    const answers: ((groups: AskGroupDTO[]) => void)[] = [];
    const request = () => new Promise<AskGroupDTO[]>((resolve) => answers.push(resolve));
    vi.spyOn(client, "permissionAsks").mockImplementation(request);
    vi.spyOn(other, "permissionAsks").mockImplementation(request);
    const { result, rerender } = renderHook(({ client, inbox }) => usePermissionAsks(client, inbox), {
      initialProps: { client, inbox: [permission] },
    });
    await act(async () => answers[0]([pushes]));
    expect(result.current.groups).toEqual([pushes]);
    let older!: Promise<void>;
    act(() => { older = result.current.reload(); });
    let newer: Promise<void> | undefined;
    if (trigger === "reload") act(() => { newer = result.current.reload(); });
    else {
      const previousReload = result.current.reload;
      rerender({ client: trigger === "client" ? other : client, inbox: [] });
      // A decision started on the old daemon can finish after switching.
      if (trigger === "client") act(() => { void previousReload(); });
    }
    await act(async () => { answers[2]([]); await newer; });
    expect(result.current.groups).toEqual([]);
    await act(async () => { answers[1]([pushes]); await older; });
    expect(result.current.groups).toEqual([]);
  });

  it.each([42, "d1"])("opens requested document %s at its section, replies in the session and dismisses the request", async (reference) => {
    const doc: InboxItemDTO = { kind: "doc", reason: "Check the storage design.", agent_id: "w1", workspace_id: "proj/feat-ls", root_id: "sup1", request_id: "dr1", doc: reference, section: "Storage" };
    // A paused session remains in the sessions feed after all its agents leave the snapshot.
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [{ ...session, status: typeof reference === "string" ? "paused" : "idle" }], workspaces: typeof reference === "string" ? [] : [root, worker], inbox: [doc] });
    const { onOpen, onNavigate } = mount(daemon);
    await screen.findByRole("region", { name: "Ship ls --json" });
    fireEvent.click(within(card("doc")).getByRole("button", { name: "Open" }));
    expect(onNavigate).toHaveBeenLastCalledWith({ screen: "knowledge", view: "docs", doc: reference, section: "Storage" });
    fireEvent.click(within(card("doc")).getByRole("button", { name: "Reply" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "w1");
    fireEvent.click(within(card("doc")).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(posts(daemon)).toEqual([{ method: "POST", path: "/inbox/dr1/dismiss", query: {}, body: {} }]));
    await waitFor(() => expect(card("doc")).toBeNull());
  });

  it("review requested card opens the shared doc, and the author's cards open it too", async () => {
    const asked: InboxItemDTO = { kind: "review_requested", reason: "Review requested", detail: "Org presets", doc: "lib-1", request_id: "p1" };
    const changes: InboxItemDTO = { kind: "changes_requested", reason: "Changes requested", detail: "Other plan", doc: "lib-2", request_id: "p2" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], inbox: [asked, changes] });
    const { sync, onNavigate } = mount(daemon);
    await waitFor(() => expect(card("review_requested")).toBeTruthy());
    expect(card("changes_requested")).toBeTruthy();
    fireEvent.click(within(card("review_requested")).getByRole("button", { name: "Open" }));
    expect(onNavigate).toHaveBeenLastCalledWith({ screen: "shared-design", doc: "lib-1", title: "Org presets", repo: REPO });
    fireEvent.click(within(card("changes_requested")).getByRole("button", { name: "Open" }));
    expect(onNavigate).toHaveBeenLastCalledWith({ screen: "knowledge", view: "docs", doc: "lib-2" });
    sync.stop();
  });

  it("groups the queue by session as banner cards, each going to its session and agent; an org notice is a card to read", async () => {
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }],
      sessions: [session],
      workspaces: [root, worker],
      inbox: [question, waiting, notice, prReady],
    });
    const openUrl = vi.fn(async () => {});
    const { onOpen } = mount(daemon, daemon.client(), { openUrl });
    const group = await screen.findByRole("region", { name: "Ship ls --json" });
    expect(within(group).getAllByTestId("inbox-card")).toHaveLength(3);
    // The PR brindle prepared (its checks passed, nothing pushed): one press of Open PR opens it, no review sheet.
    expect(card("pr_ready").textContent).toContain("PR ready");
    expect(card("pr_ready").textContent).toContain("Ship ls --json");
    expect(within(card("pr_ready")).getByRole("button", { name: "Dismiss" })).toBeTruthy();
    daemon.readyForPr("sup1");
    fireEvent.click(within(card("pr_ready")).getByRole("button", { name: "Open PR" }));
    expect(within(card("pr_ready")).getByRole("button", { name: "Opening…" })).toHaveProperty("disabled", true);
    await waitFor(() => expect(daemon.prs).toHaveLength(1));
    expect(daemon.prs[0].title).toBe("Ship ls --json");
    // The job ended with a real PR: the card becomes "PR opened" (one line, the PR, Open on GitHub, Dismiss), no review sheet.
    await waitFor(() => expect(card("pr_opened")).toBeTruthy());
    expect(card("pr_ready")).toBeNull();
    expect(card("pr_opened").textContent).toContain("PR opened");
    expect(card("pr_opened").textContent).toContain("#1 Ship ls --json");
    expect(within(card("pr_opened")).queryByRole("button", { name: "Review & open PR" })).toBeNull();
    expect(within(card("pr_opened")).getByRole("button", { name: "Dismiss" })).toBeTruthy();
    fireEvent.click(within(card("pr_opened")).getByRole("button", { name: "Open on GitHub" }));
    expect(openUrl).toHaveBeenCalledWith("https://github.com/example/proj/pull/1");
    expect(onOpen).not.toHaveBeenCalled();
    expect(group.textContent).toContain("sup1 · proj");
    fireEvent.click(within(group).getByRole("button", { name: "Open the session ›" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO);

    // A card: the kind, what is asked, who (the worker by its branch; the supervisor as such), the session, the age,
    // and Go to session: the agent in its session with the item's sheet over it, once; nothing is answered on
    // a card that only links.
    const w = card("waiting");
    expect(w.textContent).toContain("waiting");
    expect(w.textContent).toContain("Waiting on your input in its terminal");
    expect(w.textContent).toContain("ls · feat/ls");
    expect(w.textContent).toContain("answered in its session");
    expect(within(w).queryByRole("group")).toBeNull();
    fireEvent.click(within(w).getByRole("button", { name: "Go to session ›" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "w1");
    expect(card("question").textContent).toContain("supervisor");
    fireEvent.click(within(card("question")).getByRole("button", { name: "Go to session ›" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "sup1");
    expect(screen.queryByRole("dialog")).toBeNull();

    // An org notice has no session: a card to read, with Mark read on it.
    expect(card("org_notice").textContent).toContain("from your org's admin");
    expect(within(card("org_notice")).queryByRole("button", { name: "Go to session ›" })).toBeNull();
    fireEvent.click(within(card("org_notice")).getByRole("button", { name: "Mark read" }));
    await waitFor(() => expect(posts(daemon).filter((p) => p.path.startsWith("/inbox/"))).toEqual([
      { method: "POST", path: "/inbox/sup1/pr-not-now", query: {}, body: {} },   // opening the PR on GitHub dismissed its card
      { method: "POST", path: "/inbox/n-18/read", query: {}, body: {} }]));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Your org" })).toBeNull());
  });

  it("Open PR when gh can't open the PR: no PR exists, so the card stays and offers the compare page; a PR opened elsewhere notifies with its number, title and link", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], inbox: [prReady] });
    daemon.readyForPr("sup1");
    const client = daemon.client(), openUrl = vi.fn(async () => {});
    vi.spyOn(client, "openSessionPr").mockResolvedValue({ job: "j9", kind: "session.pr" } as never);
    const { store } = mount(daemon, client, { openUrl });
    await screen.findByRole("region", { name: "Ship ls --json" });
    fireEvent.click(within(card("pr_ready")).getByRole("button", { name: "Open PR" }));
    await waitFor(() => expect(client.openSessionPr).toHaveBeenCalled());
    const compare = "https://github.com/o/r/compare/main...sess?expand=1";
    act(() => daemon.publish("job.done", { root_id: "sup1", agent_id: "sup1", payload: { job: "j9", job_kind: "session.pr", result: { url: compare } } }));
    await waitFor(() => expect(within(card("pr_ready")).getByRole("alert").textContent).toContain("compare page"));
    fireEvent.click(within(card("pr_ready")).getByRole("button", { name: "Open on GitHub" }));
    expect(openUrl).toHaveBeenCalledWith(compare);

    // The OS notification for an opened PR carries which one and its link.
    const notes: string[] = [];
    const stop = watchInbox(store, { notify: (title, body, link) => notes.push(`${title}|${body}|${link}`), count: () => undefined });
    act(() => store.dispatch({ type: "snapshot", workspaces: [], inbox: [prReady] }));
    act(() => store.dispatch({ type: "snapshot", workspaces: [], inbox: [prReady, prOpened] }));
    expect(notes).toEqual(["Pull request|sup1: PR opened: #408 Ship ls --json · https://github.com/o/r/pull/408|https://github.com/o/r/pull/408"]);
    stop();

    // The real sink: the link rides in the notification, and the Open on GitHub action opens it in the browser.
    let performed: ((e: { actionId: string; notification: { extra?: Record<string, unknown> } | null }) => void) | undefined;
    const shown: unknown[] = [];
    const opened = vi.fn(async () => {});
    const sink = tauriSink({
      isPermissionGranted: async () => true,
      requestPermission: async () => "granted",
      sendNotification: (o) => shown.push(o),
      registerActionTypes: async () => {},
      onAction: async (cb) => { performed = cb; },
    }, opened);
    sink.notify("Pull request", "PR opened", "https://github.com/o/r/pull/408");
    await waitFor(() => expect(shown).toEqual([{ title: "Pull request", body: "PR opened", actionTypeId: "pr-opened", extra: { url: "https://github.com/o/r/pull/408" } }]));
    performed!({ actionId: "open-github", notification: { extra: { url: "https://github.com/o/r/pull/408" } } });
    expect(opened).toHaveBeenCalledWith("https://github.com/o/r/pull/408");
  });

  it("visual ready card opens the reader and dismisses independently on a read refresh", async () => {
    const visualItem: InboxItemDTO = { kind: "visual", agent_id: "sup1", root_id: "sup1", workspace_id: "proj", visual_id: "v_8f2c", reason: "visual is ready", detail: "Flow A" };
    const visualB = { ...visualItem, visual_id: "v_b", detail: "Flow B" };
    const decision = { ...visualItem, visual_id: "v_question", reason: "asks you to choose", detail: "Pick a flow" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [decision, visualItem, visualB, question] });
    const { sync, onOpen } = mount(daemon);
    await screen.findByRole("region", { name: "Ship ls --json" });
    expect(within(card("visual")).queryByRole("button", { name: "Go to session ›" })).toBeNull();
    fireEvent.click(within(screen.getByText("Flow A").closest("article")!).getByRole("button", { name: "Open visual" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "sup1", { kind: "visual", id: "v_8f2c" });
    expect(within(card("question")).getByRole("button", { name: "Go to session ›" }).className).toContain("btn-secondary");
    await act(async () => {
      // While the reader was open, its notice was read and the decision was answered.
      daemon.data.inbox = [visualB, question];
      daemon.publish("visual.read", { root_id: "sup1", payload: { visual: visualItem.visual_id } });
      await sync.idle();
    });
    expect(screen.queryByText("Flow A")).toBeNull();
    expect(screen.getByText("Flow B")).toBeTruthy();
    expect(screen.queryByText("Pick a flow")).toBeNull();
  });

  it("a visual decision whose session hasn't loaded yet waits to open and never reads as answered", async () => {
    const visualItem: InboxItemDTO = { kind: "visual", agent_id: "sup1", root_id: "sup1", workspace_id: "proj", visual_id: "v_8f2c", reason: "asks you to choose" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [], workspaces: [], inbox: [visualItem] });
    mount(daemon);
    await screen.findAllByTestId("inbox-card");
    expect((within(card("visual")).getByRole("button", { name: "Open visual" }) as HTMLButtonElement).disabled).toBe(true);
    expect(card("visual").textContent).not.toContain("answered in its session");
  });

  it.each([[true, "Yes", false], [false, "Not now", false], [true, "Yes", true]])("plan offer opens the session after the answer (failure: %s)", async (accepted, label, fails) => {
    const context = { session_name: "Command tools", goal: "Ship ls --json", detail: "Support shell scripts", workers: 3, repos: 2,
      milestones: [{ title: "Parse arguments", verified: true }, { title: "Encode output", verified: false }],
      reasons: ["This work spans 2 repositories.", "3 workers have been assigned to this goal."] };
    const offer: InboxItemDTO = { kind: "plan_offer", agent_id: "sup1", root_id: "sup1", workspace_id: "proj", request_id: "o1", reason: "This looks big: plan it first?", detail: context.goal, plan_offer: context };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [offer], goal: { root_id: "sup1", plan_offer: "o1" } as GoalDTO });
    const client = daemon.client();
    const actionClient = fails
      ? new DaemonClient({ baseUrl: client.baseUrl, token: client.token, fetch: async (input, init) => {
        if (String(input).endsWith("/goal/plan-offer")) throw new Error("offer unavailable");
        return daemon.fetch(input, init);
      } })
      : client;
    const { onOpen } = mount(daemon, actionClient);
    await screen.findByText("This looks big: plan it first?");
    const body = within(card("plan_offer")).getByTestId("card-detail");
    for (const value of [context.session_name, context.goal, context.detail, ...context.milestones.map((m) => m.title), ...context.reasons]) {
      expect(body.textContent).toContain(value);
    }
    expect(body.querySelectorAll("li")).toHaveLength(context.milestones.length);
    expect(within(body).getByTestId("offer-progress").textContent?.match(/\d+/g)).toEqual(["1", "2"]);
    expect(within(body).getByTestId("offer-scale").textContent?.match(/\d+/g)).toEqual([String(context.workers), String(context.repos)]);
    expect(body.className).not.toContain("line-clamp");
    // The link appears once the session list has loaded, which may land after the inbox.
    fireEvent.click(await within(card("plan_offer")).findByRole("button", { name: "Go to session ›" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "sup1");
    onOpen.mockClear();
    act(() => {
      const button = within(card("plan_offer")).getByRole("button", { name: label });
      fireEvent.click(button);
      fireEvent.click(button);
    });
    if (fails) {
      expect(await screen.findByText("offer unavailable")).toBeTruthy();
      expect(onOpen).not.toHaveBeenCalled();
      expect(screen.getByTestId("inbox-card")).toBeTruthy();
    } else {
      await waitFor(() => expect(posts(daemon).find((r) => r.path === "/goal/plan-offer")?.body).toMatchObject({ root: "sup1", offer: "o1", accept: accepted }));
      expect(posts(daemon).filter((r) => r.path === "/goal/plan-offer")).toHaveLength(1);
      await waitFor(() => expect(onOpen).toHaveBeenCalledTimes(1));
      expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "sup1");
    }
  });

  it("starts a sign-in from its card", async () => {
    // As the engine's permission-items branch emits a lapsed sign-in: no detail, the sign-in route named on the
    // item; the worker is on Codex (its record), so the sign-in must be Codex's, never Claude's.
    const codexWorker = { ...worker, agents: [{ ...worker.agents![0], provider: "codex", profile: "developer-codex" }] };
    const credentials: InboxItemDTO = { kind: "credentials", reason: "credentials need attention", agent_id: "w1", workspace_id: "proj/feat-ls", root_id: "sup1", detail: null, action: { method: "POST", path: "/v1/providers/codex/login" } };
    const login: InboxItemDTO = { kind: "login", reason: "cloud login expired", agent_id: "sup1", workspace_id: "proj", root_id: "sup1", detail: "brindle account login" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, codexWorker], inbox: [credentials, login, notice] });
    mount(daemon);
    await screen.findByRole("region", { name: "Ship ls --json" });

    // A lapsed sign-in: Sign in starts that provider CLI's own login on the engine (Codex, as the engine's route
    // and the worker's record say).
    const signIn = card("credentials");
    expect(signIn.textContent).toContain("Codex needs a sign-in");
    expect(within(signIn).queryByRole("button", { name: "Sign in to Claude" })).toBeNull();
    fireEvent.click(within(signIn).getByRole("button", { name: "Sign in to Codex" }));
    await waitFor(() => expect(daemon.actions.map((a) => a.kind)).toEqual(["login codex"]));
    expect(within(signIn).getByTestId("sign-in-flow")).toBeTruthy();

    // brindle's own cloud sign-in: the card starts it (a job on the daemon; its browser URL is followed) and says so.
    const cloud = card("login");
    fireEvent.click(within(cloud).getByRole("button", { name: "Sign in to brindle" }));
    await waitFor(() => expect(posts(daemon).map((r) => r.path)).toContain("/account/login"));
    await waitFor(() => expect(within(cloud).getByTestId("cloud-login").textContent).toContain("Signed in again"));
  });

  it("approves a plan, accepts or declines proposed checks, on the card, at once; a refusal comes back as one line on the card", async () => {
    const plan: InboxItemDTO = { kind: "plan", reason: "plan awaiting approval", agent_id: "w1", workspace_id: "proj/feat-ls", root_id: "sup1", detail: "1. Keep the plan.\n2. Ship it." };
    const checks: InboxItemDTO = { kind: "proposed_checks", reason: "checks proposed", agent_id: "sup1", workspace_id: "proj", root_id: "sup1", detail: "npm test\nnpm run build" };
    const review: InboxItemDTO = { kind: "review", reason: "to review", agent_id: "w1", workspace_id: "proj/feat-ls", root_id: "sup1" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [{ ...session, name: "Login fix" }], workspaces: [root, worker], inbox: [plan, checks, review] });
    const { onOpen } = mount(daemon);
    // A named session is headed by its name, not its goal.
    const group = await screen.findByRole("region", { name: "Login fix" });
    // A plan: Approve alone (changes are asked for in its session), no reply field, no undo; the plan whole on the
    // card, never cut short.
    expect(within(card("plan")).queryByRole("textbox")).toBeNull();
    expect(within(card("plan")).queryByRole("button", { name: /Request changes/ })).toBeNull();
    expect(within(card("plan")).getByTestId("card-detail").textContent).toBe("1. Keep the plan.\n2. Ship it.");
    expect(within(card("plan")).getByTestId("card-detail").className).not.toContain("line-clamp");
    // The plan the engine lists changes: the card shows the new one whole before anything is approved
    // (useInboxActions.test covers an approval of a plan the card no longer shows).
    daemon.data.inbox = daemon.data.inbox.map((i) => (i.kind === "plan" ? { ...i, detail: "1. Keep the plan.\n2. Ship it.\n3. Also delete the database." } : i));
    daemon.publish("message.queued", { agent_id: "w1" });
    await waitFor(() => expect(within(card("plan")).getByTestId("card-detail").textContent).toContain("3. Also delete the database."));
    fireEvent.click(within(card("plan")).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts(daemon)).toEqual([{ method: "POST", path: "/inbox/w1/approve", query: {}, body: { feedback: "" } }]));
    await waitFor(() => expect(within(group).queryAllByTestId("inbox-card").some((c) => c.dataset.kind === "plan")).toBe(false));
    expect(screen.queryByRole("status")).toBeNull();
    // Proposed checks: Accept or Decline. The engine's refusal (nothing waiting any more) comes back on the card.
    daemon.data.inbox = daemon.data.inbox.filter((i) => i.kind !== "proposed_checks");
    fireEvent.click(within(card("proposed_checks")).getByRole("button", { name: "Decline" }));
    await waitFor(() => expect(within(card("proposed_checks")).getByRole("alert").textContent).toContain("no agent sup1"));
    // A branch to review only goes to the session.
    expect(within(card("review")).queryByRole("button", { name: /Request review|Merge/ })).toBeNull();
    expect(card("review").textContent).toContain("handled in its session");
    fireEvent.click(within(card("review")).getByRole("button", { name: "Go to session ›" }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "w1");
  });

  it("self-repair: a setup problem the engine can fix is a card with one Fix, a job on the engine; the card clears when it is done, or shows the job's error", async () => {
    // As the engine's M10 inbox lists it (service/inbox._setup_items): no agent, a fix the app has a button for.
    const setupItem: InboxItemDTO = { kind: "setup", fix: "codex_hook_trust", reason: "Codex's trust of brindle's permission hook was removed from Codex's config", action: { method: "POST", path: "/v1/setup/codex-hook" } };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [setupItem, question] });
    daemon.deferFixes = true;
    daemon.fixFails = true;
    mount(daemon);
    const group = await screen.findByRole("region", { name: "Setup" });
    expect(group.textContent).toContain("brindle on this computer");
    const setup = card("setup");
    expect(setup.textContent).toContain("Codex's trust of brindle's permission hook was removed");
    expect(setup.textContent).toContain("Fix makes Codex trust brindle's permission hook again");
    // One button, inline: no confirmation, no sheet, no Go to session (it has none).
    expect(within(setup).getAllByRole("button").map((b) => b.textContent)).toEqual(["Fix"]);
    fireEvent.click(within(setup).getByRole("button", { name: "Fix" }));
    await waitFor(() => expect(posts(daemon)).toEqual([{ method: "POST", path: "/setup/codex-hook", query: {}, body: {} }]));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByTestId("upgrade-card")).toBeNull();
    // While the engine writes Codex's config the button waits, as any card's does while its request is out.
    await waitFor(() => expect(within(setup).getByRole("button", { name: "Fix" })).toHaveProperty("disabled", true));
    // The job fails: its error, in the engine's words, is the one line on the card, and Fix is there to press again.
    daemon.completeFixes();
    await waitFor(() => expect(within(card("setup")).getByRole("alert").textContent).toContain("couldn't trust brindle's Codex hook: codex isn't installed"));
    expect(within(card("setup")).getByRole("button", { name: "Fix" })).toHaveProperty("disabled", false);
    // Pressed again, it goes through: job.done, and the card is gone (the engine no longer lists the problem).
    daemon.fixFails = false;
    fireEvent.click(within(card("setup")).getByRole("button", { name: "Fix" }));
    await waitFor(() => expect(daemon.fixes).toHaveLength(2));
    daemon.completeFixes();
    await waitFor(() => expect(screen.queryByRole("region", { name: "Setup" })).toBeNull());
    expect(screen.queryByRole("alert")).toBeNull();
    // The question's card is untouched by any of it.
    expect(card("question")).toBeTruthy();
    expect(posts(daemon).map((r) => r.path)).toEqual(["/setup/codex-hook", "/setup/codex-hook"]);
  });

  it("one permission card per source: a held request (Deny, Allow once, Allow ▾, details collapsed) and an agent-window prompt (a way to the window), under a one-line stall banner", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission, permissionInTerminal], asks: [pushes] });
    const { onOpen } = mount(daemon);
    const held = await screen.findByRole("article", { name: "Allow pushes into main?" });
    const cards = screen.getAllByTestId("permission-card");
    expect(cards.map((c) => c.dataset.source)).toEqual(["hook", "window"]);

    // The banner is one line with no actions of its own but the way to the first card.
    const stall = screen.getByTestId("stall-notice");
    expect(within(stall).getAllByRole("button").map((b) => b.textContent)).toEqual(["Review all"]);
    // The held card: why it reached the person, the three actions, and the requests under collapsed details.
    expect(within(held).getAllByRole("button").filter((b) => ["Deny", "Allow once", "Allow"].includes(b.textContent ?? "")).map((b) => b.textContent)).toEqual(["Deny", "Allow once", "Allow"]);
    expect(held.querySelector("details")!.open).toBe(false);

    // The prompt only the agent's window can take: no decision, the way to that window instead.
    const prompt = cards[1];
    expect(within(prompt).queryByRole("group", { name: "Decision" })).toBeNull();
    // The way to the window comes with the agent's session, which the store syncs on its own.
    fireEvent.click(await within(prompt).findByRole("button", { name: /^Open / }));
    expect(onOpen).toHaveBeenLastCalledWith("sup1", REPO, "sup1");

    fireEvent.click(within(held).getByRole("button", { name: "Allow once" }));
    await waitFor(() => expect(posts(daemon).find((r) => r.path === "/permissions/asks/decide")?.body).toEqual({ key: pushes.key, scope: "general", mode: "once" }));
  });

  it("a bare push offers only Allow once and Deny, explains why, and translates a stale refusal", async () => {
    const reason = "This push doesn't name its branch, so it can only be allowed once.";
    const exact = { ...pushes.scopes[0], pattern: "? → ? @ ?", modes: ["once", "deny"] };
    const bare: AskGroupDTO = {
      ...pushes, general: exact, scopes: [exact], modes: ["once", "deny"], limits: null,
      save_reason: reason, items: [{ ...pushes.items[0], summary: "git push", exact: exact.pattern }],
    };
    const daemon = new FakeDaemon({ sessions: [session], workspaces: [root, worker], inbox: [permission], asks: [bare] });
    const client = daemon.client();
    vi.spyOn(client, "decidePermissionAsks").mockRejectedValue(new Error("nothing in this scope can be approved: pushes outside this scope"));
    mount(daemon, client);
    const held = await screen.findByRole("article", { name: bare.title });
    expect(within(within(held).getByRole("group", { name: "Decision" })).getAllByRole("button").map((b) => b.textContent)).toEqual(["Deny", "Allow once"]);
    expect(within(held).getByText(reason)).toBeTruthy();
    fireEvent.click(within(held).getByRole("button", { name: "Allow once" }));
    const refusal = await screen.findByRole("alert");
    expect(refusal.textContent).not.toContain("nothing in this scope can be approved");
    expect(refusal.textContent).not.toContain("pushes outside this scope");
    expect(refusal.textContent).toContain("Allow once");
  });

  it.each([
    ["the groups entry's reason", null, "the shell/unchecked baseline is ask"],
    ["the engine's d-shell-syntax rule", "d-shell-syntax", "shell syntax cannot be checked safely"],
  ])("unchecked commands opt-in: the ask (%s) offers only Deny and Allow once, and a link to the Settings row", async (_, rule, reason) => {
    const unchecked: AskGroupDTO = { ...pushes, key: "k-unchecked", title: "Allow this unchecked command?", modes: ["once", "deny"], rule, items: pushes.items.map((a) => ({ ...a, rule, reason })) };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission], asks: [unchecked] });
    const { onOpenRule } = mount(daemon);
    const held = await screen.findByRole("article", { name: "Allow this unchecked command?" });
    expect(within(held).getAllByRole("button").filter((b) => ["Deny", "Allow once", "Allow"].includes(b.textContent ?? "")).map((b) => b.textContent)).toEqual(["Deny", "Allow once"]);
    fireEvent.click(within(held).getByRole("button", { name: "Stop asking for these…" }));
    expect(within(held).queryByRole("button", { name: /rule/ })).toBeNull();
    expect(onOpenRule).toHaveBeenCalledWith("unchecked-commands");
  });

  it("a hold with no group: the request is in the headline, Allow never saves a grant by itself, and a refusal shows on the card", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission, permissionInTerminal] });
    mount(daemon);
    const cards = await screen.findAllByTestId("permission-card");
    expect(cards.map((c) => c.dataset.source)).toEqual(["hook", "window"]);
    // Both requests are named with Details closed.
    expect(cards[0].querySelector("h3")!.textContent).toContain("npx playwright install chromium");
    expect(cards[1].querySelector("h3")!.textContent).toContain("uv run alembic upgrade head");
    // Allow only opens the menu: no choice is sent, least of all Allow always.
    fireEvent.click(within(cards[0]).getByRole("button", { name: "Allow" }));
    expect(within(cards[0]).getByRole("menuitem", { name: /Allow always/ })).toBeTruthy();
    expect(posts(daemon)).toEqual([]);
    // The engine refuses (the request is gone): its reason is on the card.
    daemon.data.inbox = [];
    fireEvent.click(within(cards[0]).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(within(cards[0]).getByRole("alert")).toBeTruthy());
  });

  it("a decision out blocks a second one from the menu's duration button", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission], asks: [pushes] });
    const client = daemon.client();
    let settle!: (got: AskDecisionDTO) => void;
    const decide = vi.spyOn(client, "decidePermissionAsks").mockImplementation(() => new Promise<AskDecisionDTO>((resolve) => (settle = resolve)));
    mount(daemon, client);
    const item = await screen.findByRole("article", { name: "Allow pushes into main?" });
    fireEvent.click(within(item).getByRole("button", { name: "More ways to allow" }));
    fireEvent.click(within(item).getByRole("menuitem", { name: /^For 30 minutes/ }));
    const submit = within(item).getByRole("button", { name: /^Allow for 30 minutes/ });
    // Both clicks land before React commits anything: only the guard can stop the second.
    act(() => {
      fireEvent.click(submit);
      fireEvent.click(submit);
    });
    expect(decide).toHaveBeenCalledTimes(1);
    // Once the first is answered, the next decision goes out.
    await act(async () => settle({ mode: "timed", scope: pushes.general, resumed: [] }));
    await waitFor(() => expect(within(item).getByRole("button", { name: "Allow once" })).toHaveProperty("disabled", false));
    fireEvent.click(within(item).getByRole("button", { name: "Allow once" }));
    expect(decide).toHaveBeenCalledTimes(2);
  });

  it("Always for <group item> sets that subcategory's override through the group baselines, then resumes the request", async () => {
    const install: AskGroupDTO = {
      key: `${REPO}|bash|npm install`, repo: REPO, kind: "bash", title: "Allow npm install?", general: scopesFor("bash", "npm install left-pad")[0],
      scopes: scopesFor("bash", "npm install left-pad"), blocked: false, rule: null, limits: null, modes: ["once", "deny", "session"],
      items: [{ ...ask("w1", "req-1", "bash", "npm install left-pad", 60, "d-none"), rule: null, limits: null, reason: "the shell/install baseline is ask" }],
    };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission], asks: [install] });
    mount(daemon);
    const item = await screen.findByRole("article", { name: "Allow npm install?" });
    expect(within(item).getByTestId("permission-chip")).toBeTruthy();
    fireEvent.click(within(item).getByRole("button", { name: "More ways to allow" }));
    fireEvent.click(within(item).getByRole("menuitem", { name: /^Always for / }));
    await waitFor(() => expect(daemon.requests.find((r) => r.method === "PUT" && r.path === "/permissions/groups")?.body).toEqual({ groups: { shell: { subcategories: { install: "allow" } } } }));
    await waitFor(() => expect(daemon.data.asks).toEqual([]));
    // An override, not a saved action.
    expect(daemon.data.policy.saved).toEqual([]);
    expect(daemon.data.policy.groups?.shell.subcategories).toEqual({ install: "allow" });
  });

  it("org policy shown to member: an org Ask is labelled with the org and its reason, saves only what the org allows; a Block is one calm notice; Settings shows locked rows", async () => {
    const org = { name: "Acme", what: "local git changes", decision: "ask", reason: "commits are audited" };
    const commits: AskGroupDTO = {
      key: `${REPO}|bash|family: git/local`, repo: REPO, kind: "bash", title: "Allow local git changes?", general: scopesFor("bash", "git add -A")[0],
      scopes: scopesFor("bash", "git add -A"), blocked: false, rule: "org-git-local", limits: { life: "time", minutes: 15 }, modes: ["once", "deny", "always-deny", "timed"], org,
      items: [{ ...ask("w1", "req-1", "bash", "git add -A", 60, "org-git-local"), limits: { life: "time", minutes: 15 } }],
    };
    const blocked: InboxItemDTO = { kind: "org_blocked", reason: "Blocked by Acme policy: installs go through platform review", detail: "3 requests stopped today. Agents were told to work around it." };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [permission, blocked], asks: [commits] });
    daemon.data.policy.org_controls = [org, { name: "Acme", what: "package installs", decision: "deny", reason: "installs go through platform review" }];
    const { sync } = mount(daemon);
    const item = await screen.findByRole("article", { name: "Allow local git changes?" });
    const why = within(item).getByTestId("org-why");
    expect([why.dataset.org, why.dataset.reason, why.dataset.setting]).toEqual(["Acme", "commits are audited", "ask"]);
    // Offered: what the org allows (no lasting save), and no way into the member's own rules.
    expect(within(item).queryByRole("button", { name: /rule/ })).toBeNull();
    fireEvent.click(within(item).getByRole("button", { name: "More ways to allow" }));
    expect(within(item).getAllByRole("menuitem")).toHaveLength(1);   // a timed approval, its only save
    expect(within(item).getByRole("button", { name: "Allow once" })).toBeTruthy();
    // A Block: one quiet notice in the org's group, however many requests it stopped.
    expect(document.querySelectorAll('[data-kind="org_blocked"]')).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Your org" }).contains(card("org_blocked"))).toBe(true);
    sync.stop();
    cleanup();

    render(<Permissions client={daemon.client()} />);
    const rows = [...(await screen.findByTestId("org-controls")).querySelectorAll<HTMLElement>("[data-locked]")];
    expect(rows.map((r) => [r.dataset.org, r.dataset.what, r.dataset.setting, r.dataset.reason])).toEqual([
      ["Acme", "local git changes", "ask", "commits are audited"], ["Acme", "package installs", "deny", "installs go through platform review"]]);
    expect(rows.flatMap((r) => [...r.querySelectorAll("button, input, select")])).toEqual([]);   // locked: nothing to change
  });

  it("shows a failed load with a retry that fetches again, and all clear when nothing waits", async () => {
    const daemon = new FakeDaemon({ inbox: [question] });
    let down = true;
    const client = new DaemonClient({
      baseUrl: BASE_URL,
      token: TOKEN,
      fetch: (input, init) => (down ? Promise.reject(new TypeError("connection refused")) : daemon.fetch(input, init)),
    });
    mount(daemon, client);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("connection refused"));
    down = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(card("question")).toBeTruthy());
    daemon.data.inbox = [];
    daemon.publish("message.queued", { agent_id: "sup1" });
    await waitFor(() => expect(screen.getByTestId("all-clear")).toBeTruthy());
  });

});
