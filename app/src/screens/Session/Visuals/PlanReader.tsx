/* A planning doc (brindle/plans.py), opened over the whole window like any visual. Unlike a visual
   it isn't the agent's HTML: brindle renders its markdown itself (Knowledge's Markdown: elements
   only, no raw HTML, no fetched images), so it needs no frame, and every control on it is the
   app's own. The person reads it one step at a time (the overview, each decision, each ## section,
   the split, then a summary), with the supervisor's restating of their ask above every step for
   them to correct. They pick among a decision's alternatives (cards with trade-offs, the files each
   touches and the supervisor's pick marked Recommended), comment on any step, then Approve (the
   session's queued workers start) or Redirect (their picks and comments go back to the supervisor,
   who publishes a new revision). Drafts belong to the revision on screen: a new revision is a
   fresh read, and a verdict on an older one is a 409. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DaemonError, type Plan, type VisualDTO } from "../../../api/client";
import type { PlanDecisionDTO, PlanSectionDTO } from "../../../api/schema";
import { LoadingButton } from "../../../components/LoadingButton";
import { button, chip, field, label, main, mono, quiet } from "../../../components/ui";
import { Markdown } from "../../Knowledge/Markdown";
import type { ExportClient } from "../../Knowledge/ExportMenu";
import type { PlansClient, VisualsClient } from "./client";
import { BackButton, KeepButton, message, useEscBack } from "./chrome";
import { ExportButton } from "./ExportButton";

type Client = VisualsClient & PlansClient & Partial<ExportClient>;

/* How far along a plan is: its revision, then decided or not. Revisions only grow and a decided
   revision never reopens, so a response with less progress than the one on screen is stale. */
const progress = (p: Plan) => p.rev * 2 + (p.state === "open" ? 0 : 1);

export function PlanReader({ client, visual, repo, onBack, asking, onChanged }: {
  client: Client;
  visual: VisualDTO;
  repo?: string;
  onBack(): void;
  asking?: boolean;
  /* The engine's row after a Keep, ahead of the bus. */
  onChanged(visual: VisualDTO): void;
}) {
  useEscBack(onBack);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  // Read again whenever the session's row says the plan changed (a revision, a verdict).
  const changed = `${visual.id}:${visual.rev}:${visual.answer?.at ?? ""}`;
  const seq = useRef(0);
  const selected = useRef(visual.id);
  selected.current = visual.id;
  // Reads and verdict replies can land in any order: the plan on screen only ever moves forward
  // (a newer revision, or this revision decided), and only for the plan still open here.
  const accept = useCallback((p: Plan) => {
    if (p.id !== selected.current) return;
    setPlan((now) => (now && now.id === p.id && progress(now) > progress(p) ? now : p));
  }, []);
  useEffect(() => {
    const mine = ++seq.current;
    client.plan(visual.id).then(
      (p) => {
        accept(p);
        if (mine === seq.current) setError(null);
      },
      (e) => {
        if (mine === seq.current) setError(message(e));
      },
    );
  }, [client, visual.id, changed, accept]);
  const shown = plan && plan.id === visual.id ? plan : null;
  const exportClient = client.exportVisual && client.exportDoc && client.docPolicy && client.docExportConfig ? (client as ExportClient) : undefined;
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="plan-reader">
      <header className="flex flex-wrap items-center gap-x-2 gap-y-1 px-s2 pt-2.5">
        <BackButton onBack={onBack} asking={asking} />
        <span className="mx-1 h-5 w-px bg-line" aria-hidden />
        <h2 className="min-w-0 flex-1 truncate text-[15px] font-semibold tracking-[-0.01em]">{visual.title}</h2>
        <KeepButton client={client} visual={visual} onKept={onChanged} />
        {exportClient && shown && (
          <ExportButton key={`${shown.id}:${shown.rev}`} client={exportClient} repo={repo} open={exporting} onOpenChange={setExporting} source={{ kind: "plan", id: shown.id, rev: shown.rev, markdown: shown.markdown }} />
        )}
      </header>
      <div className="flex min-h-0 flex-1 flex-col p-s2">
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-line-strong">
          <div className={`${mono} flex items-center justify-between gap-s2 border-b border-line px-s2 py-0.5 text-[11px] text-ink-3`}>
            <span>rendered by brindle from the plan's markdown</span>
            <span className="truncate">{`${visual.id} · r${shown?.rev ?? visual.rev} · ${visual.by}`}</span>
          </div>
          {shown ? <PlanBody key={`${shown.id}:${shown.rev}`} client={client} plan={shown} by={visual.by} onDecided={accept} /> : <p className="p-s3 text-sm text-ink-3">{error ?? "Loading…"}</p>}
        </div>
      </div>
    </div>
  );
}

/* One screen of the reader. Its id is what a comment on it names (brindle/plans.py anchors). */
type Step =
  | { kind: "lede"; id: "lede"; title: string }
  | { kind: "decision"; id: string; title: string; decision: PlanDecisionDTO }
  | { kind: "section"; id: string; title: string; section: PlanSectionDTO }
  | { kind: "split"; id: "split"; title: string }
  | { kind: "summary"; id: "summary"; title: string };

function stepsOf(plan: Plan): Step[] {
  return [
    ...(plan.lede ? [{ kind: "lede", id: "lede", title: "Overview" } as const] : []),
    ...plan.decisions.map((d) => ({ kind: "decision", id: `decision:${d.id}`, title: d.title, decision: d }) as const),
    ...plan.sections.map((s) => ({ kind: "section", id: s.id, title: s.title, section: s }) as const),
    ...(plan.split.length ? [{ kind: "split", id: "split", title: "The split" } as const] : []),
    { kind: "summary", id: "summary", title: "Your verdict" },
  ];
}

/* Files more than one worker of the split expects to change, with the workers that do. */
function overlaps(plan: Plan): Map<string, string[]> {
  const by = new Map<string, string[]>();
  for (const w of plan.split) for (const f of new Set(w.files ?? [])) by.set(f, [...(by.get(f) ?? []), w.id]);
  return new Map([...by].filter(([, ws]) => ws.length > 1));
}

const fileChip = `${mono} rounded-sm border border-line bg-surface-2 px-1 text-[11px] text-ink-2`;

function PlanBody({ client, plan, by, onDecided }: { client: PlansClient; plan: Plan; by: string; onDecided(p: Plan): void }) {
  const [picks, setPicks] = useState<Record<string, string>>({});
  const [comments, setComments] = useState<Record<string, string>>({});
  const [commenting, setCommenting] = useState<Set<string>>(new Set());
  const [note, setNote] = useState("");
  const [sending, setSending] = useState<"approve" | "redirect" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const steps = useMemo(() => stepsOf(plan), [plan]);
  const [at, setAt] = useState(0);
  // Steps the person has moved past or answered; the summary step is the verdict itself.
  const [settled, setSettled] = useState<Set<string>>(new Set());
  const open = plan.state === "open";
  const said = Object.entries(comments).filter(([, t]) => t.trim()).map(([section, text]) => ({ section, text: text.trim() }));
  const differs = plan.decisions.some((d) => picks[d.id] && picks[d.id] !== d.mine);
  const canRedirect = said.length > 0 || !!note.trim() || differs;
  const shared = useMemo(() => overlaps(plan), [plan]);
  const decide = async (approved: boolean) => {
    setSending(approved ? "approve" : "redirect");
    setError(null);
    try {
      onDecided(await client.decidePlan(plan.id, { rev: plan.rev, approved, picks, comments: said, note: note.trim() }));
    } catch (e) {
      setError(e instanceof DaemonError && e.status === 409 ? "The supervisor revised this plan since you opened it, or it was already decided. Read it again." : `Couldn't send: ${message(e)}`);
    } finally {
      setSending(null);
    }
  };
  const settle = (id: string) => setSettled((s) => (s.has(id) ? s : new Set(s).add(id)));
  const go = (to: number) => {
    if (to > at) settle(steps[at].id);
    setAt(Math.max(0, Math.min(steps.length - 1, to)));
  };
  // Decided: what the person sent; open: their draft; else the supervisor's pick.
  const chosen = (d: PlanDecisionDTO) => (open ? picks[d.id] : plan.verdict?.picks?.[d.id]) ?? d.mine ?? null;
  const reading = steps.filter((s) => s.kind !== "summary");
  const done = open ? reading.filter((s) => settled.has(s.id)).length : reading.length;
  const step = steps[at];
  // What each comment anchor is called on screen.
  const titles = new Map<string, string>([...steps.map((s): [string, string] => [s.id, s.title]), ["ask", "What I understood"]]);

  const commentBox = (id: string, title: string) =>
    open && commenting.has(id) ? (
      <textarea className={`${field} min-h-16`} aria-label={`Comment on ${title}`} placeholder="What should change here?" autoFocus
        value={comments[id] ?? ""} onChange={(e) => setComments((c) => ({ ...c, [id]: e.target.value }))} />
    ) : null;
  const commentButton = (id: string, text = "Comment") =>
    open && !commenting.has(id) ? (
      <button type="button" className={`${quiet} text-xs`} onClick={() => setCommenting((c) => new Set(c).add(id))}>
        {text}
      </button>
    ) : null;

  return (
    <>
      <div className="flex min-h-0 flex-1">
        <nav aria-label="Plan steps" className="flex w-48 shrink-0 flex-col gap-0.5 overflow-auto border-r border-line bg-canvas-2 px-s1 py-s2">
          {steps.map((s, i) => {
            const isDone = s.kind !== "summary" && (!open || settled.has(s.id));
            const sub = s.kind === "decision" ? (chosen(s.decision) ? s.decision.options.find((o) => o.id === chosen(s.decision))?.label : "pick one")
              : s.kind === "split" ? `${plan.split.length} ${plan.split.length === 1 ? "worker" : "workers"}`
              : comments[s.id]?.trim() ? "commented" : undefined;
            return (
              <button key={s.id} type="button" aria-current={i === at ? "step" : undefined} onClick={() => go(i)}
                className={`grid grid-cols-[20px_1fr] items-start gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] ${i === at ? "bg-branch-soft font-semibold text-ink" : isDone ? "text-ink-2" : "text-ink-3"}`}>
                <span aria-hidden className={`${mono} grid h-[18px] w-[18px] place-items-center rounded-full border text-[10.5px] ${i === at ? "border-branch bg-branch text-on-branch" : isDone ? "border-pass bg-pass text-canvas" : "border-line-strong"}`}>
                  {isDone ? "✓" : i + 1}
                </span>
                <span className="min-w-0">
                  <span className="block truncate">{s.title}</span>
                  {sub && <span className="block truncate text-[11.5px] font-normal text-ink-3">{sub}</span>}
                </span>
              </button>
            );
          })}
          <div className={`${mono} mt-auto border-t border-line px-2 pt-s1 text-[11.5px] text-ink-3`} data-testid="plan-progress">
            {`${done} of ${reading.length} settled`}
            <div className="mt-1.5 h-1 overflow-hidden rounded-sm bg-line" aria-hidden>
              <span className="block h-full bg-pass" style={{ width: `${reading.length ? (done / reading.length) * 100 : 100}%` }} />
            </div>
          </div>
        </nav>
        <div className="min-h-0 min-w-0 flex-1 overflow-auto px-s3 py-s3">
          <article className="mx-auto flex max-w-[760px] flex-col gap-s2">
            <div>
              <p className={`${mono} text-[11px] text-ink-3`}>plan · rev {plan.rev}</p>
              <h1 className="mt-1 text-[21px] font-semibold tracking-[-0.02em]">{plan.title}</h1>
              <p className="text-xs text-ink-3">{by}</p>
            </div>
            {plan.ask && (
              <div className="flex flex-col gap-1 rounded-lg border border-line bg-canvas-2 px-s2 py-s1 text-[12.5px] text-ink-2" data-testid="plan-ask">
                <div className="flex items-start gap-s2">
                  <p className="flex-1">
                    <b className="font-semibold text-ink">What I understood:</b> {plan.ask}
                  </p>
                  {comments.ask?.trim() ? <span className="whitespace-nowrap text-[11px] text-sap">you corrected it</span> : commentButton("ask", "Correct it")}
                </div>
                {commentBox("ask", "What I understood")}
              </div>
            )}
            <section aria-labelledby={`plan-${step.id}`} className="flex flex-col gap-s1">
              <div className="flex items-center gap-2">
                <h3 id={`plan-${step.id}`} className="flex-1 text-base font-semibold">{step.title}</h3>
                {step.kind !== "summary" && commentButton(step.id)}
              </div>
              {step.kind === "lede" && <Markdown text={plan.lede} />}
              {step.kind === "section" && <Markdown text={step.section.markdown} />}
              {step.kind === "decision" && <Decision decision={step.decision} chosen={chosen(step.decision)} open={open}
                onPick={(o) => { setPicks((p) => ({ ...p, [step.decision.id]: o })); settle(step.id); }} />}
              {step.kind === "split" && <Split plan={plan} shared={shared} />}
              {step.kind === "summary" && <Summary plan={plan} chosen={chosen} said={said} titles={titles} shared={shared} />}
              {step.kind !== "summary" && commentBox(step.id, step.title)}
            </section>
            <div className="flex items-center gap-s1 border-t border-line pt-s1">
              {at > 0 && (
                <button type="button" className={button} onClick={() => go(at - 1)}>
                  ← Back
                </button>
              )}
              <span className="flex-1" />
              {at < steps.length - 1 && (
                <button type="button" className={button} onClick={() => go(at + 1)}>
                  {`Next: ${steps[at + 1].title} →`}
                </button>
              )}
            </div>
          </article>
        </div>
      </div>
      <footer className="flex flex-col gap-s1 border-t border-line px-s2 py-s1" data-testid="plan-verdict">
        {open ? (
          <div className="flex flex-wrap items-center gap-s1">
            <input className={`${field} min-w-40 flex-1`} aria-label="Add a note" placeholder="Add a note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
            {shared.size > 0 && <span className="text-xs text-sap">{`${shared.size} ${shared.size === 1 ? "file is" : "files are"} in more than one worker`}</span>}
            {plan.queued > 0 && <span className="text-xs text-ink-3">{plan.queued} assigned {plan.queued === 1 ? "task waits" : "tasks wait"} on this plan</span>}
            <LoadingButton className={button} busy={sending === "redirect"} disabled={!!sending || !canRedirect} title={canRedirect ? "Send your picks and comments back for a new revision" : "Comment, add a note or pick differently first"} onClick={() => void decide(false)}>
              Redirect
            </LoadingButton>
            <LoadingButton className={main} busy={sending === "approve"} disabled={!!sending} onClick={() => void decide(true)}>
              Approve
            </LoadingButton>
          </div>
        ) : (
          <div role="status" className="flex flex-col gap-0.5 text-sm" data-testid="plan-decided">
            <p>{plan.state === "approved" ? `You approved r${plan.rev}. Its workers start now.` : "You asked for changes. The supervisor is revising the plan."}</p>
            {plan.verdict?.comments?.map((c, i) => (
              <p key={i} className="text-ink-2">
                <span className="text-ink-3">on {titles.get(c.section) ?? c.section} · </span>
                {c.text}
              </p>
            ))}
          </div>
        )}
        {error && (
          <p role="alert" className="text-xs text-sap">
            {error}
          </p>
        )}
      </footer>
    </>
  );
}

/* A decision's alternatives as cards: the supervisor's pick marked Recommended, the trade-offs as
   + and − lines, and the files each would touch. */
function Decision({ decision: d, chosen, open, onPick }: { decision: PlanDecisionDTO; chosen: string | null; open: boolean; onPick(option: string): void }) {
  return (
    <>
      {d.challenge && <p className="border-l-2 border-line-strong pl-s1 text-xs text-ink-2">{d.challenge}</p>}
      <div role="radiogroup" aria-label={d.title} className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-s1">
        {d.options.map((o) => (
          <button key={o.id} type="button" role="radio" aria-checked={chosen === o.id} aria-label={o.label} disabled={!open}
            className={`flex flex-col gap-2 rounded-lg border bg-canvas p-s1 text-left text-[12.5px] text-ink-2 ${chosen === o.id ? "border-branch ring-2 ring-branch-soft" : "border-line-strong"}`}
            onClick={() => onPick(o.id)}>
            <span className="flex flex-wrap items-center gap-1.5 text-[13.5px] font-semibold text-ink">
              {o.label}
              {o.id === d.mine && <span className={`${chip} bg-pass-soft text-pass`}>Recommended</span>}
            </span>
            {o.detail && <span>{o.detail}</span>}
            {(!!o.pros?.length || !!o.cons?.length) && (
              <span className="flex flex-col gap-0.5 text-xs">
                {o.pros?.map((t, i) => (
                  <span key={`p${i}`}>
                    <span className={`${mono} text-[12px] text-pass`}>+ </span>
                    {t}
                  </span>
                ))}
                {o.cons?.map((t, i) => (
                  <span key={`c${i}`}>
                    <span className={`${mono} text-[12px] text-sap`}>− </span>
                    {t}
                  </span>
                ))}
              </span>
            )}
            {!!o.touches?.length && (
              <span className="mt-auto flex flex-wrap items-center gap-1 border-t border-line pt-s1 text-xs text-ink-3" data-testid={`touches-${o.id}`}>
                Touches
                {o.touches.map((f) => (
                  <code key={f} className={fileChip}>{f}</code>
                ))}
              </span>
            )}
          </button>
        ))}
      </div>
    </>
  );
}

/* The proposed split as task cards: each worker, the files it changes (a file another worker
   changes too is flagged) and the one test that proves it. */
function Split({ plan, shared }: { plan: Plan; shared: Map<string, string[]> }) {
  return (
    <>
      {shared.size > 0 && (
        <p role="alert" className="rounded-md bg-sap-soft px-s2 py-s1 text-sm text-sap" data-testid="split-overlaps">
          {[...shared].map(([f, ws]) => `${f} is in ${ws.join(" and ")}`).join("; ")}. Workers that change the same file can conflict: order them with after, or redirect.
        </p>
      )}
      <ol aria-label="Proposed split" className="grid grid-cols-[repeat(auto-fill,minmax(210px,1fr))] gap-s1">
        {plan.split.map((w) => (
          <li key={w.id} className="flex min-w-0 flex-col gap-2 rounded-lg border border-line-strong bg-canvas p-s1 text-[12.5px]" data-testid={`task-${w.id}`}>
            <span className="text-[13.5px] font-semibold text-ink">{w.title}</span>
            <span className={`${mono} text-[11px] text-ink-3`}>
              {`worker ${w.id}`}
              {w.after?.length ? ` · after ${w.after.join(", ")}` : ""}
            </span>
            {!!w.files?.length && (
              <span className="flex flex-wrap gap-1">
                {w.files.map((f) => {
                  const others = shared.get(f)?.filter((id) => id !== w.id);
                  return others ? (
                    <code key={f} className={`${mono} rounded-sm border border-sap bg-sap-soft px-1 text-[11px] text-sap`} title={`also in ${others.join(", ")}`} data-overlap="true">
                      {`${f} ⚠ also ${others.join(", ")}`}
                    </code>
                  ) : (
                    <code key={f} className={fileChip}>{f}</code>
                  );
                })}
              </span>
            )}
            <span className={`${mono} rounded-md px-1.5 py-1 text-[11px] ${w.test ? "bg-surface-2 text-ink-2" : "text-ink-3"}`}>
              {w.test ? `proves it: ${w.test}` : "no test named"}
            </span>
          </li>
        ))}
      </ol>
    </>
  );
}

/* The last step: what the verdict will send, before the person sends it. */
function Summary({ plan, chosen, said, titles, shared }: {
  plan: Plan;
  chosen(d: PlanDecisionDTO): string | null;
  said: { section: string; text: string }[];
  titles: Map<string, string>;
  shared: Map<string, string[]>;
}) {
  return (
    <div className="flex flex-col gap-s1 text-sm">
      {plan.decisions.length > 0 && (
        <div>
          <h4 className={label}>Decisions</h4>
          <ul className="mt-1 flex flex-col gap-0.5">
            {plan.decisions.map((d) => {
              const pick = d.options.find((o) => o.id === chosen(d));
              return (
                <li key={d.id}>
                  <span className="text-ink-3">{d.title}: </span>
                  {pick?.label ?? "no pick"}
                  {pick && pick.id !== d.mine && <span className="text-xs text-sap"> (not the supervisor's pick)</span>}
                </li>
              );
            })}
          </ul>
        </div>
      )}
      {said.length > 0 && (
        <div>
          <h4 className={label}>Your comments</h4>
          <ul className="mt-1 flex flex-col gap-0.5">
            {said.map((c) => (
              <li key={c.section}>
                <span className="text-ink-3">on {titles.get(c.section) ?? c.section} · </span>
                {c.text}
              </li>
            ))}
          </ul>
        </div>
      )}
      {plan.split.length > 0 && (
        <p className="text-ink-2">
          {`${plan.split.length} ${plan.split.length === 1 ? "worker starts" : "workers start"} on approval.`}
          {shared.size > 0 && <span className="text-sap">{` ${shared.size} ${shared.size === 1 ? "file is" : "files are"} in more than one worker.`}</span>}
        </p>
      )}
      <p className="text-ink-3">Approve starts the queued work; Redirect sends your picks, comments and note back for a new revision.</p>
    </div>
  );
}
