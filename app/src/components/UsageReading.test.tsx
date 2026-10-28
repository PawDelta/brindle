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
import { FakeDaemon, type DaemonData } from "../test/fakeDaemon";
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

function app(scenario: UsageScenario, data: Partial<DaemonData> = {}) {
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], usage: usageScenario(scenario), ...data });
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

describe("the sidebar's weekly usage reading", () => {
  it.each(["normal", "several"] as const)("sidebar foot usage line shows weekly usage and warnings, initials open Account, and theme choices live in Settings (%s)", async (scenario) => {
    const { daemon, stop } = app(scenario, { account: { signed_in: true, name: "Hannah Lerner", plan: "pro" } });
    await waitFor(() => expect(usageAsked(daemon)).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getByTestId("pages")).toBeTruthy());
    const reading = await screen.findByTestId("usage-reading");
    expect(reading.dataset.provider).toBe("codex");
    expect(reading.className).toContain(scenario === "normal" ? "text-ink-3" : "text-sap");
    expect(within(statusLine()).getByRole("progressbar").getAttribute("aria-valuenow")).toBe(scenario === "normal" ? "41" : "82");
    expect(screen.queryByRole("group", { name: "Theme" })).toBeNull();
    const account = screen.getByRole("button", { name: "Open Account" });
    await waitFor(() => expect(account.textContent).toBe("HL"));
    fireEvent.click(account);
    expect(await screen.findByRole("region", { name: "Account" })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    await screen.findByTestId("sign-in");
    await waitFor(() => expect(account.textContent).toBe("?"));
    fireEvent.click(within(screen.getByTestId("pages")).getByRole("button", { name: "Settings" }));
    const settings = await screen.findByRole("region", { name: "Settings" });
    const theme = within(settings).getByRole("group", { name: "Theme" });
    fireEvent.click(within(theme).getByRole("button", { name: "Dark" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    fireEvent.click(within(theme).getByRole("button", { name: "Light" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    fireEvent.click(within(theme).getByRole("button", { name: "System" }));
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(within(screen.getByRole("complementary", { name: "Sidebar" })).queryByRole("group", { name: "Theme" })).toBeNull();
    expect(within(statusLine()).queryByTestId("connection")).toBeNull();
    // The footer's links never wrap, whatever the status line carries.
    const pages = screen.getByTestId("pages");
    expect(pages.className).toContain("flex-nowrap");
    expect(pages.className).toContain("whitespace-nowrap");
    expect(within(pages).getByRole("button", { name: "Agents" }).querySelector("[data-testid*='badge']")).toBeNull();
    fireEvent.click(account);
    await screen.findByTestId("sign-in");
    expect(account.textContent).toBe("?");
    daemon.deferLogin = true;
    daemon.data.accountAfterLogin = { signed_in: true, name: "Sam Rivera", plan: "pro" };
    fireEvent.click(screen.getByRole("button", { name: "Use a device code instead" }));
    await screen.findByTestId("sign-in-state");
    daemon.completeLogin();
    await screen.findByTestId("plan");
    await waitFor(() => expect(account.textContent).toBe("SR"));
    stop();
  });

  it("near a limit shows the worst provider in a few warm words, and opens Account › Usage", async () => {
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    const { stop } = app("near");
    const reading = await screen.findByTestId("usage-reading");
    expect(reading.dataset.provider).toBe("claude");
    expect(within(reading).getByRole("progressbar").getAttribute("aria-valuenow")).toBe("63");
    expect(reading.className).toContain("text-sap");
    expect(reading.getAttribute("aria-label")).toBe("Open Account › Usage");
    expect(screen.queryByTestId("usage-more")).toBeNull();
    // The reading is above the labelled navigation row, beside any provider problem.
    const parts = [...statusLine().children];
    expect(parts.indexOf(reading)).toBe(0);
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

  it("follows the engine on a gentle poll: the weekly reading changes provider and warning style as usage changes", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], usage: usageScenario("normal") });
    const { navigate, stop } = sidebar(daemon, 40);
    await waitFor(() => expect(usageAsked(daemon)).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getByTestId("usage-reading").dataset.provider).toBe("codex"));
    daemon.data.usage = usageScenario("near");
    const reading = await screen.findByTestId("usage-reading");
    await waitFor(() => expect(reading.dataset.provider).toBe("claude"));
    expect(reading.className).toContain("text-sap");
    fireEvent.click(reading);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "account", section: "usage", ask: expect.any(Number) });
    daemon.data.usage = usageScenario("normal");
    await waitFor(() => expect(reading.dataset.provider).toBe("codex"));
    expect(reading.className).toContain("text-ink-3");
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
