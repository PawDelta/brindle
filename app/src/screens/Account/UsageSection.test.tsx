import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { UsageProvider } from "../../store/usage";
import { StoreContext } from "../../store/useStore";
import { FakeDaemon } from "../../test/fakeDaemon";
import { REPO } from "../../test/fixtures";
import { DESIGN_NOW, usageScenario, type UsageScenario } from "../../test/usage";
import { AccountScreen } from ".";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(DESIGN_NOW);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function setup(scenario: UsageScenario) {
  const daemon = new FakeDaemon({ repos: [{ repo_root: REPO, name: "proj" }], account: { signed_in: false }, usage: usageScenario(scenario) });
  const store = createAppStore();
  const client = daemon.client();
  const sync = startSync({ client, store, connect: daemon.connect });
  render(
    <StoreContext.Provider value={store}>
      <SessionsProvider client={client}>
        <UsageProvider client={client}>
          <AccountScreen client={client} openUrl={vi.fn(() => Promise.resolve())} section="usage" />
        </UsageProvider>
      </SessionsProvider>
    </StoreContext.Provider>,
  );
  return { daemon, stop: () => sync.stop() };
}

const card = (name: string) => within(screen.getByTestId("usage")).getByRole("article", { name });
const windows = (el: HTMLElement) =>
  [...el.querySelectorAll<HTMLElement>("[data-window]")].map((w) => ({
    window: w.getAttribute("data-window"),
    level: w.getAttribute("data-level"),
    bar: (w.querySelector("[role=meter] i") as HTMLElement).style.width,
    text: w.textContent,
  }));

describe("Account › Usage", () => {
  it("has a card per provider, a bar per window with the percent used and the reset time, caps in small print, and why a provider shows nothing", async () => {
    const { daemon, stop } = setup("normal");
    const usage = await screen.findByTestId("usage");
    expect(within(usage).getByRole("heading", { name: "Usage" })).toBeTruthy();
    await waitFor(() => expect(within(usage).getAllByRole("article")).toHaveLength(4));
    expect(within(usage).getAllByRole("article").map((a) => a.getAttribute("aria-label"))).toEqual(["Claude", "Codex", "Antigravity", "Local model"]);
    // Opening the page asks again, whatever the last poll said.
    expect(daemon.requests.filter((r) => r.path === "/usage").length).toBeGreaterThanOrEqual(1);

    const claude = card("Claude");
    expect(claude.textContent).toContain("Claude Code");
    expect(claude.textContent).toContain("updated 2 min ago");
    expect(claude.getAttribute("data-state")).toBe("ok");
    expect(windows(claude)).toEqual([
      { window: "5h", level: "ok", bar: "36%", text: "5-hour window36% usedResets 5:10pm" },
      { window: "wk", level: "ok", bar: "9%", text: "weekly window9% usedResets Sun 1:00pm" },
    ]);
    expect(within(claude).getByTestId("usage-caps").textContent).toBe("Fable cap · 11% used · resets Sun 1:00pm");
    expect(windows(card("Codex")).map((w) => w.text)).toEqual(["5-hour window22% usedResets 6:38pm", "weekly window41% usedResets Thu 9:00am"]);
    expect(card("Antigravity").textContent).toContain("No limit reported. Antigravity says nothing about its allowance until it refuses work");
    expect(card("Local model").textContent).toContain("Not reported.");
    expect(within(usage).queryByRole("alert")).toBeNull();
    stop();
  });

  it("near a limit: the window turns warm or red, a limited provider says when it is back, and what brindle does", async () => {
    const { stop } = setup("reset");
    await waitFor(() => expect(card("Claude").getAttribute("data-state")).toBe("stop"));
    const claude = windows(card("Claude"));
    expect(claude[0]).toEqual({ window: "5h", level: "stop", bar: "94%", text: "5-hour window94% usedResets in 12 min (3:10pm)" });
    expect(card("Claude").textContent).toContain("Past 90%: brindle has paused new Claude work by stopping new starts and paused the Claude workers that were running");
    expect(within(card("Claude")).getAllByTestId("usage-percent")[0].className).toContain("text-fail");
    // A reset within the hour reads green.
    expect(within(card("Claude")).getAllByTestId("usage-reset")[0].className).toContain("text-pass");
    expect(card("Codex").getAttribute("data-state")).toBe("warn");
    expect(windows(card("Codex"))[1]).toMatchObject({ level: "warn", bar: "83%" });
    expect(card("Codex").textContent).toContain("Past 80%. brindle pauses new Codex work by stopping new starts at 90%.");
    const ag = card("Antigravity");
    expect(ag.getAttribute("data-state")).toBe("limit");
    expect(within(ag).getByTestId("usage-limited").textContent).toContain("Limit reached · available again in 6 min (3:04pm)");
    expect(ag.querySelector("[data-window]")).toBeNull();
    stop();
  });
});
