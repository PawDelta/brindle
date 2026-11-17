/* Who reviews a design doc, in its header: each reviewer,
   required or optional, with their sign-off state, and the org rules nobody on the list meets.
   Before the doc is shared, the control that shares it; after, the same list to edit. Required
   reviewers hold the build (the gate, brindle/plans.py), optional ones never do. Once the plan is
   building the review is closed and the list is only read. */

import { useEffect, useState } from "react";
import type { DocScope, LibraryPerson, Plan, PlanReviewerChoice, PolicyPreviewRow } from "../../../api/client";
import type { PlanGateRowDTO } from "../../../api/schema";
import { button, field, main, quiet } from "../../../components/ui";
import type { PlanSharingClient } from "./client";
import { PolicyPreview, Policies } from "./DocPolicies";

const STATUS: Record<PlanGateRowDTO["status"], { text: string; tone: string }> = {
  signed_off: { text: "✓ signed off", tone: "text-pass" },
  changes_requested: { text: "changes requested", tone: "text-sap" },
  waiting: { text: "not yet", tone: "text-ink-3" },
  waived: { text: "waived", tone: "text-ink-3" },
};

const people = (plan: Plan): PlanReviewerChoice[] =>
  (plan.gate?.reviewers ?? []).flatMap((r) => (r.sub ? [{ sub: r.sub, required: r.required }] : []));

export function Reviewers({ plan, client, directory, adminOrg, me, scopes, busy, nameOf, write, reviewVersion, reviewBlocked }: {
  plan: Plan;
  client?: PlanSharingClient;
  directory: LibraryPerson[];
  adminOrg: string | null;
  me: string | null;
  scopes: DocScope[];
  busy: boolean;
  reviewVersion?: number;
  reviewBlocked?: boolean;
  nameOf(sub: string): string;
  /* One write to the doc (the reader keeps them in order); true once it landed. */
  write(work: () => Promise<Plan>): Promise<boolean>;
}) {
  const gate = plan.gate;
  const closed = plan.state === "approved";
  // The list being edited belongs to the list it was opened on: saving replaces the whole list,
  // so a change a teammate made meanwhile must not be written over unseen.
  const [draft, setDraft] = useState<{ rows: PlanReviewerChoice[]; source: string } | null>(null);
  const [scope, setScope] = useState("");
  const [typed, setTyped] = useState("");
  // The rules a first share will meet, shown before anyone is added. A result belongs to the plan and
  // revision it was asked for: it is shown only while both still are the ones on screen, and the form
  // asks again when a revision changes the files. The Library applies the rules whether or not this loads.
  const [preview, setPreview] = useState<{ plan: string; rev: number; rows: PolicyPreviewRow[] } | null>(null);
  const previewing = !!draft && !gate && !!client?.previewPlanPolicies;
  useEffect(() => {
    if (!previewing) return;
    let live = true;
    const [id, rev] = [plan.id, plan.rev];
    void client!.previewPlanPolicies!(id).then((r) => { if (live) setPreview({ plan: id, rev, rows: r.policies }); }, () => undefined);
    return () => { live = false; };
  }, [previewing, client, plan.id, plan.rev]);
  const current = JSON.stringify(people(plan));
  const stale = !!draft && draft.source !== current;
  const shareable = scopes.filter((s) => s.kind !== "user" && s.can_share !== false);
  if (!gate && (!client || closed || shareable.length === 0)) return null;

  const open = () => { setPreview(null); setDraft({ rows: people(plan), source: current }); };
  const close = () => setDraft(null);
  const add = (sub: string) => {
    if (draft && sub && !draft.rows.some((r) => r.sub === sub)) setDraft({ ...draft, rows: [...draft.rows, { sub, required: true }] });
  };
  const save = async () => {
    if (!client || !draft || stale) return;
    const chosen = shareable.find((s) => `${s.kind}:${s.id}` === scope) ?? shareable[0];
    const sent = gate
      ? () => client.setPlanReviewers(plan.id, draft.rows)
      : () => client.sharePlan(plan.id, { kind: chosen.kind, id: chosen.id }, draft.rows);
    if (await write(sent)) close();
  };
  const available = directory.filter((p) => !draft?.rows.some((r) => r.sub === p.sub));

  return (
    <>
      <dt className="text-ink-3">Reviewers</dt>
      <dd className="m-0 grid gap-1.5" data-testid="plan-reviewers">
        {!draft && (
          <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1">
            {/* An org rule's own row is in the locked list below, so it isn't shown twice. */}
            {gate?.reviewers?.filter((r) => r.sub || !gate.policies?.length).map((r) => {
              const key = r.sub ?? r.name ?? "";
              return (
                <span key={key} className="inline-flex flex-wrap items-center gap-1.5" data-testid={`plan-reviewer-${key}`} data-status={r.status} data-required={r.required}>
                  <span className="text-ink">{r.sub ? (r.you ? "You" : nameOf(r.sub)) : r.name}</span>
                  <span className="text-[11px] text-ink-3">{r.required ? "required" : "optional"}</span>
                  <span className={STATUS[r.status].tone}>{r.changes_by?.length ? `changes requested by ${r.changes_by.map(nameOf).join(", ")}` : r.met_by?.length ? `✓ met by ${r.met_by.map(nameOf).join(", ")}` : STATUS[r.status].text}</span>
                  {(r.meets?.length ?? 0) > 0 && <span className="text-[11px] text-ink-3">{`also counts for ${r.meets!.join(", ")}`}</span>}
                  {r.you && r.status !== "signed_off" && !closed && client && (
                    <button type="button" className={`${quiet} -my-0.5 text-xs`} disabled={busy || reviewBlocked} onClick={() => void write(() => client.signOffPlan(plan.id, plan.rev, reviewVersion))}>Sign off</button>
                  )}
                </span>
              );
            })}
            {!closed && client && (
              <button type="button" className={`${quiet} -my-0.5 text-xs`} onClick={open}>{gate ? "Edit" : "Share for review"}</button>
            )}
          </div>
        )}
        <Policies plan={plan} client={client} directory={directory} adminOrg={adminOrg} me={me} busy={busy} nameOf={nameOf} write={write} />
        {draft && (
          <div className="grid max-w-[60ch] gap-2 rounded-md border border-line p-2.5" data-testid="plan-reviewers-edit" data-stale={stale || undefined}>
            {!gate && (
              <select className={`${field} text-[12.5px]`} aria-label="Share with" value={scope || `${shareable[0].kind}:${shareable[0].id}`} onChange={(e) => setScope(e.target.value)}>
                {shareable.map((s) => <option key={`${s.kind}:${s.id}`} value={`${s.kind}:${s.id}`}>{s.name ?? s.id}</option>)}
              </select>
            )}
            {!gate && preview?.plan === plan.id && preview.rev === plan.rev && <PolicyPreview rows={preview.rows} />}
            {draft.rows.map((r) => (
              <div key={r.sub} className="flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-ink">{nameOf(r.sub)}</span>
                <label className="inline-flex items-center gap-1 text-xs text-ink-2">
                  <input type="checkbox" aria-label={`${nameOf(r.sub)} is required`} checked={r.required} onChange={(e) => setDraft({ ...draft, rows: draft.rows.map((x) => (x.sub === r.sub ? { ...x, required: e.target.checked } : x)) })} />
                  Required
                </label>
                <button type="button" className={`${quiet} text-xs`} aria-label={`Remove ${nameOf(r.sub)}`} onClick={() => setDraft({ ...draft, rows: draft.rows.filter((x) => x.sub !== r.sub) })}>×</button>
              </div>
            ))}
            {directory.length > 0 ? (
              <select className={`${field} text-[12.5px]`} aria-label="Add a reviewer" value="" onChange={(e) => add(e.target.value)}>
                <option value="">Add a reviewer…</option>
                {available.map((p) => <option key={p.sub} value={p.sub}>{p.email || p.sub}</option>)}
              </select>
            ) : (
              <form className="flex gap-1.5" onSubmit={(e) => { e.preventDefault(); add(typed.trim()); setTyped(""); }}>
                <input className={`${field} min-w-0 flex-1 text-[12.5px]`} aria-label="Reviewer's account id" placeholder="Reviewer's account id" value={typed} onChange={(e) => setTyped(e.target.value)} />
                <button type="submit" className={`${button} text-xs`} disabled={!typed.trim()}>Add</button>
              </form>
            )}
            {stale && <p role="alert" className="text-xs text-sap">The reviewers changed since you opened this. Cancel and edit the new list.</p>}
            <div className="flex items-center gap-1.5">
              <button type="button" className={`${main} text-xs`} disabled={busy || stale} onClick={() => void save()}>{gate ? "Save reviewers" : "Share"}</button>
              <button type="button" className={`${quiet} text-xs`} onClick={close}>Cancel</button>
            </div>
          </div>
        )}
      </dd>
    </>
  );
}
