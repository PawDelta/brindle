import { connectDaemon, renderApp, inTestContext } from "../test/harness";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { ONBOARDED_KEY } from "../api/providers";
import type { SessionDTO } from "../api/schema";
import type { Route } from "../routes";
import { UsageProvider } from "../store/usage";
import { FakeDaemon, type DaemonData } from "../test/fakeDaemon";
import { REPO, root } from "../test/fixtures";
import { DESIGN_NOW, usageScenario, type UsageScenario } from "../test/usage";
import { Sidebar } from "./Sidebar";

vi.mock("./PtyTerminal", () => import("../test/panes"));

const session: SessionDTO = { id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 0, goal: "Ship ls --json" };

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(ONBOARDED_KEY, "1");
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
  const { client, store, sync } = connectDaemon(daemon);
  renderApp(<App store={store} client={client} refresh={sync.refresh} />);
  return { daemon, stop: () => sync.stop() };
}

const foot = () => screen.getByTestId("sidebar-foot");
const usageAsked = (daemon: FakeDaemon) => daemon.requests.filter((r) => r.method === "GET" && r.path === "/usage").length;
/* The button renders as a plain "Usage" before the first load lands, so finding it says nothing about its data. */
const loadedReading = () =>
  waitFor(() => {
    const reading = screen.getByTestId("profile-usage");
    expect(reading.dataset.state).toBe("reading");
    return reading;
  });

describe("the sidebar's weekly usage reading", () => {
  it.each(["normal", "several"] as const)("sidebar foot usage line shows weekly usage and warnings, initials open Account, and theme choices live in Settings (%s)", async (scenario) => {
    const { daemon } = app(scenario, { account: { signed_in: true, name: "Hannah Lerner", plan: "pro" } });
    await waitFor(() => expect(usageAsked(daemon)).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getByTestId("sidebar-foot-row")).toBeTruthy());
    const reading = await loadedReading();
    expect(reading.dataset.provider).toBe("claude");
    expect(reading.className).toContain(scenario === "normal" ? "text-ink-3" : "text-sap");
    expect(reading.textContent).toContain(scenario === "normal" ? "9%" : "63%");
    expect(screen.queryByRole("group", { name: "Theme" })).toBeNull();
    const account = screen.getByRole("button", { name: "Open Account › Usage" });
    await waitFor(() => expect(account.firstElementChild!.textContent).toBe("HL"));
    fireEvent.click(account);
    expect(screen.getByRole("tab", { name: "Usage" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.click(screen.getByRole("tab", { name: "Account" }));
    expect(await screen.findByRole("region", { name: "Account" })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    // Signing out returns to the sign-in gate: nothing else, the sidebar gone.
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    await screen.findByTestId("signin-gate");
    expect(screen.queryByRole("complementary", { name: "Sidebar" })).toBeNull();
    // Signed in again (another account): back in the app, its initials in the sidebar.
    daemon.deferLogin = true;
    daemon.data.accountAfterLogin = { signed_in: true, name: "Sam Rivera", plan: "pro" };
    fireEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    await screen.findByText("Waiting for the browser…");
    daemon.completeLogin();
    await waitFor(() => expect(screen.getByRole("button", { name: "Open Account › Usage" }).firstElementChild!.textContent).toBe("SR"));
    // Settings opens from the profile.
    expect(within(screen.getByTestId("sidebar-foot-row")).queryByRole("button", { name: "Settings" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open Account › Usage" }));
    fireEvent.click(await screen.findByRole("tab", { name: "General" }));
    const settings = await screen.findByRole("region", { name: "Settings" });
    const theme = within(settings).getByRole("group", { name: "Theme" });
    fireEvent.click(within(theme).getByRole("button", { name: "Dark" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    fireEvent.click(within(theme).getByRole("button", { name: "Light" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    fireEvent.click(within(theme).getByRole("button", { name: "System" }));
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(within(screen.getByRole("complementary", { name: "Sidebar" })).queryByRole("group", { name: "Theme" })).toBeNull();
    // The walkthrough has no button in the foot any more.
    expect(screen.queryByRole("button", { name: "Show the walkthrough" })).toBeNull();
    // The reset day shows only from 80% of the week: Codex at 41% has none, at 82% it does.
    expect(reading.textContent?.includes("·")).toBe(scenario === "several");
    expect(within(foot()).queryByTestId("connection")).toBeNull();
    // The footer stays on one row.
    const pages = screen.getByTestId("sidebar-foot-row");
    expect(pages.className).toContain("flex-nowrap");
    expect(pages.className).toContain("whitespace-nowrap");
    expect(within(pages).getByRole("button", { name: "Open Account › Usage" }).querySelector("button")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open Account › Usage" }));
    fireEvent.click(screen.getByRole("tab", { name: "Account" }));
    await screen.findByTestId("plan");
  });

  it("near a limit shows the worst provider in a few warm words, and opens Account › Usage", async () => {
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    app("near");
    const reading = await loadedReading();
    expect(reading.dataset.provider).toBe("claude");
    expect(reading.textContent).toContain("63%");
    expect(reading.className).toContain("text-sap");
    expect(reading.closest("button")?.getAttribute("aria-label")).toBe("Open Account › Usage");
    expect(screen.queryByTestId("usage-more")).toBeNull();
    expect(reading.closest("button")?.parentElement).toBe(screen.getByTestId("sidebar-foot-row"));

    fireEvent.click(reading);
    const usage = await screen.findByTestId("usage");
    expect(screen.getByRole("tab", { name: "Usage" }).getAttribute("aria-selected")).toBe("true");
    await waitFor(() => expect(scrolled.mock.contexts).toContain(usage));
    expect(within(usage).getByRole("article", { name: "Claude" }).getAttribute("data-state")).toBe("warn");
  });

  it("keeps the selected session provider reading in the pill without repeating its model", async () => {
    const { stop } = app("normal", { workspaces: [{ ...root, agents: root.agents?.map((a) => ({ ...a, model: "claude-opus-4-7" })) }] });
    await waitFor(() => expect(screen.getByTestId("profile-usage").dataset.provider).toBe("claude"));
    expect(screen.queryByTestId("usage-model")).toBeNull();
    // The reading follows the session's provider (Claude 9%), rather than the fuller Codex (41%).
    const reading = screen.getByTestId("profile-usage");
    expect(reading.dataset.provider).toBe("claude");
    expect(reading.textContent).toContain("9%");
    stop();
    cleanup();

    // Switching sessions moves the provider reading. Antigravity is limited with no weekly window;
    // keep the warning and show the neutral usage state.
    const sup2: SessionDTO = { ...session, id: "sup2", goal: "Other work" };
    const agents = [
      { ...root.agents![0], model: "claude-opus-4-7" },
      { ...root.agents![0], id: "sup2", provider: "antigravity", profile: "antigravity", model: "gemini-3-pro" },
    ];
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session, sup2], workspaces: [{ ...root, agents }], usage: usageScenario("several") });
    const { client, store, sync } = connectDaemon(daemon);
    const tree = (root: string) => (
      inTestContext(<><UsageProvider client={client} pollMs={60_000}>
              <Sidebar route={{ screen: "session", root, repo: REPO, agent: root }} navigate={vi.fn()} refresh={sync.refresh} />
            </UsageProvider></>, { client, store }, { daemon: true, sessions: true })
    );
    const view = render(tree("sup1"));
    await waitFor(() => expect(screen.getByTestId("profile-usage").dataset.provider).toBe("claude"));
    expect(screen.queryByTestId("usage-model")).toBeNull();
    expect(screen.getByTestId("profile-usage").dataset.provider).toBe("claude");
    expect(screen.getByTestId("profile-usage").textContent).toContain("63%");
    view.rerender(tree("sup2"));
    await waitFor(() => expect(screen.getByTestId("profile-usage").dataset.state).toBe("none"));
    const second = screen.getByTestId("profile-usage");
    expect(second.dataset.provider).toBeUndefined();
    expect(second.querySelector("[role=progressbar]")).toBeNull();
    expect(second.className).toContain("text-sap");
  });

  it("clicked again with Account already open, scrolls back to Usage", async () => {
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    app("near");
    fireEvent.click(await screen.findByTestId("profile-usage"));
    const usage = await screen.findByTestId("usage");
    await waitFor(() => expect(scrolled.mock.contexts).toContain(usage));
    const before = scrolled.mock.calls.length;
    fireEvent.click(screen.getByTestId("profile-usage"));
    await waitFor(() => expect(scrolled.mock.calls.length).toBe(before + 1));
    expect(scrolled.mock.contexts.at(-1)).toBe(usage);
  });

});

describe("the usage feed", () => {
  function sidebar(daemon: FakeDaemon, pollMs: number) {
    const { client, store, sync } = connectDaemon(daemon);
    const navigate = vi.fn<(r: Route) => void>();
    render(
      inTestContext(<><UsageProvider client={client} pollMs={pollMs}>
              <Sidebar route={{ screen: "new" }} navigate={navigate} refresh={sync.refresh} />
            </UsageProvider></>, { client, store }, { daemon: true, sessions: true }),
    );
    return { navigate, stop: () => sync.stop() };
  }

  it("follows the engine on a gentle poll: the weekly reading changes provider and warning style as usage changes", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], usage: usageScenario("normal") });
    const { navigate } = sidebar(daemon, 40);
    await waitFor(() => expect(usageAsked(daemon)).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getByTestId("profile-usage").dataset.provider).toBe("codex"));
    daemon.data.usage = usageScenario("near");
    const reading = await screen.findByTestId("profile-usage");
    await waitFor(() => expect(reading.dataset.provider).toBe("claude"));
    expect(reading.className).toContain("text-sap");
    fireEvent.click(reading);
    expect(navigate).toHaveBeenLastCalledWith({ screen: "account", section: "usage", ask: expect.any(Number) });
    daemon.data.usage = usageScenario("normal");
    await waitFor(() => expect(reading.dataset.provider).toBe("codex"));
    expect(reading.className).toContain("text-ink-3");
  });

  it("is never asked per bus event: a burst of events costs no usage requests", async () => {
    const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], sessions: [session], workspaces: [root], usage: usageScenario("near") });
    sidebar(daemon, 60_000);
    await screen.findByTestId("profile-usage");
    const before = usageAsked(daemon);
    for (let i = 0; i < 30; i++) daemon.publish("agent.status", { agent_id: "sup1", payload: { status: i % 2 ? "idle" : "processing" } });
    // The sessions feed follows the bus; usage stays put.
    await waitFor(() => expect(daemon.requests.filter((r) => r.path === "/sessions").length).toBeGreaterThan(1));
    expect(usageAsked(daemon)).toBe(before);
    expect(before).toBeLessThanOrEqual(2);
  });
});
