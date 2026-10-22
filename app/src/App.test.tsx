import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { bannerStatus, landing } from "./App";
import type { EngineStatus } from "./api/engine";
import type { WebSocketFactory } from "./api/events";
import type { GoalDTO, SessionDTO } from "./api/schema";
import { LAST_SESSION_KEY } from "./routes";
import { createAppStore } from "./store/store";
import { startSync } from "./store/sync";
import { DaemonClient } from "./api/client";
import { BASE_URL, FakeDaemon, TOKEN, flush, freshProviders } from "./test/fakeDaemon";
import { PtyViewsProvider } from "./api/ptyView";
import { REPO, question, root, windowOf, worker } from "./test/fixtures";
import { standInViews } from "./test/panes";

vi.mock("./components/PtyTerminal", () => import("./test/panes"));

beforeEach(() => localStorage.clear());
afterEach(cleanup);

/* The app as the shell shows it: its panes through pty views (stand-ins here). */
const renderApp = (app: ReactElement) => render(<PtyViewsProvider value={standInViews}>{app}</PtyViewsProvider>);

const goal: GoalDTO = {
  root_id: "sup1",
  enabled: false,
  goal: "Ship the Goal screen",
  state: "running",
  progress: 0,
  milestones: [{ id: 1, position: 1, title: "Client wired", detail: "The typed client covers every v1 route.", status: "passed", check_cmd: "npm test" }],
};
const session: SessionDTO = {
  id: "sup1",
  workspace_id: "proj",
  profile: "supervisor",
  status: "idle",
  mode: "interactive",
  created_at: Date.now() / 1000 - 600,
  goal: "Ship ls --json",
};
/* Paused: its root agent is in no snapshot, so only the sessions feed knows its repo. */
const paused: SessionDTO = { ...session, id: "sup0", status: "paused", status_since: Date.now() / 1000 - 3600, goal: "Refactor the cache" };

const nav = () => screen.getByRole("complementary", { name: "Sidebar" });
const link = (name: string | RegExp) => within(nav()).getByRole("button", { name });
const treeAgent = (id: string) => nav().querySelector<HTMLButtonElement>(`button[data-tree-agent="${id}"]`)!;
const surfaceTitle = () => screen.getByTestId("surface-title").textContent;

describe("App", () => {
  it.each([false, true])("resume paused: lists the goal and resumes into the session's chat pane (already selected: %s)", async (selected) => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, paused], workspaces: [root] });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={selected ? { screen: "session", root: "sup0", repo: REPO } : { screen: "new" }} />);
    await screen.findByRole("complementary", { name: "Sidebar" });
    if (!selected) fireEvent.click(await within(nav()).findByRole("button", { name: /Paused · 1/ }));
    const row = (await waitFor(() => nav().querySelector('[data-tree-session="sup0"]')!)).closest("li")!;
    expect(row.textContent).toContain("Refactor the cache");
    // Opened paused, the session says nothing of being paused under it.
    expect(screen.queryByText("This session was paused")).toBeNull();
    fireEvent.click(within(row).getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/sessions/sup0/continue")).toHaveLength(1));
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup0"));
    expect(JSON.parse(localStorage.getItem(LAST_SESSION_KEY)!)).toEqual({ root: "sup0", repo: REPO });
    sync.stop();
  });

  it("session was paused: keeps the shown session and offers Reopen after another session pauses it", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup1"));
    expect(screen.queryByText("This session was paused")).toBeNull();
    // Starting fresh in the same checkout pauses the chat currently on screen.
    await client.startSession({ repo: REPO, prompt: "Other work", choice: "p", session_id: "sup1" });
    daemon.data.workspaces = daemon.data.workspaces.map((ws) => ({ ...ws, agents: ws.agents?.filter((a) => a.id !== "sup1") }));
    await sync.refresh();
    const notice = (await screen.findByText("This session was paused")).closest('[role="status"]')! as HTMLElement;
    expect(screen.getByRole("region", { name: "Session" })).toBeTruthy();
    expect(nav().querySelector('[data-tree-session="sup1"]')).toBeTruthy();
    fireEvent.click(within(notice).getByRole("button", { name: "Reopen" }));
    await waitFor(() => expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/sessions/sup1/continue")).toHaveLength(1));
    await waitFor(() => expect(screen.queryByText("This session was paused")).toBeNull());
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup1"));
    sync.stop();
  });

  it("opens straight into the live provider session, moves between sessions and agents from the sidebar, and reaches every page", async () => {
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }],
      sessions: [session, paused],
      workspaces: [root, worker],
      inbox: [question, { kind: "org_notice", reason: "Reviewer profiles now default to reviewer-codex.", detail: "admin", notice_id: "n-18" }],
      goal,
    });
    daemon.data.meta = { ...daemon.data.meta, engine_version: "1.4.0", api_version: "2.0" };
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);

    // No home board: the first live session's provider session, front and centre, with a newer engine's banner above.
    // (The landing waits on the sessions feed and the engine check: given room under a full parallel run.)
    expect(await screen.findByRole("region", { name: "Session" }, { timeout: 4000 })).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup1"));
    expect(surfaceTitle()).toBe("Claude Code · supervisor");
    expect(screen.getByTestId("engine-banner").textContent).toContain("update the app");
    expect(JSON.parse(localStorage.getItem(LAST_SESSION_KEY)!)).toEqual({ root: "sup1", repo: REPO });
    // The merged sidebar: Inbox at the top, the session's context (its goal and milestones) above the split, then
    // the tree with the supervisor and its worker under the session. Nothing sits beside the provider session.
    await waitFor(() => expect(treeAgent("w1")).toBeTruthy());
    expect(within(nav()).getByTestId("tree-session-sup1").textContent).toContain("Ship ls --json");
    expect(treeAgent("sup1").getAttribute("aria-current")).toBe("page");
    expect(screen.queryByRole("complementary", { name: "Session panel" })).toBeNull();
    const context = within(nav()).getByTestId("session-context");
    await waitFor(() => expect(within(context).getByTestId("goal-line").textContent).toContain("1 of 1 passed"));
    expect(within(context).getByRole("list", { name: "Milestones" }).textContent).toContain("Client wired");
    expect(within(nav()).getByTestId("split-handle")).toBeTruthy();
    expect(daemon.requests.find((r) => r.path === "/goal")?.query).toEqual({ repo: REPO, root: "sup1" });
    // The bar is the agent's alone: no goal, repo or count of the session's on it.
    expect(screen.getByTestId("session-bar").textContent).not.toContain("Ship ls --json");
    expect(screen.queryByTestId("goal-progress")).toBeNull();
    expect(within(screen.getByTestId("session-bar")).queryByRole("button", { name: /need/ })).toBeNull();

    // The tree picks a worker: its provider session at once. No per-worker review and no message box:
    // the person talks to it there, and a branch is reviewed from its inbox item.
    fireEvent.click(treeAgent("w1"));
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("w1"));
    expect(surfaceTitle()).toBe("Claude Code · ls");
    expect(treeAgent("w1").getAttribute("aria-current")).toBe("page");
    expect(screen.queryByRole("button", { name: "Review changes" })).toBeNull();
    expect(screen.queryByLabelText("Message")).toBeNull();

    // The Inbox page, from the entry at the sidebar's top: everything waiting, grouped by session as banner
    // cards; a card's Go to session jumps to the agent that asks in its provider session, nothing over it.
    expect(screen.getByTestId("inbox-badge").textContent).toBe("2");
    fireEvent.click(link(/^Inbox/));
    expect(await screen.findByRole("region", { name: "Inbox" })).toBeTruthy();
    const group = screen.getByRole("region", { name: "Ship ls --json" });
    expect(group.textContent).toContain("Has a question");
    fireEvent.click(within(group).getByRole("button", { name: "Go to session ›" }));
    expect(await screen.findByRole("region", { name: "Session" })).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup1"));
    expect(screen.queryByRole("dialog")).toBeNull();
    // The badge follows the inbox: an answered question and a read notice leave it empty.
    daemon.data.inbox = [];
    daemon.publish("message.queued", { agent_id: "sup1" });
    await waitFor(() => expect(screen.queryByTestId("inbox-badge")).toBeNull());

    // The pages behind the sidebar's foot, each for the repo in view.
    for (const name of ["Knowledge", "Agents", "Settings", "Account"]) {
      fireEvent.click(link(name));
      expect(await screen.findByRole("region", { name })).toBeTruthy();
      expect(link(name).getAttribute("aria-current")).toBe("page");
    }

    // ⌘1 is the first live session, back from any page; ⌘N the composer.
    fireEvent.keyDown(window, { key: "1", metaKey: true });
    expect(await screen.findByRole("region", { name: "Session" })).toBeTruthy();
    fireEvent.keyDown(window, { key: "n", metaKey: true });
    expect(await screen.findByRole("region", { name: "New session" })).toBeTruthy();
    expect(screen.getByLabelText("What should brindle work on?")).toBeTruthy();

    // A paused session opens too, from its repo's Paused · N row once expanded: a card in place of a session (no
    // agents to show), and its goal is asked for with the repo the sessions feed knows, not one found in a snapshot.
    fireEvent.click(within(nav()).getByRole("button", { name: /Paused · 1/ }));
    fireEvent.click(nav().querySelector<HTMLButtonElement>('button[data-tree-session="sup0"]')!);
    const card = await screen.findByTestId("state-card");
    expect(card.textContent).toContain("is paused");
    expect(within(card).getByRole("button", { name: "Continue" })).toBeTruthy();
    // The sidebar's context follows the session on screen.
    await waitFor(() => expect(within(nav()).getByRole("region", { name: "Goal" }).textContent).toContain("No goal set for this session."));
    expect(within(nav()).getByTestId("session-context").textContent).toContain("Refactor the cache");
    expect(daemon.requests.filter((r) => r.path === "/goal").at(-1)?.query).toEqual({ repo: REPO, root: "sup0" });

    // The connection fails (here: the daemon's token changed under the app, so
    // the stream's reconnect is refused): the banner says so in place of the
    // update notice, and "Check again" runs the engine check, which fails too.
    daemon.token = "rotated";
    daemon.drop();
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Can't reach the brindle engine"));
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Can't reach the brindle engine: missing or wrong token"));
    sync.stop();
  });

  it("inbox: a plan is approved and a notice marked read right on their cards, at once; Go to session opens the session with nothing over it", async () => {
    const plan = { kind: "plan", reason: "plan awaiting approval", agent_id: "sup1", workspace_id: "proj", detail: "1. Keep the plan.\n2. Ship it." };
    const notice = { kind: "org_notice", reason: "Reviewer profiles now default to reviewer-codex.", detail: "admin", notice_id: "n-18" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [plan, notice, question], goal });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "inbox" }} />);
    const group = await screen.findByRole("region", { name: "Ship ls --json" });
    const posts = () => daemon.requests.filter((r) => r.method === "POST").map((r) => r.path);
    // Approve, on the card: the request goes at once, no moment to undo, and the card is gone.
    const planCard = within(group).getAllByTestId("inbox-card").find((c) => c.dataset.kind === "plan")!;
    fireEvent.click(within(planCard).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts()).toEqual(["/inbox/sup1/approve"]));
    await waitFor(() => expect(within(group).queryAllByTestId("inbox-card").some((c) => c.dataset.kind === "plan")).toBe(false));
    expect(screen.queryByRole("dialog")).toBeNull();
    // Mark read, on the notice's card, the same way.
    const org = screen.getByRole("region", { name: "Your org" });
    fireEvent.click(within(org).getByRole("button", { name: "Mark read" }));
    await waitFor(() => expect(posts()).toEqual(["/inbox/sup1/approve", "/inbox/n-18/read"]));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Your org" })).toBeNull());
    // Go to session on the question's card: the supervisor's pane, and nothing over it.
    fireEvent.click(within(screen.getByRole("region", { name: "Ship ls --json" })).getByRole("button", { name: "Go to session ›" }));
    expect(await screen.findByRole("region", { name: "Session" })).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup1"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    sync.stop();
  });

  it("lands on the session kept from last time, else the first live one, else the composer", async () => {
    const repos = [{ repo_root: REPO, name: "proj" }];
    const sessions = { [REPO]: [session, paused] };
    expect(landing(repos, sessions, { root: "sup0", repo: REPO })).toEqual({ screen: "session", root: "sup0", repo: REPO, agent: "sup0" });
    expect(landing(repos, sessions, { root: "gone", repo: REPO })).toEqual({ screen: "session", root: "sup1", repo: REPO, agent: "sup1" });
    expect(landing(repos, { [REPO]: [paused] }, null)).toEqual({ screen: "new" });

    // Nothing running and nothing kept: the calm empty state, with the composer.
    const daemon = new FakeDaemon({ repos, sessions: [paused], workspaces: [root] });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    expect(await screen.findByRole("region", { name: "New session" })).toBeTruthy();
    expect(await screen.findByLabelText("What should brindle work on?")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Session" })).toBeNull();
    sync.stop();
  });

  it("a session started from the composer on a first launch opens running, not paused (#216)", async () => {
    // A first launch: a project just picked, nothing ever run in it. The engine says the new supervisor
    // exists only with agent.status (it has no event of its own), and nothing else happens on the bus.
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [], workspaces: [{ ...root, agents: [] }] });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    expect(await screen.findByRole("region", { name: "New session" })).toBeTruthy();
    fireEvent.change(await screen.findByLabelText("What should brindle work on?"), { target: { value: "Fix the flaky test" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    // The view opens on the session's live chat: never "paused", never a card offering to Continue it.
    expect(await screen.findByRole("region", { name: "Session" })).toBeTruthy();
    const bar = () => screen.getByTestId("session-bar");
    expect(bar().textContent).not.toContain("paused");
    expect(within(bar()).queryByRole("button", { name: "Continue" })).toBeNull();
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("new1"));
    expect(surfaceTitle()).toBe("Claude Code · supervisor");
    expect(screen.queryByTestId("state-card")).toBeNull();
    expect(within(bar()).getByRole("button", { name: "Pause" })).toBeTruthy();
    // Nothing was paused for it either.
    expect(daemon.requests.some((r) => r.path.endsWith("/pause"))).toBe(false);
    sync.stop();
  });

  it("a session started with an empty composer on a first launch opens running, not paused (#216)", async () => {
    // What a fresh engine was seen to send for this start: only agent.status "idle", once the chat is up.
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [], workspaces: [{ ...root, agents: [] }] });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    await screen.findByLabelText("What should brindle work on?");
    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    expect(await screen.findByRole("region", { name: "Session" })).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("new1"));
    const bar = screen.getByTestId("session-bar");
    expect(bar.textContent).not.toContain("paused");
    expect(within(bar).getByRole("button", { name: "Pause" })).toBeTruthy();
    expect(screen.queryByTestId("state-card")).toBeNull();
    sync.stop();
  });

  it("a session whose status beat its window into the snapshot still opens running, not paused (#216)", async () => {
    // The engine publishes the new chat's status before it records the chat's window: the snapshot that
    // status asks for lists it "exited" with no window, and nothing on the bus says when the window lands.
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [], workspaces: [{ ...root, agents: [] }] });
    daemon.holdLaunch = true;
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    fireEvent.change(await screen.findByLabelText("What should brindle work on?"), { target: { value: "Fix the flaky test" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    const early = () => store.getState().workspaces.flatMap((ws) => ws.agents ?? []).find((a) => a.id === "new1");
    await waitFor(() => expect(early()).toMatchObject({ status: "exited", window: null }));
    await sync.idle();
    daemon.finishLaunch();

    expect(await screen.findByRole("region", { name: "Session" })).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("new1"));
    const bar = screen.getByTestId("session-bar");
    expect(bar.textContent).not.toContain("paused");
    expect(within(bar).getByRole("button", { name: "Pause" })).toBeTruthy();
    expect(screen.queryByTestId("state-card")).toBeNull();
    sync.stop();
  });

  it("says where the sessions failed to load, with a retry, instead of a blank main area", async () => {
    // The engine answers meta and the snapshot but the first GET /repos fails: nothing to land on yet.
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker] });
    let down = true;
    const client = new DaemonClient({
      baseUrl: BASE_URL,
      token: TOKEN,
      fetch: (input, init) => (String(input).endsWith("/v1/repos") && down ? Promise.reject(new TypeError("connection refused")) : daemon.fetch(input, init)),
    });
    const store = createAppStore();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    const failed = await screen.findByTestId("sessions-failed");
    expect(failed.textContent).toContain("connection refused");
    expect(nav().textContent).toContain("connection refused");
    expect(screen.queryByRole("region", { name: "Session" })).toBeNull();
    // Retried, the sessions load and the app lands on the live one.
    down = false;
    fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("region", { name: "Session" })).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup1"));
    expect(screen.queryByTestId("sessions-failed")).toBeNull();
    sync.stop();
  });

  it("runs an old engine's upgrade from the banner in the background: waiting, failed with a retry, then updated", async () => {
    const daemon = new FakeDaemon();
    daemon.data.meta = { ...daemon.data.meta, engine_version: "0.4.1", api_version: "0.9" };
    daemon.deferUpgrade = true;
    daemon.upgradeFails = true;
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    const banner = await screen.findByTestId("engine-banner");
    expect(banner.textContent).toContain("is older than this app supports");
    expect(banner.textContent).not.toContain("uv tool upgrade brindle");
    // Started: the button waits, the card follows the run; no terminal anywhere.
    fireEvent.click(screen.getByRole("button", { name: "Upgrade the engine" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Upgrading…" })).toHaveProperty("disabled", true));
    await waitFor(() => expect(screen.getByTestId("upgrade-card").getAttribute("data-state")).toBe("running"));
    expect(daemon.requests.filter((r) => r.method === "POST").map((r) => r.path)).toEqual(["/engine/upgrade"]);
    expect(screen.queryByTestId("terminal")).toBeNull();
    // uv fails: the banner offers the retry, the card shows what uv printed.
    daemon.completeUpgrade();
    await waitFor(() => expect(screen.getByTestId("upgrade-card").getAttribute("data-state")).toBe("failed"));
    expect(screen.getByTestId("upgrade-log").textContent).toContain("network unreachable");
    // Retried, it goes through: updated, and the engine is asked again.
    daemon.upgradeFails = false;
    daemon.deferUpgrade = false;
    const checks = daemon.requests.filter((r) => r.path === "/meta").length;
    fireEvent.click(screen.getByRole("button", { name: "Upgrade failed · Retry" }));
    await waitFor(() => expect(screen.getByTestId("upgrade-card").getAttribute("data-state")).toBe("done"));
    expect(screen.getByTestId("upgrade-card").textContent).toContain("Updated.");
    await waitFor(() => expect(daemon.requests.filter((r) => r.path === "/meta").length).toBeGreaterThan(checks));
    // Two jobs of the engine's, no terminal's command.
    expect(daemon.upgrades).toHaveLength(2);
    expect(daemon.actions).toHaveLength(0);
    sync.stop();
  });

  it("hands the shell's command-line installer to Settings: the button is live and its result lands on the row", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker] });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    const installCli = vi.fn(() => Promise.resolve({ path: "/home/sam/.local/bin/brindle", target: "/Applications/brindle.app/Contents/Resources/engine/bin/brindle" }));
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "settings" }} installCli={installCli} />);
    const cli = await screen.findByTestId("install-cli");
    const btn = within(cli).getByRole("button", { name: "Install command-line tool" });
    expect(btn).toHaveProperty("disabled", false);
    fireEvent.click(btn);
    await waitFor(() => expect(cli.textContent).toContain("Installed as /home/sam/.local/bin/brindle"));
    expect(installCli).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    sync.stop();
  });

  it("says what to do on a page with no engine at all", () => {
    renderApp(<App store={createAppStore()} client={null} />);
    expect(screen.getByTestId("engine-banner").textContent).toContain("this page has no engine");
    expect(screen.getByText("Nothing to show without an engine.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Upgrade the engine" })).toBeNull();
  });

  it("puts a stream the engine refuses, or one that dropped, in the banner when the engine itself is fine", () => {
    const ok: EngineStatus = { state: "ok", meta: new FakeDaemon().data.meta };
    expect(bannerStatus(ok, "open")).toBe(ok);
    expect(bannerStatus(ok, "unauthorized")).toMatchObject({ state: "unreachable", message: expect.stringContaining("refused the app's token") });
    expect(bannerStatus(ok, "closed")).toMatchObject({ state: "unreachable", message: expect.stringContaining("dropped") });
    const old: EngineStatus = { state: "upgrade-engine", meta: ok.meta };
    expect(bannerStatus(old, "closed")).toBe(old);
  });

  it("self-repair: an engine that dies comes back without a click: the app keeps reconnecting, and once the shell has restarted it, catches up and clears the banner", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [question] });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect, backoff: () => 5 });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    expect(await screen.findByRole("region", { name: "Session" }, { timeout: 4000 })).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("inbox-badge").textContent).toBe("1"));
    expect(screen.queryByTestId("engine-banner")).toBeNull();

    // The engine dies: the stream drops and nothing answers. The banner says so; the app asks for nothing.
    const before = daemon.requests.length;
    daemon.die();
    await waitFor(() => expect(screen.getByTestId("engine-banner").textContent).toContain("Can't reach the brindle engine"));
    // It keeps trying on its own (the shell starts the daemon again on the next attempt).
    await waitFor(() => expect(daemon.sockets.filter((s) => s.closed).length).toBeGreaterThan(2));
    expect(store.getState().connection).not.toBe("open");
    // Nothing was clicked: the snapshot and inbox are still what the dead engine left.
    expect(screen.getByTestId("inbox-badge").textContent).toBe("1");

    // Back, as a new process: the stream opens, the snapshot and inbox are fetched afresh and the engine is
    // checked again, with no click, and the banner goes. The inbox answered meanwhile shows the new state.
    daemon.data.inbox = [];
    daemon.revive();
    await waitFor(() => expect(store.getState().connection).toBe("open"));
    await waitFor(() => expect(screen.queryByTestId("engine-banner")).toBeNull());
    await waitFor(() => expect(screen.queryByTestId("inbox-badge")).toBeNull());
    const after = daemon.requests.slice(before).filter((r) => r.method === "GET").map((r) => r.path);
    expect(after).toContain("/snapshot");
    expect(after).toContain("/inbox");
    expect(after).toContain("/meta");
    expect(screen.getByTestId("terminal").textContent).toBe("sup1");
    expect(screen.queryByRole("alert")).toBeNull();
    sync.stop();
  });

  it("checks the engine on start even when its event stream never opens, and shows a dropped stream", async () => {
    const daemon = new FakeDaemon();
    const store = createAppStore();
    const client = daemon.client();
    // No stream at all: the store stays "connecting"; the check must not wait for it.
    const stuck: WebSocketFactory = () => ({ send() {}, close() {}, onopen: null, onmessage: null, onclose: null, onerror: null });
    daemon.token = "rotated";
    const sync = startSync({ client, store, connect: stuck });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    // The first-launch gate asks the engine first, without the stream either: it says what's wrong, with Retry,
    // rather than opening an app it can't know is set up.
    await waitFor(() => expect(screen.getByTestId("starting-failed").textContent).toContain("missing or wrong token"));
    // Back with the right token: the app, whose check didn't wait on the stream either.
    daemon.token = TOKEN;
    fireEvent.click(within(screen.getByTestId("starting-failed")).getByRole("button", { name: "Retry" }));
    await screen.findByRole("complementary", { name: "Sidebar" });
    expect(store.getState().connection).not.toBe("open");
    expect(screen.queryByTestId("engine-banner")).toBeNull();
    sync.stop();
    cleanup();

    // The engine answers, but its event stream is down: the banner says so until it is back.
    const live = new FakeDaemon();
    const store2 = createAppStore();
    const sync2 = startSync({ client: live.client(), store: store2, connect: live.connect, backoff: () => 5 });
    renderApp(<App store={store2} client={live.client()} refresh={sync2.refresh} />);
    await waitFor(() => expect(store2.getState().connection).toBe("open"));
    expect(screen.queryByTestId("engine-banner")).toBeNull();
    live.drop();
    await waitFor(() => expect(screen.getByTestId("engine-banner").textContent).toContain("the event stream dropped"));
    await waitFor(() => expect(screen.queryByTestId("engine-banner")).toBeNull());
    sync2.stop();
  });
});

describe("providers in the sidebar", () => {
  const statusLine = () => within(nav()).getByTestId("status-line");
  const start = (providers = freshProviders({ claude: { path: "/home/u/.local/bin/claude", version: "2.1.47" } })) => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], providers });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    return { daemon, stop: () => sync.stop() };
  };

  it("providers: the sidebar has no providers block, and its status line says nothing about providers while none has a problem", async () => {
    const { daemon, stop } = start();
    await waitFor(() => expect(treeAgent("sup1")).toBeTruthy());
    await waitFor(() => expect(daemon.requests.some((r) => r.path === "/providers/installed")).toBe(true));
    await flush();
    expect(nav().textContent).not.toMatch(/providers/i);
    expect(nav().textContent).not.toContain("Claude Code");
    expect(within(nav()).queryByRole("button", { name: /Connect|Sign in/ })).toBeNull();
    expect(within(nav()).queryByTestId("provider-problem")).toBeNull();
    // The footer's links stay the four pages, on one line.
    expect(within(screen.getByRole("navigation", { name: "Pages" })).getAllByRole("button").map((b) => b.textContent)).toEqual(["Knowledge", "Agents", "Settings", "Account"]);
    stop();
  });

  it("providers: a signed-out provider and a failed install show as warm words in the status line, signed out first, and open Settings › Providers", async () => {
    const before = Element.prototype.scrollIntoView;
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    const { daemon, stop } = start(
      freshProviders({ claude: { path: "/home/u/.local/bin/claude", version: "2.1.47" }, codex: { path: "/opt/homebrew/bin/codex", version: "0.48.0", signed_in: false } }),
    );
    await waitFor(() => expect(treeAgent("sup1")).toBeTruthy());
    const problem = await within(nav()).findByTestId("provider-problem");
    expect(problem.textContent).toBe("Codex signed out");
    expect(problem.getAttribute("data-kind")).toBe("signed_out");
    expect(problem.className).toContain("text-sap");
    expect(within(statusLine()).queryByTestId("provider-problem-more")).toBeNull();
    expect(statusLine().className).toContain("whitespace-nowrap");
    // Clicking the words: Settings, scrolled to Providers, where the row has its Sign in.
    fireEvent.click(problem);
    const codex = await screen.findByTestId("provider-codex");
    expect(within(codex).getByRole("button", { name: "Sign in to Codex" })).toBeTruthy();
    await waitFor(() => expect(scrolled.mock.contexts.some((el) => (el as Element).id === "providers")).toBe(true));
    expect(within(nav()).getByRole("button", { name: "Settings" }).getAttribute("aria-current")).toBe("page");

    // An install that fails reaches the status line too, after the sign-out: "+1", still there on another page.
    daemon.installFails.opencode = 1;
    fireEvent.click(within(screen.getByTestId("provider-opencode")).getByRole("button", { name: "Connect OpenCode" }));
    await screen.findByTestId("provider-install-opencode-failed");
    await waitFor(() => expect(within(statusLine()).getByTestId("provider-problem-more").textContent).toBe("+1"));
    expect(within(statusLine()).getByTestId("provider-problem").textContent).toContain("Codex signed out");
    fireEvent.click(link("Knowledge"));
    expect(within(statusLine()).getByTestId("provider-problem-more").textContent).toBe("+1");
    // Back through the words; the failed install let go, the sign-out is all that's left.
    fireEvent.click(within(statusLine()).getByTestId("provider-problem"));
    fireEvent.click(within(await screen.findByTestId("provider-install-opencode-failed")).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(within(statusLine()).queryByTestId("provider-problem-more")).toBeNull());
    // An install failure alone says so in its own words.
    fireEvent.click(within(screen.getByTestId("provider-codex")).getByRole("button", { name: "Remove Codex from brindle" }));
    await waitFor(() => expect(within(nav()).queryByTestId("provider-problem")).toBeNull());
    daemon.installFails.antigravity = 1;
    fireEvent.click(within(screen.getByTestId("provider-antigravity")).getByRole("button", { name: "Connect Antigravity" }));
    await waitFor(() => expect(within(nav()).getByTestId("provider-problem").textContent).toBe("Antigravity install failed"));
    expect(screen.queryByRole("dialog")).toBeNull();
    Element.prototype.scrollIntoView = before;
    stop();
  });
});

describe("closing a session", () => {
  /* A second live session in the same repo, on its own worktree. */
  const other: SessionDTO = { ...session, id: "sup2", workspace_id: "proj/other", goal: "Fix the cache" };
  const otherWs = { ...root, id: "proj/other", name: "other", branch: "other", path: `${REPO}/.brindle/worktrees/other`, agents: [{ ...root.agents![0], id: "sup2", window: windowOf("sup2") }] };
  const done: GoalDTO = { ...goal, state: "done", goal_phase: "done" };
  const posts = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "POST").map((r) => r.path);
  const sessionRow = (id: string) => within(nav()).queryByTestId(`tree-session-${id}`);

  it("Done → Close session: one click, the session leaves the sidebar at once and the view moves to the next session", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, other], workspaces: [root, worker, otherWs], goal: done });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO, agent: "sup1" }} />);
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup1"));
    const context = within(nav()).getByTestId("session-context");
    // Beside the Done status: a primary Close session.
    const close = await within(context).findByRole("button", { name: "Close session" });
    expect(within(context).getByTestId("goal-phase").textContent).toBe("Done");
    expect(close.parentElement).toBe(within(context).getByTestId("goal-line").parentElement);
    expect(sessionRow("sup1")).toBeTruthy();

    fireEvent.click(close);
    // At once, before the engine answers: no confirmation, no "Closing…", the row gone, the next session on screen.
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(/Closing/)).toBeNull();
    expect(sessionRow("sup1")).toBeNull();
    expect(sessionRow("sup2")).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup2"));
    await waitFor(() => expect(posts(daemon)).toEqual(["/sessions/sup1/close"]));
    // The engine closed the whole session (its worker too) and keeps it out of the list; branches stay.
    expect(daemon.data.closed.map((a) => a.id)).toEqual(["sup1", "w1"]);
    expect(daemon.data.workspaces.map((w) => w.id)).toEqual(["proj", "proj/feat-ls", "proj/other"]);
    await waitFor(() => expect(daemon.requests.filter((r) => r.path === "/sessions").length).toBeGreaterThan(2));
    expect(sessionRow("sup1")).toBeNull();
    expect(within(nav()).queryByTestId("close-error")).toBeNull();
    sync.stop();
  });

  it("the ⋯ menu's Close session says what still runs, closes in one click and moves to the Inbox when no session is left", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], goal });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO, agent: "sup1" }} />);
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup1"));
    await waitFor(() => expect(treeAgent("w1")).toBeTruthy());
    // Not Done: the goal panel has no Close session; the ⋯ has it, saying w1 runs and feat/ls isn't merged.
    expect(within(nav()).queryByRole("button", { name: "Close session" })).toBeNull();
    fireEvent.click(within(screen.getByTestId("session-bar")).getByRole("button", { name: "More actions" }));
    const item = within(screen.getByRole("menu")).getByTestId("close-session");
    expect(item.textContent).toBe("Close session · 1 worker running · 1 unmerged branch");

    fireEvent.click(item);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(sessionRow("sup1")).toBeNull();
    expect(await screen.findByRole("region", { name: "Inbox" })).toBeTruthy();
    await waitFor(() => expect(posts(daemon)).toEqual(["/sessions/sup1/close"]));
    expect(daemon.data.closed.map((a) => a.id)).toEqual(["sup1", "w1"]);
    sync.stop();
  });

  it("the ⋯ menu's Close session is the bare label when nothing runs and everything is merged, from a worker's menu too", async () => {
    const quietWorker = { ...worker, ahead: 0, dirty: 0, agents: [{ ...worker.agents![0], status: "done" }] };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, other], workspaces: [root, quietWorker, otherWs], goal });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO, agent: "w1" }} />);
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("w1"));
    fireEvent.click(within(screen.getByTestId("session-bar")).getByRole("button", { name: "More actions" }));
    const item = within(screen.getByRole("menu")).getByTestId("close-session");
    expect(item.textContent).toBe("Close session");
    fireEvent.click(item);
    expect(sessionRow("sup1")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup2"));
    await waitFor(() => expect(posts(daemon)).toEqual(["/sessions/sup1/close"]));
    sync.stop();
  });

  it("a paused session whose root is in no snapshot still has the ⋯ with Close session, one click", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, paused], workspaces: [root, worker], goal });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup0", repo: REPO, agent: "sup0" }} />);
    const pausedRow = () => nav().querySelector('[data-tree-session="sup0"]');
    await waitFor(() => expect(pausedRow()).toBeTruthy());
    await waitFor(() => expect(store.getState().synced).toBe(true));
    expect(screen.queryByTestId("agent-menu")).toBeNull();
    fireEvent.click(within(screen.getByTestId("session-bar")).getByRole("button", { name: "More actions" }));
    const item = within(screen.getByRole("menu")).getByTestId("close-session");
    expect(item.textContent).toBe("Close session");
    fireEvent.click(item);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(pausedRow()).toBeNull();
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup1"));
    await waitFor(() => expect(posts(daemon)).toEqual(["/sessions/sup0/close"]));
    expect(daemon.data.closed.map((a) => a.id)).toEqual(["sup0"]);
    sync.stop();
  });

  it.each([
    ["its own workspace kept, with work in it", [] as string[], "Close session · 1 unmerged branch"],
    ["a worker under it still running there", ["w9"], "Close session · 1 worker running · 1 unmerged branch"],
  ])("a paused session with no snapshot root still says what its close stops: %s", async (_, running, label) => {
    const cache: SessionDTO = { ...paused, workspace_id: "proj/cache" };
    const cacheWs = {
      ...worker,
      id: "proj/cache",
      name: "cache",
      branch: "cache",
      path: `${REPO}/.brindle/worktrees/cache`,
      ahead: 3,
      dirty: 1,
      agents: running.map((id) => ({ ...worker.agents![0], id, parent_id: "sup0" })),
    };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, cache], workspaces: [root, worker, cacheWs], goal });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup0", repo: REPO, agent: "sup0" }} />);
    await waitFor(() => expect(nav().querySelector('[data-tree-session="sup0"]')).toBeTruthy());
    await waitFor(() => expect(store.getState().synced).toBe(true));
    fireEvent.click(within(screen.getByTestId("session-bar")).getByRole("button", { name: "More actions" }));
    expect(within(screen.getByRole("menu")).getByTestId("close-session").textContent).toBe(label);
    sync.stop();
  });

  it("counts the supervisor's own workspace: a session on its feature branch with commits ahead says so, no worker running", async () => {
    const feature = { ...root, branch: "feat/cache", base_branch: "main", ahead: 3, dirty: 0 };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [feature], goal });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO, agent: "sup1" }} />);
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup1"));
    fireEvent.click(within(screen.getByTestId("session-bar")).getByRole("button", { name: "More actions" }));
    expect(within(screen.getByRole("menu")).getByTestId("close-session").textContent).toBe("Close session · 1 unmerged branch");
    sync.stop();
  });

  it("a closed session stays out of the sidebar when the refresh after the close fails", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, other], workspaces: [root, worker, otherWs], goal: done });
    let closed = false;
    let refused = 0;
    const client = new DaemonClient({
      baseUrl: BASE_URL,
      token: TOKEN,
      fetch: async (input, init) => {
        const url = String(input);
        if (closed && url.includes("/v1/sessions?")) {
          refused++;
          return Promise.reject(new TypeError("connection refused"));
        }
        const response = await daemon.fetch(input, init);
        if (url.endsWith("/v1/sessions/sup1/close")) closed = true;
        return response;
      },
    });
    const store = createAppStore();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO, agent: "sup1" }} />);
    // The app shows once its first-launch gate has asked the engine.
    fireEvent.click(await within(await screen.findByRole("complementary", { name: "Sidebar" })).findByRole("button", { name: "Close session" }));
    await waitFor(() => expect(posts(daemon)).toEqual(["/sessions/sup1/close"]));
    await waitFor(() => expect(refused).toBeGreaterThan(0));
    await flush();
    // The old list still has sup1, but the engine closed it: it stays out, and no close error is said.
    expect(sessionRow("sup1")).toBeNull();
    expect(sessionRow("sup2")).toBeTruthy();
    expect(within(nav()).queryByTestId("close-error")).toBeNull();
    sync.stop();
  });

  it("a close the engine refuses brings the session back to the sidebar and says why", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, other], workspaces: [root, worker, otherWs], goal: done });
    const client = new DaemonClient({
      baseUrl: BASE_URL,
      token: TOKEN,
      fetch: (input, init) =>
        String(input).endsWith("/v1/sessions/sup1/close")
          ? Promise.resolve(new Response(JSON.stringify({ error: { code: "conflict", message: "the engine is busy" } }), { status: 409, headers: { "Content-Type": "application/json" } }))
          : daemon.fetch(input, init),
    });
    const store = createAppStore();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO, agent: "sup1" }} />);
    // The app shows once its first-launch gate has asked the engine.
    fireEvent.click(await within(await screen.findByRole("complementary", { name: "Sidebar" })).findByRole("button", { name: "Close session" }));
    expect(sessionRow("sup1")).toBeNull();
    await waitFor(() => expect(within(nav()).getByTestId("close-error").textContent).toContain("Couldn't close sup1"));
    expect(sessionRow("sup1")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    sync.stop();
  });
});
