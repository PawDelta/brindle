/* "What routing aims for", the top of Agents (design/routing-goals, option A
   as the owner approved it): a point in a triangle whose corners are
   Cheaper, Faster and First try. The nearer a corner, the more that goal
   counts in every pick: workers, reviews, the supervisor, how much runs at
   once.

   Beside it, the setup at that point against one model doing everything
   (GET /routing/goal's preview: Fable measured, the others estimated from
   list price), the person's recent work at that point, and what each role
   runs. Dragging (or the arrow keys) moves the aim at once; the PUT goes
   when the point rests, and a reply for an aim the person has since moved
   away from is dropped. An org that locks the goal shows it, unmovable. */

import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { DaemonClient, RoutingGoalDTO, RoutingGoalFigures, RoutingGoalReference, RoutingWeights } from "../../api/client";
import { plain } from "../../api/wording";
import { alert, card, h2, label, mono, quiet } from "../../components/ui";
import { BALANCED, BL, BR, H, TOP, W, fromPoint, pct, stepBy, toPoint, weightsKey } from "./goal";
import { Mark } from "./Mark";

/* How long the point rests before its PUT goes. */
export const DEBOUNCE_MS = 250;

export interface RoutingGoalProps {
  client: Pick<DaemonClient, "routingGoal" | "putRoutingGoal">;
  repo: string;
}

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));
const usd = (n: number) => `$${n.toFixed(2)}`;

/* A setup's diamond: solid when measured, hollow and dashed when estimated. */
function Diamond({ x, y, fill, estimated }: { x: number; y: number; fill: string; estimated?: boolean }) {
  return estimated ? (
    <rect x={x - 5} y={y - 5} width={10} height={10} transform={`rotate(45 ${x} ${y})`} fill="var(--canvas-2)" stroke="var(--ink-3)" strokeWidth={1.5} strokeDasharray="2 2" />
  ) : (
    <rect x={x - 5} y={y - 5} width={10} height={10} transform={`rotate(45 ${x} ${y})`} fill={fill} />
  );
}

function Reference({ r }: { r: RoutingGoalReference }) {
  if (!r.point) return null;
  const [x, y] = toPoint(r.point);
  // Right of centre the label sits beside the marker, else under it.
  const right = x > TOP[0] + 10;
  return (
    <g data-reference={r.key} data-measured={r.measured}>
      <Diamond x={x} y={y} fill="var(--ink-2)" estimated={!r.measured} />
      <text x={right ? x + 12 : x} y={right ? y - 6 : y + 22} fontSize="11" fill={r.measured ? "var(--ink)" : "var(--ink-3)"} textAnchor={right ? "start" : "middle"}>
        {r.name}
      </text>
    </g>
  );
}

function Triangle({ weights, landed, references, locked, onMove, onKey }: {
  weights: RoutingWeights;
  landed: RoutingWeights | null;
  references: RoutingGoalReference[];
  locked: boolean;
  onMove(w: RoutingWeights): void;
  onKey(e: KeyboardEvent<SVGSVGElement>): void;
}) {
  const dragging = useRef(false);
  const [x, y] = toPoint(weights);
  const at = (e: PointerEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const px = box.width ? ((e.clientX - box.left) * W) / box.width : e.clientX;
    const py = box.height ? ((e.clientY - box.top) * H) / box.height : e.clientY;
    onMove(fromPoint(px, py));
  };
  const pt = (c: number, s: number, f: number) => toPoint({ cost: c, speed: s, first_try: f });
  const line = (a: number[], b: number[], k: string) => <line key={k} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke="var(--line)" />;
  const [lx, ly] = landed ? toPoint(landed) : [0, 0];
  return (
    <svg
      width={W}
      height={H}
      viewBox={`0 0 ${W} ${H}`}
      role="slider"
      aria-label="Goal mix"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(weights.cost * 100)}
      aria-valuetext={`cost ${pct(weights.cost)}, speed ${pct(weights.speed)}, first try ${pct(weights.first_try)}`}
      aria-disabled={locked || undefined}
      tabIndex={locked ? -1 : 0}
      data-weights={weightsKey(weights)}
      className={`touch-none select-none rounded-md outline-none focus-visible:ring-2 focus-visible:ring-branch ${locked ? "opacity-60" : "cursor-pointer"}`}
      onKeyDown={onKey}
      onPointerDown={(e) => {
        if (locked) return;
        dragging.current = true;
        e.currentTarget.setPointerCapture?.(e.pointerId);
        at(e);
      }}
      onPointerMove={(e) => {
        if (dragging.current) at(e);
      }}
      onPointerUp={(e) => {
        dragging.current = false;
        e.currentTarget.releasePointerCapture?.(e.pointerId);
      }}
      onPointerCancel={() => (dragging.current = false)}
    >
      <polygon points={[TOP, BL, BR].map((q) => q.join(",")).join(" ")} fill="var(--canvas-2)" stroke="var(--line-strong)" />
      {[0.25, 0.5, 0.75].map((g) => [
        line(pt(g, 1 - g, 0), pt(g, 0, 1 - g), `c${g}`),
        line(pt(1 - g, g, 0), pt(0, g, 1 - g), `s${g}`),
        line(pt(1 - g, 0, g), pt(0, 1 - g, g), `f${g}`),
      ])}
      {references.filter((r) => r.key !== "you").map((r) => (
        <Reference key={r.key} r={r} />
      ))}
      {landed && (
        <g data-marker="landed">
          <Diamond x={lx} y={ly} fill="var(--branch)" />
          <text x={lx - 10} y={ly - 8} fontSize="11" fill="var(--ink)" textAnchor="end">
            Your routing
          </text>
        </g>
      )}
      <g data-marker="aim">
        <circle cx={x} cy={y} r={14} fill="var(--branch-soft)" />
        <circle cx={x} cy={y} r={7} fill="var(--branch)" stroke="var(--canvas)" strokeWidth={2} />
        <text x={x - 14} y={y + 4} fontSize="11" fill="var(--ink)" fontWeight={600} textAnchor="end">
          Your aim
        </text>
      </g>
      <text x={TOP[0]} y={TOP[1] - 12} fontSize="12" fill="var(--ink)" fontWeight={600} textAnchor="middle">
        First try
      </text>
      <text x={BL[0]} y={BL[1] + 20} fontSize="12" fill="var(--ink)" fontWeight={600} textAnchor="start">
        Cheaper
      </text>
      <text x={BR[0]} y={BR[1] + 20} fontSize="12" fill="var(--ink)" fontWeight={600} textAnchor="end">
        Faster
      </text>
    </svg>
  );
}

function Legend() {
  return (
    <div className="flex w-[400px] max-w-full flex-col gap-1 text-xs text-ink-3">
      <span>Drag your point, or use the arrow keys. The nearer a corner, the more that goal counts.</span>
      <span className="flex flex-wrap items-center gap-1.5">
        <svg width="12" height="12" aria-hidden>
          <rect x="2" y="2" width="8" height="8" transform="rotate(45 6 6)" fill="var(--ink-2)" />
        </svg>
        measured on brindle's benchmark
        <svg width="12" height="12" className="ml-s1" aria-hidden>
          <rect x="2" y="2" width="8" height="8" transform="rotate(45 6 6)" fill="none" stroke="var(--ink-3)" strokeWidth="1.5" strokeDasharray="2 2" />
        </svg>
        estimated from list price
      </span>
      <span>Fable alone sits in the middle. Anything else leans toward each goal it beats Fable on, by how many times better it does there: twice as cheap and otherwise equal sits at Cheaper 67%.</span>
      <span>Your aim (●) is what you ask for. Your routing (◆) is where what it delivered lands.</span>
    </div>
  );
}

function Comparison({ references, task, onTask }: { references: RoutingGoalReference[]; task: string | null; onTask(t: string): void }) {
  const tasks = (references.find((r) => r.key === "you") ?? references[0])?.tasks ?? [];
  const figures = (r: RoutingGoalReference): RoutingGoalFigures | undefined => r.tasks.find((t) => t.task === task);
  return (
    <>
      <div className="flex flex-wrap items-baseline justify-between gap-s1">
        <span className={label}>You against one model doing everything</span>
        {tasks.length > 1 && (
          <span role="group" aria-label="Benchmark task" className="inline-flex flex-wrap gap-0.5">
            {tasks.map((t) => (
              <button
                key={t.task}
                type="button"
                aria-pressed={t.task === task}
                data-task={t.task}
                className={`rounded-md px-s1 py-0.5 text-sm ${t.task === task ? "bg-surface-2 font-medium text-ink" : "text-ink-2 hover:text-ink"}`}
                onClick={() => onTask(t.task)}
              >
                {t.label}
              </button>
            ))}
          </span>
        )}
      </div>
      <table className="w-full text-sm" aria-label="Comparison" data-task={task ?? undefined}>
        <thead>
          <tr className="text-left text-xs text-ink-3">
            <th className="py-1 font-normal">Per task, on brindle's benchmark</th>
            <th className="py-1 text-right font-normal">$ a task</th>
            <th className="py-1 text-right font-normal">min to merge</th>
            <th className="py-1 text-right font-normal">merged on first review</th>
          </tr>
        </thead>
        <tbody>
          {references.map((r) => {
            const you = r.key === "you";
            const f = figures(r);
            const estimated = !r.measured;
            return (
              <tr key={r.key} data-reference={r.key} className={`border-t border-line ${you ? "bg-branch-soft" : ""}`}>
                <td className="py-1.5 pl-1">
                  <span className={you ? "font-semibold" : "font-medium"}>{you ? "You, at this point" : `${r.name} alone`}</span>
                  {f?.basis && <span className={`ml-s1 text-xs text-ink-3 ${estimated ? "italic" : ""}`}>{f.basis}</span>}
                </td>
                <td className={`py-1.5 text-right tabular-nums ${estimated ? "text-ink-2" : ""}`}>{f?.usd ?? "—"}</td>
                <td className="py-1.5 text-right tabular-nums">{f?.minutes ?? "—"}</td>
                <td className="py-1.5 pr-1 text-right tabular-nums">{f?.first_try ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

const ROW = "grid grid-cols-[8rem_minmax(0,15rem)_minmax(0,1fr)] items-baseline gap-x-s2 border-t border-line px-s2 py-1.5 text-sm";

function Roles({ roles }: { roles: RoutingGoalDTO["preview"]["roles"] }) {
  return (
    <ul className="flex flex-col" aria-label="What changes">
      {roles.map((r) => {
        const changed = !!r.was && r.was !== r.value;
        return (
          <li key={r.key} className={ROW} data-role={r.key} data-changed={changed}>
            <span className="font-medium">{r.label}</span>
            <span className="flex min-w-0 items-center gap-1">
              {r.provider && <Mark provider={r.provider} />}
              <span className={changed ? "font-medium" : "text-ink-2"}>{r.value}</span>
              {changed && <span className="truncate text-xs text-ink-3 line-through">{r.was}</span>}
            </span>
            <span className="truncate text-xs text-ink-2" title={changed ? (r.note ?? undefined) : undefined}>
              {changed ? r.note : <span className="text-ink-3">unchanged</span>}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function RoutingGoal({ client, repo }: RoutingGoalProps) {
  const [goal, setGoal] = useState<RoutingGoalDTO | null>(null);
  const [weights, setWeights] = useState<RoutingWeights | null>(null);
  const [task, setTask] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  /* The aim the newest change asked for: a reply for any other is stale. */
  const latest = useRef<string | null>(null);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    let live = true;
    client.routingGoal(repo).then(
      (g) => {
        // A move made while this loaded wins over what it read.
        if (!live || latest.current !== null) return;
        setGoal(g);
        setWeights(g.weights);
      },
      () => {}, // an engine without the route: the section stays out
    );
    return () => {
      live = false;
      window.clearTimeout(timer.current);
    };
  }, [client, repo]);

  const send = useCallback(
    (w: RoutingWeights, scope: RoutingGoalDTO["scope"]) => {
      const asked = weightsKey(w);
      client.putRoutingGoal({ repo, weights: w, scope }).then(
        (g) => {
          if (latest.current !== asked) return;
          setGoal(g);
          setWeights(g.preview.aim ?? g.weights);
          setSaveError(null);
        },
        (e: unknown) => {
          if (latest.current === asked) setSaveError(message(e));
        },
      );
    },
    [client, repo],
  );

  if (!goal || !weights) return null;
  const locked = goal.locked;
  const preview = goal.preview;
  const shownTask = task ?? (preview.references.find((r) => r.key === "you") ?? preview.references[0])?.tasks[0]?.task ?? null;
  const current = weightsKey(preview.aim ?? goal.weights) === weightsKey(weights);

  /* Moves the aim now; the PUT goes once it rests (`now`: at once). */
  const change = (w: RoutingWeights, now = false) => {
    if (locked) return;
    latest.current = weightsKey(w);
    setWeights(w);
    window.clearTimeout(timer.current);
    if (now) send(w, goal.scope);
    else timer.current = window.setTimeout(() => send(w, goal.scope), DEBOUNCE_MS);
  };

  const onKey = (e: KeyboardEvent<SVGSVGElement>) => {
    if (locked) return;
    const next = stepBy(weights, e.key);
    if (!next) return;
    e.preventDefault();
    change(next);
  };

  const you = preview.you;
  const project = goal.scope === "user" ? "you, in every project" : "this project";

  return (
    <section className={`${card} flex flex-col gap-s2 py-s2`} aria-labelledby="goal-title" data-testid="routing-goal" data-state={locked ? "locked" : "editable"}>
      <header className="flex flex-wrap items-baseline justify-between gap-x-s2 px-s2">
        <div className="flex flex-wrap items-baseline gap-x-s2">
          <h2 id="goal-title" className={h2}>
            What routing aims for
          </h2>
          <span className="text-sm text-ink-2">Every pick follows it: workers, reviews, the supervisor and how much runs at once.</span>
        </div>
        <button type="button" className={quiet} disabled={locked} data-action="reset" onClick={() => change(goal.balanced ?? BALANCED, true)}>
          Reset to Balanced
        </button>
      </header>
      {saveError && (
        <p role="alert" className={`${alert} mx-s2`} data-state="save-error">
          Could not save: {saveError}
        </p>
      )}
      <div className="grid gap-s3 px-s2 lg:grid-cols-[430px_minmax(0,1fr)]">
        <div className="flex flex-col items-center gap-s1">
          <Triangle weights={weights} landed={preview.landed} references={preview.references} locked={locked} onMove={(w) => change(w)} onKey={onKey} />
          <div className={`${mono} text-ink-2`} data-testid="goal-weights">
            cost {pct(weights.cost)} · speed {pct(weights.speed)} · first try {pct(weights.first_try)}
          </div>
          <Legend />
        </div>
        <div className={`flex min-w-0 flex-col gap-s2 transition-opacity ${current ? "" : "opacity-60"}`} data-testid="goal-preview" data-state={current ? "current" : "pending"} data-preview-for={weightsKey(preview.aim ?? goal.weights)}>
          <Comparison references={preview.references} task={shownTask} onTask={setTask} />
          {(preview.notes?.length ?? 0) > 0 && (
            <div className="flex flex-col gap-1 text-xs text-ink-3">
              {preview.notes!.map((n) => (
                <span key={n}>{n}</span>
              ))}
            </div>
          )}
          {you && (
            <div className="flex flex-wrap items-baseline gap-x-s2 gap-y-1 border-t border-line pt-s1 text-sm" data-testid="goal-you">
              <span className={label}>Your recent work, at this point</span>
              <span>
                <span className="font-semibold tabular-nums">{usd(you.usd)}</span> <span className="text-xs text-ink-2">a task</span>
              </span>
              <span>
                <span className="font-semibold tabular-nums">{Math.round(you.minutes)}</span> <span className="text-xs text-ink-2">min</span>
              </span>
              <span>
                <span className="font-semibold tabular-nums">{pct(you.first_try)}</span> <span className="text-xs text-ink-2">on first review</span>
              </span>
              {you.today && (
                <span className="text-xs text-ink-3">
                  estimate, from {usd(you.today.usd)} · {Math.round(you.today.minutes)} min · {pct(you.today.first_try)} today
                </span>
              )}
            </div>
          )}
          {preview.roles.length > 0 && (
            <div className="-mx-s2 flex flex-col">
              <div className={`${label} px-s2 pb-1`}>What changes</div>
              <Roles roles={preview.roles} />
            </div>
          )}
        </div>
      </div>
      {preview.basis && <p className="px-s2 text-xs text-ink-3">{preview.basis}</p>}
      <div className="flex flex-wrap items-center gap-x-s2 gap-y-1 px-s2 text-xs text-ink-3">
        <span>
          Set for <span className="text-ink-2">{project}</span>
        </span>
        {(locked || goal.lock_reason) && (
          <>
            <span aria-hidden>·</span>
            <span data-testid="goal-lock">🔒 {goal.lock_reason ?? "Your org sets what routing aims for"}</span>
          </>
        )}
      </div>
    </section>
  );
}
