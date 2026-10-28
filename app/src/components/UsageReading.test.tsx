import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { DaemonContext } from "../api/context";
import type { SessionDTO } from "../api/schema";
import type { Route } from "../routes";
import { SessionsProvider } from "../store/sessions";
import { createAppStore } from "../store/store";
import { startSync } from "../store/sync";
import { UsageProvider } from "../store/usage";
import { StoreContext } from "../store/useStore";
import { FakeDaemon } from "../test/fakeDaemon";
import { REPO, root } from "../test/fixtures";
import { PtyViewsProvider } from "../api/ptyView";
import { standInViews } from "../test/panes";
import { DESIGN_NOW, usageScenario, type UsageScenario } from "../test/usage";
import { Sidebar } from "./Sidebar";

vi.mock("./PtyTerminal", () => import("../test/panes"));

const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };

beforeEach(() => {
  localStorage.clear();
  // The design's clock, so the readings say what it says; timers stay real.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(DESIGN_NOW);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function app(scenario: UsageScenario) {
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], usage: usageScenario(scenario) });
  const store = createAppStore();
  const client = daemon.client();
  const sync = startSync({ client, store, connect: daemon.connect });
  render(
    <PtyViewsProvider value={standInViews}>
      <App store={store} client={client} refresh={sync.refresh} />
    </PtyViewsProvider>,
  );
  return { daemon, stop: () => sync.stop() };
}

const statusLine = () => screen.getByTestId("status-line");
const usageAsked = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "GET" && r.path === "/usage").length;

describe("the sidebar's reading near a usage limit", () => {
  it("says nothing about usage in the normal state, keeping the links on one line", async () => {
    const { daemon, stop } = app("normal");
    await waitFor(() => expect(usageAsked(daemon)).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getByTestId("pages")).toBeTruthy());
    expect(screen.queryByTestId("usage-reading")).toBeNull();
    expect(statusLine().textContent).not.toMatch(/%|limit/);
    expect(within(statusLine()).queryByTestId("connection")).toBeNull();
    // The footer's links never wrap, whatever the status line carries.
    const pages = screen.getByTestId("pages");
    expect(pages.className).toContain("flex-nowrap");
    expect(pages.className).toContain("whitespace-nowrap");
    expect(within(pages).getAllByRole("button").map((b) => b.textContent)).toEqual(["Knowledge", "Agents", "Settings", "Account"]);
    stop();
  });

  it("near a limit shows the worst provider in a few warm words, and opens Account › Usage", async () => {
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    const { stop } = app("near");
    const reading = await screen.findByTestId("usage-reading");
    expect(reading.textContent).toBe("Claude 88% · 5:10pm");
    expect(reading.className).toContain("text-sap");
    expect(reading.getAttribute("aria-label")).toBe("Open Account › Usage");
    expect(screen.queryByTestId("usage-more")).toBeNull();
    // The reading sits before the terminal and theme switches, beside any provider problem.
    const parts = [...statusLine().children];
    const slot = reading.parentElement!;
    expect(parts.indexOf(slot)).toBe(0);
    expect(statusLine().className).toContain("whitespace-nowrap");
    expect(within(statusLine()).queryByTestId("connection")).toBeNull();

    fireEvent.click(reading);
    const usage = await screen.findByTestId("usage");
    expect(screen.getByRole("region", { name: "Account" })).toBeTruthy();
    await waitFor(() => expect(scrolled.mock.contexts).toContain(usage));
    expect(within(usage).getByRole("article", { name: "Claude" }).getAttribute("data-state")).toBe("warn");
    stop();
  });

  it("clicked again with Account already open, scrolls back to Usage", async () => {
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    const { stop } = app("near");
    fireEvent.click(await screen.findByTestId("usage-reading"));
    const usage = await screen.findByTestId("usage");
    // Once the plan's cards have loaded above it, the scrolls settle.
    await screen.findByTestId("sign-in");
    await waitFor(() => expect(scrolled.mock.contexts).toContain(usage));
    const before = scrolled.mock.calls.length;
    fireEvent.click(screen.getByTestId("usage-reading"));
    await waitFor(() => expect(scrolled.mock.calls.length).toBe(before + 1));
    expect(scrolled.mock.contexts.at(-1)).toBe(usage);
    stop();
  });

  it("with several near reads the worst and +N for the others", async () => {
    const { stop } = app("several");
    const reading = await screen.findByTestId("usage-reading");
    // Limited first (the engine's order): Antigravity refuses work until 7:18pm, Claude and Codex past 80%.
    expect(reading.getAttribute("data-provider")).toBe("antigravity");
    expect(within(reading).getByText("Antigravity limit · 7:18pm")).toBeTruthy();
    expect(screen.getByTestId("usage-more").textContent).toBe("+2");
    expect(screen.getByTestId("usage-more").className).toContain("text-ink-3");
    stop();
  });
});

describe("the usage feed", () => {
  function sidebar(daemon: FakeDaemon, pollMs: number) {
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    const navigate = vi.fn<(r: Route) => void>();
    render(
      <DaemonContext.Provider value={client}>
        <StoreContext.Provider value={store}>
          <SessionsProvider client={client}>
            <UsageProvider client={client} pollMs={pollMs}>
              <Sidebar route={{ screen: "new" }} navigate={navigate} refresh={sync.refresh} />
            </UsageProvider>
          </SessionsProvider>
        </StoreContext.Provider>
      </DaemonContext.Provider>,
    );
    return { navigate, stop: () => sync.stop() };
  }

  it("follows the engine on a gentle poll: the reading comes when a provider nears its limit and goes once it is back", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], usage: usageScenario("normal") });
    const { navigate, stop } = sidebar(daemon, 40);
    await waitFor(() => expect(usageAsked(daemon)).toBeGreaterThan(0));
    expect(screen.queryByTestId("usage-reading")).toBeNull();
    daemon.data.usage = usageScenario("near");
    const reading = await screen.findByTestId("usage-reading");
    fireEvent.click(reading);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "account", section: "usage", ask: expect.any(Number) });
    daemon.data.usage = usageScenario("normal");
    await waitFor(() => expect(screen.queryByTestId("usage-reading")).toBeNull());
    stop();
  });

  it("is never asked per bus event: a burst of events costs no usage requests", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], usage: usageScenario("near") });
    const { stop } = sidebar(daemon, 60_000);
    await screen.findByTestId("usage-reading");
    const before = usageAsked(daemon);
    for (let i = 0; i < 30; i++) daemon.publish("agent.status", { agent_id: "sup1", payload: { status: i % 2 ? "idle" : "processing" } });
    // The sessions feed follows the bus; usage stays put.
    await waitFor(() => expect(daemon.requests.filter((r) => r.path === "/sessions").length).toBeGreaterThan(1));
    expect(usageAsked(daemon)).toBe(before);
    expect(before).toBeLessThanOrEqual(2);
    stop();
  });
});
