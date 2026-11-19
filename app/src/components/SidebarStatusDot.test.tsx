import { connectDaemon, inTestContext } from "../test/harness";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DaemonClient } from "../api/client";
import type { AppUpdates } from "../api/tauri";
import type { GoalDTO, SessionDTO } from "../api/schema";
import { SessionsProvider } from "../store/sessions";
import { UsageProvider } from "../store/usage";
import { BASE_URL, FakeDaemon, TOKEN } from "../test/fakeDaemon";
import { REPO, root } from "../test/fixtures";
import { AppUpdateProvider } from "./AppUpdateBanner";
import { Sidebar } from "./Sidebar";

beforeEach(() => localStorage.clear());
afterEach(cleanup);

// The sessions feed is fixed: only the live snapshot (the supervisor's agent) moves.
const live: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };
const staged: GoalDTO = { root_id: "sup1", enabled: true, goal: "g", state: "running", stage: "working", progress: 0, milestones: [] };

async function mountDot(session: SessionDTO, goal: GoalDTO | null = null) {
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], goal });
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch: daemon.fetch });
  const { store } = connectDaemon(daemon, client);
  render(
    inTestContext(
      <AppUpdateProvider updates={{} as AppUpdates}>
        <SessionsProvider client={client}>
          <UsageProvider client={client}>
            <Sidebar route={{ screen: "new" }} navigate={vi.fn()} refresh={async () => {}} />
          </UsageProvider>
        </SessionsProvider>
      </AppUpdateProvider>,
      { client, store },
      { daemon: true, providers: true },
    ),
  );
  const dot = await screen.findByRole("button", { name: "Hide sup1's agents" });
  const withStatus = (status: string) => act(() => store.dispatch({ type: "snapshot", workspaces: [{ ...root, agents: [{ ...root.agents![0], status }] }] }));
  return { dot, withStatus };
}

it("a supervisor row's dot follows its agent's live status", async () => {
  const { dot, withStatus } = await mountDot(live);
  withStatus("idle");
  expect(dot.dataset.state).toBe("idle");
  withStatus("processing");
  expect(dot.dataset.state).toBe("working");
  withStatus("waiting");
  expect(dot.dataset.state).toBe("waiting");
  withStatus("paused");
  expect(dot.dataset.state).toBe("paused");
});

it("live idle beats a stale processing session feed", async () => {
  const { dot, withStatus } = await mountDot({ ...live, status: "processing" });
  withStatus("processing");
  expect(dot.dataset.state).toBe("working");
  withStatus("idle");
  expect(dot.dataset.state).toBe("idle");
});

it("live paused beats an active goal stage", async () => {
  const { dot, withStatus } = await mountDot(live, staged);
  withStatus("processing");
  await waitFor(() => expect(dot.dataset.state).toBe("working"));
  withStatus("paused");
  expect(dot.dataset.state).toBe("paused");
});
