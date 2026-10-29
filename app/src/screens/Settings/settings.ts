/* What the Settings screen works on, apart from React: the repo settings
   that have a UI (config.RepoConfig's keys), as a draft, and the PATCH body
   for what changed. The engine writes them to the person's
   ~/.brindle/config.json or the repo's gitignored .brindle/config.local.json
   (routes_actions.patch_config) and reads them back merged
   (config.load_repo_config): the command lists (checks) from the repo's
   files only, shared then local; every other key from local over the
   shared file over the person's own over the default. A value is stored
   as given, so the draft never sends a null for a setting the engine
   needs a number for: the field is required instead. Below that, what the
   doctor's lines mean for the screen: each provider's sign-in as a row,
   and which lines the app can fix itself. */

import { LOGIN_PROVIDERS, type DoctorCheck, type LoginProvider } from "../../api/client";

/* The settings with a UI, in the order shown, each with what it means. */
export interface Setting {
  key: string;
  label: string;
  blurb: string;
  /* A number needs a value; a boolean is on or off; a choice may be the
     engine's own null when it lists it; a list is one entry per line. */
  kind: "number" | "boolean" | "choice" | "list" | "text";
  choices?: { value: string | null; label: string }[];
  /* The engine's default, for a field the config leaves unset. */
  fallback: unknown;
  /* Needs brindle Pro (the engine ignores it otherwise). */
  pro?: boolean;
  /* Read from the repo's files only (load_repo_config): the person's settings never apply. */
  repoOnly?: boolean;
  /* One of the handful people change most: shown first; the rest behind More. */
  common?: boolean;
  /* The unit a number is in, after the field. */
  unit?: string;
}

export const SETTINGS: Setting[] = [
  { key: "checks", label: "Checks", kind: "list", fallback: [], repoOnly: true, common: true, blurb: "Commands a branch must pass before it merges, one per line, run from the repo root." },
  { key: "setup", label: "Setup", kind: "list", fallback: [], repoOnly: true, blurb: "Commands run in a new worktree before a worker starts, one per line." },
  { key: "base_branch", label: "PR branch", kind: "text", fallback: null, repoOnly: true, blurb: "The branch pull requests target. Empty: the repo's default branch." },
  { key: "rules", label: "Project rules", kind: "list", fallback: [], repoOnly: true, blurb: "Rules every worker in this repo follows, one per line." },
  { key: "max_agents", label: "Workers at once", kind: "number", fallback: 5, common: true, blurb: "How many workers a session runs at the same time. 0 means no cap." },
  { key: "plan_first", label: "Plan first", kind: "boolean", fallback: false, common: true, blurb: "Workers propose a plan and wait for your approval before they edit anything." },
  {
    key: "overlap",
    label: "Overlapping tasks",
    kind: "choice",
    fallback: null,
    blurb: "What happens to a task whose files overlap one already running.",
    choices: [
      { value: null, label: "Default: block within a session, queue behind another session's" },
      { value: "block", label: "Block it" },
      { value: "warn", label: "Warn and start it" },
      { value: "queue", label: "Queue it until the other merges" },
    ],
  },
  { key: "stale_after", label: "Close idle workers after", kind: "number", fallback: 30, unit: "minutes", blurb: "A worker that has reported and sits idle this long is closed. 0 keeps it open." },
  { key: "coordinator", label: "Coordinator", kind: "boolean", fallback: false, pro: true, blurb: "A coordinator above this repo's sessions holds and orders queued work." },
  {
    key: "sidebar",
    label: "Sidebar",
    kind: "choice",
    fallback: "left",
    blurb: "Where the terminal dashboard sits beside the chat.",
    choices: [
      { value: "left", label: "Left of the chat" },
      { value: "bottom", label: "Below the chat" },
    ],
  },
];

export type Draft = Record<string, unknown>;

/* The draft from the repo's effective config: just the keys with a UI, each
   the engine's default where the config has none. */
export function draftOf(repo: Record<string, unknown>): Draft {
  return Object.fromEntries(SETTINGS.map((s) => [s.key, repo[s.key] ?? s.fallback]));
}

const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/* The keys whose draft differs from what was loaded. */
export const changed = (loaded: Draft, draft: Draft): string[] => SETTINGS.map((s) => s.key).filter((k) => !same(loaded[k], draft[k]));

/* The changed keys the draft can't be saved with: a number left empty. */
export const invalid = (draft: Draft): string[] =>
  SETTINGS.filter((s) => s.kind === "number" && (typeof draft[s.key] !== "number" || Number.isNaN(draft[s.key]))).map((s) => s.key);

/* A draft over a freshly loaded one: what was loaded, but every field with an unsaved edit keeps it. */
export const carryEdits = (oldLoaded: Draft, oldDraft: Draft, next: Draft): Draft =>
  ({ ...next, ...Object.fromEntries(changed(oldLoaded, oldDraft).map((k) => [k, oldDraft[k]])) });

/* The settings the person's own file can carry (everything but the repo-only ones), and the ones only a repo's files do. */
export const YOURS = SETTINGS.filter((s) => !s.repoOnly);
export const PROJECT_RULES = SETTINGS.filter((s) => s.repoOnly);

/* A value as the status line reads it ("Off", "5 minutes"). */
export function overrideText(s: Setting, value: unknown): string {
  if (s.kind === "boolean") return value === true ? "On" : "Off";
  if (s.kind === "choice") return s.choices?.find((c) => c.value === (value ?? null))?.label ?? String(value);
  return value === undefined || value === null ? "Default" : `${String(value)}${s.unit ? ` ${s.unit}` : ""}`;
}

/* Which layer decides a setting in a repo (GET /config's `overrides`: key -> the file that sets it). */
export type Scope = "team" | "local" | "yours";
export const scopeOf = (overrides: Record<string, "local" | "repo"> | undefined, key: string): Scope =>
  overrides?.[key] === "repo" ? "team" : overrides?.[key] === "local" ? "local" : "yours";

/* The one plain line under a repo-view row; yours falls back to the engine's default. */
export const statusLine = (s: Setting, scope: Scope, yourValue: unknown): string =>
  scope === "team" ? "Set by the team" : scope === "local" ? "Just for you" : `Same as yours (${overrideText(s, yourValue ?? s.fallback)})`;

/* PATCH /config's `values`: only what changed, as the engine takes it (a
   list setting's lines, a number, a bool, a choice; null only where the
   engine's own choice is null). */
export function values(loaded: Draft, draft: Draft): Record<string, unknown> {
  return Object.fromEntries(changed(loaded, draft).map((k) => [k, draft[k]]));
}

/* A list setting's lines from its text, as the engine keeps them. */
export const listText = (value: unknown): string => (Array.isArray(value) ? value.map(String).join("\n") : "");
export const listOf = (text: string): string[] =>
  text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

/* doctor's levels, as marks. */
export const MARK: Record<string, string> = { ok: "✓", warn: "!", fail: "✗" };

/* doctor.is_optional: a warning about something only some setups use. */
export const isOptional = (c: DoctorCheck): boolean => c.level === "warn" && c.detail.includes("only needed");

/* The doctor's line about a newer brindle on PyPI (updates.notice), if any. */
export const updateNotice = (checks: DoctorCheck[]): string | null => checks.find((c) => c.name === "brindle update")?.detail ?? null;

/* The settings keys the person has set for every repo, read as key: value lines. */
export function userLines(user: Record<string, unknown>): [string, string][] {
  return Object.entries(user)
    .filter(([k]) => !k.startsWith("_"))
    .map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)]);
}

/* Each provider CLI as the doctor names it: its binary's line (doctor._tool)
   and the name its sign-in line starts with (doctor.CLI_INSTALL's label;
   older engines and the fixtures use the provider's own name). */
const CLI: Record<LoginProvider, { label: string; binary: string }> = {
  claude: { label: "Claude Code", binary: "claude" },
  codex: { label: "Codex", binary: "codex" },
  antigravity: { label: "Google Antigravity", binary: "agy" },
  opencode: { label: "OpenCode", binary: "opencode" },
};

/* The provider a doctor sign-in line ("Claude Code sign-in", "claude sign-in") is about, or null. */
export function signInProvider(c: DoctorCheck): LoginProvider | null {
  const m = /^(.+) sign-in$/.exec(c.name);
  if (!m) return null;
  const who = m[1].toLowerCase();
  return LOGIN_PROVIDERS.find((p) => p === who || CLI[p].label.toLowerCase() === who || CLI[p].binary === who) ?? null;
}

/* What the app can do about a doctor line itself: the engine's update it
   can run (a sign-in has its own row and button). Install pointers stay
   words: the app has no route to install a tool. */
export type Fix = { kind: "update" };

export function fixFor(c: DoctorCheck): Fix | null {
  return c.name === "brindle update" ? { kind: "update" } : null;
}

/* The doctor's sign-in lines, kept out of the health list: Settings › Providers says that. */
export const inSignInRows = (c: DoctorCheck): boolean => signInProvider(c) !== null;
