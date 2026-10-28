/* What the app knows about each inbox item kind (service/inbox.py items):
   its heading, what it asks of the person, the id its actions are posted
   under, and which action answers it first. */

import { LOGIN_PROVIDERS, type InboxAction, type LoginProvider } from "../../api/client";
import type { InboxItemDTO } from "../../api/schema";
import { providerLabel } from "../../api/providers";

/* The provider a credentials item is about: the sign-in route the engine
   names on the item (`action`: POST /v1/providers/{p}/login, engine branch
   desktop/permission-items), else the CLI its detail names ("claude
   /login", "codex login"), else the agent's own (`provider`: its record's),
   else Claude Code. */
export function loginProvider(item: InboxItemDTO, provider?: string | null): LoginProvider {
  const known = (name: string | null | undefined): name is LoginProvider => (LOGIN_PROVIDERS as readonly string[]).includes(name ?? "");
  const routed = item.action?.path.match(/^(?:\/v1)?\/providers\/([^/]+)\/login$/)?.[1];
  if (known(routed)) return routed;
  const named = (item.detail ?? "").trim().split(/\s+/)[0];
  if (known(named)) return named;
  if (named === "agy") return "antigravity";
  return known(provider) ? provider : "claude";
}

export const KIND_LABEL: Record<string, string> = {
  waiting: "Waiting for you",
  permission: "Permission",
  question: "Question",
  plan: "Plan to approve",
  review: "Branch to review",
  merge_failed: "Merge failed",
  pr_ready: "Pull request",
  pr_failed: "Pull request",
  proposed_checks: "Proposed checks",
  credentials: "Credentials",
  login: "Cloud login",
  org_notice: "Org notice",
  setup: "Setup",
};

/* What the app can repair itself for a `setup` item, by its `fix` (engine
   M10, service/inbox._setup_items): the button's name, what pressing it
   does on the engine, and one line on the card saying so. The route is
   the engine's (POST /v1/setup/codex-hook), a job the card follows. */
export const FIXES: Record<string, { label: string; help: string; route: "setupCodexHook" }> = {
  codex_hook_trust: {
    label: "Fix",
    help: "Fix makes Codex trust brindle's permission hook again, so Codex workers' permission requests reach the Inbox instead of stopping their terminals.",
    route: "setupCodexHook",
  },
};

/* A setup item with a fix the app has a button for. */
export const fixable = (item: InboxItemDTO): boolean => item.kind === "setup" && !!item.fix && item.fix in FIXES;

export const kindLabel = (item: InboxItemDTO) => KIND_LABEL[item.kind] ?? item.kind;

/* A short word for the kind, as a pill. */
export const KIND_SHORT: Record<string, string> = {
  waiting: "waiting",
  permission: "permission",
  question: "question",
  plan: "plan",
  review: "review",
  merge_failed: "merge",
  pr_ready: "PR",
  pr_failed: "PR",
  proposed_checks: "checks",
  credentials: "login",
  login: "login",
  org_notice: "notice",
  setup: "setup",
};

/* What is being asked, in a line the person can decide on. */
export function describeItem(item: InboxItemDTO): string {
  switch (item.kind) {
    case "waiting":
      return "Waiting on your input in its terminal";
    case "permission": {
      // The request whole where the hook recorded it; else the prompt's own words.
      const p = item.permission;
      if (p?.target) return `Asks to run ${p.tool ? `${p.tool}: ` : ""}${p.target}`;
      return item.detail?.trim() || "Asks your permission";
    }
    case "question":
      return "Has a question for you";
    case "plan":
      return "Plan ready for your approval";
    case "review":
      return item.reason === "to review" ? "Branch ready to review" : `Branch ${item.reason}`;
    case "merge_failed":
      return item.reason;
    /* brindle prepared the session's PR (service/inbox._pr_items): the person's Open PR is the only push. */
    case "pr_ready":
      return "PR ready for your approval";
    case "pr_failed":
      return item.reason;
    case "proposed_checks": {
      const n = (item.detail ?? "").split("\n").filter(Boolean).length;
      return `Proposes ${n === 1 ? "a check command" : `${n} check commands`} for the repo (below)`;
    }
    case "credentials":
      return `${providerLabel(loginProvider(item))} needs a sign-in`;
    case "login":
      return "brindle's cloud sign-in expired";
    case "org_notice":
      return item.reason;
    case "setup":
      // The engine's own words: what is wrong with brindle's setup on this computer.
      return item.reason;
    default:
      return item.reason;
  }
}

/* The inbox action that answers an item (POST /inbox/{id}/{action}); null
   when it is settled another way (a sign-in, in a terminal of the daemon's). */
export function primaryAction(item: InboxItemDTO): InboxAction | null {
  switch (item.kind) {
    case "question":
    case "waiting":
      return "answer";
    case "plan":
      return "approve";
    case "review":
      return "review";
    case "merge_failed":
      return "merge";
    case "proposed_checks":
      return "accept";
    case "org_notice":
      return "read";
    default:
      return null;
  }
}

/* A permission request the app can answer in place: the engine offers the
   provider's choices and names no reason it must be answered in the agent's
   own terminal (PermissionDTO.terminal). Else the way to it is "Go to it". */
export const answerable = (item: InboxItemDTO): boolean =>
  item.kind === "permission" && !!item.permission && !item.permission.terminal && (item.permission.choices ?? []).length > 0;

/* What a permission asks, in one readable line: the tool, then the command
   or path in mono, from the request the hook recorded (`target`), which is
   what a choice answers; or, for a prompt only the agent's terminal can
   take, the prompt's own words (its "$ command" line in mono). `whole` is
   the request in full: a choice is never made on a line the person can't
   read whole. */
export function permissionLine(item: InboxItemDTO): { lead: string | null; code: string | null; whole: string } {
  const p = item.permission;
  if (p?.target) return { lead: p.tool ? `${p.tool}` : null, code: p.target, whole: `${p.tool ? `${p.tool}: ` : ""}${p.target}` };
  const words = (p?.wording ?? item.detail ?? "").trim();
  const first = words.split("\n").map((l) => l.trim()).find((l) => l && !/^[╭╰│─]/.test(l)) ?? describeItem(item);
  // The prompt's own command line, only where no choice is offered here (the answer goes in at its prompt).
  const code = answerable(item) ? undefined : words.split("\n").map((l) => l.trim()).find((l) => l.startsWith("$ "));
  return { lead: code ? first : null, code: code ? code.slice(2) : null, whole: words || first };
}

/* The body POST /inbox/{id}/permission takes: the request exactly as shown, and the choice. */
export const permissionAnswer = (item: InboxItemDTO, choice: string): Record<string, unknown> => ({
  request: item.permission?.request_id,
  digest: item.permission?.digest ?? null,
  choice,
});

/* An item the person can complete where it is listed (a permission answered
   from its choices, a provider's or brindle's own sign-in started), rather
   than in the review sheet. */
export const completable = (item: InboxItemDTO): boolean => item.kind === "credentials" || item.kind === "login" || answerable(item) || fixable(item);

/* An item that needs a decision, not just a reply: the review sheet opens
   on it when its session is opened from the inbox. */
export const needsDecision = (item: InboxItemDTO): boolean => ["plan", "review", "merge_failed", "proposed_checks"].includes(item.kind);

/* The id POST /inbox/{id}/{action} takes: an org notice's notice_id, else the agent's. */
export const actionId = (item: InboxItemDTO) => (item.kind === "org_notice" ? item.notice_id : item.agent_id) ?? "";

/* Stable across snapshots, so a new item can be told from one already seen.
   A setup item belongs to no agent: it is told by what it fixes. */
export const itemKey = (item: InboxItemDTO) => `${item.kind}:${item.kind === "setup" ? (item.fix ?? "") : actionId(item)}:${item.workspace_id ?? ""}`;

/* The groups the inbox can be filtered to, by kind. */
export const FILTERS: { key: string; label: string; kinds: string[] }[] = [
  { key: "questions", label: "Questions", kinds: ["question", "waiting"] },
  { key: "permissions", label: "Permissions", kinds: ["permission"] },
  { key: "plans", label: "Plans", kinds: ["plan"] },
  { key: "reviews", label: "Reviews", kinds: ["review", "merge_failed"] },
  { key: "checks", label: "Checks", kinds: ["proposed_checks"] },
  { key: "other", label: "Other", kinds: ["credentials", "login", "org_notice", "setup"] },
];

export const filterOf = (item: InboxItemDTO) => FILTERS.find((f) => f.kinds.includes(item.kind))?.key ?? "other";

/* When each item was first seen by this window (the daemon serves no time):
   the age shown is how long it has been waiting here. */
const firstSeen = new Map<string, number>();

export function seenAt(item: InboxItemDTO, now = Date.now()): number {
  const key = itemKey(item);
  const at = firstSeen.get(key);
  if (at !== undefined) return at;
  firstSeen.set(key, now);
  return now;
}

/* Forget items no longer in the inbox, so a returning one counts as new. */
export function forgetGone(inbox: InboxItemDTO[]): void {
  const keys = new Set(inbox.map(itemKey));
  for (const key of firstSeen.keys()) if (!keys.has(key)) firstSeen.delete(key);
}
