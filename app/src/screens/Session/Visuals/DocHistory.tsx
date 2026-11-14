/* Two parts of the reader's side column (DocReview.tsx) whose data comes from elsewhere, drawn
   from plain lists so whoever fills them only has to hand the list over:
   - History: one timeline of what happened to the doc, newest first (revisions, freezes, reopens,
     sign-offs, shares), each with who, when and what it touched. Until the reader is given the
     events (`events` null), the tab says where they will come from.
   - Work items: the tracker items (Azure Boards, Jira) linked to the design's tasks and passages,
     under the Tasks tab. Until given (`items` null), the tab says so. */

import { label } from "../../../components/ui";

export interface HistoryEvent {
  id: string;
  kind: "revision" | "freeze" | "reopen" | "sign_off" | "changes" | "share";
  /* "Dana Okafor signed version 2", with who in bold: `who` then `what`. */
  who: string;
  what: string;
  /* When, as the reader shows it ("Today 14:31"), and what else to say under it. */
  when: string;
  detail?: string;
  /* A revision's number: picking it shows what it changed in the doc. */
  rev?: number;
}

export interface WorkItem {
  key: string;
  title: string;
  /* The tracker's own word for its state ("Active", "New", "Closed") and when it was read. */
  state: string;
  done?: boolean;
  active?: boolean;
  url?: string;
  /* What it was made from: a task of the split, or a passage. */
  from?: string;
}

const dot = { sign_off: "border-pass bg-pass", freeze: "border-branch bg-branch" } as Record<string, string>;

export function DocHistory({ events, current, onPick }: { events: HistoryEvent[] | null; current?: number | null; onPick?(rev: number): void }) {
  if (!events) {
    return (
      <div className="grid gap-1.5 px-3.5 py-3 text-ink-3" data-testid="doc-history" data-state="pending">
        <span className={`${label} text-[11px]`}>History</span>
        <p>Every revision, freeze, reopen and sign-off of this doc will be listed here in order, with who and when. Pick a revision to see what it changed in the doc.</p>
      </div>
    );
  }
  if (events.length === 0) return <p className="px-3.5 py-3 text-ink-3" data-testid="doc-history" data-state="empty">Nothing has happened to this doc yet.</p>;
  return (
    <ol className="m-0 list-none px-3.5 py-1.5" data-testid="doc-history" data-state="ready">
      {events.map((e) => {
        const on = e.rev != null && e.rev === current;
        const body = (
          <span className={`grid gap-0.5 rounded-md py-2.5 pr-2.5 ${on ? "-ml-2.5 bg-surface-2 pl-2.5 shadow-[inset_2px_0_0_var(--branch)]" : ""}`}>
            <span><b className="font-medium text-ink">{e.who}</b>{` ${e.what}`}</span>
            <span className="text-[11.5px] text-ink-3">{[e.when, e.detail].filter(Boolean).join(" · ")}</span>
          </span>
        );
        return (
          <li key={e.id} className="grid grid-cols-[18px_minmax(0,1fr)] gap-x-2.5" data-kind={e.kind}>
            <span className="relative w-px justify-self-center bg-line-strong">
              <i className={`absolute -left-1 top-[15px] size-[9px] rounded-full border-[1.5px] ${dot[e.kind] ?? "border-ink-3 bg-canvas"}`} />
            </span>
            {e.rev != null && onPick ? <button type="button" className="cursor-pointer text-left" aria-pressed={on} onClick={() => onPick(e.rev!)}>{body}</button> : body}
          </li>
        );
      })}
    </ol>
  );
}

export function WorkItems({ items }: { items: WorkItem[] | null }) {
  return (
    <div className="grid gap-2 border-t border-line px-3.5 py-3" data-testid="doc-work-items" data-state={items ? "ready" : "pending"}>
      <span className={`${label} text-[11px]`}>Work items</span>
      {!items ? <p className="text-ink-3">Items in Azure Boards or Jira linked to this design and its tasks will be listed here, with their state.</p>
        : items.length === 0 ? <p className="text-ink-3">No work items are linked to this design.</p>
        : items.map((w) => (
          <div key={w.key} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2 gap-y-0.5">
            <b className="truncate font-medium text-ink">{w.title}</b>
            <span className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-sm border border-line px-1.5 font-mono text-[11px] leading-[17px] text-ink-2">
              <i className={`size-1.5 rounded-full ${w.done ? "bg-pass" : w.active ? "bg-[var(--group-1)]" : "bg-ink-3"}`} />
              {`${w.key} · ${w.state}`}
            </span>
            {w.from && <span className="col-span-full text-[11.5px] text-ink-3">{w.from}</span>}
          </div>
        ))}
    </div>
  );
}
