/* The typed client for the brindle daemon's v1 HTTP routes
   (brindle/daemon/app.py, routes_read.py). Every request carries the token as
   `Authorization: Bearer <token>`; an engine error arrives as
   {"error": {"code", "message"}} (brindle/service/errors.py) and is thrown as a
   DaemonError with that code. */

import type {
  AccountDTO,
  AgentDTO,
  DecisionDTO,
  GoalDTO,
  GrantDTO,
  InboxItemDTO,
  PermissionActionDTO,
  PermissionChangeDTO,
  PermissionConflictDTO,
  PermissionEditorDTO,
  PermissionReviewDTO,
  PermissionRuleDTO,
  PermissionSaveDTO,
  PermissionVerifyDTO,
  PreflightItemDTO,
  ProfileDTO,
  RoutingDTO,
  SessionDTO,
  WorkspaceDTO,
} from "./schema";

/* PUT /routing (routes_actions.put_routing): what to write and where. The
   tiers given replace their lists in that scope's file; the others, and a
   reviewer or exclusion list not given, keep what is there. "user" is
   ~/.brindle/config.json, "local" the repo's gitignored
   .brindle/config.local.json, "repo" its committed .brindle/config.json. */
export type RoutingScope = "user" | "local" | "repo";

export interface RoutingPut {
  repo: string;
  scope: RoutingScope;
  tiers?: Record<string, string[]>;
  reviewer?: string;
  exclude_providers?: string[];
  /* Per kind of work (engine branch feat/routing-learned-lock): its model
     (a profile, the supervisor's too), its ordered fallbacks (every kind's,
     the reviewer's and the supervisor's included), and the lock against hosted
     learning. Choosing a model or fallbacks locks the kind unless `locked`
     is given. */
  kinds?: Record<string, KindPut>;
}

export interface KindPut {
  model?: string;
  fallbacks?: string[];
  locked?: boolean;
  /* Ahead of the engine: the effort and the instructions added to whatever
     runs the kind (the Agents tab's "More"). */
  effort?: string;
  /* null clears them. */
  instructions?: string | null;
}

/* Ahead of the engine: what GET /routing's `kinds` carries besides
   RoutingKindDTO, the "More" a kind was given. */
export interface KindExtras {
  effort?: string | null;
  instructions?: string | null;
}

/* POST /profiles (ahead of the engine, whose `brindle profile new` writes
   the same file): a custom agent, a model plus how it works, written to
   ~/.brindle/agents/<name>.md. */
export interface ProfileNew {
  repo: string;
  name: string;
  description: string;
  provider: string;
  model: string;
  effort: string;
  instructions: string;
  rules: string[];
}

export interface ProfileCreated {
  name: string;
  path: string;
}

/* What was written (routing, reviewer, exclude_providers) and the file it went to. */
export interface RoutingUpdated {
  updated: string[];
  path: string;
}

/* POST /providers/{p}/login and /keys/{name}/set (routes_actions.py,
   terminal.open_action; the engine's ActionTerminalDTO): the real command
   runs in a window of the daemon's own tmux session. The app's tmux client
   attaches to its pane, `target` on `server` (null: the default server),
   as to an agent's window (api/ptyView.ts); `terminal` is the id POST
   /terminals/{id}/input takes. The person types into it there (a sign-in,
   a secret): nothing passes through the API. A second press while the
   same action still runs gets its terminal again (`started` false). */
export interface ActionTerminal {
  terminal: string;
  started: boolean;
  target: string;
  server?: string | null;
  /* Ahead of the engine: for POST /terminals/shell, the shell's program
     ("zsh", $SHELL's last part), which the Terminal's bar names. */
  shell?: string | null;
}

/* DELETE /terminals/{id} (routes_actions.close_terminal): its window is gone. */
export interface TerminalClosed {
  terminal: string;
  closed: boolean;
}

/* POST /terminals/{id}/input: the line went in. */
export interface TerminalInputSent {
  terminal: string;
  sent: boolean;
}

/* PUT /keys/{name} (routes_actions.put_key): what was stored, as names and
   last four only; 403 "forbidden" when the org bans personal keys, 422
   "not_stored" when the provider refused the key. */
export interface KeyStored {
  name: string;
  last4: string;
  set: boolean;
}

/* GET /providers/{p}/preflight: what a provider's CLI needs
   before brindle can launch it, each a thing the app can settle natively:
   a sign-in (ProviderSignIn), a key (PUT /keys/{name}), the CLI's trust of
   the folder, or a consent it asks for once. A start or continue that
   would launch a provider with an item still open is refused with 409
   {error: {code: "preflight"}, preflight: PreflightItemDTO[]}.
   The client adapts those unresolved items to the card's PreflightDTO. */
export type PreflightKind = "signin" | "key" | "trust" | "consent";

export interface PreflightItem {
  id: string;
  kind: PreflightKind;
  label: string;
  detail?: string | null;
  done: boolean;
  /* The key's name (kind "key"). */
  key_name?: string | null;
  action?: PreflightItemDTO["action"];
}

export interface PreflightDTO {
  provider: string;
  ok: boolean;
  items: PreflightItem[];
}

/* The engine sends only unresolved items. The desktop's original contract
   grouped items with done flags; keep both shapes at the API boundary. */
function preflightResult(value: PreflightDTO | PreflightItemDTO[], provider = ""): PreflightDTO {
  if (!Array.isArray(value)) return value;
  return {
    provider: provider || value[0]?.provider || "",
    ok: value.length === 0,
    items: value.map((item) => ({
      ...item, kind: item.kind as PreflightKind, label: item.title, done: false,
      key_name: item.kind === "key" && item.action?.name === "set_key"
        ? item.action.path.match(/^\/v1\/keys\/([A-Z][A-Z0-9_]*)$/)?.[1] : undefined,
    })),
  };
}

/* GET /workspaces/{id}/file?path= (engine branch desktop/review-edit,
   routes_read.read_file): a file of the worker's worktree as it stands, and
   `base`, the blob sha the PUT below takes. */
export interface FileContent {
  path: string;
  content: string;
  base: string;
}

/* PUT /workspaces/{id}/file {path, content, base} (engine branch
   desktop/review-edit): the person's edit from the review sheet, written to
   the worker's worktree and committed on its branch. 409 "stale" when the
   file changed since the diff was shown, 409 "busy" while an agent in the
   workspace is at work. */
export interface FileEdited {
  workspace_id: string;
  path: string;
  commit: string | null;
  blob: string;
  stat: string;
}

/* GET /sessions/{root}/pr/readiness (routes_read.session_readiness,
   daemon/session_review.py): whether brindle judges the supervisor session
   ready for a PR (every milestone passed, the completion audit approved,
   no worker running or task queued, nothing waiting on the person, the
   branch committed and ahead of its base), and `reasons`, what is missing
   in plain words (none when ready). `session.pr_ready` arrives on the bus
   when it flips. `pr`: once brindle has opened the PR itself (the engine's
   auto-PR setting, branch desktop/auto-pr; stubbed in the fake daemon until
   it merges), where it is. */
export interface SessionReadiness {
  root_id: string;
  ready: boolean;
  reasons: string[];
  pr?: { url: string } | null;
}

/* GET /sessions/{root}/diff (session_review.diff): the session branch
   against its PR base, each file with its hunks. The engine attributes each
   file and hunk to whoever wrote it (`authors`, `workers`); the app never
   shows that. 409 "not_ready" (with `reasons`) unless the session is ready. */
export interface SessionHunk {
  header: string;
  old_start: number;
  old_lines: number;
  new_start: number;
  new_lines: number;
  /* The hunk whole: its header line, then its lines. */
  text: string;
  authors?: string[];
}

export interface SessionFileDiff {
  path: string;
  old_path: string | null;
  /* git's status letter: M, A, D, R ... */
  status: string;
  binary: boolean;
  authors?: string[];
  hunks: SessionHunk[];
}

export interface SessionDiff {
  root_id: string;
  branch: string;
  base: string;
  base_sha: string;
  head: string;
  files: SessionFileDiff[];
  workers?: unknown[];
}

/* GET /sessions/{root}/pr/draft (session_review.draft): the PR's title and
   body from the goal and its milestones, to edit before opening. */
export interface PrDraft {
  title: string;
  body: string;
  base: string;
  branch: string;
}

/* PUT / DELETE /sessions/{root}/file (routes_actions._edit_session): the
   person's edit of any file on the session branch, committed there as
   "review edits: <path>"; `base` is the blob GET .../file gave (left out to
   create a file). 409 "stale" when the file moved on, "busy" while the
   supervisor is mid-turn or a merge into the branch is under way. */
export interface SessionFileEdited {
  root_id: string;
  path: string;
  commit: string | null;
  blob: string | null;
}

/* POST /sessions/{root}/pr (routes_actions.open_session_pr): pushes the
   session branch and opens its PR as `brindle pr` does; a job whose
   job.done result carries {url}. */
export interface OpenSessionPr {
  title?: string;
  body?: string;
  draft?: boolean;
}

/* The providers whose CLI has a sign-in of its own (team_policy.CLI_NAMES). */
/* GET /providers/installed (engine branch feat/provider-install): one row
   per provider CLI, as brindle detects it. brindle uses an installed CLI
   as-is, wherever it is, and the CLI keeps itself up to date
   (providers_install.status). `state`: installed (found and used), missing
   (none found) or off (the person took it out of brindle with DELETE
   /providers/{p}: still found, never used, nothing uninstalled). `path` and
   `version`: the copy found (both null when none is), an off one's too;
   `signed_in`: whether it reads as signed in (null: couldn't tell);
   `installs`: every copy found, which POST /providers/{p}/use-mine picks
   from (and turns the provider back on). */
export type ProviderState = "installed" | "missing" | "off";

export interface ProviderInstalled {
  provider: string;
  name: string;
  state: ProviderState;
  path: string | null;
  version: string | null;
  signed_in: boolean | null;
  selected?: boolean;
  installs?: { path: string; version: string | null }[];
  config_home?: string | null;
}

export const LOGIN_PROVIDERS = ["claude", "codex", "antigravity", "opencode"] as const;
export type LoginProvider = (typeof LOGIN_PROVIDERS)[number];

export interface GoalEnabledAck {
  enabled: boolean;
}

export interface Meta {
  engine_version: string;
  api_version: string;
  pid: number;
  db_path: string;
  tmux: boolean;
  airgap: boolean;
}

export interface Repo {
  repo_root: string;
  name: string;
}

export interface Diff {
  workspace_id: string;
  base: string;
  diff: string;
  stat: string;
}

/* A long action's answer (202): it runs on its own, and job.progress, then
   job.done or job.failed, arrive on the event stream with payload.job = `job`. */
export interface Job {
  job: string;
  kind: string;
}

export interface ReviewStarted {
  /* null while the repo's checks run first; the reviewer starts after them. */
  reviewer: string | null;
  checks_running: boolean;
}

export interface Removed {
  removed: string;
  branch_note?: string | null;
  note?: string | null;
  teardown_failed?: string | null;
}

export interface KbSource {
  id: string;
  kind: string;
  scope?: string;
  mode?: string;
  visibility?: string;
  credential_name?: string | null;
  retention_days?: number | null;
  added_at?: number;
  docs?: number;
  quarantined?: boolean;
}

/* DELETE /kb/sources/{id} (engine branch desktop/kb-sources): the source is gone. */
export interface KbSourceRemoved {
  removed: string;
}

/* GET /kb/search (kb.search): one of the best chunks for a query among the
   repo's indexed documents. */
export interface KbHit {
  chunk_id: string;
  heading_path: string;
  text: string;
  url: string | null;
  title: string | null;
  external_id: string;
  updated_at: number | null;
  /* The document's last update as a date, or null. */
  updated: string | null;
}

/* GET /profiles/{name} (routes_read.get_profile): a profile as `brindle
   profile show` resolves it, with `brindle profile lint`'s findings. */
export interface RulePackInfo {
  name: string;
  description: string;
  source: string;
}

export interface ProfileDetail extends ProfileDTO {
  /* Where it is read from: a path, or "built-in profile <name>". */
  source: string | null;
  /* The extends chain, nearest parent first. */
  extends: string[];
  rules: RulePackInfo[];
  /* The prompt with the rule packs' text, as the agent gets it. */
  prompt: string;
  lint: { errors: string[]; warnings: string[] };
}

/* GET /account: AccountDTO, and when signed in the entitlement's details
   (routes_read.account), never a token. `learning` is whether hosted
   learning is on; `learning_note` the learner's kept note for the repo. */
export interface AccountInfo extends AccountDTO {
  org_id?: string;
  status?: string;
  features?: string[];
  seats?: number;
  role?: string | null;
  expires_at?: number;
  in_grace?: boolean;
  grace_until?: number | null;
  learning?: boolean;
  learning_note?: string | null;
}

/* A secret leaves the daemon only as its name and last 4 characters. */
export interface KeyInfo {
  name: string;
  set: boolean;
  last4: string;
}

export interface Config {
  user: Record<string, unknown>;
  repo?: Record<string, unknown>;
  keys: KeyInfo[];
}

export interface DoctorCheck {
  level: string;
  name: string;
  detail: string;
}

/* POST /decisions (routes_actions.record_decision): a decision agents in the
   repo must follow, scoped to `paths` (none: the whole repo). An accepted
   decision on the same topic over the same paths refuses it (409) unless
   `supersedes` names that one (its id, or a unique prefix). */
export interface DecisionPut {
  repo: string;
  topic: string;
  decision: string;
  rationale?: string;
  paths?: string[];
  supersedes?: string;
}

/* The decision as recorded, and which sessions were told of it. */
export interface DecisionRecorded {
  decision: DecisionDTO & Record<string, unknown>;
  told: string[];
}

/* PATCH /config (routes_actions.patch_config): `values` into
   ~/.brindle/config.json ("user") or the repo's gitignored
   .brindle/config.local.json ("repo"); only the repo config's own keys,
   each a value of its default's type, or null to unset it. */
export interface ConfigPatch {
  scope: "user" | "repo";
  repo?: string;
  values: Record<string, unknown>;
}

export interface ConfigUpdated {
  updated: string[];
  path: string;
}

/* GET and PATCH /user-settings (engine branch desktop/user-settings): the
   person's own settings that are not a repo's. `claude_permission_mode`
   (config.claude_permission_mode): "default" holds Claude's permission
   requests for the Inbox; "auto" lets Claude approve most commands itself.
   A PATCH with any other value is refused (400). */
export type ClaudePermissionMode = "default" | "auto";

export interface UserSettings {
  claude_permission_mode: ClaudePermissionMode;
}

/* GET and PUT /debug (staff-only debug logging, owner decision): whether
   this account is staff, and whether debug logs upload to brindle. Only
   staff may turn it on. */
export interface DebugState {
  staff: boolean;
  enabled: boolean;
}

export type DebugLevel = "debug" | "info" | "warning" | "error";

/* POST /debug/log: one of the app's own events, uploaded with the engine's
   while debug logging is on. */
export interface DebugEntry {
  source: string;
  level: DebugLevel;
  message: string;
  detail?: Record<string, unknown>;
}

/* POST /permissions (routes_actions.change_permissions). */
export type PermissionChange =
  | { op: "allow" | "deny"; kind: string; match: string; match_type?: string }
  | { op: "forget" | "accept"; id: string }
  | { op: "reset" };

export type PermissionChanged = { rule: string; describe: string } | { reset: true };

/* The permission editor reviews a draft against the engine's current policy.
   Save repeats that review and verifies its proposal digest atomically. */
export type PermissionEditorRule = PermissionRuleDTO;
export type PermissionAction = PermissionActionDTO;
export type PermissionEditorState = PermissionEditorDTO;
export type PermissionConflict = PermissionConflictDTO;
export type PermissionRuleChange = PermissionChangeDTO;
export type PermissionEditorReview = PermissionReviewDTO;
export type PermissionEditorSaved = PermissionSaveDTO;

export interface PermissionEditorRequest {
  repo: string;
  revision: string;
  operation: "upsert" | "remove";
  rule?: Partial<PermissionEditorRule>;
  id?: string;
  resolution: "manual" | "auto" | "existing" | "new";
}

/* What an account action said (the CLI's own lines). */
export interface AccountSaid {
  messages: string[];
}

export interface CostBucket {
  dollars: number;
  tokens: number;
  unknown: number;
  free: number;
}

export interface Cost {
  days: number;
  total: CostBucket;
  by_model: Record<string, CostBucket>;
}

/* One quota window a provider reports (quota.Window): percent used, when it
   resets (epoch seconds), its length in minutes. `name` and `short` are the
   window's words ("5-hour", "5h"), or for a one-model cap its model's name
   in both ("Fable"). */
export interface UsageWindow {
  used: number;
  resets_at: number | null;
  minutes: number | null;
  name: string;
  short: string;
}

/* A provider's entry in GET /usage: `known` false when brindle has nothing
   current about it; `limited` while it refuses work, until `limited_until`. */
export interface ProviderUsage {
  provider: string;
  known: boolean;
  limited: boolean;
  limited_until: number | null;
  updated_at: number | null;
  windows: UsageWindow[];
}

/* A provider near its limit: refusing work, past 80% of its fullest window,
   or resetting a window within the hour; `resets_at` is when that ends. */
export interface UsageNear {
  provider: string;
  reason: "limited" | "high" | "resets_soon";
  used: number | null;
  resets_at: number | null;
}

/* GET /usage (routes_read.get_usage): every provider, and the near ones worst first. */
export interface Usage {
  providers: ProviderUsage[];
  near: UsageNear[];
}

/* POST /sessions (routes_actions.start_session → service.sessions.start). */
export interface StartSession {
  repo: string;
  /* The supervisor's CLI (claude, codex ...); the repo's default when omitted. */
  provider?: string;
  prompt?: string;
  watch?: boolean;
  /* The repo's default when omitted. */
  autopilot?: boolean;
  /* Start in this branch's (or this worktree's) checkout instead of the repo's. */
  branch?: string;
  worktree?: string;
  /* The answer to a "running" SessionConflict: a choice key and the session it applies to. */
  choice?: string;
  session_id?: string;
}

/* What the CLI prints along the way (service.sessions.Note). */
export interface Note {
  text: string;
  tone: "" | "ok" | "warn";
}

/* A started session, or (choice "o") the running one to open instead. */
export type StartResult = (SessionDTO & { notes: Note[] }) | { opened: SessionDTO };

export interface ContinueResult {
  state: "recovered" | "running" | "resumed";
  restarted: number;
  session: SessionDTO;
  notes: Note[];
}

/* A 409 that asks the person to choose before anything happens
   (service.sessions.Conflict): e.g. sessions already running in the repo. */
export interface SessionConflict {
  kind: "running" | "paused";
  message: string;
  /* Index into `sessions` of the default pick. */
  default: number;
  choices: { key: string; label: string }[];
  sessions: SessionDTO[];
}

/* POST /inbox/{id}/{action} (daemon/routes_actions.py inbox_action).
   "permission" (engine branch desktop/permission-items) takes {request,
   digest, choice}: the person's answer to the request the item showed. */
export type InboxAction = "answer" | "approve" | "reject" | "accept" | "decline" | "review" | "merge" | "read" | "permission";

/* What an action answers: a message or status, a reviewer, or a job id. */
export interface InboxActionResult {
  message?: string;
  status?: string;
  reviewer?: string | null;
  checks_running?: boolean;
  job?: string;
  kind?: string;
  read?: unknown;
  /* A permission answer: how it went in, and whether the request then cleared. */
  choice?: string;
  via?: string;
  cleared?: boolean;
  rule?: string | null;
}

/* POST /agents/{id}/message, /close, /kill (routes_actions.py). `status` is
   agents.send_message's own word for how the message went (queued, delivered ...). */
export interface MessageResult {
  status: string;
}

/* `stopped`: the agent was still running and had to be stopped first. */
export interface CloseResult {
  closed: string;
  stopped: boolean;
}

export interface KillResult {
  killed: string;
}

/* POST /agents/{id}/rewind (routes_actions.rewind_agent, brindle/rewind.py):
   the worktree back to how it was after turn `to`, the worker stopped and a
   fresh one started there (on `profile`, default the same) with a brief of
   turns 1..to and `note`. A job; job.done's result is the fresh worker. */
export interface Rewind {
  to: number;
  profile?: string;
  note?: string;
}

export interface Rewound {
  agent: string;
  workspace_id: string;
}

/* POST /sessions/{root}/handover (routes_actions.handover_session): `to` is a
   branch name or a worktree path (workspaces.checkout_for_target); `note`
   becomes the new supervisor's first message. */
export interface Handover {
  to: string;
  note?: string;
}

/* POST /sessions/{root}/transfer (routes_actions.transfer_session, engine
   branch desktop/transfer-route): a scratch session's commits replayed onto
   a new branch of the repo at `target_path` (scratch.transfer_to_repo);
   `branch` defaults to brindle/from-<scratch>. Refused at once (400
   "invalid") for a session that isn't scratch; otherwise a job
   ("session.transfer") whose failure says why (a folder outside a git
   repo, a conflict replaying the work). Sent again while it runs, the same
   target and branch get the same job, another target a 409. */
export interface Transfer {
  target_path: string;
  branch?: string;
}

/* job.done's result: the workspace made in the target repo, and how many commits moved. */
export interface Transferred {
  workspace_id: string;
  branch: string;
  repo_root: string;
  commits: number;
}

export interface DaemonConfig {
  /* Where the daemon listens, e.g. http://127.0.0.1:47321 (routes live under /v1). */
  baseUrl: string;
  /* The per-start token from ~/.brindle/daemon.json. */
  token: string;
  fetch?: typeof fetch;
}

export class DaemonError extends Error {
  /* The engine's ServiceError code ("not_found", "conflict", ...), "unreachable"
     when no response came back, or "http" for a response without an error body. */
  readonly code: string;
  readonly status: number;
  /* Set on a 409 that lists choices instead of an error (code "conflict"). */
  readonly conflict?: SessionConflict;
  /* Set on a 409 "preflight": what the provider needs before it can be launched. */
  readonly preflight?: PreflightDTO;
  /* Set on a 409 "not_ready": what keeps the session from a PR (session_review.NotReady). */
  readonly reasons?: string[];

  constructor(code: string, message: string, status: number, conflict?: SessionConflict, preflight?: PreflightDTO, reasons?: string[]) {
    super(message);
    this.name = "DaemonError";
    this.code = code;
    this.status = status;
    this.conflict = conflict;
    this.preflight = preflight;
    this.reasons = reasons;
  }
}

type Query = Record<string, string | number | null | undefined>;

export class DaemonClient {
  readonly baseUrl: string;
  readonly token: string;
  private readonly fetchImpl: typeof fetch;

  constructor(config: DaemonConfig) {
    this.baseUrl = config.baseUrl.replace(/\/+$/, "");
    this.token = config.token;
    this.fetchImpl = config.fetch ?? ((input, init) => fetch(input, init));
  }

  meta() {
    return this.get<Meta>("/meta");
  }

  repos() {
    return this.get<Repo[]>("/repos");
  }

  /* `git clone url` into `parent_dir` (engine branch feat/repo-clone-route),
     a job (repo.clone): job.progress carries each `line` git prints,
     job.done the clone's `path`, job.failed git's own message (an auth
     failure among them). */
  cloneRepo(url: string, parent_dir: string) {
    return this.post<Job>("/repos/clone", { url, parent_dir });
  }

  /* A folder on this machine, as a repo brindle works in. */
  openRepo(path: string) {
    return this.post<Repo>("/repos/open", { path });
  }

  sessions(repo: string) {
    return this.get<SessionDTO[]>("/sessions", { repo });
  }

  /* Throws a DaemonError with `conflict` set when sessions already run in the
     repo: ask, then call again with `choice` and `session_id`. */
  startSession(body: StartSession) {
    return this.post<StartResult>("/sessions", body);
  }

  /* Resume a paused session (or recover its chat if it has stopped). */
  continueSession(root: string) {
    return this.post<ContinueResult>(`/sessions/${encodeURIComponent(root)}/continue`);
  }

  /* The ids of the agents it paused. */
  pauseSession(root: string) {
    return this.post<{ paused: string[] }>(`/sessions/${encodeURIComponent(root)}/pause`);
  }

  /* Closes the whole session: its supervisor and every agent under it are
     stopped and hidden (the session leaves GET /sessions); branches and
     worktrees stay. `closed`: the agents it closed; `stopped`: those of them
     that were running. Closing twice closes nothing more. */
  closeSession(root: string) {
    return this.post<{ closed: string[]; stopped: string[] }>(`/sessions/${encodeURIComponent(root)}/close`);
  }

  /* The new supervisor's session: the goal, milestones, workers and queued
     tasks move to it, and this one is paused. */
  handover(root: string, body: Handover) {
    return this.post<SessionDTO>(`/sessions/${encodeURIComponent(root)}/handover`, body);
  }

  /* A scratch session's work into a real repo: a job whose result is a Transferred. */
  transfer(root: string, body: Transfer) {
    return this.post<Job>(`/sessions/${encodeURIComponent(root)}/transfer`, body);
  }

  /* Whether brindle judges the session ready for a PR, and what is missing when not. */
  prReadiness(root: string) {
    return this.get<SessionReadiness>(`${this.sessionPath(root)}/pr/readiness`);
  }

  /* The session branch against its PR base (throws DaemonError "not_ready" with `reasons` until it is). */
  sessionDiff(root: string) {
    return this.get<SessionDiff>(`${this.sessionPath(root)}/diff`);
  }

  /* The PR's title and body as brindle drafts them. */
  prDraft(root: string) {
    return this.get<PrDraft>(`${this.sessionPath(root)}/pr/draft`);
  }

  /* Any file of the session's checkout, with the blob the edit below takes. */
  sessionFile(root: string, path: string) {
    return this.get<FileContent>(`${this.sessionPath(root)}/file`, { path });
  }

  /* The edit, committed on the session branch; without `base`, a new file. */
  putSessionFile(root: string, body: { path: string; content: string; base?: string }) {
    return this.put<SessionFileEdited>(`${this.sessionPath(root)}/file`, body);
  }

  deleteSessionFile(root: string, body: { path: string; base: string }) {
    return this.delete<SessionFileEdited>(`${this.sessionPath(root)}/file`, body);
  }

  /* Pushes the session branch and opens its PR: a job whose result carries the URL. */
  openSessionPr(root: string, body: OpenSessionPr = {}) {
    return this.post<Job>(`${this.sessionPath(root)}/pr`, body);
  }

  snapshot(repo?: string) {
    return this.get<WorkspaceDTO[]>("/snapshot", { repo });
  }

  /* null when the session has no goal set. */
  goal(repo: string, root?: string) {
    return this.get<GoalDTO | null>("/goal", { repo, root });
  }

  /* Autopilot on or off for the session's goal. */
  setGoalEnabled(root: string, enabled: boolean) {
    return this.post<GoalEnabledAck>("/goal/enabled", { root, enabled });
  }

  inbox(repo?: string) {
    return this.get<InboxItemDTO[]>("/inbox", { repo });
  }

  /* `id` is the item's agent_id (an org notice's notice_id for "read"). A long
     action (merge) answers with a job, whose end arrives on the bus. */
  inboxAction(id: string, action: InboxAction, body: Record<string, unknown> = {}) {
    return this.post<InboxActionResult>(
      `/inbox/${encodeURIComponent(id)}/${encodeURIComponent(action)}`,
      body,
    );
  }

  agent(id: string) {
    return this.get<AgentDTO>(`/agents/${encodeURIComponent(id)}`);
  }

  /* The person's message, queued until the agent is idle. */
  message(id: string, text: string) {
    return this.post<MessageResult>(`/agents/${encodeURIComponent(id)}/message`, { text });
  }

  /* Hide the agent for good, stopping it first if it runs; its record and work stay. */
  close(id: string) {
    return this.post<CloseResult>(`/agents/${encodeURIComponent(id)}/close`, {});
  }

  /* Stop the agent and delete its record. */
  kill(id: string) {
    return this.post<KillResult>(`/agents/${encodeURIComponent(id)}/kill`, {});
  }

  /* A job (its result a Rewound); a 409 preflight when the fresh worker's provider can't launch. */
  rewind(id: string, body: Rewind) {
    return this.post<Job>(`/agents/${encodeURIComponent(id)}/rewind`, body);
  }

  diff(workspaceId: string) {
    return this.get<Diff>(`${this.workspacePath(workspaceId)}/diff`);
  }

  /* A file of the worker's worktree, for an inline edit in the review sheet. */
  readFile(workspaceId: string, path: string) {
    return this.get<FileContent>(`${this.workspacePath(workspaceId)}/file`, { path });
  }

  /* The edit, committed on the worker's branch; `base` is the blob the
     app showed (throws DaemonError "stale" or "busy" on a 409). */
  editFile(workspaceId: string, body: { path: string; content: string; base: string }) {
    return this.put<FileEdited>(`${this.workspacePath(workspaceId)}/file`, body);
  }

  /* What a provider's CLI still needs before brindle can launch it. */
  async preflight(provider: string, repo?: string | null, context?: Record<string, unknown> | null) {
    const path = `/providers/${encodeURIComponent(provider)}/preflight`;
    const result = context?.repo && context?.profile
      ? await this.post<PreflightItemDTO[]>(path, context)
      : await this.get<PreflightDTO | PreflightItemDTO[]>(path, { repo });
    return preflightResult(result, provider);
  }

  /* A trust or consent item settled by the person here. */
  async settlePreflight(provider: string, item: string, action?: PreflightItemDTO["action"], repo?: string | null) {
    if (action?.name === "check") return this.preflight(provider, repo, action.body);
    if (action) {
      await this.request(this.url(action.path.replace(/^\/v1(?=\/)/, "")), {
        method: action.method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...action.body, accept: true }),
      });
      return this.preflight(provider, repo);
    }
    return this.post<PreflightDTO>(`/providers/${encodeURIComponent(provider)}/preflight/${encodeURIComponent(item)}`, { accept: true });
  }

  /* Starts a reviewer on a worker's branch; its verdict arrives as review.submitted. */
  review(workspaceId: string, body: { profile?: string; focus?: string } = {}) {
    return this.post<ReviewStarted>(`${this.workspacePath(workspaceId)}/review`, body);
  }

  merge(workspaceId: string, body: { squash?: boolean } = {}) {
    return this.post<Job>(`${this.workspacePath(workspaceId)}/merge`, body);
  }

  /* Rebases on the base branch, or merges it in with `merge: true`. */
  sync(workspaceId: string, body: { merge?: boolean } = {}) {
    return this.post<Job>(`${this.workspacePath(workspaceId)}/sync`, body);
  }

  pr(workspaceId: string, body: { title?: string; draft?: boolean } = {}) {
    return this.post<Job>(`${this.workspacePath(workspaceId)}/pr`, body);
  }

  remove(workspaceId: string, body: { force?: boolean; delete_branch?: boolean } = {}) {
    return this.post<Removed>(`${this.workspacePath(workspaceId)}/remove`, body);
  }

  /* The repo's accepted decisions, oldest first. */
  decisions(repo: string) {
    return this.get<DecisionDTO[]>("/decisions", { repo });
  }

  /* Throws a DaemonError "conflict" naming the decision in the way (its id's
     first 8 characters in the message) when one already covers the topic. */
  recordDecision(body: DecisionPut) {
    return this.post<DecisionRecorded>("/decisions", body);
  }

  kbSources(repo: string) {
    return this.get<KbSource[]>("/kb/sources", { repo });
  }

  /* The best chunks of the repo's documents for `q`; none for an empty query. */
  kbSearch(repo: string, q: string) {
    return this.get<KbHit[]>("/kb/search", { repo, q });
  }

  /* Adds a folder of documents to the repo's knowledge and indexes it
     (engine branch desktop/kb-sources: POST /kb/sources?repo=, a job whose
     job.done means the folder is indexed). `isPrivate`: the folder is this
     person's, not the team's. */
  addKbSource(repo: string, path: string, isPrivate = false) {
    return this.post<Job>(`/kb/sources?repo=${encodeURIComponent(repo)}`, isPrivate ? { path, private: true } : { path });
  }

  /* Removes a source with everything indexed from it (DELETE /kb/sources/{id}?repo=). */
  removeKbSource(repo: string, id: string) {
    return this.delete<KbSourceRemoved>(`/kb/sources/${encodeURIComponent(id)}?repo=${encodeURIComponent(repo)}`, {});
  }

  /* Indexes a source again (POST /kb/sources/{id}/sync?repo=): a job, done when the index is current. */
  syncKbSource(repo: string, id: string) {
    return this.post<Job>(`/kb/sources/${encodeURIComponent(id)}/sync?repo=${encodeURIComponent(repo)}`, {});
  }

  profile(name: string, repo?: string) {
    return this.get<ProfileDetail>(`/profiles/${encodeURIComponent(name)}`, { repo });
  }

  patchConfig(body: ConfigPatch) {
    return this.patch<ConfigUpdated>("/config", body);
  }

  /* The person's own settings (~/.brindle/config.json's claude_permission_mode). */
  userSettings() {
    return this.get<UserSettings>("/user-settings");
  }

  patchUserSettings(body: Partial<UserSettings>) {
    return this.patch<UserSettings>("/user-settings", body);
  }

  debug() {
    return this.get<DebugState>("/debug");
  }

  setDebug(enabled: boolean) {
    return this.put<DebugState>("/debug", { enabled });
  }

  debugLog(entry: DebugEntry) {
    return this.post<{ ok: boolean }>("/debug/log", entry);
  }

  changePermissions(body: PermissionChange) {
    return this.post<PermissionChanged>("/permissions", body);
  }

  permissionEditor(repo: string) {
    return this.get<PermissionEditorState>("/permission-editor", { repo });
  }

  reviewPermissionRule(body: PermissionEditorRequest) {
    return this.post<PermissionEditorReview>("/permission-editor/review", body);
  }

  savePermissionRule(body: PermissionEditorRequest & { proposal: string }) {
    return this.post<PermissionEditorSaved>("/permission-editor/save", body);
  }

  verifyPermissionAction(tool: string) {
    return this.post<PermissionVerifyDTO>("/permission-editor/verify", { tool });
  }

  /* GET /grants (routes_read.list_grants): the live grants the person gave from the Inbox. */
  grants() {
    return this.get<GrantDTO[]>("/grants");
  }

  /* POST /grants/{id}/revoke (routes_actions.revoke_grant). */
  revokeGrant(id: string) {
    return this.post<{ revoked: string }>(`/grants/${encodeURIComponent(id)}/revoke`);
  }

  /* Starts a sign-in as a job: a job.progress event carries the URL to open
     in the system browser (or, with `device`, the code to type); job.done
     means the daemon stored the tokens. */
  /* `restart`: a sign-in still running is ended and a fresh one started (Try again). */
  accountLogin(device = false, restart = false) {
    return this.post<Job>("/account/login", restart ? { device, restart } : { device });
  }

  accountLogout() {
    return this.post<AccountSaid>("/account/logout", {});
  }

  switchOrg(org: string) {
    return this.post<AccountSaid>("/account/switch-org", { org });
  }

  profiles(repo?: string) {
    return this.get<ProfileDTO[]>("/profiles", { repo });
  }

  createProfile(body: ProfileNew) {
    return this.post<ProfileCreated>("/profiles", body);
  }

  /* Every rule pack that would load for the repo (profiles.list_rule_packs),
     each with its one-line description. Ahead of the engine: GET /rules. */
  rulePacks(repo: string) {
    return this.get<RulePackInfo[]>("/rules", { repo });
  }

  config(repo?: string) {
    return this.get<Config>("/config", { repo });
  }

  /* Which profiles each weight tier tries, the reviewer, the excluded
     providers (each with the scope it comes from) and every profile's
     model, provider, availability and cost tier. */
  routing(repo: string) {
    return this.get<RoutingDTO>("/routing", { repo });
  }

  putRouting(body: RoutingPut) {
    return this.put<RoutingUpdated>("/routing", body);
  }

  doctor(repo?: string) {
    return this.get<DoctorCheck[]>("/doctor", { repo });
  }

  /* With `repo`, the learner's kept note for it comes too. */
  account(repo?: string) {
    return this.get<AccountInfo>("/account", { repo });
  }

  /* The provider CLI's own sign-in (claude auth login, codex login ...), in a
     terminal of the daemon's: the browser and the CLI do the signing in.
     `method`, where the CLI has a flag for it (picker.LOGIN_METHODS:
     claude's claudeai / console / sso, codex's device). `restart`: a login
     still running is ended and the CLI started afresh, rather than its
     terminal handed back (Try again). */
  providerLogin(provider: string, method?: string, restart = false, context?: Record<string, unknown> | null) {
    return this.post<ActionTerminal>(`/providers/${encodeURIComponent(provider)}/login`, { ...context, ...(method ? { method } : {}), ...(restart ? { restart } : {}) });
  }

  /* Each provider CLI as brindle detects it: where it is, its version, whether it's signed in. */
  providersInstalled() {
    return this.get<ProviderInstalled[]>("/providers/installed");
  }

  /* Runs the maker's official installer as-is, into its normal location, a
     job (provider.install): job.progress carries each `line` it prints,
     job.done the CLI as detected afterwards, job.failed with why. One per
     provider: a second press gets the running job back. */
  installProvider(provider: string) {
    return this.post<Job>(`/providers/${encodeURIComponent(provider)}/install`, {});
  }

  /* Takes the provider out of brindle: it stops using the CLI, which stays
     installed (brindle never uninstalls); answers the provider, now off. */
  removeProvider(provider: string) {
    return this.delete<ProviderInstalled>(`/providers/${encodeURIComponent(provider)}`, {});
  }

  /* Runs `path`, one of the copies found, and turns the provider back on
     (an off one comes back without running any installer); answers the provider. */
  pickProviderInstall(provider: string, path: string) {
    return this.post<ProviderInstalled>(`/providers/${encodeURIComponent(provider)}/use-mine`, { path });
  }

  /* One line the person typed or pasted (a sign-in's code, a choice), typed
     into a running action terminal, then Enter unless `submit` is false. */
  terminalInput(terminal: string, text: string, submit = true) {
    return this.post<TerminalInputSent>(`/terminals/${encodeURIComponent(terminal)}/input`, submit ? { text } : { text, submit });
  }

  /* The person's own login shell ($SHELL -l) in `cwd`, a repo's root or one
     of brindle's worktrees of it (routes_actions.shell_terminal): a new
     terminal of the daemon's each call, attached to as any action's. */
  openShell(cwd: string) {
    return this.post<ActionTerminal>("/terminals/shell", { cwd });
  }

  /* Ends a terminal's command and its window (a shell's, an action's). */
  closeTerminal(terminal: string) {
    return this.delete<TerminalClosed>(`/terminals/${encodeURIComponent(terminal)}`, {});
  }

  /* Stores a key as `brindle keys set NAME` does (PUT /keys/{name}): write-only,
     the answer carries the last four characters and never the value. */
  putKey(name: string, value: string, repo?: string) {
    return this.put<KeyStored>(`/keys/${encodeURIComponent(name)}`, repo ? { value, repo } : { value });
  }

  /* `brindle keys set NAME` in a terminal of the daemon's: the value is typed
     there, hidden; the app never sees it. */
  setKey(name: string, repo?: string) {
    return this.post<ActionTerminal>(`/keys/${encodeURIComponent(name)}/set`, repo ? { repo } : {});
  }

  /* `uv tool upgrade brindle` on the engine itself, a job
     (routes_actions.upgrade_engine): job.progress while it runs, then
     job.done, or job.failed with uv's last lines in the error's `tail`. The
     engine exits after a success, and the shell starts the new one on the
     next request. A press while one runs gets that job back. */
  upgradeEngine() {
    return this.post<Job>("/engine/upgrade", {});
  }

  /* What `brindle permissions install-codex-hook --yes` does, on the engine,
     in the background (routes_actions.setup_codex_hook: the inbox's
     "codex_hook_trust" setup item): Codex trusts brindle's permission hook
     again. A job; job.done ends it, or job.failed with why. A press while
     one runs gets that job back. */
  setupCodexHook() {
    return this.post<Job>("/setup/codex-hook", {});
  }

  cost(repo?: string, days?: number) {
    return this.get<Cost>("/cost", { repo, days });
  }

  /* Each provider's subscription windows as its own CLI reports them, and the near ones. */
  usage() {
    return this.get<Usage>("/usage");
  }

  /* The URL of WS /v1/events. The token is never in it: the stream sends it
     as its first frame (see events.ts). */
  eventsUrl(since: number, repo?: string): string {
    const url = new URL(this.url("/events", { since, repo }));
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    return url.toString();
  }

  private url(path: string, query: Query = {}): string {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
    }
    const qs = params.toString();
    return `${this.baseUrl}/v1${path}${qs ? `?${qs}` : ""}`;
  }

  private workspacePath(id: string): string {
    return `/workspaces/${encodeURIComponent(id)}`;
  }

  private sessionPath(root: string): string {
    return `/sessions/${encodeURIComponent(root)}`;
  }

  private delete<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>(this.url(path), {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  private get<T>(path: string, query?: Query): Promise<T> {
    return this.request<T>(this.url(path, query), {});
  }

  private post<T>(path: string, body: unknown = {}): Promise<T> {
    return this.request<T>(this.url(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  private put<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>(this.url(path), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  private patch<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>(this.url(path), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  private async request<T>(url: string, init: RequestInit): Promise<T> {
    let response: Response;
    try {
      response = await this.fetchImpl(url, {
        ...init,
        headers: { ...init.headers, Authorization: `Bearer ${this.token}`, Accept: "application/json" },
      });
    } catch (e) {
      throw new DaemonError("unreachable", e instanceof Error ? e.message : String(e), 0);
    }
    const body: unknown = await response.json().catch(() => undefined);
    if (!response.ok) {
      const got = body as { error?: { code?: string; message?: string }; conflict?: SessionConflict; preflight?: PreflightDTO | PreflightItemDTO[]; reasons?: string[] } | undefined;
      if (got?.conflict) throw new DaemonError("conflict", got.conflict.message, response.status, got.conflict);
      const err = got?.error;
      if (got?.preflight) {
        const preflight = preflightResult(got.preflight);
        throw new DaemonError("preflight", err?.message ?? `${preflight.provider} needs setting up first`, response.status, undefined, preflight);
      }
      throw new DaemonError(err?.code ?? "http", err?.message ?? response.statusText, response.status, undefined, undefined, Array.isArray(got?.reasons) ? got.reasons : undefined);
    }
    return body as T;
  }
}
