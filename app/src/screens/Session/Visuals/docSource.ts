/* Where the doc on screen (PlanReader.tsx) gets its content and sends what the person writes: one
   interface, so the screen doesn't care which it is. Two sources today: the author's plan on this
   machine, and a design someone else shared, read from the Library by its doc id. */

import { DaemonError, type Plan, type PlanAnchor, type PlanChange, type SharedDesign } from "../../../api/client";
import type { PlanDocClient, PlansClient, PlanSharingClient, ShareLinkDesignClient, SharedDesignClient } from "./client";
import type { SharePeopleTarget } from "./SharePanel";
import type { HistoryClient } from "./DocHistory";

export interface DocSource<P extends Plan = Plan> {
  /* The doc's own id there: a plan's, or a Library doc's. Anything kept about a doc is keyed by it. */
  readonly id: string;
  history?: HistoryClient;
  people?: SharePeopleTarget;
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

export function planSource(client: PlansClient & PlanDocClient & Partial<PlanSharingClient>, id: string): DocSource {
  return {
    id,
    people: client.planPeople && client.planAccess && client.invitePlanPerson && client.setPlanPersonAccess && client.setPlanPersonSignOff
      && client.removePlanPerson && client.removePlanScope ? {
      search: (query) => client.planPeople!(id, query),
      access: () => client.planAccess!(id),
      invite: (sub, access, signOff) => client.invitePlanPerson!(id, sub, access, signOff),
      change: (sub, access, signOff) => client.setPlanPersonAccess!(id, sub, access, signOff),
      signOff: (sub, must) => client.setPlanPersonSignOff!(id, sub, must),
      revoke: (sub) => client.removePlanPerson!(id, sub),
      revokeScope: (scope) => client.removePlanScope!(id, scope),
      shareScope: client.sharePlan && ((scope) => client.sharePlan!(id, scope)),
      stopAll: client.unsharePlan && (() => client.unsharePlan!(id)),
    } : undefined,
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

/* A design opened from its share link (lib/deeplink.ts): read through the link, as the web page reads it,
   and read only. Its id is the daemon's (a hash of the token, never the token). */
export function linkSource(client: ShareLinkDesignClient, token: string, id: string): DocSource<SharedDesign> {
  const readOnly = () => Promise.reject(new Error("A plan opened from a link is read only here."));
  return {
    id,
    read: () => client.shareLinkDesign(token),
    comment: readOnly,
    reply: readOnly,
    resolve: readOnly,
    suggest: readOnly,
    vote: readOnly,
    lost: accessGone,
  };
}

export function sharedSource(client: SharedDesignClient, id: string, repo?: string): DocSource<SharedDesign> {
  const send = (tail: string, body: Record<string, unknown>) => client.writeSharedDesign(id, tail, repo ? { ...body, repo } : body);
  return {
    id,
    people: client.designPeople && client.designAccess && client.inviteDesignPerson && client.setDesignPersonAccess && client.setDesignPersonSignOff
      && client.removeDesignPerson && client.removeDesignScope ? {
      search: (query) => client.designPeople!(id, query, repo),
      access: () => client.designAccess!(id, repo),
      invite: (sub, access, signOff) => client.inviteDesignPerson!(id, sub, access, signOff, repo),
      change: (sub, access, signOff) => client.setDesignPersonAccess!(id, sub, access, signOff, repo),
      signOff: (sub, must) => client.setDesignPersonSignOff!(id, sub, must, repo),
      revoke: (sub) => client.removeDesignPerson!(id, sub, repo),
      revokeScope: (scope) => client.removeDesignScope!(id, scope, repo),
    } : undefined,
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
