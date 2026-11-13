/* The org review policies on a design doc: locked rows nobody
   on the doc can remove, each with why it applies and who can meet it, and any waiver or stand-in an
   org admin has recorded. The Library decides who may override a rule; adminOrg only says whom to offer it to. */

import { useState } from "react";
import type { LibraryPerson, Plan, PlanPolicyOverride, PolicyPreviewRow } from "../../../api/client";
import type { PlanGatePolicyDTO } from "../../../api/schema";
import { field, main, quiet } from "../../../components/ui";
import type { PlanSharingClient } from "./client";

type Covered = Pick<PlanGatePolicyDTO, "name" | "needs_kind" | "needs_name" | "approvals" | "eligible" | "when" | "label" | "matched">;

/* Before the doc is shared: the rules that will cover it, already there when the person adds reviewers. */
export function PolicyPreview({ rows }: { rows: PolicyPreviewRow[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="grid gap-1" data-testid="plan-policy-preview">
      <span className="text-[11px] text-ink-3">Required by org policy · can't remove</span>
      {rows.map((p) => (
        <div key={`${p.org_id}:${p.policy}`} className="flex flex-wrap items-center gap-x-2" data-testid={`plan-policy-preview-${p.policy}`}>
          <span className="text-ink">{who(p)}</span>
          <span className="text-[11px] text-ink-3">{`${p.name} · ${why(p)}`}</span>
        </div>
      ))}
      <span className="text-[11px] text-ink-3">Policy reviewers are notified when you share.</span>
    </div>
  );
}

const who = (p: Covered) => {
  const needs = p.needs_name ?? p.name;
  if (p.needs_kind === "person") return needs;
  return p.eligible == null ? `${needs} · any ${p.approvals}` : `${needs} · any ${p.approvals} of ${p.eligible}`;
};

const why = (p: Covered) => {
  if (p.when === "label") return `Has the label ${p.label}`;
  if (p.when === "paths") return `Touches ${(p.matched ?? []).join(", ")}`;
  return "Every design";
};

const day = (seconds: number) => new Date(seconds * 1000).toLocaleDateString();

function Standing({ p, nameOf }: { p: PlanGatePolicyDTO; nameOf(sub: string): string }) {
  const standing = p.stand_ins ?? [];
  const signer = (sub: string) => (standing.some((s) => s.stand_in === sub) ? `${nameOf(sub)} (stand-in)` : nameOf(sub));
  if (p.status === "waived" && p.waiver) return <span className="text-ink-3">{`waived once by ${nameOf(p.waiver.by)}: ${p.waiver.reason}`}</span>;
  if (p.changes_by?.length) return <span className="text-sap">{`changes requested by ${p.changes_by.map(nameOf).join(", ")}`}</span>;
  if (p.status === "signed_off") return <span className="text-pass">{p.met_by?.length ? `✓ met by ${p.met_by.map(signer).join(", ")}` : "✓ met"}</span>;
  return <span className="text-ink-3">waiting</span>;
}

function Override({ plan, rev, policy, action, client, candidates, label, busy, write, close }: {
  plan: Plan;
  /* The revision this was opened on: a waiver is of what the admin read, so it never follows a newer one. */
  rev: number;
  policy: PlanGatePolicyDTO;
  action: PlanPolicyOverride["action"];
  client: PlanSharingClient;
  candidates: string[];
  label(sub: string): string;
  busy: boolean;
  write(work: () => Promise<Plan>): Promise<boolean>;
  close(): void;
}) {
  const [reason, setReason] = useState("");
  const [person, setPerson] = useState("");
  const [until, setUntil] = useState("");
  const standIn = action === "stand_in";
  const stale = rev !== plan.rev;
  const ready = !stale && !!reason.trim() && (!standIn || (!!person && !!until));
  const send = async () => {
    if (!ready) return;
    const base = { rev, org_id: policy.org_id, policy: policy.policy, action, reason: reason.trim() };
    const body = standIn ? { ...base, stand_in: person, until: Math.floor(new Date(`${until}T23:59:59`).getTime() / 1000) } : base;
    if (await write(() => client.overridePlanPolicy(plan.id, body))) close();
  };
  return (
    <div className="grid max-w-[60ch] gap-1.5 rounded-md border border-line p-2" data-testid={`plan-policy-override-${policy.policy}`} data-stale={stale || undefined}>
      {standIn && (
        <>
          <select className={`${field} text-[12.5px]`} aria-label="Stand-in" value={person} onChange={(e) => setPerson(e.target.value)}>
            <option value="">Who stands in…</option>
            {candidates.map((sub) => <option key={sub} value={sub}>{label(sub)}</option>)}
          </select>
          <input type="date" className={`${field} text-[12.5px]`} aria-label="Stands in until" value={until} onChange={(e) => setUntil(e.target.value)} />
        </>
      )}
      <input className={`${field} text-[12.5px]`} aria-label="Reason" placeholder={standIn ? "Why they're standing in" : "Why it's waived for this revision"} value={reason} onChange={(e) => setReason(e.target.value)} />
      {stale && <p role="alert" className="m-0 text-xs text-sap">This design has a new revision. Cancel, read it, and start again.</p>}
      <div className="flex items-center gap-1.5">
        <button type="button" className={`${main} text-xs`} disabled={busy || !ready} onClick={() => void send()}>{standIn ? "Name stand-in" : "Waive once"}</button>
        <button type="button" className={`${quiet} text-xs`} onClick={close}>Cancel</button>
      </div>
    </div>
  );
}

export function Policies({ plan, client, directory, adminOrg, me, busy, nameOf, write }: {
  plan: Plan;
  client?: PlanSharingClient;
  directory: LibraryPerson[];
  adminOrg: string | null;
  me: string | null;
  busy: boolean;
  nameOf(sub: string): string;
  write(work: () => Promise<Plan>): Promise<boolean>;
}) {
  const [open, setOpen] = useState<{ policy: string; action: PlanPolicyOverride["action"]; rev: number } | null>(null);
  const policies = plan.gate?.policies ?? [];
  if (policies.length === 0) return null;
  const closed = plan.state === "approved";
  const label = (sub: string) => (sub === me ? "You" : nameOf(sub));
  // The directory leaves the person out, and an admin may name themself.
  const candidates = [...(me ? [me] : []), ...directory.map((p) => p.sub)];
  return (
    <div className="grid gap-1" data-testid="plan-policies">
      <span className="text-[11px] text-ink-3">Required by org policy · can't remove</span>
      {policies.map((p) => {
        const unmet = p.status !== "signed_off" && p.status !== "waived";
        const canOverride = !!client && !closed && unmet && p.org_id === adminOrg;
        const standing = (p.stand_ins ?? []).filter((s) => s.until * 1000 > Date.now());
        return (
          <div key={`${p.org_id}:${p.policy}`} className="grid gap-0.5" data-testid={`plan-policy-${p.policy}`} data-status={p.status}>
            <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
              <span className="text-ink">{who(p)}</span>
              <span className="text-[11px] text-ink-3">{`${p.name} · ${why(p)}`}</span>
              <Standing p={p} nameOf={label} />
              {!!client && !closed && unmet && standing.some((s) => s.stand_in === me) && (
                <button type="button" className={`${quiet} -my-0.5 text-xs`} disabled={busy} onClick={() => void write(() => client.signOffPlan(plan.id, plan.rev))}>Sign off</button>
              )}
              {canOverride && (
                <>
                  <button type="button" className={`${quiet} -my-0.5 text-xs`} onClick={() => setOpen({ policy: p.policy, action: "waive", rev: plan.rev })}>Waive once</button>
                  {candidates.length > 0 && <button type="button" className={`${quiet} -my-0.5 text-xs`} onClick={() => setOpen({ policy: p.policy, action: "stand_in", rev: plan.rev })}>Name a stand-in</button>}
                  {client.askPlanPolicyAgain && (
                    <button type="button" className={`${quiet} -my-0.5 text-xs`} disabled={busy} onClick={() => void write(() => client.askPlanPolicyAgain!(plan.id, { rev: plan.rev, org_id: p.org_id, policy: p.policy }))}>
                      {`Ask ${p.needs_name ?? p.name} again`}
                    </button>
                  )}
                </>
              )}
            </span>
            {p.note && <span className="text-[11px] text-ink-3">{p.note}</span>}
            {unmet && (p.asked_at != null || !!p.opened_by?.length) && (
              <span className="text-[11px] text-ink-3" data-testid={`plan-policy-asked-${p.policy}`}>
                {[p.asked_at != null && `Asked ${day(p.asked_at)}`, p.opened_by?.length && `Opened by ${p.opened_by.map((o) => label(o.sub)).join(", ")}`].filter(Boolean).join(". ")}
              </span>
            )}
            {standing.map((s) => <span key={s.stand_in} className="text-[11px] text-ink-3">{`${label(s.stand_in)} can stand in until ${day(s.until)}`}</span>)}
            {canOverride && open?.policy === p.policy && (
              <Override key={`${open.action}:${open.rev}`} plan={plan} rev={open.rev} policy={p} action={open.action} client={client} candidates={candidates} label={label} busy={busy} write={write} close={() => setOpen(null)} />
            )}
          </div>
        );
      })}
    </div>
  );
}
