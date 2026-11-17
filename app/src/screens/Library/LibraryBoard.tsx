/* The Library as a board (library-redesign option A, state 2): the same designs as the list, each
   in the column of the stage it has reached (Drafting, In review, Approved, Building, Shipped),
   newest first in each. A card carries what a row carries: its folder, reviewers with their
   sign-off rings and open threads, the linked work item, its plan's tasks once building and the
   briefs it went into once shipped. A card that waits on you has an orange foot saying what for.
   Cards are not dragged: a stage is a fact about the design, not something set by hand. */

import type { AnalysisDoc } from "../../api/client";
import { CommentIcon, LockIcon } from "../../components/icons";
import { BOARD_STAGES, changedAgo, needsYou, newestFirst, openItems, signOffs, stageOf, taskProgress, type DesignRecord, type DesignStage, type Queue } from "./libraryStages";
import { Face, SignOffs, WorkItem } from "./parts";

const DOT: Record<DesignStage, string> = { drafting: "var(--ink-3)", "in-review": "var(--group-7)", approved: "var(--pass)", building: "var(--group-1)", shipped: "var(--ink-2)" };
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/* The plan's tasks as a bar: green once merged, violet while in review. */
function TaskBar({ record }: { record?: DesignRecord | null }) {
  const tasks = (record?.tasks ?? []).filter((t) => t.state !== "cancelled");
  const p = taskProgress(record);
  return (
    <span className="flex flex-1 gap-0.5" title={p ? `${plural(p.total, "task")}: ${p.merged} merged, ${p.review} in review` : undefined}>
      {tasks.map((t) => <i key={t.id} className={`h-[3px] flex-1 rounded-[1px] ${t.state === "merged" ? "bg-pass" : t.state === "in review" ? "bg-branch" : "bg-line-strong"}`} />)}
    </span>
  );
}

/* The card's middle line: what moves the design on from where it is. */
function Meta({ doc, stage, record }: { doc: AnalysisDoc; stage: DesignStage; record?: DesignRecord | null }) {
  const threads = openItems(doc, record).threads;
  const comments = threads > 0 && (
    <>
      <span className="flex-1" />
      <CommentIcon size={14} />
      <span>{threads}</span>
    </>
  );
  const p = taskProgress(record);
  if ((stage === "building" || stage === "shipped") && p) {
    return (
      <span className="flex flex-wrap items-center gap-2 text-[11.5px] text-ink-3">
        <TaskBar record={record} />
        <span>{stage === "shipped" ? "merged" : `${p.merged} of ${p.total} green`}</span>
      </span>
    );
  }
  const s = signOffs(record?.design);
  if (s) {
    const said = stage === "approved" && record?.design?.rev ? `signed version ${record.design.rev}` : s.required ? `${s.signed} of ${s.required} signed` : "optional reviewers";
    return (
      <span className="flex flex-wrap items-center gap-2 text-[11.5px] text-ink-3">
        <SignOffs record={record} say={said} />
        {comments}
      </span>
    );
  }
  if (doc.created_by) {
    return (
      <span className="flex flex-wrap items-center gap-2 text-[11.5px] text-ink-3">
        <Face sub={doc.created_by} />
        <span className="truncate">by {doc.created_by.split("@")[0]}</span>
        {comments}
      </span>
    );
  }
  return null;
}

function Card({ doc, record, queue, folder, onOpen }: { doc: AnalysisDoc; record?: DesignRecord | null; queue: Queue; folder?: { label: string; private: boolean }; onOpen(): void }) {
  const stage = stageOf(doc.id, queue, record);
  const need = needsYou(doc.id, queue);
  const work = record?.work_item ?? doc.work_item;
  const decide = openItems(doc, record).decide;
  const briefs = doc.used_in_briefs ?? 0;
  return (
    <button
      type="button"
      data-doc={doc.id}
      data-needs-you={need ?? undefined}
      onClick={onOpen}
      className={`grid w-full grid-cols-[minmax(0,1fr)] gap-[7px] rounded-md border bg-canvas-2 px-3 pb-2.5 pt-[11px] text-left transition-[border-color,transform] duration-[120ms] hover:-translate-y-px hover:border-ink-3 ${need ? "border-[color-mix(in_srgb,var(--sap)_55%,transparent)]" : "border-line-strong"}`}
    >
      <span className="flex items-center gap-1.5 text-[11.5px] text-ink-3">
        {folder && <span className="min-w-0 truncate">{folder.label}</span>}
        {folder?.private && <LockIcon size={13} className="shrink-0" />}
        <span className="flex-1" />
        <span className="shrink-0 whitespace-nowrap">{changedAgo(doc.updated_at ?? doc.created_at)}</span>
      </span>
      <span className="text-[13.5px] font-semibold leading-[1.3] text-ink">{doc.title || doc.id}</span>
      {doc.summary && <span className="line-clamp-2 text-xs leading-[1.4] text-ink-3">{doc.summary}</span>}
      <Meta doc={doc} stage={stage} record={record} />
      {(work || (stage === "shipped" && briefs > 0)) && (
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11.5px] text-ink-3">
          <WorkItem item={work} />
          {stage === "shipped" && briefs > 0 && <span className="ml-auto whitespace-nowrap">{plural(briefs, "brief")}</span>}
        </span>
      )}
      {need && (
        <span className="-mx-3 -mb-2.5 mt-0.5 rounded-b-[5px] border-t border-[color-mix(in_srgb,var(--sap)_35%,transparent)] bg-sap-soft px-3 py-1.5 text-xs text-ink">
          <b className="font-medium text-sap">{need === "review" ? "Your review" : "Changes asked"}</b>
          {need === "review" && decide > 0 && ` · ${plural(decide, "decision")} to take`}
        </span>
      )}
    </button>
  );
}

export function LibraryBoard({ docs, queue, records, folderOf, onOpen }: { docs: AnalysisDoc[]; queue: Queue; records: Map<string, DesignRecord>; folderOf(id: string): { label: string; private: boolean } | undefined; onOpen(id: string): void }) {
  const sorted = newestFirst(docs);
  return (
    <div className="grid min-h-0 flex-1 grid-cols-5 overflow-auto border-t border-line" data-testid="library-board">
      {BOARD_STAGES.map((column) => {
        const cards = sorted.filter((d) => stageOf(d.id, queue, records.get(d.id)) === column.id);
        return (
          <section key={column.id} aria-label={column.label} data-column={column.id} className="flex min-w-0 flex-col gap-2.5 border-r border-line px-3 pb-5 pt-3.5 last:border-r-0">
            <h5 className="mx-0.5 mb-0.5 flex items-center gap-[7px] text-[11px] font-medium uppercase tracking-[0.07em] text-ink-3">
              <i className="h-2 w-2 rounded-full" style={{ background: DOT[column.id] }} />
              {column.label}
              <small className="ml-auto font-mono text-[11px] tracking-normal">{cards.length}</small>
            </h5>
            {cards.map((d) => <Card key={d.id} doc={d} record={records.get(d.id)} queue={queue} folder={folderOf(d.id)} onOpen={() => onOpen(d.id)} />)}
          </section>
        );
      })}
    </div>
  );
}
