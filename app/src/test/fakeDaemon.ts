/* An in-memory brindle daemon for tests: a `fetch` serving the v1 read routes
   and a WebSocket factory serving /v1/events, both behind the same guard as
   brindle/daemon/auth.py (Bearer token on HTTP, token as the first WS frame
   or close 4401), with the events feed's replay rules (events.py Feed.start:
   events above `since`, or "resnapshot" when they can't all be replayed).
   Its panes (agents' windows, action terminals) are shown as the app's tmux
   client shows them, through `ptyViews()`: PtyViews whose views draw a pane's
   screen, then what it prints, and tmux's "Pane is dead" once its command
   has exited. */

import { DaemonClient } from "../api/client";
import type { PermissionEditorRequest, PermissionEditorReview, PermissionEditorRule, PermissionEditorState } from "../api/client";
import type { BusEvent, StreamMessage, WebSocketLike } from "../api/events";
import { WS_UNAUTHORIZED } from "../api/events";
import type { PtyStatus, PtyViewHandlers, PtyViewOpener, PtyViews, PtyViewTarget } from "../api/ptyView";
import type {
  AccountDTO,
  AgentDTO,
  DecisionDTO,
  GoalDTO,
  GrantDTO,
  InboxItemDTO,
  ProfileDTO,
  RoutingDTO,
  RoutingKindDTO,
  RoutingLockDTO,
  RoutingPolicyDTO,
  RoutingProfileDTO,
  RoutingSettingDTO,
  RoutingTierDTO,
  SessionDTO,
  WorkspaceDTO,
} from "../api/schema";
import { LOGIN_PROVIDERS } from "../api/client";
import { hunksOf, parseDiff } from "../screens/Branch/parseDiff";
import type { AccountInfo, Cost, DebugEntry, DebugState, Diff, DoctorCheck, KbHit, KbSource, KeyInfo, KindExtras, Meta, PreflightItem, ProfileDetail, ProviderInstalled, Repo, RoutingScope, RulePackInfo, Usage, UserSettings } from "../api/client";

export const BASE_URL = "http://127.0.0.1:47321";
export const TOKEN = "test-token";

export interface DaemonData {
  meta: Meta;
  repos: Repo[];
  sessions: SessionDTO[];
  workspaces: WorkspaceDTO[];
  inbox: InboxItemDTO[];
  /* GET /grants: the live permission grants (brindle.daemon.grants); POST /grants/{id}/revoke ends one. */
  grants: GrantDTO[];
  goal: GoalDTO | null;
  decisions: DecisionDTO[];
  profiles: ProfileDTO[];
  /* GET /profiles/{name}: what the resolved view adds to a listed profile
     (source, extends, rules, prompt, lint); a profile with no entry gets
     a built-in source, no packs, an empty prompt and a clean lint. */
  profileDetails: Record<string, Partial<Omit<ProfileDetail, keyof ProfileDTO>>>;
  /* GET /kb/search: the indexed chunks; a hit matches when its text or
     heading contains any word of the query (kb.search's OR of terms). */
  kbHits: KbHit[];
  /* GET /account: the DTO, with the entitlement's details when signed in. */
  account: AccountInfo;
  /* POST /account/switch-org: what the account (and the cost entitlement)
     becomes under each org; an org not listed is refused as the engine
     refuses one the person isn't a member of. */
  orgs: Record<string, { account: AccountInfo; costEntitled: boolean }>;
  /* The pane of each agent (by id), as its window shows it; an agent with none has no window to view. */
  terminals: Record<string, Pane>;
  /* Agents closed with `brindle close`: left out of the snapshot (their
     running children stay in it), still served by GET /agents/{id}. */
  closed: AgentDTO[];
  /* Agent id → how many turn snapshots it has (brindle/rewind.py turns);
     POST /agents/{id}/rewind to a turn past these fails in its job. */
  turns: Record<string, number>;
  /* Set: a rewind fails with this message after dismissing the worker (its spawn failed). */
  rewindFails: string | null;
  /* Repo root → GET /profiles?repo= for it (a repo's own profiles); `profiles` otherwise. */
  profilesByRepo: Record<string, ProfileDTO[]>;
  /* Workspace id → its GET /diff answer (an empty diff when absent). */
  diffs: Record<string, Diff>;
  /* Workspace id → path → the file as it stands in the worktree (GET and
     PUT /workspaces/{id}/file; engine branch desktop/review-edit). The blob
     sha the routes answer is fakeBlob(content). */
  files: Record<string, Record<string, string>>;
  /* Provider → what its CLI needs before a launch (GET
     /providers/{p}/preflight; engine branch desktop/provider-preflight).
     A provider with none listed is ready. A start or continue of a session
     on a provider with an item still open is refused with 409 {preflight}. */
  preflight: Record<string, PreflightItem[]>;
  /* GET /providers/installed (engine branch feat/provider-install): each
     provider CLI as brindle detects it. By default Claude Code is found and
     signed in (the app goes straight in); the first launch's tests start
     with none found, or some (freshProviders). An install (POST .../install,
     the maker's own installer) makes a row found where that installer puts
     it, not signed in yet; signInProvider() finishes a provider's own sign-in.
     DELETE /providers/{p} turns a row off (state "off": still found,
     unused); POST /providers/{p}/use-mine or an install turns it back on. */
  providers: ProviderInstalled[];
  /* Every profile as GET /routing describes it (`excluded` is worked out
     from the excluded providers). */
  routingProfiles: RoutingProfile[];
  /* The routing keys of each settings file (config.scope_paths): the
     person's ~/.brindle/config.json, the repo's shared .brindle/config.json
     and its gitignored .brindle/config.local.json. One set for every repo,
     unless the repo has its own below. */
  routingConfig: Record<RoutingScope, RoutingFile>;
  /* Repo root → its own shared and local files (a scope left out is empty). */
  routingByRepo: Record<string, Partial<Record<"repo" | "local", RoutingFile>>>;
  /* The org policy GET /routing answers to (service/routing.py RoutingPolicy):
     none when the person is in no Team or Enterprise org. */
  routingPolicy: RoutingPolicyData | null;
  /* Kind of work → the profile hosted learning would run for it instead of
     the owner's pick (routes_read._routing_kinds' `learned`): reported only
     while the kind isn't locked and differs from the pick. */
  learned: Record<string, string>;
  /* GET /rules (ahead of the engine; profiles.list_rule_packs): every rule
     pack with its one-line description. POST /profiles names only these. */
  rulePacks: RulePackInfo[];
  /* The screen each action terminal starts with (POST /providers/{p}/login,
     /keys/{name}/set, /engine/upgrade), by the action's kind ("login codex",
     "key OPENAI_API_KEY", "upgrade"); a kind with none prints its command. */
  actionScreens: Record<string, string>;
  /* GET /kb/sources: the repo's knowledge base sources. */
  kbSources: KbSource[];
  /* The settings files GET /config reads (config.load_repo_config): the
     person's ~/.brindle/config.json, the repo's shared .brindle/config.json
     and its local config.local.json; and the keys' names. A PATCH writes
     into `user` or `local`. */
  config: { user: Record<string, unknown>; repo: Record<string, unknown>; local: Record<string, unknown>; keys: KeyInfo[] };
  /* GET and PATCH /user-settings (engine branch desktop/user-settings): the
     person's own settings that are not a repo's. A PATCH with a value the
     engine doesn't know is refused (400). */
  userSettings: UserSettings;
  /* GET and PUT /debug (staff-only debug logging): whether the account is
     staff and whether logging is on. A PUT from someone not staff is
     refused (403). POST /debug/log's entries land in `debugLog`. */
  debug: DebugState;
  debugLog: DebugEntry[];
  /* GET /doctor. */
  doctor: DoctorCheck[];
  /* GET /cost: refused (402 not_entitled, as cost.require_entitled does) unless `costEntitled`. */
  cost: Cost;
  costEntitled: boolean;
  /* GET /usage (routes_read.get_usage): every provider, and the near ones
     worst first. By default nothing is known and nothing is near. */
  usage: Usage;
  /* What POST /account/login makes the account once it ends. */
  accountAfterLogin: AccountDTO;
  /* The authorize URL the login job publishes for the app to open. */
  loginUrl: string;
  /* Root agent id → the session's review (daemon/session_review.py): whether
     brindle judges it ready for a PR and why not, its branch and base, and
     its diff against the base as a unified patch (GET /sessions/{root}/diff
     splits it into files and hunks as the engine does; 409 not_ready until
     ready). `pr`: the PR brindle opened itself (the engine's auto-PR,
     branch desktop/auto-pr; stubbed here). A session with no entry reads
     as not ready. readyForPr() flips one and puts session.pr_ready on the bus. */
  sessionReview: Record<string, SessionReviewData>;
  /* Root agent id → path → the file as the session's checkout has it (GET,
     PUT and DELETE /sessions/{root}/file; fakeBlob(content) is its blob). */
  sessionFiles: Record<string, Record<string, string>>;
  /* Root agent id → path → the file as the PR base has it. With an entry
     here the session's diff is made from these against sessionFiles on
     every read, as the engine's follows the branch (an edit from the sheet
     shows in the next read; a new file joins the list, a deleted one
     leaves it); without one, sessionReview's fixed patch is served. */
  sessionBase: Record<string, Record<string, string>>;
}

/* A unified diff of two texts, as git prints it for one file: an LCS of
   their lines, the changes in hunks with three lines of context. */
export function unifiedDiff(path: string, before: string | null, after: string | null): string {
  const split = (t: string | null) => (t === null || t === "" ? [] : (t.endsWith("\n") ? t.slice(0, -1) : t).split("\n"));
  const a = split(before);
  const b = split(after);
  // The LCS table, then the edit script walked back from its end.
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const ops: { tag: " " | "-" | "+"; text: string; i: number; j: number }[] = [];
  for (let i = 0, j = 0; i < n || j < m; ) {
    if (i < n && j < m && a[i] === b[j]) ops.push({ tag: " ", text: a[i], i: i++, j: j++ });
    else if (j < m && (i >= n || lcs[i][j + 1] >= lcs[i + 1][j])) ops.push({ tag: "+", text: b[j], i, j: j++ });
    else ops.push({ tag: "-", text: a[i], i: i++, j });
  }
  const changed = ops.map((o, k) => (o.tag === " " ? -1 : k)).filter((k) => k >= 0);
  if (changed.length === 0) return "";
  const header = [`diff --git a/${path} b/${path}`];
  if (before === null) header.push("new file mode 100644", `index 0000000..${fakeBlob(after ?? "").slice(0, 7)}`, "--- /dev/null", `+++ b/${path}`);
  else if (after === null) header.push("deleted file mode 100644", `index ${fakeBlob(before).slice(0, 7)}..0000000`, `--- a/${path}`, "+++ /dev/null");
  else header.push(`index ${fakeBlob(before).slice(0, 7)}..${fakeBlob(after).slice(0, 7)} 100644`, `--- a/${path}`, `+++ b/${path}`);
  const hunks: string[] = [];
  for (let c = 0; c < changed.length; ) {
    let end = c;
    while (end + 1 < changed.length && changed[end + 1] - changed[end] <= 6) end++;
    const from = Math.max(0, changed[c] - 3);
    const to = Math.min(ops.length, changed[end] + 4);
    const slice = ops.slice(from, to);
    const oldCount = slice.filter((o) => o.tag !== "+").length;
    const newCount = slice.filter((o) => o.tag !== "-").length;
    const oldStart = oldCount === 0 ? slice[0].i : slice.find((o) => o.tag !== "+")!.i + 1;
    const newStart = newCount === 0 ? slice[0].j : slice.find((o) => o.tag !== "-")!.j + 1;
    hunks.push(`@@ -${oldStart},${oldCount} +${newStart},${newCount} @@`, ...slice.map((o) => `${o.tag}${o.text}`));
    c = end + 1;
  }
  return [...header, ...hunks, ""].join("\n");
}

export interface SessionReviewData {
  ready: boolean;
  reasons: string[];
  branch: string;
  base: string;
  diff: string;
  pr?: { url: string } | null;
}

/* Permission rules as POST /permissions keeps them (permissions.Rule.to_dict). */
export interface PermissionRule {
  id: string;
  kind: string;
  match: string;
  match_type: string;
  decision: string;
  source: string;
}

/* permissions.KINDS, MATCH_TYPES. */
export const PERMISSION_KINDS = ["read", "write", "edit", "bash", "fetch", "mcp", "other"];
export const MATCH_TYPES = ["exact", "prefix", "glob"];

/* config.RepoConfig's fields, with the type of each default, for PATCH /config's checks. */
const REPO_CONFIG_TYPES: Record<string, "string" | "number" | "boolean" | "list" | "dict" | "any"> = {
  setup: "list", teardown: "list", copy: "list", base_branch: "any", branch_prefix: "string", worktree_root: "any",
  default_agent: "string", fetch: "boolean", auto_update: "boolean", autopilot: "boolean", checks: "list",
  review: "any", reviewer: "string", review_profile: "any", second_review: "dict", pre_commit: "boolean",
  max_agents: "number", check_timeout: "number", check_concurrency: "number", usage_limit: "number",
  limit_cooldown_minutes: "any", graphify: "any", stale_after: "number", pipeline: "boolean", goal_audit: "boolean",
  review_rounds: "number", fable_escalation: "any", merge_into: "any", auto_merge_default_branch: "boolean",
  delegation: "string", max_weight: "any", plan_first: "boolean", permission_policy: "string", agy_approvals: "string",
  overlap: "any", coordinator: "boolean", protected_paths: "list", pool_size: "any", add_dirs: "list",
  worker_extra_read_dirs: "list", services: "list", local_models: "boolean", delete_merged_branches: "boolean",
  pr_footer: "boolean", sidebar: "string", rules: "list", learning: "string", learning_candidates: "list",
  // Engine branch desktop/auto-pr (stubbed until it merges): brindle opens a ready session's PR itself.
  auto_pr: "boolean",
};

/* config.load_repo_config: the command lists come from the repo's files only
   (config._merge_commands: the local file's list replaces the shared one, an
   empty list clears it, and only a local {before, after} wraps the shared
   list); every other key from local, else shared, else the person's
   settings, else the default. */
const REPO_ONLY_LISTS = ["setup", "teardown", "copy", "checks", "add_dirs", "worker_extra_read_dirs", "protected_paths"];

export function mergeCommands(shared: unknown, local: unknown): string[] {
  const base = Array.isArray(shared) ? shared.map(String) : [];
  if (Array.isArray(local)) return local.map(String);
  if (local && typeof local === "object") {
    const wrap = local as { before?: unknown; after?: unknown };
    const list = (v: unknown) => (Array.isArray(v) ? v.map(String) : []);
    return [...list(wrap.before), ...base, ...list(wrap.after)];
  }
  return base;
}

function effectiveConfig(files: { user: Record<string, unknown>; repo: Record<string, unknown>; local: Record<string, unknown> }): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of REPO_ONLY_LISTS) out[key] = mergeCommands(files.repo[key], files.local[key]);
  for (const key of Object.keys(REPO_CONFIG_TYPES)) {
    if (REPO_ONLY_LISTS.includes(key)) continue;
    for (const source of [files.local, files.repo, files.user]) {
      if (key in source) {
        out[key] = source[key];
        break;
      }
    }
  }
  return out;
}

/* routes_actions._fits: a value fits when it is null, or of its default's type. */
function fits(value: unknown, type: string): boolean {
  if (value === null || type === "any") return true;
  switch (type) {
    case "string":
      return typeof value === "string";
    case "number":
      return typeof value === "number";
    case "boolean":
      return typeof value === "boolean";
    case "list":
      return Array.isArray(value);
    default:
      return typeof value === "object" && !Array.isArray(value);
  }
}

/* permissions.rule_id, shortened the same way (8 characters), without the hash. */
const ruleId = (kind: string, matchType: string, match: string, decision: string) =>
  `${kind}-${matchType}-${match}-${decision}`.replace(/[^a-z0-9]+/gi, "").slice(0, 8).padEnd(8, "0");

const describeRule = (r: PermissionRule) => `${r.kind} ${r.match_type} '${r.match}'`;

export type RoutingProfile = Omit<RoutingProfileDTO, "excluded" | "allowed" | "refusal" | "provider_name">;

/* An org's policy as team_policy.Access and managed_models hold it: what the
   org allows (null: anything), what it locks, and whether it could be read
   at all (`error`: it applies but can't be, so nothing may change). */
export interface RoutingPolicyData {
  org_id: string;
  allowed_providers?: string[] | null;
  allowed_models?: string[] | null;
  allowed_profiles?: string[] | null;
  deny_personal_keys?: boolean;
  /* Providers whose routes the org manages (managed_models: only an admin changes them). */
  managed_routes?: string[];
  error?: string | null;
}

/* team_policy.CLI_NAMES. */
export const CLI_NAMES: Record<string, string> = { claude: "Claude Code", codex: "Codex", antigravity: "Google Antigravity", opencode: "OpenCode" };

/* An action terminal the daemon opened (terminal.open_action), by id. */
/* The providers brindle can install, and what each maker's official
   installer prints and where it puts the CLI here. */
export const INSTALLABLE = ["claude", "codex", "opencode", "antigravity"] as const;
export const INSTALLERS: Record<string, { version: string; path: string; lines: string[] }> = {
  claude: {
    version: "2.1.47",
    path: "/home/u/.local/bin/claude",
    lines: ["Setting up Claude Code...", "Downloading Claude Code 2.1.47 (darwin-arm64)", "Verifying checksum", "Installing to /home/u/.local/bin/claude", "✔ Claude Code successfully installed!"],
  },
  codex: { version: "0.48.0", path: "/opt/homebrew/bin/codex", lines: ["==> Fetching codex", "==> Downloading codex-0.48.0.arm64_sequoia.bottle.tar.gz", "==> Pouring codex-0.48.0", "🍺  /opt/homebrew/Cellar/codex/0.48.0: 4 files"] },
  opencode: { version: "1.2.8", path: "/home/u/.opencode/bin/opencode", lines: ["Installing opencode version: 1.2.8", "Downloading opencode-darwin-arm64.zip", "Successfully installed opencode to /home/u/.opencode/bin"] },
  antigravity: { version: "1.3.0", path: "/home/u/.local/bin/agy", lines: ["Downloading Antigravity 1.3.0", "Installing to /home/u/.local/bin/agy", "Antigravity installed."] },
};

/* One row of GET /providers/installed: none found unless `fields` says so. */
export function providerRow(provider: string, fields: Partial<ProviderInstalled> = {}): ProviderInstalled {
  const row: ProviderInstalled = { provider, name: CLI_NAMES[provider] ?? provider, state: fields.path ? "installed" : "missing", path: null, version: null, signed_in: null, ...fields };
  return { ...row, installs: row.path ? [{ path: row.path, version: row.version }] : [] };
}

/* What a first launch finds: no CLI, or the person's own (`found`, by provider). */
export function freshProviders(found: Record<string, { path: string; version: string; signed_in?: boolean }> = {}): ProviderInstalled[] {
  return INSTALLABLE.map((p) => (found[p] ? providerRow(p, { path: found[p].path, version: found[p].version, signed_in: found[p].signed_in ?? true }) : providerRow(p)));
}

/* An action terminal's window: its id, kind and command, its pane (`target`
   on the daemon's own tmux server, ACTION_SERVER), and whether its command still runs. */
export interface ActionWindow {
  id: string;
  kind: string;
  command: string[];
  target: string;
  alive: boolean;
}

/* The tmux server the daemon's action terminals run on (tmux.current_server). */
export const ACTION_SERVER = "brindle";

/* picker._login_command, keystore / providers.brindle_invocation and the upgrade. */
function actionCommand(kind: string, name: string): string[] {
  if (kind === "login") return name === "antigravity" ? ["agy"] : name === "opencode" ? ["opencode", "auth", "login"] : [name, name === "codex" ? "login" : "auth", ...(name === "claude" ? ["login"] : [])];
  if (kind === "key") return ["brindle", "keys", "set", name];
  return ["uv", "tool", "upgrade", "brindle"];
}

/* keystore.check_name: a key agents may be given; brindle's own token never. */
const KEY_NAME = /^[A-Z][A-Z0-9_]{2,}$/;
const RESERVED_KEYS = new Set(["BRINDLE_TOKEN"]);

/* picker.LOGIN_METHODS: the ways a CLI's sign-in can be asked for, by flag. */
const LOGIN_METHODS: Record<string, string[]> = { claude: ["claudeai", "console", "sso"], codex: ["device"] };
const LOGIN_FLAGS: Record<string, string> = { claudeai: "--claudeai", console: "--console", sso: "--sso", device: "--device-auth" };

export interface RoutingFile {
  routing?: Record<string, string[]>;
  reviewer?: string;
  exclude_providers?: string[];
  /* The kinds of work hosted learning never overrides (config routing_locked). */
  routing_locked?: string[];
  /* The supervisor's profile (the user file's `supervisor`; a known profile, validated). */
  supervisor?: string;
  /* The reviewer's and the supervisor's ordered fallbacks (a weight's are its `routing` list). */
  kind_fallbacks?: Record<string, string[]>;
  /* Ahead of the engine: each kind's effort and instructions. */
  kinds?: Record<string, KindExtras>;
}

/* config.WEIGHTS, DEFAULT_ROUTING and RepoConfig.reviewer. */
export const WEIGHTS = ["light", "medium", "heavy", "advanced"] as const;
/* config.ROUTING_KINDS. */
export const ROUTING_KINDS: readonly string[] = [...WEIGHTS, "reviewer", "supervisor"];
export const DEFAULT_ROUTING: Record<string, string[]> = {
  light: ["developer-light", "developer", "developer-codex", "developer-local"],
  medium: ["developer", "developer-codex", "developer-heavy"],
  heavy: ["developer-heavy", "developer-gemini", "developer", "developer-codex"],
  advanced: ["developer-fable", "developer-heavy", "developer-gemini"],
};
export const DEFAULT_REVIEWER = "reviewer";
/* providers.PROVIDERS. */
export const PROVIDERS = ["claude", "codex", "antigravity", "opencode", "native", "shell", "subagent"];
const ROUTING_SCOPES: RoutingScope[] = ["user", "local", "repo"];

/* A pane: a live one (its screen as tmux capture-pane prints it, the cursor
   as (column, row) from 0), or a dead one (its command exited and tmux kept
   it, remain-on-exit: its last screen, or nothing). */
export type Pane = { live: true; screen: string; cursor?: [number, number] } | { live: false; text: string | null };

/* What tmux draws under a dead pane's last screen (remain-on-exit-format). */
export const PANE_DEAD_LINE = "Pane is dead (status 0, Tue Oct 20 09:12:00 2026)";

/* Newlines as a terminal needs them. */
const asTerminal = (text: string) => text.replace(/\n$/, "").replaceAll("\n", "\r\n");

/* A pane as a tmux client draws it on attaching: the screen cleared, the
   pane's screen, then its cursor; a dead pane's last screen and tmux's line. */
const drawn = (pane: Pane): string =>
  "\x1b[H\x1b[2J" +
  (pane.live ? asTerminal(pane.screen) + (pane.cursor ? `\x1b[${pane.cursor[1] + 1};${pane.cursor[0] + 1}H` : "") : `${pane.text ? asTerminal(pane.text) + "\r\n" : ""}\r\n${PANE_DEAD_LINE}`);

/* A view the app's tmux client opened (PtyViews.open): where, on whose pane
   (an agent's or an action terminal's id; null when no pane is there), at
   what size, what the person typed into it, its resizes, how often the
   person came to it (focused or clicked), and whether it is closed. */
export interface FakeView {
  at: PtyViewTarget;
  pane: string | null;
  size: { cols: number; rows: number };
  handlers: PtyViewHandlers;
  written: string[];
  resizes: [number, number][];
  focuses: number;
  closed: boolean;
}

export interface Request {
  method: string;
  path: string;
  query: Record<string, string>;
  /* A POST's JSON body, as sent. */
  body?: unknown;
}

/* A session root in one of these is listed as paused (brindle/sessions.py paused). */
const PAUSED = new Set(["paused", "failed"]);

/* routes_actions.BUSY: an agent mid-turn may be writing the file an edit touches. */
const BUSY = new Set(["starting", "processing", "waiting"]);

/* The blob sha the fake answers for a file's content: stable, 40 hex characters,
   so a diff's `index` line in a fixture can name it (git's would be SHA-1). */
export function fakeBlob(content: string): string {
  let h = 5381;
  for (let i = 0; i < content.length; i++) h = ((h * 33) ^ content.charCodeAt(i)) >>> 0;
  return (h.toString(16).padStart(8, "0") + "b10b".repeat(8)).slice(0, 40);
}

/* What each inbox action answers in the engine (routes_actions.inbox_action). */
const INBOX_ACTIONS: Record<string, unknown> = {
  answer: { status: "delivered" },
  approve: { message: "plan approved" },
  reject: { message: "plan sent back" },
  accept: { message: "checks added" },
  decline: { message: "nothing was written" },
  review: { reviewer: "rev1", checks_running: false },
  merge: { job: "job1", kind: "merge" },
  read: { read: 1 },
};

/* The workspace actions that run as jobs (routes_actions.py start_job). */
const JOB_ACTIONS = new Set(["merge", "sync", "pr"]);

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const error = (code: string, message: string, status: number) => json({ error: { code, message } }, status);

export class FakeSocket implements WebSocketLike {
  onopen: WebSocketLike["onopen"] = null;
  onmessage: WebSocketLike["onmessage"] = null;
  onclose: WebSocketLike["onclose"] = null;
  onerror: WebSocketLike["onerror"] = null;
  /* What the client sent, in order; frames[0] should be the token. */
  readonly frames: string[] = [];
  authed = false;
  closed = false;
  readonly url: URL;
  private readonly daemon: FakeDaemon;

  constructor(daemon: FakeDaemon, url: string) {
    this.daemon = daemon;
    this.url = new URL(url);
    queueMicrotask(() => {
      if (!this.closed) this.onopen?.({});
    });
  }

  get since(): number {
    return Number(this.url.searchParams.get("since") ?? "0");
  }

  get repo(): string | null {
    return this.url.searchParams.get("repo");
  }

  send(data: string): void {
    if (this.closed) return;
    this.frames.push(data);
    if (this.frames.length === 1) this.daemon.authenticate(this, data);
  }

  close(code = 1000): void {
    if (this.closed) return;
    this.closed = true;
    queueMicrotask(() => this.onclose?.({ code }));
  }

  deliver(message: StreamMessage): void {
    this.push(JSON.stringify(message));
  }

  /* The daemon closes the socket once the frames it has sent are delivered. */
  hangUp(code = 1000): void {
    queueMicrotask(() => this.close(code));
  }

  /* A frame as the daemon sends it: text, or binary (an ArrayBuffer, as a
     socket with binaryType "arraybuffer" receives it). */
  push(data: string | ArrayBuffer): void {
    if (this.closed) return;
    queueMicrotask(() => {
      if (!this.closed) this.onmessage?.({ data });
    });
  }
}

export class FakeDaemon {
  token = TOKEN;
  data: DaemonData;
  /* Whether a job's job.done reaches the bus (false: a missed event). */
  jobEvents = true;
  /* The engine's upgrade fails (a download that didn't come). */
  upgradeFails = false;
  /* Hold each upgrade until completeUpgrade(): uv still running. */
  deferUpgrade = false;
  private pendingUpgrades: Array<() => void> = [];
  /* Hold a session start between its status going out and its window being recorded (agents._launch's
     order: a snapshot then lists it "exited", with no window) until finishLaunch() answers the start. */
  holdLaunch = false;
  private pendingLaunch: (() => void) | null = null;
  finishLaunch(): void {
    const finish = this.pendingLaunch;
    this.pendingLaunch = null;
    finish?.();
  }
  /* The upgrade job running now (a second press gets it back), and every one started. */
  private upgrading: string | null = null;
  readonly upgrades: string[] = [];
  /* The Codex hook fix (POST /setup/codex-hook) fails: Codex isn't installed. */
  fixFails = false;
  /* Hold each fix until completeFixes(): still writing Codex's config. */
  deferFixes = false;
  private pendingFixes: Array<() => void> = [];
  /* The fix job running now (a second press gets it back), and every one started. */
  private fixing: string | null = null;
  readonly fixes: string[] = [];
  /* URL → how many of its next clones fail as git does without credentials for it. */
  cloneFails: Record<string, number> = {};
  /* Folders POST /repos/open refuses: not a git repository. */
  notGitFolders: string[] = [];
  /* The clones started ({url, parent_dir}) and the folders opened, in order. */
  readonly clones: { url: string; parent_dir: string }[] = [];
  readonly opened: string[] = [];
  /* Provider → how many of its next installs fail (the installer exits 1 partway). */
  installFails: Record<string, number> = {};
  /* Hold each install partway until completeInstall(): the installer is still running. */
  deferInstall = false;
  private pendingInstalls: Array<() => void> = [];
  /* Provider → the install job running for it (a second press gets it back), and every install started. */
  private installing = new Map<string, string>();
  readonly installs: string[] = [];
  /* Every provider taken out of brindle (DELETE /providers/{p}), in order,
     and every copy picked to run ("<provider> <path>", POST .../use-mine). */
  readonly removedProviders: string[] = [];
  readonly pickedInstalls: string[] = [];
  /* How many GET /providers/installed fail (a 500) right after the next
     successful install: the CLI is in place, the look for it hiccups. */
  installDetectFails = 0;
  private failNextDetects = 0;
  /* The engine is dead (`die()`): every request is refused and every socket
     closes at once, until `revive()` brings it back as a new process. */
  dead = false;
  /* Hold a sign-in after its URL went out, until completeLogin(): the browser hasn't come back yet. */
  deferLogin = false;
  private pendingLogins: Array<{ job: string; finish(): void }> = [];
  /* The rules POST /permissions added (allow, deny, accept), by id. */
  readonly permissionRules: PermissionRule[] = [];
  permissionEditor: PermissionEditorState = {
    revision: "editor-1", rules: [],
    catalog: [
      { id: "bash", kind: "bash", name: "Run commands", tool: null, source: "builtin" },
      { id: "read", kind: "read", name: "Read files", tool: null, source: "builtin" },
      { id: "edit", kind: "edit", name: "Edit files", tool: null, source: "builtin" },
    ],
    presets: [], limitations: [],
    restrictions: { editable: true, managed: false, reason: "", org_id: null, policy_version: null, permission_grants: null,
      protected_paths: [], permission_policy: "strict", roles_supported: false, repo_supported: true, expiry_supported: true, checks: [] },
  };
  /* Engine review fixtures can describe scope relationships which belong to
     the Python engine. The fake persists exactly the reviewed proposal. */
  readonly permissionReviews: PermissionEditorReview[] = [];
  private permissionProposal: { request: PermissionEditorRequest; review: PermissionEditorReview } | null = null;
  private permissionSequence = 1;
  /* The orgs POST /account/switch-org was asked for, in order. */
  readonly orgSwitches: string[] = [];
  /* The views the app's tmux client opened on panes (ptyViews()), in order, and what tmux copied onto the app's clipboard. */
  readonly views: FakeView[] = [];
  readonly copied: string[] = [];
  /* What the app's clipboard holds: the last copied, or what a test put there, for a view's ⌘V. */
  clipboard = "";
  /* The action terminals opened so far (terminal.open_action), in order. */
  readonly actions: ActionWindow[] = [];
  /* What POST /terminals/{id}/input typed into them, in order. */
  readonly inputs: { terminal: string; text: string }[] = [];
  /* The shells POST /terminals/shell started, in order, and the terminals DELETE /terminals/{id} closed. */
  readonly shells: { terminal: string; cwd: string }[] = [];
  readonly closedTerminals: string[] = [];
  /* Panes on a real tmux server for the next shells to be (the e2e's, over its bridge); none: drawn here. */
  shellPanes: PtyViewTarget[] = [];
  /* The keys PUT /keys/{name} stored, in order (never their values). */
  readonly keysStored: string[] = [];
  /* The knowledge sources synced and removed, in order, and how many documents an index finds in a folder. */
  readonly kbSyncs: string[] = [];
  readonly kbRemovals: string[] = [];
  kbDocsPerFolder = 3;
  /* The edits PUT /workspaces/{id}/file committed, in order. */
  readonly edits: { workspace: string; path: string; content: string }[] = [];
  /* The permission requests POST /inbox/{id}/permission answered, in order. */
  readonly permissionAnswers: { agent: string; request: string; choice: string }[] = [];
  /* The edits PUT / DELETE /sessions/{root}/file committed (content null: deleted), in order. */
  readonly sessionEdits: { root: string; path: string; content: string | null }[] = [];
  /* The PRs POST /sessions/{root}/pr opened, in order, with their URLs. */
  readonly prs: { root: string; title?: string; body?: string; draft?: boolean; url: string }[] = [];
  events: BusEvent[] = [];
  readonly requests: Request[] = [];
  readonly sockets: FakeSocket[] = [];
  private nextId = 1;
  private nextSession = 1;
  private nextJob = 1;

  constructor(data: Partial<DaemonData> = {}) {
    this.data = {
      meta: {
        engine_version: "0.5.0",
        api_version: "1.0",
        pid: 4242,
        db_path: "/home/u/.brindle/brindle.db",
        tmux: true,
        airgap: false,
      },
      repos: [],
      sessions: [],
      workspaces: [],
      inbox: [],
      goal: null,
      decisions: [],
      profiles: [],
      account: { signed_in: false },
      terminals: {},
      closed: [],
      turns: {},
      rewindFails: null,
      profilesByRepo: {},
      diffs: {},
      files: {},
      preflight: {},
      providers: freshProviders({ claude: { path: "/home/u/.local/bin/claude", version: "2.1.47" } }),
      routingProfiles: [],
      routingConfig: { user: {}, repo: {}, local: {} },
      routingByRepo: {},
      routingPolicy: null,
      learned: {},
      grants: [],
      rulePacks: [],
      actionScreens: {},
      kbSources: [],
      kbHits: [],
      profileDetails: {},
      orgs: {},
      config: { user: {}, repo: {}, local: {}, keys: [] },
      userSettings: { claude_permission_mode: "default" },
      debug: { staff: false, enabled: false },
      debugLog: [],
      doctor: [],
      cost: { days: 30, total: { dollars: 0, tokens: 0, unknown: 0, free: 0 }, by_model: {} },
      costEntitled: false,
      usage: { providers: ["claude", "codex", "antigravity", "native"].map((provider) => ({ provider, known: false, limited: false, limited_until: null, updated_at: null, windows: [] })), near: [] },
      accountAfterLogin: { signed_in: true, name: "sam@example.com", plan: "pro" },
      loginUrl: "https://pawdelta.com/api/brindle/v1/authorize?client_id=brindle-cli&state=s1",
      sessionReview: {},
      sessionFiles: {},
      sessionBase: {},
      ...data,
    };
  }

  /* brindle judges the session ready for a PR (session_review.ReadinessWatch):
     its review reads ready, its goal's phase ready_for_pr (pr_open when the
     review carries brindle's own PR), and session.pr_ready goes on the bus. */
  readyForPr(root: string, review: Partial<SessionReviewData> = {}): void {
    const session = this.data.sessions.find((s) => s.id === root);
    const was = this.data.sessionReview[root];
    const defaults: SessionReviewData = { ready: false, reasons: [], branch: "dev/feature", base: "main", diff: "" };
    this.data.sessionReview[root] = { ...defaults, ...was, ...review, ready: true, reasons: [] };
    const pr = this.data.sessionReview[root].pr;
    this.setGoalPhase(root, pr ? "pr_open" : "ready_for_pr", pr?.url);
    this.publish("session.pr_ready", { root_id: root, agent_id: root, repo_root: session ? this.repoOf(session) : null, workspace_id: session?.workspace_id ?? null });
  }

  /* The session's goal moves along its lifecycle (the engine's goal_phase, branch feat/goal-lifecycle). */
  setGoalPhase(root: string, phase: string, prUrl?: string | null): void {
    const goal = this.data.goal;
    if (goal?.root_id !== root) return;
    this.data.goal = { ...goal, goal_phase: phase, pr_url: prUrl ?? goal.pr_url ?? null };
  }

  /* The session reaches its goal (engine branch feat/keep-finished-goal): the goal is kept, its title and
     every milestone, all passed, with goal_phase done, until a new goal replaces it. */
  completeGoal(root: string): void {
    const goal = this.data.goal;
    if (goal?.root_id !== root) return;
    const at = Date.now() / 1000;
    this.data.goal = {
      ...goal,
      state: "done",
      goal_phase: "done",
      milestones: goal.milestones?.map((m) => ({ ...m, status: "passed", state: "passed", state_why: null, phase: "done", checked_at: m.checked_at ?? at })),
    };
    this.publish("autopilot.done", { root_id: root, agent_id: root, payload: {} });
  }

  /* GET /sessions/{root}/pr/readiness, /diff, /pr/draft, /file (routes_read). */
  private sessionReviewRead(root: string, what: string, query: Record<string, string>): Response {
    const d = this.data;
    const session = d.sessions.find((s) => s.id === root);
    if (!session) return error("not_found", `no session ${root}`, 404);
    const review = d.sessionReview[root] ?? { ready: false, reasons: ["autopilot isn't on for this session, so there is no goal to judge it by"], branch: "", base: "", diff: "" };
    if (what === "pr/readiness") return json({ root_id: root, ready: review.ready, reasons: review.reasons, pr: review.pr ?? null });
    if (what === "file") {
      const content = d.sessionFiles[root]?.[query.path ?? ""];
      if (!query.path) return error("invalid", "path is required", 400);
      if (content === undefined) return error("not_found", `no file ${query.path} in the session's checkout`, 404);
      return json({ path: query.path, content, base: fakeBlob(content) });
    }
    if (!review.ready) return json({ error: { code: "not_ready", message: `the session isn't ready for a PR: ${review.reasons.join("; ")}` }, reasons: review.reasons }, 409);
    if (what === "diff") {
      // From the base and the checkout as they stand, when the base is known: the diff follows every edit.
      const base = d.sessionBase[root];
      const current = d.sessionFiles[root] ?? {};
      const patch = base ? [...new Set([...Object.keys(base), ...Object.keys(current)])].sort().map((p) => unifiedDiff(p, base[p] ?? null, current[p] ?? null)).join("") : review.diff;
      const files = parseDiff(patch).files.map((f) => ({
        path: f.path,
        old_path: f.oldPath,
        status: f.status === "added" ? "A" : f.status === "deleted" ? "D" : f.status === "renamed" ? "R" : "M",
        binary: f.binary,
        authors: ["supervisor"],
        hunks: hunksOf(f).map((h) => ({ header: h.header, old_start: 0, old_lines: 0, new_start: h.newStart, new_lines: h.newCount, text: f.lines.slice(h.start, h.end).join("\n"), authors: ["supervisor"] })),
      }));
      return json({ root_id: root, branch: review.branch, base: review.base, base_sha: "b".repeat(40), head: `h${this.nextId}`.padEnd(40, "0"), files, workers: [] });
    }
    if (what === "pr/draft") {
      const goal = d.goal?.root_id === root ? d.goal : null;
      const titles = goal?.milestones?.map((m) => `- ${m.title}`) ?? [];
      const parts = [goal?.detail?.trim(), titles.length ? `Milestones, each verified by its check:\n${titles.join("\n")}` : ""].filter(Boolean);
      return json({ title: (goal?.goal ?? session.goal ?? "Changes").split("\n")[0], body: parts.join("\n\n"), base: review.base, branch: review.branch });
    }
    return error("not_found", `no route /sessions/${root}/${what}`, 404);
  }

  /* PUT / DELETE /sessions/{root}/file (routes_actions._edit_session): the
     person's edit committed on the session branch; refused while the
     supervisor is mid-turn (409 busy) or when the file moved on (409 stale);
     a PUT with no base creates the file. */
  private editSessionFile(root: string, body: Record<string, unknown>, remove: boolean): Response {
    const d = this.data;
    if (!d.sessions.some((s) => s.id === root)) return error("not_found", `no session ${root}`, 404);
    const path = body.path;
    if (typeof path !== "string" || !path) return error("invalid", "path is required", 400);
    if (path.startsWith("/") || path.split("/").some((p) => p === ".." || p.toLowerCase() === ".git")) return error("invalid", `'${path}' isn't a path inside the checkout`, 400);
    const supervisor = d.workspaces.flatMap((w) => w.agents ?? []).find((a) => a.id === root);
    if (supervisor && BUSY.has(supervisor.status)) return error("busy", `the supervisor ${root} is working; edit once it has stopped`, 409);
    const files = (d.sessionFiles[root] ??= {});
    const base = typeof body.base === "string" ? body.base.trim().toLowerCase() : null;
    if (remove || path in files) {
      if (base === null) return error("invalid", "base is required", 400);
      if (!(path in files)) return error("not_found", `no file ${path} in the session's checkout`, 404);
      if (base.length < 7 || !fakeBlob(files[path]).startsWith(base)) return error("stale", `${path} has changed since it was shown; reload it`, 409);
    }
    let blob: string | null = null;
    if (remove) delete files[path];
    else {
      if (typeof body.content !== "string") return error("invalid", "content is required", 400);
      files[path] = body.content;
      blob = fakeBlob(body.content);
    }
    this.sessionEdits.push({ root, path, content: remove ? null : (body.content as string) });
    const commit = `c${this.nextId++}`.padEnd(40, "0");
    this.publish("session.edited", { root_id: root, agent_id: root, payload: { path, commit, deleted: remove } });
    return json({ root_id: root, path, commit, blob });
  }

  /* Finish the sign-ins held by deferLogin: the browser came back and the tokens are stored. */
  completeLogin(): void {
    for (const run of this.pendingLogins.splice(0)) run.finish();
  }

  /* An action terminal's command exits (with `screen` as its last words): its
     viewers' sockets close as the pane dies; the next press starts afresh. */
  exitAction(id: string, screen?: string): void {
    const action = this.actions.find((a) => a.id === id);
    if (!action) return;
    action.alive = false;
    const pane = this.data.terminals[id];
    this.data.terminals[id] = { live: false, text: screen ?? (pane?.live ? pane.screen : null) };
    // tmux keeps the pane (remain-on-exit): its views see the last words, then tmux's line.
    this.print(id, `${screen ? asTerminal(screen) : ""}\r\n${PANE_DEAD_LINE}`);
  }

  /* terminal.open_action: the command in a window of the daemon's own tmux
     session, found again while it still runs; its ActionTerminalDTO says
     where its pane is, for the app's tmux client to attach to. */
  private openAction(kind: string, name: string, method?: string): Response {
    const key = kind === "upgrade" ? "upgrade" : `${kind} ${name}`;
    const running = this.actions.find((a) => a.kind === key && a.alive);
    if (running) return json({ terminal: running.id, started: false, target: running.target, server: ACTION_SERVER });
    const n = this.nextId++;
    const id = `t-${n.toString(16).padStart(16, "0")}`;
    const target = `%${n}`;
    const command = [...actionCommand(kind, name), ...(method ? [LOGIN_FLAGS[method] ?? `--${method}`] : [])];
    this.actions.push({ id, kind: key, command, target, alive: true });
    const screen = this.data.actionScreens[key] ?? `$ ${command.join(" ")}\n`;
    this.data.terminals[id] = { live: true, screen, cursor: [0, screen.split("\n").length - 1] };
    return json({ terminal: id, started: true, target, server: ACTION_SERVER });
  }

  /* routes_actions.shell_terminal: the person's login shell in `cwd` (a
     repo's root or one of its worktrees), a new window each call; its pane
     the next of `shellPanes` where a real tmux server holds them (the e2e's,
     a zsh too). It names the shell, as the app expects the engine to. */
  private openShell(body: Record<string, unknown>): Response {
    const cwd = body.cwd;
    if (typeof cwd !== "string" || !cwd) return error("invalid", "cwd is required", 400);
    const allowed = new Set([...this.data.repos.map((r) => r.repo_root), ...this.data.workspaces.map((w) => w.path)]);
    if (!allowed.has(cwd)) return error("invalid", `${cwd} isn't an attached repo or one of its worktrees`, 400);
    const n = this.nextId++;
    const id = `t-${n.toString(16).padStart(16, "0")}`;
    const real = this.shellPanes.shift();
    const target = real?.target ?? `%${n}`;
    this.actions.push({ id, kind: "shell", command: ["zsh", "-l"], target, alive: true });
    this.shells.push({ terminal: id, cwd });
    const screen = `${cwd} % `;
    this.data.terminals[id] = { live: true, screen, cursor: [screen.length, 0] };
    return json({ terminal: id, started: true, target, server: real ? real.server : ACTION_SERVER, shell: "zsh" });
  }

  /* routes_actions.close_terminal: the command ended and its window gone. */
  private closeTerminal(id: string): Response {
    const action = this.actions.find((a) => a.id === id && a.alive);
    if (!action) return error("not_found", `no terminal ${id}`, 404);
    action.alive = false;
    delete this.data.terminals[id];
    this.closedTerminals.push(id);
    this.windowGone(id);
    return json({ terminal: id, closed: true });
  }

  /* routes_actions.terminal_input: one printable line, typed into a running action terminal. */
  private terminalInput(id: string, body: Record<string, unknown>): Response {
    const text = body.text;
    if (typeof text !== "string" || !text || text.length > 4096 || /[\x00-\x1f\x7f]/.test(text)) return error("invalid", "text must be one line of printable characters", 400);
    const action = this.actions.find((a) => a.id === id && a.alive);
    if (!action) return error("not_found", `no running terminal ${id}`, 404);
    this.inputs.push({ terminal: id, text });
    // tmux echoes the line into the pane, as the CLI's own prompt would.
    this.print(id, `${text}${body.submit === false ? "" : "\r\n"}`);
    return json({ terminal: id, sent: true });
  }

  /* routes_actions.put_key: stored as `brindle keys set` stores it; the value never comes back. */
  private putKey(name: string, body: Record<string, unknown>): Response {
    const value = body.value;
    if (typeof value !== "string" || !value.trim()) return error("invalid", "value must be a non-empty string", 400);
    if (!KEY_NAME.test(name) || RESERVED_KEYS.has(name)) return error("invalid", `${name} isn't a key agents can be given`, 400);
    if (this.data.routingPolicy?.deny_personal_keys) return error("forbidden", `org ${this.data.routingPolicy.org_id} doesn't allow personal API keys`, 403);
    const last4 = value.trim().slice(-4);
    const keys = this.data.config.keys;
    const at = keys.findIndex((k) => k.name === name);
    if (at >= 0) keys[at] = { name, set: true, last4 };
    else keys.push({ name, set: true, last4 });
    this.keysStored.push(name);
    return json({ name, last4, set: true });
  }

  /* Finish the upgrades held by deferUpgrade: the command prints its last lines and exits. */
  completeUpgrade(): void {
    for (const run of this.pendingUpgrades.splice(0)) run();
  }

  /* routes_actions.upgrade_engine: the upgrade on the engine itself, no
     terminal, a job (202 {job, kind}): job.progress (stage "upgrading") as
     uv starts, then job.done and the engine reads as the new version, or,
     with `upgradeFails`, job.failed with uv's last lines in the error's
     `tail`. A press while one runs gets that job back. */
  private upgradeEngine(): Response {
    if (this.upgrading) return json({ job: this.upgrading, kind: "engine.upgrade" }, 202);
    const job = `job${this.nextJob++}`;
    this.upgrading = job;
    this.upgrades.push(job);
    // The stage goes out as uv starts, right after the ack; the end once it has run (held by deferUpgrade).
    setTimeout(() => this.publish("job.progress", { payload: { job, stage: "upgrading" } }), 0);
    const run = () => {
      this.upgrading = null;
      if (this.upgradeFails) {
        this.publish("job.failed", {
          payload: { job, job_kind: "engine.upgrade", error: { code: "upgrade_failed", message: "uv tool upgrade brindle exited 1", tail: ["Resolved 24 packages in 412ms", "error: Failed to download brindle==0.5.1: network unreachable"] } },
        });
      } else {
        this.data.meta = { ...this.data.meta, engine_version: "0.5.1" };
        this.publish("job.done", { payload: { job, job_kind: "engine.upgrade", result: { upgraded: true } } });
      }
    };
    if (this.deferUpgrade) this.pendingUpgrades.push(run);
    else setTimeout(run, 0);
    return json({ job, kind: "engine.upgrade" }, 202);
  }

  /* Finish the installs held by deferInstall: the rest of the download, the check, the unpack. */
  completeInstall(): void {
    for (const run of this.pendingInstalls.splice(0)) run();
  }

  /* The provider's own sign-in finished in the browser: its row reads signed
     in, and its login terminal (POST /providers/{p}/login) exits. */
  signInProvider(provider: string): void {
    this.data.providers = this.data.providers.map((r) => (r.provider === provider ? { ...r, signed_in: true } : r));
    for (const a of this.actions.filter((x) => x.kind === `login ${provider}` && x.alive)) this.exitAction(a.id, "Login successful.");
  }

  /* routes_actions.install_provider: the maker's official installer run
     as-is, a job (provider.install), one per provider. Each line it prints
     is a job.progress {line}; then job.done with the CLI as detected after
     (where the installer put it, not signed in yet). With installFails the
     installer exits 1 partway: job.failed (install_failed) with its last
     line, the row as it was. */
  private installProvider(provider: string): Response {
    if (!(INSTALLABLE as readonly string[]).includes(provider)) return error("invalid", `provider must be one of ${INSTALLABLE.join(", ")}`, 400);
    const running = this.installing.get(provider);
    if (running) return json({ job: running, kind: "provider.install" }, 202);
    const job = `job${this.nextJob++}`;
    this.installing.set(provider, job);
    this.installs.push(provider);
    const { version, path, lines } = INSTALLERS[provider];
    // As routes_actions.install_provider publishes it: each line the installer prints as the progress `text`.
    const say = (text: string) => this.publish("job.progress", { payload: { job, provider, text } });
    const half = Math.ceil(lines.length / 2);
    const finish = () => {
      this.installing.delete(provider);
      if ((this.installFails[provider] ?? 0) > 0) {
        this.installFails[provider]--;
        say("curl: (56) Recv failure: Connection reset by peer");
        this.publish("job.failed", {
          payload: { job, job_kind: "provider.install", provider, error: { code: "install_failed", message: `${CLI_NAMES[provider]}'s installer exited 1: curl: (56) Recv failure: Connection reset by peer` } },
        });
        return;
      }
      lines.slice(half).forEach(say);
      // An install turns the provider back on; a CLI that was off keeps its sign-in, a fresh one has none yet.
      this.data.providers = this.data.providers.map((r) =>
        r.provider === provider ? { ...r, state: "installed", path, version, signed_in: r.state === "off" ? r.signed_in : false, installs: [{ path, version }] } : r,
      );
      if (this.installDetectFails > 0) this.failNextDetects = this.installDetectFails;
      this.publish("job.done", { payload: { job, job_kind: "provider.install", provider, result: this.data.providers.find((r) => r.provider === provider) } });
    };
    setTimeout(() => {
      lines.slice(0, half).forEach(say);
      if (this.deferInstall) this.pendingInstalls.push(finish);
      else finish();
    }, 0);
    return json({ job, kind: "provider.install" }, 202);
  }

  /* routes_actions.turn_off_provider (DELETE /providers/{p}): brindle stops
     using the CLI and never uninstalls it. Answers the provider as GET
     /providers/installed now shows it: state "off", the copy still found. */
  private removeProvider(provider: string): Response {
    if (!(INSTALLABLE as readonly string[]).includes(provider)) return error("invalid", `provider must be one of ${INSTALLABLE.join(", ")}`, 400);
    this.removedProviders.push(provider);
    this.data.providers = this.data.providers.map((r) => (r.provider === provider ? { ...r, state: "off" } : r));
    return json(this.data.providers.find((r) => r.provider === provider));
  }

  /* routes_actions.use_provider_install (POST /providers/{p}/use-mine
     {path}): runs that copy, one of those found, and turns the provider back on. */
  private pickProviderInstall(provider: string, body: Record<string, unknown>): Response {
    if (!(INSTALLABLE as readonly string[]).includes(provider)) return error("invalid", `provider must be one of ${INSTALLABLE.join(", ")}`, 400);
    const row = this.data.providers.find((r) => r.provider === provider);
    const copy = row?.installs?.find((i) => i.path === body.path);
    if (!row || !copy) return error("not_found", `${String(body.path)} isn't an install of ${CLI_NAMES[provider] ?? provider} brindle finds`, 404);
    this.pickedInstalls.push(`${provider} ${copy.path}`);
    this.data.providers = this.data.providers.map((r) => (r.provider === provider ? { ...r, state: "installed", path: copy.path, version: copy.version, selected: true } : r));
    return json(this.data.providers.find((r) => r.provider === provider));
  }

  /* POST /repos/clone (engine branch feat/repo-clone-route): `git clone`
     into parent_dir, a job (repo.clone); git's lines as job.progress {line},
     then job.done {path} and the clone is one of the repos. With cloneFails,
     git's auth failure: job.failed with git's own message. */
  private cloneRepo(body: Record<string, unknown>): Response {
    const { url, parent_dir: parent } = body;
    if (typeof url !== "string" || !url.trim()) return error("invalid", "url is required", 400);
    if (typeof parent !== "string" || !parent.trim()) return error("invalid", "parent_dir is required", 400);
    const job = `job${this.nextJob++}`;
    this.clones.push({ url, parent_dir: parent });
    const name = url.replace(/\/+$/, "").replace(/\.git$/, "").split(/[/:]/).at(-1) ?? "repo";
    const path = `${parent.replace(/\/+$/, "")}/${name}`;
    const say = (line: string) => this.publish("job.progress", { payload: { job, line } });
    setTimeout(() => {
      say(`Cloning into '${name}'...`);
      if ((this.cloneFails[url] ?? 0) > 0) {
        this.cloneFails[url]--;
        const message = `remote: Repository not found.\nfatal: Authentication failed for '${url}/'`;
        this.publish("job.failed", { payload: { job, job_kind: "repo.clone", error: { code: "clone_failed", message } } });
        return;
      }
      say("remote: Enumerating objects: 1204, done.");
      say("Receiving objects: 100% (1204/1204), 2.31 MiB | 8.10 MiB/s, done.");
      say("Resolving deltas: 100% (611/611), done.");
      if (!this.data.repos.some((r) => r.repo_root === path)) this.data.repos = [...this.data.repos, { repo_root: path, name }];
      this.publish("job.done", { payload: { job, job_kind: "repo.clone", result: { path, name } } });
    }, 0);
    return json({ job, kind: "repo.clone" }, 202);
  }

  /* POST /repos/open: a folder as a repo brindle works in; refused (409) when it isn't a git repository. */
  private openRepo(body: Record<string, unknown>): Response {
    const path = typeof body.path === "string" ? body.path.replace(/\/+$/, "") : "";
    if (!path) return error("invalid", "path is required", 400);
    if (this.notGitFolders.includes(path)) return error("conflict", `${path} is not a git repository`, 409);
    this.opened.push(path);
    const repo = this.data.repos.find((r) => r.repo_root === path) ?? { repo_root: path, name: path.split("/").at(-1) ?? path };
    if (!this.data.repos.includes(repo)) this.data.repos = [...this.data.repos, repo];
    return json(repo);
  }

  /* Finish the fixes held by deferFixes: Codex's config is written (or refused). */
  completeFixes(): void {
    for (const run of this.pendingFixes.splice(0)) run();
  }

  /* routes_actions.setup_codex_hook: Codex trusts brindle's permission hook
     again, a job (202 {job, kind}); job.done with {trusted: true} once its
     config is written and the inbox's setup item is gone, or, with
     `fixFails`, job.failed with why (codex_hook_failed). A press while one
     runs gets that job back. */
  private fixCodexHook(): Response {
    if (this.fixing) return json({ job: this.fixing, kind: "setup.codex_hook" }, 202);
    const job = `job${this.nextJob++}`;
    this.fixing = job;
    this.fixes.push(job);
    const run = () => {
      this.fixing = null;
      if (this.fixFails) {
        this.publish("job.failed", {
          payload: { job, job_kind: "setup.codex_hook", error: { code: "codex_hook_failed", message: "couldn't trust brindle's Codex hook: codex isn't installed (no `codex` on PATH)" } },
        });
      } else {
        this.data.inbox = this.data.inbox.filter((i) => !(i.kind === "setup" && i.fix === "codex_hook_trust"));
        this.publish("job.done", { payload: { job, job_kind: "setup.codex_hook", result: { trusted: true } } });
      }
    };
    if (this.deferFixes) this.pendingFixes.push(run);
    else setTimeout(run, 0);
    return json({ job, kind: "setup.codex_hook" }, 202);
  }

  /* The engine dies (a crash, not an idle exit): every open socket drops
     (1006), and until `revive()` nothing answers: a request is refused, a
     new socket closes at once with 1006, as the shell reports a daemon it
     can't reach. The webview is expected to keep reconnecting on its own
     (store/sync.ts EventStream's backoff) and to catch up once it is back. */
  die(): void {
    this.dead = true;
    this.drop(1006);
  }

  /* The engine is back, as the shell restarts it: a new process (a new pid
     in its meta), the same token (the shell carries it), the same bus. */
  revive(): void {
    this.dead = false;
    this.data.meta = { ...this.data.meta, pid: this.data.meta.pid + 1 };
  }

  /* GET /providers/{p}/preflight: what the CLI still needs, as the engine's
     branch answers it; a provider with nothing listed is ready. */
  private preflightOf(provider: string) {
    const items = this.data.preflight[provider] ?? [];
    return { provider, ok: items.every((i) => i.done), items };
  }

  /* A launch of `provider` refused while its preflight has an open item. */
  private preflightRefusal(provider: string): Response | null {
    const got = this.preflightOf(provider);
    if (got.ok) return null;
    return json({ error: { code: "preflight", message: `${provider} needs setting up before brindle can launch it` }, preflight: got }, 409);
  }

  /* service/inbox.answer_permission (engine branch desktop/permission-items):
     the person's answer to the request the item showed, refused (409) for
     any other request, a changed one, or a choice the item doesn't offer
     (one only its terminal can answer). Answered, the item goes, the agent
     goes back to work, and permission.answered reaches the bus. */
  private answerPermission(id: string, body: Record<string, unknown>): Response {
    const d = this.data;
    if (typeof body.request !== "string") return error("invalid", "request is required", 400);
    if (typeof body.choice !== "string") return error("invalid", "choice is required", 400);
    if (!["allow_once", "allow_always", "deny"].includes(body.choice)) return error("invalid", "choice must be one of allow_once, allow_always, deny", 400);
    const item = d.inbox.find((i) => i.kind === "permission" && i.agent_id === id);
    if (!item) return error("not_found", `no agent ${id}`, 404);
    const p = item.permission;
    if (!p || p.request_id !== body.request || (p.digest ?? null) !== (body.digest ?? null)) {
      return error("conflict", `${id} isn't waiting on that request, as shown, any more: fetch the inbox again`, 409);
    }
    const offered = (p.choices ?? []).find((c) => c.choice === body.choice);
    if (!offered) return error("conflict", `${id}'s request offers no ${body.choice} brindle can give: answer it in its terminal (${p.terminal ?? "not offered"})`, 409);
    this.permissionAnswers.push({ agent: id, request: p.request_id, choice: body.choice });
    d.inbox = d.inbox.filter((i) => i !== item);
    const status = body.choice === "deny" ? "idle" : "processing";
    d.workspaces = d.workspaces.map((ws) => ({ ...ws, agents: ws.agents?.map((a) => (a.id === id ? { ...a, status } : a)) }));
    this.publish("permission.answered", { agent_id: id, workspace_id: item.workspace_id ?? null, payload: { request_id: p.request_id, choice: body.choice, via: p.via, cleared: true } });
    return json({ choice: body.choice, via: p.via, cleared: true, rule: body.choice === "allow_always" ? (offered.rule ?? null) : null });
  }

  /* routes_actions.edit_file (engine branch desktop/review-edit): the person's
     edit committed on the worker's branch, refused while an agent in the
     workspace is busy (409 busy) or when the file moved on (409 stale). */
  private editFile(id: string, body: Record<string, unknown>): Response {
    const ws = this.data.workspaces.find((w) => w.id === id);
    if (!ws) return error("not_found", `no workspace ${id}`, 404);
    if (!ws.base_branch) return error("invalid", "only a worker's workspace (its own branch and worktree) can be edited", 400);
    for (const key of ["path", "content", "base"]) if (typeof body[key] !== "string") return error("invalid", `${key} is required`, 400);
    const [path, content, base] = [body.path as string, body.content as string, (body.base as string).trim().toLowerCase()];
    if (path.startsWith("/") || path.split("/").some((p) => p === ".." || p.toLowerCase() === ".git")) return error("invalid", `'${path}' isn't a path inside the worktree`, 400);
    const busy = (ws.agents ?? []).filter((a) => BUSY.has(a.status)).map((a) => a.id);
    if (busy.length > 0) return error("busy", `${busy.join(", ")} is working in ${ws.id}; edit once it has stopped`, 409);
    const files = (this.data.files[id] ??= {});
    if (!(path in files)) return error("not_found", `no file ${path} in ${ws.id}`, 404);
    if (base.length < 7 || !fakeBlob(files[path]).startsWith(base)) return error("stale", `${path} has changed since the diff was shown; reload it`, 409);
    const changed = files[path] !== content;
    files[path] = content;
    this.edits.push({ workspace: id, path, content });
    const commit = changed ? `c${this.nextId++}`.padEnd(40, "0") : null;
    this.publish("workspace.edited", { workspace_id: id, payload: { path, commit } });
    return json({ workspace_id: id, path, commit, blob: fakeBlob(content), stat: this.data.diffs[id]?.stat ?? "" });
  }

  /* routes_actions.provider_login, set_key, upgrade_engine. */
  private action(path: string, body: Record<string, unknown>): Response | null {
    let m: RegExpMatchArray | null;
    if ((m = path.match(/^\/providers\/([^/]+)\/preflight\/([^/]+)$/))) {
      const items = this.data.preflight[m[1]] ?? [];
      const item = items.find((i) => i.id === m![2]);
      if (!item) return error("not_found", `no preflight item ${m[2]} for ${m[1]}`, 404);
      if (item.kind !== "trust" && item.kind !== "consent") return error("invalid", `${item.kind} is settled by the CLI itself, not here`, 400);
      item.done = body.accept === true;
      return json(this.preflightOf(m[1]));
    }
    if ((m = path.match(/^\/providers\/([^/]+)\/login$/))) {
      if (!(LOGIN_PROVIDERS as readonly string[]).includes(m[1])) return error("invalid", `provider must be one of ${LOGIN_PROVIDERS.join(", ")}`, 400);
      const methods = LOGIN_METHODS[m[1]] ?? [];
      if (body.method != null && !methods.includes(String(body.method))) return error("invalid", `${m[1]} sign-in methods: ${methods.join(", ") || "none"}`, 400);
      // restart: the login still running is ended (its window goes) and the CLI started afresh.
      if (body.restart === true) {
        for (const a of this.actions.filter((x) => x.kind === `login ${m![1]}` && x.alive)) {
          a.alive = false;
          delete this.data.terminals[a.id];
          this.windowGone(a.id);
        }
      }
      return this.openAction("login", m[1], typeof body.method === "string" ? body.method : undefined);
    }
    if ((m = path.match(/^\/providers\/([^/]+)\/install$/))) return this.installProvider(m[1]);
    if ((m = path.match(/^\/providers\/([^/]+)\/use-mine$/))) return this.pickProviderInstall(m[1], body);
    if (path === "/repos/clone") return this.cloneRepo(body);
    if (path === "/repos/open") return this.openRepo(body);
    if (path === "/terminals/shell") return this.openShell(body);
    if ((m = path.match(/^\/terminals\/([^/]+)\/input$/))) return this.terminalInput(m[1], body);
    if ((m = path.match(/^\/keys\/([^/]+)\/set$/))) {
      const name = m[1];
      if (!KEY_NAME.test(name) || RESERVED_KEYS.has(name)) return error("invalid", `${name} isn't a key agents can be given`, 400);
      if (body.repo != null && typeof body.repo !== "string") return error("invalid", "repo has the wrong type", 400);
      return this.openAction("key", name);
    }
    if (path === "/engine/upgrade") return this.upgradeEngine();
    if (path === "/setup/codex-hook") return this.fixCodexHook();
    return null;
  }

  /* team_policy.Access.refusal: why this person may not run `cli`, or null. */
  private providerRefusal(cli: string): string | null {
    const policy = this.data.routingPolicy;
    if (!policy || policy.error) return null;
    if (policy.allowed_providers && !policy.allowed_providers.includes(cli)) {
      return `org ${policy.org_id} doesn't allow ${CLI_NAMES[cli] ?? cli}; it allows ${policy.allowed_providers.map((c) => CLI_NAMES[c] ?? c).join(", ")}`;
    }
    if (policy.managed_routes?.length && cli in CLI_NAMES && !policy.managed_routes.includes(cli)) {
      return `org ${policy.org_id} manages its agents' routes and gives ${CLI_NAMES[cli]} none`;
    }
    return null;
  }

  /* service/routing.py RoutingPolicy.refusal: why a worker on `p` would be refused, or null. */
  private refusal(p: RoutingProfile): string | null {
    const policy = this.data.routingPolicy;
    if (!policy) return null;
    if (policy.error) return policy.error;
    const cli = this.providerRefusal(p.provider);
    if (cli) return cli;
    if (policy.allowed_models && !(p.model && policy.allowed_models.includes(p.model))) {
      return `profile '${p.name}' runs ${p.model ?? "no declared model"}; org ${policy.org_id} allows only ${policy.allowed_models.join(", ") || "no models"}`;
    }
    if (policy.allowed_profiles && !policy.allowed_profiles.includes(p.name)) {
      return `org ${policy.org_id} allows only the profiles ${policy.allowed_profiles.join(", ") || "(none)"}, not '${p.name}'`;
    }
    return null;
  }

  /* RoutingPolicy.to_dto. */
  private policyDto(): RoutingPolicyDTO {
    const policy = this.data.routingPolicy;
    if (!policy) return { applies: false, editable: true, models: {}, locked: [] };
    if (policy.error) return { applies: true, org_id: policy.org_id, editable: false, reason: policy.error, models: {}, locked: [] };
    const models: Record<string, string[]> = Object.fromEntries((policy.allowed_models ?? []).map((m) => [m, []]));
    for (const p of this.data.routingProfiles) {
      if (!p.model || this.refusal(p)) continue;
      (models[p.model] ??= []).push(p.name);
    }
    const locked: RoutingLockDTO[] = [];
    for (const cli of Object.keys(CLI_NAMES)) {
      const why = this.providerRefusal(cli);
      if (why) locked.push({ kind: "provider", name: cli, reason: why });
    }
    for (const cli of policy.managed_routes ?? []) {
      locked.push({ kind: "route", name: cli, reason: `org ${policy.org_id} routes ${CLI_NAMES[cli] ?? cli} through its own gateway; only an org admin can change these routes` });
    }
    if (policy.deny_personal_keys) locked.push({ kind: "keys", name: "personal_api_keys", reason: `org ${policy.org_id} doesn't allow personal API keys` });
    return {
      applies: true,
      org_id: policy.org_id,
      editable: true,
      allowed_providers: policy.allowed_providers ?? null,
      allowed_models: policy.allowed_models ?? null,
      allowed_profiles: policy.allowed_profiles ?? null,
      deny_personal_keys: policy.deny_personal_keys ?? false,
      models,
      locked,
    };
  }

  /* routes_actions.patch_config: `values` into the user's file or the repo's local one. */
  private patchConfig(body: Record<string, unknown>): Response {
    const scope = body.scope ?? "user";
    if (scope !== "user" && scope !== "repo") return error("invalid", "scope must be user or repo", 400);
    if (scope === "repo" && typeof body.repo !== "string") return error("invalid", "repo is required", 400);
    const values = body.values;
    if (values == null) return error("invalid", "values is required", 400);
    if (typeof values !== "object" || Array.isArray(values)) return error("invalid", "values has the wrong type", 400);
    const entries = Object.entries(values as Record<string, unknown>);
    const unknown = entries.map(([k]) => k).filter((k) => !(k in REPO_CONFIG_TYPES)).sort();
    if (unknown.length > 0) return error("invalid", `unknown setting(s): ${unknown.join(", ")}`, 400);
    for (const [key, value] of entries) {
      const type = REPO_CONFIG_TYPES[key];
      if (!fits(value, type)) return error("invalid", `${key} must be a ${type === "number" ? "int" : type === "boolean" ? "bool" : type === "string" ? "str" : type}`, 400);
    }
    // config.set_user / set_local store the value as given: a null is a stored null, not an unset.
    const file = scope === "user" ? this.data.config.user : this.data.config.local;
    for (const [key, value] of entries) file[key] = value;
    return json({ updated: entries.map(([k]) => k).sort(), path: scope === "user" ? "/home/u/.brindle/config.json" : `${String(body.repo)}/.brindle/config.local.json` });
  }

  /* Engine branch desktop/kb-sources: a folder added to the repo's knowledge
     (POST /kb/sources?repo= {path, private?}), a job: the source is listed at
     once with nothing indexed, and job.done (job_kind "kb.add") once its
     documents are. A folder already added answers the same source. */
  private addKbSource(repo: string | undefined, body: Record<string, unknown>): Response {
    if (!repo) return error("invalid", "repo is required", 400);
    if (typeof body.path !== "string" || !body.path.trim()) return error("invalid", "path is required", 400);
    if (body.private !== undefined && typeof body.private !== "boolean") return error("invalid", "private has the wrong type", 400);
    const path = body.path.replace(/\/+$/, "");
    const job = `job${this.nextJob++}`;
    let source = this.data.kbSources.find((s) => s.kind === "folder" && s.scope === path);
    if (!source) {
      source = { id: `${this.nextId++}`.padStart(32, "a"), kind: "folder", scope: path, mode: "indexed", visibility: body.private ? "private" : "repo", added_at: Date.now() / 1000, quarantined: false };
      this.data.kbSources.push(source);
    }
    const id = source.id;
    setTimeout(() => {
      const s = this.data.kbSources.find((x) => x.id === id);
      if (s) s.docs = this.kbDocsPerFolder;
      if (this.jobEvents) this.publish("job.done", { payload: { job, job_kind: "kb.add", result: { source: id, indexed: this.kbDocsPerFolder } } });
    }, 0);
    return json({ job, kind: "kb.add" }, 202);
  }

  /* POST /kb/sources/{id}/sync?repo=: the source indexed again, a job. */
  private syncKbSource(repo: string | undefined, id: string): Response {
    if (!repo) return error("invalid", "repo is required", 400);
    const source = this.data.kbSources.find((s) => s.id === id);
    if (!source) return error("not_found", `no source ${id}`, 404);
    const job = `job${this.nextJob++}`;
    this.kbSyncs.push(id);
    setTimeout(() => {
      const s = this.data.kbSources.find((x) => x.id === id);
      if (s) s.docs = this.kbDocsPerFolder;
      if (this.jobEvents) this.publish("job.done", { payload: { job, job_kind: "kb.sync", result: { source: id, indexed: this.kbDocsPerFolder, unchanged: 0, tombstoned: 0 } } });
    }, 0);
    return json({ job, kind: "kb.sync" }, 202);
  }

  /* DELETE /kb/sources/{id}?repo=: the source and everything indexed from it are gone. */
  private removeKbSource(repo: string | undefined, id: string): Response {
    if (!repo) return error("invalid", "repo is required", 400);
    const at = this.data.kbSources.findIndex((s) => s.id === id);
    if (at < 0) return error("not_found", `no source ${id}`, 404);
    this.data.kbSources.splice(at, 1);
    this.kbRemovals.push(id);
    return json({ removed: id });
  }

  /* PATCH /user-settings (engine branch desktop/user-settings): the keys
     given, each a value the engine knows; the whole document comes back. */
  private patchUserSettings(body: Record<string, unknown>): Response {
    const mode = body.claude_permission_mode;
    if (mode !== undefined && mode !== "default" && mode !== "auto") return error("invalid", "claude_permission_mode must be default or auto", 400);
    const unknown = Object.keys(body).filter((k) => k !== "claude_permission_mode");
    if (unknown.length > 0) return error("invalid", `unknown setting(s): ${unknown.sort().join(", ")}`, 400);
    if (mode !== undefined) this.data.userSettings = { ...this.data.userSettings, claude_permission_mode: mode };
    return json(this.data.userSettings);
  }

  /* PUT /debug: staff only; `enabled` a boolean. */
  private putDebug(body: Record<string, unknown>): Response {
    if (!this.data.debug.staff) return error("forbidden", "debug logging is for brindle staff", 403);
    if (typeof body.enabled !== "boolean") return error("invalid", "enabled must be true or false", 400);
    this.data.debug = { ...this.data.debug, enabled: body.enabled };
    return json(this.data.debug);
  }

  /* routes_actions.record_decision: refused (409) while an accepted decision
     on the same topic covers the same paths, unless `supersedes` names it. */
  private recordDecision(body: Record<string, unknown>): Response {
    for (const key of ["repo", "topic", "decision"]) {
      if (body[key] == null) return error("invalid", `${key} is required`, 400);
      if (typeof body[key] !== "string") return error("invalid", `${key} has the wrong type`, 400);
    }
    const topic = String(body.topic);
    const paths = Array.isArray(body.paths) ? body.paths.map(String).filter(Boolean) : [];
    const supersedes = typeof body.supersedes === "string" ? body.supersedes : null;
    const old = supersedes ? this.data.decisions.find((d) => d.id.startsWith(supersedes)) : null;
    if (supersedes && !old) return error("invalid", `No decision ${supersedes} in this repo.`, 400);
    const overlaps = (a: string[], b: string[]) => a.length === 0 || b.length === 0 || a.some((x) => b.includes(x));
    const clash = this.data.decisions.find((d) => d.topic.toLowerCase() === topic.toLowerCase() && overlaps(d.scope, paths) && d !== old);
    if (clash) {
      return error("conflict", `an accepted decision on this topic already covers these paths (${clash.id.slice(0, 8)}); pass supersedes to replace it`, 409);
    }
    const made: DecisionDTO = {
      id: `d${this.nextId++}`.padEnd(32, "0"),
      repo_key: String(body.repo),
      topic,
      body: String(body.decision),
      rationale: typeof body.rationale === "string" ? body.rationale : "",
      scope: paths,
      status: "accepted",
      created_at: Date.now() / 1000,
    };
    this.data.decisions = [...this.data.decisions.filter((d) => d !== old), made];
    this.publish("decision.recorded", { payload: { decision_id: made.id, topic, supersedes: old?.id ?? null } });
    return json({ decision: { ...made, supersedes: old?.id ?? null }, told: [] });
  }

  /* routes_actions.change_permissions. */
  private changePermissions(body: Record<string, unknown>): Response {
    const op = body.op;
    if (op == null) return error("invalid", "op is required", 400);
    if (op === "allow" || op === "deny") {
      for (const key of ["kind", "match"]) if (typeof body[key] !== "string") return error("invalid", `${key} is required`, 400);
      const kind = String(body.kind);
      const matchType = typeof body.match_type === "string" ? body.match_type : "exact";
      if (!PERMISSION_KINDS.includes(kind)) return error("invalid", `kind must be one of ${PERMISSION_KINDS.join(", ")}`, 400);
      if (!MATCH_TYPES.includes(matchType)) return error("invalid", `match_type must be one of ${MATCH_TYPES.join(", ")}`, 400);
      const rule: PermissionRule = { id: ruleId(kind, matchType, String(body.match), op), kind, match: String(body.match), match_type: matchType, decision: op, source: "user" };
      if (!this.permissionRules.some((r) => r.id === rule.id)) this.permissionRules.push(rule);
      return json({ rule: rule.id, describe: `${op} ${describeRule(rule)}` });
    }
    if (op === "forget" || op === "accept") {
      const id = body.id;
      if (typeof id !== "string") return error("invalid", "id is required", 400);
      const at = this.permissionRules.findIndex((r) => r.id === id);
      if (at < 0) return error("not_found", `no rule or suggestion ${id} of yours`, 404);
      const [rule] = this.permissionRules.splice(at, 1);
      return json({ rule: id, describe: op === "forget" ? `forgot ${describeRule(rule)}` : `allow ${describeRule(rule)}` });
    }
    if (op === "reset") {
      this.permissionRules.splice(0);
      return json({ reset: true });
    }
    return error("invalid", "op must be allow, deny, forget, accept or reset", 400);
  }

  private editPermissionRules(body: PermissionEditorRequest & { proposal?: string }, save: boolean): Response {
    const state = this.permissionEditor;
    if (!state.restrictions.editable) return error("forbidden", state.restrictions.reason, 403);
    if (body.revision !== state.revision) return error("stale", "Permission rules changed; review again.", 409);
    if (save) {
      const proposal = this.permissionProposal;
      const { proposal: digest, ...request } = body;
      if (!proposal || digest !== proposal.review.proposal || JSON.stringify(request) !== JSON.stringify(proposal.request)) return error("stale", "Proposal changed; review again.", 409);
      if (!proposal.review.can_save) return error("conflict", "Unresolved permission conflict.", 409);
      this.permissionEditor = { ...state, rules: structuredClone(proposal.review.rules), revision: `editor-${++this.permissionSequence}` };
      this.permissionProposal = null;
      return json({ revision: this.permissionEditor.revision, rules: this.permissionEditor.rules, changes: proposal.review.changes, restrictions: state.restrictions, limitations: state.limitations });
    }
    const existing = state.rules.find((rule) => rule.id === body.id);
    const sameTarget = existing && existing.kind === body.rule?.kind && existing.match === body.rule.match && existing.match_type === body.rule.match_type;
    if (body.operation === "upsert" && body.rule?.kind === "mcp" && !sameTarget && (body.rule.match_type !== "exact" || !state.catalog.some((action) => action.tool === body.rule?.match))) return error("invalid", "Choose an exact tool from the available MCP inventory.", 400);
    let review = this.permissionReviews.shift();
    if (!review) {
      const before = state.rules.find((rule) => rule.id === body.id) ?? null;
      if (body.id && (!before || !before.editable || before.protected)) return error("forbidden", "This rule cannot be edited.", 403);
      const after: PermissionEditorRule | null = body.operation === "remove" ? null : {
        id: before?.id ?? `rule-${++this.permissionSequence}`, kind: "bash", match: "", match_type: "exact", decision: "ask", repo_root: null,
        expires_at: null, exclusions: [], source: "user", created: 1, editable: true, protected: false, ...body.rule,
      };
      const rules = state.rules.filter((rule) => rule.id !== before?.id);
      if (after) rules.push(after);
      review = { revision: state.revision, proposal: `proposal-${++this.permissionSequence}`, rules, conflicts: [], unresolved: [], can_save: true,
        changes: [{ type: after ? before ? "updated" : "added" : "removed", id: (after ?? before)!.id, before, after }] };
    }
    this.permissionProposal = { request: structuredClone(body), review: structuredClone(review) };
    return json(review);
  }

  /* routes_actions.account_login: a job; its progress carries the authorize
     URL (the app opens it), or the device code's lines; job.done once the
     tokens are stored. */
  private accountLogin(body: Record<string, unknown>): Response {
    const device = body.device === true;
    // restart: the sign-in still waiting on the browser is ended (its job fails as restarted), a fresh one starts.
    if (body.restart === true) {
      for (const old of this.pendingLogins.splice(0)) {
        this.publish("job.failed", { payload: { job: old.job, job_kind: "account.login", error: { code: "restarted", message: "a new sign-in was started" } } });
      }
    }
    const job = `job${this.nextId++}`;
    const finish = () => {
      this.data.account = this.data.accountAfterLogin;
      this.publish("job.done", { payload: { job, job_kind: "account.login", result: { signed_in: true } } });
    };
    setTimeout(() => {
      if (device) {
        this.publish("job.progress", { payload: { job, text: "Open https://pawdelta.com/device and enter the code ABCD-EFGH." } });
      } else {
        this.publish("job.progress", { payload: { job, url: this.data.loginUrl } });
      }
      if (this.deferLogin) this.pendingLogins.push({ job, finish });
      else finish();
    }, 0);
    return json({ job, kind: "account.login" }, 202);
  }

  /* The three settings files a repo is read with: the user's, and the repo's own
     shared and local ones where it has them. */
  private routingFiles(repo: string): Record<RoutingScope, RoutingFile> {
    const own = this.data.routingByRepo[repo];
    if (!own) return this.data.routingConfig;
    own.repo ??= {};
    own.local ??= {};
    return { user: this.data.routingConfig.user, repo: own.repo, local: own.local };
  }

  /* config.scope_paths: the file of each settings scope. */
  private scopePath(scope: RoutingScope, repo: string): string {
    if (scope === "user") return "/home/u/.brindle/config.json";
    return `${repo}/.brindle/${scope === "repo" ? "config.json" : "config.local.json"}`;
  }

  /* GET /routing (routes_read.get_routing): each tier's list and the scope it
     comes from, as load_repo_config reads them (local over repo over user over
     the default, per tier), the reviewer and the excluded providers likewise
     (config.setting_source), and every profile with `excluded` from the latter. */
  private routing(repo: string): RoutingDTO {
    const files = this.routingFiles(repo);
    const precedence: RoutingScope[] = ["local", "repo", "user"];
    const byName = new Map(this.data.routingProfiles.map((p) => [p.name, p]));
    const refusals = new Map(this.data.routingProfiles.map((p) => [p.name, this.refusal(p)]));
    // routes_read.get_routing as_model: each entry in model terms.
    const asModel = (name: string) => {
      const p = byName.get(name);
      return { profile: name, provider: p?.provider ?? null, provider_name: p ? (CLI_NAMES[p.provider] ?? p.provider) : null, model: p?.model ?? null, allowed: (refusals.get(name) ?? null) === null };
    };
    const tiers: Record<string, RoutingTierDTO> = {};
    for (const tier of WEIGHTS) {
      const scope = precedence.find((s) => files[s].routing?.[tier]);
      const values = scope ? files[scope].routing![tier] : DEFAULT_ROUTING[tier];
      tiers[tier] = { values, source: scope ?? "default", models: values.map(asModel) };
    }
    const setting = <K extends "reviewer" | "exclude_providers">(key: K, fallback: string[]): RoutingSettingDTO => {
      const scope = precedence.find((s) => files[s][key] !== undefined);
      if (!scope) return { values: fallback, source: "default" };
      const value = files[scope][key]!;
      return { values: Array.isArray(value) ? value : [value], source: scope };
    };
    const reviewer = setting("reviewer", [DEFAULT_REVIEWER]);
    const excluded = setting("exclude_providers", []);
    // routes_read._routing_kinds: each kind's pick, fallbacks, lock and what learning runs instead (never for a locked one).
    const lockedIn = precedence.find((s) => files[s].routing_locked !== undefined);
    const locked = new Set(lockedIn ? files[lockedIn].routing_locked : []);
    const kind = (k: string, names: string[]): RoutingKindDTO & KindExtras => {
      const learned = this.data.learned[k];
      return {
        model: names[0] ?? null,
        fallbacks: names.slice(1),
        locked: locked.has(k),
        learned: learned && !locked.has(k) && learned !== names[0] ? learned : null,
        ...files.user.kinds?.[k],
        ...files.repo.kinds?.[k],
        ...files.local.kinds?.[k],
      };
    };
    const kinds: Record<string, RoutingKindDTO & KindExtras> = Object.fromEntries(WEIGHTS.map((t) => [t, kind(t, tiers[t].values)]));
    const fallbacksOf = (k: string) => files[precedence.find((s) => files[s].kind_fallbacks?.[k]) ?? "user"].kind_fallbacks?.[k] ?? [];
    kinds.reviewer = kind("reviewer", [...reviewer.values, ...fallbacksOf("reviewer")]);
    kinds.supervisor = { ...kind("supervisor", files.user.supervisor ? [files.user.supervisor, ...fallbacksOf("supervisor")] : []), learned: null };
    return {
      tiers,
      reviewer,
      exclude_providers: excluded,
      kinds,
      profiles: this.data.routingProfiles.map((p) => ({
        ...p,
        provider_name: CLI_NAMES[p.provider] ?? p.provider,
        excluded: excluded.values.includes(p.provider),
        allowed: refusals.get(p.name) === null,
        refusal: refusals.get(p.name) ?? null,
      })),
      policy: this.policyDto(),
    };
  }

  /* POST /profiles (ahead of the engine; `brindle profile new` writes the
     same file): a custom agent in ~/.brindle/agents/<name>.md, listed by
     GET /profiles and GET /routing from then on, costed as the provider's
     other profiles on that model are, and checked as profile lint would. */
  private createProfile(body: Record<string, unknown>): Response {
    const d = this.data;
    const { repo, name, description, provider, model, effort, instructions, rules } = body;
    if (typeof repo !== "string") return error("invalid", "repo is required", 400);
    if (typeof name !== "string" || !/^[a-z0-9][a-z0-9-]*$/.test(name)) return error("invalid", "name must be lowercase letters, digits and dashes", 400);
    if ([...d.profiles, ...d.routingProfiles].some((p) => p.name === name)) return error("conflict", `there's already a profile called ${name}`, 409);
    if (typeof provider !== "string" || !PROVIDERS.includes(provider)) return error("invalid", `no such provider: ${String(provider)}`, 400);
    if (typeof model !== "string" || !model) return error("invalid", "model is required", 400);
    const packs = Array.isArray(rules) ? rules.map(String) : [];
    const same = d.routingProfiles.filter((p) => p.provider === provider);
    const like = same.find((p) => p.model === model);
    const lint_errors = packs.filter((r) => !d.rulePacks.some((x) => x.name === r)).map((r) => `no rule pack named ${r}`);
    const file = `/home/u/.brindle/agents/${name}.md`;
    const listed: ProfileDTO = { name, description: typeof description === "string" ? description : "", provider, model, effort: typeof effort === "string" ? effort : null };
    d.profiles = [...d.profiles, listed];
    if (d.profilesByRepo[repo]) d.profilesByRepo[repo] = [...d.profilesByRepo[repo], listed];
    d.routingProfiles = [
      ...d.routingProfiles,
      { name, provider, model, cost_tier: like?.cost_tier ?? "medium", installed: same.some((p) => p.installed), signed_in: same.some((p) => p.signed_in), lint_errors, lint_warnings: [] },
    ];
    d.profileDetails[name] = {
      source: file,
      rules: packs.map((r) => d.rulePacks.find((x) => x.name === r) ?? { name: r, description: "", source: "missing" }),
      prompt: typeof instructions === "string" ? instructions : "",
      lint: { errors: lint_errors, warnings: [] },
    };
    return json({ name, path: file }, 201);
  }

  /* PUT /routing (routes_actions.put_routing): the tiers given replace theirs
     in the scope's file (the others stay), as do a reviewer or an exclusion
     list given; every name must be a known profile, every provider a known one. */
  private putRouting(body: Record<string, unknown>): Response {
    const repo = body.repo;
    if (typeof repo !== "string") return error("invalid", "repo is required", 400);
    const scope = body.scope;
    if (typeof scope !== "string") return error("invalid", "scope is required", 400);
    if (!ROUTING_SCOPES.includes(scope as RoutingScope)) return error("invalid", "scope must be user, local or repo", 400);
    const { exclude_providers: excluded, kinds } = body as { exclude_providers?: unknown; kinds?: unknown };
    let { tiers, reviewer } = body as { tiers?: unknown; reviewer?: unknown };
    if (tiers != null && (typeof tiers !== "object" || Array.isArray(tiers))) return error("invalid", "tiers has the wrong type", 400);
    if (reviewer != null && typeof reviewer !== "string") return error("invalid", "reviewer has the wrong type", 400);
    if (excluded != null && !Array.isArray(excluded)) return error("invalid", "exclude_providers has the wrong type", 400);
    if (kinds != null && (typeof kinds !== "object" || Array.isArray(kinds))) return error("invalid", "kinds has the wrong type", 400);
    // Per kind (routes_actions.put_routing's `kinds`): a weight's model and fallbacks become its tier list,
    // the reviewer's model the reviewer, the supervisor's the user's pick. A choice locks the kind unless `locked` is given.
    const lockChanges = new Map<string, boolean>();
    const extras: Record<string, KindExtras> = {};
    const kindFallbacks: Record<string, string[]> = {};
    let supervisor: string | null = null;
    const current = this.routing(repo);
    for (const [k, spec] of Object.entries((kinds ?? {}) as Record<string, unknown>)) {
      if (!ROUTING_KINDS.includes(k) || typeof spec !== "object" || spec === null) return error("invalid", `kind must be one of ${ROUTING_KINDS.join(", ")}, not '${k}'`, 400);
      const { model, fallbacks, locked, effort, instructions } = spec as Record<string, unknown>;
      if (locked != null) {
        if (typeof locked !== "boolean") return error("invalid", `${k}: locked must be true or false`, 400);
        lockChanges.set(k, locked);
      } else if (model != null || fallbacks != null) lockChanges.set(k, true);
      if (model != null && (typeof model !== "string" || !model)) return error("invalid", `${k}: model must be a name`, 400);
      if (fallbacks != null && (!Array.isArray(fallbacks) || !fallbacks.every((n) => typeof n === "string"))) {
        return error("invalid", `${k}: fallbacks must be a list of profile names`, 400);
      }
      if ((WEIGHTS as readonly string[]).includes(k) && (model != null || fallbacks != null)) {
        const old = current.tiers[k].values;
        const names = [model != null ? (model as string) : old[0], ...(fallbacks != null ? (fallbacks as string[]) : old.slice(1))];
        tiers = { ...((tiers ?? {}) as Record<string, string[]>), [k]: [...new Set(names.filter(Boolean))] };
      } else {
        // The reviewer and the supervisor: a model (a known profile) and fallbacks of their own.
        if (k === "reviewer" && model != null) reviewer = model;
        if (k === "supervisor" && model != null) supervisor = model as string;
        if (fallbacks != null) kindFallbacks[k] = [...new Set(fallbacks as string[])].filter((n) => n !== (model ?? current.kinds?.[k]?.model));
      }
      // Instructions: text, or null to clear them (like the engine's PUT /v1/routing); blank text is refused.
      if (instructions != null && (typeof instructions !== "string" || !instructions.trim())) {
        return error("invalid", `${k}: instructions must be text, or null to clear them`, 400);
      }
      if (effort != null || instructions !== undefined) {
        if (effort != null && !["low", "medium", "high"].includes(effort as string)) return error("invalid", `${k}: effort must be low, medium or high`, 400);
        extras[k] = { ...(effort != null ? { effort: effort as string } : {}), ...(instructions !== undefined ? { instructions: instructions as string | null } : {}) };
      }
    }
    const known = new Set(this.data.routingProfiles.map((p) => p.name));
    const named: string[] = [...(reviewer ? [reviewer as string] : []), ...(supervisor ? [supervisor] : []), ...Object.values(kindFallbacks).flat()];
    for (const [tier, names] of Object.entries((tiers ?? {}) as Record<string, unknown>)) {
      if (!(WEIGHTS as readonly string[]).includes(tier)) return error("invalid", `tier must be one of ${WEIGHTS.join(", ")}, not '${tier}'`, 400);
      if (!Array.isArray(names) || names.length === 0 || !names.every((n) => typeof n === "string")) {
        return error("invalid", `${tier} must be a non-empty list of profile names`, 400);
      }
      named.push(...(names as string[]));
    }
    const unknown = [...new Set(named.filter((n) => !known.has(n)))].sort();
    if (unknown.length > 0) return error("invalid", `no such profile(s): ${unknown.join(", ")}`, 400);
    if (excluded) {
      const bad = [...new Set((excluded as unknown[]).map(String).filter((p) => !PROVIDERS.includes(p)))].sort();
      if (bad.length > 0) return error("invalid", `no such provider(s): ${bad.join(", ")}`, 400);
    }
    // A profile the org's policy refuses, or any change while the policy can't be read: 403.
    const policy = this.data.routingPolicy;
    if (policy?.error) return error("forbidden", `routing can't be changed now: ${policy.error}`, 403);
    for (const name of new Set(named)) {
      const why = this.refusal(this.data.routingProfiles.find((p) => p.name === name)!);
      if (why) return error("forbidden", why, 403);
    }
    const file = this.routingFiles(repo)[scope as RoutingScope];
    const updated: string[] = [];
    if (tiers && Object.keys(tiers).length > 0) {
      file.routing = { ...file.routing, ...(tiers as Record<string, string[]>) };
      updated.push("routing");
    }
    if (reviewer != null) {
      file.reviewer = reviewer as string;
      updated.push("reviewer");
    }
    if (excluded != null) {
      file.exclude_providers = (excluded as unknown[]).map(String);
      updated.push("exclude_providers");
    }
    if (Object.keys(kindFallbacks).length > 0) {
      file.kind_fallbacks = { ...file.kind_fallbacks, ...kindFallbacks };
      updated.push("kind_fallbacks");
    }
    if (lockChanges.size > 0) {
      const now = new Set(file.routing_locked ?? []);
      for (const [k, on] of lockChanges) on ? now.add(k) : now.delete(k);
      file.routing_locked = [...now].sort();
      updated.push("routing_locked");
    }
    if (Object.keys(extras).length > 0) {
      file.kinds = { ...file.kinds };
      for (const [k, x] of Object.entries(extras)) file.kinds[k] = { ...file.kinds[k], ...x };
      updated.push("kinds");
    }
    let path = this.scopePath(scope as RoutingScope, repo);
    if (supervisor !== null) {
      this.data.routingConfig.user.supervisor = supervisor;
      path = this.scopePath("user", repo);
      updated.push("supervisor");
    }
    if (updated.length === 0) return error("invalid", "give tiers, reviewer, exclude_providers or kinds", 400);
    return json({ updated, path });
  }

  client(token = this.token): DaemonClient {
    return new DaemonClient({ baseUrl: BASE_URL, token, fetch: this.fetch });
  }

  /* The live /v1/events sockets that passed the guard. */
  get open(): FakeSocket[] {
    return this.sockets.filter((s) => s.authed && !s.closed);
  }

  /* The open views on a pane (an agent's or an action terminal's id). */
  viewers(pane: string): FakeView[] {
    return this.views.filter((v) => !v.closed && v.pane === pane);
  }

  /* The pane prints `text`: every view on it gets the bytes, as its tmux client draws them. */
  print(pane: string, text: string): void {
    const bytes = new TextEncoder().encode(text);
    for (const v of this.viewers(pane)) queueMicrotask(() => v.closed || v.handlers.onData(bytes));
  }

  /* The pane's window went (its agent closed, killed or rewound): the tmux
     client of each view on it exits, its view session gone with the window. */
  windowGone(pane: string): void {
    for (const v of this.viewers(pane)) {
      v.closed = true;
      queueMicrotask(() => v.handlers.onExit(0));
    }
  }

  /* The pane at a tmux target: an action terminal's, or the agent's whose window it is. */
  private paneAt(at: PtyViewTarget): string | null {
    const action = this.actions.find((a) => a.target === at.target && at.server === ACTION_SERVER);
    if (action) return action.id;
    const agents = [...this.data.workspaces.flatMap((w) => w.agents ?? []), ...this.data.closed];
    return agents.find((a) => a.window === at.target && (a.tmux_server ?? null) === at.server)?.id ?? null;
  }

  /* A view of the pane at `at`, as the shell's pty view opens one: the pane
     drawn once it opens, then what it prints; no pane there, and it says why. */
  openView: PtyViewOpener = (at, size, handlers) => {
    const view: FakeView = { at, pane: this.paneAt(at), size, handlers, written: [], resizes: [], focuses: 0, closed: false };
    this.views.push(view);
    queueMicrotask(() => {
      if (view.closed) return;
      const pane = view.pane ? this.data.terminals[view.pane] : undefined;
      if (!pane) {
        view.closed = true;
        handlers.onError(`can't find pane: ${at.target}`);
        return;
      }
      handlers.onData(new TextEncoder().encode(drawn(pane)));
    });
    return {
      write: (data) => void (view.closed || view.written.push(data)),
      resize: (cols, rows) => void (view.closed || view.resizes.push([cols, rows])),
      focus: () => void (view.closed || view.focuses++),
      close: () => void (view.closed = true),
    };
  };

  /* PtyViews over these panes, as the shell's would show them (`status` as pty_view_status would say). */
  ptyViews(status: PtyStatus = { kind: "ready", tmux: "tmux 3.8" }): PtyViews {
    const copy = (text: string) => {
      this.copied.push(text);
      this.clipboard = text;
      return Promise.resolve();
    };
    return { status, open: this.openView, copy, paste: () => Promise.resolve(this.clipboard), recheck() {} };
  }

  /* Append a bus row and push it to every subscribed client. */
  publish(kind: string, fields: Partial<Omit<BusEvent, "type" | "id" | "kind">> = {}): BusEvent {
    const event: BusEvent = {
      type: "event",
      id: this.nextId++,
      ts: fields.ts ?? Date.now() / 1000,
      kind,
      repo_root: null,
      root_id: null,
      agent_id: null,
      workspace_id: null,
      task_id: null,
      payload: {},
      ...fields,
    };
    this.events.push(event);
    for (const s of this.open) if (this.matches(s, event)) s.deliver(event);
    return event;
  }

  /* Push a message no process publishes (liveness, git, status). */
  send(message: StreamMessage): void {
    for (const s of this.open) s.deliver(message);
  }

  /* Delete the bus rows with ids up to `id`, as the 24 h cull does. */
  prune(id: number): void {
    this.events = this.events.filter((e) => e.id > id);
  }

  /* Whether the agent's pane is live (a running agent, as close's `stopped` reports it). */
  private alive(agent: string): boolean {
    return this.data.terminals[agent]?.live === true;
  }

  /* Drop every connection, as a daemon restart does. */
  drop(code = 1006): void {
    for (const s of this.sockets) s.close(code);
  }

  connect = (url: string): FakeSocket => {
    const socket = new FakeSocket(this, url);
    this.sockets.push(socket);
    // Dead: the connection is refused (closed before it opens, as the shell's 1006 says).
    if (this.dead) socket.close(1006);
    return socket;
  };

  authenticate(socket: FakeSocket, frame: string): void {
    if (frame !== this.token) {
      socket.close(WS_UNAUTHORIZED);
      return;
    }
    socket.authed = true;
    for (const m of this.replay(socket)) socket.deliver(m);
  }

  private replay(socket: FakeSocket): StreamMessage[] {
    const since = socket.since;
    const newest = this.events.at(-1)?.id ?? 0;
    const oldest = this.events[0]?.id;
    const gap =
      since > newest ||
      (since > 0 && oldest !== undefined && oldest > since + 1) ||
      (since === 0 && oldest !== undefined && oldest > 1);
    if (gap) return [{ type: "resnapshot", reason: "events_lost" }];
    return this.events.filter((e) => e.id > since && this.matches(socket, e));
  }

  private matches(socket: FakeSocket, event: BusEvent): boolean {
    return socket.repo === null || event.repo_root === socket.repo;
  }

  /* The goal's POST route, answering as the engine does (routes_actions.py):
     /goal/enabled answers {enabled}. It needs the session's `root`, as the
     engine's _root lookup does. */
  private post(path: string, body: Record<string, unknown>): Response {
    if (typeof body.root !== "string") return error("invalid", "root is required", 400);
    const goal = this.data.goal;
    if (!goal || body.root !== goal.root_id) return error("not_found", `no agent ${body.root}`, 404);
    if (path === "/goal/enabled") {
      if (typeof body.enabled !== "boolean") return error("invalid", "enabled is required", 400);
      goal.enabled = body.enabled;
      return json({ enabled: goal.enabled });
    }
    return error("not_found", `no route ${path}`, 404);
  }

  fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input));
    const query = Object.fromEntries(url.searchParams);
    // The routes see the path percent-decoded, as Starlette's do (uvicorn
    // unquotes it before routing), so "%2F" is a "/" by the time it matches.
    const path = decodeURIComponent(url.pathname).replace(/^\/v1/, "");
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
    this.requests.push({ method, path, query, body });
    if (this.dead) throw new TypeError("the brindle daemon is unreachable: connection refused");
    const auth = new Headers(init?.headers).get("authorization") ?? "";
    if (auth !== `Bearer ${this.token}`) {
      return error("unauthorized", "missing or wrong token (see ~/.brindle/daemon.json)", 401);
    }
    if (method === "GET" && path === "/permission-editor") return json(this.permissionEditor);
    if (method === "POST" && path === "/permission-editor/verify") {
      const tool = (body as { tool?: string }).tool;
      const action = this.permissionEditor.catalog.find((item) => item.tool === tool) ?? null;
      return json({ verified: action !== null, action });
    }
    if (method === "POST" && (path === "/permission-editor/review" || path === "/permission-editor/save")) return this.editPermissionRules(body as PermissionEditorRequest, path.endsWith("/save"));
    if (method === "POST" && /^\/sessions(\/|$)/.test(path)) return this.write(path, (body ?? {}) as Record<string, unknown>);
    if (method === "PUT" || method === "DELETE") {
      const sessionFile = path.match(/^\/sessions\/([^/]+)\/file$/);
      if (sessionFile) return this.editSessionFile(sessionFile[1], (body ?? {}) as Record<string, unknown>, method === "DELETE");
      const source = path.match(/^\/kb\/sources\/([^/]+)$/);
      if (method === "DELETE" && source) return this.removeKbSource(query.repo, source[1]);
      const provider = path.match(/^\/providers\/([^/]+)$/);
      if (method === "DELETE" && provider) return this.removeProvider(provider[1]);
      const terminal = path.match(/^\/terminals\/([^/]+)$/);
      if (method === "DELETE" && terminal) return this.closeTerminal(terminal[1]);
      if (method === "DELETE") return error("not_found", `no route DELETE ${path}`, 404);
      if (path === "/routing") return this.putRouting((body ?? {}) as Record<string, unknown>);
      if (path === "/debug") return this.putDebug((body ?? {}) as Record<string, unknown>);
      const key = path.match(/^\/keys\/([^/]+)$/);
      if (key) return this.putKey(key[1], (body ?? {}) as Record<string, unknown>);
      const file = path.match(/^\/workspaces\/(.+)\/file$/);
      if (file) return this.editFile(file[1], (body ?? {}) as Record<string, unknown>);
      return error("not_found", `no route PUT ${path}`, 404);
    }
    if (method === "PATCH") {
      if (path === "/config") return this.patchConfig((body ?? {}) as Record<string, unknown>);
      if (path === "/user-settings") return this.patchUserSettings((body ?? {}) as Record<string, unknown>);
      return error("not_found", `no route PATCH ${path}`, 404);
    }
    const d = this.data;
    const repo = query.repo;
    const needRepo = () => (repo ? null : error("invalid", "repo is required", 400));
    const inRepo = (ws: WorkspaceDTO) => !repo || ws.path.startsWith(repo);

    let m: RegExpMatchArray | null;
    if (method === "POST") {
      if (path.startsWith("/goal/")) return this.post(path, body as Record<string, unknown>);
      const fields = (body ?? {}) as Record<string, unknown>;
      if (path === "/decisions") return this.recordDecision(fields);
      if (path === "/profiles") return this.createProfile(fields);
      if (path === "/kb/sources") return this.addKbSource(repo, fields);
      if ((m = path.match(/^\/kb\/sources\/([^/]+)\/sync$/))) return this.syncKbSource(repo, m[1]);
      if (path === "/permissions") return this.changePermissions(fields);
      if ((m = path.match(/^\/grants\/([^/]+)\/revoke$/))) {
        const id = decodeURIComponent(m[1]);
        if (!d.grants.some((g) => g.id === id)) return error("not_found", `no live grant ${id}`, 404);
        d.grants = d.grants.filter((g) => g.id !== id);
        return json({ revoked: id });
      }
      if (path === "/debug/log") {
        // Dropped, as the engine drops them, while logging is off.
        if (d.debug.staff && d.debug.enabled) d.debugLog.push(fields as unknown as DebugEntry);
        return json({ ok: true });
      }
      if (path === "/account/login") return this.accountLogin(fields);
      const opened = this.action(path, fields);
      if (opened) return opened;
      if (path === "/account/logout") {
        d.account = { signed_in: false };
        return json({ messages: ["Logged out of brindle Pro."] });
      }
      if (path === "/account/switch-org") {
        if (typeof fields.org !== "string") return error("invalid", "org is required", 400);
        if (!d.account.signed_in) return error("signed_out", "not logged in", 401);
        const org = d.orgs[fields.org];
        if (!org) return error("signed_out", `you are not a member of ${fields.org}`, 401);
        d.account = org.account;
        d.costEntitled = org.costEntitled;
        this.orgSwitches.push(fields.org);
        return json({ messages: [`Switched to org ${fields.org}.`] });
      }
      if ((m = path.match(/^\/inbox\/([^/]+)\/permission$/))) return this.answerPermission(m[1], fields);
      if ((m = path.match(/^\/inbox\/([^/]+)\/([^/]+)$/))) {
        const [id, action] = [m[1], m[2]];
        const answer = INBOX_ACTIONS[action];
        if (!answer) return error("invalid", `unknown inbox action '${action}'`, 400);
        const item = d.inbox.find((i) => (action === "read" ? i.notice_id : i.agent_id) === id);
        if (!item) return error("not_found", `no agent ${id}`, 404);
        // The action settles the item, and the bus row it writes sends clients for a fresh inbox.
        d.inbox = d.inbox.filter((i) => i !== item);
        this.publish("message.queued", { agent_id: item.agent_id ?? null, workspace_id: item.workspace_id ?? null });
        return json(answer, action === "merge" ? 202 : 200);
      }
      if ((m = path.match(/^\/agents\/([^/]+)\/rewind$/))) return this.rewind(m[1], fields);
      if ((m = path.match(/^\/agents\/([^/]+)\/(message|close|kill)$/))) {
        const [id, action] = [m[1], m[2]];
        const ws = d.workspaces.find((w) => w.agents?.some((a) => a.id === id));
        if (!ws) return error("not_found", `no agent ${id}`, 404);
        if (action === "message") {
          const text = (body as { text?: unknown } | undefined)?.text;
          if (text == null) return error("invalid", "text is required", 400);
          if (typeof text !== "string") return error("invalid", "text has the wrong type", 400);
          this.publish("message.queued", { agent_id: id, workspace_id: ws.id, payload: { message_id: this.events.length + 1 } });
          return json({ status: "queued" });
        }
        // Both leave the snapshot without a bus row: close hides the agent
        // (its record stays), kill deletes it. Its children stay either way.
        const running = this.alive(id);
        if (action === "close") d.closed = [...d.closed, ws.agents!.find((a) => a.id === id)!];
        d.workspaces = d.workspaces.map((w) => (w === ws ? { ...w, agents: w.agents?.filter((a) => a.id !== id) } : w));
        this.windowGone(id);
        return json(action === "close" ? { closed: id, stopped: running } : { killed: id });
      }
      // {id:path}: a worker's id has a slash ("proj/feat-ls").
      if ((m = path.match(/^\/workspaces\/(.+)\/(review|merge|sync|pr|remove)$/))) {
        const [id, action] = [m[1], m[2]];
        if (!d.workspaces.some((w) => w.id === id)) return error("not_found", `no workspace ${id}`, 404);
        if (JOB_ACTIONS.has(action)) return json({ job: `job${this.nextJob++}`, kind: action }, 202);
        if (action === "review") return json({ reviewer: "rev1", checks_running: false });
        return json({ removed: id, branch_note: null, note: null, teardown_failed: null });
      }
      return error("not_found", `no route ${path}`, 404);
    }
    switch (path) {
      case "/meta":
        return json(d.meta);
      case "/repos":
        return json(d.repos);
      case "/sessions": {
        // Live sessions first, then paused ones (routes_read.list_sessions).
        // A closed session's root is hidden for good: it leaves the list.
        const mine = d.sessions.filter((s) => this.sessionInRepo(s, repo) && s.status !== "done" && !d.closed.some((a) => a.id === s.id));
        return needRepo() ?? json([...mine.filter((s) => !PAUSED.has(s.status)), ...mine.filter((s) => PAUSED.has(s.status))]);
      }
      case "/snapshot":
        return json(d.workspaces.filter(inRepo));
      case "/goal": {
        // A session with no autopilot (another root) has no goal.
        const forRoot = !query.root || (d.goal !== null && query.root === d.goal.root_id);
        return needRepo() ?? json(forRoot ? d.goal : null);
      }
      case "/inbox":
        return json(d.inbox);
      case "/decisions":
        return needRepo() ?? json(d.decisions);
      case "/kb/sources":
        return needRepo() ?? json(d.kbSources);
      case "/kb/search": {
        const terms = (query.q ?? "").toLowerCase().match(/\w+/g) ?? [];
        const hits = terms.length === 0 ? [] : d.kbHits.filter((h) => terms.some((t) => `${h.heading_path} ${h.text}`.toLowerCase().includes(t)));
        return needRepo() ?? json(hits);
      }
      case "/profiles":
        return json((repo && d.profilesByRepo[repo]) || d.profiles);
      case "/routing":
        return needRepo() ?? json(this.routing(repo));
      case "/rules":
        return needRepo() ?? json(d.rulePacks);
      case "/config":
        // The repo's config comes only when a repo is asked for (routes_read.get_config).
        return json(repo ? { user: d.config.user, repo: effectiveConfig(d.config), keys: d.config.keys } : { user: d.config.user, keys: d.config.keys });
      case "/user-settings":
        return json(d.userSettings);
      case "/debug":
        return json(d.debug);
      case "/doctor":
        return json(d.doctor);
      case "/account":
        return json(d.account);
      case "/cost":
        if (!d.costEntitled) return error("not_entitled", "cost reports need brindle Pro: `brindle account upgrade`", 402);
        return json({ ...d.cost, days: query.days ? Number(query.days) : d.cost.days });
      case "/usage":
        return json(d.usage);
    }
    if ((m = path.match(/^\/profiles\/([^/]+)$/))) {
      const name = m[1];
      const p = d.profiles.find((x) => x.name === name);
      if (!p) return error("not_found", `no profile ${name}`, 404);
      const extra = d.profileDetails[name] ?? {};
      return json({ ...p, source: `built-in profile ${name}`, extends: [], rules: [], prompt: "", lint: { errors: [], warnings: [] }, ...extra });
    }
    if ((m = path.match(/^\/agents\/([^/]+)$/))) {
      const id = m[1];
      const agent =[...d.workspaces.flatMap((ws) => ws.agents ?? []), ...d.closed].find((a: AgentDTO) => a.id === id);
      return agent ? json(agent) : error("not_found", `no agent ${id}`, 404);
    }
    if ((m = path.match(/^\/workspaces\/(.+)\/diff$/))) {
      const id = m[1];
      const ws = d.workspaces.find((w) => w.id === id);
      if (!ws) return error("not_found", "no such workspace", 404);
      return json(d.diffs[id] ?? { workspace_id: ws.id, base: ws.base_branch ?? "main", diff: "", stat: "" });
    }
    if ((m = path.match(/^\/workspaces\/(.+)\/file$/))) {
      const id = m[1];
      if (!d.workspaces.some((w) => w.id === id)) return error("not_found", `no workspace ${id}`, 404);
      const content = d.files[id]?.[query.path ?? ""];
      if (content === undefined) return error("not_found", `no file ${query.path ?? ""} in ${id}`, 404);
      // routes_read.read_file: {path, content, base}, base being the blob sha the PUT takes.
      return json({ path: query.path, content, base: fakeBlob(content) });
    }
    if (path === "/grants") return json(d.grants);
    if (path === "/providers/installed") {
      if (this.failNextDetects > 0) {
        this.failNextDetects--;
        return error("internal", "couldn't look for the providers: agy --version timed out", 500);
      }
      return json(d.providers);
    }
    if ((m = path.match(/^\/providers\/([^/]+)\/preflight$/))) return json(this.preflightOf(m[1]));
    if ((m = path.match(/^\/sessions\/([^/]+)\/(pr\/readiness|pr\/draft|diff|file)$/))) return this.sessionReviewRead(m[1], m[2], query);
    return error("not_found", `no route ${path}`, 404);
  };

  /* Scratch workspace → the transfer running for it (routes_actions._TRANSFERRING). */
  private transferring = new Map<string, { job: string; target: string; branch: string | null }>();

  /* While true, a rewind or transfer stops between its two steps (what a
     snapshot then sees, before the job ends) until releaseJobs(). */
  holdJobs = false;
  private heldJobs: (() => void)[] = [];

  releaseJobs(): void {
    for (const next of this.heldJobs.splice(0)) next();
  }

  private then(next: () => void): void {
    if (this.holdJobs) this.heldJobs.push(next);
    else setTimeout(next, 0);
  }

  /* routes_actions.rewind_agent: `to` is checked at once, the rest in the
     job, in rewind.rewind's order: a turn with no snapshot fails it before
     anything is touched; then the worker is stopped and dismissed (status
     "rewound": it leaves the snapshot), and only after that is the fresh
     one spawned (on the profile picked, with turns 1..to), or the job fails
     there with `rewindFails` (a spawn that fails), the old worker gone either way. */
  private rewind(id: string, body: Record<string, unknown>): Response {
    const d = this.data;
    const ws = d.workspaces.find((w) => w.agents?.some((a) => a.id === id));
    if (!ws) return error("not_found", `no agent ${id}`, 404);
    if (body.to == null) return error("invalid", "to is required", 400);
    if (typeof body.to !== "number" || !Number.isInteger(body.to)) return error("invalid", "to has the wrong type", 400);
    const to = body.to;
    const job = `job${this.nextJob++}`;
    const fields = { agent_id: id, workspace_id: ws.id };
    const failed = (message: string) => this.publish("job.failed", { ...fields, payload: { job, job_kind: "rewind", error: { code: "conflict", message } } });
    setTimeout(() => {
      const have = d.turns[id] ?? 0;
      if (to < 1 || to > have) {
        failed(have === 0
          ? `${id} has no turn snapshots (it hasn't finished a turn that changed files)`
          : `${id} has no turn ${to}; its snapshots: ${Array.from({ length: have }, (_, i) => i + 1).join(", ")}`);
        return;
      }
      const old = d.workspaces.flatMap((w) => w.agents ?? []).find((a) => a.id === id)!;
      d.workspaces = d.workspaces.map((w) => (w.id === ws.id ? { ...w, agents: (w.agents ?? []).filter((a) => a.id !== id) } : w));
      this.windowGone(id);
      this.publish("agent.status", { agent_id: id, workspace_id: ws.id, payload: { status: "rewound" } });
      const spawn = () => {
        if (d.rewindFails) {
          failed(d.rewindFails);
          return;
        }
        const fresh: AgentDTO = { ...old, id: `${id}-r${to}`, profile: typeof body.profile === "string" && body.profile ? body.profile : old.profile, status: "processing", status_since: Date.now() / 1000, reported: false };
        d.turns[fresh.id] = to;
        if (d.terminals[id]) d.terminals[fresh.id] = d.terminals[id];
        d.workspaces = d.workspaces.map((w) => (w.id === ws.id ? { ...w, agents: [...(w.agents ?? []), fresh] } : w));
        this.publish("agent.status", { agent_id: fresh.id, workspace_id: ws.id, payload: { status: "processing" } });
        this.publish("job.done", { ...fields, payload: { job, job_kind: "rewind", result: { agent: fresh.id, workspace_id: ws.id } } });
      };
      this.then(spawn);
    }, 0);
    return json({ job, kind: "rewind" }, 202);
  }

  /* A session belongs to a known repo when its workspace id is the repo's name
     or under it ("proj", "proj/feat-ls"); for an unknown repo, every session. */
  private sessionInRepo(s: SessionDTO, repo: string): boolean {
    const name = this.data.repos.find((r) => r.repo_root === repo)?.name;
    return name === undefined || s.workspace_id === name || s.workspace_id.startsWith(`${name}/`);
  }

  private repoOf(s: SessionDTO): string | null {
    return this.data.repos.find((r) => r.name === s.workspace_id.split("/")[0])?.repo_root ?? null;
  }

  /* A session root's status changes as the engine's does: its agent row too
     (so the snapshot agrees), and agent.status plus session.paused / resumed on the bus. */
  private setStatus(id: string, status: string): SessionDTO {
    const d = this.data;
    const since = Date.now() / 1000;
    d.sessions = d.sessions.map((s) => (s.id === id ? { ...s, status, status_since: since } : s));
    d.workspaces = d.workspaces.map((ws) => ({
      ...ws,
      agents: ws.agents?.map((a) => (a.id === id ? { ...a, status, status_since: since } : a)),
    }));
    const session = d.sessions.find((s) => s.id === id)!;
    const fields = { agent_id: id, repo_root: this.repoOf(session), workspace_id: session.workspace_id };
    this.publish("agent.status", { ...fields, payload: { status } });
    this.publish(status === "paused" ? "session.paused" : "session.resumed", fields);
    return session;
  }

  /* routes_actions.close_session (service.sessions.close_session): the
     supervisor and every agent under it are closed as one agent's close is
     (stopped if running, hidden from the snapshot, the record kept), and the
     session leaves GET /sessions; workspaces and branches stay. Agents
     already closed are skipped, so closing twice closes nothing. */
  private closeSession(id: string): Response {
    const d = this.data;
    const session = d.sessions.find((s) => s.id === id);
    if (!session && !d.closed.some((a) => a.id === id)) return error("not_found", `no agent ${id}`, 404);
    const members = new Set([id]);
    const all = d.workspaces.flatMap((w) => w.agents ?? []);
    for (let grew = true; grew; ) {
      grew = false;
      for (const a of all) {
        if (!members.has(a.id) && a.parent_id && members.has(a.parent_id)) {
          members.add(a.id);
          grew = true;
        }
      }
    }
    const closing = all.filter((a) => members.has(a.id));
    // A paused session's root is in no snapshot: its record is closed all the same.
    const root = session && !closing.some((a) => a.id === id) && !d.closed.some((a) => a.id === id) ? [{ id, profile: session.profile, status: session.status, mode: session.mode } as AgentDTO] : [];
    const stopped = closing.filter((a) => this.alive(a.id)).map((a) => a.id);
    d.closed = [...d.closed, ...root, ...closing];
    d.workspaces = d.workspaces.map((w) => ({ ...w, agents: w.agents?.filter((a) => !members.has(a.id)) }));
    for (const a of closing) {
      this.windowGone(a.id);
      if (stopped.includes(a.id)) this.publish("agent.status", { agent_id: a.id, payload: { status: "paused" } });
    }
    return json({ closed: [...root, ...closing].map((a) => a.id), stopped });
  }

  /* The action routes for sessions (routes_actions.py), answering with the
     engine's shapes: a start with sessions already running in the repo and no
     `choice` is a 409 {conflict} (service.sessions.place). */
  private write(path: string, body: Record<string, unknown>): Response | Promise<Response> {
    const d = this.data;
    if (path === "/sessions") {
      if (typeof body.repo !== "string") return error("invalid", "repo is required", 400);
      const repo = d.repos.find((r) => r.repo_root === body.repo);
      if (!repo) return error("conflict", `${body.repo} is not a git repository`, 409);
      const refused = this.preflightRefusal(typeof body.provider === "string" && body.provider ? body.provider : "claude");
      if (refused) return refused;
      const running = d.sessions.filter((s) => this.sessionInRepo(s, repo.repo_root) && !PAUSED.has(s.status) && s.status !== "done");
      let workspace = repo.name;
      if (running.length > 0 && !body.branch && !body.worktree) {
        if (body.choice === undefined) {
          return json(
            {
              conflict: {
                kind: "running",
                message: "Sessions running in this repo:",
                default: 0,
                choices: [
                  { key: "o", label: "open" },
                  { key: "n", label: "new session in its own worktree" },
                  { key: "p", label: "pause it and start fresh" },
                ],
                sessions: running,
              },
            },
            409,
          );
        }
        const picked = running.find((s) => s.id === body.session_id);
        if (!picked || !["o", "n", "p"].includes(String(body.choice))) {
          return error("invalid", `no running session ${JSON.stringify(body.session_id ?? null)} to ${String(body.choice)}`, 400);
        }
        if (body.choice === "o") return json({ opened: picked });
        if (body.choice === "n") workspace = `${repo.name}/session-${this.nextSession + 1}`;
        else this.setStatus(picked.id, "paused");
      } else if (typeof body.branch === "string" && body.branch) {
        workspace = `${repo.name}/${body.branch.replace(/\//g, "-")}`;
      }
      const prompt = typeof body.prompt === "string" ? body.prompt.split("\n").find((l) => l.trim())?.trim() : undefined;
      const session: SessionDTO = {
        id: `new${this.nextSession++}`,
        workspace_id: workspace,
        profile: "supervisor",
        status: "processing",
        mode: "interactive",
        created_at: Date.now() / 1000,
        status_since: null,
        goal: null,
      };
      // GET /sessions shows the goal line (the start prompt); the start reply has none.
      d.sessions = [...d.sessions, { ...session, goal: prompt ?? null }];
      // Like the engine, only agent.status says a new agent exists: no event of its own, and none when
      // its window is recorded. agents._launch: with a brief, "processing" goes out before the chat's
      // window is recorded, so a snapshot read in between lists the agent "exited" with no window
      // (view.agent_entry); without one, the one event is "idle", once the chat is up (as a fresh
      // engine was seen to do for #216: workspace.created at the project's open, then only that).
      const status = prompt ? "processing" : "idle";
      const agent: AgentDTO = { id: session.id, profile: "supervisor", provider: String(body.provider ?? "claude"), status, mode: "interactive", subagents: [] };
      const place = (a: AgentDTO) => {
        d.workspaces = d.workspaces.map((ws) =>
          ws.id === workspace ? { ...ws, agents: [...(ws.agents ?? []).filter((x) => x.id !== a.id), a] } : ws,
        );
      };
      const announce = () => this.publish("agent.status", { agent_id: session.id, repo_root: repo.repo_root, workspace_id: workspace, payload: { status } });
      place({ ...agent, status: "exited", window: null, tmux_server: null });
      if (prompt) announce();
      const launched = () => {
        place({ ...agent, window: `brindle-${repo.name}:${session.id}`, tmux_server: "brindle" });
        if (!prompt) announce();
        return json({ ...session, notes: [] });
      };
      if (!this.holdLaunch) return launched();
      return new Promise<Response>((resolve) => {
        this.pendingLaunch = () => resolve(launched());
      });
    }
    const pr = path.match(/^\/sessions\/([^/]+)\/pr$/);
    if (pr) {
      // routes_actions.open_session_pr: refused with the reasons unless ready; a job whose result carries the URL.
      const root = pr[1];
      if (!d.sessions.some((s) => s.id === root)) return error("not_found", `no session ${root}`, 404);
      const review = d.sessionReview[root];
      if (!review?.ready) return json({ error: { code: "not_ready", message: "the session isn't ready for a PR" }, reasons: review?.reasons ?? ["autopilot isn't on for this session"] }, 409);
      const job = `job${this.nextJob++}`;
      const url = `https://github.com/example/proj/pull/${this.prs.length + 1}`;
      this.prs.push({ root, title: body.title as string | undefined, body: body.body as string | undefined, draft: body.draft as boolean | undefined, url });
      this.setGoalPhase(root, "pr_open", url);
      setTimeout(() => this.publish("job.done", { root_id: root, agent_id: root, payload: { job, job_kind: "session.pr", result: { url, keeps_merged_branches: true } } }), 0);
      return json({ job, kind: "session.pr" }, 202);
    }
    const handover = path.match(/^\/sessions\/([^/]+)\/handover$/);
    if (handover) {
      // routes_actions.handover_session (sessions.handover): a new supervisor in
      // the checkout for `to`, the workers its, the old session paused.
      const old = d.sessions.find((s) => s.id === handover[1]);
      if (!old) return error("not_found", `no agent ${handover[1]}`, 404);
      if (body.to == null) return error("invalid", "to is required", 400);
      if (typeof body.to !== "string") return error("invalid", "to has the wrong type", 400);
      const repo = d.repos.find((r) => r.name === old.workspace_id.split("/")[0]);
      const name = body.to.replace(/\/+$/, "").split("/").at(-1)!;
      const workspace = `${repo?.name ?? old.workspace_id}/${body.to.replace(/\//g, "-")}`;
      const session: SessionDTO = { id: `new${this.nextSession++}`, workspace_id: workspace, profile: "supervisor", status: "processing", mode: "interactive", created_at: Date.now() / 1000, status_since: null, goal: null };
      const agent: AgentDTO = { id: session.id, profile: "supervisor", provider: "claude", status: "processing", mode: "supervisor", subagents: [] };
      if (!d.workspaces.some((w) => w.id === workspace)) {
        d.workspaces = [...d.workspaces, { id: workspace, name, branch: body.to, base_branch: null, path: `${repo?.repo_root ?? ""}/.brindle/worktrees/${body.to}`, ahead: 0, behind: 0, dirty: 0, agents: [], review: null, review_state: null, autopilot: false, asking: null }];
      }
      d.workspaces = d.workspaces.map((ws) => ({
        ...ws,
        agents: [...(ws.agents ?? []).map((a) => (a.parent_id === old.id ? { ...a, parent_id: session.id } : a)), ...(ws.id === workspace ? [agent] : [])],
      }));
      d.sessions = [...d.sessions, { ...session, goal: old.goal }];
      this.setStatus(old.id, "paused");
      this.publish("agent.status", { agent_id: session.id, repo_root: repo?.repo_root ?? null, workspace_id: workspace, payload: { status: "processing" } });
      return json(session);
    }
    const transfer = path.match(/^\/sessions\/([^/]+)\/transfer$/);
    if (transfer) {
      // routes_actions.transfer_session (engine branch desktop/transfer-route):
      // refused at once (400) for a session outside brindle's scratch folder;
      // sent again while its job runs, the same target and branch get that job
      // and another a 409. The job (scratch.transfer) fails for a folder outside
      // a git repo, or a scratch one; otherwise it makes the branch's workspace in
      // the target repo and pauses the scratch chat, whose root then leaves the
      // snapshot (nothing of the session runs any more), and answers what it made.
      const session = d.sessions.find((s) => s.id === transfer[1]);
      if (!session) return error("not_found", `no agent ${transfer[1]}`, 404);
      if (body.target_path == null) return error("invalid", "target_path is required", 400);
      if (typeof body.target_path !== "string") return error("invalid", "target_path has the wrong type", 400);
      if (body.branch != null && typeof body.branch !== "string") return error("invalid", "branch has the wrong type", 400);
      const scratchDir = `${d.meta.db_path.replace(/\/[^/]*$/, "")}/scratch/`;
      const from = this.repoOf(session);
      if (!from?.startsWith(scratchDir)) return error("invalid", "This isn't a scratch session; the work is already in a real repository.", 400);
      const asked = { target: body.target_path, branch: (body.branch as string | undefined) ?? null };
      const running = this.transferring.get(session.workspace_id);
      if (running) {
        if (running.target !== asked.target || running.branch !== asked.branch) return error("conflict", "this session is already being transferred elsewhere", 409);
        return json({ job: running.job, kind: "session.transfer" }, 202);
      }
      const job = `job${this.nextJob++}`;
      this.transferring.set(session.workspace_id, { job, ...asked });
      const fields = { root_id: session.id, agent_id: session.id, repo_root: from };
      setTimeout(() => {
        const target = d.repos.find((r) => asked.target === r.repo_root || asked.target.startsWith(`${r.repo_root}/`));
        const refusal = !target
          ? `${asked.target} isn't inside a git repository. To make it one, run \`git init\` there first (brindle won't do that for you).`
          : target.repo_root.startsWith(scratchDir) ? "the target is itself a scratch session; pick a real repository" : null;
        if (!target || refusal) {
          this.transferring.delete(session.workspace_id);
          this.publish("job.failed", { ...fields, payload: { job, job_kind: "session.transfer", error: { code: "conflict", message: refusal } } });
          return;
        }
        const branch = asked.branch || `brindle/from-${from.replace(/\/+$/, "").split("/").at(-1)}`;
        const id = `${target.name}/${branch.replace(/\//g, "-")}`;
        if (!d.workspaces.some((w) => w.id === id)) {
          d.workspaces = [...d.workspaces, { id, name: branch.split("/").at(-1)!, branch, base_branch: "main", path: `${target.repo_root}/.brindle/worktrees/${branch}`, ahead: 2, behind: 0, dirty: 0, agents: [], review: null, review_state: null, autopilot: false, asking: null }];
        }
        if (!PAUSED.has(session.status)) this.setStatus(session.id, "paused");
        d.workspaces = d.workspaces.map((w) => (w.id === session.workspace_id ? { ...w, agents: (w.agents ?? []).filter((a) => a.id !== session.id) } : w));
        this.then(() => {
          this.transferring.delete(session.workspace_id);
          this.publish("job.done", { ...fields, payload: { job, job_kind: "session.transfer", result: { workspace_id: id, branch, repo_root: target.repo_root, commits: 2 } } });
        });
      }, 0);
      return json({ job, kind: "session.transfer" }, 202);
    }
    const closing = path.match(/^\/sessions\/([^/]+)\/close$/);
    if (closing) return this.closeSession(closing[1]);
    const m = path.match(/^\/sessions\/([^/]+)\/(continue|pause)$/);
    if (m) {
      const id = m[1];
      const session = d.sessions.find((s) => s.id === id);
      if (!session) return error("not_found", `no agent ${id}`, 404);
      if (m[2] === "pause") {
        if (PAUSED.has(session.status)) return json({ paused: [] });
        return json({ paused: [this.setStatus(id, "paused").id] });
      }
      if (!PAUSED.has(session.status)) {
        return json({ state: "recovered", restarted: 0, session: { ...session, goal: null }, notes: [] });
      }
      // The resumed chat's provider must be ready to launch (the root agent's, else Claude Code's).
      const rootAgent = d.workspaces.flatMap((w) => w.agents ?? []).find((a) => a.id === id);
      const refused = this.preflightRefusal(rootAgent?.provider ?? "claude");
      if (refused) return refused;
      // service.sessions.continue_: at most one live session per checkout, so
      // whatever runs in the resumed one's checkout is paused first.
      const notes = d.sessions
        .filter((s) => s.id !== id && s.workspace_id === session.workspace_id && !PAUSED.has(s.status) && s.status !== "done")
        .map((s) => {
          this.setStatus(s.id, "paused");
          return {
            text: `Paused the session that was still running here (${s.id}); \`brindle continue ${s.id}\` brings it back.`,
            tone: "",
          };
        });
      // A paused root may have left the snapshot entirely; continuing brings it back.
      if (!rootAgent) {
        const agent: AgentDTO = { id, profile: session.profile, provider: "claude", status: "idle", mode: session.mode, subagents: [], window: `brindle-${id}:${id}`, tmux_server: "brindle" };
        d.workspaces = d.workspaces.map((ws) => (ws.id === session.workspace_id ? { ...ws, agents: [...(ws.agents ?? []), agent] } : ws));
      }
      const next = this.setStatus(id, "idle");
      return json({ state: "resumed", restarted: 0, session: { ...next, goal: null }, notes });
    }
    return error("not_found", `no route POST ${path}`, 404);
  }
}

/* Let queued socket callbacks, fetches and promise chains run. */
export async function flush(rounds = 5): Promise<void> {
  for (let i = 0; i < rounds; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}
