/* Which Library stage a design is at. A shared design carries its own review state (the
   `design` of GET /analysis-docs/{id}: ready, reviewers, pending reviewers and policies):
   not ready is "in review", ready is "approved". Only the personal queue (the Inbox's
   review_requested / changes_requested cards, service/inbox.py) says "needs you". The server's
   list has no stage field, so a design with neither has no stage, and Building and Shipped are
   not derived. The filter and the rail count use these same functions on the same documents. */

import type { AnalysisDoc, AnalysisDocDetail, DaemonClient } from "../../api/client";
import type { InboxItemDTO } from "../../api/schema";
import type { LibraryStage } from "../../routes";

export type DesignReview = NonNullable<AnalysisDocDetail["design"]>;

/* The designs the person's own queue asks about, by library doc id. */
export function docStages(inbox: InboxItemDTO[]): Map<string, LibraryStage> {
  const stages = new Map<string, LibraryStage>();
  for (const item of inbox) {
    if (item.doc == null) continue;
    const id = String(item.doc);
    if (item.kind === "review_requested" || item.kind === "changes_requested") stages.set(id, "needs-you");
    else if (item.kind === "signed_off" && !stages.has(id)) stages.set(id, "approved");
  }
  return stages;
}

export function stageOfDoc(id: string, queue: Map<string, LibraryStage>, design?: DesignReview | null): LibraryStage | undefined {
  if (queue.get(id) === "needs-you") return "needs-you";
  if (design) {
    if (design.ready) return "approved";
    if (design.ready === false || design.reviewers?.length || design.pending_reviewers?.length || design.pending_policies?.length) return "in-review";
  }
  return queue.get(id);
}

/* The review state of each listed design that has one (a personal design has none). A failed read is skipped. */
export async function loadDesigns(client: Pick<DaemonClient, "doc">, docs: AnalysisDoc[], repo?: string): Promise<Map<string, DesignReview>> {
  const got = await Promise.all(docs.map((d) => client.doc(d.id, repo).then((x) => x.design ?? null, () => null)));
  const designs = new Map<string, DesignReview>();
  docs.forEach((d, i) => got[i] && designs.set(d.id, got[i]!));
  return designs;
}

/* The record's sign-off rows: everyone who recorded a sign-off (with its revision), even if not
   on the reviewer list, then the listed reviewers who have not signed. `signed` is for the revision on screen. */
export function signOffRows(design?: DesignReview | null): { sub: string; role: string; rev?: number; signed: boolean }[] {
  if (!design) return [];
  const role = (sub: string) => { const r = design.reviewers?.find((x) => x.sub === sub); return r ? (r.required === false ? "optional" : "required") : "signed off"; };
  // One row per recorded signature (a person may have signed several revisions), then a waiting row
  // for each listed reviewer with no signature on the revision on screen.
  const rows = (design.sign_offs ?? []).map((s) => ({ sub: s.sub, role: role(s.sub), rev: s.rev, signed: s.rev === undefined || s.rev === design.rev }));
  for (const r of design.reviewers ?? []) if (!rows.some((x) => x.sub === r.sub && x.signed)) rows.push({ sub: r.sub, role: role(r.sub), rev: undefined, signed: false });
  return rows;
}

/* The count of designs at each stage, over the documents given. */
export function stageCounts(docs: AnalysisDoc[], queue: Map<string, LibraryStage>, designs: Map<string, DesignReview>): Partial<Record<LibraryStage, number>> {
  const counts: Partial<Record<LibraryStage, number>> = {};
  for (const d of docs) {
    const stage = stageOfDoc(d.id, queue, designs.get(d.id));
    if (stage) counts[stage] = (counts[stage] ?? 0) + 1;
  }
  return counts;
}
