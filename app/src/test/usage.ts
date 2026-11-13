/* GET /usage as the engine serves it (routes_read.get_usage), in the
   readings design/usage drew: normal (nothing near), near (Claude's 5-hour
   window at 88%), several (Claude at 88%, Codex's week at 82%, Antigravity
   refusing work until 7:18pm) and reset (Claude at 94% resetting in 12
   minutes, Codex's 5-hour window in 25, Antigravity back in 6). Times are
   on `now`'s day, so at the design's clock (Wednesday 2:58pm) they read as
   it does. `near` is worked out as the engine does. */

import type { ProviderUsage, Usage, UsageNear, UsageWindow } from "../api/client";

export type UsageScenario = "normal" | "near" | "several" | "reset";

/* The design's clock: Wednesday Oct 21 2026, 2:58pm, local time. */
export const DESIGN_NOW = new Date(2026, 9, 21, 14, 58);

const H5 = 300;
const WK = 10080;
const win = (used: number, resets_at: number | null, minutes: number, cap?: string): UsageWindow =>
  cap ? { used, resets_at, minutes, name: cap, short: cap } : { used, resets_at, minutes, name: minutes === H5 ? "5-hour" : "weekly", short: minutes === H5 ? "5h" : "wk" };
const unknown = (provider: string): ProviderUsage => ({ provider, known: false, limited: false, limited_until: null, updated_at: null, windows: [] });
const known = (provider: string, updated_at: number, windows: UsageWindow[], limited_until: number | null = null): ProviderUsage => ({ provider, known: true, limited: limited_until != null, limited_until, updated_at, windows });

/* routes_read.get_usage's `near`: limited, past 80% of the fullest plan window, or a plan window resetting within the hour; limited first, then the fuller. */
export function nearOf(providers: ProviderUsage[], now: number): UsageNear[] {
  const near: UsageNear[] = [];
  for (const q of providers) {
    if (!q.known) continue;
    const plan = q.windows.filter((w) => w.name !== w.short);
    const top = plan.reduce<UsageWindow | null>((a, w) => (!a || w.used > a.used ? w : a), null);
    const soon = plan.filter((w) => w.resets_at != null && w.resets_at - now <= 3600).sort((a, b) => a.resets_at! - b.resets_at!)[0];
    if (q.limited) near.push({ provider: q.provider, reason: "limited", used: top?.used ?? null, resets_at: q.limited_until });
    else if (top && top.used >= 80) near.push({ provider: q.provider, reason: "high", used: top.used, resets_at: top.resets_at });
    else if (soon) near.push({ provider: q.provider, reason: "resets_soon", used: soon.used, resets_at: soon.resets_at });
  }
  return near.sort((a, b) => Number(a.reason !== "limited") - Number(b.reason !== "limited") || (b.used ?? 0) - (a.used ?? 0));
}

export function usageScenario(name: UsageScenario, nowMs: number = Date.now()): Usage {
  const now = nowMs / 1000;
  const today = new Date(nowMs);
  const at = (h: number, m: number, days = 0) => new Date(today.getFullYear(), today.getMonth(), today.getDate() + days, h, m).getTime() / 1000;
  const inMin = (n: number) => now + n * 60;
  const providers: Record<UsageScenario, ProviderUsage[]> = {
    normal: [
      known("claude", now - 95, [win(36, at(17, 10), H5), win(9, at(13, 0, 4), WK), win(11, at(13, 0, 4), WK, "Fable")]),
      known("codex", now - 28, [win(22, at(18, 38), H5), win(41, at(9, 0, 1), WK)]),
      unknown("antigravity"),
      unknown("native"),
    ],
    near: [
      known("claude", now - 40, [win(88, at(17, 10), H5), win(63, at(13, 0, 4), WK), win(71, at(13, 0, 4), WK, "Fable")]),
      known("codex", now - 28, [win(34, at(18, 38), H5), win(41, at(9, 0, 1), WK)]),
      unknown("antigravity"),
      unknown("native"),
    ],
    several: [
      known("claude", now - 40, [win(88, at(16, 40), H5), win(63, at(13, 0, 4), WK), win(71, at(13, 0, 4), WK, "Fable")]),
      known("codex", now - 28, [win(34, at(18, 38), H5), win(82, at(9, 0, 1), WK)]),
      known("antigravity", at(14, 18), [], at(19, 18)),
      unknown("native"),
    ],
    reset: [
      known("claude", now - 40, [win(94, inMin(12), H5), win(64, at(13, 0, 4), WK), win(72, at(13, 0, 4), WK, "Fable")]),
      known("codex", now - 28, [win(71, inMin(25), H5), win(83, at(9, 0, 1), WK)]),
      known("antigravity", at(10, 4), [], inMin(6)),
      unknown("native"),
    ],
  };
  return { providers: providers[name], near: nearOf(providers[name], now) };
}
