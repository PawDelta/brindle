/* Where each design stands, for the Library's list, board and sidebar counts. A design reaches
   five stages in turn (the board's columns): Drafting, In review (it has reviewers, or someone is
   asked to review it), Approved (its review is ready, or its plan was approved), Building (a task
   of its plan has started) and Shipped (every task of its plan merged). Building and Shipped are
   read from the plan's tasks (GET /analysis-docs/{id} `tasks`, the plan's board on this machine).
   "Needs you" is not a stage but a mark over one: the person's own queue (the Inbox's
   review_requested / changes_requested cards, service/inbox.py) says what waits on them. The
   server's list carries none of this, so each design's detail is read once (loadRecords); the
   filter, the rows, the cards and the sidebar's counts all use these same functions. */

import type { AnalysisDoc, AnalysisDocDetail, DaemonClient, DocScope, FolderGroup, LibraryFolder } from "../../api/client";
import type { DesignTaskDTO, InboxItemDTO, RepoDTO } from "../../api/schema";
import type { LibraryStage } from "../../routes";

export type DesignReview = NonNullable<AnalysisDocDetail["design"]>;
/* What the Library reads from a design's detail. */
export type DesignRecord = Pick<AnalysisDocDetail, "design" | "tasks" | "threads" | "decisions" | "verdict" | "work_item">;
/* The five stages a design reaches, in order: the board's columns. */
export type DesignStage = Exclude<LibraryStage, "needs-you">;
export const BOARD_STAGES: { id: DesignStage; label: string }[] = [
  { id: "drafting", label: "Drafting" },
  { id: "in-review", label: "In review" },
  { id: "approved", label: "Approved" },
  { id: "building", label: "Building" },
  { id: "shipped", label: "Shipped" },
];

/* What the person's own queue says of a design: asked to review it, asked for changes on it
   (its author), or told it was signed off. */
export type QueueMark = "review" | "changes" | "signed";
export type Queue = Map<string, QueueMark>;

/* The designs the person's own queue names, by library doc id. */
export function docQueue(inbox: InboxItemDTO[]): Queue {
  const queue: Queue = new Map();
  for (const item of inbox) {
    if (item.doc == null) continue;
    const id = String(item.doc);
    if (item.kind === "review_requested") queue.set(id, "review");
    else if (item.kind === "changes_requested" && queue.get(id) !== "review") queue.set(id, "changes");
    else if (item.kind === "signed_off" && !queue.has(id)) queue.set(id, "signed");
  }
  return queue;
}

/* What waits on the person in a design, or null: its review, or the changes asked of its author. */
export function needsYou(id: string, queue: Queue): "review" | "changes" | null {
  const mark = queue.get(id);
  return mark === "review" || mark === "changes" ? mark : null;
}

const live = (tasks?: DesignTaskDTO[] | null) => (tasks ?? []).filter((t) => t.state !== "cancelled");

export function stageOf(id: string, queue: Queue, record?: DesignRecord | null): DesignStage {
  const tasks = live(record?.tasks);
  if (tasks.length > 0 && tasks.every((t) => t.state === "merged")) return "shipped";
  if (tasks.some((t) => t.state !== "queued")) return "building";
  const design = record?.design;
  if (design?.ready || record?.verdict?.approved || queue.get(id) === "signed") return "approved";
  if (needsYou(id, queue) || (design && (design.ready === false || design.reviewers?.length || design.pending_reviewers?.length || design.pending_policies?.length))) return "in-review";
  return "drafting";
}

/* Whether a design is at the stage the sidebar picked ("needs-you" being the person's queue). */
export function atStage(stage: LibraryStage, id: string, queue: Queue, record?: DesignRecord | null): boolean {
  return stage === "needs-you" ? needsYou(id, queue) !== null : stageOf(id, queue, record) === stage;
}

/* The count of designs at each stage (and waiting on the person), over the documents given. */
export function stageCounts(docs: AnalysisDoc[], queue: Queue, records: Map<string, DesignRecord>): Record<LibraryStage, number> {
  const counts: Record<LibraryStage, number> = { "needs-you": 0, drafting: 0, "in-review": 0, approved: 0, building: 0, shipped: 0 };
  for (const d of docs) {
    counts[stageOf(d.id, queue, records.get(d.id))]++;
    if (needsYou(d.id, queue)) counts["needs-you"]++;
  }
  return counts;
}

/* Each listed design's record (from its detail). A failed read is skipped: the design shows as Drafting. */
export async function loadRecords(client: Pick<DaemonClient, "doc">, docs: AnalysisDoc[], repo?: string): Promise<Map<string, DesignRecord>> {
  const got = await Promise.all(docs.map((d) => client.doc(d.id, repo).then((x): DesignRecord => ({ design: x.design, tasks: x.tasks, threads: x.threads, decisions: x.decisions, verdict: x.verdict, work_item: x.work_item }), () => null)));
  const records = new Map<string, DesignRecord>();
  docs.forEach((d, i) => got[i] && records.set(d.id, got[i]!));
  return records;
}

/* The plan's progress once it is building: tasks merged, in review, and in all (cancelled ones left out). */
export function taskProgress(record?: DesignRecord | null): { merged: number; review: number; total: number } | null {
  const tasks = live(record?.tasks);
  if (tasks.length === 0) return null;
  return { merged: tasks.filter((t) => t.state === "merged").length, review: tasks.filter((t) => t.state === "in review").length, total: tasks.length };
}

/* The record's sign-off rows: everyone who recorded a sign-off (with its revision), even if not
   on the reviewer list, then the listed reviewers who have not signed. `signed` is for the revision on screen. */
export function signOffRows(design?: DesignReview | null): { sub: string; role: string; rev?: number; signed: boolean }[] {
  if (!design) return [];
  const role = (sub: string) => { const r = design.reviewers?.find((x) => x.sub === sub); return r ? (r.required === false ? "optional" : "required") : "signed off"; };
  const rows = (design.sign_offs ?? []).map((s) => ({ sub: s.sub, role: role(s.sub), rev: s.rev, signed: s.rev === undefined || s.rev === design.rev }));
  for (const r of design.reviewers ?? []) if (!rows.some((x) => x.sub === r.sub && x.signed)) rows.push({ sub: r.sub, role: role(r.sub), rev: undefined, signed: false });
  return rows;
}

/* A row's sign-offs: each reviewer once (signed on the revision on screen or not), and how many of
   the required ones have signed. Null for a design with no reviewers. */
export function signOffs(design?: DesignReview | null): { people: { sub: string; signed: boolean; required: boolean }[]; signed: number; required: number } | null {
  const reviewers = design?.reviewers ?? [];
  if (!design || reviewers.length === 0) return null;
  const signedNow = (sub: string) => (design.sign_offs ?? []).some((s) => s.sub === sub && (s.rev === undefined || s.rev === design.rev));
  const people = reviewers.map((r) => ({ sub: r.sub, signed: signedNow(r.sub), required: r.required !== false }));
  const required = people.filter((p) => p.required);
  return { people, signed: required.filter((p) => p.signed).length, required: required.length };
}

/* What is still open on a design: unresolved threads, decisions nobody has confirmed yet, and the
   open questions the server read from it. */
export function openItems(doc: AnalysisDoc, record?: DesignRecord | null): { threads: number; decide: number; questions: number } {
  const threads = (record?.threads ?? record?.design?.threads ?? []).filter((t) => t.status !== "resolved").length;
  const decide = record?.verdict?.approved ? 0 : (record?.decisions ?? []).filter((d) => !d.confirmed).length;
  return { threads, decide, questions: doc.open_questions?.length ?? 0 };
}

/* ---------- Folders ---------- */

const scopeSpec = (s: DocScope) => (s.kind === "user" ? "user" : `${s.kind === "org" ? "org" : "team"}:${s.id}`);
/* A folder's key, as the route and the filter name it: its scope and its id. */
export const folderKey = (scope: DocScope, id: string) => `${scope.kind}:${scope.id}/${id}`;

/* A folder as the Library shows it: its key, name, the scope's spelling (to move a design into it),
   its repo (null when not known here) and whether only its creator sees it. */
export interface PlacedFolder {
  key: string;
  id: string;
  name: string;
  scope: DocScope;
  spec: string;
  repo: string | null;
  private: boolean;
  created_at?: number | null;
}

export function placedFolders(groups: FolderGroup[]): PlacedFolder[] {
  return groups.flatMap(({ scope, folders }) => (folders ?? []).map((f: LibraryFolder) => ({ key: folderKey(scope, f.id), id: f.id, name: f.name, scope, spec: scopeSpec(scope), repo: f.repo ?? null, private: f.visibility === "private", created_at: f.created_at })));
}

/* The folder a design is in (its key), or null. */
export const folderOf = (record?: DesignRecord | null): string | null => {
  const f = record?.design?.folder;
  return f && f.scope && f.id ? folderKey(f.scope, f.id) : null;
};

/* The repo's name as the app knows it, else its folder's last part. */
export function repoName(root: string, repos?: Pick<RepoDTO, "name" | "repo_root">[] | null): string {
  return repos?.find((r) => r.repo_root === root)?.name ?? (root.replace(/\/+$/, "").split("/").pop() || root);
}

const changed = (d: AnalysisDoc) => d.updated_at ?? d.created_at ?? 0;
export const newestFirst = (docs: AnalysisDoc[]) => [...docs].sort((a, b) => changed(b) - changed(a));

/* The sidebar's folders, grouped by repo: each repo with its folders, newest first (a folder by the
   last change to a design in it, else when it was made; a repo by its newest folder). Folders
   whose repo this machine doesn't know are grouped under their scope's name. */
export interface FolderTree {
  /* A repo's root, or the scope's spelling for folders without one. */
  key: string;
  label: string;
  repo: string | null;
  count: number;
  folders: (PlacedFolder & { count: number })[];
}

export function folderTree(folders: PlacedFolder[], docs: AnalysisDoc[], records: Map<string, DesignRecord>, repos?: Pick<RepoDTO, "name" | "repo_root">[] | null): FolderTree[] {
  const inFolder = new Map<string, AnalysisDoc[]>();
  for (const d of docs) {
    const key = folderOf(records.get(d.id));
    if (key) inFolder.set(key, [...(inFolder.get(key) ?? []), d]);
  }
  const latest = (f: PlacedFolder) => Math.max(f.created_at ?? 0, ...(inFolder.get(f.key) ?? []).map(changed));
  const trees = new Map<string, FolderTree & { latest: number }>();
  for (const f of folders) {
    const key = f.repo ?? f.spec;
    const label = f.repo ? repoName(f.repo, repos) : f.scope.kind === "user" ? "My folders" : f.scope.name || (f.scope.kind === "org" ? "Organization" : "Team");
    const tree = trees.get(key) ?? { key, label, repo: f.repo, count: 0, folders: [], latest: 0 };
    const count = inFolder.get(f.key)?.length ?? 0;
    tree.folders.push({ ...f, count });
    tree.count += count;
    tree.latest = Math.max(tree.latest, latest(f));
    trees.set(key, tree);
  }
  return [...trees.values()]
    .sort((a, b) => Number(!a.repo) - Number(!b.repo) || b.latest - a.latest)
    .map(({ latest: _latest, ...tree }) => ({ ...tree, folders: [...tree.folders].sort((a, b) => latest(b) - latest(a)) }));
}

/* ---------- The list's groups ---------- */

export type GroupBy = "folder" | "stage" | "none";
export interface DocGroup {
  key: string;
  label: string;
  /* "need": the pinned Needs you group; "folder": a folder's (`folder` set); "other". */
  kind: "need" | "folder" | "other";
  folder?: PlacedFolder;
  docs: AnalysisDoc[];
  /* How many of the group's designs are shown above it, in Needs you. */
  above: number;
}

/* The list's rows in groups, newest first everywhere: "Needs you" pinned on top whatever the
   grouping, then the rest by folder (each labelled repo / folder), by stage, or in one run. A
   group is placed by its newest design; a design in Needs you is not repeated below, but its
   group says how many of its designs are above. */
export function groupDocs(docs: AnalysisDoc[], by: GroupBy, queue: Queue, records: Map<string, DesignRecord>, folders: PlacedFolder[], repos?: Pick<RepoDTO, "name" | "repo_root">[] | null): DocGroup[] {
  const sorted = newestFirst(docs);
  const need = sorted.filter((d) => needsYou(d.id, queue));
  const out: DocGroup[] = need.length ? [{ key: "needs-you", label: "Needs you", kind: "need", docs: need, above: 0 }] : [];
  const pinned = new Set(need.map((d) => d.id));
  if (by === "none") {
    const rest = sorted.filter((d) => !pinned.has(d.id));
    if (rest.length) out.push({ key: "all", label: need.length ? "Everything else" : "", kind: "other", docs: rest, above: 0 });
    return out;
  }
  const byKey = new Map(folders.map((f) => [f.key, f]));
  const groups = new Map<string, DocGroup & { all: number }>();
  for (const d of sorted) {
    const record = records.get(d.id);
    let key: string;
    let label: string;
    let folder: PlacedFolder | undefined;
    if (by === "stage") {
      const stage = stageOf(d.id, queue, record);
      key = stage;
      label = BOARD_STAGES.find((s) => s.id === stage)!.label;
    } else {
      const fk = folderOf(record);
      folder = fk ? byKey.get(fk) : undefined;
      key = folder?.key ?? "none";
      label = folder ? (folder.repo ? `${repoName(folder.repo, repos)} / ${folder.name}` : folder.name) : "Not in a folder";
    }
    const group = groups.get(key) ?? { key, label, kind: folder ? "folder" : "other", folder, docs: [], above: 0, all: 0 };
    group.all++;
    if (pinned.has(d.id)) group.above++;
    else group.docs.push(d);
    groups.set(key, group);
  }
  // Groups follow their newest design (sorted above), with designs in no folder last.
  const ordered = [...groups.values()].filter((g) => g.docs.length > 0).sort((a, b) => Number(a.key === "none") - Number(b.key === "none"));
  return [...out, ...ordered.map(({ all: _all, ...g }) => g)];
}

/* "5h", "3d", "Nov 5": when a design last changed, short. */
export function changedAgo(at: number | undefined, now = Date.now() / 1000): string {
  if (!at) return "—";
  const age = Math.max(0, now - at);
  if (age < 3600) return `${Math.max(1, Math.round(age / 60))}m`;
  if (age < 86400) return `${Math.round(age / 3600)}h`;
  if (age < 7 * 86400) return `${Math.floor(age / 86400)}d`;
  return new Date(at * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
