/* What the permission policy (GET /permissions, GET /permissions/asks) means
   for the screens, apart from React: each rule, saved action and ask in
   words, the scope ladder a saved action can be moved along (as
   permission_grants.scopes_for builds it), whether a scope is within an ask
   rule's limits (permission_grants.limits_allow), which waiting asks a
   scope covers (scope_matches), and whether two rules may disagree on the
   same requests (the editor's check before a save). */

import type { PermissionLevel } from "./client";
import type { AskDTO, AskGroupDTO, AskScopeDTO, PermissionLimitsDTO, PermissionRuleDTO, SavedActionDTO } from "./schema";

export const PUSH_ARROW = " → ";
export const PUSH_AT = " @ ";
export const LEVELS: PermissionLevel[] = ["branch", "destination", "remote", "repository", "all"];
export const PLAIN_MATCHES = ["exact", "prefix", "glob"];

const KIND_LABEL: Record<string, string> = {
  read: "Read files",
  write: "Write files",
  edit: "Edit files",
  bash: "Run commands",
  fetch: "Fetch web content",
  mcp: "MCP tools",
  other: "Other tools",
  push: "Push commits",
};
export const kindLabel = (kind: string) => KIND_LABEL[kind] ?? kind;
/* A rule's action as people read it: a push rule pushes commits. */
export const ruleAction = (r: Pick<PermissionRuleDTO, "kind" | "match_type">) => (r.match_type === "push" ? KIND_LABEL.push : kindLabel(r.kind));

export const DECISIONS = [
  { value: "ask", label: "Ask me" },
  { value: "allow", label: "Allow automatically" },
  { value: "deny", label: "Deny" },
] as const;

export const UNMATCHED = [
  { value: "ask", label: "Ask me" },
  { value: "allow", label: "Allow and record in Activity" },
  { value: "deny", label: "Deny until a rule is added" },
] as const;

export const LEVEL_LABEL: Record<string, string> = {
  branch: "This source and destination only",
  destination: "Into this destination from any branch",
  remote: "All branches on this remote",
  repository: "All pushes in this repository",
  all: "Across repositories",
};

export const presetLabel = (name: string | null | undefined) => (name ? name[0].toUpperCase() + name.slice(1) : "Custom");

/* 30 → "30 minutes", 120 → "2 hours", 1440 → "1 day". */
export function minutesText(minutes: number): string {
  const [n, unit] = minutes % 1440 === 0 ? [minutes / 1440, "day"] : minutes % 60 === 0 ? [minutes / 60, "hour"] : [minutes, "minute"];
  return `${n} ${unit}${n === 1 ? "" : "s"}`;
}

export const UNITS = [
  { value: "minutes", label: "minutes", factor: 1 },
  { value: "hours", label: "hours", factor: 60 },
  { value: "days", label: "days", factor: 1440 },
] as const;
export type Unit = (typeof UNITS)[number]["value"];
/* A number of minutes as the largest unit it divides into. */
export function splitMinutes(minutes: number): { value: number; unit: Unit } {
  if (minutes % 1440 === 0) return { value: minutes / 1440, unit: "days" };
  if (minutes % 60 === 0) return { value: minutes / 60, unit: "hours" };
  return { value: minutes, unit: "minutes" };
}
export const toMinutes = (value: number, unit: Unit) => value * (UNITS.find((u) => u.value === unit)?.factor ?? 1);

export const when = (epochSeconds: number) => new Date(epochSeconds * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

/* The pill on a rule's row. */
export function ruleSummary(r: Pick<PermissionRuleDTO, "decision">): string {
  return r.decision === "deny" ? "Blocked" : r.decision === "allow" ? "Allowed automatically" : "Ask for approval";
}

/* How far and how long an ask rule's approvals may reach, in words. */
export function limitsText(limits: PermissionLimitsDTO | null | undefined): string {
  // "once": no saved approval settles it (pushes into a release branch); each request is decided on its own.
  if (limits?.life === "once") return "Approvals are never saved";
  const life = limits?.life === "time" && limits.minutes ? `up to ${minutesText(limits.minutes)}` : limits?.life === "session" ? "for the session" : "until revoked";
  const scope = limits?.max_scope;
  const reach = !scope || scope === "*" || scope === "**" ? "" : LEVELS.includes(scope as PermissionLevel) ? ` · ${LEVEL_LABEL[scope]}` : ` · within ${scope}`;
  return `Approvals last ${life}${reach}`;
}

/* The line under a rule's pill. */
export function ruleLimits(r: PermissionRuleDTO): string {
  if (r.expired) return "Expired: no longer applies";
  if (r.decision === "deny") return "No approvals";
  if (r.decision === "allow") return r.expires ? `Until ${when(r.expires)}` : "Until this rule changes";
  return limitsText(r.limits);
}

/* How long a saved action lasts, as its row says it. */
export function savedLasts(s: SavedActionDTO): string {
  if (s.expired) return "Expired";
  if (s.life === "session") return "This session";
  if (s.life === "time") return `${s.minutes ? minutesText(s.minutes) : "Timed"}${s.expires ? ` · until ${when(s.expires)}` : ""}`;
  return "Until deleted";
}

/* -- matching ------------------------------------------------------------------------------- */

const WILD = /[*?[\]]/;
const escape = (s: string) => s.replace(/[.+^${}()|\\]/g, "\\$&");
/* fnmatch's * and ? (a [...] class as written). */
export const globMatch = (pattern: string, text: string) => new RegExp(`^${pattern.split(/(\*+|\?)/).map((part) => (part.startsWith("*") ? ".*" : part === "?" ? "." : escape(part))).join("")}$`).test(text);

/* permission_grants.scope_matches: whether a saved pattern covers an ask key. */
export function scopeMatches(kind: string, pattern: string, key: string): boolean {
  if (pattern === "*" || pattern === "**") return true;
  if (kind === "bash" && pattern.endsWith(" *")) {
    const head = pattern.slice(0, -2).split(/\s+/);
    return key.split(/\s+/).slice(0, head.length).join(" ") === head.join(" ");
  }
  if (kind === "push") return key.split(", ").every((k) => globMatch(pattern, k));
  return key === pattern || globMatch(pattern, key);
}

/* permission_grants.limits_allow's scope part: whether a grant of `scope` stays within the ask rule's limits. */
export function scopeWithin(scope: AskScopeDTO, limits: PermissionLimitsDTO | null | undefined): boolean {
  const max = limits?.max_scope;
  if (!max) return true;
  if (scope.level) return !LEVELS.includes(max as PermissionLevel) || LEVELS.indexOf(scope.level as PermissionLevel) <= LEVELS.indexOf(max as PermissionLevel);
  return max === "*" || max === "**" || scope.pattern.startsWith(max.replace(/\*+$/, ""));
}

/* Every waiting ask of the item's repo and kind a grant of `scope` covers. */
export function covered(groups: AskGroupDTO[], g: AskGroupDTO, scope: AskScopeDTO): AskDTO[] {
  return groups.filter((x) => x.repo === g.repo && x.kind === g.kind).flatMap((x) => x.items.filter((a) => scopeMatches(g.kind, scope.pattern, a.exact)));
}

/* -- the scope ladder of a saved action ------------------------------------------------------ */

const rung = (value: string, pattern: string, label: string, level: string | null = null): AskScopeDTO => ({ value, pattern, label, level });

/* permission_grants.scopes_for: the ladder for a request of `kind` with ask key `key`, narrowest first. */
export function scopesFor(kind: string, key: string): AskScopeDTO[] {
  if (kind === "push") {
    const [refs, remote = "*"] = key.split(PUSH_AT);
    const [src, dst = "*"] = refs.split(PUSH_ARROW);
    return [
      rung("exact", key, `This push only · ${src} into ${dst} on ${remote}`, "branch"),
      rung("general", `*${PUSH_ARROW}${dst}${PUSH_AT}${remote}`, `Into ${dst} from any branch on ${remote}`, "destination"),
      rung("remote", `*${PUSH_ARROW}*${PUSH_AT}${remote}`, `All branches on ${remote}`, "remote"),
      rung("repository", `*${PUSH_ARROW}*${PUSH_AT}*`, "All pushes in this repository", "repository"),
      rung("all", `*${PUSH_ARROW}*${PUSH_AT}*`, "All pushes in every repository", "all"),
    ];
  }
  if (kind === "read" || kind === "write" || kind === "edit") {
    const top = key.includes("/") ? key.split("/")[0] : "";
    return [
      rung("exact", key, `This file only · ${key}`),
      top && !WILD.test(top) ? rung("general", `${top}/**`, `Everything under ${top}/**`) : rung("general", key, `This file only · ${key}`),
      rung("repository", "**", "Any file in the repository"),
    ];
  }
  if (kind === "bash") {
    const family = key.split(/\s+/).slice(0, 2).join(" ");
    return [rung("exact", key, `This command only · ${key}`), rung("general", `${family} *`, `Commands starting with ${family}`), rung("repository", "*", "Any command in the repository")];
  }
  if (kind === "mcp") {
    const server = key.startsWith("mcp__") ? key.split("__")[1] : "";
    return [rung("general", key, `This tool · ${key}`), ...(server ? [rung("server", `mcp__${server}__*`, `Every ${server} tool`)] : []), rung("all", "mcp__*", "Every MCP tool")];
  }
  if (kind === "fetch") {
    const host = /^https?:\/\/[^/]+/.exec(key)?.[0];
    return [rung("exact", key, `This URL only · ${key}`), host ? rung("general", `${host}/*`, `Everything on ${host}`) : rung("general", key, `This URL only · ${key}`), rung("all", "*", "Any URL")];
  }
  return [rung("exact", key, `This only · ${key}`), rung("all", "*", `Every ${kind} request`)];
}

/* The rungs a saved action can move to: its ladder from its pattern (the
   exact rung only when the pattern is an exact one), with its own scope
   first when the ladder doesn't hold it. */
export function savedScopes(s: SavedActionDTO): AskScopeDTO[] {
  const seen = new Set<string>();
  const out = scopesFor(s.kind, s.pattern)
    .filter((r) => s.scope === "exact" || r.value !== "exact")
    .filter((r) => !seen.has(r.value) && (seen.add(r.value), true));
  return out.some((r) => r.value === s.scope && r.pattern === s.pattern) ? out : [rung(s.scope, s.pattern, `Current · ${s.pattern}`, s.level ?? null), ...out.filter((r) => r.value !== s.scope)];
}

/* A specific target typed in place of a rung: it must sit inside the pattern the action has now. */
export function specificTarget(s: SavedActionDTO, text: string): AskScopeDTO | string {
  const t = text.trim();
  if (!t) return "Enter a target.";
  if (s.kind === "fetch" && !/^https?:\/\//.test(t)) return "Enter a URL starting with http:// or https://.";
  if (t !== s.pattern && !scopeMatches(s.kind, s.pattern, t.replace(/\*+$/, "x"))) return `Enter a target inside ${s.pattern}.`;
  return rung("exact", t, `Specific target · ${t}`);
}

/* -- conflicts ------------------------------------------------------------------------------ */

export type RuleShape = Pick<PermissionRuleDTO, "kind" | "match" | "match_type" | "decision"> & { id?: string };

/* Whether two rules cover some of the same requests: true, false, or null
   when the patterns can't be proven to overlap or to be apart. A built-in
   matcher (tracked, git-force, push ...) is compared only with the same
   matcher: what it covers is the engine's, not a pattern to compare. */
export function overlap(a: RuleShape, b: RuleShape): boolean | null {
  if (a.kind !== b.kind) return false;
  const plainA = PLAIN_MATCHES.includes(a.match_type);
  const plainB = PLAIN_MATCHES.includes(b.match_type);
  if (!plainA || !plainB) return a.match_type !== b.match_type ? false : a.match === b.match ? true : null;
  const any = (r: RuleShape) => r.match_type === "glob" && /^\*+$/.test(r.match);
  if (any(a) || any(b)) return true;
  if (a.match_type === "exact" && b.match_type === "exact") return a.match === b.match;
  const [exact, other] = a.match_type === "exact" ? [a, b] : b.match_type === "exact" ? [b, a] : [null, null];
  if (exact && other) return other.match_type === "prefix" ? exact.match.startsWith(other.match) : globMatch(other.match, exact.match);
  const lit = (r: RuleShape) => (r.match_type === "prefix" ? r.match : r.match.split(WILD)[0]);
  const pure = (r: RuleShape) => r.match_type === "prefix" || /^[^*?[\]]*\*+$/.test(r.match);
  const [la, lb] = [lit(a), lit(b)];
  if (!la.startsWith(lb) && !lb.startsWith(la)) return false;
  return pure(a) && pure(b) ? true : null;
}

export interface Conflict {
  rule: PermissionRuleDTO;
  /* null: the patterns may overlap; true: they do. */
  certain: boolean;
}

/* The live rules that decide differently on requests `draft` covers too. */
export function conflictsFor(draft: RuleShape, rules: PermissionRuleDTO[]): Conflict[] {
  return rules
    .filter((r) => r.id !== draft.id && !r.expired && r.decision !== draft.decision)
    .map((r) => ({ rule: r, o: overlap(draft, r) }))
    .filter((x) => x.o !== false)
    .map((x) => ({ rule: x.rule, certain: x.o === true }));
}

export const strength = (decision: string) => (decision === "deny" ? 3 : decision === "ask" ? 2 : 1);
