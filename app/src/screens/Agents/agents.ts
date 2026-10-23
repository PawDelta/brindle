/* What the Agents tab works on, apart from React: the kinds of work, what
   each one runs and what takes over at a limit, the choices a row's list
   offers, and the custom agent being made. Everything comes from GET
   /v1/routing (engine branch feat/routing-learned-lock): every profile with
   its model, provider, availability, cost and automatic check (lint_errors),
   and per kind of work the owner's pick, its ordered fallbacks, whether it is
   locked, and the different profile hosted learning runs for it now.

   In the person's words, a built-in profile is a model ("Sonnet 5.5 on
   Claude Code"); one of their own is a custom agent, named. The word
   "profile" isn't shown. */

import type { RoutingDTO, RoutingKindDTO, RoutingProfileDTO } from "../../api/schema";
import type { KindExtras } from "../../api/client";
import { FACTS } from "../../api/providers";

export const SIZES = ["light", "medium", "heavy", "advanced"] as const;
export const KINDS = [...SIZES, "reviewer", "supervisor"] as const;
export type Kind = (typeof KINDS)[number];
export const isSize = (k: Kind): boolean => (SIZES as readonly string[]).includes(k);

export const KIND_INFO: Record<Kind, { name: string; blurb: string }> = {
  light: { name: "Light", blurb: "Small, well-specified, mechanical work: docs, renames, simple tests." },
  medium: { name: "Medium", blurb: "Normal feature work and bugfixes." },
  heavy: { name: "Heavy", blurb: "System architecture and broad refactoring." },
  advanced: { name: "Advanced", blurb: "Complex, long-running design and implementation." },
  reviewer: { name: "Reviewer", blurb: "Reviews every branch before it merges. Never edits." },
  supervisor: { name: "Supervisor", blurb: "Plans the work, sizes each task, hands it out, merges." },
};

/* How a task is run, in four steps. */
export const FLOW: [string, string][] = [
  ["A task comes in", "From you, or from a supervisor splitting up bigger work."],
  ["It's sized", "The supervisor calls it Light, Medium, Heavy or Advanced. Automatic: you don't size tasks."],
  ["That size's agent runs it", "The model or custom agent you chose for that size."],
  ["At a limit, the next one", "A usage limit, or a provider that isn't connected: the next fallback takes over. None left: the task waits and you're told."],
];

export const EFFORT = ["low", "medium", "high"] as const;

export type Cost = "low" | "medium" | "high";
export const COST_MARK: Record<string, string> = { low: "$", medium: "$$", high: "$$$" };
export const COST_WORDS: Record<string, string> = { low: "low cost", medium: "medium cost", high: "high cost" };

/* The providers in the order lists show them, by their product names. */
export const PROVIDER_ORDER = ["claude", "codex", "opencode", "antigravity", "native"];
const OWN_NAMES: Record<string, string> = { native: "Local models" };
export const productName = (provider: string): string => FACTS[provider]?.name ?? OWN_NAMES[provider] ?? provider;

/* The built-in profiles (the engine's brindle/builtin_agents): any other is one of the person's custom agents. */
export const BUILT_IN = new Set([
  "backend-auditor", "coordinator", "developer", "developer-codex", "developer-fable", "developer-gemini", "developer-heavy", "developer-light",
  "developer-local", "developer-opencode", "qa", "reviewer", "reviewer-codex", "reviewer-gemini", "reviewer-local", "reviewer-opencode", "subagent", "supervisor",
]);
/* Never offered for a kind of work: the engine's own helpers. */
const HELPERS = new Set(["coordinator", "subagent", "supervisor"]);
const HELPER_PROVIDERS = new Set(["shell", "subagent"]);

export const isCustom = (p: Pick<RoutingProfileDTO, "name">): boolean => !BUILT_IN.has(p.name);

/* A provider is connected when one of its profiles can run here: installed and signed in. */
export function connectedProviders(profiles: RoutingProfileDTO[]): Set<string> {
  return new Set(profiles.filter((p) => p.installed && p.signed_in).map((p) => p.provider));
}

/* Every provider the profiles run on, connected or not, in the lists' order. */
export function providersOf(profiles: RoutingProfileDTO[]): string[] {
  const seen = [...new Set(profiles.filter((p) => !HELPER_PROVIDERS.has(p.provider)).map((p) => p.provider))];
  const at = (p: string) => (PROVIDER_ORDER.indexOf(p) + 1 || 99);
  return seen.sort((a, b) => at(a) - at(b) || a.localeCompare(b));
}

/* How fast a model is, as its cost and build say it: no engine field yet. */
export function speedOf(p: Pick<RoutingProfileDTO, "name" | "cost_tier">): string {
  if (/-(fable|advanced|reasoning)/.test(p.name)) return "slowest";
  return p.cost_tier === "low" ? "fast" : p.cost_tier === "high" ? "slower" : "steady";
}

/* A model's name as the person reads it: "sonnet" → "Sonnet", "gpt-5.5" → "GPT-5.5". */
export function prettyModel(model: string | null | undefined): string {
  if (!model) return "the CLI's default model";
  if (/^gpt/i.test(model)) return model.replace(/^gpt/i, "GPT");
  return model
    .split("-")
    .map((w) => (/^\d/.test(w) ? w : w[0].toUpperCase() + w.slice(1)))
    .join(" ");
}

/* What one entry of a kind's list is, and whether it can run here now. */
export interface Choice {
  /* What the engine is given: a profile name, or (the supervisor's) a provider. */
  id: string;
  /* How it reads: the model, or the custom agent's name. */
  label: string;
  custom: boolean;
  provider: string;
  model: string | null;
  cost: string | null;
  speed: string;
  /* What it's good at: the profile's own description. */
  good: string;
  ok: boolean;
  /* What stops it, and why in words, when it can't run. */
  blocker: Blocker | null;
  why: string;
  /* The automatic check's findings (profile lint): routing skips it until they're fixed. */
  problems: string[];
}

/* Why a profile can't run here, from its own flags (each profile carries its
   own sign-in: two on one provider can differ), or null when it can. */
export type Blocker = "problems" | "not_installed" | "signed_out" | "excluded" | "refused";

export function blockerOf(p: RoutingProfileDTO): Blocker | null {
  if ((p.lint_errors ?? []).length > 0) return "problems";
  if (!p.installed) return "not_installed";
  if (!p.signed_in) return "signed_out";
  if (p.excluded) return "excluded";
  if (p.allowed === false) return "refused";
  return null;
}

function whyOf(p: RoutingProfileDTO, blocker: Blocker | null, connected: Set<string>): string {
  const name = productName(p.provider);
  switch (blocker) {
    case "problems":
      return problemCount(p.lint_errors!.length);
    case "not_installed":
      return `${name} isn't connected`;
    case "signed_out":
      // Another of the provider's agents may be signed in under a different identity.
      return connected.has(p.provider) ? `${name} is signed out for it` : `${name} is signed out`;
    case "excluded":
      return `${name} is excluded from routing`;
    case "refused":
      return p.refusal ?? "your org doesn't allow it";
    default:
      return "";
  }
}

export function choiceOf(p: RoutingProfileDTO, connected: Set<string>, describe: Record<string, string> = {}): Choice {
  const blocker = blockerOf(p);
  return {
    id: p.name,
    label: isCustom(p) ? p.name : prettyModel(p.model),
    custom: isCustom(p),
    provider: p.provider,
    model: p.model ?? null,
    cost: p.cost_tier,
    speed: speedOf(p),
    good: describe[p.name] ?? "",
    ok: blocker === null,
    blocker,
    why: whyOf(p, blocker, connected),
    problems: p.lint_errors ?? [],
  };
}

/* A name in a list that no profile has any more. */
export const missingChoice = (id: string): Choice => ({ id, label: id, custom: true, provider: "", model: null, cost: null, speed: "", good: "", ok: false, blocker: null, why: "no such agent here", problems: [] });

export const problemCount = (n: number): string => (n === 1 ? "1 problem" : `${n} problems`);

/* One kind of work as GET /routing gives it, with what the app adds ("More"). */
export interface KindState {
  model: string | null;
  fallbacks: string[];
  locked: boolean;
  learned: string | null;
  effort: string | null;
  instructions: string;
}

/* `kinds` when the engine sends it; from the tiers and the reviewer when it's older. */
export function kindsOf(routing: RoutingDTO): Record<Kind, KindState> {
  const out = {} as Record<Kind, KindState>;
  for (const k of KINDS) {
    const got = (routing.kinds?.[k] ?? {}) as RoutingKindDTO & KindExtras;
    const tier = isSize(k) ? (routing.tiers[k]?.values ?? []) : k === "reviewer" ? routing.reviewer.values : [];
    out[k] = {
      model: got.model ?? tier[0] ?? null,
      fallbacks: got.fallbacks ?? tier.slice(1),
      locked: got.locked ?? false,
      learned: got.locked ? null : (got.learned ?? null),
      effort: got.effort ?? null,
      instructions: got.instructions ?? "",
    };
  }
  return out;
}

/* What a kind's work runs on, in order: what learning picked first when it
   runs something else, then the owner's pick and fallbacks. */
export function runOrder(k: KindState): string[] {
  const own = [k.model, ...k.fallbacks].filter((x): x is string => !!x);
  return k.learned ? [k.learned, ...own.filter((x) => x !== k.learned)] : own;
}

/* The profiles a kind's list offers: the built-in reviewers for the
   reviewer, the built-in developers for a size or the supervisor (one per
   model), and the person's custom agents for any. One the org refuses never shows. */
export function offeredFor(kind: Kind, profiles: RoutingProfileDTO[]): RoutingProfileDTO[] {
  const family = kind === "reviewer" ? "reviewer" : "developer";
  return profiles.filter((p) => {
    if (HELPERS.has(p.name) || HELPER_PROVIDERS.has(p.provider) || p.allowed === false) return false;
    return isCustom(p) || p.name === family || p.name.startsWith(`${family}-`);
  });
}

export function move<T>(list: T[], from: number, to: number): T[] {
  if (to < 0 || to >= list.length || from === to) return list;
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

/* ---- A custom agent being made: the five steps' answers. ---- */
export interface Draft {
  name: string;
  purpose: string;
  provider: string | null;
  model: string | null;
  effort: string;
  instructions: string;
  rules: string[];
  /* The kind of work it runs, or null: any kind, picked from any list. */
  useFor: Kind | null;
}

export const newDraft = (useFor: Kind | null): Draft => ({ name: "", purpose: "", provider: null, model: null, effort: "medium", instructions: "", rules: [], useFor });

export const STEPS: [string, string][] = [
  ["what", "What it's for"],
  ["model", "Provider and model"],
  ["how", "How it works"],
  ["where", "Where it's used"],
  ["check", "Check and create"],
];

/* The custom agent's name as it is saved (its file's name). */
export const cleanName = (name: string): string => name.trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");

/* What stops Create, or null. */
export function draftProblem(d: Draft, taken: string[]): string | null {
  const name = cleanName(d.name);
  if (!name) return "Give it a name.";
  if (!d.provider || !d.model) return "Pick a model.";
  if (taken.includes(name)) return `There's already an agent called ${name}.`;
  return null;
}

/* The models a provider offers the builder: each model its built-in developers run, once. */
export function modelsOf(provider: string, profiles: RoutingProfileDTO[]): RoutingProfileDTO[] {
  const out = new Map<string, RoutingProfileDTO>();
  for (const p of profiles) {
    if (p.provider !== provider || !p.model || !(p.name === "developer" || p.name.startsWith("developer-")) || p.allowed === false) continue;
    if (!out.has(p.model)) out.set(p.model, p);
  }
  return [...out.values()];
}
