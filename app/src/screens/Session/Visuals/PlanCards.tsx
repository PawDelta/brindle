/* The design doc's parts that aren't text (PlanReader.tsx): a decision's alternatives as a table
   to pick from, with who voted for each, and the split's tasks as cards, which after approval
   are the live board. */

import type { Plan } from "../../../api/client";
import type { PlanDecisionDTO, PlanWorkerDTO } from "../../../api/schema";
import { alert, chip, pill } from "../../../components/ui";
import { peerClass } from "./docAwareness";

const card = "flex min-w-0 flex-col gap-2 rounded-lg border bg-canvas p-3 text-[12.5px] text-ink-2";
const cardTitle = "text-[13.5px] font-semibold leading-5 text-ink";
/* Cards share a row evenly, and a few of them don't stretch across a wide window. */
const CARD_MAX = 410;
const fileChip = "rounded-sm border border-line bg-surface-2 px-1 font-mono text-[10.5px] leading-[18px] text-ink-2";
/* A chip in a tone: the reader's header, once approved. */
export const passChip = `${chip} whitespace-nowrap border border-pass/40 bg-pass-soft text-pass`;

/* Who wrote or voted: the engine's "person" is the one reading, "supervisor" the session's. */
export const personName = (who: string) => (who === "person" ? "You" : who === "supervisor" ? "Supervisor" : who);

/* Two letters for a person: "Maya Chen" → MC, "dana.okafor@…" → DO, "ravi@…" → RA. */
export const initials = (who: string) => {
  const words = who.split("@")[0].split(/[\s._-]+/).filter(Boolean);
  return (words.length > 1 ? `${words[0][0]}${words[1][0]}` : (words[0] ?? who).slice(0, 2)).toUpperCase();
};

/* Everyone has a colour of their own, the same as their caret's in a live doc (docAwareness.ts). */
export function Avatar({ who, small, ring }: { who: string; small?: boolean; ring?: "wait" | "done" | "dim" }) {
  const colour = who === "person" ? "var(--branch)" : who === "supervisor" ? "var(--pass)" : `var(--group-${Number(peerClass(who).slice("loro-peer-".length)) + 2})`;
  const rings = ring === "wait" ? "shadow-[0_0_0_1.5px_var(--canvas),0_0_0_3px_var(--sap)]" : ring === "done" ? "shadow-[0_0_0_1.5px_var(--canvas),0_0_0_3px_var(--pass)]" : ring === "dim" ? "opacity-45" : "";
  return (
    <span title={personName(who)} style={{ backgroundColor: colour }} className={`inline-grid flex-none place-items-center rounded-full font-mono font-semibold leading-none text-canvas ${small ? "size-4 text-[7.5px]" : "size-5 text-[9px]"} ${rings}`}>
      {who === "person" ? "Y" : who === "supervisor" ? "S" : initials(who)}
    </span>
  );
}

/* Files more than one worker of the split expects to change, with the workers that do. */
export function overlaps(plan: Plan): Map<string, string[]> {
  const by = new Map<string, string[]>();
  for (const w of plan.split) for (const f of new Set(w.files ?? [])) by.set(f, [...(by.get(f) ?? []), w.id]);
  return new Map([...by].filter(([, ws]) => ws.length > 1));
}

const optionRow = "grid grid-cols-[14px_minmax(0,1.15fr)_minmax(0,1.5fr)_minmax(0,.8fr)_60px] items-start gap-3 border-b px-2.5";

/* A decision's alternatives as a table: one row each, the picked one marked, the supervisor's pick
   "recommended", the trade-offs as + and − lines, the files it would touch, and who voted for it.
   A row is a radio: picking one is the person's draft until they confirm it in the bottom bar. */
export function Decision({ decision: d, chosen, open, onPick }: { decision: PlanDecisionDTO; chosen: string | null; open: boolean; onPick(option: string): void }) {
  return (
    <div role="radiogroup" aria-label={d.title} className="-mx-2.5 grid font-sans text-[13px] leading-normal">
      <div aria-hidden className={`${optionRow} border-line-strong pb-1.5 pt-1 text-[11px] font-medium uppercase tracking-[0.08em] text-ink-3`}>
        <span />
        <span>Option</span>
        <span>For and against</span>
        <span>Touches</span>
        <span>Votes</span>
      </div>
      {d.options.map((o) => {
        const picked = chosen === o.id;
        const votes = d.votes?.[o.id] ?? [];
        return (
          <div key={o.id} role="radio" aria-checked={picked} aria-label={o.label} aria-disabled={!open} tabIndex={open ? 0 : -1}
            className={`${optionRow} py-2.5 text-ink-2 ${picked ? "rounded-md border-transparent bg-branch-soft" : "border-line"} ${open ? "cursor-pointer" : ""}`}
            onClick={() => open && onPick(o.id)}
            onKeyDown={(e) => {
              if (!open || (e.key !== " " && e.key !== "Enter")) return;
              e.preventDefault();
              onPick(o.id);
            }}>
            <span aria-hidden className={`mt-1 size-3 rounded-full ${picked ? "border-4 border-branch" : "border-[1.5px] border-line-strong"}`} />
            <span className="grid justify-items-start gap-0.5">
              <b className="font-medium text-ink">{o.label}</b>
              {o.id === d.mine && <span className={`${pill} text-pass`}>recommended</span>}
              {o.detail && <span>{o.detail}</span>}
            </span>
            <span className="grid gap-px">
              {o.pros?.map((t, i) => (
                <span key={`p${i}`} data-tradeoff="pro" className="grid grid-cols-[auto_1fr] gap-x-1.5">
                  <span className="font-mono text-pass">+</span>
                  {t}
                </span>
              ))}
              {o.cons?.map((t, i) => (
                <span key={`c${i}`} data-tradeoff="con" className="grid grid-cols-[auto_1fr] gap-x-1.5">
                  <span className="font-mono text-sap">−</span>
                  {t}
                </span>
              ))}
            </span>
            <span className="flex flex-wrap gap-1">
              {o.touches?.map((f) => <code key={f} className="rounded-sm bg-surface-2 px-1 font-mono text-[11.5px] text-ink-2">{f}</code>)}
            </span>
            <span className="flex min-h-5 items-center gap-1 text-ink-3" data-votes={votes.length}>
              {votes.length ? votes.map((v) => <Avatar key={v} who={v} small />) : "none"}
            </span>
          </div>
        );
      })}
    </div>
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
