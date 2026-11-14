import { connectDaemon, renderApp, trackedSync as startSync } from "./test/harness";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import * as shell from "@tauri-apps/api/core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { landing } from "./App";
import type { WebSocketFactory } from "./api/events";
import type { GoalDTO, SessionDTO } from "./api/schema";
import { LAST_SESSION_KEY } from "./routes";
import { ONBOARDED_KEY } from "./api/providers";
import { createAppStore, initialState } from "./store/store";
import { DaemonClient } from "./api/client";
import { BASE_URL, FakeDaemon, TOKEN, flush, freshProviders } from "./test/fakeDaemon";
import { REPO, question, root, windowOf, worker } from "./test/fixtures";

vi.mock("./components/PtyTerminal", () => import("./test/panes"));
vi.mock("@tauri-apps/api/core", { spy: true });

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/* The app as the shell shows it: its panes through pty views (stand-ins here). */


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
const treeSession = (id: string) => nav().querySelector<HTMLButtonElement>(`button[data-tree-session="${id}"]`)!;
const surfaceTitle = () => screen.getByTestId("surface-title").textContent;
/* The session's pane is held back (the engine is still bringing sessions back): its calm card, no terminal. */
const paneWaits = () => screen.queryByTestId("state-card")?.dataset.kind === "returning" && !screen.queryByTestId("terminal");

describe("App", () => {
  beforeEach(() => localStorage.setItem(ONBOARDED_KEY, "1"));
  it("opens an Inbox review card's design in the Library on the card's own repo, and a question to the supervisor stays in it", async () => {
    const OTHER = "/home/me/other";
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }], sessions: [], workspaces: [], inbox: [] });
    const plan = { id: "lib-1", session: "", title: "Workers", rev: 1, state: "open" as const, markdown: "", lede: "Workers run here.", sections: [], decisions: [], split: [], queued: 0, created_at: 1 };
    daemon.seedLive("lib-1", { lede: "Workers run here." }, { plan, me: "Bo", author: "Ann" });
    const { client, store, sync } = connectDaemon(daemon);
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "shared-design", doc: "lib-1", title: "Workers", repo: OTHER }} />);
    const words = (await screen.findByText("Workers run here.")).firstChild!;
    // In the Library, with its navigation, on the card's repo.
    expect(screen.getByRole("region", { name: "Library" })).toBeTruthy();
    expect(daemon.requests.find((r) => r.path === "/shared-designs/lib-1")?.query.repo).toBe(OTHER);
    const range = document.createRange();
    range.setStart(words, 0);
    range.setEnd(words, 7);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    fireEvent.mouseUp(screen.getByTestId("plan-doc"));
    fireEvent.click(within(screen.getByRole("toolbar", { name: "For the highlighted text" })).getByRole("button", { name: /^Ask/ }));
    fireEvent.change(within(screen.getByTestId("thread-draft")).getByRole("textbox"), { target: { value: "Which machine?" } });
    await act(async () => fireEvent.submit(screen.getByTestId("thread-draft")));
    const asked = daemon.requests.find((r) => r.method === "POST" && r.path === "/shared-designs/lib-1/comments");
    expect(asked?.body).toMatchObject({ question: true, repo: OTHER });
    sync.stop();
  });
  it("keeps recovery visible in the session row and header until it succeeds or fails", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [{ ...session, status: "paused" }], workspaces: [], inbox: [{ ...question, kind: "failed", agent_id: "sup1", root_id: "sup1" }], preflight: { claude: [{ id: "trust", kind: "trust", label: "Trust folder", done: false }] } });
    const { client, store, sync } = connectDaemon(daemon);
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO }} />);
    const states = () => screen.getAllByTestId("session-status").map((el) => el.getAttribute("data-state"));
    await waitFor(() => expect(states()).toEqual(["paused", "paused"]));
    const startControls = () => document.querySelectorAll('[data-session-start="sup1"]');
    fireEvent.click(screen.getByTestId("session-bar").querySelector<HTMLButtonElement>('[data-session-start="sup1"]')!);
    await screen.findByTestId("preflight");
    daemon.data.sessions = [{ ...session, status: "resuming" }];
    daemon.data.workspaces = [root, worker];
    daemon.publish("agent.status", { repo_root: REPO, agent_id: "sup1", payload: { status: "starting" } });
    await waitFor(() => expect(states()).toEqual(["resuming", "resuming"]));
    const row = screen.getByTestId("tree-session-sup1");
    expect(screen.queryByTestId("paused-toggle-proj")).toBeNull();
    expect(row.querySelector('.session-root-dot[data-tone="work"]')).toBeTruthy();
    expect(row.querySelector('[data-testid="session-close-sup1"], [data-testid="session-done-sup1"]')).toBeNull();
    expect(screen.getByTestId("session-bar").querySelector('[data-state="resuming"][data-tone="work"]')).toBeTruthy();
    expect(screen.getByTestId("state-card").getAttribute("data-kind")).toBe("returning");
    expect(startControls()).toHaveLength(0);
    expect(screen.queryByTestId("agent-menu")).toBeNull();
    await waitFor(() => expect(treeAgent("w1")).toBeTruthy());
    fireEvent.click(treeAgent("w1"));
    expect(screen.getByTestId("state-card").getAttribute("data-kind")).toBe("returning");
    expect(startControls()).toHaveLength(0);
    expect(screen.queryByTestId("agent-menu")).toBeNull();
    fireEvent.click(screen.getByTestId("inbox-link"));
    await screen.findByTestId("inbox-card");
    expect(startControls()).toHaveLength(0);
    fireEvent.click(treeSession("sup1"));
    await waitFor(() => expect(states()).toEqual(["resuming", "resuming"]));
    daemon.data.sessions = [{ ...session, status: "paused" }];
    daemon.data.workspaces = [];
    daemon.publish("agent.status", { repo_root: REPO, agent_id: "sup1", payload: { status: "paused" } });
    await waitFor(() => expect(states()).toEqual(["paused", "paused"]));
    expect(startControls().length).toBeGreaterThan(0);
    daemon.data.sessions = [session];
    daemon.data.workspaces = [root];
    daemon.publish("agent.status", { repo_root: REPO, agent_id: "sup1", payload: { status: "idle" } });
    await waitFor(() => expect(states()).toEqual(["idle", "idle"]));
  });
  it("startup steps are timed", async () => {
    const native = vi.spyOn(shell, "isTauri").mockReturnValue(true);
    const invoke = vi.spyOn(shell, "invoke").mockResolvedValue(undefined);
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const { client, store, sync } = connectDaemon(daemon);
    try {
      renderApp(<App store={store} client={client} refresh={sync.refresh} />);
      await screen.findByTestId("terminal");
      await waitFor(() => {
        const steps = invoke.mock.calls.filter(([command]) => command === "app_startup_step").map(([, args]) => (args as { step: string }).step);
        expect(steps).toEqual(expect.arrayContaining(["meta", "snapshot", "sessions_feed", "shell_painted", "panes_ready"]));
      });
    } finally { sync.stop(); native.mockRestore(); invoke.mockRestore(); }
  });
  it("a sync read superseded by the gate's snapshot is read again, so the inbox still syncs without another event", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const client = daemon.client();
    const store = createAppStore();
    // The sync's first inbox read is still out when the gate (daemon not recovering) applies its own snapshot.
    const read = client.inbox.bind(client);
    let finish!: () => void;
    vi.spyOn(client, "inbox").mockImplementationOnce(() => new Promise((ok) => { finish = () => void read().then(ok); }));
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO }} />);
    await screen.findByTestId("terminal");
    expect(store.getState().synced).toBe(false);
    await act(async () => { finish(); await flush(); await sync.idle(); });
    expect(store.getState().synced).toBe(true);
  });
  it.each([windowOf("new"), null])("a gate read that a later sync read overtook doesn't put its older rows over the newer ones (newer window: %s)", async (newer) => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const client = daemon.client();
    const store = createAppStore();
    const read = client.snapshot.bind(client);
    let finishGate: ((rows: typeof daemon.data.workspaces) => void) | undefined;
    // Sync's first read, then the gate's (slow), then a sync refresh begun after it that answers first; later reads are slow.
    vi.spyOn(client, "snapshot").mockImplementationOnce(read).mockImplementationOnce(() => new Promise((ok) => { finishGate = ok; }))
      .mockImplementationOnce(read).mockReturnValue(new Promise(() => {}));
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO }} />);
    await waitFor(() => expect(finishGate).toBeDefined());
    daemon.data.workspaces = [{ ...root, agents: root.agents?.map((a) => ({ ...a, window: newer })) }];
    await act(async () => { await sync.refresh(); });
    await act(async () => { finishGate!([{ ...root, agents: root.agents?.map((a) => ({ ...a, window: windowOf("old") })) }]); await flush(); });
    expect(store.getState().workspaces[0].agents?.[0].window).toBe(newer);
    // The rows the store kept decide whether the pane may show, not the older ones it turned down.
    expect(paneWaits()).toBe(newer === null);
  });

  it("shows the app at once while sessions come back; only the pane waits until they are ready", async () => {
    vi.useFakeTimers();
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const { client, store, sync } = connectDaemon(daemon);
    let resuming = true;
    const meta = client.meta.bind(client);
    const metadata = vi.spyOn(client, "meta").mockImplementation(async () => ({ ...await meta(), resuming }));
    daemon.data.workspaces = [{ ...root, agents: root.agents?.map((a) => ({ ...a, window: null })) }];
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    // No "Starting" in front of it all: the sidebar and the session are there, the pane says it's coming.
    expect(screen.queryByTestId("starting")).toBeNull();
    expect(screen.getByRole("complementary", { name: "Sidebar" })).toBeTruthy();
    expect(paneWaits()).toBe(true);
    resuming = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(paneWaits()).toBe(true);
    daemon.data.workspaces = [root, { ...worker, agents: worker.agents?.map((a) => ({ ...a, provider: "subagent", window: null })) }];
    let resolve!: (rows: Awaited<ReturnType<typeof client.snapshot>>) => void;
    vi.spyOn(client, "snapshot").mockReturnValueOnce(new Promise((ok) => { resolve = ok; }));
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    // Reopening restores the connection string, but the old read stays stale.
    await act(async () => {
      store.dispatch({ type: "connection", state: "closed" });
      store.dispatch({ type: "connection", state: "open" });
      resuming = true;
      resolve([root]);
    });
    expect(paneWaits()).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(paneWaits()).toBe(true);
    // Recovery starts after an apparently ready check, before releasing the gate.
    metadata.mockImplementationOnce(async () => ({ ...await meta(), resuming: false }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(paneWaits()).toBe(true);
    resuming = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(paneWaits()).toBe(false);
    expect(screen.getByTestId("terminal").textContent).toBe("sup1");
    // A blip of the same daemon leaves the pane be: no flash of "Bringing … back".
    daemon.drop();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(paneWaits()).toBe(false);
    expect(screen.getByTestId("terminal").textContent).toBe("sup1");
  });

  it.each(["running", "resuming", "no window", "recovered"])("a reopen onto a running daemon releases only ready panes from the first snapshot (%s)", async (state) => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const client = daemon.client();
    const meta = client.meta.bind(client);
    vi.spyOn(client, "meta").mockImplementation(async () => ({ ...await meta(), resuming: state === "resuming", recovered_at: state === "recovered" ? client.createdAt - 1 : undefined }));
    const rows = state === "no window" ? [{ ...root, agents: root.agents?.map((a) => ({ ...a, window: null })) }] : [root];
    vi.spyOn(client, "snapshot").mockResolvedValueOnce(rows).mockReturnValue(new Promise(() => {}));
    vi.spyOn(client, "inbox").mockReturnValue(new Promise(() => {}));
    const store = createAppStore({ ...initialState, workspaces: [{ ...root, agents: root.agents?.map((a) => ({ ...a, window: windowOf("old") })) }] });
    const refresh = () => Promise.all([client.snapshot(), client.inbox()]).then(() => {});
    renderApp(<App store={store} client={client} refresh={refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO }} />);
    await screen.findByRole("complementary", { name: "Sidebar" });
    await act(async () => { await flush(); });
    if (state === "resuming" || state === "no window") {
      await waitFor(() => expect(paneWaits()).toBe(true));
    } else {
      await screen.findByTestId("terminal");
      expect(store.getState().workspaces).toEqual([root]);
      expect(store.getState().synced).toBe(false);
    }
  });

  it.each(["agent.closed", "rewound"])("discards the gate snapshot when %s arrives during its read", async (kind) => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker] });
    const { client, store, sync } = connectDaemon(daemon);
    await sync.idle();
    await flush();
    await sync.idle();
    let finish!: (rows: typeof daemon.data.workspaces) => void;
    const snapshot = vi.spyOn(client, "snapshot").mockImplementationOnce(() => new Promise((ok) => { finish = ok; })).mockRejectedValue(new Error("offline"));
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO }} />);
    await waitFor(() => expect(snapshot).toHaveBeenCalledTimes(1));
    await act(async () => {
      daemon.publish(kind === "rewound" ? "agent.status" : kind, { repo_root: REPO, agent_id: "w1", payload: { status: "rewound" } });
      await flush();
      await sync.idle();
      finish([root, worker]);
      await flush();
    });
    expect(store.getState().pastWorkers).toContain("w1");
    expect(store.getState().workspaces.flatMap((ws) => ws.agents ?? []).some((a) => a.id === "w1")).toBe(false);
    expect(paneWaits()).toBe(true);
  });

  it.each(["prioritize a pending session", "a snapshot still in flight", "events keeping the store stale"])("shows a recovered pane while other sessions wait: %s", async (scenario) => {
    vi.useFakeTimers();
    const rootB = { ...root, id: "proj2", agents: root.agents?.map((a) => ({ ...a, id: "sup2", window: null })) };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, { ...session, id: "sup2" }], workspaces: [root, rootB] });
    const client = daemon.client();
    const store = createAppStore();
    const meta = client.meta.bind(client);
    vi.spyOn(client, "meta").mockImplementation(async () => {
      const got = await meta();
      if (scenario === "events keeping the store stale") daemon.publish("agent.status", { repo_root: REPO, agent_id: "sup2", payload: { status: "starting" } });
      return { ...got, resuming: true, recovery: { done: 1, total: 2, pending: ["sup2"] } };
    });
    let finish!: (rows: typeof daemon.data.workspaces) => void;
    if (scenario === "a snapshot still in flight") {
      const read = client.snapshot.bind(client);
      vi.spyOn(client, "snapshot").mockImplementationOnce(() => new Promise((ok) => { finish = ok; })).mockImplementationOnce(read).mockReturnValue(new Promise(() => {}));
    } else if (scenario === "events keeping the store stale") {
      const inbox = client.inbox.bind(client);
      vi.spyOn(client, "inbox").mockImplementationOnce(inbox).mockReturnValue(new Promise(() => {}));
    }
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO }} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(scenario === "a snapshot still in flight" ? 300 : 1500); });
    expect(screen.getByTestId("terminal").textContent).toBe("sup1");
    if (scenario === "a snapshot still in flight") {
      await act(async () => { finish([{ ...root, agents: root.agents?.map((a) => ({ ...a, window: windowOf("old") })) }, rootB]); });
      await act(async () => { await vi.advanceTimersByTimeAsync(300); });
      expect(store.getState().workspaces.find((w) => w.id === "proj")?.agents?.[0].window).toBe(windowOf("sup1"));
      expect(screen.getByTestId("terminal").textContent).toBe("sup1");
    } else if (scenario === "events keeping the store stale") {
      expect(daemon.viewed).toEqual(["sup1"]);
    } else {
      expect(paneWaits()).toBe(false);
      expect(daemon.prioritized).toEqual([]);
      fireEvent.click(treeSession("sup2"));
      await act(async () => { await vi.advanceTimersByTimeAsync(500); });
      expect(paneWaits()).toBe(true);
      expect(daemon.prioritized).toEqual(["sup2"]);
    }
  });

  it("panes per session: a session that finishes coming back while its rows are being read is not shown from the old rows", async () => {
    vi.useFakeTimers();
    const at = (id: string, proj: string, window: string | null) => ({ ...root, id: proj, agents: root.agents?.map((a) => ({ ...a, id, window })) });
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }],
      sessions: [session, { ...session, id: "sup2" }, { ...session, id: "sup3" }],
      workspaces: [root, at("sup2", "proj2", windowOf("old")), at("sup3", "proj3", null)],
    });
    const { client, store, sync } = connectDaemon(daemon);
    let pending = ["sup2", "sup3"];
    const meta = client.meta.bind(client);
    vi.spyOn(client, "meta").mockImplementation(async () => ({ ...await meta(), resuming: true, recovery: { done: 1, total: 3, pending } }));
    const snapshot = client.snapshot.bind(client);
    // sup2 finishes coming back after its rows were read, before the engine is asked again.
    let reads = 0;
    // The sync's own first read comes first; the gate's read is the second.
    vi.spyOn(client, "snapshot").mockImplementation(async () => { const rows = await snapshot(); if (++reads === 2) pending = ["sup3"]; return rows; });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO }} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(screen.getByTestId("terminal").textContent).toBe("sup1");
    fireEvent.click(treeSession("sup2"));
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(paneWaits()).toBe(true);
  });

  it("panes per session: a released session reopens a pane that closed while the engine was away, though its agent row is unchanged", async () => {
    vi.useFakeTimers();
    const at = (id: string, proj: string, window: string | null) => ({ ...root, id: proj, agents: root.agents?.map((a) => ({ ...a, id, window })) });
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }],
      sessions: [session, { ...session, id: "sup2" }, { ...session, id: "sup3" }],
      workspaces: [root, at("sup2", "proj2", windowOf("sup2")), at("sup3", "proj3", null)],
    });
    const { client, store, sync } = connectDaemon(daemon);
    let live = { pid: 0, pending: ["sup3"] };
    const meta = client.meta.bind(client);
    vi.spyOn(client, "meta").mockImplementation(async () => {
      const got = await meta();
      return { ...got, pid: got.pid + live.pid, resuming: true, recovery: { done: 1, total: 3, pending: live.pending } };
    });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup2", repo: REPO }} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(screen.getByTestId("terminal").textContent).toBe("sup2");
    fireEvent.click(screen.getByTestId("terminal-close"));
    expect(screen.queryByTestId("terminal")).toBeNull();
    // A new engine brings sup2 back: the same row, so only the session's release says its pane is new.
    live = { pid: 1, pending: ["sup2", "sup3"] };
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(paneWaits()).toBe(true);
    live = { pid: 1, pending: ["sup3"] };
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(screen.getByTestId("terminal").textContent).toBe("sup2");
    // Another session coming back leaves this one's pane be.
    const pane = screen.getByTestId("terminal");
    daemon.data.workspaces = [root, at("sup2", "proj2", windowOf("sup2")), at("sup3", "proj3", windowOf("sup3"))];
    live = { pid: 1, pending: [] };
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(store.getState().workspaces.find((w) => w.id === "proj3")?.agents?.[0].window).toBe(windowOf("sup3"));
    expect(screen.getByTestId("terminal")).toBe(pane);
  });

  it("test_update_relaunch: a session's pane shown during recovery is not reopened when the recovery ends", async () => {
    vi.useFakeTimers();
    const sup2 = { ...root, id: "proj2", agents: root.agents?.map((a) => ({ ...a, id: "sup2", window: windowOf("sup2") })) };
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }],
      sessions: [session, { ...session, id: "sup2" }],
      workspaces: [root, sup2],
    });
    const client = daemon.client();
    const store = createAppStore();
    const sync = startSync({ client, store, connect: daemon.connect });
    let resuming = true;
    const meta = client.meta.bind(client);
    vi.spyOn(client, "meta").mockImplementation(async () => ({ ...await meta(), resuming, ...(resuming ? { recovery: { done: 1, total: 2, pending: ["sup1"] } } : {}) }));
    try {
      renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup2", repo: REPO }} />);
      await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
      const pane = screen.getByTestId("terminal");
      expect(pane.textContent).toBe("sup2");
      resuming = false;
      await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
      expect(screen.getByTestId("terminal")).toBe(pane);
    } finally { sync.stop(); vi.useRealTimers(); }
  });

  it.each([false, true])("releases the pane from its first post-recovery read while recovery changes (other sessions: %s)", async (others) => {
    vi.useFakeTimers();
    const at = (id: string, proj: string, window: string | null) => ({ ...root, id: proj, agents: root.agents?.map((a) => ({ ...a, id, window })) });
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }],
      sessions: others ? [session, { ...session, id: "sup2" }, { ...session, id: "sup3" }] : [session],
      workspaces: others ? [root, at("sup2", "proj2", null), at("sup3", "proj3", null)] : [{ ...root, agents: root.agents?.map((a) => ({ ...a, window: null })) }],
    });
    const client = daemon.client();
    const store = createAppStore();
    const SLOW = 4000;
    let pending = others ? ["sup2", "sup3"] : ["sup1"];
    let resuming = true, reads = 0;
    const meta = client.meta.bind(client);
    vi.spyOn(client, "meta").mockImplementation(async () => ({ ...await meta(), resuming, recovery: resuming ? { done: (others ? 3 : 1) - pending.length, total: others ? 3 : 1, pending } : undefined }));
    const read = client.snapshot.bind(client);
    vi.spyOn(client, "snapshot").mockImplementation(async () => {
      reads++;
      const rows = await read();
      return new Promise((ok) => setTimeout(() => ok(rows), SLOW));
    });
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO }} />);
    if (others) {
      while (reads < 2) await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      await act(async () => { await vi.advanceTimersByTimeAsync(500); });
      daemon.data.workspaces = [root, at("sup2", "proj2", windowOf("sup2")), at("sup3", "proj3", null)];
      pending = ["sup3"];
      await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
      daemon.data.workspaces = [root, at("sup2", "proj2", windowOf("sup2")), at("sup3", "proj3", windowOf("sup3"))];
      pending = [];
      await act(async () => { await vi.advanceTimersByTimeAsync(SLOW - 2100); });
    } else {
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(screen.queryByTestId("terminal")).toBeNull();
      daemon.data.workspaces = [root];
      pending = [];
      await act(async () => { await vi.advanceTimersByTimeAsync(600); });
      resuming = false;
      await act(async () => { await vi.advanceTimersByTimeAsync(SLOW + 500); });
    }
    expect(screen.getByTestId("terminal").textContent).toBe("sup1");
  });

  it("recovery progress line: counts sessions on the waiting pane and goes once it is ready", async () => {
    vi.useFakeTimers();
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const { client, store, sync } = connectDaemon(daemon);
    let live: { resuming: boolean; recovery: { done: number; total: number } | null } = { resuming: true, recovery: { done: 3, total: 6 } };
    const meta = client.meta.bind(client);
    vi.spyOn(client, "meta").mockImplementation(async () => ({ ...await meta(), ...live }));
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(screen.getByTestId("recovery-progress").textContent).toBe("Reopening sessions, 3 of 6");
    live = { resuming: false, recovery: null };
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(paneWaits()).toBe(false);
    expect(screen.queryByTestId("recovery-progress")).toBeNull();
  });

  it.each([false, true])("shows from the first read once recovery ends (daemon changed: %s)", async (changed) => {
    vi.useFakeTimers();
    const old = { ...root, agents: root.agents?.map((a) => ({ ...a, window: windowOf("old") })) };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [old] });
    const { client, store, sync } = connectDaemon(daemon);
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    await sync.idle();
    daemon.data.workspaces = [root];
    const meta = client.meta.bind(client);
    let resuming = true;
    let pidOffset = 0;
    vi.spyOn(client, "meta").mockImplementation(async () => {
      const got = await meta();
      return { ...got, resuming, pid: got.pid + pidOffset };
    });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(store.getState().workspaces).toEqual([old]);
    expect(paneWaits()).toBe(true);
    // Only the first post-recovery read returns; the inbox and subsequent reads never do.
    vi.spyOn(client, "snapshot").mockImplementationOnce(async () => { if (changed) pidOffset = 1; return [root]; }).mockReturnValue(new Promise(() => {}));
    vi.spyOn(client, "inbox").mockReturnValue(new Promise(() => {}));
    resuming = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    if (changed) expect(paneWaits()).toBe(true);
    else expect(screen.getByTestId("terminal").dataset.target).toBe(windowOf("sup1"));
  });

  it("a slow engine read holds back nothing but the pane, which opens at the startup deadline and discards the pending response", async () => {
    vi.useFakeTimers();
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const client = daemon.client();
    let resolve!: (rows: Awaited<ReturnType<typeof client.snapshot>>) => void;
    vi.spyOn(client, "snapshot").mockReturnValue(new Promise((ok) => { resolve = ok; }));
    const store = createAppStore();
    store.dispatch({ type: "snapshot", workspaces: [root], inbox: [] });
    renderApp(<App store={store} client={client} initialRoute={{ screen: "session", root: "sup1", repo: REPO }} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    // The first screen is up and usable at once, from what the store already has.
    expect(screen.getByRole("complementary", { name: "Sidebar" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Session" })).toBeTruthy();
    expect(paneWaits()).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(screen.getByTestId("terminal").textContent).toBe("sup1");
    await act(async () => { resolve([worker]); });
    expect(store.getState().workspaces).toEqual([root]);
    expect(screen.queryByTestId("starting")).toBeNull();
  });

  it.each([false, true])("resume paused: lists the goal and resumes into the session's chat pane (already selected: %s)", async (selected) => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, paused], workspaces: [root] });
    const { client, store, sync } = connectDaemon(daemon);
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={selected ? { screen: "session", root: "sup0", repo: REPO } : { screen: "new" }} />);
    await screen.findByRole("complementary", { name: "Sidebar" });
    if (!selected) fireEvent.click(await within(nav()).findByRole("button", { name: /Paused · 1/ }));
    // waitFor only retries on a throw: a null row would otherwise end the wait early.
    const row = await waitFor(() => {
      const found = nav().querySelector('[data-tree-session="sup0"]')?.closest("li");
      if (!found) throw new Error("the paused session isn't listed yet");
      return found;
    });
    expect(row.textContent).toContain("Refactor the cache");
    // Opened paused, the session says nothing of being paused under it.
    expect(screen.queryByText("This session was paused")).toBeNull();
    fireEvent.click(within(row).getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/sessions/sup0/continue")).toHaveLength(1));
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup0"));
    expect(JSON.parse(localStorage.getItem(LAST_SESSION_KEY)!)).toEqual({ root: "sup0", repo: REPO });
  });

  it("session was paused: keeps the shown session and offers Reopen after another session pauses it", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const { client, store, sync } = connectDaemon(daemon);
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
  });

  it("restores page and terminal scroll after Settings, navigates with shortcuts and scopes a paused session's goal", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, paused], workspaces: [root, worker], goal });
    const { client, store, sync } = connectDaemon(daemon);
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    await screen.findByRole("region", { name: "Session" });

    fireEvent.click(link(/^Inbox/));
    const previousPage = await screen.findByRole("region", { name: "Inbox" });
    const main = screen.getByRole("main");
    main.scrollTop = 180;
    fireEvent.click(link("Open Account › Usage"));
    await screen.findByTestId("usage");
    expect(main.scrollTop).toBe(0);
    expect(within(main).queryByRole("tablist")).toBeNull();
    expect(within(nav()).getByRole("tablist", { name: "Settings" })).toBeTruthy();
    expect(within(nav()).queryByRole("navigation", { name: "Sessions" })).toBeNull();
    expect(within(nav()).queryByRole("region", { name: "Session context" })).toBeNull();
    const accountTab = within(nav()).getByRole("tab", { name: "Account" });
    fireEvent.keyDown(accountTab, { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Usage" }));
    expect(screen.getByRole("tab", { name: "Usage" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(document.activeElement!, { key: "ArrowUp" });
    expect(document.activeElement).toBe(accountTab);
    fireEvent.click(screen.getByRole("tab", { name: "Permissions" }));
    fireEvent.click(link("Open Account › Usage"));
    await waitFor(() => expect(screen.getByRole("tab", { name: "Usage" }).getAttribute("aria-selected")).toBe("true"));
    for (const leave of [() => fireEvent.keyDown(window, { key: "Escape" }), () => fireEvent.click(link("← Sessions"))]) {
      main.scrollTop = 0;
      leave();
      expect(screen.getByRole("region", { name: "Inbox" })).toBe(previousPage);
      expect(main.scrollTop).toBe(180);
      fireEvent.click(link("Open Account › Usage"));
    }
    fireEvent.keyDown(window, { key: "1", metaKey: true });
    await screen.findByRole("region", { name: "Session" });
    const terminal = screen.getByTestId("terminal");
    const sessionsList = within(nav()).getByRole("navigation", { name: "Sessions" });
    sessionsList.scrollTop = 64;
    terminal.scrollTop = 96;
    fireEvent.click(link("Open Account › Usage"));
    expect(within(nav()).getByRole("button", { name: "New session" })).toBeTruthy();
    expect(within(nav()).getByRole("button", { name: "Home" })).toBeTruthy();
    expect(within(nav()).getByRole("button", { name: "Agents" })).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.getByTestId("terminal")).toBe(terminal);
    expect(terminal.scrollTop).toBe(96);
    expect(within(nav()).getByRole("navigation", { name: "Sessions" })).toBe(sessionsList);
    expect(sessionsList.scrollTop).toBe(64);
    fireEvent.keyDown(window, { key: "n", metaKey: true });
    await screen.findByRole("region", { name: "New session" });
    fireEvent.click(within(nav()).getByRole("button", { name: /Paused · 1/ }));
    fireEvent.click(treeSession("sup0"));
    await screen.findByTestId("state-card");
    await waitFor(() => expect(daemon.requests.filter((r) => r.path === "/goal").at(-1)?.query).toEqual({ repo: REPO, root: "sup0" }));
    expect(within(nav()).getByTestId("session-context").textContent).toContain(paused.goal);

    daemon.token = "rotated";
    daemon.drop();
    await screen.findByRole("alert");
    const checked = vi.spyOn(client, "meta");
    fireEvent.click(await screen.findByRole("button", { name: "Check again" }));
    await waitFor(() => expect(checked).toHaveBeenCalled());
    await expect(checked.mock.results.at(-1)!.value).rejects.toMatchObject({ status: 401 });
  });

  it("library sidebar: the foot's button opens the Library with the sidebar as its navigation, ← Sessions and ⌘L go back, and Settings keeps Agents", async () => {
    const plan = { kind: "plan", reason: "plan awaiting approval", agent_id: "sup1", workspace_id: "proj", detail: "1. Keep the plan." };
    const decided = { id: "a".repeat(32), repo_key: "proj", topic: "storage", body: "use sqlite", rationale: "", scope: [], status: "accepted", created_at: 1 };
    const OTHER = "/home/u/other";
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }], sessions: [session], workspaces: [root], inbox: [plan], decisions: [decided] });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    const sessionPage = await screen.findByRole("region", { name: "Session" }, { timeout: 4000 });
    // The foot's button, beside the terminal switch: off, with the dot while a design waits on the person.
    const foot = screen.getByTestId("sidebar-foot-row");
    const button = await within(foot).findByRole("button", { name: /^Library, 1 needs you/ });
    expect(button.nextElementSibling).toBe(screen.getByTestId("terminal-toggle"));
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(within(button).getByTestId("library-dot")).toBeTruthy();

    // Pressed: the Library's page, and the sidebar is its navigation in the sessions' place.
    fireEvent.click(button);
    expect(await screen.findByRole("region", { name: "Library" })).toBeTruthy();
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(within(nav()).queryByRole("navigation", { name: "Sessions" })).toBeNull();
    const rail = within(nav()).getByRole("navigation", { name: "Library" });
    // Only what is real is listed: the plans waiting on the person, and the Sources. No tab strip on the page.
    expect([...rail.querySelectorAll("button")].map((row) => row.textContent?.replace(/^\W+/, "").replace(/\s*\d*$/, ""))).toEqual(["Needs you", "Map", "Imported docs", "Decisions", "Kept visuals"]);
    expect(within(screen.getByRole("main")).queryByRole("tablist")).toBeNull();
    // The repo picked on the page stays picked as the sidebar moves between a stage and a Source.
    const picked = () => screen.getByRole<HTMLSelectElement>("combobox", { name: "Repo" }).value;
    fireEvent.change(screen.getByRole("combobox", { name: "Repo" }), { target: { value: OTHER } });
    fireEvent.click(within(rail).getByRole("button", { name: /^Needs you/ }));
    expect(within(rail).getByRole("button", { name: /^Needs you/ }).getAttribute("aria-current")).toBe("page");
    expect(picked()).toBe(OTHER);
    // A Source's row counts what it holds, opens today's view of it, and is marked.
    await waitFor(() => expect(within(rail).getByRole("button", { name: /^Decisions/ }).textContent).toContain("1"));
    fireEvent.click(within(rail).getByRole("button", { name: /^Decisions/ }));
    expect(await screen.findByTestId("decisions")).toBeTruthy();
    expect(within(rail).getByRole("button", { name: /^Decisions/ }).getAttribute("aria-current")).toBe("page");
    expect(picked()).toBe(OTHER);
    expect(daemon.requests.filter((r) => r.path === "/decisions").at(-1)?.query).toEqual({ repo: OTHER });

    // ← Sessions: the session as it was left, and the sessions list again.
    fireEvent.click(link("← Sessions"));
    expect(screen.getByRole("region", { name: "Session" })).toBe(sessionPage);
    expect(within(nav()).getByRole("navigation", { name: "Sessions" })).toBeTruthy();
    expect(button.getAttribute("aria-pressed")).toBe("false");

    // Settings has no Library heading or Knowledge entry; Agents sits under "brindle", after General.
    fireEvent.click(link("Open Account › Usage"));
    expect(within(nav()).queryByRole("button", { name: "Knowledge" })).toBeNull();
    expect(within(nav()).queryByText("Library")).toBeNull();
    expect(screen.getByRole("tab", { name: "General" }).nextElementSibling).toBe(link("Agents"));
    fireEvent.click(link("Agents"));
    expect(await screen.findByRole("region", { name: "Agents" })).toBeTruthy();
    expect(link("Agents").getAttribute("aria-current")).toBe("page");
    // ⌘L from there opens the Library, and ⌘L again goes back to where the person was.
    fireEvent.keyDown(window, { key: "l", metaKey: true });
    expect(await screen.findByRole("region", { name: "Library" })).toBeTruthy();
    fireEvent.keyDown(window, { key: "l", metaKey: true });
    expect(await screen.findByRole("region", { name: "Agents" })).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.getByRole("region", { name: "Session" })).toBe(sessionPage);
    sync.stop();
  });

  it("library sidebar recent and stages: shared designs with no Inbox card are counted from their own review state, and the rail lists the newest", async () => {
    const plan = { kind: "plan", reason: "plan awaiting approval", agent_id: "sup1", workspace_id: "proj", detail: "1. Keep the plan." };
    const libraryDocs = [
      { doc_id: "a1", title: "Waiting on sam", updated_at: 30, design: { rev: 1, ready: false, reviewers: [{ sub: "sam", required: true }], sign_offs: [] } },
      { doc_id: "a2", title: "All signed", updated_at: 20, design: { rev: 1, ready: true, reviewers: [{ sub: "sam", required: true }], sign_offs: [{ sub: "sam", rev: 1 }] } },
      { doc_id: "a3", title: "Personal note", updated_at: 10 },
      { doc_id: "a4", title: "Asked of me", updated_at: 40, design: { rev: 1, ready: false, reviewers: [{ sub: "me", required: true }], sign_offs: [] } },
    ];
    const asked = { kind: "review_requested", reason: "Review requested", doc: "a4" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], inbox: [plan, asked], libraryDocs });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    await screen.findByRole("region", { name: "Session" }, { timeout: 4000 });
    fireEvent.click(await within(screen.getByTestId("sidebar-foot-row")).findByRole("button", { name: /^Library/ }));
    const rail = await within(nav()).findByRole("navigation", { name: "Library" });
    await waitFor(() => expect(within(rail).getByRole("button", { name: /^In review/ }).textContent).toContain("1"));
    expect(within(rail).getByRole("button", { name: /^Approved/ }).textContent).toContain("1");
    expect(within(rail).getByRole("button", { name: /^Needs you/ }).textContent).toContain("1");     // a4, in the queue; the unrelated plan isn't a Library design
    expect([...rail.querySelectorAll("button")].slice(0, 3).map((row) => row.textContent?.replace(/^\W+/, "").trim())).toEqual(["Asked of me", "Waiting on sam", "All signed"]);
    // Choosing a stage opens the Library's results narrowed to it, and they are the designs its count counted.
    const listed = () => [...screen.getByTestId("docs").querySelectorAll("li[data-doc]")].map((li) => li.getAttribute("data-doc"));
    fireEvent.click(within(rail).getByRole("button", { name: /^In review/ }));
    await waitFor(() => expect(screen.getByRole<HTMLSelectElement>("combobox", { name: "Stage" }).value).toBe("in-review"));
    await waitFor(() => expect(listed()).toEqual(["a1"]));
    fireEvent.click(within(rail).getByRole("button", { name: /^Needs you/ }));
    await waitFor(() => expect(screen.getByRole<HTMLSelectElement>("combobox", { name: "Stage" }).value).toBe("needs-you"));
    await waitFor(() => expect(listed()).toEqual(["a4"]));
    sync.stop();
  });

  it("project settings: opens from a repo's sidebar heading and from Settings' link, and a shell opened there starts in that repo", async () => {
    const OTHER = "/home/u/other";
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }],
      sessions: [session, { ...session, id: "sup2", workspace_id: "other" }],
      workspaces: [root],
    });
    const { client, store, sync } = connectDaemon(daemon);
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "new" }} />);
    // The second repo's heading: its Project settings, and a shell from the foot starts at that repo.
    await screen.findByRole("complementary", { name: "Sidebar" });
    fireEvent.click(await within(nav()).findByRole("button", { name: "Project settings for other" }));
    await screen.findByRole("region", { name: "Project settings" });
    expect(screen.getByRole("tab", { name: "Project" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("combobox", { name: "Repo" })).toHaveProperty("value", OTHER);
    fireEvent.click(screen.getByTestId("terminal-toggle"));
    await waitFor(() => expect(daemon.shells.map((s) => s.cwd)).toEqual([OTHER]));
    // Settings (opened from the profile) has a Project tab on the current session's repo (here the second), with a picker for the others.
    localStorage.setItem(LAST_SESSION_KEY, JSON.stringify({ root: "sup2", repo: OTHER }));
    fireEvent.click(link("Open Account › Usage"));
    fireEvent.click(await screen.findByRole("tab", { name: "Project" }));
    await screen.findByRole("region", { name: "Project settings" });
    expect(screen.getByRole("combobox", { name: "Repo" })).toHaveProperty("value", OTHER);
    fireEvent.change(screen.getByRole("combobox", { name: "Repo" }), { target: { value: REPO } });
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Repo" })).toHaveProperty("value", REPO));
  });

  it("inbox: a plan is approved and a notice marked read right on their cards, at once; Go to session opens the session with nothing over it", async () => {
    const plan = { kind: "plan", reason: "plan awaiting approval", agent_id: "sup1", workspace_id: "proj", detail: "1. Keep the plan.\n2. Ship it." };
    const notice = { kind: "org_notice", reason: "Reviewer profiles now default to reviewer-codex.", detail: "admin", notice_id: "n-18" };
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root, worker], inbox: [plan, notice, question], goal });
    const { client, store, sync } = connectDaemon(daemon);
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
  });

  it("lands on the session kept from last time, else the first live one, else the composer", async () => {
    const repos = [{ repo_root: REPO, name: "proj" }];
    const sessions = { [REPO]: [session, paused] };
    expect(landing(repos, sessions, { root: "sup0", repo: REPO })).toEqual({ screen: "session", root: "sup0", repo: REPO, agent: "sup0" });
    expect(landing(repos, sessions, { root: "gone", repo: REPO })).toEqual({ screen: "session", root: "sup1", repo: REPO, agent: "sup1" });
    expect(landing(repos, { [REPO]: [paused] }, null)).toEqual({ screen: "new" });

    // Nothing running and nothing kept: the calm empty state, with the composer.
    const daemon = new FakeDaemon({ repos, sessions: [paused], workspaces: [root] });
    const { client, store, sync } = connectDaemon(daemon);
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    expect(await screen.findByRole("region", { name: "New session" })).toBeTruthy();
    expect(await screen.findByLabelText("What should brindle work on?")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Session" })).toBeNull();
  });

  it("a session started from the composer on a first launch opens running, not paused (#216)", async () => {
    // A first launch: a project just picked, nothing ever run in it. The engine says the new supervisor
    // exists only with agent.status (it has no event of its own), and nothing else happens on the bus.
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [], workspaces: [{ ...root, agents: [] }] });
    const { client, store, sync } = connectDaemon(daemon);
    renderApp(<App store={store} client={client} refresh={sync.refresh} />);
    expect(await screen.findByRole("region", { name: "New session" })).toBeTruthy();
    fireEvent.change(await screen.findByLabelText("What should brindle work on?"), { target: { value: "Fix the flaky test" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

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
  });

  it.each(["before", "after"])("a session whose status beat its window into the snapshot still opens running, not paused (#216), read %s launch completes", async (readWhen) => {
    // The engine publishes the new chat's status before it records the chat's window: the snapshot that
    // status asks for lists it "exited" with no window, and nothing on the bus says when the window lands.
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [], workspaces: [{ ...root, agents: [] }] });
    daemon.holdLaunch = true;
    const store = createAppStore();
    const client = daemon.client();
    const snapshot = client.snapshot.bind(client);
    let readStarted!: () => void;
    const reading = new Promise<void>((resolve) => { readStarted = resolve; });
    let releaseRead!: () => void;
    const held = new Promise<void>((resolve) => { releaseRead = resolve; });
    vi.spyOn(client, "snapshot").mockImplementation(async (...args) => {
      if (daemon.data.sessions.some((s) => s.id === "new1")) {
        readStarted();
        await held;
      }
      return snapshot(...args);
    });
    const sync = startSync({ client, store, connect: daemon.connect });
    try {
      renderApp(<App store={store} client={client} refresh={sync.refresh} />);
      fireEvent.change(await screen.findByLabelText("What should brindle work on?"), { target: { value: "Fix the flaky test" } });
      await act(async () => { await sync.idle(); });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      await reading;
      // A slow read can miss the early row altogether. Exercise both orders without waiting
      // for that transient row in the store: only the running session after the ack matters.
      await act(async () => {
        if (readWhen === "after") daemon.finishLaunch();
        releaseRead();
        await sync.idle();
        if (readWhen === "before") daemon.finishLaunch();
      });

      expect(await screen.findByRole("region", { name: "Session" })).toBeTruthy();
      await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("new1"));
      const bar = screen.getByTestId("session-bar");
      expect(bar.textContent).not.toContain("paused");
      expect(within(bar).queryByRole("button", { name: "Continue" })).toBeNull();
      expect(within(bar).getByRole("button", { name: "Pause" })).toBeTruthy();
      expect(screen.queryByTestId("state-card")).toBeNull();
      expect(daemon.requests.some((r) => r.path.endsWith("/pause"))).toBe(false);
    } finally { releaseRead(); daemon.finishLaunch(); sync.stop(); }
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
    const { store, sync } = connectDaemon(daemon, client);
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
  });

  it("runs an old engine's upgrade from the banner in the background: waiting, failed with a retry, then updated", async () => {
    const daemon = new FakeDaemon();
    daemon.data.meta ={ ...daemon.data.meta, engine_version: "0.4.1", api_version: "0.9" };
    daemon.deferUpgrade = true;
    daemon.upgradeFails = true;
    const { client, store, sync } = connectDaemon(daemon);
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

    fireEvent.click(link("Open Account › Usage"));
    fireEvent.click(await screen.findByRole("tab", { name: "About" }));
    const engineSection = screen.getByTestId("engine");
    fireEvent.click(within(engineSection).getByRole("button", { name: /Details/ }));
    expect(within(engineSection).getByTestId("connection").textContent).toBe("daemon · connected");
    expect(screen.queryByTestId("engine-health")).toBeNull();
    expect(within(nav()).queryByTestId("connection")).toBeNull();
    fireEvent.click(within(nav()).getByTestId("inbox-link"));
    await screen.findByRole("region", { name: "Inbox" });

    // The engine dies: the stream drops and nothing answers. The reconnect overlay says so over the
    // Inbox, which stays where it was; no raw banner. The app asks for nothing.
    const before = daemon.requests.length;
    daemon.die();
    await waitFor(() => expect(screen.getByTestId("reconnecting")).toBeTruthy(), { timeout: 3000 });
    expect(screen.getByRole("region", { name: "Inbox" })).toBeTruthy();
    expect(screen.queryByTestId("engine-health")).toBeNull();
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
    await waitFor(() => expect(screen.queryByTestId("reconnecting")).toBeNull());
    expect(screen.queryByTestId("engine-banner")).toBeNull();
    await waitFor(() => expect(screen.queryByTestId("inbox-badge")).toBeNull());
    fireEvent.click(link("Open Account › Usage"));
    fireEvent.click(await screen.findByRole("tab", { name: "About" }));
    fireEvent.click(within(screen.getByTestId("engine")).getByRole("button", { name: /Details/ }));
    expect(within(screen.getByTestId("engine")).getByTestId("connection").textContent).toBe("daemon · connected");
    const after = daemon.requests.slice(before).filter((r) => r.method === "GET").map((r) => r.path);
    expect(after).toContain("/snapshot");
    expect(after).toContain("/inbox");
    expect(after).toContain("/meta");
    fireEvent.click(link("Library"));
    expect(await screen.findByRole("region", { name: "Library" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("engine restart: the last screen stays under a calm reconnect overlay, a next step after a long wait, then the normal screen once the new engine has its sessions back", async () => {
    vi.useFakeTimers();
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root] });
    const client = daemon.client();
    const store = createAppStore();
    const sync = startSync({ client, store, connect: daemon.connect, backoff: () => 1000 });
    const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
    let inertBlurs: { mockRestore(): void } | null = null;
    try {
      renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO }} />);
      await advance(1000);
      const terminal = screen.getByTestId("terminal");
      expect(terminal.textContent).toBe("sup1");
      // Keys the pane would hear; and the focus in the app, where the overlay must take it from.
      const typed = vi.fn();
      terminal.addEventListener("keydown", typed);
      const focused = within(nav()).getByTestId("inbox-link");
      focused.focus();
      // Inert as a browser has it (jsdom doesn't): the moment the app goes inert, the focus leaves it for
      // the body, before any effect runs, and nothing inert takes the focus back (React's own restore
      // after its commit included). Where it was must be known from before that.
      const setAttribute = Element.prototype.setAttribute;
      const focus = HTMLElement.prototype.focus;
      const inert = [
        vi.spyOn(Element.prototype, "setAttribute").mockImplementation(function (this: Element, name: string, value: string) {
          setAttribute.call(this, name, value);
          if (name === "inert" && this.contains(document.activeElement)) (document.activeElement as HTMLElement).blur();
        }),
        vi.spyOn(HTMLElement.prototype, "focus").mockImplementation(function (this: HTMLElement, options?: FocusOptions) {
          if (!this.closest("[inert]")) focus.call(this, options);
        }),
      ];
      inertBlurs = { mockRestore: () => inert.forEach((s) => s.mockRestore()) };

      daemon.die();
      await advance(0);
      // A moment's drop shows nothing at all: no overlay yet, and no raw strip.
      expect(screen.queryByTestId("reconnecting")).toBeNull();
      expect(screen.queryByTestId("engine-health")).toBeNull();
      await advance(1000);
      const overlay = screen.getByTestId("reconnecting");
      expect(within(overlay).getByRole("alertdialog")).toBeTruthy();
      expect(within(overlay).queryByRole("button")).toBeNull();
      // Over the last good screen, which stays mounted; nothing raw beside it.
      expect(screen.getByRole("region", { name: "Session" })).toBeTruthy();
      expect(screen.queryByTestId("engine-banner")).toBeNull();
      expect(screen.queryByRole("alert")).toBeNull();
      // Modal: the focus is in it, and the app under it hears no keys: not the pane, not ⌘N, not ⌃`.
      expect(overlay.contains(document.activeElement)).toBe(true);
      fireEvent.keyDown(terminal, { key: "a" });
      fireEvent.keyDown(focused, { key: "n", metaKey: true });
      fireEvent.keyDown(document.activeElement!, { key: "n", metaKey: true });
      fireEvent.keyDown(document.activeElement!, { key: "`", ctrlKey: true });
      await advance(0);
      expect(typed).not.toHaveBeenCalled();
      expect(screen.getByRole("region", { name: "Session" })).toBeTruthy();
      expect(screen.queryByRole("form", { name: "Start a session" })).toBeNull();
      expect(screen.queryByTestId("terminal-panel")).toBeNull();

      // A long wait: a next step, focused, and it asks the engine at once.
      await advance(30000);
      expect(overlay.dataset.phase).toBe("long");
      const again = within(overlay).getByRole("button");
      expect(document.activeElement).toBe(again);
      const asked = daemon.requests.filter((r) => r.path === "/meta").length;
      fireEvent.click(again);
      await advance(0);
      expect(daemon.requests.filter((r) => r.path === "/meta").length).toBeGreaterThan(asked);

      // Back as a new process that finished recovery before reconnect: the gate's fresh
      // rows release the pane even while sync's inbox remains pending.
      let resolveInbox!: (items: []) => void;
      vi.spyOn(client, "inbox").mockReturnValueOnce(new Promise((ok) => { resolveInbox = ok; }));
      daemon.revive();
      await advance(1000);
      expect(screen.queryByTestId("reconnecting")).toBeNull();
      expect(screen.queryByTestId("engine-banner")).toBeNull();
      // The focus goes back where it was.
      expect(document.activeElement).toBe(focused);
      expect(screen.getByTestId("terminal").textContent).toBe("sup1");
      resolveInbox([]);
      await advance(1000);
      expect(paneWaits()).toBe(false);
      expect(screen.getByTestId("terminal").textContent).toBe("sup1");
    } finally { inertBlurs?.mockRestore(); sync.stop(); vi.useRealTimers(); }
  });

  it("first launch with a signed-in CLI offers the permission presets", async () => {
    for (const action of ["skip", "continue", "read-failed"]) {
      localStorage.clear();
      const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }] });
      daemon.data.policy.preset = "secure";
      const original = structuredClone(daemon.data.policy);
      const store = createAppStore();
      const client = daemon.client();
      if (action === "read-failed") vi.spyOn(client, "permissions").mockRejectedValue(new Error("unavailable"));
      const sync = startSync({ client, store, connect: daemon.connect });
      renderApp(<App store={store} client={client} refresh={sync.refresh} />);
      if (action !== "read-failed") {
        await screen.findByRole("radiogroup", { name: "Permission preset" });
        expect(screen.getByTestId("onb-preset-secure").getAttribute("aria-checked")).toBe("true");
        expect(screen.queryByRole("complementary", { name: "Sidebar" })).toBeNull();
        fireEvent.click(screen.getByTestId("onb-preset-standard"));
        fireEvent.click(screen.getByRole("button", { name: action === "skip" ? "Skip for now" : "Continue with Standard" }));
      }
      expect(await screen.findByRole("form", { name: "Start a session" })).toBeTruthy();
      if (action === "continue") expect(daemon.data.policy.preset).toBe("standard");
      else {
        expect(daemon.data.policy).toEqual(original);
      }
      expect(localStorage.getItem(ONBOARDED_KEY)).toBe("1");
      sync.stop();
      cleanup();
    }
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
    // The sign-in gate asks the engine first, without the stream either: it says what's wrong, with Retry,
    // rather than opening an app it can't know is signed in.
    await waitFor(() => expect(screen.getByTestId("signin-gate-failed").textContent).toContain("missing or wrong token"));
    // Back with the right token: the app, whose check didn't wait on the stream either.
    daemon.token = TOKEN;
    fireEvent.click(within(screen.getByTestId("signin-gate-failed")).getByRole("button", { name: "Retry" }));
    await screen.findByRole("complementary", { name: "Sidebar" });
    expect(store.getState().connection).not.toBe("open");
    expect(screen.queryByTestId("engine-banner")).toBeNull();
    sync.stop();
    cleanup();

    // The engine answered, then its event stream drops for a moment: nothing flashes up, raw or overlaid.
    vi.useFakeTimers();
    const live = new FakeDaemon();
    const store2 = createAppStore();
    const sync2 = startSync({ client: live.client(), store: store2, connect: live.connect, backoff: () => 5 });
    try {
      renderApp(<App store={store2} client={live.client()} refresh={sync2.refresh} />);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(store2.getState().connection).toBe("open");
      expect(screen.getByRole("complementary", { name: "Sidebar" })).toBeTruthy();
      expect(screen.queryByTestId("engine-banner")).toBeNull();
      // Observe the closed state before allowing the scheduled reconnect to run.
      await act(async () => {
        live.drop();
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(store2.getState().connection).toBe("closed");
      expect(screen.queryByTestId("engine-banner")).toBeNull();
      expect(screen.queryByTestId("reconnecting")).toBeNull();
      await act(async () => { await vi.advanceTimersByTimeAsync(5); });
      expect(store2.getState().connection).toBe("open");
      expect(screen.queryByTestId("engine-banner")).toBeNull();
    } finally {
      sync2.stop();
      cleanup();
      vi.useRealTimers();
    }
  });
});

describe("closing a session", () => {
  beforeEach(() => localStorage.setItem(ONBOARDED_KEY, "1"));
  /* A second live session in the same repo, on its own worktree. */
  const other: SessionDTO = { ...session, id: "sup2", workspace_id: "proj/other", goal: "Fix the cache" };
  const otherWs = { ...root, id: "proj/other", name: "other", branch: "other", path: `${REPO}/.brindle/worktrees/other`, agents: [{ ...root.agents![0], id: "sup2", window: windowOf("sup2") }] };
  const done: GoalDTO = { ...goal, state: "done", goal_phase: "done" };
  const posts = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "POST").map((r) => r.path);
  const sessionRow = (id: string) => within(nav()).queryByTestId(`tree-session-${id}`);

  it("Done → Close session: one click, the session leaves the sidebar at once and the view moves to the next session", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, other], workspaces: [root, worker, otherWs], goal: done });
    const { client, store, sync } = connectDaemon(daemon);
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO, agent: "sup1" }} />);
    await waitFor(() => expect(screen.getByTestId("terminal").textContent).toBe("sup1"));
    // Finished: the count line is green, with no phase label; Close session sits under the milestone list.
    const close = await within(nav()).findByRole("button", { name: "Close session" });
    const context = within(nav()).getByTestId("session-context");
    expect(within(context).getByTestId("goal-line").className).toContain("text-pass");
    expect(within(context).queryByTestId("goal-phase")).toBeNull();
    expect(within(context).getByRole("list", { name: "Milestones" }).compareDocumentPosition(close) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
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
    const { client, store, sync } = connectDaemon(daemon);
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup0", repo: REPO, agent: "sup0" }} />);
    await waitFor(() => expect(nav().querySelector('[data-tree-session="sup0"]')).toBeTruthy());
    await waitFor(() => expect(store.getState().synced).toBe(true));
    fireEvent.click(within(screen.getByTestId("session-bar")).getByRole("button", { name: "More actions" }));
    expect(within(screen.getByRole("menu")).getByTestId("close-session").textContent).toBe(label);
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
    const { store, sync } = connectDaemon(daemon, client);
    renderApp(<App store={store} client={client} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO, agent: "sup1" }} />);
    // The app shows once its first-launch gate has asked the engine.
    fireEvent.click(await within(await screen.findByRole("complementary", { name: "Sidebar" })).findByRole("button", { name: "Close session" }));
    expect(sessionRow("sup1")).toBeNull();
    await waitFor(() => expect(within(nav()).getByTestId("close-error").textContent).toContain("Couldn't close sup1"));
    expect(sessionRow("sup1")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

/* The sign-in gate: the app needs a brindle account, before onboarding and before the app. */
describe("the sign-in gate", () => {
  const SIGNED_OUT = { signed_in: false };
  const start = (daemon: FakeDaemon, fetch?: typeof globalThis.fetch) => {
    const store = createAppStore();
    const client = fetch ? new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch }) : daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    const openUrl = vi.fn(() => Promise.resolve());
    renderApp(<App store={store} client={client} refresh={sync.refresh} openUrl={openUrl} />);
    return { sync, openUrl };
  };
  const gate = () => screen.findByTestId("signin-gate");
  const sidebar = () => screen.queryByRole("complementary", { name: "Sidebar" });
  const providersAsked = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.path === "/providers/installed").length;
  /* Nothing but brindle's sign-in: no sidebar, no onboarding step, no way past it. */
  const onlySignIn = () => {
    expect(screen.getByTestId("onboarding").getAttribute("data-step")).toBe("signin");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Sign in to brindle");
    expect(sidebar()).toBeNull();
    expect(screen.queryByRole("form", { name: "Start a session" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Skip for now" })).toBeNull();
    expect(screen.getByTestId("signin-free").textContent).toContain("free account");
  };

  it("signed out: only the sign-in screen, and the providers aren't asked about until it's done", async () => {
    const daemon = new FakeDaemon({ account: SIGNED_OUT, providers: freshProviders() });
    start(daemon);
    await gate();
    onlySignIn();
    await flush();
    expect(providersAsked(daemon)).toBe(0);
  });

  it("app opened from an agent says how to reopen it", async () => {
    vi.useFakeTimers();
    const daemon = new FakeDaemon();
    const requests: string[] = [];
    const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: async (input) => {
      requests.push(String(input));
      return new Response(JSON.stringify({ error: { code: "agent_caller", message: "refused: the caller runs under brindle agent w1" } }), { status: 403, headers: { "Content-Type": "application/json" } });
    } });
    const { store, sync } = connectDaemon(daemon, client);
    renderApp(<App store={store} client={client} refresh={sync.refresh} onAgentRefused={sync.stop} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(screen.getByRole("alert").textContent).toContain("brindle was opened from an agent's terminal. Open it from the Dock");
    expect(screen.queryByTestId("signin-gate")).toBeNull();
    expect(sidebar()).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(store.getState().connection).toBe("closed");
    const count = requests.length;
    daemon.drop();
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(requests).toHaveLength(count);
    expect(requests.some((path) => path.endsWith("/providers/installed"))).toBe(false);
  });

  it("onboarded on this Mac with a CLI signed in, but signed out of brindle: still the gate, then the app once signed in", async () => {
    localStorage.setItem(ONBOARDED_KEY, "1");
    const daemon = new FakeDaemon({ account: SIGNED_OUT, repos: [{ repo_root: REPO, name: "proj" }], providers: freshProviders({ claude: { path: "/home/u/.local/bin/claude", version: "2.1.47", signed_in: true }, opencode: { path: "/usr/local/bin/opencode", version: "0.9.0" } }) });
    daemon.deferLogin = true;
    const { openUrl } = start(daemon);
    await gate();
    onlySignIn();
    // Not the first launch: no step count.
    expect(screen.queryByText("Step 1 of 3")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    await waitFor(() => expect(openUrl).toHaveBeenCalledWith(daemon.data.loginUrl));
    onlySignIn();
    daemon.completeLogin();
    expect(await screen.findByRole("form", { name: "Start a session" })).toBeTruthy();
    expect(sidebar()).toBeTruthy();
    expect(screen.queryByTestId("signin-gate")).toBeNull();
  });

  it("signed in: straight to the app when onboarded, to onboarding when not", async () => {
    localStorage.setItem(ONBOARDED_KEY, "1");
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }] });
    const first = start(daemon);
    expect(await screen.findByRole("form", { name: "Start a session" })).toBeTruthy();
    expect(screen.queryByTestId("signin-gate")).toBeNull();
    first.sync.stop();
    cleanup();

    localStorage.clear();
    const fresh = new FakeDaemon({ providers: freshProviders() });
    const second = start(fresh);
    await waitFor(() => expect(screen.getByTestId("onboarding").getAttribute("data-step")).toBe("connect"), { timeout: 3000 });
    expect(screen.queryByTestId("signin-gate")).toBeNull();
    expect(sidebar()).toBeNull();
    second.sync.stop();
  });

  it("an account read that fails shows the sign-in screen with Retry, never the app; Retry reads it again", async () => {
    localStorage.setItem(ONBOARDED_KEY, "1");
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }] });
    let failing = true;
    start(daemon, (input, init) =>
      failing && String(input).endsWith("/v1/account")
        ? Promise.resolve(new Response(JSON.stringify({ error: { code: "not_found", message: "no such route" } }), { status: 404, headers: { "Content-Type": "application/json" } }))
        : daemon.fetch(input, init),
    );
    const failed = await screen.findByTestId("signin-gate-failed");
    expect(failed.textContent).toContain("Couldn't check your brindle account");
    onlySignIn();
    await flush();
    expect(sidebar()).toBeNull();
    failing = false;
    fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("form", { name: "Start a session" })).toBeTruthy();
    expect(screen.queryByTestId("signin-gate")).toBeNull();
  });

  it("a first launch: sign in, then New session with its demo offer, as before", async () => {
    // The CLI used before this app: a repo registered already, Claude Code signed in; brindle not yet.
    const daemon = new FakeDaemon({ account: SIGNED_OUT, repos: [{ repo_root: REPO, name: "proj" }] });
    start(daemon);
    await gate();
    expect(screen.getByText("Step 1 of 3")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Practice run" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    expect(await screen.findByRole("radiogroup", { name: "Permission preset" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(await screen.findByRole("form", { name: "Start a session" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Practice run" })).toBeTruthy();
    expect(localStorage.getItem(ONBOARDED_KEY)).toBe("1");
    expect(daemon.demos).toEqual([]);
  });
});
