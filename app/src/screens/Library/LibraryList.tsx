/* The Library as a list (library-redesign option A, state 1): a table you can scan. Each row says
   where the design stands (its stage, or what it waits on you for, with its linked work item),
   who has signed, what is still open, who it is shared with and when it changed. "Needs you" is
   pinned on top whatever the grouping; the rest follow in groups (a folder as repo / folder, a
   stage, or none), newest first. A row opens the design. */

import type { AnalysisDoc, DocScope } from "../../api/client";
import { FolderIcon, LockIcon } from "../../components/icons";
import { changedAgo, needsYou, openItems, stageOf, type DesignRecord, type DocGroup, type Queue } from "./libraryStages";
import { scopeLabel, SignOffs, StagePill, WorkItem } from "./parts";

/* The table's columns: design, stage, sign-offs, open, shared with, changed. */
export const COLUMNS = "grid grid-cols-[minmax(0,1fr)_150px_120px_150px_124px_76px] items-center gap-x-4 px-8";

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function openLine(doc: AnalysisDoc, record?: DesignRecord | null): string {
  const o = openItems(doc, record);
  const parts = [o.threads ? plural(o.threads, "comment") : null, o.decide ? `${o.decide} to decide` : null, !o.threads && o.questions ? plural(o.questions, "open question") : null].filter(Boolean);
  return parts.length ? parts.join(" · ") : "—";
}

function Row({ doc, record, queue, scopes, match, onOpen }: { doc: AnalysisDoc; record?: DesignRecord | null; queue: Queue; scopes: DocScope[]; match?: string | null; onOpen(): void }) {
  const stage = stageOf(doc.id, queue, record);
  const need = needsYou(doc.id, queue);
  const work = record?.work_item ?? doc.work_item;
  return (
    <li data-doc={doc.id} data-stage={stage} data-needs-you={need ?? undefined}>
      <button type="button" className={`${COLUMNS} w-full border-b border-line py-[9px] text-left hover:bg-surface-2`} onClick={onOpen}>
        <span className="min-w-0">
          <span className="block truncate text-[13.5px] font-semibold text-ink">{doc.title || doc.id}</span>
          {(match || doc.summary) && <span className="block truncate text-[12.5px] text-ink-3">{match || doc.summary}</span>}
        </span>
        <span className="grid justify-items-start gap-1">
          <StagePill stage={stage} need={need} record={record} />
          <WorkItem item={work} />
        </span>
        <SignOffs record={record} />
        <span className="truncate text-xs text-ink-3">{openLine(doc, record)}</span>
        <span className="truncate text-xs text-ink-2">{scopeLabel(doc.scope, scopes)}</span>
        <span className="text-right text-xs text-ink-3">{changedAgo(doc.updated_at ?? doc.created_at)}</span>
      </button>
    </li>
  );
}

function GroupHead({ group, queue }: { group: DocGroup; queue: Queue }) {
  if (group.kind === "need") {
    const review = group.docs.filter((d) => needsYou(d.id, queue) === "review").length;
    const changes = group.docs.length - review;
    return (
      <div className="flex items-center gap-2 px-8 pb-1.5 pt-3.5 text-xs text-ink-3">
        <b className="text-[13px] font-semibold text-sap">Needs you</b>
        {[review ? `${review} to review` : null, changes ? `${changes} with changes asked` : null].filter(Boolean).join(", ")}
      </div>
    );
  }
  if (!group.label) return null;
  const total = group.docs.length + group.above;
  const above = group.above === 0 ? "" : group.above === total ? ", all of them above" : group.above === 1 ? ", one of them above" : `, ${group.above} of them above`;
  return (
    <div className="flex items-center gap-2 px-8 pb-1.5 pt-3.5 text-xs text-ink-3">
      {group.kind === "folder" && <FolderIcon size={14} />}
      <b className="text-[13px] font-semibold text-ink">{group.label}</b>
      {total}
      {above}
      {group.folder?.private && <LockIcon size={13} />}
    </div>
  );
}

export function LibraryList({ groups, queue, records, scopes, matches, onOpen }: { groups: DocGroup[]; queue: Queue; records: Map<string, DesignRecord>; scopes: DocScope[]; matches?: Map<string, string | null>; onOpen(id: string): void }) {
  if (groups.length === 0) return null;
  return (
    <div className="flex flex-col" data-testid="library-list">
      <div className={`${COLUMNS} sticky top-0 z-[2] border-b border-line-strong bg-canvas pb-1.5 text-[11px] font-medium uppercase tracking-[0.07em] text-ink-3`} aria-hidden>
        <span>Design</span>
        <span>Stage</span>
        <span>Sign-offs</span>
        <span>Open</span>
        <span>Shared with</span>
        <span className="text-right text-branch" title="Most recently changed first">Changed ↓</span>
      </div>
      {groups.map((group) => (
        <section key={group.key} aria-label={group.label || "Designs"} data-group={group.key}>
          <GroupHead group={group} queue={queue} />
          <ul className="flex flex-col">
            {group.docs.map((d) => <Row key={d.id} doc={d} record={records.get(d.id)} queue={queue} scopes={scopes} match={matches?.get(d.id)} onOpen={() => onOpen(d.id)} />)}
          </ul>
        </section>
      ))}
    </div>
  );
}
