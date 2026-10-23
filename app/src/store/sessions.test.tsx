import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DaemonClient } from "../api/client";
import { BASE_URL, FakeDaemon, TOKEN } from "../test/fakeDaemon";
import { REPO } from "../test/fixtures";
import { SessionsProvider, useSessions } from "./sessions";
import { createAppStore } from "./store";
import { startSync } from "./sync";
import { StoreContext } from "./useStore";

afterEach(cleanup);

const OTHER = "/home/me/other";

describe("SessionsProvider", () => {
  it("loads the sessions once at a time, however many events arrive while a load is out", async () => {
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }, { repo_root: OTHER, name: "other" }],
      sessions: [{ id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship it" }],
    });
    // A daemon that takes a while to answer, as a busy one does: every request through the
    // app's shell is a connection open until it answers (src-tauri/src/daemon.rs).
    let inFlight = 0;
    let peak = 0;
    const slow: typeof fetch = async (input, init) => {
      inFlight++;
      peak = Math.max(peak, inFlight);
      try {
        await new Promise((resolve) => setTimeout(resolve, 20));
        return await daemon.fetch(input, init);
      } finally {
        inFlight--;
      }
    };
    const store = createAppStore();
    const sync = startSync({ client: daemon.client(), store, connect: daemon.connect });
    const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: slow });
    let feed: ReturnType<typeof useSessions> | null = null;
    function Probe() {
      feed = useSessions();
      return null;
    }
    render(
      <StoreContext.Provider value={store}>
        <SessionsProvider client={client}>
          <Probe />
        </SessionsProvider>
      </StoreContext.Provider>,
    );
    await waitFor(() => expect(daemon.open.length).toBe(1));
    await waitFor(() => expect(feed?.repos?.length).toBe(2));
    await waitFor(() => expect(inFlight).toBe(0));
    daemon.requests.length = 0;
    peak = 0;

    // The stream replays a backlog (launch, a reconnect): an event per tick, each a new lastEventId.
    for (let i = 0; i < 100; i++) {
      await act(async () => {
        daemon.publish("agent.status", { agent_id: "sup1", payload: { status: "processing" } });
        await Promise.resolve();
      });
    }
    await waitFor(() => expect(inFlight).toBe(0), { timeout: 5000 });
    // A load is the repos and a sessions list per repo: one out at a time, never one per event.
    expect(peak).toBeLessThanOrEqual(2);
    const loads = daemon.requests.filter((r) => r.path === "/repos").length;
    expect(loads).toBeGreaterThan(0);
    expect(loads).toBeLessThan(20);
    // The last event's load still happens: what the feed shows is current.
    const last = daemon.requests.at(-1);
    expect(last?.path).toBe("/sessions");
    sync.stop();
  });
});
