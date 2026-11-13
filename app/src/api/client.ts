/* The typed client for the brindle daemon's v1 HTTP routes
   (brindle/daemon/app.py, routes_read.py). Every request carries the token as
   `Authorization: Bearer <token>`; an engine error arrives as
   {"error": {"code", "message"}} (brindle/service/errors.py) and is thrown as a
   DaemonError with that code. */

import type { WebSocketFactory, WebSocketLike } from "./events";
import { startupStep } from "./startup";
import type {
  AccountDTO,
  AgentDTO,
  AllowAllPlanDTO,
  AskDecisionDTO,
  AskGroupDTO,
  CloudOptionsDTO,
  DecisionDTO,
  GoalDTO,
  InboxItemDTO,
  OrgApprovalPolicyDTO,
  OrgAssignmentDTO,
  OrgAssignmentRequestDTO,
  OrgPermissionsDTO,
  OrgPolicyRequestDTO,
  PermissionLimitsDTO,
  PermissionPolicyDTO,
  PermissionRuleDTO,
  PlanCommentDTO,
  PlanDTO,
  PreflightItemDTO,
  ProfileDTO,
  ProviderConnectionDTO,
  RepoDTO,
  SharedDesignDTO,
  RoutingDTO,
  SavedActionDTO,
  SessionDTO,
  WorkspaceDTO,
} from "./schema";
import { engineAttachment, type Attachment } from "./attachments";

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
   The client adapts those unresolved items to the card's PreflightDTO.
   "setup" (the CLI, or tmux, to install) and "off" (the CLI turned off in
   brindle) have no route: only the person settles them, outside the card. */
export type PreflightKind = "signin" | "key" | "trust" | "consent" | "setup" | "off";

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
   when it flips. `pr`: once the person has opened the PR, where it is and
   `unpushed`, how many session commits aren't on it yet (brindle never
   pushes them itself: pushSessionPr does). */
export interface SessionReadiness {
  root_id: string;
  ready: boolean;
  reasons: string[];
  pr?: { url: string; unpushed: number } | null;
}

/* GET /sessions/{root}/diff (session_review.diff): the session branch
   against its PR base, each file with its hunks. The engine attributes each
   file and hunk to whoever wrote it (`authors`, `workers`); the app never
   shows that. Available independently of PR readiness. */
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

/* GET /sessions/{root}/tree (routes_read.session_tree): the session
   checkout's tracked files, paths only, and its branch. */
export interface SessionTree {
  root_id: string;
  branch: string;
  files: string[];
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
  visuals?: boolean;
  /* The daemon is still bringing back sessions from its previous shutdown. */
  resuming?: boolean;
  /* Unix seconds when startup recovery ended; absent on older engines. */
  recovered_at?: number | null;
  /* How far that has come: sessions finished with, of those it is bringing back. */
  recovery?: { done: number; total: number; pending?: string[] } | null;
  engine_version: string;
  api_version: string;
  pid: number;
  db_path: string;
  tmux: boolean;
  airgap: boolean;
}

/* The visualizer's wave-1 contract (design/visualizer-build-plan.html §4; brindle/visuals.py,
   daemon/routes_visuals.py): a page an agent published into a session, its options when it asks
   the person to choose, and the person's answer once given. `rev` is the revision on screen; an
   answer names it, and a newer revision makes an older one's answer a 409. */
export interface VisualOption {
  id: string;
  label: string;
  detail?: string;
}

export interface VisualAnswer {
  option: string | null;
  note: string;
  at: number;
  /* Paths of the files attached to the answer. */
  attachments?: string[];
}

export interface VisualDTO {
  id: string;
  session: string;
  /* The agent that receives the answer, and whether its provider takes files. */
  answer_agent_id?: string;
  accepts_files?: boolean;
  title: string;
  /* The agent that published it. */
  by: string;
  kind: "visual" | "plan";
  rev: number;
  question?: string;
  options?: VisualOption[];
  answer?: VisualAnswer | null;
  kept: boolean;
  unread: boolean;
  created_at: number;
}

/* Knowledge › Kept visuals: a copy kept from a session, readable after the session is gone. */
export interface KeptVisualDTO {
  id: string;
  title: string;
  from_session_name: string;
  repo: string;
  answer?: VisualAnswer | null;
  kept_at: number;
}

/* A planning doc (brindle/plans.py): markdown the app renders itself, by section. */
export type Plan = PlanDTO;
export type PlanComment = PlanCommentDTO;
export type SharedDesign = SharedDesignDTO;

/* The person's verdict on the plan revision on screen: approve, or redirect with what to change. */
export interface PlanVerdict {
  rev: number;
  /* The frozen live version being built, when this is a live doc. */
  version?: number;
  approved: boolean;
  picks: Record<string, string>;
  comments: PlanComment[];
  note: string;
  /* Paths of the files attached to the note. */
  attachments?: string[];
}

/* Where a new comment on the design doc is anchored (brindle/plan_docs.py): a text range of a
   section (`start` is a hint, `quote` the section's own text there), a diagram, a decision, or a
   whole part of the doc (a section, "ask", "lede", "split"). */
export type PlanAnchor =
  | { kind: "text"; section: string; start: number; quote: string }
  | { kind: "diagram"; section: string }
  | { kind: "decision"; decision: string; option?: string }
  | { kind: "section"; section: string };

/* A change to a section's text: `before` at `start` becomes `after` (an empty `before` inserts). */
export interface PlanChange {
  section: string;
  start: number;
  before: string;
  after: string;
}

export type Repo = RepoDTO;

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
  /* Where the folder is for the signed-in account: local (the default; it stays on this machine),
     adding, in_library (Pro) or removing (shown until every doc is deleted). */
  library_state?: "local" | "adding" | "in_library" | "removing";
  /* Files of an added folder the last upload could not send. */
  library_pending?: number;
  /* Docs of a folder being removed from the library that the server has not deleted yet. */
  library_removing?: number;
}

/* POST /kb/sources/{id}/library with dry_run: what adding a folder to the library would send. */
export interface KbLibraryPlan {
  files: { name: string; bytes: number }[];
  skipped: { name: string; reason: string }[];
  allowed: boolean;
  /* Names this exact list; adding with it uploads these files only. */
  token: string;
}

/* The answer to adding: the folder changed since the list (`changed`, with a fresh one), or it went up. */
export type KbLibraryAdded = ({ changed: true } & KbLibraryPlan) | { changed?: undefined };

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

/* brindle Pro's Library of approved plans, proxied by the daemon (routes_read.analysis_docs).
   The Wire* types are the server's own shapes (pawdelta-web pro_api.analysis_docs: _listed,
   _view, topics); the client methods turn them into the AnalysisDoc* below. A scope is
   {kind: user | group | org, id}; the daemon's filter spells it `user`, `team:<id>` or `org:<id>`. */
export interface PlanReviewerChoice {
  sub: string;
  required: boolean;
}

export interface LibraryPerson {
  sub: string;
  email: string;
}

/* An org admin's waiver of a review rule for one revision, or a stand-in for it until `until` (Unix seconds). */
export interface PlanPolicyOverride {
  rev: number;
  org_id: string;
  policy: string;
  action: "waive" | "stand_in";
  reason: string;
  stand_in?: string;
  until?: number;
}

/* An org review rule (docs/shared-designs-api.md "Review policies"). `needs.name`, `eligible`, `set_by`
   and `set_at` come back to an admin on a read; the daemon leaves them out of a save. */
export interface ReviewPolicyRule {
  id: string;
  name: string;
  when: { kind: "paths" | "label" | "any"; paths?: string[]; label?: string };
  needs: { kind: "team" | "role" | "person"; id: string; name?: string };
  approvals: number;
  note?: string | null;
  eligible?: number;
  set_by?: string;
  set_at?: number;
  reviewer_editors?: { kind: string; id?: string }[];
}

export interface ReviewPoliciesState {
  org_id: string;
  version: number;
  policies: ReviewPolicyRule[];
  directory: {
    teams: { id: string; name: string; path?: string; members?: number }[];
    roles: { id: string; name: string; builtin?: boolean; members?: number }[];
  };
  in_review: { truncated?: boolean; policies: { policy: string; count: number; designs: { doc_id: string; title: string }[] }[] };
}

/* What saving a draft list would do to the designs in review (`truncated`: count is a lower bound). */
export interface DraftImpact {
  org_id: string;
  truncated: boolean;
  count: number;
  designs: { doc_id: string; title: string }[];
  reopens_gates: boolean;
  reopened: number;
}

/* A rule that would cover a design, before it is shared. */
export interface PolicyPreviewRow {
  org_id: string;
  policy: string;
  name: string;
  note: string | null;
  when: string | null;
  label: string | null;
  paths: string[];
  matched: string[];
  needs_kind: string | null;
  needs_name: string | null;
  approvals: number;
  eligible: number | null;
}

export interface DocScope {
  kind: string;
  id: string;
  /* From GET /analysis-docs/scopes only: the team's or org's name, and whether the person may share into it. */
  name?: string;
  can_share?: boolean;
}

/* A Library folder as the server lists it: private (its creator only) or shared (the scope's members). */
export type FolderVisibility = "private" | "shared";
export interface LibraryFolder {
  id: string;
  name: string;
  visibility: FolderVisibility;
}
/* One scope's folders (GET /library/folders). */
export interface FolderGroup {
  scope: DocScope;
  folders: LibraryFolder[];
}

export interface DocSource {
  kind?: string;
  ref?: string;
  session?: string;
}

/* A library graph or a path neighbourhood. Paths are repo-relative. */
export interface MapNode {
  id: string;
  kind: "doc" | "path";
  title?: string;
  doc_id?: string;
  /* Indexed on this device: open the file instead of the hosted doc reader. */
  local_path?: string;
  updated_at?: number;
  topics?: string[];
  path?: string;
  paths?: string[];
  source?: DocSource;
  created_by?: string;
  scope?: DocScope;
  created_at?: number;
  /* A doc's category (see AnalysisDoc.category). */
  category?: string;
}

export interface MapEdge {
  source: string;
  target: string;
  kind: string;
}

export interface KnowledgeMap {
  nodes: MapNode[];
  edges: MapEdge[];
}

interface WireMentions {
  files?: string[];
  symbols?: string[];
  services?: string[];
}

interface WireListedDoc {
  doc_id: string;
  version?: number;
  title?: string;
  summary?: string;
  kind?: string;
  findings?: string[];
  open_questions?: string[];
  topics?: string[];
  category?: string;
  mentions?: WireMentions;
  related?: string[];
  supersedes?: string[];
  superseded_by?: string | null;
  stale?: boolean;
  used_in_briefs?: number;
  created_at?: number;
  scope?: DocScope;
}

interface WireDoc extends Omit<WireListedDoc, "doc_id"> {
  id: string;
  files?: { name: string; md: string }[];
  /* Every scope it is shared into, its owner's (`scope`) among them. */
  scopes?: DocScope[];
}

export interface AnalysisDoc {
  id: string;
  title?: string;
  kind?: string;
  scope?: DocScope;
  summary?: string;
  topics?: string[];
  /* A short label ("Security", "Desktop updates") the server's reader model
     picks when the doc is created and reuses across the scope; absent when
     the server gave none. */
  category?: string;
  version?: number;
  created_at?: number;
  updated_at?: number;
  created_by?: string;
  source?: DocSource;
  used_in_briefs?: number;
  findings?: string[];
  open_questions?: string[];
  stale?: boolean;
  /* Other docs' ids. */
  related?: string[];
  supersedes?: string[];
  superseded_by?: string | null;
}

/* GET /analysis-docs/{id}: the files as written and what the server read from them. */
export interface AnalysisDocDetail extends AnalysisDoc {
  files: { name: string; md: string }[];
  /* The files, symbols and services it names, in that order. */
  mentions: string[];
  /* The scopes it is shared into besides its owner's. */
  shared_with: DocScope[];
  /* Optional plan record fields, using the plan engine's DTO names. */
  revisions?: { rev: number; author: string; source: string; changed: string[]; at: number }[];
  threads?: { id: number; author: string; text: string; replies: { text: string }[] }[];
  decisions?: { id: string; title: string; mine?: string | null; confirmed?: string | null; options: { id: string; label: string }[] }[];
  verdict?: { approved: boolean; picks?: Record<string, string> } | null;
  /* A shared design's review state (shared-designs-api.md: GET /analysis-docs/{id} `design`):
     the revision on screen, who is asked to review (`required` or optional) and who has signed it. */
  design?: { rev?: number; reviewers?: { sub: string; required?: boolean }[]; sign_offs?: { sub: string; rev?: number }[]; pending_reviewers?: unknown[]; pending_policies?: unknown[]; ready?: boolean } | null;
}

export interface DocTopic {
  name: string;
  count?: number;
}

const sameScope = (a: DocScope, b: DocScope) => a.kind === b.kind && a.id === b.id;

/* A blank or non-text category is no category. */
export function categoryOf(raw: unknown): { category?: string } {
  return typeof raw === "string" && raw.trim() ? { category: raw.trim() } : {};
}

export function listedDoc({ doc_id, mentions: _, category, ...rest }: WireListedDoc): AnalysisDoc {
  return { ...rest, id: doc_id, ...categoryOf(category) };
}

export function docDetail({ mentions, scopes, files, category, ...rest }: WireDoc): AnalysisDocDetail {
  return {
    ...rest,
    ...categoryOf(category),
    files: files ?? [],
    mentions: [...(mentions?.files ?? []), ...(mentions?.symbols ?? []), ...(mentions?.services ?? [])],
    shared_with: (scopes ?? []).filter((s) => !rest.scope || !sameScope(s, rest.scope)),
  };
}

/* GET /analysis-docs/results (analysis_brief.results): review rounds of finished tasks
   whose brief got a doc block, against similar ones (same repo, kind and size) without;
   the averages are null until each side has `minimum` tasks. */
export interface DocResults {
  days: number;
  minimum: number;
  enough: boolean;
  briefed: number;
  large: number;
  compared: number;
  similar: number;
  with_brief: number | null;
  without_brief: number | null;
}

export interface DocSessionSummary {
  total: number;
  writing: number;
  updated: number;
  using: number;
}

/* POST /analysis-docs/{id}/export: the file's path in a picked folder, or the file itself. */
export interface DocExport {
  filename: string;
  path?: string;
  content_b64?: string;
}

export type ExportDestination = "download" | "google_docs" | "sharepoint";

/* GET /analysis-docs/policy (the rest of the org's policy passes through untouched). */
export interface DocPolicy {
  export_destinations?: ExportDestination[];
}

export type DocSwitchName = "store" | "share" | "assist" | "prompt_analysis" | "import_upload";

/* GET /analysis-docs/switches: one of the person's own switches. `choice` is
   theirs (null while unset), `on` what applies, `locked` whether their org
   enforces it (`org` then says which way). */
export interface DocSwitch {
  name: DocSwitchName;
  on: boolean;
  choice: "on" | "off" | null;
  locked: boolean;
  org: "enforced_on" | "enforced_off" | null;
}

/* GET /analysis-docs/export-config */
export interface DocExportConfig {
  google_client_id: string | null;
  google_client_secret: string | null;
  microsoft_client_id: string | null;
}

export type TeamDestinationKind = "sharepoint" | "google_drive";

/* GET /analysis-docs/destination */
export interface TeamDestination {
  kind: TeamDestinationKind | null;
  target_name: string | null;
  connected_at: number | string | null;
}

export interface DocsQuery {
  repo?: string;
  scope?: string;
  topic?: string;
  kind?: string;
  session?: string;
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
  provider?: string | null;
  /* How the last check came out; null while no key is stored. */
  status?: "verified" | "rejected" | "unchecked" | null;
  checked_at?: number | null;
  /* Where to get a key. */
  key_page?: string | null;
  /* Storing it makes a signed-in subscription's agents bill the key instead. */
  bills?: boolean;
  personal_keys_denied?: boolean;
  /* Set in the daemon's environment but not stored (POST /keys/{name}/import). */
  importable?: boolean;
}

/* Whether the provider can launch the way it connects (engine branch
   feat/connection-readiness): when not, why, and the one action that fixes it. */
export interface ConnectionReady {
  ok: boolean;
  reason: string | null;
  action: "install" | "sign_in" | "sign_out" | "renew" | "set_key" | null;
}

/* GET /providers/{p}/connection with its readiness: `blocked_by` "codex_login"
   when Codex's ChatGPT login wins over its API key, and the agents that
   stopped on this provider's credentials. */
export type ProviderConnection = ProviderConnectionDTO & {
  ready?: ConnectionReady | null;
  blocked_by?: "codex_login" | null;
  stopped_agents?: { id: string; name: string; profile: string }[];
};

export interface KeyTested {
  name: string;
  status: "verified" | "rejected" | "unchecked";
  checked_at: number | null;
}

export interface Config {
  user: Record<string, unknown>;
  repo?: Record<string, unknown>;
  /* Settings this project's own files set over the person's: key -> the file's scope. */
  overrides?: Record<string, "local" | "repo">;
  /* What the repo's local file itself sets for the keys whose effective value unions every scope's (rules). */
  local?: Record<string, unknown>;
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
   .brindle/config.local.json ("repo") or its committed .brindle/config.json
   ("shared"); only the repo config's own keys,
   each a value of its default's type, or null to unset it. `unset` (scope
   "repo") removes keys from the local file: a project's override, cleared. */
export interface ConfigPatch {
  scope: "user" | "repo" | "shared";
  repo?: string;
  values?: Record<string, unknown>;
  unset?: string[];
}

export interface ConfigUpdated {
  updated: string[];
  path: string;
}

/* GET and PATCH /user-settings (engine branch desktop/user-settings): the
   person's own settings that are not a repo's. `claude_permission_mode`
   (config.claude_permission_mode): "default" holds Claude's permission
   requests for the Inbox; "auto" lets Claude approve most commands itself.
   `opencode_permission_mode` (config.opencode_permission_mode), for OpenCode
   sessions whose profile sets none: "ask" keeps OpenCode's own prompts;
   "auto" lets edits run; "autonomous" asks nothing and brindle's rules decide
   each call, refusing what they can't check (asking again with the rules off).
   A PATCH with any other value is refused (400). */
export type ClaudePermissionMode = "default" | "auto";
export type OpenCodePermissionMode = "ask" | "auto" | "autonomous";

export interface VisualOffer {
  id: string;
  kind: string;
  counts: Record<string, number>;
  expires_at: number;
}

export interface UserSettings {
  visuals?: "on" | "off";
  /* The visuals feature itself (on unless turned off); `visuals` above is only the offers. */
  visuals_enabled?: "on" | "off";
  claude_permission_mode: ClaudePermissionMode;
  /* Absent from an engine older than this setting. */
  opencode_permission_mode?: OpenCodePermissionMode;
  /* Whether launched CLIs get the login shell's proxy and certificate settings
     (on unless turned off). Absent from an engine older than this setting. */
  login_shell_env?: "on" | "off";
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

/* The permission policy (engine: brindle/permissions.py, brindle/permission_grants.py;
   routes in daemon/routes_read.py and routes_actions.py). Nothing is built in:
   every rule, the chosen preset's included, can be edited or removed. */
export type PermissionPreset = "autonomous" | "permissive" | "standard" | "secure";
/* What a request no rule covers does. */
export type PermissionUnmatched = "ask" | "allow" | "deny";
/* GET /permissions/report (permission_asks.report): how often requests asked over the last `days`, and why. */
export interface PermissionReport {
  days: number;
  decisions: number;
  asks: number;
  ask_rate: number;
  avg_wait_seconds: number | null;
  causes: { reason: string; count: number; kinds: string[]; avg_wait_seconds: number | null; example: string }[];
}
export type PermissionDecision = "allow" | "deny" | "ask";
export type PermissionKind = "read" | "write" | "edit" | "bash" | "fetch" | "mcp" | "other";
/* A saved action's kind: a request kind, or a git push. */
export type SavedActionKind = PermissionKind | "push";
/* exact | prefix | glob, or a named matcher: tracked, check, git-readonly, git-push, git-force (a force
   flag, a +refspec or a branch deletion), push ("<source> → <destination>[ @ <remote>]", @default for
   the repo's default branch; a saved push action's pattern always carries its remote), worktree,
   readonly-pipeline, rm-destructive. */
export type PermissionMatchType = "exact" | "prefix" | "glob" | "tracked" | "check" | "git-readonly" | "git-push"
  | "git-force" | "push" | "worktree" | "readonly-pipeline" | "rm-destructive";
export type PermissionLife = "session" | "time" | "forever";
/* How far a saved push approval may reach, narrowest first. */
export type PermissionLevel = "branch" | "destination" | "remote" | "repository" | "all";

/* POST /permissions/rules and PUT /permissions/rules/{id}: a PUT changes only the fields given
   (a preset's rule becomes the person's). `minutes` makes a timed rule, expiring that long from
   now; `expires: null` clears one. `limits` belongs to an ask rule. */
export interface PermissionRulePut {
  kind?: PermissionKind;
  match?: string;
  match_type?: PermissionMatchType;
  decision?: PermissionDecision;
  label?: string;
  limits?: PermissionLimitsDTO | null;
  minutes?: number | null;
  expires?: number | null;
}

/* POST /permissions/rules/{id}/exceptions: requests the rule stops applying to (a deny's exception). */
export interface PermissionExceptionPut {
  kind: PermissionKind;
  match: string;
  match_type?: "exact" | "prefix" | "glob";
}

/* POST /permissions/saved and PUT /permissions/saved/{id}: a saved allow or deny with its
   structured scope. A timed one (`life` "time") takes `minutes` and restarts its expiry on
   every PUT; one for a session takes `session` (the root agent); `repo` null means every repo. */
export interface SavedActionPut {
  kind?: SavedActionKind;
  pattern?: string;
  decision?: "allow" | "deny";
  scope?: string;
  level?: PermissionLevel | null;
  request?: Record<string, unknown>;
  life?: PermissionLife;
  minutes?: number | null;
  session?: string | null;
  repo?: string | null;
  note?: string;
}

/* POST /permissions/asks/decide: one decision on an Inbox item (AskGroupDTO.key). `scope` is one
   of the item's scope values; `mode` once | session | timed (with `minutes`) | forever | deny |
   always-deny, from the item's `modes`. */
export interface AskDecide {
  key: string;
  scope: string;
  mode: "once" | "session" | "timed" | "forever" | "deny" | "always-deny";
  minutes?: number;
}

export interface PermissionDeleted {
  deleted: string;
  describe?: string;
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
  /* The new session's name, kept with it (listed instead of its goal). */
  name?: string;
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

/* POST /demo (routes_actions.start_demo): the session started in `brindle demo`'s practice repo, and that repo's root. */
export type DemoResult = SessionDTO & { repo: string; notes: Note[] };

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
export type InboxAction = "answer" | "approve" | "reject" | "accept" | "decline" | "review" | "merge" | "read" | "permission" | "dismiss" | "pr-not-now";

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

/* GET /storage (disk.storage_report): brindle's disk use. A worktree is
   Tree.to_json: `removable` false with `reason` (uncommitted changes, a live
   session, not brindle's, still in use) when Clean would keep it. */
export type StorageState = "merged" | "abandoned" | "active";

export interface StorageWorktree {
  path: string;
  repo_root: string;
  branch: string;
  sha: string;
  state: StorageState;
  /* Epoch seconds: its last commit, checkout or creation. */
  last_activity: number;
  dirty: boolean;
  live: boolean;
  owned: boolean;
  size: number;
  build_size: number;
  workspace_id: string | null;
  removable: boolean;
  reason: string | null;
}

export interface StorageCategory {
  name: string;
  size: number;
  detail: string;
}

export interface StorageReport {
  worktrees: StorageWorktree[];
  categories: StorageCategory[];
  /* Bytes Clean and retention could give back. */
  reclaimable: number;
  free_bytes: number | null;
  low_disk: string | null;
  dead_branches: { repo_root: string; name: string; sha: string; last_commit: number; merged: boolean }[];
}

/* POST /storage/clean (disk.clean): what went, and what was kept and why
   (the engine refuses a dirty or live worktree). */
export interface StorageCleaned {
  removed: { path: string; repo_root: string; branch: string; state: StorageState; size: number; archived: string | null; branch_kept: string | null }[];
  kept: { path: string; reason: string }[];
}

export interface DaemonConfig {
  /* Where the daemon listens, e.g. http://127.0.0.1:47321 (routes live under /v1). */
  baseUrl: string;
  /* The per-start token from ~/.brindle/daemon.json. */
  token: string;
  fetch?: typeof fetch;
  /* Opens the client's own sockets (visualLive); a DOM WebSocket when absent. */
  connect?: WebSocketFactory;
}

export interface LiveDocState {
  author: string;
  lock: "sign_off" | "author_offline" | null;
  version: { id: number; writers: string[]; signOffs: string[]; snapshot: string } | null;
  /* The gate for this live version, including any org policy checks. */
  gate?: Plan["gate"];
}

/* What a shared design doc's live channel delivers (DaemonClient.visualLive). */
export interface LiveHandlers {
  /* Loro update bytes to import: the doc as it is first (a snapshot), then each other writer's change. */
  onUpdate(bytes: Uint8Array): void;
  /* Another writer's presence in one section's text: Loro EphemeralStore bytes to apply. */
  onAwareness(section: string, bytes: Uint8Array): void;
  onState?(state: LiveDocState): void;
  /* The number this window writes under, a decimal string (relay numbers pass 2**53): set it with
     LoroDoc.setPeerId once the snapshot is in and before the first edit. null: none is free yet,
     so the text stays read only until one arrives. Never sent: write under the doc's own number. */
  onPeer?(peer: `${number}` | null): void;
  /* The channel closed: 4401 a wrong token, 4404 no live text for this visual,
     1006 the connection dropped, LIVE_RELOAD the daemon replaced the text (open the channel
     again for the new one). Locks arrive as state and keep the channel open. */
  onClose?(code: number): void;
}

/* The live channel's close code when the daemon replaced the doc's text with the server relay's:
   whatever was loaded is gone, so open the channel again (pro/live_docs.py RELOAD). */
export const LIVE_RELOAD = 4205;

export interface LiveChannel {
  /* A local change, as LoroDoc.subscribeLocalUpdates hands it over. */
  sendUpdate(bytes: Uint8Array): void;
  /* This writer's caret and name in `section`, as EphemeralStore.subscribeLocalUpdates hands them over. */
  sendAwareness(section: string, bytes: Uint8Array): void;
  freeze(): void;
  reopen(): void;
  close(): void;
}

/* Bytes as base64 and back: the live channel's frames are text, as the shell's stream sends them. */
export function toBase64(bytes: Uint8Array): string {
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(text);
}
export function fromBase64(data: string): Uint8Array {
  const text = atob(data);
  const bytes = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i++) bytes[i] = text.charCodeAt(i);
  return bytes;
}

/* GET/POST/DELETE /github/connection. `expires_at` is an ISO date or unix seconds (the contract doesn't say), or null. */
export interface GithubConnection {
  connected: boolean;
  login: string | null;
  route: "gh" | "token" | null;
  storage: "keychain" | "file" | null;
  expires_at: string | number | null;
  needs_reconnect: boolean;
  gh_available: boolean;
}

export type GithubConnect = { route: "gh" } | { route: "token"; token: string };

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
  /* Set on a 409 "preflight" of POST /demo: the practice repo kept for that launch. */
  repo?: string;

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
  // Before any reads on this connection, including sync's first snapshot.
  readonly createdAt = Date.now() / 1000;
  readonly baseUrl: string;
  readonly token: string;
  private readonly fetchImpl: typeof fetch;
  private readonly connect?: WebSocketFactory;
  private inboxAcknowledgements = 0;

  /* Sync reads begun before an action settled cannot restore its old card.
     Ordinary stream traffic leaves this generation alone. */
  get inboxGeneration(): number {
    return this.inboxAcknowledgements;
  }

  constructor(config: DaemonConfig) {
    this.baseUrl = config.baseUrl.replace(/\/+$/, "");
    this.token = config.token;
    this.fetchImpl = config.fetch ?? ((input, init) => fetch(input, init));
    this.connect = config.connect;
  }

  meta() {
    return this.get<Meta>("/meta");
  }

  /* Ask the engine to bring this session back next (a no-op when it already has). */
  prioritize(root: string) {
    return this.post<unknown>("/recovery/prioritize", { root });
  }

  /* Tell the engine which session is on screen: after a restart, it brings that one back first. */
  viewed(root: string) {
    return this.post<unknown>("/recovery/viewed", { root });
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

  /* Starts Apple's installer for the Command Line Developer Tools (`xcode-select --install`);
     only ever from a click. */
  installDeveloperTools() {
    return this.post<{ started: boolean }>("/developer-tools/install", {});
  }

  /* Connect GitHub (engine branch feat/connect-github): how brindle reaches private
     repositories over HTTPS. Typed here by hand until api-schema.json has the routes.
     POST throws a DaemonError coded github_token_invalid or github_gh_unavailable. */
  githubConnection() {
    return this.get<GithubConnection>("/github/connection");
  }

  connectGithub(body: GithubConnect) {
    return this.post<GithubConnection>("/github/connection", body);
  }

  disconnectGithub() {
    return this.delete<GithubConnection>("/github/connection", {});
  }

  /* A folder on this machine, as a repo brindle works in. */
  openRepo(path: string, init = false) {
    return this.post<Repo>("/repos/open", { path, ...(init ? { init: true } : {}) });
  }

  sessions(repo: string) {
    return this.get<SessionDTO[]>("/sessions", { repo });
  }

  /* Throws a DaemonError with `conflict` set when sessions already run in the
     repo: ask, then call again with `choice` and `session_id`. */
  startSession(body: StartSession) {
    return this.post<StartResult>("/sessions", body);
  }

  setRepoGitIdentity(repo: string, name: string, email: string) {
    return this.post<{ saved: boolean }>("/repos/git-identity", { repo, name, email });
  }

  /* `brindle demo` from the app: a practice repo of brindle's own (never one of the person's)
     and a session started there, on the CLI this Mac is signed in to. Throws a DaemonError with
     `preflight` set as startSession does, and `repo` the practice repo kept for it (the next
     start launches there), and code "airgap" in air-gap mode. */
  startDemo() {
    return this.post<DemoResult>("/demo", {});
  }

  /* DELETE /demo: the practice repo a held demo kept goes (the person didn't go on with it). */
  cancelDemo() {
    return this.delete<{ removed: string[] }>("/demo", {});
  }

  /* Resume a paused session (or recover its chat if it has stopped). */
  continueSession(root: string) {
    return this.post<ContinueResult>(`/sessions/${encodeURIComponent(root)}/continue`);
  }

  /* POST /sessions/{root}/rename (routes_actions.rename_session): an empty
     name clears it, and the session is listed by its goal again. */
  renameSession(root: string, name: string) {
    return this.post<{ id: string; name: string | null }>(`/sessions/${encodeURIComponent(root)}/rename`, { name });
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

  /* The session branch against its PR base, including work still in progress. */
  sessionDiff(root: string) {
    return this.get<SessionDiff>(`${this.sessionPath(root)}/diff`);
  }

  /* The PR's title and body as brindle drafts them. */
  prDraft(root: string) {
    return this.get<PrDraft>(`${this.sessionPath(root)}/pr/draft`);
  }

  /* Every tracked file of the session's checkout. */
  sessionTree(root: string) {
    return this.get<SessionTree>(`${this.sessionPath(root)}/tree`);
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

  /* Pushes the session's commits not on its open PR yet, through the same checks: a job. */
  pushSessionPr(root: string) {
    return this.post<Job>(`${this.sessionPath(root)}/pr/push`, {});
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

  resolvePlanOffer(root: string, offer: string, accept: boolean) {
    return this.postInbox<{ accepted: boolean }>("/goal/plan-offer", { root, offer, accept });
  }

  inbox(repo?: string) {
    return this.get<InboxItemDTO[]>("/inbox", { repo });
  }

  /* `id` is the item's agent_id (an org notice's notice_id for "read"). A long
     action (merge) answers with a job, whose end arrives on the bus. */
  inboxAction(id: string, action: InboxAction, body: Record<string, unknown> = {}) {
    return this.postInbox<InboxActionResult>(
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

  async attach(id: string, file: Attachment) {
    return this.post<{ paths: string[]; text: string }>(`/agents/${encodeURIComponent(id)}/attachments`, await engineAttachment(file));
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
  /* One indexed document's whole text (GET /kb/docs/{id}); id is the number in a map node's `local:kb:<id>`. */
  kbDoc(repo: string, id: string) {
    return this.get<{ id: number; title: string | null; url: string | null; text: string }>(`/kb/docs/${encodeURIComponent(id)}`, { repo });
  }

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

  /* The files adding a folder to the library would upload, and the ones skipped; nothing is sent. */
  kbLibraryPlan(repo: string, id: string) {
    return this.post<KbLibraryPlan>(`/kb/sources/${encodeURIComponent(id)}/library?repo=${encodeURIComponent(repo)}`, { dry_run: true });
  }

  /* Uploads exactly the files `token` (from kbLibraryPlan) listed (403 not_allowed when uploading is off,
     502 when a file was rejected). */
  addKbToLibrary(repo: string, id: string, token: string) {
    return this.post<KbLibraryAdded>(`/kb/sources/${encodeURIComponent(id)}/library?repo=${encodeURIComponent(repo)}`, { token });
  }

  /* Deletes the folder's docs from the library; the folder stays. */
  removeKbFromLibrary(repo: string, id: string) {
    return this.delete<{ left: number }>(`/kb/sources/${encodeURIComponent(id)}/library?repo=${encodeURIComponent(repo)}`, {});
  }

  /* A session's visuals, newest first. An engine without the visualizer answers 404. */
  async visuals(root: string): Promise<VisualDTO[]> {
    const got = await this.get<VisualDTO[] | { visuals?: VisualDTO[] }>(`${this.sessionPath(root)}/visuals`);
    return Array.isArray(got) ? got : (got.visuals ?? []);
  }

  visualOffer(root: string) {
    return this.get<VisualOffer | null>(`${this.sessionPath(root)}/visual-offer`);
  }

  beginVisualInput(root: string, expiresAt: number, signal?: AbortSignal) {
    return this.request<{ generation: number }>(this.url(`${this.sessionPath(root)}/visual-input`), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ begin: true, expires_at: expiresAt }), signal,
    });
  }

  visualInput(root: string, text: string, expiresAt?: number, signal?: AbortSignal,
              observation?: { generation: number; sequence: number; submitted_at: number; typed?: boolean }) {
    return this.request<{ observed: boolean }>(this.url(`${this.sessionPath(root)}/visual-input`), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, expires_at: expiresAt, ...observation }), signal,
    });
  }

  visualTypedWords(root: string, text: string, expiresAt: number, submittedAt: number, signal?: AbortSignal) {
    return this.request<{ observed: boolean }>(this.url(`${this.sessionPath(root)}/visual-input`), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ typed_words: text, expires_at: expiresAt, submitted_at: submittedAt }), signal,
    });
  }

  refreshVisualInput(root: string, observer: number, expiresAt: number, signal: AbortSignal | undefined, submittedAt: number) {
    return this.request<{ generation: number }>(this.url(`${this.sessionPath(root)}/visual-input`), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh: observer, expires_at: expiresAt, submitted_at: submittedAt }), signal,
    });
  }

  resolveVisualOffer(root: string, offer: string, accept: boolean) {
    return this.post<{ accepted: boolean }>(`${this.sessionPath(root)}/visual-offer`, { offer, accept });
  }

  /* The person's answer to the revision on screen; a newer revision makes this a 409. */
  answerVisual(id: string, rev: number, option: string | undefined, note: string, attachments?: string[]) {
    return this.postInbox<VisualDTO>(`/visuals/${encodeURIComponent(id)}/answer`, { rev, option, note, ...(attachments?.length ? { attachments } : {}) });
  }

  /* The person won't answer the revision on screen; the agent is told, the Inbox card clears. */
  dismissVisual(id: string, rev: number) {
    return this.postInbox<VisualDTO>(`/visuals/${encodeURIComponent(id)}/dismiss`, { rev });
  }

  exportVisual(id: string, rev: number, html: string, destination: ExportDestination = "download", markdown?: string) {
    return this.post<{ filename: string; mime: string; content_b64: string }>(`/visuals/${encodeURIComponent(id)}/snapshot`, { rev, html, destination, markdown });
  }

  keepVisual(id: string) {
    return this.post<VisualDTO>(`/visuals/${encodeURIComponent(id)}/keep`, {});
  }

  unkeepVisual(id: string) {
    return this.delete<VisualDTO>(`/visuals/${encodeURIComponent(id)}/keep`, {});
  }

  markVisualRead(id: string, rev: number) {
    return this.postInbox<unknown>(`/visuals/${encodeURIComponent(id)}/read`, { rev });
  }

  /* WS /v1/visuals/{id}/live: a shared design doc's text, edited live by everyone writing in it
     (DocEditor.tsx). The daemon implements this later; test/fakeDaemon.ts serves it now.

     The text is one Loro document per visual (loro-crdt). Each text section is the root LoroText
     named by the section's id ("lede" for the overview), positions in UTF-16 units as Loro's JS
     API and CodeMirror count them. The daemon owns the document: it seeds it from the doc's
     current revision, keeps every update, and is the only one that turns the live text into a
     revision. A client never seeds a section itself (two would each insert the same text).

     Frames, all text (the shell's stream carries text: src-tauri/src/daemon.rs):
     - client → daemon: the token first, as on /v1/events (a wrong one closes with 4401). Then
       {"update": base64} for each local Loro change (LoroDoc.subscribeLocalUpdates), and
       {"awareness": base64, "section": id} for this writer's caret and name in a section
       (an EphemeralStore per section, keyed `<peer>-cm-cursor` / `<peer>-cm-user` by
       loro-codemirror).
     - daemon → client: first, when the doc is co-edited through the server's relay, {"peer": "n"}:
       the relay-issued number this window writes under (a decimal string; numbers pass 2**53).
       The relay refuses a change under any number it did not issue, so the client sets it with
       LoroDoc.setPeerId after importing the snapshot and before its first edit. {"peer": null}:
       none is free yet; the text stays read only until a later {"peer": "n"}. No peer frame: the
       doc is only on this machine, and the client writes under its own number.
     - daemon → client: then state, then {"update": base64}, the whole document (a snapshot); then every
       other client's frames as they were sent, update or awareness, and on joining each
       section's last-known awareness. A visual with no live text closes with 4404. Closing with
       LIVE_RELOAD (4205) means the daemon replaced the text (it took the relay's draft): open the
       channel again and load the new snapshot.
     - daemon → client: {"state": LiveDocState} on joining and whenever the lock or version
       changes. `author` is the author's display name. `lock` is null, "sign_off" (read only
       for everyone), or "author_offline" (read only for others; comments and suggestions
       remain available). Locked channels stay open for reading, awareness and state changes.
       A version contains an increasing id, credited writers, signOffs (account subjects) and
       a base64 snapshot. `gate` is the associated review gate; reopening invalidates its
       sign-offs too. Sign-off and build HTTP requests include `version`, so an old request
       cannot sign or build a withdrawn version. No version means sign-off/build are disabled.
       Readers publish their user awareness in section "__document__", refresh it while open,
       and delete it on departure. Section editors continue to own their own caret awareness.
       An initial 4404 means the document has no live text: keep its HTTP review controls.
       Other connection failures, and any loss after live text/state was received, keep live
       review controls blocked until the channel is available again.
     - author → daemon: {"freeze": true} snapshots the text and credits its writers, then
       locks it for sign-off; {"reopen": true} withdraws that version and its sign-offs.
       The daemon authorizes these commands and rejects text updates while locked.
     Importing is idempotent, so a frame relayed twice (or echoed back) changes nothing. */
  visualLive(id: string, handlers: LiveHandlers): LiveChannel {
    const url = new URL(this.url(`/visuals/${encodeURIComponent(id)}/live`));
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket: WebSocketLike = this.connect ? this.connect(url.toString()) : (new WebSocket(url) as unknown as WebSocketLike);
    let open = false;
    let closed = false;
    const queued: string[] = [];
    const send = (frame: object) => {
      if (closed) return;
      const text = JSON.stringify(frame);
      if (open) socket.send(text);
      else queued.push(text);
    };
    socket.onopen = () => {
      socket.send(this.token);
      open = true;
      for (const text of queued.splice(0)) socket.send(text);
    };
    socket.onmessage = (ev) => {
      if (closed || typeof ev.data !== "string") return;
      let said: { update?: string; awareness?: string; section?: string; state?: LiveDocState; peer?: string | null };
      try {
        said = JSON.parse(ev.data) as typeof said;
      } catch {
        return;
      }
      if ("peer" in said) {
        const peer = said.peer;
        if (peer === null) handlers.onPeer?.(null);
        else if (typeof peer === "string" && /^\d+$/.test(peer)) handlers.onPeer?.(peer as `${number}`);
      } else if (typeof said.update === "string") handlers.onUpdate(fromBase64(said.update));
      else if (typeof said.awareness === "string" && typeof said.section === "string") handlers.onAwareness(said.section, fromBase64(said.awareness));
      else if (said.state) handlers.onState?.(said.state);
    };
    socket.onclose = (ev) => {
      if (closed) return;
      closed = true;
      handlers.onClose?.(ev.code);
    };
    socket.onerror = () => {};
    return {
      sendUpdate: (bytes) => send({ update: toBase64(bytes) }),
      sendAwareness: (section, bytes) => send({ awareness: toBase64(bytes), section }),
      freeze: () => send({ freeze: true }),
      reopen: () => send({ reopen: true }),
      close() {
        if (closed) return;
        closed = true;
        socket.close(1000);
      },
    };
  }

  /* A planning doc: the visual of kind "plan" with the same id. */
  plan(id: string) {
    return this.get<Plan>(`/plans/${encodeURIComponent(id)}`);
  }

  /* Approve or redirect the revision on screen; a newer revision, or a second verdict, is a 409. */
  decidePlan(id: string, verdict: PlanVerdict) {
    return this.postInbox<Plan>(`/plans/${encodeURIComponent(id)}/decide`, verdict);
  }

  /* The design doc's writes (brindle/plan_docs.py). Each names the revision on screen where the
     engine checks it (a newer one is a 409) and answers with the plan as it is now. */
  commentPlan(id: string, rev: number, anchor: PlanAnchor, text: string, question = false) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/comments`, { rev, anchor, text, ...(question ? { question } : {}) });
  }

  replyPlan(id: string, thread: number, text: string) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/comments`, { parent: thread, text });
  }

  /* Resolve a comment thread, or (resolved false) reopen it. */
  resolvePlanThread(id: string, thread: number, resolved = true) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/threads/${thread}/resolve`, { resolved });
  }

  suggestPlan(id: string, rev: number, change: PlanChange, text = "") {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/suggestions`, { rev, ...change, text });
  }

  /* Accepting a suggestion makes its change: a new revision. */
  acceptPlanSuggestion(id: string, thread: number, rev: number) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/threads/${thread}/accept`, { rev });
  }

  rejectPlanSuggestion(id: string, thread: number) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/threads/${thread}/reject`, {});
  }

  /* The person's direct edit: a new revision. */
  editPlan(id: string, rev: number, change: PlanChange) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/edit`, { rev, ...change });
  }

  /* The person's pick on one decision (Confirm A); the verdict starts from these. */
  confirmPlan(id: string, rev: number, decision: string, option: string) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/confirm`, { rev, decision, option });
  }

  /* Team review through the Library (brindle/pro/shared_docs.py): each answers with the plan, its build gate included. */
  sharePlan(id: string, scope: { kind: string; id: string }, reviewers: PlanReviewerChoice[]) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/share`, { scope, reviewers });
  }

  setPlanReviewers(id: string, reviewers: PlanReviewerChoice[]) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/reviewers`, { reviewers });
  }

  /* Bring back what teammates wrote; what changed reaches the reader as an event. */
  syncPlan(id: string) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/sync`, {});
  }

  /* Make a plan the Library can't confirm a personal doc. The engine says when it may be offered. */
  stopSharingPlan(id: string, rev: number) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/stop-sharing`, { rev });
  }

  /* The person's own sign-off, as one of the plan's reviewers, on the revision on screen. */
  signOffPlan(id: string, rev: number, version?: number) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/sign-off`, { rev, ...(version === undefined ? {} : { version }) });
  }

  /* An org admin's waiver of a review rule, or a stand-in for it. The Library checks they are an admin. */
  overridePlanPolicy(id: string, override: PlanPolicyOverride) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/policy-override`, override);
  }

  /* An org admin asks the people a waiting rule counts again, on revision `rev`. */
  askPlanPolicyAgain(id: string, ask: { rev: number; org_id: string; policy: string }) {
    return this.post<Plan>(`/plans/${encodeURIComponent(id)}/policy-ask`, ask);
  }

  /* The org rules that would cover the plan once it is shared. */
  previewPlanPolicies(id: string) {
    return this.post<{ policies: PolicyPreviewRow[] }>(`/plans/${encodeURIComponent(id)}/policy-preview`, {});
  }

  /* A design someone else shared (brindle/pro/shared_reader.py), by its Library doc id: read from the
     Library each time and held only in memory. A write answers with the design as it is now. */
  sharedDesign(doc: string, repo?: string) {
    return this.get<SharedDesign>(`/shared-designs/${encodeURIComponent(doc)}`, { repo });
  }

  writeSharedDesign(doc: string, tail: string, body: Record<string, unknown>) {
    return this.post<SharedDesign>(`/shared-designs/${encodeURIComponent(doc)}/${tail}`, body);
  }

  /* An org admin's review rules, with the teams and roles they can name and the designs each covers now. */
  reviewPolicies() {
    return this.get<ReviewPoliciesState>("/review-policies");
  }

  /* What saving `policies` as `org_id`'s list would do to the designs in review; writes nothing. */
  reviewPolicyImpact(org_id: string, policies: ReviewPolicyRule[]) {
    return this.post<DraftImpact>("/review-policies/draft-impact", { org_id, policies });
  }

  /* Replaces the rules of `org_id`, the org they were read for (another active org is a 409, nothing written);
     `version` is the one last read (a stale one is a 409). */
  saveReviewPolicies(org_id: string, version: number, policies: ReviewPolicyRule[]) {
    return this.put<{ version: number; policies: ReviewPolicyRule[] }>("/review-policies", { org_id, version, policies });
  }

  /* The people of the person's org, to pick reviewers from, without the person (me); admin_org, the org whose rules they can override. */
  libraryPeople() {
    return this.get<{ people: LibraryPerson[]; me?: string; admin_org?: string | null }>("/library/people");
  }

  async keptVisuals(): Promise<KeptVisualDTO[]> {
    const got = await this.get<KeptVisualDTO[] | { visuals?: KeptVisualDTO[] }>("/kept-visuals");
    return Array.isArray(got) ? got : (got.visuals ?? []);
  }

  keptVisual(id: string) {
    return this.get<KeptVisualDTO>(`/kept-visuals/${encodeURIComponent(id)}`);
  }

  /* The Library of approved plans (brindle Pro), through the daemon; nothing is kept on disk.
     A plan without it throws DaemonError "not_entitled". */
  async docs(query: DocsQuery = {}): Promise<{ docs: AnalysisDoc[] }> {
    const got = await this.get<{ docs?: WireListedDoc[] }>("/analysis-docs", { ...query });
    return { docs: (got.docs ?? []).map(listedDoc) };
  }

  docSessionSummary(session: string) {
    return this.get<DocSessionSummary>("/analysis-docs/session-summary", { session });
  }

  async docTopics(query: { repo?: string; scope?: string } = {}): Promise<{ topics: DocTopic[] }> {
    const got = await this.get<{ topics?: { topic: string; count?: number }[] }>("/analysis-docs/topics", { ...query });
    return { topics: (got.topics ?? []).map((t) => ({ name: t.topic, count: t.count })) };
  }

  /* Every scope the person reads (their own, their teams', their org's), with or without docs. */
  docScopes(repo?: string) {
    return this.get<{ scopes: DocScope[] }>("/analysis-docs/scopes", { repo });
  }

  /* The folders the person can see, per scope they read (GET /library/folders). */
  libraryFolders() {
    return this.get<{ scopes: FolderGroup[] }>("/library/folders", {});
  }

  /* `scope` is the daemon's spelling: user, team:<id> or org:<id>. */
  createLibraryFolder(scope: string, name: string, visibility: FolderVisibility, canAdd: "owner" | "members" = "owner") {
    return this.post<LibraryFolder>("/library/folders", { scope, name, visibility, can_add: canAdd });
  }

  moveDocToFolder(id: string, scope: string, folder: string) {
    return this.put<unknown>(`/analysis-docs/${encodeURIComponent(id)}/folder`, { scope, folder });
  }

  async knowledgeMap(repo: string, scope: DocScope, paths: string[] = []): Promise<KnowledgeMap> {
    const got = await this.get<KnowledgeMap>("/analysis-docs/map", { repo, scope_kind: scope.kind, scope_id: scope.id, paths: JSON.stringify(paths), depth: 1 });
    return { ...got, nodes: got.nodes.map(({ category, ...node }) => ({ ...node, ...categoryOf(category) })) };
  }

  async doc(id: string, repo?: string): Promise<AnalysisDocDetail> {
    const doc = docDetail(await this.get<WireDoc>(`/analysis-docs/${encodeURIComponent(id)}`, { repo }));
    // Approved plans already link their local record through source.ref. The
    // saved text stays readable when that session is no longer on this device.
    if (doc.source?.kind === "goal" && doc.source.ref?.startsWith("v_")) {
      const plan = await this.plan(doc.source.ref).catch(() => null);
      if (plan?.state === "approved" && plan.session === doc.source.session && plan.markdown && doc.files.some((f) => f.md === plan.markdown || f.md.startsWith(`${plan.markdown.trimEnd()}\n\n## What the person approved (r${plan.rev})`))) {
        const record = plan as Plan & Pick<AnalysisDocDetail, "revisions" | "threads">;
        return { ...doc, revisions: record.revisions, threads: record.threads, decisions: plan.decisions, verdict: plan.verdict };
      }
    }
    return doc;
  }

  /* Computed on this machine from its own records; needs no plan. */
  docResults(repo?: string) {
    return this.get<DocResults>("/analysis-docs/results", { repo });
  }

  shareDoc(id: string, scope: string) {
    return this.post<unknown>(`/analysis-docs/${encodeURIComponent(id)}/share`, { scope });
  }

  unshareDoc(id: string, scope: string) {
    return this.delete<unknown>(`/analysis-docs/${encodeURIComponent(id)}/share`, { scope });
  }

  deleteDoc(id: string) {
    return this.delete<unknown>(`/analysis-docs/${encodeURIComponent(id)}`, {});
  }

  /* With `dir` the daemon writes the file there; without, it comes back as base64. */
  exportDoc(id: string, format: "md" | "docx" | "zip", dir?: string, destination: ExportDestination = "download") {
    return this.post<DocExport>(`/analysis-docs/${encodeURIComponent(id)}/export`, { format, destination, ...(dir ? { dir } : {}) });
  }

  /* The org's doc policy: which export destinations it allows. */
  docPolicy(repo?: string) {
    return this.get<DocPolicy>("/analysis-docs/policy", { repo });
  }

  /* The person's own doc switches, and which their org has locked. */
  async docSwitches(repo?: string) {
    return (await this.get<{ switches: DocSwitch[] }>("/analysis-docs/switches", { repo })).switches;
  }

  /* Sets one of the person's own switches; the daemon refuses (403) one the org has locked. */
  async setDocSwitch(name: DocSwitchName, on: boolean) {
    return (await this.put<{ switches: DocSwitch[] }>("/analysis-docs/switches", { name, on })).switches;
  }

  /* brindle's disk use (slow: the daemon measures every worktree). */
  storage(repo?: string) {
    return this.get<StorageReport>("/storage", { repo });
  }

  /* Removes one worktree (merged or abandoned; the engine checks it again),
     its branch tip archived first. The app asks the person before calling
     this, and sends one call per confirmed path: never the route's
     `abandoned`, which removes whatever the engine lists at that moment. */
  cleanStorage(target: { path: string }) {
    return this.post<StorageCleaned>("/storage/clean", target);
  }

  /* The public client ids for the in-app Google / Microsoft sign-in; null hides that button. */
  docExportConfig(repo?: string) {
    return this.get<DocExportConfig>("/analysis-docs/export-config", { repo });
  }

  /* The team's export destination (kind null when none). Changing it is admin only. */
  docDestination(repo?: string) {
    return this.get<TeamDestination>("/analysis-docs/destination", { repo });
  }

  setDocDestination(body: { kind: TeamDestinationKind; target: { drive_id: string; folder_id?: string } | { site: string; folder?: string }; credential: string | { tenant_id: string; client_id: string; client_secret: string } }) {
    return this.put<TeamDestination>("/analysis-docs/destination", body);
  }

  clearDocDestination() {
    return this.delete<unknown>("/analysis-docs/destination", {});
  }

  profile(name: string, repo?: string) {
    return this.get<ProfileDetail>(`/profiles/${encodeURIComponent(name)}`, { repo });
  }

  patchConfig(body: ConfigPatch) {
    return this.patch<ConfigUpdated>("/config", body);
  }

  /* The person's own settings (~/.brindle/config.json's claude_ and opencode_permission_mode). */
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

  /* The permission policy: the chosen preset, the unmatched setting, every rule
     with its 24-hour prompt count, and the saved actions. */
  permissions() {
    return this.get<PermissionPolicyDTO>("/permissions");
  }

  /* The ask rate and top causes of permission asks over the last `days` (default 7). */
  permissionReport(days?: number) {
    return this.get<PermissionReport>(`/permissions/report${days ? `?days=${days}` : ""}`);
  }

  /* Start over from a preset; the person's own and learned rules stay. Answers the whole policy. */
  setPermissionPreset(preset: PermissionPreset) {
    return this.put<PermissionPolicyDTO>("/permissions/preset", { preset });
  }

  /* PUT /permissions/groups: change capability group baselines and subcategory answers
     (allow | ask | deny | inherit); the rest stay. Answers the whole policy. */
  setPermissionGroups(groups: Record<string, { baseline?: string; subcategories?: Record<string, string> }>) {
    return this.put<PermissionPolicyDTO>("/permissions/groups", { groups });
  }

  setPermissionUnmatched(unmatched: PermissionUnmatched) {
    return this.patch<PermissionPolicyDTO>("/permissions", { unmatched });
  }

  createPermissionRule(body: PermissionRulePut & { kind: PermissionKind; match: string; decision: PermissionDecision }) {
    return this.post<PermissionRuleDTO>("/permissions/rules", body);
  }

  updatePermissionRule(id: string, body: PermissionRulePut) {
    return this.put<PermissionRuleDTO>(`/permissions/rules/${encodeURIComponent(id)}`, body);
  }

  deletePermissionRule(id: string) {
    return this.delete<PermissionDeleted>(`/permissions/rules/${encodeURIComponent(id)}`, {});
  }

  addPermissionException(id: string, body: PermissionExceptionPut) {
    return this.post<PermissionRuleDTO>(`/permissions/rules/${encodeURIComponent(id)}/exceptions`, body);
  }

  createSavedAction(body: SavedActionPut & { kind: SavedActionKind; pattern: string; decision: "allow" | "deny" }) {
    return this.post<SavedActionDTO>("/permissions/saved", body);
  }

  updateSavedAction(id: string, body: SavedActionPut) {
    return this.put<SavedActionDTO>(`/permissions/saved/${encodeURIComponent(id)}`, body);
  }

  deleteSavedAction(id: string) {
    return this.delete<PermissionDeleted>(`/permissions/saved/${encodeURIComponent(id)}`, {});
  }

  /* The requests waiting on the person, one item per kind and general form (`repo` narrows). */
  permissionAsks(repo?: string) {
    return this.get<AskGroupDTO[]>("/permissions/asks", { repo });
  }

  decidePermissionAsks(body: AskDecide) {
    return this.post<AskDecisionDTO>("/permissions/asks/decide", body);
  }

  /* What allowing everything waiting would grant, within each request's own policy. */
  permissionAllowAllPlan(repo?: string, forever = false) {
    return this.get<AllowAllPlanDTO>("/permissions/asks/plan", { repo, forever: forever ? "1" : undefined });
  }

  permissionAllowAll(repo?: string, forever = false) {
    return this.post<AllowAllPlanDTO>("/permissions/asks/allow-all", { repo, forever });
  }

  /* Except the blocked requests from the deny rules that block them, then allow them. */
  permissionUnblock(repo?: string, forever = false) {
    return this.post<AllowAllPlanDTO>("/permissions/asks/unblock", { repo, forever });
  }

  /* The organization's approval administration (routes_org_permissions): its members' roles
     and the approval policy. */
  orgPermissions() {
    return this.get<OrgPermissionsDTO>("/org-permissions");
  }

  assignOrgRole(body: OrgAssignmentRequestDTO) {
    return this.put<OrgAssignmentDTO>("/org-permissions/role-assignment", body);
  }

  saveOrgApprovalPolicy(body: OrgPolicyRequestDTO) {
    return this.put<OrgApprovalPolicyDTO>("/org-permissions/policy", body);
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

  /* `keys: false`: without the stored keys, which take the engine the longest to read. */
  config(repo?: string, opts: { keys?: boolean } = {}) {
    return this.get<Config>("/config", { repo, keys: opts.keys === false ? 0 : undefined });
  }

  /* Which profiles each weight tier tries, the reviewer, the excluded
     providers (each with the scope it comes from) and every profile's
     model, provider, availability and cost tier. `scope: "user"`: the
     person's own settings and library, as every repo starts from them (`repo`
     still gives the org policy). `learned: false`: each kind's `learned` is
     null, without the seconds working it out takes. */
  routing(repo: string, opts: { scope?: "user"; learned?: boolean } = {}) {
    return this.get<RoutingDTO>("/routing", { repo, scope: opts.scope, learned: opts.learned === false ? 0 : undefined });
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

  /* The CLIs a new supervisor session in `repo` can run on, in preference order (engine: GET /providers/usable). */
  usableProviders(repo: string) {
    return this.get<{ usable: string[]; default: string | null }>("/providers/usable", { repo });
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

  /* Signs brindle's own copy of Claude Code or Codex out (`claude auth logout`,
     `codex logout`); the person's own CLI login stays. Answers the provider, not signed in. */
  signOutProvider(provider: string) {
    return this.post<ProviderInstalled>(`/providers/${encodeURIComponent(provider)}/logout`, {});
  }

  /* How the provider connects (brindle.connections): its own sign-in, an API
     key or the person's cloud, and for a cloud its settings and login. */
  providerConnection(provider: string, repo?: string) {
    return this.get<ProviderConnection>(`/providers/${encodeURIComponent(provider)}/connection`, { repo });
  }

  /* Saves how the provider connects: `method` auto | sign_in | api_key | cloud,
     and for cloud the route (`cloud`) and its non-secret `settings`. 403 when
     the org decides instead, with why. */
  setProviderConnection(provider: string, choice: { method: string; cloud?: string; settings?: Record<string, string>; repo?: string }) {
    return this.put<ProviderConnection>(`/providers/${encodeURIComponent(provider)}/connection`, choice);
  }

  /* Signs Codex's ChatGPT login out so its API key is used (ready.action
     "sign_out"), a job like cloudSignIn: job.done's result is the connection after. */
  codexSignOut() {
    return this.post<Job>("/providers/codex/sign-out", {});
  }

  /* What this machine's cloud CLIs are set up with: AWS profiles, gcloud, the Azure tenant, usual regions. */
  cloudOptions() {
    return this.get<CloudOptionsDTO>("/cloud/options");
  }

  /* The cloud sign-in (or its renewal) for the provider's saved route, a job
     (provider.cloud_sign_in): the CLI's lines as job.progress, job.done's
     result {result: "signed in" | "failed" | "waiting" | "manual"}. */
  cloudSignIn(provider: string) {
    return this.post<Job>(`/providers/${encodeURIComponent(provider)}/cloud-sign-in`, {});
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

  /* `brindle keys unset NAME` (DELETE /keys/{name}). */
  deleteKey(name: string) {
    return this.delete<{ name: string; set: false }>(`/keys/${encodeURIComponent(name)}`, {});
  }

  /* Re-checks the stored key with its provider (POST /keys/{name}/test). */
  testKey(name: string) {
    return this.post<KeyTested>(`/keys/${encodeURIComponent(name)}/test`);
  }

  /* Stores the daemon's own environment value of `name` (POST /keys/{name}/import). */
  importKey(name: string, repo?: string) {
    return this.post<KeyStored>(`/keys/${encodeURIComponent(name)}/import`, repo ? { repo } : {});
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

  /* All decisions that can remove an Inbox card invalidate older reads,
     including refusals that say the request has already moved on. */
  private postInbox<T>(path: string, body: unknown): Promise<T> {
    return this.post<T>(path, body).finally(() => {
      this.inboxAcknowledgements++;
    });
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
      const got = body as { error?: { code?: string; message?: string }; conflict?: SessionConflict; preflight?: PreflightDTO | PreflightItemDTO[]; reasons?: string[]; repo?: string } | undefined;
      if (got?.conflict) throw new DaemonError("conflict", got.conflict.message, response.status, got.conflict);
      const err = got?.error;
      if (got?.preflight) {
        const preflight = preflightResult(got.preflight);
        const held = new DaemonError("preflight", err?.message ?? `${preflight.provider} needs setting up first`, response.status, undefined, preflight);
        if (typeof got.repo === "string") held.repo = got.repo;
        throw held;
      }
      throw new DaemonError(err?.code ?? "http", err?.message ?? response.statusText, response.status, undefined, undefined, Array.isArray(got?.reasons) ? got.reasons : undefined);
    }
    const route = new URL(url).pathname;
    if (route === "/v1/meta") startupStep("meta");
    if (route === "/v1/snapshot") startupStep("snapshot");
    return body as T;
  }
}
