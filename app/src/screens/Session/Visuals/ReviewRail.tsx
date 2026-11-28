import { useContext, useLayoutEffect, useState, type ReactNode } from "react";
import type { Plan, TrackerLink } from "../../../api/client";
import { ago } from "../../../components/ProviderConnect";
import type { SignOffRow } from "./DocReview";
import { DeliveryStatus, PendingNote, PlanDelivery } from "./PlanDelivery";
import { WorkItemChip } from "./WorkPanel";

function QueuedItems({ plan }: { plan: Plan }) {
  const items = useContext(PlanDelivery).filter(item => item.state === "waiting" || item.state === "sending");
  if (!items.length) return null;
  return <section aria-label="Queued changes" className="grid gap-1 text-xs text-ink-3" data-testid="rail-queue">
    {items.map(item => {
      if (item.tail === "/comments" || item.tail === "/suggestions") return <PendingNote key={item.client_id} item={item} />;
      const decision = plan.decisions.find(d => d.id === item.body.decision);
      const option = decision?.options.find(o => o.id === item.body.option);
      return <div key={item.client_id} className="grid gap-1">
        <span>{item.tail === "/sign-offs" ? "Sign-off" : [decision?.title, option?.label].filter(Boolean).join(" · ") || "Decision"}</span>
        <DeliveryStatus item={item} />
      </div>;
    })}
  </section>;
}

export function useRailWidth() {
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  const [folded, setFolded] = useState(false);
  useLayoutEffect(() => {
    if (!host) return;
    const measure = (width: number) => setFolded(width > 0 && width < 900);
    measure(host.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const watch = new ResizeObserver(([entry]) => measure(entry?.contentRect.width ?? host.clientWidth));
    watch.observe(host);
    return () => watch.disconnect();
  }, [host]);
  return { setHost, folded };
}

const heading = "flex justify-between text-[10.5px] uppercase font-semibold tracking-[0.08em] text-ink-2";

export function ReviewRevisions({ plan, compact = false }: { plan: Plan; compact?: boolean }) {
  const revisions = (plan.revisions ?? []).map(({ rev, title, at, source }) => ({
    rev, at, title: title?.trim() || (source === "suggestion" ? "Accepted suggestion" : source === "edit" ? "Edit" : rev === 1 ? "The first proposal" : "Revised proposal"),
  }));
  const recorded = revisions.find(r => r.rev === plan.rev);
  const latest = recorded ?? {
    rev: plan.rev, at: plan.rev === 1 ? plan.created_at : undefined, title: plan.rev === 1 ? "The first proposal" : plan.title,
  };
  if (compact) {
    return <span>Revisions · rev {latest.rev} · {latest.title}{latest.at !== undefined && ` · ${ago(latest.at)}`}</span>;
  }
  const rows = recorded ? revisions : [...revisions, latest];
  return <section className="grid gap-1">
    <h3 className={heading}>Revisions</h3>
    {rows.sort((a, b) => b.rev - a.rev).map(({ rev, title, at }) => <div className="flex items-baseline gap-3" key={rev}>
      <span className="shrink-0 font-mono">rev {rev}</span>
      <div className="min-w-0 [overflow-wrap:anywhere]">
        <div>{title}</div>
        {at !== undefined && <div className="text-xs text-ink-3">{ago(at)}</div>}
      </div>
    </div>)}
  </section>;
}

export function ReviewRail({ children, sheet, plan, settled, chosen, jump, alongside, workItem, folded, rows, held, reviewing }: {
  children: ReactNode; sheet: ReactNode; plan: Plan; settled(d: Plan["decisions"][number]): boolean;
  jump(id: string): void; alongside?: ReactNode; folded: boolean; rows: SignOffRow[];
  workItem?: Pick<TrackerLink, "display" | "title" | "state" | "done" | "stale"> | null;
  chosen(d: Plan["decisions"][number]): string | null; held: boolean; reviewing: boolean;
}) {
  const picks = plan.decisions.map(settled);
  const signatures = rows.filter(r => r.required).map(r => r.state === "signed" || r.state === "waived");
  const current = plan.state !== "open" ? null : picks.some(done => !done) ? "Decide"
    : held || reviewing ? "Sign-offs" : "Approve";
  const steps = [
    { name: "Decide", segments: picks }, { name: "Sign-offs", segments: signatures },
    { name: "Approve", segments: [held || plan.state === "approved"] },
  ];
  const workTitle = workItem?.title?.trim();
  return <aside aria-label="Review" data-testid="review-rail" data-folded={folded}
    className={folded ? "flex shrink-0 flex-col border-t border-line" : "flex w-[304px] min-h-0 shrink-0 flex-col border-l border-line"}>
    {!folded && <div className="grid min-h-0 flex-1 content-start gap-[22px] overflow-auto px-4 pb-5 pt-[30px] text-[12.5px] text-ink-2">
      {sheet}
      <section className="grid gap-2"><h3 className={heading}>Decisions <small className="font-normal normal-case">{picks.filter(Boolean).length} of {picks.length} decided</small></h3>{plan.decisions.map(d =>
        <button key={d.id} type="button" className="grid grid-cols-[14px_1fr] gap-x-1 text-left hover:underline" onClick={() => jump(d.id)}><span aria-hidden className={settled(d) ? "text-pass" : "text-ink-3"}>{settled(d) ? "✓" : "○"}</span> {d.title}
          {settled(d) && <small className="col-start-2 text-ink-3">{d.options.find(o => o.id === chosen(d))?.label}</small>}</button>)}</section>
      {alongside}
      <ReviewRevisions plan={plan} />
      {workItem && <section className="grid gap-1">
        <h3 className={heading}>Work item</h3>
        <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
          {workTitle && workTitle !== workItem.display && <span className="min-w-0 [overflow-wrap:anywhere]">{workTitle}</span>}
          <WorkItemChip item={workItem} />
        </div>
      </section>}
      <QueuedItems plan={plan} />
    </div>}
    {folded && <div className="max-h-24 overflow-auto px-4"><QueuedItems plan={plan} /></div>}
    <div className="grid grid-cols-3 gap-3 border-t border-line bg-canvas-2 px-4 pt-3 text-xs text-ink-2">
      {steps.map(({ name, segments }) => {
        const total = segments.length || 1;
        const completed = segments.length ? segments.filter(Boolean).length : 1;
        return <span key={name} aria-current={name === current ? "step" : undefined} className={name === current ? "font-semibold text-ink" : undefined}>
          {name}<span aria-hidden className="mt-1.5 flex gap-0.5">
            {Array.from({ length: total }, (_, i) => <i key={i} className={`h-1 flex-1 rounded-sm ${i < completed ? "bg-pass" : "bg-line-strong"}`} />)}
          </span>
        </span>;
      })}
    </div>
    {children}
  </aside>;
}
