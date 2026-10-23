import { describe, expect, it } from "vitest";
import type { BusEvent, StreamMessage } from "../api/events";
import { RECENT_EVENTS, createAppStore, initialState, reduce, type AppState } from "./store";
import { question, root, waiting, worker } from "../test/fixtures";

const loaded: AppState = { ...initialState, workspaces: [root, worker], inbox: [question, waiting], alive: { sup1: true, w1: true } };

const event = (kind: string, fields: Partial<BusEvent> = {}): BusEvent => ({
  type: "event",
  id: 7,
  ts: 200,
  kind,
  repo_root: "/home/u/proj",
  root_id: "sup1",
  agent_id: null,
  workspace_id: null,
  task_id: null,
  payload: {},
  ...fields,
});

const agentOf = (s: AppState, id: string) => s.workspaces.flatMap((ws) => ws.agents ?? []).find((a) => a.id === id);

describe("reduce: stream messages", () => {
  it.each<[string, StreamMessage, (s: AppState) => void]>([
    [
      "agent.status patches the agent in place",
      event("agent.status", { agent_id: "w1", payload: { status: "waiting" } }),
      (s) => {
        expect(agentOf(s, "w1")).toMatchObject({ status: "waiting", status_since: 200 });
        expect(s.stale).toBe(false);
      },
    ],
    [
      // A session just started is announced only so (#216): left out, it would read as not running.
      "agent.status of an agent the snapshot lacks marks the store stale",
      event("agent.status", { agent_id: "new1", payload: { status: "processing" } }),
      (s) => expect(s.stale).toBe(true),
    ],
    [
      "workspace.removed drops the workspace and its inbox items",
      event("workspace.removed", { workspace_id: "proj/feat-ls" }),
      (s) => {
        expect(s.workspaces.map((ws) => ws.id)).toEqual(["proj"]);
        expect(s.inbox).toEqual([question]);
      },
    ],
    [
      "an event it can't patch marks the store stale",
      event("agent.reported", { agent_id: "w1" }),
      (s) => expect(s.stale).toBe(true),
    ],
    ["resnapshot marks the store stale", { type: "resnapshot", reason: "events_lost" }, (s) => expect(s.stale).toBe(true)],
    [
      "a first liveness replaces the map",
      { type: "liveness", agents: { w1: false } },
      (s) => expect(s.alive).toEqual({ w1: false }),
    ],
    [
      "a later liveness merges changes and forgets the gone",
      { type: "liveness", agents: { w2: true }, gone: ["sup1"] },
      (s) => expect(s.alive).toEqual({ w1: true, w2: true }),
    ],
    [
      "git updates the workspace's counts",
      {
        type: "git",
        workspace_id: "proj/feat-ls",
        status: { branch: "feat/ls", base: "main", ahead: 3, behind: 1, dirty_files: ["a", "b"], unpushed: null },
      },
      (s) => expect(s.workspaces[1]).toMatchObject({ ahead: 3, behind: 1, dirty: 2 }),
    ],
    [
      "status sets the org bar and notices",
      { type: "status", bar: ["acme", "Pro"], notices: [{ id: "n1", text: "maintenance" }] },
      (s) => expect(s.org).toEqual({ bar: ["acme", "Pro"], notices: [{ id: "n1", text: "maintenance" }] }),
    ],
  ])("%s", (_case, message, check) => {
    check(reduce(loaded, { type: "stream", message }));
  });

  it("tracks the newest event id and keeps only the recent events", () => {
    let s = loaded;
    for (let id = 1; id <= RECENT_EVENTS + 5; id++) s = reduce(s, { type: "stream", message: event("agent.result", { id }) });
    s = reduce(s, { type: "stream", message: event("agent.result", { id: 3 }) }); // a replayed duplicate
    expect(s.lastEventId).toBe(RECENT_EVENTS + 5);
    expect(s.recent).toHaveLength(RECENT_EVENTS);
  });

  it("a snapshot replaces workspaces and inbox and clears stale", () => {
    const s = reduce({ ...loaded, stale: true }, { type: "snapshot", workspaces: [root], inbox: [] });
    expect(s).toMatchObject({ workspaces: [root], inbox: [], stale: false });
  });
});

describe("createAppStore", () => {
  it("notifies subscribers on a change and stops after unsubscribe", () => {
    const store = createAppStore();
    let calls = 0;
    const off = store.subscribe(() => calls++);
    store.dispatch({ type: "connection", state: "open" });
    off();
    store.dispatch({ type: "connection", state: "closed" });
    expect(calls).toBe(1);
    expect(store.getState().connection).toBe("closed");
  });
});
