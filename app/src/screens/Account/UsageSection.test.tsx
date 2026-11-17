import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { UsageContext } from "../../store/usage";
import { usageScenario } from "../../test/usage";
import { UsageSection } from "./UsageSection";

afterEach(cleanup);

it("renders Antigravity model quotas with the same meters as Claude windows", () => {
  const now = Date.now() / 1000;
  const usage = usageScenario("normal");
  const claude = usage.providers[0];
  claude.windows[0].resets_at = now + 7200;
  const antigravity = usage.providers.find((q) => q.provider === "antigravity")!;
  Object.assign(antigravity, {
    known: true,
    updated_at: now,
    windows: [
      { name: "Gemini Pro 5h", short: "Gemini Pro 5h", minutes: 300, used: 42, resets_at: claude.windows[0].resets_at },
      { name: "Gemini Flash wk", short: "Gemini Flash wk", minutes: 10080, used: 67, resets_at: null },
    ],
  });
  render(
    <UsageContext.Provider value={{ usage, error: null, reload: async () => {} }}>
      <UsageSection />
    </UsageContext.Provider>,
  );
  const claudeCard = screen.getByRole("article", { name: "Claude" });
  const agyCard = screen.getByRole("article", { name: "Antigravity" });
  const claudeRow = within(claudeCard).getAllByRole("meter")[0].parentElement!;
  const meters = within(agyCard).getAllByRole("meter");
  expect(meters).toHaveLength(2);
  meters.forEach((meter, i) => {
    const row = meter.parentElement!;
    expect(row.className).toBe(claudeRow.className);
    expect(row.getAttribute("data-window")).toBe(antigravity.windows[i].short);
    expect(meter.getAttribute("aria-valuenow")).toBe(String(antigravity.windows[i].used));
    expect(within(row).getByTestId("usage-percent")).toBeTruthy();
    expect(within(row).getByTestId("usage-reset").className).toBe(within(claudeRow).getByTestId("usage-reset").className);
  });
  expect(within(meters[0].parentElement!).getByTestId("usage-reset").textContent).toBe(within(claudeRow).getByTestId("usage-reset").textContent);
});
