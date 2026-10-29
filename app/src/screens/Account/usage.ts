/* Usage's words, apart from React (design/usage/usage.js, which mirrors the
   engine's quota.py): each provider's name and the CLI that reports for it,
   a reset time as quota._when writes it ("5:10pm" today, "Thu 9:00am"
   later) or "in N min" under the hour, how old a reading is, and the two
   thresholds brindle acts on: 80% is `brindle watch`'s alert line, 90% is
   RepoConfig.usage_limit, where brindle stops starting new work. The
   sidebar's status-line reading (design/usage/A4) is `nearReading`. Every
   function takes `now` (epoch seconds) so the words can be checked. */

import type { ProviderUsage, UsageNear, UsageWindow } from "../../api/client";

export const WARN = 80;
export const STOP = 90;

const NAMES: Record<string, string> = { claude: "Claude", codex: "Codex", antigravity: "Antigravity", native: "Local model", opencode: "OpenCode" };
const CLIS: Record<string, string> = { claude: "Claude Code", codex: "Codex", antigravity: "Antigravity", native: "local model server", opencode: "OpenCode" };

export const providerName = (p: string) => NAMES[p] ?? p.charAt(0).toUpperCase() + p.slice(1);
export const providerCli = (p: string) => CLIS[p] ?? providerName(p);

export const nowSeconds = () => Date.now() / 1000;

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
export const clock = (ts: number) => {
  const d = new Date(ts * 1000);
  const h = d.getHours();
  return `${h % 12 || 12}:${String(d.getMinutes()).padStart(2, "0")}${h >= 12 ? "pm" : "am"}`;
};
const sameDay = (ts: number, now: number) => new Date(ts * 1000).toDateString() === new Date(now * 1000).toDateString();
export const when = (ts: number, now: number) => (sameDay(ts, now) ? clock(ts) : `${DAYS[new Date(ts * 1000).getDay()]} ${clock(ts)}`);
/* Within the hour. */
export const soon = (ts: number | null, now: number) => ts != null && ts - now < 3600;
export const resets = (ts: number, now: number) => (soon(ts, now) ? `in ${Math.max(1, Math.round((ts - now) / 60))} min` : when(ts, now));
export const ago = (ts: number, now: number) => {
  const m = Math.round((now - ts) / 60);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : `${Math.round(m / 60)}h ago`;
};

/* A one-model cap (Fable) is named for its model in both words; a plan window has "5-hour" and "5h". */
export const isCap = (w: UsageWindow) => w.name === w.short;
export const planWindows = (q: ProviderUsage) => q.windows.filter((w) => !isCap(w));
export const caps = (q: ProviderUsage) => q.windows.filter(isCap);
/* The plan window nearest its limit (quota.fullest): a cap doesn't hold up the plan's other models. */
export const fullest = (q: ProviderUsage) => planWindows(q).reduce<UsageWindow | null>((a, w) => (!a || w.used > a.used ? w : a), null);

export type Level = "ok" | "warn" | "stop";
export const level = (used: number): Level => (used >= STOP ? "stop" : used >= WARN ? "warn" : "ok");

/* A provider's state in a word, for its card's edge: none, limit, stop, warn, soon (a reset within the hour), ok. */
export type UsageState = "none" | "limit" | "stop" | "warn" | "soon" | "ok";
export function stateOf(q: ProviderUsage, now: number): UsageState {
  if (!q.known) return "none";
  if (q.limited) return "limit";
  const w = fullest(q);
  if (!w) return "none";
  const l = level(w.used);
  if (l !== "ok") return l;
  return planWindows(q).some((x) => soon(x.resets_at, now)) ? "soon" : "ok";
}

/* The window's label on the Account page: "5-hour window", "Fable cap, this week". */
export const windowLabel = (w: UsageWindow) => (isCap(w) ? `${w.name} cap${w.minutes === 10080 ? ", this week" : ""}` : `${w.name} window`);
/* "Resets 4:40pm", "Resets in 12 min (3:10pm)", or that it isn't known. */
export const resetLine = (w: UsageWindow, now: number) =>
  w.resets_at == null ? "Reset time unknown" : `Resets ${resets(w.resets_at, now)}${soon(w.resets_at, now) ? ` (${clock(w.resets_at)})` : ""}`;

/* Why a provider shows no windows. */
export function noneText(p: string): string {
  if (p === "antigravity") return "No limit reported. Antigravity says nothing about its allowance until it refuses work; brindle then rests it for 5 hours and shows that here.";
  if (p === "opencode") return "Not reported. OpenCode publishes no usage limits, and its free models have none to count.";
  if (p === "native") return "Not reported. A local model runs on your own machine, with no subscription allowance to count.";
  return `Nothing reported yet. ${providerCli(p)} reports while a session of it runs.`;
}

/* What brindle does about it, past 80% and 90%. */
export function hint(q: ProviderUsage): string | null {
  const w = fullest(q);
  if (!w) return null;
  const name = providerName(q.provider);
  const claude = q.provider === "claude";
  if (w.used >= STOP) return `Past ${STOP}%: brindle has stopped starting new ${name} work${claude ? " and paused the Claude workers that were running; they resume when the window resets" : ""}.`;
  if (w.used >= WARN) return `Past ${WARN}%. brindle stops starting new ${name} work at ${STOP}%${claude ? " and pauses running Claude workers until the window resets" : ""}.`;
  return null;
}

/* The sidebar's status-line reading (design/usage/A4): the worst near
   provider (the engine lists them worst first) in the fewest words, "Claude
   88% · 5:10pm" or "Antigravity limit · 7:18pm", and how many more are
   near; null when none is. */
export function nearReading(near: UsageNear[], now: number): { provider: string; text: string; more: number } | null {
  const n = near[0];
  if (!n) return null;
  const head = n.reason === "limited" ? `${providerName(n.provider)} limit` : `${providerName(n.provider)}${n.used != null ? ` ${Math.round(n.used)}%` : ""}`;
  return { provider: n.provider, text: n.resets_at != null ? `${head} · ${resets(n.resets_at, now)}` : head, more: near.length - 1 };
}
