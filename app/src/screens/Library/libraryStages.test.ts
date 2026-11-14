import { expect, it } from "vitest";
import type { AnalysisDoc, FolderGroup } from "../../api/client";
import { docQueue, folderTree, groupDocs, loadRecords, placedFolders, stageCounts, stageOf, type DesignRecord } from "./libraryStages";

const task = (id: string, state: string) => ({ id, title: id, state });
const TEAM = { kind: "group", id: "t1", name: "Founding Team" };
const ME = { kind: "user", id: "u1" };
const inFolder = (scope: typeof TEAM | typeof ME, id: string): DesignRecord => ({ design: { folder: { scope, id } } });

it("a design's stage is read from its review and its plan's tasks; Needs you marks one without moving it", () => {
  const records = new Map<string, DesignRecord>([
    ["draft", { design: null }],
    ["review", { design: { ready: false, reviewers: [{ sub: "maya" }] } }],
    ["asked", { design: null }],                                                       // only the queue knows it
    ["ready", { design: { ready: true } }],
    ["plan", { verdict: { approved: true }, tasks: [task("a", "queued")] }],           // approved, nothing started
    ["building", { design: { ready: true }, tasks: [task("a", "merged"), task("b", "in review"), task("c", "queued")] }],
    ["shipped", { tasks: [task("a", "merged"), task("b", "cancelled"), task("c", "merged")] }],
  ]);
  const queue = docQueue([
    { kind: "review_requested", reason: "r", doc: "asked" },
    { kind: "changes_requested", reason: "r", doc: "building" },
    { kind: "plan", reason: "r" },                      // no doc: not a Library design
  ]);
  const stages = Object.fromEntries([...records.keys()].map((id) => [id, stageOf(id, queue, records.get(id))]));
  expect(stages).toEqual({ draft: "drafting", review: "in-review", asked: "in-review", ready: "approved", plan: "approved", building: "building", shipped: "shipped" });
  const docs = [...records.keys()].map((id) => ({ id }));
  expect(stageCounts(docs, queue, records)).toEqual({ "needs-you": 2, drafting: 1, "in-review": 2, approved: 2, building: 1, shipped: 1 });
});

it("the list pins Needs you on top and groups the rest by folder as repo / folder, newest first, saying how many are above", () => {
  const folders = placedFolders([
    { scope: TEAM, folders: [{ id: "eng", name: "Engine", visibility: "shared", repo: "/src/pawdelta" }, { id: "sec", name: "Security", visibility: "shared", repo: "/src/pawdelta" }] },
  ] as FolderGroup[]);
  const docs: AnalysisDoc[] = [
    { id: "cloud", updated_at: 90 }, { id: "outbox", updated_at: 50 }, { id: "sandbox", updated_at: 70 }, { id: "loose", updated_at: 95 },
  ];
  const records = new Map<string, DesignRecord>([
    ["cloud", inFolder(TEAM, "eng")], ["outbox", inFolder(TEAM, "eng")], ["sandbox", inFolder(TEAM, "sec")],
  ]);
  const queue = docQueue([{ kind: "review_requested", reason: "r", doc: "cloud" }]);
  const groups = groupDocs(docs, "folder", queue, records, folders, [{ name: "pawdelta", repo_root: "/src/pawdelta" }]);
  expect(groups.map((g) => [g.label, g.docs.map((d) => d.id), g.above])).toEqual([
    ["Needs you", ["cloud"], 0],
    ["pawdelta / Engine", ["outbox"], 1],          // placed by its newest design, the one pinned above
    ["pawdelta / Security", ["sandbox"], 0],
    ["Not in a folder", ["loose"], 0],             // last, however new
  ]);
  expect(groupDocs(docs, "none", queue, records, folders).map((g) => g.docs.map((d) => d.id))).toEqual([["cloud"], ["loose", "sandbox", "outbox"]]);
});

it("the sidebar groups folders by the repo each was made for, newest first, and folders of an unknown repo by their scope", () => {
  const groups = [
    { scope: ME, folders: [{ id: "rel", name: "Releases", visibility: "private", repo: "/src/brindle" }, { id: "fld_default", name: "Designs", visibility: "private", repo: null }] },
    { scope: TEAM, folders: [{ id: "eng", name: "Engine", visibility: "shared", repo: "/src/pawdelta", created_at: 1 }, { id: "app", name: "Desktop app", visibility: "shared", repo: "/src/pawdelta", created_at: 2 }] },
  ] as FolderGroup[];
  const docs: AnalysisDoc[] = [{ id: "a", updated_at: 100 }, { id: "b", updated_at: 40 }, { id: "c", updated_at: 60 }];
  const records = new Map<string, DesignRecord>([["a", inFolder(TEAM, "eng")], ["b", inFolder(ME, "rel")], ["c", inFolder(TEAM, "eng")]]);
  const tree = folderTree(placedFolders(groups), docs, records, [{ name: "brindle", repo_root: "/src/brindle" }]);
  expect(tree.map((t) => [t.label, t.count, t.folders.map((f) => [f.name, f.count, f.private])])).toEqual([
    ["pawdelta", 2, [["Engine", 2, false], ["Desktop app", 0, false]]],
    ["brindle", 1, [["Releases", 1, true]]],
    ["My folders", 0, [["Designs", 0, true]]],
  ]);
});

it("the list's and board's work item is the design's own item with the tracker's state; a failed read leaves it empty", async () => {
  const doc = (id: string) => ({ id }) as AnalysisDoc;
  const detail = { design: null, tasks: [], threads: [], decisions: [], verdict: null };
  const epic = { link_id: "wl_1", tracker: "azure_devops", key: "4810", display: "AB#4810", target: { kind: "design" }, state: "Active", done: false } as const;
  const base = { doc: async () => detail as never };
  const withItems = await loadRecords({ ...base, libraryWorkItems: async () => ({ items: { a: { epic, done: 2, total: 3 } } }) }, [doc("a"), doc("b")]);
  expect(withItems.get("a")?.work_item).toMatchObject({ key: "AB#4810", state: "Active", tone: "active" });
  expect(withItems.get("b")?.work_item).toBeUndefined();
  const failed = await loadRecords({ ...base, libraryWorkItems: async () => { throw new Error("offline"); } }, [doc("a")]);
  expect(failed.get("a")).toBeDefined();
  expect(failed.get("a")?.work_item).toBeUndefined();
});
