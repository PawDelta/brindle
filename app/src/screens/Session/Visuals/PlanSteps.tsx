/* The plan reader's left column (PlanReader.tsx): every step with its number, a tick once it is
   settled and what was answered under its name, and at the foot how many are settled, as a bar. */

export interface StepRow {
  id: string;
  title: string;
  /* What the person answered there (the option picked, "commented"), or what it holds. */
  sub?: string;
  done: boolean;
}

export function PlanSteps({ rows, at, settled, total, onGo }: { rows: StepRow[]; at: number; settled: number; total: number; onGo(step: number): void }) {
  return (
    <nav aria-label="Plan steps" className="flex w-[200px] shrink-0 flex-col border-r border-line bg-canvas-2 px-2.5 py-3.5">
      <ol className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-auto">
        {rows.map((r, i) => {
          const current = i === at;
          return (
            <li key={r.id}>
              <button type="button" aria-current={current ? "step" : undefined} onClick={() => onGo(i)}
                className={`grid w-full grid-cols-[18px_1fr] items-start gap-2 rounded-md px-2 py-[7px] text-left text-[12.5px] leading-[18px] transition-colors focus-visible:-outline-offset-2 ${current ? "bg-branch-soft font-semibold text-ink" : `hover:bg-surface-2 ${r.done ? "text-ink-2" : "text-ink-3"}`}`}>
                <span aria-hidden className={`grid size-[18px] place-items-center rounded-full border font-mono text-[10.5px] font-normal leading-none ${current ? "border-branch bg-branch text-on-branch" : r.done ? "border-pass bg-pass text-canvas" : "border-line-strong"}`}>
                  {r.done ? "✓" : i + 1}
                </span>
                <span className="min-w-0">
                  <span className="line-clamp-2">{r.title}</span>
                  {r.sub && <span className="block truncate text-[11.5px] font-normal text-ink-3">{r.sub}</span>}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      <div className="mt-2.5 border-t border-line px-2 pt-2.5 font-mono text-[11.5px] text-ink-3" data-testid="plan-progress">
        {`${settled} of ${total} settled`}
        <div role="progressbar" aria-label="Steps settled" aria-valuemin={0} aria-valuemax={total} aria-valuenow={settled} className="mt-1.5 h-1 overflow-hidden rounded-sm bg-line">
          <span className="block h-full bg-pass transition-[width]" style={{ width: `${total ? (settled / total) * 100 : 100}%` }} />
        </div>
      </div>
    </nav>
  );
}
