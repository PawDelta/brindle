/* Two parts of the reader's side column (DocReview.tsx) whose data comes from elsewhere, drawn
   from plain lists so whoever fills them only has to hand the list over:
   - History: one timeline of what happened to the doc, newest first (revisions, freezes, reopens,
     sign-offs, shares), each with who, when and what it touched. Until the reader is given the
     events (`events` null), the tab says where they will come from.
   - Work items: the tracker items (Azure Boards, Jira) linked to the design's tasks and passages,
     under the Tasks tab. Until given (`items` null), the tab says so. */

import { useEffect, useRef, useState, type ReactNode } from "react";
import type { DocHistoryEntry, DocRevisionComparison } from "../../../api/client";
import { label, quiet } from "../../../components/ui";

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
        const on = e.kind === "revision" && e.rev != null && e.rev === current;
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
            {e.kind === "revision" && e.rev != null && onPick ? <button type="button" className="cursor-pointer text-left" aria-pressed={on} onClick={() => onPick(e.rev!)}>{body}</button> : body}
          </li>
        );
      })}
    </ol>
  );
}

export interface HistoryClient {
  planHistory(id: string): Promise<{ events: DocHistoryEntry[] }>;
  planRevisionChanges(id: string, rev: number): Promise<DocRevisionComparison>;
}

export function historyEvents(entries: DocHistoryEntry[]): HistoryEvent[] {
  return [...entries].sort((a, b) => b.at - a.at || b.id.localeCompare(a.id)).map((e) => ({
    id: e.id, kind: e.kind, who: e.actor === "person" ? "You" : e.actor.replace(/^teammate:/, ""),
    what: e.kind === "revision" ? `made revision ${e.rev} · ${e.source ?? "edit"}`
      : e.kind === "freeze" ? `froze version ${e.version} for sign-off`
      : e.kind === "reopen" ? `reopened version ${e.version} · its sign-offs withdrawn`
      : e.kind === "sign_off" ? `signed ${e.version == null ? `revision ${e.rev}` : `version ${e.version}`}`
      : "requested changes",
    when: `${e.observed ? "Observed " : ""}${new Date(e.at * 1000).toLocaleString()}`, detail: e.changed?.join(", "),
    rev: e.kind === "revision" ? e.rev ?? undefined : undefined,
  }));
}

/* Drop into the History tab. Pass onCompare to put the comparison in the reader's document
   pane; without it the component includes its own read-only document preview. Increment refresh
   after a live action or sync. No writes are made when selecting or opening an old revision. */
export function DocumentHistory({ client, planId, refresh, onCompare }: {
  client: HistoryClient; planId: string; refresh?: unknown;
  onCompare?(comparison: DocRevisionComparison | null, close: () => void): void;
}) {
  const [events, setEvents] = useState<HistoryEvent[] | null>(null);
  const [comparison, setComparison] = useState<DocRevisionComparison | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const request = useRef(0);
  const compareCallback = useRef(onCompare);
  compareCallback.current = onCompare;
  const identity = useRef({ client, planId });
  identity.current = { client, planId };
  useEffect(() => {
    let active = true;
    request.current++;
    setEvents(null); setComparison(null); setError(false); setLoading(false);
    compareCallback.current?.(null, close);
    void client.planHistory(planId).then((result) => {
      if (active && identity.current.client === client && identity.current.planId === planId) setEvents(historyEvents(result.events));
    }, () => { if (active && identity.current.client === client && identity.current.planId === planId) setError(true); });
    return () => { active = false; request.current++; compareCallback.current?.(null, close); };
  }, [client, planId, refresh]);
  const pick = async (rev: number) => {
    const sequence = ++request.current;
    const isCurrent = () => request.current === sequence && identity.current.client === client && identity.current.planId === planId;
    setError(false); setLoading(true);
    try {
      const result = await client.planRevisionChanges(planId, rev);
      if (!isCurrent()) return;
      setComparison(result); compareCallback.current?.(result, close);
    } catch {
      if (isCurrent()) setError(true);
    } finally {
      if (isCurrent()) setLoading(false);
    }
  };
  const close = () => { request.current++; setLoading(false); setComparison(null); compareCallback.current?.(null, close); };
  return <div aria-busy={loading}>
    {error && <p role="alert" className="px-3.5 py-3 text-sap">History could not be loaded.</p>}
    <DocHistory events={events} current={comparison?.revision.rev} onPick={(rev) => { void pick(rev); }} />
    {comparison && (onCompare
      ? <button type="button" className={quiet} onClick={close}>Back to the latest</button>
      : <DocHistoryComparison key={comparison.revision.rev} comparison={comparison} onClose={close} />)}
  </div>;
}

/* Render in the reader's document pane: the complete revision, with deletions retained in place.
   Text is escaped by React, including SVG and diagram source in historical changes. */
export function DocHistoryComparison({ comparison, onClose }: { comparison: DocRevisionComparison; onClose(): void }) {
  const { revision, previous, changes } = comparison;
  const [active, setActive] = useState(0);
  const baselineAvailable = comparison.baseline_available ?? (previous != null || revision.rev === 1);
  const [showOriginal, setShowOriginal] = useState(false);
  const original = showOriginal || !baselineAvailable;
  const blocks = useRef(new Map<string, HTMLElement>());
  const targets = changes.flatMap((change) => change.spans.length
    ? change.spans.map((_, span) => ({ section: change.section, span: span as number | null }))
    : [{ section: change.section, span: null }]);
  const jump = (index: number) => {
    setActive(index);
    const target = targets[index];
    (blocks.current.get(`${target.section}:${target.span}`) ?? blocks.current.get(target.section))?.scrollIntoView?.({ block: "center" });
  };
  const sections: { id: string; title: string; markdown: string }[] = [...(comparison.document ?? [
    { id: "lede", title: "Overview", markdown: revision.lede }, ...revision.sections,
  ])];
  for (const change of changes) {
    if (!change.after && change.before && !original) sections.push({ id: change.section, ...change.before });
  }
  const text = (id: string, markdown: string): ReactNode => {
    const change = changes.find((c) => c.section === id);
    if (original || !change) return markdown;
    const result: ReactNode[] = [];
    const body = change.after?.markdown ?? "";
    // Engine offsets count Unicode code points; JavaScript string offsets count UTF-16 units.
    const characters = Array.from(body);
    let position = 0;
    change.spans.forEach((span, i) => {
      result.push(characters.slice(position, span.start).join(""));
      result.push(<span key={i} data-active-change={targets[active]?.section === id && targets[active]?.span === i}
        ref={(node) => { if (node) blocks.current.set(`${id}:${i}`, node); else blocks.current.delete(`${id}:${i}`); }}>
        {span.before && <del className="bg-sap-soft text-sap">{span.before}</del>}
        {span.after && <ins className="bg-pass-soft text-pass no-underline">{span.after}</ins>}
      </span>);
      position = span.end;
    });
    result.push(characters.slice(position).join(""));
    return result;
  };
  return <section aria-label="Revision comparison" data-revision={revision.rev} data-state={!baselineAvailable ? "unavailable-baseline" : original ? "original" : "changes"}>
    <div className="flex flex-wrap items-center gap-2 border-b border-line p-3">
      <b>{`Revision ${revision.rev}${previous ? ` against revision ${previous.rev}` : baselineAvailable ? " · first revision" : " · earlier revision unavailable"}`}</b>
      <button type="button" className={quiet} disabled={original || active === 0 || !targets.length} onClick={() => jump(active - 1)}>Previous change</button>
      <button type="button" className={quiet} disabled={original || active >= targets.length - 1} onClick={() => jump(active + 1)}>Next change</button>
      <button type="button" className={quiet} disabled={!baselineAvailable} onClick={() => setShowOriginal(!showOriginal)}>{!baselineAvailable ? "Comparison unavailable" : original ? "Show changes" : "Open revision as it was"}</button>
      <button type="button" className={quiet} onClick={onClose}>Back to the latest</button>
    </div>
    {sections.map((s) => {
      const change = changes.find((c) => c.section === s.id);
      return <section key={s.id} ref={(node) => { if (node) blocks.current.set(s.id, node); else blocks.current.delete(s.id); }}
        className="p-3" data-section={s.id} data-changed={!!change} data-active={!original && targets[active]?.section === s.id}>
        <h3>{!original && change && change.before?.title !== change.after?.title
          ? <>{change.before && <del className="text-sap">{change.before.title}</del>}{" "}{change.after && <ins className="text-pass no-underline">{change.after.title}</ins>}</>
          : s.title}</h3>
        {!original && change && <p className="text-ink-3">{!change.after ? "Section removed" : !change.before ? "Section added" : "Section changed"}</p>}
        <div className="whitespace-pre-wrap break-words">{text(s.id, s.markdown)}</div>
        {!original && change && change.before?.caption !== change.after?.caption && <p>
          {change.before?.caption && <del className="text-sap">{change.before.caption}</del>}{" "}
          {change.after?.caption && <ins className="text-pass no-underline">{change.after.caption}</ins>}
        </p>}
      </section>;
    })}
  </section>;
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
