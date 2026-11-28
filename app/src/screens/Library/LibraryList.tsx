/* The Library as a list (library-redesign option A, state 1): a table you can scan. Each row says
   where the design stands (its stage, or what it waits on you for, with its linked work item),
   who has signed, what is still open, who it is shared with and when it changed. "Needs you" is
   pinned on top whatever the grouping; the rest follow in groups (a folder as repo / folder, a
   stage, or none), newest first. A row opens the design. */

import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { AnalysisDoc, BulkDocResult, BulkDocsRequest, DaemonClient, DocScope } from "../../api/client";
import type { RepoDTO } from "../../api/schema";
import { plain } from "../../api/wording";
import { FolderIcon, LockIcon } from "../../components/icons";
import { alert, button, card, danger, notice, quiet } from "../../components/ui";
import { libraryChanged } from "../../store/libraryReader";
import { changedAgo, folderTree, needsYou, openItems, scopeName, scopeSpec, stageOf, withScopes, type DesignRecord, type DocGroup, type PlacedFolder, type Queue } from "./libraryStages";
import { scopeLabel, SignOffs, StagePill, WorkItem } from "./parts";

/* The table's columns: design, stage, sign-offs, open, shared with, changed. */
export const COLUMNS = "grid grid-cols-[minmax(0,1fr)_150px_120px_150px_124px_76px] items-center gap-x-4 px-8";
// A 1400px window leaves 1048px for the Library beside the 352px sidebar.
const TABLE_MIN_WIDTH = 1048;
const checkbox = "absolute left-2 top-1/2 size-4 -translate-y-1/2 accent-branch";

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function openLine(doc: AnalysisDoc, record?: DesignRecord | null): string {
  const o = openItems(doc, record);
  const parts = [o.threads ? plural(o.threads, "comment") : null, o.decide ? `${o.decide} to decide` : null, !o.threads && o.questions ? plural(o.questions, "open question") : null].filter(Boolean);
  return parts.length ? parts.join(" · ") : "—";
}

function Row({ doc, record, queue, scopes, match, check, failure, stacked, onOpen }: { doc: AnalysisDoc; record?: DesignRecord | null; queue: Queue; scopes: DocScope[]; match?: string | null; check: ReactNode; failure?: BulkDocResult; stacked: boolean; onOpen(): void }) {
  const stage = stageOf(doc.id, queue, record);
  const need = needsYou(doc.id, queue);
  const work = record?.work_item ?? doc.work_item;
  const grants = record?.design?.can_edit !== undefined || record?.design?.can_comment !== undefined ? record.design
    : record?.can_edit !== undefined || record?.can_comment !== undefined ? record : doc;
  const access = grants?.can_edit ? "you can edit" : grants?.can_comment ? "you can comment"
    : grants?.can_edit === false && grants.can_comment === false ? "you can view" : null;
  const changed = (
    <span data-field="changed" className={`text-right text-xs text-ink-3 ${stacked ? "whitespace-nowrap" : ""}`}>
      {changedAgo(doc.updated_at ?? doc.created_at)}
    </span>
  );
  const metadata = (
    <>
      <span data-field="stage" className={stacked ? "flex flex-wrap items-center gap-x-s1 gap-y-1" : "grid justify-items-start gap-1"}>
        <StagePill stage={stage} need={need} record={record} />
        <WorkItem item={work} />
      </span>
      <span data-field="sign-offs" className={stacked ? undefined : "contents"}><SignOffs record={record} /></span>
      <span data-field="open" className={`text-xs text-ink-3 ${stacked ? "wrap-anywhere" : "truncate"}`}>{openLine(doc, record)}</span>
      <span data-field="shared-with" className={`text-xs text-ink-2 ${stacked ? "wrap-anywhere" : ""}`}>
        {scopeLabel(doc.scope, scopes)}{access && <span className="block text-ink-3">{access}</span>}
      </span>
    </>
  );
  return (
    <li className="group border-b border-line" data-doc={doc.id} data-layout={stacked ? "stacked" : "table"} data-stage={stage} data-needs-you={need ?? undefined} data-to-decide={openItems(doc, record).decide}>
      <div className="relative">
        {check}
        <button type="button" className={`${stacked ? "grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-s2 gap-y-1 px-8" : COLUMNS} w-full py-[9px] text-left hover:bg-surface-2`} onClick={onOpen}>
          <span data-field="title" className="min-w-0">
            <span data-plan-title className={`block text-[13.5px] font-semibold text-ink ${stacked ? "wrap-anywhere" : "truncate"}`}>{doc.title || doc.id}</span>
            {(match || doc.summary) && <span className="block truncate text-[12.5px] text-ink-3">{match || doc.summary}</span>}
          </span>
          {stacked ? (
            <>
              {changed}
              <span data-field="metadata" className="col-span-2 flex min-w-0 flex-wrap items-center gap-x-s2 gap-y-1">{metadata}</span>
            </>
          ) : <>{metadata}{changed}</>}
        </button>
      </div>
      {failure && (
        <p role="alert" data-code={failure.code} className="px-8 pb-2 text-xs text-sap">
          {(failure.error && plain(failure.error)) || "The action failed for this plan."}
        </p>
      )}
    </li>
  );
}

function GroupHead({ group, queue, check }: { group: DocGroup; queue: Queue; check: ReactNode }) {
  if (group.kind === "need") {
    const review = group.docs.filter((d) => needsYou(d.id, queue) === "review").length;
    const changes = group.docs.length - review;
    return (
      <div className="relative flex items-center gap-2 px-8 pb-1.5 pt-3.5 text-xs text-ink-3">
        {check}
        <b className="text-[13px] font-semibold text-sap">Needs you</b>
        {[review ? `${review} to review` : null, changes ? `${changes} with changes asked` : null].filter(Boolean).join(", ")}
      </div>
    );
  }
  if (!group.label) return null;
  const total = group.docs.length + group.above;
  const above = group.above === 0 ? "" : group.above === total ? ", all of them above" : group.above === 1 ? ", one of them above" : `, ${group.above} of them above`;
  return (
    <div className="relative flex items-center gap-2 px-8 pb-1.5 pt-3.5 text-xs text-ink-3">
      {check}
      {group.kind === "folder" && <FolderIcon size={14} />}
      <b className="text-[13px] font-semibold text-ink">{group.label}</b>
      {total}
      {above}
      {group.folder?.private && <LockIcon size={13} />}
    </div>
  );
}

export type BulkDocsClient = Partial<Pick<DaemonClient, "bulkDocs" | "exportDocs">>;

interface LibraryListProps {
  client: BulkDocsClient;
  repo?: string;
  groups: DocGroup[];
  queue: Queue;
  records: Map<string, DesignRecord>;
  scopes: DocScope[];
  folders: PlacedFolder[];
  repos?: Pick<RepoDTO, "name" | "repo_root">[] | null;
  matches?: Map<string, string | null>;
  onOpen(id: string): void;
}

type Action = BulkDocsRequest["action"] | "download";
type Menu = "move" | "share" | "delete";
const outcome: Record<Action, [string, string]> = {
  move: ["Moved", "moved"],
  share: ["Shared", "shared"],
  unshare: ["Stopped sharing", "unshared"],
  delete: ["Deleted", "deleted"],
  download: ["Downloaded", "downloaded"],
};

export function LibraryList({ client, repo, groups, queue, records, scopes, folders, repos, matches, onOpen }: LibraryListProps) {
  const [stacked, setStacked] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [menu, setMenu] = useState<Menu | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [failures, setFailures] = useState<Map<string, BulkDocResult>>(() => new Map());
  const deleteDescription = useId();
  const moveDescription = useId();
  const list = useRef<HTMLDivElement>(null);
  const anchor = useRef<string | null>(null);
  const running = useRef(false);
  const visible = groups.length > 0 || Boolean(said);

  useLayoutEffect(() => {
    const node = list.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    setStacked(node.clientWidth < TABLE_MIN_WIDTH);
    const observer = new ResizeObserver(([entry]) => {
      setStacked(entry.contentRect.width < TABLE_MIN_WIDTH);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [visible]);

  const rows = groups.flatMap((group) => group.docs);
  const picked = rows.filter((doc) => selected.has(doc.id));
  const ids = picked.map((doc) => doc.id);
  const hasSelection = ids.length > 0;
  const hasLocalOnly = picked.some((doc) => doc.local_only);

  function clear() {
    setSelected(new Set());
    anchor.current = null;
    setMenu(null);
  }

  useEffect(() => {
    if (!hasSelection) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented && !running.current) {
        event.preventDefault();
        clear();
      }
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [hasSelection]);

  function select(docs: AnalysisDoc[], checked: boolean) {
    setSelected((previous) => {
      const next = new Set(previous);
      for (const { id } of docs) {
        if (checked) next.add(id);
        else next.delete(id);
      }
      return next;
    });
    setMenu(null);
  }

  function selectRow(doc: AnalysisDoc, checked: boolean, shift: boolean) {
    const start = shift ? rows.findIndex(({ id }) => id === anchor.current) : -1;
    const end = rows.indexOf(doc);
    select(start < 0 ? [doc] : rows.slice(Math.min(start, end), Math.max(start, end) + 1), checked);
    anchor.current = doc.id;
  }

  function groupCheck(group: DocGroup) {
    const count = group.docs.filter(({ id }) => selected.has(id)).length;
    return (
      <input type="checkbox" className={checkbox} checked={count === group.docs.length} disabled={busy}
        aria-label={`Select all in ${group.label || "Plans"}`}
        ref={(node) => { if (node) node.indeterminate = count > 0 && count < group.docs.length; }}
        onChange={(event) => {
          anchor.current = null;
          select(group.docs, event.currentTarget.checked);
        }} />
    );
  }

  async function run(action: Action, task: () => Promise<BulkDocResult[]>) {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setMenu(null);
    setSaid(null);
    try {
      const results = await task();
      const succeeded = new Set(results.filter((result) => result.ok).map((result) => result.id));
      const done = ids.filter((id) => succeeded.has(id)).length;
      const failed = ids.length - done;
      setSelected((previous) => new Set([...previous].filter((id) => !succeeded.has(id))));
      setFailures((previous) => {
        const next = new Map(previous);
        for (const result of results) {
          if (result.ok) next.delete(result.id);
          else next.set(result.id, result);
        }
        return next;
      });
      if (action === "delete" && results.some((result) => !result.ok && result.code === "confirm_shared")) {
        setMenu("delete");
      }
      const [verb, past] = outcome[action];
      setSaid(`${verb} ${done}${failed ? ` · ${failed} couldn't be ${past}` : ""}`);
    } catch (error) {
      setSaid(plain(error instanceof Error ? error.message : String(error)));
    } finally {
      if (action !== "download") libraryChanged();
      setBusy(false);
      running.current = false;
    }
  }

  function bulk(action: BulkDocsRequest["action"], options: Omit<BulkDocsRequest, "action" | "ids" | "repo"> = {}) {
    if (!client.bulkDocs) return;
    const submit = client.bulkDocs.bind(client);
    void run(action, async () => (await submit({ action, ids, repo, ...options })).results);
  }

  function download() {
    if (!client.exportDocs) return;
    const exportDocs = client.exportDocs.bind(client);
    void run("download", async () => {
      const { downloads, skipped } = await exportDocs(ids, "zip", "download", repo);
      for (const [index, archive] of downloads.entries()) {
        const bytes = Uint8Array.from(atob(archive.content_b64), (c) => c.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bytes], { type: "application/zip" }));
        const link = document.createElement("a");
        link.href = url;
        link.download = downloads.length > 1 ? archive.filename.replace(/\.zip$/i, ` (${index + 1}).zip`) : archive.filename;
        link.click();
        URL.revokeObjectURL(url);
      }
      const refused = new Map(skipped.map((result) => [result.id, result]));
      return ids.map((id) => ({ id, ok: !refused.has(id), ...refused.get(id) }));
    });
  }

  const sharedWith = (id: string) => records.get(id)?.shared_with ?? [];
  const hasShared = picked.some((doc) => failures.get(doc.id)?.code === "confirm_shared" || (doc.scope && doc.scope.kind !== "user") || sharedWith(doc.id).length > 0);
  const shareTargets = menu === "share" ? withScopes(scopes, picked.flatMap(({ id }) => sharedWith(id))).filter((scope) => scope.kind !== "user") : [];
  const shareButtons = shareTargets.flatMap((scope) => {
    const spec = scopeSpec(scope);
    const shared = (id: string) => sharedWith(id).some((target) => scopeSpec(target) === spec);
    const canShare = scope.can_share && picked.some((doc) => (!doc.scope || scopeSpec(doc.scope) !== spec) && !shared(doc.id));
    return [
      canShare && <button key={`share:${spec}`} type="button" className={button} onClick={() => bulk("share", { scope: spec })}>Share with {scopeName(scope)}</button>,
      picked.some(({ id }) => shared(id)) && <button key={`unshare:${spec}`} type="button" className={button} onClick={() => bulk("unshare", { scope: spec })}>Stop sharing with {scopeName(scope)}</button>,
    ].filter(Boolean);
  });
  const unlabelled = groups.find((group) => !group.label);

  if (!visible) return null;
  return (
    <div ref={list} className="flex min-w-0 flex-col" data-testid="library-list">
      <div className="sticky top-0 z-[2] bg-canvas">
        {hasSelection && (
          <div className={`${card} mx-8 mb-2 flex flex-col gap-1 pt-2`}>
            <div className="flex flex-wrap items-center gap-1" role="toolbar" aria-label="Selected plans">
              <span className="mr-2 text-sm font-medium">{ids.length} selected</span>
              <button type="button" className={button} disabled={busy || !client.bulkDocs || hasLocalOnly}
                aria-describedby={hasLocalOnly ? moveDescription : undefined} aria-expanded={menu === "move"}
                onClick={() => setMenu(menu === "move" ? null : "move")}>Move to folder</button>
              <button type="button" className={button} disabled={busy || !client.bulkDocs} aria-expanded={menu === "share"} onClick={() => setMenu(menu === "share" ? null : "share")}>Share / Stop sharing</button>
              <button type="button" className={button} disabled={busy || !client.exportDocs} onClick={download}>Download</button>
              <button type="button" className={`${quiet} text-sap`} disabled={busy || !client.bulkDocs} aria-haspopup="dialog" aria-expanded={menu === "delete"} onClick={() => setMenu(menu === "delete" ? null : "delete")}>Delete</button>
              <button type="button" className={quiet} disabled={busy} onClick={clear}>Clear</button>
            </div>
            {hasLocalOnly && <p id={moveDescription} className="text-xs text-ink-2">Plans saved only on this device can’t be moved to Library folders.</p>}
            {menu === "move" && !hasLocalOnly && (
              <div className={`${card} flex flex-col gap-1 px-s2 py-s1 text-sm`} role="group" aria-label="Move to folder">
                {!folders.length && <span className="text-ink-2">There are no Library folders yet.</span>}
                {folderTree(folders, rows, records, repos).map((tree) => (
                  <div key={tree.key} className="flex flex-wrap items-center gap-1">
                    <span className="mr-1 text-xs text-ink-3">{tree.label}</span>
                    {tree.folders.map((folder) => (
                      <button key={folder.key} type="button" className={button} onClick={() => bulk("move", { folder: folder.id, scope: folder.spec })}>
                        <FolderIcon size={14} />{folder.name}{folder.private && <LockIcon size={13} />}
                      </button>
                    ))}
                  </div>
                ))}
              </div>
            )}
            {menu === "share" && (
              <div className={`${card} flex flex-wrap items-center gap-1 px-s2 py-s1 text-sm`} role="group" aria-label="Share with">
                {shareButtons.length ? shareButtons : <span className="text-ink-2">There are no sharing options for these plans.</span>}
              </div>
            )}
            {menu === "delete" && (
              <div role="alertdialog" aria-label="Delete selected plans" aria-describedby={deleteDescription} className={`${alert} flex flex-wrap items-center gap-s1`}>
                <span id={deleteDescription} className="flex-1">Delete {plural(ids.length, "plan")} from the Library? {hasShared && "Some are shared. Deleting them stops sharing them. "}This can't be undone.</span>
                <button type="button" className={quiet} autoFocus onClick={() => setMenu(null)}>Cancel</button>
                <button type="button" className={danger} onClick={() => bulk("delete", hasShared ? { confirm: true } : {})}>{hasShared ? "Stop sharing and delete" : "Delete for good"}</button>
              </div>
            )}
          </div>
        )}
        {said && <p role="status" className={`${notice} mx-8 mb-2`}>{said}</p>}
        {groups.length > 0 && (!stacked || unlabelled) && <div className={`${stacked ? "px-8" : COLUMNS} relative border-b border-line-strong pb-1.5 text-[11px] font-medium uppercase tracking-[0.07em] text-ink-3`}>
          {unlabelled && groupCheck(unlabelled)}
          <span>Plan</span>
          {!stacked && <>
            <span>Stage</span>
            <span>Sign-offs</span>
            <span>Open</span>
            <span>Shared with</span>
            <span className="text-right text-branch" title="Most recently changed first">Changed ↓</span>
          </>}
        </div>}
      </div>
      {groups.map((group) => (
        <section key={group.key} aria-label={group.label || "Plans"} data-group={group.key}>
          <GroupHead group={group} queue={queue} check={groupCheck(group)} />
          <ul className="flex flex-col">
            {group.docs.map((d) => (
              <Row key={d.id} doc={d} record={records.get(d.id)} queue={queue} scopes={scopes} match={matches?.get(d.id)} failure={failures.get(d.id)} stacked={stacked} onOpen={() => onOpen(d.id)}
                check={<input type="checkbox" aria-label={`Select ${d.title || d.id}`} checked={selected.has(d.id)} disabled={busy}
                  className={`${checkbox} ${hasSelection ? "" : "opacity-0 group-hover:opacity-100 group-focus-within:opacity-100"}`}
                  onChange={(event) => selectRow(d, event.currentTarget.checked, (event.nativeEvent as MouseEvent).shiftKey)} />} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
