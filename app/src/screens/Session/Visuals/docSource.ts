/* Where the doc on screen (PlanReader.tsx) gets its content and sends what the person writes: one
   interface, so the screen doesn't care which it is. Two sources today: the author's plan on this
   machine, and a design someone else shared, read from the Library by its doc id. */

import { DaemonError, type Plan, type PlanAnchor, type PlanChange, type SharedDesign } from "../../../api/client";
import type { PlanDocClient, PlansClient, SharedDesignClient } from "./client";
import type { HistoryClient } from "./DocHistory";

export interface DocSource<P extends Plan = Plan> {
  /* The doc's own id there: a plan's, or a Library doc's. Anything kept about a doc is keyed by it. */
  readonly id: string;
  history?: HistoryClient;
  read(): Promise<P>;
  comment(rev: number, anchor: PlanAnchor, text: string, question: boolean): Promise<P>;
  reply(rev: number, thread: number, text: string): Promise<P>;
  resolve(rev: number, thread: number, resolved: boolean): Promise<P>;
  suggest(rev: number, change: PlanChange, text?: string): Promise<P>;
  vote(rev: number, decision: string, option: string): Promise<P>;
  /* Reviewing someone else's design: this person's own sign-off. */
  signOff?(rev: number, version?: number): Promise<P>;
  /* Whether a failed read means this person can no longer open the doc, so it leaves the screen. */
  lost?(e: unknown): boolean;
}

export function planSource(client: PlansClient & PlanDocClient, id: string): DocSource {
  return {
    id,
    history: client.planHistory && client.planRevisionChanges ? {
      planHistory: (planId) => client.planHistory!(planId),
      planRevisionChanges: (planId, rev) => client.planRevisionChanges!(planId, rev),
    } : undefined,
    read: () => client.plan(id),
    comment: (rev, anchor, text, question) => client.commentPlan(id, rev, anchor, text, question),
    reply: (_rev, thread, text) => client.replyPlan(id, thread, text),
    resolve: (_rev, thread, resolved) => client.resolvePlanThread(id, thread, resolved),
    suggest: (rev, change, text) => client.suggestPlan(id, rev, change, text),
    vote: (rev, decision, option) => client.confirmPlan(id, rev, decision, option),
  };
}

/* Refused outright, signed out, or the org's key revoked: the Library won't show it any more. */
const accessGone = (e: unknown) => e instanceof DaemonError && ([401, 402, 403, 404].includes(e.status) || e.code === "key_unavailable");

export function sharedSource(client: SharedDesignClient, id: string, repo?: string): DocSource<SharedDesign> {
  const send = (tail: string, body: Record<string, unknown>) => client.writeSharedDesign(id, tail, repo ? { ...body, repo } : body);
  return {
    id,
    history: client.sharedDesignHistory && client.sharedDesignRevisionChanges ? {
      planHistory: (docId) => client.sharedDesignHistory!(docId, repo),
      planRevisionChanges: (docId, rev) => client.sharedDesignRevisionChanges!(docId, rev, repo),
    } : undefined,
    read: () => client.sharedDesign(id, repo),
    comment: (rev, anchor, text, question) => send("comments", { rev, anchor, text, ...(question ? { question } : {}) }),
    reply: (rev, thread, text) => send("comments", { rev, parent: thread, text }),
    resolve: (rev, thread, resolved) => send(`threads/${thread}`, { rev, action: resolved ? "resolve" : "reopen" }),
    suggest: (rev, change, text = "") => send("suggestions", { rev, ...change, text }),
    vote: (rev, decision, option) => send("votes", { rev, decision, option }),
    signOff: (rev, version) => send("sign-offs", { rev, ...(version === undefined ? {} : { version }) }),
    lost: accessGone,
  };
}
