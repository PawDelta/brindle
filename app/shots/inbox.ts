/* The Inbox scenes (inbox-redesign.html's frames): the queue with what waits on the person and what the team
   did, the same with only team news, all clear, loading, and many items. The people and titles are the
   design's own; the agents are fixtures.ts's live session (sup-42). */

import type { DaemonClient } from "../src/api/client";
import type { InboxItemDTO, SharedDesignDTO } from "../src/api/schema";

const person = (sub: string, name: string) => ({ sub, name });
const ana = person("u_ana", "Ana Ruiz");
const chen = person("u_chen", "Chen Wei");
const sam = person("u_sam", "Sam Ortiz");

const question = (agent: string, ws: string, detail: string, id: string): InboxItemDTO =>
  ({ kind: "question", reason: "has a question", agent_id: agent, workspace_id: ws, root_id: "sup-42", detail, request_id: id, inbox_group: "needs_you" });
const review = (doc: string, detail: string, from = ana): InboxItemDTO =>
  ({ kind: "review_requested", reason: "Review requested", doc, detail, from_person: from, item_key: `review:${doc}`, read: false, inbox_group: "needs_you" });
const thread = (id: number, from: { sub: string; name: string }, reason: string, excerpt: string, doc = "d_hooks", detail = "Webhook delivery with retries"): InboxItemDTO =>
  ({ kind: "shared_doc_comment", reason, doc, detail, from_person: from, item_key: `comment:${doc}:${id}`, read: false, thread_id: id, excerpt, inbox_group: "from_team" });
const signoffs = (doc: string, detail: string, signed: number, required: number, by: { sub: string; name: string }, waiting: { sub: string; name: string }[], read: boolean): InboxItemDTO =>
  ({ kind: "signoff_progress", reason: `${signed} of ${required} signed off`, doc, detail, from_person: by, item_key: `signoffs:${doc}`, read, signoff: { rev: 2, signed, required, waiting }, inbox_group: "from_team" });
const notice = (id: string, reason: string): InboxItemDTO => ({ kind: "org_notice", reason, detail: "admin", notice_id: id, inbox_group: "from_team" });

const invite = question("a7aaa807", "pawdelta/desktop-ux-revamp", "Should an invite to an address that already has an account add them to the workspace right away, or still send the email?", "3");
const prReady: InboxItemDTO = { kind: "pr_ready", reason: "PR ready for your approval", agent_id: "sup-42", workspace_id: "pawdelta", root_id: "sup-42", detail: "Saved filters on the search page" };
const retries = [thread(31, chen, "3 comments", "Then we need a separate cap, or one bad endpoint retries forever."), thread(32, sam, "Commented", "Dead letters after the fifth try?")];
const billing = notice("n-21", "Plans that change billing now need two reviewers.");
const rateLimits = signoffs("d_rates", "Rate limits for the public API", 2, 3, chen, [sam], true);

/* The design's main frame: three waits, three team rows. */
export const inboxMain: InboxItemDTO[] = [invite, review("d_csv", "Add CSV export to reports"), prReady, ...retries, billing, rateLimits];

/* Nothing waiting, news only. */
export const inboxNews: InboxItemDTO[] = [...retries, billing, rateLimits];

/* Many items: more than eight, so tabs by kind; one teammate's three review requests are one row, two notices one. */
export const inboxMany: InboxItemDTO[] = [
  question("c58d1e77", "pawdelta/docs-readme", "Which currency should the totals round in: the invoice's or the account's?", "4"),
  invite,
  question("sup-42", "pawdelta", "The staging key is read-only. Use the sandbox account for the delivery test instead?", "5"),
  review("d_csv", "Add CSV export to reports"), review("d_audit", "Audit log retention"), review("d_share", "Report sharing links"),
  review("d_prorate", "Proration rules for plan changes", sam), review("d_dlq", "Dead-letter queue for failed webhooks", chen),
  prReady,
  ...retries,
  thread(41, ana, "Commented", "Email is fine, but say so before the click.", "d_csv", "Add CSV export to reports"),
  signoffs("d_auditx", "Audit log export", 1, 2, sam, [ana], false),
  billing, notice("n-22", "Codex is off for acme-api until Friday."),
  rateLimits,
];

/* The day so far, as Done today lists it. */
export function seedDoneToday(): void {
  const at = (h: number, m: number) => new Date(new Date().setHours(h, m, 0, 0)).getTime();
  localStorage.setItem("brindle.inbox.triage", JSON.stringify({
    day: new Date().toDateString(),
    later: { "proposed_checks:sup-0:x": { until: at(23, 59) } },
    cleared: {},
    done: [
      { key: "q:1", what: "Answered", text: "Round in the invoice's currency, not the account's.", where: "Checkout totals", at: at(10, 42), back: false },
      { key: "pr:1", what: "Opened PR", text: "Totals rounding fix", where: "acme-web", at: at(10, 31), back: false },
      { key: "q:2", what: "Answered", text: "Yes, drop it. Nothing calls it since March.", where: "Webhook delivery with retries", at: at(9, 58), back: false },
      { key: "proposed_checks:sup-0:x", what: "Set aside", text: "Proposed checks for acme-api", where: "acme-api", at: at(9, 40), back: true },
    ],
  }));
}

/* The Library's threads for the comment rows, when one is opened. */
export function installThreads(client: DaemonClient): void {
  const design = {
    id: "d_hooks", rev: 2, title: "Webhook delivery with retries",
    threads: [
      { id: 31, rev: 2, kind: "comment", status: "open", at: Date.now() / 1000 - 35 * 60, author: "Chen Wei", text: "Should the resend budget count against the org limit?", anchor: { kind: "text", quote: "Resend up to 5 times with backoff, inside the org's request budget." },
        replies: [
          { id: 1, at: Date.now() / 1000 - 31 * 60, author: "Sam Ortiz", text: "I'd say no. A resend is our delivery failing, not their traffic." },
          { id: 2, at: Date.now() / 1000 - 28 * 60, author: "Chen Wei", text: "Then we need a separate cap, or one bad endpoint retries forever. Which do you want?" },
        ] },
    ],
  } as unknown as SharedDesignDTO;
  Object.assign(client, { sharedDesign: async () => design });
}
