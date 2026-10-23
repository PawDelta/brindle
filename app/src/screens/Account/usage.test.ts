import { describe, expect, it } from "vitest";
import { DESIGN_NOW, usageScenario } from "../../test/usage";
import { caps, fullest, hint, nearReading, planWindows, resetLine, resets, stateOf, windowLabel } from "./usage";

const now = DESIGN_NOW.getTime() / 1000;
const at = (h: number, m: number, days = 0) => new Date(2026, 9, 21 + days, h, m).getTime() / 1000;
const scenario = (name: Parameters<typeof usageScenario>[0]) => usageScenario(name, DESIGN_NOW.getTime());

describe("usage words", () => {
  it("says a reset as quota._when does: the clock today, the day after, minutes under the hour", () => {
    expect(resets(at(17, 10), now)).toBe("5:10pm");
    expect(resets(at(9, 0, 1), now)).toBe("Thu 9:00am");
    expect(resets(now + 12 * 60, now)).toBe("in 12 min");
    expect(resetLine({ used: 94, resets_at: now + 12 * 60, minutes: 300, name: "5-hour", short: "5h" }, now)).toBe("Resets in 12 min (3:10pm)");
    expect(resetLine({ used: 9, resets_at: null, minutes: 10080, name: "weekly", short: "wk" }, now)).toBe("Reset time unknown");
  });

  it("tells a one-model cap from the plan's windows, and the plan's fullest from them", () => {
    const claude = scenario("near").providers[0];
    expect(planWindows(claude).map(windowLabel)).toEqual(["5-hour window", "weekly window"]);
    expect(caps(claude).map(windowLabel)).toEqual(["Fable cap, this week"]);
    expect(fullest(claude)!.used).toBe(88);
    expect(hint(claude)).toBe("Past 80%. Autopilot stops starting new Claude work at 90% and pauses running Claude workers until the window resets.");
  });

  it("puts each provider in a state for its card", () => {
    const states = (name: Parameters<typeof scenario>[0]) => scenario(name).providers.map((q) => stateOf(q, now));
    expect(states("normal")).toEqual(["ok", "ok", "none", "none"]);
    expect(states("several")).toEqual(["warn", "warn", "limit", "none"]);
    expect(states("reset")).toEqual(["stop", "warn", "limit", "none"]);
  });

  it("reads the worst near provider in the fewest words, with how many more", () => {
    expect(nearReading(scenario("normal").near, now)).toBeNull();
    expect(nearReading(scenario("near").near, now)).toEqual({ provider: "claude", text: "Claude 88% · 5:10pm", more: 0 });
    expect(nearReading(scenario("several").near, now)).toEqual({ provider: "antigravity", text: "Antigravity limit · 7:18pm", more: 2 });
    expect(nearReading(scenario("reset").near, now)).toEqual({ provider: "antigravity", text: "Antigravity limit · in 6 min", more: 2 });
    expect(nearReading([{ provider: "codex", reason: "resets_soon", used: 71.4, resets_at: now + 25 * 60 }], now)!.text).toBe("Codex 71% · in 25 min");
  });
});
