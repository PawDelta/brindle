/* A planning doc (brindle/plans.py), opened over the whole window like any visual. Unlike a visual
   it isn't the agent's HTML: brindle renders its markdown itself (Knowledge's Markdown: elements
   only, no raw HTML, no fetched images), so it needs no frame, and every control on it is the
   app's own. The person reads it like a document, picks among a decision's alternatives, comments
   on any ## section, then Approves (the session's queued workers start) or Redirects (their picks
   and comments go back to the supervisor, who publishes a new revision). Drafts belong to the
   revision on screen: a new revision is a fresh read, and a verdict on an older one is a 409. */

import { useCallback, useEffect, useRef, useState } from "react";
import { DaemonError, type Plan, type VisualDTO } from "../../../api/client";
import { LoadingButton } from "../../../components/LoadingButton";
import { button, field, label, main, mono } from "../../../components/ui";
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

function PlanBody({ client, plan, by, onDecided }: { client: PlansClient; plan: Plan; by: string; onDecided(p: Plan): void }) {
  const [picks, setPicks] = useState<Record<string, string>>({});
  const [comments, setComments] = useState<Record<string, string>>({});
  const [commenting, setCommenting] = useState<Set<string>>(new Set());
  const [note, setNote] = useState("");
  const [sending, setSending] = useState<"approve" | "redirect" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const open = plan.state === "open";
  const said = Object.entries(comments).filter(([, t]) => t.trim()).map(([section, text]) => ({ section, text: text.trim() }));
  const differs = plan.decisions.some((d) => picks[d.id] && picks[d.id] !== d.mine);
  const canRedirect = said.length > 0 || !!note.trim() || differs;
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
  const titles = new Map(plan.sections.map((s) => [s.id, s.title]));
  return (
    <>
      <div className="min-h-0 flex-1 overflow-auto px-s3 py-s3">
        <article className="mx-auto flex max-w-[680px] flex-col gap-s2">
          <div>
            <p className={`${mono} text-[11px] text-ink-3`}>plan · rev {plan.rev}</p>
            <h1 className="mt-1 text-[21px] font-semibold tracking-[-0.02em]">{plan.title}</h1>
            <p className="text-xs text-ink-3">{by}</p>
          </div>
          {plan.lede && <Markdown text={plan.lede} />}
          {plan.split.length > 0 && (
            <section aria-label="Proposed split">
              <h3 className={label}>Proposed split · {plan.split.length} {plan.split.length === 1 ? "worker" : "workers"}</h3>
              <ol className="mt-1 flex flex-col gap-0.5 text-sm">
                {plan.split.map((w) => (
                  <li key={w.id} className="flex gap-2">
                    <span className={`${mono} text-[11px] text-branch`}>{w.id}</span>
                    <span className="flex-1">{w.title}</span>
                    {!!w.after?.length && <span className="text-xs text-ink-3">after {w.after.join(", ")}</span>}
                  </li>
                ))}
              </ol>
            </section>
          )}
          {plan.decisions.length > 0 && (
            <section aria-label="Key decisions" className="flex flex-col gap-s1">
              <h3 className={label}>Key decisions</h3>
              {plan.decisions.map((d) => {
                // Decided: what the person sent; open: their draft; else the supervisor's pick.
                const chosen = (open ? picks[d.id] : plan.verdict?.picks?.[d.id]) ?? d.mine ?? null;
                return (
                  <div key={d.id} className="flex flex-col gap-1">
                    <p className="text-sm font-medium">{d.title}</p>
                    <div role="radiogroup" aria-label={d.title} className="flex flex-wrap gap-1">
                      {d.options.map((o) => (
                        <button key={o.id} type="button" role="radio" aria-checked={chosen === o.id} disabled={!open} title={o.detail ?? undefined}
                          className={`${button} ${chosen === o.id ? "border-branch bg-branch-soft" : ""}`}
                          onClick={() => setPicks((p) => ({ ...p, [d.id]: o.id }))}>
                          {o.label}
                          {o.id === d.mine && <span className="text-[11px] text-ink-3">supervisor's</span>}
                        </button>
                      ))}
                    </div>
                    {d.challenge && <p className="border-l-2 border-line-strong pl-s1 text-xs text-ink-2">{d.challenge}</p>}
                  </div>
                );
              })}
            </section>
          )}
          {plan.sections.map((s) => (
            <section key={s.id} aria-labelledby={`plan-${s.id}`} className="flex flex-col gap-1">
              <div className="flex items-center gap-2">
                <h3 id={`plan-${s.id}`} className="flex-1 text-base font-semibold">{s.title}</h3>
                {open && !commenting.has(s.id) && (
                  <button type="button" className="btn btn-quiet text-xs" onClick={() => setCommenting((c) => new Set(c).add(s.id))}>
                    Comment
                  </button>
                )}
              </div>
              <Markdown text={s.markdown} />
              {open && commenting.has(s.id) && (
                <textarea className={`${field} min-h-16`} aria-label={`Comment on ${s.title}`} placeholder="What should change here?" autoFocus
                  value={comments[s.id] ?? ""} onChange={(e) => setComments((c) => ({ ...c, [s.id]: e.target.value }))} />
              )}
            </section>
          ))}
        </article>
      </div>
      <footer className="flex flex-col gap-s1 border-t border-line px-s2 py-s1" data-testid="plan-verdict">
        {open ? (
          <div className="flex flex-wrap items-center gap-s1">
            <input className={`${field} min-w-40 flex-1`} aria-label="Add a note" placeholder="Add a note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
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
