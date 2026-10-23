/* What the app says about each provider CLI (the first launch and Settings
   › Providers) that the engine's GET /providers/installed doesn't: who
   makes it, one line about it, whose official installer Connect runs (and
   where that installer comes from), whose terms installing it accepts,
   whose account its own sign-in uses, and where brindle keeps its settings.
   The engine's rows say what is on this Mac: brindle uses a CLI it finds
   as-is, and the CLI keeps itself up to date. */

import type { ProviderInstalled } from "./client";
import type { Install } from "./providersInstall";

export interface ProviderFacts {
  name: string;
  maker: string;
  blurb: string;
  /* The maker's official installer, as Connect runs it. */
  installer: { label: string; url: string };
  license: { label: string; url: string };
  /* Whose sign-in it is ("Sign in to Claude") and with what. */
  signin: string;
  account: string;
}

export const FACTS: Record<string, ProviderFacts> = {
  claude: {
    name: "Claude Code",
    maker: "Anthropic",
    blurb: "Anthropic's agent in the terminal, for the Claude models.",
    installer: { label: "claude.ai/install.sh", url: "https://claude.ai/install.sh" },
    license: { label: "Anthropic Commercial Terms of Service", url: "https://www.anthropic.com/legal/commercial-terms" },
    signin: "Claude",
    account: "your Claude subscription or Console account",
  },
  codex: {
    name: "Codex",
    maker: "OpenAI",
    blurb: "OpenAI's agent in the terminal, for the GPT models.",
    installer: { label: "github.com/openai/codex", url: "https://github.com/openai/codex#installing-and-running-codex-cli" },
    license: { label: "Apache License 2.0", url: "https://github.com/openai/codex/blob/main/LICENSE" },
    signin: "OpenAI",
    account: "your ChatGPT account",
  },
  opencode: {
    name: "OpenCode",
    maker: "Anomaly",
    blurb: "The open-source agent that runs on the models you bring.",
    installer: { label: "opencode.ai/install", url: "https://opencode.ai/install" },
    license: { label: "MIT License", url: "https://github.com/anomalyco/opencode/blob/dev/LICENSE" },
    signin: "OpenCode",
    account: "a model provider of your choice",
  },
  antigravity: {
    name: "Antigravity",
    maker: "Google",
    blurb: "Google's agent for the Gemini models.",
    installer: { label: "antigravity.google", url: "https://antigravity.google" },
    license: { label: "Google Antigravity Terms of Service", url: "https://antigravity.google/terms" },
    signin: "Google",
    account: "your Google account",
  },
};

/* A provider the engine knows but this table doesn't: its own name, nothing more. */
export const factsOf = (row: Pick<ProviderInstalled, "provider" | "name">): ProviderFacts =>
  FACTS[row.provider] ?? { name: row.name, maker: row.name, blurb: "", installer: { label: "", url: "" }, license: { label: "", url: "" }, signin: row.name, account: "its own account" };

/* Where brindle keeps the CLI's settings:
   a copy of its own for each, except Antigravity, which has no setting for
   it and shares the person's ~/.gemini. */
export const settingsNote = (row: Pick<ProviderInstalled, "provider" | "name">): string =>
  row.provider === "antigravity" ? `${factsOf(row).name} shares its settings with yours, in ~/.gemini.` : `brindle keeps its own copy of ${factsOf(row).name}'s settings.`;

/* brindle found the CLI on this Mac and uses it as-is (not one the person turned off). */
export const connected = (row: ProviderInstalled): boolean => row.state === "installed" && !!row.path;

/* Taken out of brindle (DELETE /providers/{p}) but still on this Mac: Connect turns it back on. */
export const turnedOff = (row: ProviderInstalled): boolean => row.state === "off";

/* Signed in, as the engine confirms it. */
export const signedIn = (row: ProviderInstalled): boolean => connected(row) && row.signed_in === true;

/* Its sign-in is still to do: signed out, or not known to be signed in (null, "couldn't tell"). */
export const unsigned = (row: ProviderInstalled): boolean => connected(row) && row.signed_in !== true;

/* The first launch is over once one provider is connected and confirmed
   signed in; until then every launch opens on it. */
export const onboarded = (rows: ProviderInstalled[]): boolean => rows.some(signedIn);

/* Kept once a provider has been confirmed connected and signed in: the
   first launch is done for good on this Mac, and a later launch goes
   straight to the app even when that provider has since signed out (the
   app says so where it belongs, not with the first launch again). */
export const ONBOARDED_KEY = "brindle.onboarded";

export function readOnboarded(): boolean {
  try {
    return localStorage.getItem(ONBOARDED_KEY) === "1";
  } catch {
    return false;
  }
}

export function markOnboarded(): void {
  try {
    localStorage.setItem(ONBOARDED_KEY, "1");
  } catch {
    // Storage refused: the engine's answer decides next time.
  }
}

/* What the sidebar's status line says about providers: only a problem, a
   provider that can't run sessions as it is. Signed out first (sessions on
   it stop), then an install that failed. */
export interface ProviderProblem {
  provider: string;
  kind: "signed_out" | "install_failed";
  text: string;
}

export function providerProblems(rows: ProviderInstalled[] | null, installs: Record<string, Install>): ProviderProblem[] {
  const all = rows ?? [];
  const named = (provider: string) => factsOf(all.find((r) => r.provider === provider) ?? { provider, name: provider }).name;
  const out: ProviderProblem[] = all.filter((r) => connected(r) && r.signed_in === false).map((r) => ({ provider: r.provider, kind: "signed_out", text: `${named(r.provider)} signed out` }));
  for (const [provider, i] of Object.entries(installs)) {
    if (i.status === "failed") out.push({ provider, kind: "install_failed", text: `${named(provider)} install failed` });
  }
  return out;
}

/* How the person sees a profile's provider in a sentence ("Codex needs a sign-in"). */
export const PROVIDER_LABEL: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  antigravity: "Gemini (Antigravity)",
  opencode: "OpenCode",
  native: "Local (native)",
  shell: "Shell",
  subagent: "Subagent",
};
export const providerLabel = (provider: string): string => PROVIDER_LABEL[provider] ?? provider;

/* "A", "A and B", "A, B and C". */
export function names(list: string[]): string {
  if (list.length <= 1) return list[0] ?? "";
  return `${list.slice(0, -1).join(", ")} and ${list.at(-1)}`;
}
