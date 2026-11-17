import { describe, expect, it, onTestFinished, vi } from "vitest";
import { createAppStore } from "./store";
import { startSync } from "./sync";
import { BASE_URL, FakeDaemon, flush } from "../test/fakeDaemon";
import { question, waiting, REPO, root, worker } from "../test/fixtures";
import { DaemonClient } from "../api/client";

function setup() {
  const daemon = new FakeDaemon({ workspaces: [root, worker], inbox: [question] });
  const store = createAppStore();
  const client = new DaemonClient({ baseUrl: BASE_URL, token: daemon.token, fetch: (input, init) => daemon.fetch(input, init) });
  const sync = startSync({ client, store, repo: REPO, connect: daemon.connect, backoff: () => 0 });
  onTestFinished(() => sync.stop());
  const snapshots = () => daemon.requests.filter((r) => r.path === "/snapshot").length;
  return { daemon, client, store, sync, snapshots };
}

describe("startSync", () => {
  it.each(["plan offer", "visual answer", "visual dismiss", "visual read", "plan verdict"])("does not restore a settled %s from an older refresh", async (action) => {
    const { daemon, client, store, sync } = setup();
    await flush();
    await sync.idle();
    vi.spyOn(daemon, "fetch").mockResolvedValue(new Response("{}"));
    let finish!: (items: typeof daemon.data.inbox) => void;
    vi.spyOn(client, "snapshot").mockResolvedValue([root]);
    vi.spyOn(client, "inbox").mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; })).mockResolvedValue([]);
    const read = sync.refresh();
    if (action === "plan offer") await client.resolvePlanOffer("sup1", "o1", false);
    else if (action === "visual answer") await client.answerVisual("v1", 1, "yes", "");
    else if (action === "visual dismiss") await client.dismissVisual("v1", 1);
    else if (action === "plan verdict") await client.decidePlan("v1", { rev: 1, approved: true, picks: {}, comments: [], note: "" });
    else await client.markVisualRead("v1", 1);
    finish([question]);
    await read;
    expect(store.getState().inbox).toEqual([]);
  });

  it("answered visual leaves the inbox through a repo-scoped event without an Inbox screen mounted", async () => {
    const { daemon, store, sync } = setup();
    await flush();
    await sync.idle();
    expect(store.getState().inbox).toEqual([question]);
    daemon.data.inbox = [];
    // Engine publishers carry repo context (covered by test_visual_offers.py).
    daemon.publish("visual.offer_resolved", { repo_root: REPO, root_id: "sup1", payload: { offer: "o_1" } });
    await flush();
    await sync.idle();
    expect(store.getState().inbox).toEqual([]);
  });

  it.each(["agent.closed", "rewound"])("discards a snapshot started before %s even if the queued refresh fails", async (kind) => {
    const { daemon, client, store, sync } = setup();
    await flush();
    await sync.idle();
    let finish!: (rows: typeof daemon.data.workspaces) => void;
    vi.spyOn(client, "snapshot")
      .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
      .mockRejectedValueOnce(new Error("offline"));
    vi.spyOn(client, "inbox").mockResolvedValueOnce([waiting]);
    const older = sync.refresh();
    daemon.publish(kind === "rewound" ? "agent.status" : kind, { repo_root: REPO, agent_id: "w1", payload: { status: "rewound" } });
    await flush();
    const workers = () => store.getState().workspaces.flatMap((ws) => ws.agents ?? []);
    expect(workers().some((a) => a.id === "w1")).toBe(false);
    finish([root, worker]);
    await older;
    await sync.idle();
    expect(store.getState().error).toBe("offline");
    expect(workers().some((a) => a.id === "w1")).toBe(false);
    expect(store.getState().inbox.some((i) => i.agent_id === "w1")).toBe(false);
    daemon.data.inbox = [waiting];
    await sync.refresh(); // a response requested after the lifecycle event may resume the worker
    expect(workers().some((a) => a.id === "w1")).toBe(true);
    expect(store.getState().inbox).toEqual([waiting]);
    sync.stop();
  });

  it("loads the snapshot and inbox once the stream opens, for its repo only", async () => {
    const { daemon, store, sync } = setup();
    await flush();
    await sync.idle();
    expect(store.getState()).toMatchObject({ connection: "open", workspaces: [root, worker], inbox: [question] });
    expect(daemon.sockets[0].repo).toBe(REPO);
    expect(daemon.requests.map((r) => r.query.repo)).toEqual([REPO, REPO]);
  });

  it("patches what it can and refetches for what it can't", async () => {
    const { daemon, store, sync, snapshots } = setup();
    await flush();
    await sync.idle();
    store.dispatch({ type: "snapshot", workspaces: [root, worker], inbox: [waiting] });
    daemon.data.inbox = [];
    daemon.publish("agent.status", { repo_root: REPO, agent_id: "w1", payload: { status: "processing" } });
    await flush();
    await sync.idle();
    expect(store.getState().inbox).toEqual([]);
    expect(snapshots()).toBe(2);

    daemon.data.inbox = [];
    daemon.publish("agent.reported", { repo_root: REPO, agent_id: "w1" });
    await flush();
    await sync.idle();
    expect(snapshots()).toBe(3);
    expect(store.getState()).toMatchObject({ stale: false, inbox: [], lastEventId: 2 });
  });

  it("applies successful reads during stream traffic and runs each queued refresh", async () => {
    const { daemon, client, store, sync } = setup();
    await flush();
    await sync.idle();
    const reads: ((items: typeof daemon.data.inbox) => void)[] = [];
    vi.spyOn(client, "inbox").mockImplementation(() => new Promise((resolve) => reads.push(resolve)));
    const first = sync.refresh();
    for (let i = 0; i < 3; i++) {
      daemon.publish("message.queued", { repo_root: REPO, agent_id: "sup1" });
      await flush();
      daemon.sockets[0].deliver({ type: "liveness", agents: { sup1: true } });
      await flush();
      reads[i]([waiting]); await flush();
      expect(store.getState()).toMatchObject({ snapshotVersion: i + 2, inbox: [waiting] });
    }
    reads[3]([]);
    await first;
    await sync.idle();
    expect(store.getState().inbox).toEqual([]);
    sync.stop();
  });

  it("after a drop, resumes from the last event and takes a fresh snapshot", async () => {
    const { daemon, store, sync, snapshots } = setup();
    await flush();
    await sync.idle();
    daemon.publish("agent.status", { repo_root: REPO, agent_id: "w1", payload: { status: "idle" } });
    await flush();
    daemon.drop();
    await flush();
    await sync.idle();
    expect(daemon.sockets.map((s) => s.since)).toEqual([0, 1]);
    expect(snapshots()).toBe(3);
    expect(store.getState().connection).toBe("open");
    sync.stop();
    expect(store.getState().connection).toBe("closed");
  });
});
