/* What the visuals' screens ask of the daemon: the slice of DaemonClient they use, so a test hands
   them a typed fake of just this (design/visualizer-build-plan.html §4, the wave-1 contract). */

import type { DaemonClient } from "../../../api/client";

export type VisualsClient = Pick<DaemonClient, "visuals" | "answerVisual" | "dismissVisual" | "keepVisual" | "unkeepVisual" | "markVisualRead">;

/* A planning doc's reads and the person's verdict (brindle/plans.py). */
export type PlansClient = Pick<DaemonClient, "plan" | "decidePlan">;
/* What the person writes on the design doc (brindle/plan_docs.py): comments and replies,
   suggestions and their answers, direct edits, and their pick on a decision. */
export type PlanDocClient = Pick<DaemonClient, "commentPlan" | "replyPlan" | "resolvePlanThread" | "suggestPlan" | "acceptPlanSuggestion" | "rejectPlanSuggestion" | "editPlan" | "confirmPlan">;

/* Team review through the Library (brindle/pro/shared_docs.py). Not every build of the screen has it. */
export type PlanSharingClient = Pick<DaemonClient, "sharePlan" | "setPlanReviewers" | "syncPlan" | "signOffPlan" | "overridePlanPolicy" | "libraryPeople" | "docScopes"> &
  Partial<Pick<DaemonClient, "askPlanPolicyAgain" | "previewPlanPolicies" | "stopSharingPlan">>;

/* A design someone else shared, read and reviewed from the Library (brindle/pro/shared_reader.py). */
export type SharedDesignClient = Pick<DaemonClient, "sharedDesign" | "writeSharedDesign" | "libraryPeople">;

export type KeptVisualsClient = Pick<DaemonClient, "keptVisuals">;

/* An id as the engine makes them (v_8f2c): letters, digits, _ and -, nothing a path could use. */
const ID = /^[A-Za-z0-9_-]{1,64}$/;

/* The visual a chat link names: brindle://visual/<id>, nothing after it. */
export function visualLink(uri: string): string | null {
  const m = /^brindle:\/\/visual\/([^/?#\s]+)\/?$/i.exec(uri.trim());
  return m && ID.test(m[1]) ? m[1] : null;
}
