import { act, cleanup, render } from "@testing-library/react";
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
    // Hold captured responses until released: every request through the app's shell
    // is a connection open until it answers (src-tauri/src/daemon.rs).
    let hold = false;
    const pending: (() => void)[] = [];
    let inFlight = 0;
    let peak = 0;
    const gated: typeof fetch = async (input, init) => {
      inFlight++;
      peak = Math.max(peak, inFlight);
      try {
        const response = await daemon.fetch(input, init);
        if (hold) await new Promise<void>((resolve) => pending.push(resolve));
        return response;
      } finally {
        inFlight--;
      }
    };
    const answerPending = () => act(async () => {
      for (const resolve of pending.splice(0)) resolve();
    });
    const paths = () => daemon.requests.filter((r) => r.path === "/repos" || r.path === "/sessions").map((r) => r.path);
    const store = createAppStore();
    const sync = startSync({ client: daemon.client(), store, connect: daemon.connect });
    const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: gated });
    let feed!: ReturnType<typeof useSessions>;
    function Probe() {
      feed = useSessions();
      return null;
    }
    await act(async () => {
      render(
        <StoreContext.Provider value={store}>
          <SessionsProvider client={client}>
            <Probe />
          </SessionsProvider>
        </StoreContext.Provider>,
      );
      await sync.idle();
    });
    expect(daemon.open.length).toBe(1);
    expect(feed.repos?.length).toBe(2);
    expect(inFlight).toBe(0);
    daemon.requests.length = 0;
    peak = 0;
    hold = true;

    // Each act delivers a separate bus event and runs the provider's effect.
    // Further events must queue one follow-up while /repos is still pending.
    for (const status of ["processing", "waiting"]) {
      await act(async () => {
        const event = daemon.publish("agent.status", { agent_id: "sup1", payload: { status } });
        await Promise.resolve();
        expect(store.getState().lastEventId).toBe(event.id);
      });
      expect(inFlight).toBe(1);
      expect(paths()).toEqual(["/repos"]);
    }
    await answerPending();
    expect(inFlight).toBe(2);
    expect(pending.length).toBe(2);

    // These responses already contain idle. Changes announced while both repo
    // lists are pending can only appear through the follow-up refresh.
    for (const status of ["processing", "paused"]) {
      daemon.data.sessions[0] = { ...daemon.data.sessions[0], status };
      await act(async () => {
        const event = daemon.publish("agent.status", { agent_id: "sup1", payload: { status } });
        await Promise.resolve();
        expect(store.getState().lastEventId).toBe(event.id);
      });
      expect(inFlight).toBe(2);
      expect(paths()).toEqual(["/repos", "/sessions", "/sessions"]);
    }
    await answerPending();
    expect(feed.find("sup1")?.status).toBe("idle");
    expect(inFlight).toBe(1);
    await answerPending();
    expect(inFlight).toBe(2);
    await answerPending();

    // Exactly one follow-up load, with at most the two repo lists in flight.
    expect(peak).toBe(2);
    expect(inFlight).toBe(0);
    expect(pending.length).toBe(0);
    expect(paths()).toEqual([
      "/repos", "/sessions", "/sessions", "/repos", "/sessions", "/sessions",
    ]);
    expect(feed.find("sup1")?.status).toBe("paused");
    expect(feed.sessions[OTHER]).toEqual([]);
    sync.stop();
  });
});
