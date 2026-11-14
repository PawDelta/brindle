/* The Library scenes' designs (dev only): the eight of the approved rendering
   (library-redesign option A, states 1 and 2), in their folders grouped by repo, at each stage,
   two of them waiting on the person. Installed over the fake daemon's client for the "library"
   and "library-board" scenes. */

import type { AnalysisDoc, AnalysisDocDetail, DaemonClient, DocScope, FolderGroup } from "../src/api/client";
import type { InboxItemDTO } from "../src/api/schema";
import { OTHER as OTHER_REPO, REPO } from "./fixtures";

const now = Date.now() / 1000;
const hours = (n: number) => now - n * 3600;
const days = (n: number) => now - n * 86400;

const MINE: DocScope = { kind: "user", id: "u_me" };
const TEAM: DocScope = { kind: "group", id: "t_cc0acb7c", name: "Founding Team", can_share: true };
const ORG: DocScope = { kind: "org", id: "org_6e83ecdd", name: "PawDelta", can_share: true };
const at = (scope: DocScope, id: string) => ({ scope, id });

const folders: FolderGroup[] = [
  { scope: MINE, folders: [
    { id: "f_q4", name: "Q4 launch", visibility: "private", repo: REPO, created_at: days(20) },
    { id: "f_rel", name: "Releases", visibility: "private", repo: OTHER_REPO, created_at: days(40) },
    { id: "f_read", name: "Reading list", visibility: "private", repo: OTHER_REPO, created_at: days(41) },
  ] },
  { scope: TEAM, folders: [
    { id: "f_engine", name: "Engine", visibility: "shared", repo: REPO, created_at: days(60) },
    { id: "f_desktop", name: "Desktop app", visibility: "shared", repo: REPO, created_at: days(60) },
  ] },
  { scope: ORG, folders: [{ id: "f_sec", name: "Security reviews", visibility: "shared", repo: REPO, created_at: days(60) }] },
];

type Row = AnalysisDoc & { detail: Partial<AnalysisDocDetail> };
const task = (id: string, state: string) => ({ id, title: id, state });
const thread = (id: number, author: string, status = "open") => ({ id, author, text: "", replies: [], status });
const rows: Row[] = [
  { id: "d_cloud", title: "Cloud workers for Pro", scope: TEAM, updated_at: hours(5), created_by: "maya@pawdelta.com",
    summary: "Pro sessions keep running on a machine the customer controls; results land as reviewed pull requests.",
    work_item: { key: "AB#4810", state: "Active", tone: "active", title: "Azure Boards epic" },
    detail: { design: { rev: 3, ready: false, reviewers: [{ sub: "dana.ortiz@pawdelta.com" }, { sub: "sam.rivera@pawdelta.com" }], sign_offs: [], folder: at(TEAM, "f_engine") },
      threads: [thread(1, "dana"), thread(2, "sam"), thread(3, "ravi"), thread(4, "maya")],
      decisions: [{ id: "where", title: "Where", options: [] }, { id: "lease", title: "Lease", options: [] }, { id: "relay", title: "Relay", confirmed: "daemon", options: [] }] } },
  { id: "d_perm", title: "Permission prompts: read safe shell forms", scope: ORG, updated_at: hours(9), created_by: "ravi@pawdelta.com",
    summary: "The checker reads pipes, redirects and globs it can prove are read-only, so fewer commands stop for a person.",
    open_questions: ["Should `find -exec` ever be read as safe?", "Do we log what was auto-allowed?"],
    work_item: { key: "AB#4795", state: "New", title: "Azure Boards feature" },
    detail: { design: { rev: 1, ready: false, reviewers: [{ sub: "sam.rivera@pawdelta.com" }, { sub: "maya.chen@pawdelta.com" }], sign_offs: [], folder: at(ORG, "f_sec") } } },
  { id: "d_outbox", title: "Outbox for worker reports", scope: TEAM, updated_at: days(8), created_by: "dana@pawdelta.com", used_in_briefs: 5,
    summary: "A worker's report is written to an outbox first, so a busy supervisor never loses it.",
    work_item: { key: "AB#4610", state: "Closed", tone: "done" },
    detail: { design: { rev: 2, ready: true, reviewers: [{ sub: "sam.rivera@pawdelta.com" }], sign_offs: [{ sub: "sam.rivera@pawdelta.com", rev: 2 }], folder: at(TEAM, "f_engine") },
      tasks: [task("w1", "merged"), task("w2", "merged")] } },
  { id: "d_sandbox", title: "OS sandbox for worker shells", scope: TEAM, updated_at: days(3), created_by: "maya@pawdelta.com",
    summary: "Worker shells run under the OS sandbox with the worktree as the only writable path.",
    open_questions: ["What about package caches outside the worktree?"],
    detail: { design: { rev: 1, ready: false, reviewers: [{ sub: "ravi.patel@pawdelta.com" }], sign_offs: [], folder: at(ORG, "f_sec") } } },
  { id: "d_usage", title: "Anonymous usage counts", scope: ORG, updated_at: days(7), created_by: "sam@pawdelta.com",
    summary: "Opt-out counts with no install id; the server only adds to counters.",
    work_item: { key: "AB#4688", state: "New" },
    detail: { design: { rev: 4, ready: true, reviewers: [{ sub: "maya.chen@pawdelta.com" }], sign_offs: [{ sub: "maya.chen@pawdelta.com", rev: 4 }], folder: at(ORG, "f_sec") } } },
  { id: "d_coedit", title: "Live co-editing for design docs", scope: TEAM, updated_at: days(1), created_by: "sam@pawdelta.com",
    summary: "A doc in team review is edited live; the author freezes a version for sign-off.",
    work_item: { key: "AB#4702", state: "2 of 3 done", tone: "active", title: "Azure Boards epic: 2 of its 3 stories closed" },
    detail: { design: { rev: 5, ready: true, reviewers: [{ sub: "maya.chen@pawdelta.com" }], sign_offs: [{ sub: "maya.chen@pawdelta.com", rev: 5 }], folder: at(TEAM, "f_desktop") },
      tasks: [task("w1", "merged"), task("w2", "merged"), task("w3", "in review")] } },
  { id: "d_sidebar", title: "Sidebar at every window size", scope: MINE, updated_at: days(14), created_by: "sam@pawdelta.com", used_in_briefs: 2,
    summary: "The context gives way first when the window is short; the session tree keeps at least 80px.",
    detail: { verdict: { approved: true }, design: { folder: at(TEAM, "f_desktop") }, tasks: [task("w1", "merged"), task("w2", "merged"), task("w3", "merged")] } },
  { id: "d_release", title: "Release flow: gate on CI", scope: MINE, updated_at: days(22), created_by: "sam@pawdelta.com",
    summary: "Build first, then tag: a release tag only exists for a commit whose app bundle passed CI.",
    work_item: { key: "BRN-88", state: "To Do", title: "Jira story" },
    detail: { design: { folder: at(MINE, "f_rel") } } },
];

/* What waits on the person: their review of two designs. */
export const libraryInbox: InboxItemDTO[] = [
  { kind: "review_requested", reason: "Review requested", detail: "Cloud workers for Pro", doc: "d_cloud", request_id: "p_cloud" },
  { kind: "review_requested", reason: "Review requested", detail: "Permission prompts: read safe shell forms", doc: "d_perm", request_id: "p_perm" },
];

export function installLibrary(client: DaemonClient): void {
  const listed = rows.map(({ detail: _detail, ...doc }) => doc);
  Object.assign(client, {
    docs: async (q: { scope?: string; session?: string } = {}) => ({ docs: listed.filter((d) => !q.scope || (q.scope === "user" ? d.scope?.kind === "user" : q.scope.endsWith(d.scope?.id ?? "-"))) }),
    doc: async (id: string): Promise<AnalysisDocDetail> => {
      const row = rows.find((r) => r.id === id);
      if (!row) throw new Error(`no doc ${id}`);
      const { detail, ...doc } = row;
      return { ...doc, files: [{ name: "", md: `## Summary\n\n${doc.summary}` }], mentions: [], shared_with: [], ...detail };
    },
    docScopes: async () => ({ scopes: [MINE, TEAM, ORG] }),
    docResults: async () => ({ days: 30, minimum: 5, enough: true, briefed: 18, large: 31, compared: 18, similar: 11, with_brief: 1.4, without_brief: 2.3 }),
    libraryFolders: async () => ({ scopes: folders }),
    createLibraryFolder: async () => ({ id: "f_new", name: "New", visibility: "private" }),
    moveDocToFolder: async () => ({}),
  });
}
