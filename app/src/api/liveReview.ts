import type { LiveDocState, Plan } from "./client";

/* A stale HTTP read cannot restore sign-offs withdrawn on the live channel. The daemon's
   gate includes org policy checks; without one, only explicit current signers can count. */
export function liveReviewGate(gate: Plan["gate"], state: LiveDocState | null): Plan["gate"] {
  if (!gate) return gate;
  if (state?.version && state.gate) return state.gate;
  const signed = new Set(state?.version?.signOffs ?? []);
  const reviewers = gate.reviewers?.map((row) => {
    const met_by = row.met_by?.filter((sub) => signed.has(sub));
    const met = row.sub ? signed.has(row.sub) : !!met_by?.length && met_by.length === row.met_by?.length;
    return { ...row, met_by, status: row.status === "waived" ? row.status : met ? "signed_off" as const : "waiting" as const, changes_by: [] };
  });
  const policies = gate.policies?.map((row) => {
    const met_by = row.met_by?.filter((sub) => signed.has(sub)) ?? [];
    return { ...row, met_by, changes_by: [], status: row.status === "waived" ? row.status : met_by.length >= row.approvals ? "signed_off" as const : "waiting" as const };
  });
  const required = reviewers?.filter((row) => row.required) ?? [];
  const waiting = required.filter((row) => row.status !== "signed_off" && row.status !== "waived");
  const ready = !!state?.version && required.length >= gate.required && !waiting.length && !policies?.some((row) => row.status !== "signed_off" && row.status !== "waived");
  return { ...gate, reviewers, policies, signed: required.length - waiting.length,
    state: gate.state === "closed" || gate.state === "conflict" ? gate.state : ready ? "ready" : "waiting",
    waiting_on: waiting.map((row) => row.sub ?? row.name ?? ""), reason: ready ? null : state?.version ? "Waiting for sign-offs on this version" : "Freeze a version before sign-off",
    unconfirmed: gate.unconfirmed ? { ...gate.unconfirmed, approval_held: false } : gate.unconfirmed,
  };
}
