/* The plan reader's cards (PlanReader.tsx): a decision's alternatives to pick among, and the
   split's tasks, which after approval are the live board. Both share one card and one file chip. */

import type { Plan } from "../../../api/client";
import type { PlanDecisionDTO, PlanWorkerDTO } from "../../../api/schema";
import { alert, chip } from "../../../components/ui";

const card = "flex min-w-0 flex-col gap-2 rounded-lg border bg-canvas p-3 text-[12.5px] text-ink-2";
const cardTitle = "text-[13.5px] font-semibold leading-5 text-ink";
/* Cards share a row evenly, and a few of them don't stretch across a wide window. */
const CARD_MAX = 410;
const fileChip = "rounded-sm border border-line bg-surface-2 px-1 font-mono text-[10.5px] leading-[18px] text-ink-2";
/* A chip in a tone: the supervisor's pick, and the reader's header (approved, the revision). */
export const passChip = `${chip} whitespace-nowrap border border-pass/40 bg-pass-soft text-pass`;
export const branchChip = `${chip} whitespace-nowrap border border-branch/40 bg-branch-soft text-branch`;

/* Files more than one worker of the split expects to change, with the workers that do. */
export function overlaps(plan: Plan): Map<string, string[]> {
  const by = new Map<string, string[]>();
  for (const w of plan.split) for (const f of new Set(w.files ?? [])) by.set(f, [...(by.get(f) ?? []), w.id]);
  return new Map([...by].filter(([, ws]) => ws.length > 1));
}

/* A decision's alternatives as cards: the supervisor's pick marked Recommended, the trade-offs as
   + and − lines, the chosen one ringed, and under the cards the files it would touch. */
export function Decision({ decision: d, chosen, open, onPick }: { decision: PlanDecisionDTO; chosen: string | null; open: boolean; onPick(option: string): void }) {
  // The picked alternative's files; every alternative's while none is picked, or none can be any more.
  const touching = d.options.filter((o) => o.touches?.length && (!open || chosen == null || o.id === chosen));
  return (
    <>
      {d.challenge && <p className="max-w-[72ch] border-l-2 border-line-strong pl-s1 text-[12.5px] text-ink-2">{d.challenge}</p>}
      <div role="radiogroup" aria-label={d.title} className="grid grid-cols-[repeat(auto-fit,minmax(210px,1fr))] gap-2.5" style={{ maxWidth: d.options.length * CARD_MAX }}>
        {d.options.map((o) => (
          <button key={o.id} type="button" role="radio" aria-checked={chosen === o.id} aria-label={o.label} disabled={!open}
            className={`${card} text-left transition-colors enabled:cursor-pointer ${chosen === o.id ? "border-branch ring-[3px] ring-branch-soft" : "border-line-strong enabled:hover:border-ink-3"}`}
            onClick={() => onPick(o.id)}>
            <span className={`${cardTitle} flex flex-wrap items-center gap-1.5`}>
              {o.label}
              {o.id === d.mine && <span className={passChip}>Recommended</span>}
            </span>
            {o.detail && <span>{o.detail}</span>}
            {(!!o.pros?.length || !!o.cons?.length) && (
              <span className="flex flex-col gap-[3px] text-xs">
                {o.pros?.map((t, i) => (
                  <span key={`p${i}`} data-tradeoff="pro" className="grid grid-cols-[auto_1fr] gap-x-1.5">
                    <span className="font-mono text-pass">+ </span>
                    {t}
                  </span>
                ))}
                {o.cons?.map((t, i) => (
                  <span key={`c${i}`} data-tradeoff="con" className="grid grid-cols-[auto_1fr] gap-x-1.5">
                    <span className="font-mono text-sap">− </span>
                    {t}
                  </span>
                ))}
              </span>
            )}
          </button>
        ))}
      </div>
      {touching.length > 0 && (
        <div className="flex flex-col gap-1.5 border-t border-line pt-2.5">
          {touching.map((o) => (
            <p key={o.id} className="flex flex-wrap items-center gap-1.5 text-xs text-ink-3" data-testid={`touches-${o.id}`}>
              <span>
                Touches for <span className="font-medium text-ink-2">{o.label}</span>
              </span>
              {o.touches!.map((f) => (
                <code key={f} className={fileChip}>{f}</code>
              ))}
            </p>
          ))}
        </div>
      )}
    </>
  );
}

/* The proving test as a status chip: grey until it runs, red failing, green passing. */
const proof: Record<NonNullable<PlanWorkerDTO["test_state"]>, { tone: string; mark: string }> = {
  "not run": { tone: "bg-surface-2 text-ink-3", mark: "○" },
  failing: { tone: "bg-fail-soft text-fail", mark: "✗" },
  passing: { tone: "bg-pass-soft text-pass", mark: "✓" },
};
const stateTone: Partial<Record<NonNullable<PlanWorkerDTO["state"]>, string>> = { merged: "text-pass", cancelled: "text-fail" };

/* The split as task cards: each worker, the files it changes (a file another worker changes too
   is flagged) and the one test that proves it. Approved, the cards are live: where each worker
   is, and its test's last result. */
export function Tasks({ plan, shared }: { plan: Plan; shared: Map<string, string[]> }) {
  const live = plan.state === "approved";
  return (
    <>
      {shared.size > 0 && (
        <p role="alert" className={alert} data-testid="split-overlaps">
          {[...shared].map(([f, ws]) => `${f} is in ${ws.join(" and ")}`).join("; ")}. Workers that change the same file can conflict: order them with after, or redirect.
        </p>
      )}
      <ol aria-label="Proposed split" className="grid grid-cols-[repeat(auto-fit,minmax(230px,1fr))] gap-2.5" style={{ maxWidth: plan.split.length * CARD_MAX }}>
        {plan.split.map((w) => {
          const tested = w.test_state ?? "not run";
          const state = w.state ?? "queued";
          return (
            <li key={w.id} className={`${card} ${tested === "passing" ? "border-pass/45" : "border-line-strong"}`} data-testid={`task-${w.id}`}>
              <h4 className={cardTitle}>{w.title}</h4>
              <p className="font-mono text-[11px] text-ink-3">
                {`worker ${w.id}`}
                {w.after?.length ? ` · after ${w.after.join(", ")}` : ""}
                {live && " · "}
                {live && <span className={stateTone[state] ?? "text-ink-2"}>{state}</span>}
              </p>
              {live && (w.task_id || w.agent_id) && (
                <p className="-mt-1 font-mono text-[10.5px] text-ink-3">
                  {[w.task_id && `task ${w.task_id}`, w.agent_id && `worker ${w.agent_id}`].filter(Boolean).join(" · ")}
                </p>
              )}
              {!!w.files?.length && (
                <span className="flex flex-wrap gap-1">
                  {w.files.map((f) => {
                    const others = shared.get(f)?.filter((id) => id !== w.id);
                    return others ? (
                      <code key={f} className="rounded-sm border border-sap/60 bg-sap-soft px-1 font-mono text-[10.5px] leading-[18px] text-sap" title={`also in ${others.join(", ")}`} data-overlap="true">
                        {`${f} ⚠ also ${others.join(", ")}`}
                      </code>
                    ) : (
                      <code key={f} className={fileChip}>{f}</code>
                    );
                  })}
                </span>
              )}
              <p data-test-state={tested} className={`mt-auto flex items-start gap-1.5 rounded-md px-[7px] py-1 font-mono text-[11px] ${proof[tested].tone}`}>
                {w.test && <span aria-hidden>{proof[tested].mark}</span>}
                <span className="min-w-0 break-words">
                  {w.test ? `proves it: ${w.test}` : "no test named"}
                  {live && ` · ${tested}`}
                </span>
              </p>
            </li>
          );
        })}
      </ol>
    </>
  );
}
