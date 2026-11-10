/* A planning doc (brindle/plans.py), opened over the whole window like any visual. Unlike a visual
   it isn't the agent's HTML: brindle renders its markdown itself (Knowledge's Markdown: elements
   only, no raw HTML, no fetched images), so it needs no frame, and every control on it is the
   app's own. The person reads it one step at a time (the overview, each decision, each ## section,
   the split, then a summary): the steps down a left column (PlanSteps.tsx), the step itself in the
   main pane under the supervisor's restating of their ask, and a bar along the bottom with what
   they picked, a note, the way back and on, and the verdict. They pick among a decision's
   alternatives (cards with trade-offs, the files each touches and the supervisor's pick marked
   Recommended; PlanCards.tsx), comment on any step, then Approve (the session's queued workers
   start, and the split's task cards stay on top as a live board) or Redirect (their picks and
   comments go back to the supervisor, who publishes a new revision). Drafts belong to the revision
   on screen: a new revision is a fresh read, and a verdict on an older one is a 409. */

import { useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { DaemonError, type Plan, type VisualDTO } from "../../../api/client";
import type { PlanDecisionDTO, PlanSectionDTO } from "../../../api/schema";
import { LoadingButton } from "../../../components/LoadingButton";
import { button, field, h2, label, main, quiet } from "../../../components/ui";
import { Markdown } from "../../Knowledge/Markdown";
import type { ExportClient } from "../../Knowledge/ExportMenu";
import type { PlansClient, VisualsClient } from "./client";
import { BackButton, KeepButton, message, useEscBack } from "./chrome";
import { ExportButton } from "./ExportButton";
import { Decision, Tasks, branchChip, overlaps, passChip } from "./PlanCards";
import { PlanSteps, type StepRow } from "./PlanSteps";
import { StoreContext } from "../../../store/useStore";

type Client = VisualsClient & PlansClient & Partial<ExportClient>;

/* How far along a plan is: its revision, then decided or not. Revisions only grow and a decided
   revision never reopens, so a response with less progress than the one on screen is stale. */
const progress = (p: Plan) => p.rev * 2 + (p.state === "open" ? 0 : 1);
const noSubscribe = () => () => {};

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
  const store = useContext(StoreContext);
  const updates = useSyncExternalStore(store?.subscribe ?? noSubscribe, () => {
    const s = store?.getState();
    return `${s?.recent.filter((e) => e.root_id === visual.session || e.payload.caller_id === visual.session).at(-1)?.id ?? 0}:${s?.snapshotVersion ?? 0}`;
  });
  // Read again whenever the session's row says the plan changed (a revision, a verdict).
  const changed = `${visual.id}:${visual.rev}:${visual.answer?.at ?? ""}`;
  const seq = useRef(0);
  const selected = useRef(visual.id);
  selected.current = visual.id;
  // Reads and verdict replies can land in any order: the plan on screen only ever moves forward
  // (a newer revision, or this revision decided), and only for the plan still open here.
  const accept = useCallback((p: Plan, verdict = false) => {
    if (p.id !== selected.current) return;
    setPlan((now) => (now && now.id === p.id && (progress(now) > progress(p) || (verdict && progress(now) === progress(p))) ? now : p));
  }, []);
  useEffect(() => {
    const mine = ++seq.current;
    client.plan(visual.id).then(
      (p) => {
        if (mine !== seq.current) return;
        accept(p);
        if (mine === seq.current) setError(null);
      },
      (e) => {
        if (mine === seq.current) setError(message(e));
      },
    );
  }, [client, visual.id, changed, accept, updates]);
  const shown = plan && plan.id === visual.id ? plan : null;
  const exportClient = client.exportVisual && client.exportDoc && client.docPolicy && client.docExportConfig ? (client as ExportClient) : undefined;
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="plan-reader">
      {/* One toolbar, as a visual's: Back (Esc) and the title, then what this is and its actions. */}
      <header className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-line px-s2 py-2">
        <BackButton onBack={onBack} asking={asking} />
        <span className="mx-1 h-5 w-px bg-line" aria-hidden />
        <h2 className="min-w-0 flex-1 truncate text-[15px] font-semibold tracking-[-0.01em]">{visual.title}</h2>
        {shown?.state === "approved" && (
          <span className={`${passChip} font-mono`} data-testid="plan-board-progress">
            approved · {shown.split.filter((w) => w.test_state === "passing").length} of {shown.split.length} green
          </span>
        )}
        <span className={`${branchChip} whitespace-pre font-mono`} title={`${visual.id} · rendered by brindle from the plan's markdown`}>
          <span>{`plan · rev ${shown?.rev ?? visual.rev}`}</span>
          <span>{` · ${visual.by}`}</span>
        </span>
        <KeepButton client={client} visual={visual} onKept={onChanged} />
        {exportClient && shown && (
          <ExportButton key={`${shown.id}:${shown.rev}`} client={exportClient} repo={repo} open={exporting} onOpenChange={setExporting} source={{ kind: "plan", id: shown.id, rev: shown.rev, markdown: shown.markdown }} />
        )}
      </header>
      {shown ? <>
        {shown.state === "approved" && (
          <section aria-label="Plan board" className="flex max-h-[50vh] shrink-0 flex-col gap-3.5 overflow-auto border-b border-line px-s3 py-5">
            <Tasks plan={shown} shared={overlaps(shown)} />
          </section>
        )}
        <PlanBody key={`${shown.id}:${shown.rev}`} client={client} plan={shown} onDecided={(p) => accept(p, true)} />
      </> : <p className="p-s3 text-sm text-ink-3">{error ?? "Loading…"}</p>}
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

function PlanBody({ client, plan, onDecided }: { client: PlansClient; plan: Plan; onDecided(p: Plan): void }) {
  const [picks, setPicks] = useState<Record<string, string>>({});
  const [comments, setComments] = useState<Record<string, string>>({});
  const [commenting, setCommenting] = useState<Set<string>>(new Set());
  const [note, setNote] = useState("");
  const [sending, setSending] = useState<"approve" | "redirect" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const steps = useMemo(() => stepsOf(plan), [plan]);
  const [at, setAt] = useState(0);
  // Each step opens at its top (its heading and the restated ask), not where the last one was scrolled to.
  const pane = useRef<HTMLDivElement>(null);
  const askStrip = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (pane.current) pane.current.scrollTop = 0;
  }, [at]);
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
  const last = at === steps.length - 1;
  // What each comment anchor is called on screen.
  const titles = new Map<string, string>([...steps.map((s): [string, string] => [s.id, s.title]), ["ask", "What I understood"]]);
  const rows: StepRow[] = steps.map((s) => ({
    id: s.id,
    title: s.title,
    done: s.kind !== "summary" && (!open || settled.has(s.id)),
    sub: s.kind === "decision" ? (chosen(s.decision) ? s.decision.options.find((o) => o.id === chosen(s.decision))?.label : "pick one")
      : s.kind === "split" ? `${plan.split.length} ${plan.split.length === 1 ? "worker" : "workers"}`
      : comments[s.id]?.trim() ? "commented" : undefined,
  }));
  // The restated ask: corrected once the person says so; confirmed once they read on without correcting it.
  const corrected = open ? !!comments.ask?.trim() : !!plan.verdict?.comments?.some((c) => c.section === "ask");
  const confirmed = !corrected && (!open || settled.size > 0);
  const correctAsk = () => {
    setCommenting((c) => new Set(c).add("ask"));
    if (pane.current) pane.current.scrollTop = 0;
    // Already open: back to it. Opening now: the box takes the focus as it mounts.
    askStrip.current?.querySelector("textarea")?.focus();
  };
  const picked = step.kind === "decision" ? step.decision.options.find((o) => o.id === chosen(step.decision)) : undefined;

  const commentBox = (id: string, title: string) =>
    open && commenting.has(id) ? (
      <textarea className={`${field} min-h-16 w-full`} aria-label={`Comment on ${title}`} placeholder="What should change here?" autoFocus
        value={comments[id] ?? ""} onChange={(e) => setComments((c) => ({ ...c, [id]: e.target.value }))} />
    ) : null;
  const commentButton = (id: string, text = "Comment") =>
    open && !commenting.has(id) ? (
      <button type="button" className={`${quiet} -my-0.5 text-xs`} onClick={() => setCommenting((c) => new Set(c).add(id))}>
        {text}
      </button>
    ) : null;
  // The way back and on lives in the bottom bar, decided or not.
  const back = at > 0 && (
    <button type="button" className={button} onClick={() => go(at - 1)}>
      ← Back
    </button>
  );
  const next = (tone: string) => !last && (
    <button type="button" className={`${tone} max-w-80`} onClick={() => go(at + 1)}>
      <span className="truncate">{`Next: ${steps[at + 1].title}`}</span>
      <span aria-hidden>→</span>
    </button>
  );

  return (
    <>
      <div className="flex min-h-0 flex-1">
        <PlanSteps rows={rows} at={at} settled={done} total={reading.length} onGo={go} />
        <div ref={pane} className="min-h-0 min-w-0 flex-1 overflow-auto px-s3 py-5" data-testid="plan-step-pane">
          <article className="flex max-w-[1080px] flex-col gap-3.5">
            {plan.ask && (
              <div ref={askStrip} className="flex flex-col gap-2 rounded-lg border border-line bg-canvas-2 px-3 py-2.5 text-[12.5px] text-ink-2" data-testid="plan-ask">
                <div className="flex items-start gap-3">
                  <p className="min-w-0 flex-1">
                    <b className="font-semibold text-ink">What I understood:</b> {plan.ask}
                  </p>
                  {corrected ? <span className="whitespace-nowrap font-mono text-[11px] leading-[19px] text-sap">you corrected it</span>
                    : confirmed && <span className="whitespace-nowrap font-mono text-[11px] leading-[19px] text-pass">✓ you confirmed</span>}
                  {!corrected && commentButton("ask", "Correct it")}
                </div>
                {commentBox("ask", "What I understood")}
              </div>
            )}
            <section aria-labelledby={`plan-${step.id}`} className="flex flex-col gap-3.5">
              <div className="flex items-center gap-2">
                <h3 id={`plan-${step.id}`} className={`${h2} min-w-0 flex-1 text-ink`}>{step.title}</h3>
                {step.kind !== "summary" && commentButton(step.id)}
              </div>
              {step.kind === "lede" && <div className="max-w-[72ch]"><Markdown text={plan.lede} /></div>}
              {step.kind === "section" && <div className="max-w-[72ch]"><Markdown text={step.section.markdown} /></div>}
              {step.kind === "decision" && <Decision decision={step.decision} chosen={chosen(step.decision)} open={open}
                onPick={(o) => { setPicks((p) => ({ ...p, [step.decision.id]: o })); settle(step.id); }} />}
              {step.kind === "split" && (plan.state === "approved" ? <p className="text-sm text-ink-3">Its tasks are the live board above.</p> : <Tasks plan={plan} shared={shared} />)}
              {step.kind === "summary" && <Summary plan={plan} chosen={chosen} said={said} titles={titles} shared={shared} />}
              {step.kind !== "summary" && commentBox(step.id, step.title)}
            </section>
          </article>
        </div>
      </div>
      <footer className="flex flex-col gap-1.5 border-t border-line bg-canvas-2 px-s2 py-2" data-testid="plan-verdict">
        {open ? (
          <div className="flex flex-wrap items-center gap-2">
            {step.kind === "decision" && (
              <span className="text-xs text-ink-2" data-testid="plan-picked">
                {picked ? <>Picked <b className="font-semibold text-ink">{picked.label}</b></> : "Nothing picked yet"}
              </span>
            )}
            <input className={`${field} min-w-40 flex-1`} aria-label="Add a note" placeholder="Add a note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
            {plan.ask && (
              <button type="button" className={quiet} onClick={correctAsk}>
                Correct the ask
              </button>
            )}
            {back}
            <LoadingButton className={button} busy={sending === "redirect"} disabled={!!sending || !canRedirect} title={canRedirect ? "Send your picks and comments back for a new revision" : "Comment, add a note or pick differently first"} onClick={() => void decide(false)}>
              Redirect
            </LoadingButton>
            {/* One main action at a time: reading on, then, on the last step, the approval. */}
            <LoadingButton className={last ? main : button} busy={sending === "approve"} disabled={!!sending} onClick={() => void decide(true)}>
              Approve
            </LoadingButton>
            {next(main)}
          </div>
        ) : (
          <div className="flex items-start gap-2">
            <div role="status" className="flex min-w-0 flex-1 flex-col gap-0.5 text-sm" data-testid="plan-decided">
              <p>{plan.state === "approved" ? `You approved r${plan.rev}. Its workers start now.` : "You asked for changes. The supervisor is revising the plan."}</p>
              {plan.verdict?.comments?.map((c, i) => (
                <p key={i} className="text-ink-2">
                  <span className="text-ink-3">on {titles.get(c.section) ?? c.section} · </span>
                  {c.text}
                </p>
              ))}
            </div>
            {back}
            {next(button)}
          </div>
        )}
        {open && (shared.size > 0 || plan.queued > 0) && (
          <p className="flex flex-wrap gap-x-3 text-xs">
            {shared.size > 0 && <span className="text-sap">{`${shared.size} ${shared.size === 1 ? "file is" : "files are"} in more than one worker`}</span>}
            {plan.queued > 0 && <span className="text-ink-3">{plan.queued} assigned {plan.queued === 1 ? "task waits" : "tasks wait"} on this plan</span>}
          </p>
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

/* The last step: what the verdict will send, before the person sends it. */
function Summary({ plan, chosen, said, titles, shared }: {
  plan: Plan;
  chosen(d: PlanDecisionDTO): string | null;
  said: { section: string; text: string }[];
  titles: Map<string, string>;
  shared: Map<string, string[]>;
}) {
  return (
    <div className="flex max-w-[72ch] flex-col gap-3.5 text-sm">
      {plan.decisions.length > 0 && (
        <div>
          <h4 className={label}>Decisions</h4>
          <ul className="mt-1.5 flex flex-col gap-1">
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
          <ul className="mt-1.5 flex flex-col gap-1">
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
