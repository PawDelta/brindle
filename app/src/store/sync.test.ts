import { describe, expect, it } from "vitest";
import { createAppStore } from "./store";
import { startSync } from "./sync";
import { FakeDaemon, flush } from "../test/fakeDaemon";
import { question, REPO, root, worker } from "../test/fixtures";

function setup() {
  const daemon = new FakeDaemon({ workspaces: [root, worker], inbox: [question] });
  const store = createAppStore();
  const sync = startSync({ client: daemon.client(), store, repo: REPO, connect: daemon.connect, backoff: () => 0 });
  const snapshots = () => daemon.requests.filter((r) => r.path === "/snapshot").length;
  return { daemon, store, sync, snapshots };
}

describe("startSync", () => {
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
    daemon.publish("agent.status", { repo_root: REPO, agent_id: "w1", payload: { status: "waiting" } });
    await flush();
    expect(snapshots()).toBe(1);
    expect(store.getState().workspaces[1].agents?.[0].status).toBe("waiting");

    daemon.data.inbox = [];
    daemon.publish("agent.reported", { repo_root: REPO, agent_id: "w1" });
    await flush();
    await sync.idle();
    expect(snapshots()).toBe(2);
    expect(store.getState()).toMatchObject({ stale: false, inbox: [], lastEventId: 2 });
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
    expect(snapshots()).toBe(2);
    expect(store.getState().connection).toBe("open");
    sync.stop();
    expect(store.getState().connection).toBe("closed");
  });
});
