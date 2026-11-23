/* What the Agents page works on, apart from React (A1, "separate layers"):
   who does a task (agents: how to work, no model) and what model runs each
   job size (one shared table). Everything comes from GET /v1/routing: every
   profile with its kind (model, agent or pinned), model, provider,
   availability, cost and automatic check (lint_errors); per size the
   owner's pick, its ordered fallbacks, the lock and what hosted learning
   runs instead; and per agent its description, rules and the sizes where it
   keeps a model of its own (`agents[name].models`).

   In the person's words, a built-in model profile is a model ("Sonnet 5.5
   on Claude Code"); an agent is named. The word "profile" isn't shown. */

import type { RoutingDTO, RoutingKindDTO, RoutingModelOrderDTO, RoutingProfileDTO } from "../../api/schema";
import type { KindExtras, WorksLike } from "../../api/client";
import { FACTS } from "../../api/providers";

export const SIZES = ["light", "medium", "heavy", "advanced"] as const;
export type Size = (typeof SIZES)[number];
export const KINDS = [...SIZES, "reviewer", "supervisor"] as const;
export type Kind = (typeof KINDS)[number];
export const isSize = (k: Kind): boolean => (SIZES as readonly string[]).includes(k);

export const KIND_INFO: Record<Kind, { name: string; blurb: string }> = {
  light: { name: "Light", blurb: "Docs, renames, simple tests" },
  medium: { name: "Medium", blurb: "Features and bugfixes" },
  heavy: { name: "Heavy", blurb: "Architecture, broad refactors" },
  advanced: { name: "Advanced", blurb: "Long-running design and build" },
  reviewer: { name: "Reviewer", blurb: "Reviews every branch before it merges. Never edits." },
  supervisor: { name: "Supervisor", blurb: "Plans the work, sizes each task, hands it out, merges." },
};

export const EFFORT = ["low", "medium", "high"] as const;

export type Cost = "low" | "medium" | "high";
export const COST_MARK: Record<string, string> = { low: "$", medium: "$$", high: "$$$", unknown: "?" };
export const COST_WORDS: Record<string, string> = { low: "low cost", medium: "medium cost", high: "high cost", unknown: "unknown cost" };

/* The providers in the order lists show them, by their product names. */
export const PROVIDER_ORDER = ["claude", "codex", "opencode", "antigravity", "native"];
const OWN_NAMES: Record<string, string> = { native: "Local models" };
export const productName = (provider: string): string => FACTS[provider]?.name ?? OWN_NAMES[provider] ?? provider;

/* Never offered for a kind of work: the engine's own helpers (by the role the engine gives each profile). */
const HELPERS = new Set(["helper", "supervisor"]);
const HELPER_PROVIDERS = new Set(["shell", "subagent"]);

/* The engine says which profiles are its own (built-in); any other is one of the person's custom agents. */
export const isCustom = (p: Pick<RoutingProfileDTO, "builtin">): boolean => !p.builtin;

/* A model as the engine names it to a person ("Claude Opus"); its id only when an older engine sends no name. */
export const displayOf = (p: Pick<RoutingProfileDTO, "display" | "model" | "name">): string => p.display ?? p.model ?? p.name;

/* Who made the model and the tool that runs it: "Anthropic · runs in Claude Code". */
export function madeBy(p: Pick<RoutingProfileDTO, "maker" | "tool">): string {
  return [p.maker, p.tool && `runs in ${p.tool}`].filter(Boolean).join(" · ");
}

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

/* What one entry of a kind's list is, and whether it can run here now. */
export interface Choice {
  /* What the engine is given: a profile name, or (the supervisor's) a provider. */
  id: string;
  /* How it reads: the model's display name, or the custom agent's name. */
  label: string;
  custom: boolean;
  provider: string;
  model: string | null;
  /* The model as the engine names it ("Claude Opus"), and "<maker> · runs in <tool>". */
  display: string;
  madeBy: string;
  effort: string | null;
  cost: string | null;
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
    label: isCustom(p) ? p.name : displayOf(p),
    custom: isCustom(p),
    provider: p.provider,
    model: p.model ?? null,
    display: displayOf(p),
    madeBy: madeBy(p),
    effort: p.effort ?? null,
    cost: p.cost_tier,
    good: describe[p.name] ?? "",
    ok: blocker === null,
    blocker,
    why: whyOf(p, blocker, connected),
    problems: p.lint_errors ?? [],
  };
}

/* A name in a list that no profile has any more. */
export const missingChoice = (id: string): Choice => ({ id, label: id, custom: true, provider: "", model: null, display: id, madeBy: "", effort: null, cost: null, good: "", ok: false, blocker: null, why: "no such agent here", problems: [] });

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
   model), and the person's custom agents that pin a model for any. An agent
   (how to work, no model of its own) is never a model to pick. One the org
   refuses never shows. */
export function offeredFor(kind: Kind, profiles: RoutingProfileDTO[]): RoutingProfileDTO[] {
  const role = kind === "reviewer" ? "reviewer" : "developer";
  return profiles.filter((p) => {
    if (p.kind === "agent" || HELPERS.has(p.role ?? "") || HELPER_PROVIDERS.has(p.provider) || p.allowed === false) return false;
    return isCustom(p) || p.role === role;
  });
}

export function move<T>(list: T[], from: number, to: number): T[] {
  if (to < 0 || to >= list.length || from === to) return list;
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

/* ---- Who does the work: the agents. ---- */

/* How an agent's model is chosen: by the job size, by the job size except
   some sizes where it keeps its own, or its own (every size, or a custom
   profile that pins a model). */
export type ModelMode = "follows" | "except" | "own";

export interface AgentRow {
  name: string;
  /* As the person reads it: "Backend auditor" for a built-in, its own name for theirs. */
  label: string;
  custom: boolean;
  description: string;
  rules: string[];
  role: string | null;
  /* An agent follows the job size where it has no own model; a pinned one runs its own profile's model. */
  pinned: boolean;
  /* The sizes where it keeps a model of its own. */
  models: Partial<Record<Size, RoutingModelOrderDTO>>;
  mode: ModelMode;
}

/* The built-ins in the order the page lists them; the person's own come
   after them, and the supervisor, which runs the session itself, last. */
const BUILTIN_ORDER = ["developer", "qa", "reviewer", "backend-auditor"];
const LAST = "supervisor";

/* A built-in agent's name in words: "backend-auditor" reads "Backend auditor", "qa" reads "QA". */
export function agentLabel(name: string): string {
  if (name === "qa") return "QA";
  const words = name.replace(/[-_.]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function modeOf(models: Partial<Record<Size, RoutingModelOrderDTO>>, pinned: boolean): ModelMode {
  if (pinned) return "own";
  const own = SIZES.filter((s) => models[s]?.model);
  return own.length === 0 ? "follows" : own.length === SIZES.length ? "own" : "except";
}

/* Every agent GET /routing lists, and every custom profile of the
   person's that pins a model (it still runs as itself), in the page's order. */
export function agentRows(routing: RoutingDTO): AgentRow[] {
  const rows: AgentRow[] = [];
  for (const [name, a] of Object.entries(routing.agents ?? {})) {
    const models: Partial<Record<Size, RoutingModelOrderDTO>> = {};
    for (const s of SIZES) if (a.models?.[s]?.model) models[s] = a.models[s];
    rows.push({ name, label: a.builtin ? agentLabel(name) : name, custom: !a.builtin, description: a.description, rules: a.rules ?? [], role: a.role, pinned: false, models, mode: modeOf(models, false) });
  }
  for (const p of routing.profiles) {
    if (p.builtin || p.kind === "agent" || p.kind === "model" || rows.some((r) => r.name === p.name)) continue;
    rows.push({ name: p.name, label: p.name, custom: true, description: p.description ?? "", rules: p.rules ?? [], role: p.role ?? null, pinned: true, models: {}, mode: "own" });
  }
  const rank = (r: AgentRow) => (r.name === LAST ? 3 : r.custom ? 2 : BUILTIN_ORDER.includes(r.name) ? 0 : 1);
  const at = (r: AgentRow) => BUILTIN_ORDER.indexOf(r.name);
  return rows.sort((a, b) => rank(a) - rank(b) || at(a) - at(b) || a.name.localeCompare(b.name));
}

/* ---- An agent being added or edited: the form's answers. ---- */
export interface Draft {
  name: string;
  purpose: string;
  instructions: string;
  rules: string[];
  worksLike: WorksLike;
  /* Model: follows the job size, or the sizes given here run their own. */
  own: Partial<Record<Size, string>>;
}

export const newDraft = (): Draft => ({ name: "", purpose: "", instructions: "", rules: [], worksLike: "developer", own: {} });

export const WORKS_LIKE: WorksLike[] = ["developer", "qa", "reviewer"];

/* The agent's name as it is saved (its file's name). */
export const cleanName = (name: string): string => name.trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");

/* What stops Create, or null. */
export function draftProblem(d: Draft, taken: string[]): string | null {
  const name = cleanName(d.name);
  if (!name) return "Give it a name.";
  if (!d.purpose.trim()) return "Say what it's for.";
  if (taken.includes(name)) return `There's already an agent called ${name}.`;
  return null;
}
