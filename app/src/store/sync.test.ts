import { describe, expect, it, vi } from "vitest";
import { createAppStore } from "./store";
import { startSync } from "./sync";
import { FakeDaemon, flush } from "../test/fakeDaemon";
import { question, waiting, REPO, root, worker } from "../test/fixtures";

function setup() {
  const daemon = new FakeDaemon({ workspaces: [root, worker], inbox: [question] });
  const store = createAppStore();
  const client = daemon.client();
  const sync = startSync({ client, store, repo: REPO, connect: daemon.connect, backoff: () => 0 });
  const snapshots = () => daemon.requests.filter((r) => r.path === "/snapshot").length;
  return { daemon, client, store, sync, snapshots };
}

describe("startSync", () => {
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

  it("runs a refresh requested during a fetch after that fetch ends", async () => {
    const { daemon, client, store, sync } = setup();
    await flush();
    await sync.idle();
    let finish!: (items: typeof daemon.data.inbox) => void;
    vi.spyOn(client, "inbox").mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const first = sync.refresh();
    daemon.data.inbox = [];
    void sync.refresh();
    finish([question]);
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
