/* Which Library stage a design is at, from what the Inbox already knows (the review queue and
   shared-doc gate, service/inbox.py): a design someone asked the person to review, or that came
   back with changes, "needs you"; one every required reviewer signed is "approved". The server's
   list has no stage field, so a design the Inbox says nothing about has no stage here and the
   Building and Shipped stages are not counted. */

import type { InboxItemDTO } from "../../api/schema";
import type { LibraryStage } from "../../routes";

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

/* The count of designs at each stage the Inbox places, for the rail. */
export function stageCounts(stages: Map<string, LibraryStage>): Partial<Record<LibraryStage, number>> {
  const counts: Partial<Record<LibraryStage, number>> = {};
  for (const stage of stages.values()) counts[stage] = (counts[stage] ?? 0) + 1;
  return counts;
}
