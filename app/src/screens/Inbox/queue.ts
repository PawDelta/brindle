/* The Inbox as a queue you clear (design: inbox-redesign.html, owner-approved):
   which part of the Inbox each item belongs in, in what order, which items
   are one decision and so one row, and the person's triage of the day (Done,
   Later, Put back), kept in this window and its storage.

   Two parts: Waiting on you (an agent or a person is stopped until the person
   acts) and From the team (nothing is stopped). The engine says which
   (InboxItemDTO.inbox_group, inbox_team.group); an item from an engine that
   doesn't yet, by the same kinds. */

import type { InboxItemDTO } from "../../api/schema";
import { itemKey } from "./items";

export type Part = "wait" | "team";

/* inbox_team.TEAM_KINDS: rows that ask nothing of the person. */
const TEAM_KINDS = ["signoff_progress", "shared_doc_comment", "signed_off", "changes_requested", "org_notice"];

export const partOf = (item: InboxItemDTO): Part =>
  item.inbox_group ? (item.inbox_group === "from_team" ? "team" : "wait") : TEAM_KINDS.includes(item.kind) ? "team" : "wait";

/* What's stopped comes first: an agent stopped on the person, then a sign-in or setup that stops work,
   then what a teammate or brindle is waiting on. Kinds not named keep the engine's order, last. */
const WAIT_ORDER = [
  "question", "waiting", "handover", "provider_switch", "provider_switch_back", "plan_offer", "credentials", "login", "setup",
  "proposed_checks", "plan", "visual", "review_requested", "review", "merge_failed", "pr_ready", "pr_failed", "pr_opened", "doc",
];
const rank = (kind: string) => {
  const at = WAIT_ORDER.indexOf(kind);
  return at < 0 ? WAIT_ORDER.length : at;
};

/* One row of the queue: one item, or several that are one decision (a teammate's review requests, a
   design's comment threads, an admin's notices). `key` is the first item's itemKey, or the group's. */
export interface Row {
  key: string;
  part: Part;
  kind: string;
  items: InboxItemDTO[];
}

const personKey = (item: InboxItemDTO) => item.from_person?.sub ?? item.from_person?.name ?? "";

/* The rows of one part, in order. Comment threads on one design are one row (its threads inside);
   two or more review requests from one person, or notices from the org, are one row as well. */
export function rowsOf(items: InboxItemDTO[], part: Part): Row[] {
  const mine = items.filter((i) => partOf(i) === part);
  const ordered = part === "wait" ? mine.map((item, at) => ({ item, at })).sort((a, b) => rank(a.item.kind) - rank(b.item.kind) || a.at - b.at).map((x) => x.item) : mine;
  const rows: Row[] = [];
  const byGroup = new Map<string, Row>();
  for (const item of ordered) {
    const group = item.kind === "shared_doc_comment" ? `thread:${item.doc ?? ""}`
      : item.kind === "review_requested" && personKey(item) ? `asks:${personKey(item)}`
      : item.kind === "org_notice" ? "notices" : null;
    const found = group ? byGroup.get(group) : undefined;
    if (found) {
      found.items.push(item);
      continue;
    }
    const row: Row = { key: itemKey(item), part, kind: item.kind, items: [item] };
    rows.push(row);
    if (group) byGroup.set(group, row);
  }
  for (const row of rows) if (row.items.length > 1 && row.kind !== "shared_doc_comment") row.key = `group:${row.kind}:${personKey(row.items[0])}`;
  return rows;
}

/* A row's team keys (POST /inbox/read), and whether any of them is unread. */
export const teamKeys = (row: Row) => row.items.map((i) => i.item_key).filter((k): k is string => !!k);
export const unread = (row: Row) => row.items.some((i) => i.read === false);

/* What a row shows, to tell a row the person cleared from the same row changed since (new comments, another sign-off). */
export const rowSig = (row: Row) => JSON.stringify(row.items.map((i) => [i.item_key, i.reason, i.excerpt, i.signoff?.signed, i.detail, i.request_id]));

/* The kinds, as tabs once the queue holds more than eight items. */
export const TABS: { key: string; label: string; kinds: string[] }[] = [
  { key: "questions", label: "Questions", kinds: ["question", "waiting"] },
  { key: "permissions", label: "Permissions", kinds: ["permission"] },
  { key: "reviews", label: "Reviews", kinds: ["review_requested", "review", "merge_failed", "changes_requested"] },
  { key: "prs", label: "PRs", kinds: ["pr_ready", "pr_failed", "pr_opened"] },
  { key: "plans", label: "Plans", kinds: ["plan", "plan_offer", "signoff_progress", "signed_off", "visual"] },
  { key: "comments", label: "Comments", kinds: ["shared_doc_comment", "doc"] },
  { key: "notices", label: "Notices", kinds: ["org_notice", "org_blocked"] },
];
export const TABS_FROM = 8;

/* How long ago, as the queue says it. */
export function ageOf(ms: number): string {
  const s = Math.max(0, ms / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  if (s < 2 * 86400) return "yesterday";
  return `${Math.floor(s / 86400)} d`;
}

/* An agent's wait past thirty minutes: its age turns orange. */
export const LONG_WAIT_MS = 30 * 60 * 1000;

/* ---- The person's triage, kept for the day in this window's storage ---- */

/* One line of Done today: what was done (Answered, Opened PR, Read, Done, Set aside), on what, where, when,
   and whether Put back brings the row back (a row set aside or cleared here; a dismissal sent to the engine
   is not undone). */
export interface DoneEntry {
  key: string;
  what: string;
  text: string;
  where: string;
  at: number;
  back: boolean;
}

interface Triage {
  day: string;
  done: DoneEntry[];
  /* Rows set aside (Later): until when, or until the row changes (`sig`). */
  later: Record<string, { until: number; sig?: string }>;
  /* Team rows cleared with Done, as they read when cleared: a change brings them back. */
  cleared: Record<string, string>;
}

const STORE = "brindle.inbox.triage";
const today = (now = Date.now()) => new Date(now).toDateString();
/* Read from storage on first use, not at import. */
let triage: Triage | null = null;
const listeners = new Set<() => void>();

function load(): Triage {
  const empty: Triage = { day: today(), done: [], later: {}, cleared: {} };
  try {
    const raw = localStorage.getItem(STORE);
    if (!raw) return empty;
    const read = JSON.parse(raw) as Partial<Triage>;
    // A new day starts an empty Done today; what was set aside stays set aside.
    return { ...empty, later: read.later ?? {}, cleared: read.cleared ?? {}, done: read.day === empty.day ? read.done ?? [] : [] };
  } catch {
    return empty;
  }
}

function save(next: Triage) {
  triage = next;
  try {
    localStorage.setItem(STORE, JSON.stringify(next));
  } catch {
    // Storage blocked: the triage lasts as long as this window.
  }
  for (const l of listeners) l();
}

export const triageState = (): Triage => {
  triage ??= load();
  if (triage.day !== today()) triage = { ...triage, day: today(), done: [] };
  return triage;
};
export const onTriage = (l: () => void) => {
  listeners.add(l);
  return () => void listeners.delete(l);
};

export function recordDone(entry: DoneEntry) {
  const t = triageState();
  save({ ...t, done: [entry, ...t.done.filter((d) => d.key !== entry.key || d.what !== entry.what)] });
}

export function setAside(key: string, until: number, sig?: string) {
  const t = triageState();
  save({ ...t, later: { ...t.later, [key]: { until, ...(sig === undefined ? {} : { sig }) } } });
}

export function clearRow(key: string, sig: string) {
  const t = triageState();
  save({ ...t, cleared: { ...t.cleared, [key]: sig } });
}

/* Put back: the row set aside or cleared comes back, and leaves Done today. */
export function putBack(key: string) {
  const t = triageState();
  const { [key]: _l, ...later } = t.later;
  const { [key]: _c, ...cleared } = t.cleared;
  save({ ...t, later, cleared, done: t.done.filter((d) => !(d.key === key && d.back)) });
}

/* A row the person set aside or cleared, as it reads now. */
export function hiddenByTriage(row: Row, now = Date.now()): boolean {
  const t = triageState();
  const later = t.later[row.key];
  if (later && (later.sig !== undefined ? later.sig === signoffSig(row) : now < later.until)) return true;
  return t.cleared[row.key] !== undefined && t.cleared[row.key] === rowSig(row);
}

/* A sign-off row's progress: Later "when the last sign-off lands" lasts while it reads the same. */
export const signoffSig = (row: Row) => row.items.map((i) => `${i.signoff?.signed ?? ""}/${i.signoff?.required ?? ""}`).join(",");

/* The times Later offers, from now. */
export function laterTimes(now = Date.now()): { label: string; at: number; time: string }[] {
  const d = new Date(now);
  const clock = (t: number) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const hour = now + 60 * 60 * 1000;
  const afternoon = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 15).getTime();
  const morning = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1, 9).getTime();
  return [
    { label: "In an hour", at: hour, time: clock(hour) },
    ...(afternoon > hour ? [{ label: "This afternoon", at: afternoon, time: clock(afternoon) }] : []),
    { label: "Tomorrow morning", at: morning, time: clock(morning) },
  ];
}

/* Forget the day's triage (tests). */
export function resetTriage() {
  save({ day: today(), done: [], later: {}, cleared: {} });
}
