/* The Inbox as a queue you clear (design inbox-redesign.html, approved by the
   owner as is): what is stopped until the person acts comes first, under
   Waiting on you, and the first of those is open with its answer box; what
   the team did that stops nothing is under From the team. Every row is two
   lines (the thing itself over who did what) with its age and one action:
   Answer, Review, Open PR, Reply, Mark read, Open plan. One row is open at a
   time: it holds the answer box, a comment thread with its reply box, or a
   short preview, and the screen's one green action.

   Triage: Done (E) clears a row (on a row waiting on the person, today's
   Dismiss; on a team row, read and out of the way until it changes), Later
   (H) sets it aside until a time, Put back (U) brings back what was set
   aside or cleared here. J and K move the open row, Enter sends or opens.
   An acted-on row settles to one grey line where it stood, so nothing jumps;
   the day's lines live under Done today, folded away.

   Read state is the person's (the Library keeps it, POST /v1/inbox/read):
   an unread team row has a dot and a bolder title, and opening it, or Done,
   marks it read on every machine. */

import { useEffect, useReducer, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import type { DaemonClient, LibraryPerson } from "../../api/client";
import type { OpenUrl } from "../../api/open";
import type { InboxItemDTO, PersonDTO, PlanThreadDTO, SharedDesignDTO } from "../../api/schema";
import { CheckIcon, ChevronRightIcon, ClockIcon, FileIcon, GearIcon, LockIcon } from "../../components/icons";
import type { Route } from "../../routes";
import { displayName } from "../Session/Visuals/PlanCards";
import { describeItem, itemKey, kindLabel, opensVisual, seenAt, seenKey, REVIEW_KINDS } from "./items";
import {
  LONG_WAIT_MS, TABS, TABS_FROM, ageOf, clearRow, hiddenByTriage, laterTimes, onTriage, putBack, recordDone, rowSig, rowsOf, setAside, signoffSig,
  teamKeys, triageState, unread, type DoneEntry, type Part, type Row,
} from "./queue";
import { OpenPr, itemControls, itemDetail } from "./rowActions";
import type { InboxActions } from "./useInboxActions";

/* What the queue reads about an item's agent, session and repo, and where it can go from a row. */
export interface QueueContext {
  who(item: InboxItemDTO): string | null;
  branch(item: InboxItemDTO): string | null;
  sessionTitle(item: InboxItemDTO): string | null;
  /* The repo root a sign-in or a shared design is read in, and the repo's name for a row. */
  repo(item: InboxItemDTO): string | null;
  repoName(item: InboxItemDTO): string | null;
  /* The item's agent in its session (a visual decision: its reader); none until its session has loaded. */
  goTo(item: InboxItemDTO): (() => void) | undefined;
  /* Its session with Review & open PR over it. */
  review(item: InboxItemDTO): (() => void) | undefined;
  onNavigate?(route: Route): void;
  openUrl?: OpenUrl;
  /* Agents at work, and the sessions they are in, for the calm and all-clear lines. */
  working: { agents: number; sessions: number };
}

export interface InboxQueueProps {
  items: InboxItemDTO[];
  /* The first inbox has arrived: before it, the queue's places are drawn empty. */
  synced: boolean;
  /* Keys (seenKey) this page hasn't shown the person before. */
  fresh: Set<string>;
  actions: InboxActions;
  client: DaemonClient;
  refresh(): Promise<void>;
  ctx: QueueContext;
  /* What else waits on the person, drawn first in Waiting on you (permission requests), and how many. */
  waitingExtra?: ReactNode;
  waitingExtraCount?: number;
}

/* An agent's own wait: a square braid, its age orange past half an hour. */
const AGENT_KINDS = ["question", "waiting", "handover", "provider_switch", "provider_switch_back", "plan_offer", "idle_stopped", "failed", "review", "merge_failed", "plan", "visual", "proposed_checks", "sync_conflict"];
const STOPPED = ["question", "waiting"];
/* Kinds whose one action goes somewhere other than the item's session (a doc, a plan, GitHub). */
const ELSEWHERE = ["doc", "changes_requested", "signed_off", "pr_opened", "pr_failed"];
const PR_KINDS = ["pr_ready", "pr_failed", "pr_opened"];

/* A grey line where an acted-on row stood, until the person leaves the Inbox; `after` is the row that followed it. */
interface Line {
  key: string;
  /* The row as it read when acted on (queue.ts rowSig): the line stands for that, not for whatever comes next under the key. */
  sig: string;
  part: Part;
  entry: DoneEntry;
  meta: string | null;
  after: string | null;
}

const personName = (p: PersonDTO | null | undefined) => p?.name || "A teammate";
const initial = (name: string) => (name.trim()[0] ?? "?").toUpperCase();
const people = (row: Row) => [...new Map(row.items.filter((i) => i.from_person).map((i) => [i.from_person!.sub, personName(i.from_person)])).values()];
const joined = (parts: ReactNode[]) => parts.filter((p) => p !== null && p !== undefined && p !== false && p !== "").map((p, i) => <span key={i}>{i > 0 && <span className="iq-sep" />}{p}</span>);
const names = (list: string[]) => list.length <= 1 ? list.join("") : `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;

function titleOf(row: Row): string {
  const item = row.items[0];
  const n = row.items.length;
  switch (row.kind) {
    case "question":
      return item.detail?.trim() || describeItem(item);
    case "review_requested":
      return n > 1 ? `${n} plans to review from ${personName(item.from_person)}` : item.detail || "Plan";
    case "org_notice":
      return n > 1 ? `${n} notices from your ${item.detail || "admin"}` : item.reason;
    case "signoff_progress":
    case "shared_with_you":
    case "access_requested":
    case "shared_doc_comment":
    case "changes_requested":
    case "signed_off":
      return item.detail || "Plan";
    case "pr_ready":
      return item.detail?.trim() || describeItem(item);
    case "pr_opened":
    case "pr_failed":
      return item.detail?.trim() || item.reason;
    default:
      return opensVisual(item) && item.detail ? item.detail.split("\n")[0] : describeItem(item);
  }
}

const Braid = () => (
  <svg className="braid" viewBox="1.2 5.6 29.6 20.8" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" aria-hidden="true">
    <path d="M9 16H23" /><path d="M8 16C11 16 11.5 7.5 16 7.5C20.5 7.5 21 16 24 16" /><path d="M8 16C11 16 11.5 24.5 16 24.5C20.5 24.5 21 16 24 16" />
    <path strokeWidth="4.8" d="M3.8 16H9" /><path strokeWidth="4.8" d="M23 16H28.2" />
  </svg>
);
const line = (d: ReactNode) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{d}</svg>;
const PrGlyph = () => line(<><circle cx="7" cy="6" r="2.2" /><circle cx="7" cy="18" r="2.2" /><circle cx="17" cy="18" r="2.2" /><path d="M7 8.2v7.6M17 15.8V11a4 4 0 0 0-4-4h-2.5" /></>);
const Shield = () => line(<path d="M12 3.5l7 2.5v5.5c0 4.4-2.9 7.6-7 9-4.1-1.4-7-4.6-7-9V6z" />);

/* Colour only for state: people are round, agents and system rows square. */
function Avatar({ row }: { row: Row }) {
  const who = people(row);
  if (AGENT_KINDS.includes(row.kind)) return <span className="iq-av sq" data-av="agent"><Braid /></span>;
  if (PR_KINDS.includes(row.kind)) return <span className="iq-av sq" data-av="pr"><PrGlyph /></span>;
  if (row.kind === "org_notice" || row.kind === "org_blocked") return <span className="iq-av sq" data-av="org"><Shield /></span>;
  if (row.kind === "credentials" || row.kind === "login") return <span className="iq-av sq"><LockIcon /></span>;
  if (row.kind === "setup") return <span className="iq-av sq"><GearIcon /></span>;
  if (who.length >= 2) return <span className="iq-av pair" data-av="pair"><b>{initial(who[0])}</b><b>{initial(who[1])}</b></span>;
  if (who.length === 1) return <span className="iq-av" data-av="person">{initial(who[0])}</span>;
  if (row.kind === "doc" || REVIEW_KINDS.includes(row.kind)) return <span className="iq-av sq"><FileIcon /></span>;
  return <span className="iq-av sq"><Braid /></span>;
}

function Pips({ signed, required }: { signed: number; required: number }) {
  return <span className="iq-pips" data-testid="signoff-pips" data-signed={signed} data-required={required} aria-hidden="true">
    {Array.from({ length: Math.max(required, 0) }, (_, i) => <i key={i} className={i < signed ? "on" : ""} />)}
  </span>;
}

function decisionCount(item: InboxItemDTO) {
  return item.decision_count != null && <span data-decision-count={item.decision_count}>{item.decision_count} {item.decision_count === 1 ? "decision" : "decisions"}</span>;
}

function prFacts(item: InboxItemDTO): ReactNode[] {
  return [item.file_count != null && <span data-file-count={item.file_count}>{item.file_count} {item.file_count === 1 ? "file" : "files"}</span>,
    item.checks_state && <span data-checks={item.checks_state} className={item.checks_state === "passed" ? "iq-ok" : item.checks_state === "failed" ? "iq-wait" : undefined}>checks {item.checks_state}</span>];
}

function metaOf(row: Row, ctx: QueueContext): ReactNode[] {
  const item = row.items[0];
  const who = ctx.who(item);
  const session = ctx.sessionTitle(item);
  const branch = ctx.branch(item);
  switch (row.kind) {
    case "question":
      return [<><b>{who ?? "An agent"}</b> asks</>, session, branch && <span className="mono">{branch}</span>, <span className="iq-wait">paused until you answer</span>];
    case "waiting":
      return [<><b>{who ?? "An agent"}</b> waits at its prompt</>, session, branch && <span className="mono">{branch}</span>, <span className="iq-wait">paused until you answer</span>];
    case "review_requested":
      return row.items.length > 1 ? row.items.map((i) => <>{i.detail || "Plan"} {decisionCount(i)}</>) : [<><b>{personName(item.from_person)}</b> asked for your review</>, "plan", decisionCount(item)];
    case "shared_with_you":
      return [<><b>{personName(item.from_person)}</b> shared this plan with you</>];
    case "access_requested":
      return [<><b>{personName(item.from_person)}</b> asked to {item.access_role}</>];
    case "signoff_progress": {
      const s = item.signoff;
      const waiting = (s?.waiting ?? []).map(personName);
      return [<>{s && <Pips signed={s.signed} required={s.required} />}<b>{s ? `${s.signed} of ${s.required}` : item.reason}</b> signed off</>,
        item.from_person && `${personName(item.from_person)} signed`, waiting.length > 0 && `waiting on ${names(waiting)}`];
    }
    case "shared_doc_comment": {
      const n = row.items.length;
      const said = n > 1 ? `${n} threads` : item.reason === "Commented" ? "1 new comment" : item.reason === "Suggested" ? "a suggestion" : item.reason.replace(/comments$/, "new comments");
      return [<>{people(row).map((p, i, all) => <span key={p}>{i > 0 && (i === all.length - 1 ? " and " : ", ")}<b>{p}</b></span>)}</>, said, item.excerpt && `“${item.excerpt}”`];
    }
    case "org_notice":
      return row.items.length > 1 ? row.items.map((i) => <>{i.reason}{i.from_name && <span data-notice-sender> · {i.from_name}</span>}</>) : [<>From <b>your {item.detail || "admin"}</b></>, item.from_name && <span data-notice-sender>{item.from_name}</span>];
    case "provider_update":
      return [item.detail];
    case "pr_ready":
      return [item.checks_state === "running" ? <b>Preparing PR</b> : <><b>PR ready</b> to open</>, ctx.repoName(item), ...prFacts(item), branch && <span className="mono">{branch}</span>];
    case "pr_opened":
    case "pr_failed":
      return [<b>{item.reason}</b>, ctx.repoName(item), ...prFacts(item)];
    case "changes_requested":
    case "signed_off":
      return [item.from_person ? <><b>{personName(item.from_person)}</b> · {kindLabel(item).toLowerCase()}</> : kindLabel(item)];
    case "setup":
      return ["brindle on this computer", kindLabel(item)];
    case "org_blocked":
      return ["set by your organization"];
    default:
      return [who && <b>{who}</b>, session, branch && branch !== who && <span className="mono">{branch}</span>, kindLabel(item)];
  }
}

export function InboxQueue({ items: receivedItems, synced, fresh, actions, client, refresh, ctx, waitingExtra, waitingExtraCount = 0 }: InboxQueueProps) {
  const [members, setMembers] = useState<{ client: DaemonClient; people: LibraryPerson[] } | null>(null);
  useEffect(() => {
    let active = true;
    client.libraryPeople().then((got) => { if (active) setMembers({ client, people: got.people }); }, () => {});
    return () => { active = false; };
  }, [client]);
  const directory = members?.client === client ? members.people : [];
  const namedPerson = (person: PersonDTO | null | undefined) => {
    if (!person) return person;
    const member = directory.find((p) => p.sub === person.sub);
    return { ...person, name: displayName(member && { ...member, email: null }) || (person.name !== "A teammate" && person.name) || member?.email || null };
  };
  const items = receivedItems.map((item) => ({ ...item, from_person: namedPerson(item.from_person),
    signoff: item.signoff && { ...item.signoff, waiting: item.signoff.waiting?.map((person) => namedPerson(person)!) } }));
  const [, redraw] = useReducer((n: number) => n + 1, 0);
  useEffect(() => onTriage(redraw), []);
  const [tab, setTab] = useState("all");
  /* undefined: not chosen yet (the first waiting row opens once the inbox is read). */
  const [openKey, setOpenKey] = useState<string | null | undefined>(undefined);
  const [lines, setLines] = useState<Line[]>([]);
  const [menu, setMenu] = useState<string | null>(null);
  const [allDone, setAllDone] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const now = Date.now();

  const total = items.length + waitingExtraCount;
  const tabs = total > TABS_FROM;
  const kinds = tabs && tab !== "all" ? TABS.find((t) => t.key === tab)?.kinds ?? null : null;
  /* A grey line stands in for its row only while the row reads as it did when acted on: a new question from the
     same agent, a thread with new comments, is a row again (a line set aside defers to Later's own clock). */
  const settledAs = (row: Row) => lines.some((l) => l.key === row.key && l.sig === rowSig(row) && l.entry.what !== "Set aside");
  const shown = (row: Row) => !settledAs(row) && !hiddenByTriage(row, now) && !row.items.every((i) => actions.hidden.has(itemKey(i))) && (!kinds || kinds.includes(row.kind));
  const waitRows = rowsOf(items, "wait").filter(shown);
  const teamRows = rowsOf(items, "team").filter(shown);
  const order = [...waitRows, ...teamRows];
  const extraHere = !kinds || kinds.includes("permission");
  // A row that is back drops its line.
  const rowKeys = new Set(order.map((r) => r.key));
  const drawnLines = lines.filter((l) => !rowKeys.has(l.key));

  useEffect(() => {
    if (synced && openKey === undefined) setOpenKey(waitRows[0]?.key ?? null);
  }, [synced, openKey, waitRows]);

  /* What was set aside comes back when its time is up: drawn again at the next one due. */
  const due = Math.min(...Object.values(triageState().later).filter((l) => l.sig === undefined && l.until > now).map((l) => l.until));
  useEffect(() => {
    if (!Number.isFinite(due)) return;
    const timer = window.setTimeout(redraw, Math.min(due - Date.now() + 1, 2 ** 31 - 1));
    return () => window.clearTimeout(timer);
  }, [due]);

  /* Read for this person in the Library; false, with the reason on the row, when the Library didn't take it. */
  const markRowsRead = (rows: Row[]): Promise<boolean> => {
    const keys = rows.flatMap(teamKeys);
    if (keys.length === 0) return Promise.resolve(true);
    for (const row of rows) for (const item of row.items) actions.dismiss(itemKey(item));
    return client.markInboxRead(keys).then(() => {
      void refresh();
      return true;
    }, (e: unknown) => {
      actions.report(rows[0].items[0], e instanceof Error ? e.message : String(e));
      return false;
    });
  };
  const markRead = (row: Row) => markRowsRead([row]);

  /* The row settles to its grey line where it stood; an open row hands the open to the row after it. */
  const settle = (row: Row, entry: Omit<DoneEntry, "key" | "at">, meta: string | null = null) => {
    const part = row.part === "wait" ? waitRows : teamRows;
    const at = part.findIndex((r) => r.key === row.key);
    const after = at >= 0 ? part[at + 1]?.key ?? null : null;
    const done: DoneEntry = { ...entry, key: row.key, at: Date.now() };
    recordDone(done);
    setLines((ls) => [...ls.filter((l) => l.key !== row.key), { key: row.key, sig: rowSig(row), part: row.part, entry: done, meta, after }]);
    setOpenKey((k) => (k === row.key ? after : k));
    setMenu(null);
  };

  /* A team row's Done waiting for the Library, by row: a second press waits on the first, and a later triage
     (Put back, Later) takes its place, so a late answer no longer clears the row. (A waiting row's dismissals
     are deduplicated by useInboxActions.) */
  const pending = useRef(new Map<string, object>());
  const [doing, setDoing] = useState<ReadonlySet<string>>(new Set());
  const supersede = (key: string) => {
    if (!pending.current.delete(key)) return;
    setDoing((d) => new Set([...d].filter((k) => k !== key)));
  };

  const done = (row: Row) => {
    const title = titleOf(row);
    if (row.part === "team") {
      if (pending.current.has(row.key)) return;
      const token = {};
      pending.current.set(row.key, token);
      setDoing((d) => new Set(d).add(row.key));
      // Cleared only once the Library has it read; refused, the row stays with the reason.
      void markRead(row).then((ok) => {
        if (pending.current.get(row.key) !== token) return;
        supersede(row.key);
        if (!ok) return;
        clearRow(row.key, rowSig(row));
        settle(row, { what: "Done", text: title, where: "", back: true });
      });
      return;
    }
    // A row of several items settles once every dismissal is taken; one refused keeps its item, and its reason, in the row.
    let waiting = row.items.length;
    let refused = false;
    for (const item of row.items) {
      const pr = PR_KINDS.includes(item.kind);
      actions.run(item, pr ? "pr-not-now" : "dismiss", pr || item.kind === "doc" ? {} : { item }, (ok) => {
        refused ||= !ok;
        if (--waiting === 0 && !refused) settle(row, { what: "Done", text: title, where: ctx.sessionTitle(item) ?? "", back: false });
      });
    }
  };

  const later = (row: Row, until: number, sig?: string) => {
    supersede(row.key);
    setAside(row.key, until, sig);
    settle(row, { what: "Set aside", text: titleOf(row), where: ctx.sessionTitle(row.items[0]) ?? ctx.repoName(row.items[0]) ?? "", back: true });
  };

  const back = (key: string) => {
    supersede(key);
    putBack(key);
    setLines((ls) => ls.filter((l) => l.key !== key));
  };

  const toggle = (row: Row) => {
    const opening = openKey !== row.key;
    setOpenKey(opening ? row.key : null);
    setMenu(null);
    if (opening && row.part === "team" && unread(row)) void markRead(row);
  };

  /* The keyboard: J/K move, E done, H later, U put back, Enter sends or opens. Not while typing. */
  const keys = useRef<(e: KeyboardEvent) => void>(() => {});
  keys.current = (e: KeyboardEvent) => {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
    const target = e.target as HTMLElement | null;
    if (target && (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable)) return;
    // Enter on a focused control is that control's own (Later opens its menu); only elsewhere is it the open row's action.
    if (e.key === "Enter" && target?.closest?.("button, a, summary, [role=menuitem], [role=tab]")) return;
    const at = order.findIndex((r) => r.key === openKey);
    const row = at >= 0 ? order[at] : undefined;
    const key = e.key.toLowerCase();
    if (key === "j" || key === "k") {
      const next = order[at < 0 ? 0 : Math.min(Math.max(at + (key === "j" ? 1 : -1), 0), order.length - 1)];
      if (next && next.key !== openKey) toggle(next);
    } else if (key === "e" && row && canDone(row)) {
      done(row);
    } else if (key === "h" && row) {
      setMenu(row.key);
    } else if (key === "u") {
      const last = [...lines].reverse().find((l) => l.entry.back) ?? triageState().done.find((d) => d.back);
      if (last) back(last.key);
    } else if (e.key === "Enter" && row) {
      const el = [...(container.current?.querySelectorAll<HTMLElement>("[data-testid=inbox-row]") ?? [])].find((n) => n.dataset.key === row.key);
      const box = el?.querySelector("textarea");
      if (box) box.focus();
      else el?.querySelector<HTMLElement>("[data-primary], [data-primary-wrap] button")?.click();
    } else {
      return;
    }
    e.preventDefault();
  };
  useEffect(() => {
    const on = (e: KeyboardEvent) => keys.current(e);
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  /* New to the person: a team row the Library says is unread; any other row this page hasn't shown before. */
  const isNew = (row: Row) => (teamKeys(row).length > 0 ? unread(row) : row.items.some((i) => fresh.has(seenKey(i))));

  /* Rows and the grey lines of rows acted on, each line where its row stood. */
  const withLines = (rows: Row[], part: Part) => {
    const out: ({ row: Row } | { line: Line })[] = rows.map((row) => ({ row }));
    for (const l of [...drawnLines].reverse().filter((x) => x.part === part)) {
      const at = l.after ? out.findIndex((x) => ("row" in x ? x.row.key : x.line.key) === l.after) : -1;
      if (at < 0) out.push({ line: l });
      else out.splice(at, 0, { line: l });
    }
    return out.map((x) => ("row" in x
      ? <QueueRow key={x.row.key} row={x.row} open={openKey === x.row.key} menu={menu === x.row.key} isNew={isNew(x.row)}
          ctx={ctx} actions={actions} client={client} refresh={refresh} onToggle={() => toggle(x.row)} onDone={canDone(x.row) ? () => done(x.row) : undefined} doing={doing.has(x.row.key)}
          onLater={() => setMenu((m) => (m === x.row.key ? null : x.row.key))} onLaterAt={(at, sig) => later(x.row, at, sig)} settle={(entry, meta) => settle(x.row, entry, meta)} markRead={() => markRead(x.row)} />
      : <DoneLine key={`line:${x.line.key}`} line={x.line} onBack={() => back(x.line.key)} />));
  };

  const waitCount = waitRows.length + (extraHere ? waitingExtraCount : 0);
  const newTeam = teamRows.filter(isNew);
  const unreadTeam = teamRows.filter(unread);
  const teamLines = drawnLines.some((l) => l.part === "team");
  const doneToday = triageState().done.filter((d) => !drawnLines.some((l) => l.key === d.key && l.entry.what === d.what));
  const clear = synced && waitCount === 0 && teamRows.length === 0 && drawnLines.length === 0;

  if (!synced) {
    return (
      <div className="iq" aria-busy="true" data-testid="inbox-loading">
        {(["Waiting on you", "From the team"] as const).map((name, s) => (
          <section key={name} aria-label={name}>
            <h2 className="iq-sec-h"><i className="iq-dot" />{name}</h2>
            {[[62, 38], [34, 46]].map(([a, b], i) => (
              <div key={i} className="iq-item"><div className="iq-r"><span className="iq-u" /><span className="iq-sk av" />
                <div className="iq-sk-tx"><span className="iq-sk" style={{ width: `${a - s * 8 + i * 6}%` }} /><span className="iq-sk" style={{ width: `${b + s * 6}%` }} /></div>
                <div className="iq-end"><span className="iq-sk" style={{ width: 78, height: 24 }} /></div></div></div>
            ))}
          </section>
        ))}
      </div>
    );
  }

  return (
    <>
      {tabs && (
        <div className="iq-tabs" role="tablist" aria-label="Kinds" data-testid="inbox-tabs">
          <button type="button" role="tab" aria-selected={tab === "all"} onClick={() => setTab("all")}>All <i>{total}</i></button>
          {TABS.map((t) => {
            const n = items.filter((i) => t.kinds.includes(i.kind)).length + (t.key === "permissions" ? waitingExtraCount : 0);
            return n > 0 && <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} onClick={() => setTab(t.key)}>{t.label} <i>{n}</i></button>;
          })}
        </div>
      )}
      <div className="iq" ref={container} data-testid="inbox-queue">
        {clear ? (
          <div className="iq-clear" data-testid="all-clear">
            <b><CheckIcon />All clear</b>
            <span>
              Nothing is waiting on you and there is nothing new from the team.
              {ctx.working.agents > 0 ? ` ${ctx.working.agents} agent${ctx.working.agents === 1 ? " is" : "s are"} working in ${ctx.working.sessions} session${ctx.working.sessions === 1 ? "" : "s"}; what` : " What"} needs you shows up here and on the sidebar's Inbox.
            </span>
          </div>
        ) : (
          <>
            <section aria-label="Waiting on you" data-testid="inbox-waiting">
              <h2 className="iq-sec-h"><i className={`iq-dot ${waitCount > 0 ? "wait" : ""}`} />Waiting on you <span className="n" data-testid="waiting-count">{waitCount}</span>
                {waitCount > 0 && <span className="aside">what's stopped comes first</span>}</h2>
              {extraHere && waitingExtra}
              {waitCount === 0 && !drawnLines.some((l) => l.part === "wait") && (
                <div className="iq-calm" data-testid="inbox-calm"><CheckIcon />Nothing is waiting on you.{ctx.working.agents > 0 && <span>{ctx.working.agents} agent{ctx.working.agents === 1 ? " is" : "s are"} working.</span>}</div>
              )}
              {withLines(waitRows, "wait")}
            </section>
            {(teamRows.length > 0 || teamLines) && (
              <section aria-label="From the team" data-testid="inbox-team">
                <h2 className="iq-sec-h"><i className="iq-dot" />From the team {newTeam.length > 0 && <span className="n">{newTeam.length} new</span>}
                  {unreadTeam.length > 0 && waitCount === 0
                    ? <button type="button" className="aside" data-testid="mark-all-read" onClick={() => void markRowsRead(unreadTeam)}>Mark all read</button>
                    : <span className="aside">nothing here is waiting on you</span>}</h2>
                {withLines(teamRows, "team")}
              </section>
            )}
          </>
        )}
        {doneToday.length > 0 && (
          <details className="iq-fold" open={clear || allDone} onToggle={(e) => setAllDone((e.target as HTMLDetailsElement).open)} data-testid="done-today">
            <summary><ChevronRightIcon /><b>Done today</b> {doneToday.length}<span className="aside">{summaryOf(doneToday)}</span></summary>
            {doneToday.map((d) => (
              <div key={`${d.key}:${d.what}:${d.at}`} className="iq-dl" data-testid="done-entry" data-key={d.key} data-what={d.what}>
                <CheckIcon /><span className="what"><b>{d.what}</b> {quoted(d) ? `“${d.text}”` : `· ${d.text}`}</span><span>{d.where}</span>
                <span className="at">{new Date(d.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}</span>
                {d.back ? <button type="button" className="back" data-testid="put-back" onClick={() => back(d.key)}>Put back</button> : <span />}
              </div>
            ))}
          </details>
        )}
      </div>
      <div className="iq-keys" aria-label="Keys">
        <span><kbd>J</kbd><kbd>K</kbd> move</span><span><kbd>⏎</kbd> send or open</span><span><kbd>E</kbd> done</span><span><kbd>H</kbd> later</span><span><kbd>U</kbd> put back</span>
      </div>
    </>
  );
}

/* A notice is cleared by reading it (its one action); every other row has Done. */
const canDone = (row: Row) => row.kind !== "org_notice";

const quoted = (d: DoneEntry) => d.what === "Answered" || d.what === "Replied";

const NOUN: Record<string, string> = { Answered: "answered", Replied: "replied", "Opened PR": "PR opened", "Set aside": "set aside", Read: "read", Done: "done" };
function summaryOf(done: DoneEntry[]): string {
  const counts = new Map<string, number>();
  for (const d of done) counts.set(d.what, (counts.get(d.what) ?? 0) + 1);
  return [...counts].map(([what, n]) => `${n} ${NOUN[what] ?? what.toLowerCase()}`).join(" · ");
}

function DoneLine({ line, onBack }: { line: Line; onBack(): void }) {
  const { entry } = line;
  return (
    <div className="iq-item doneline" data-testid="inbox-line" data-key={line.key} data-what={entry.what}>
      <div className="iq-r">
        <span className="iq-u" /><span className="iq-av tick"><CheckIcon /></span>
        <div className="iq-tx"><div className="iq-t">{entry.what} {quoted(entry) ? `“${entry.text}”` : `· ${entry.text}`}</div>{line.meta && <div className="iq-m">{line.meta}</div>}</div>
        <div className="iq-end">
          {entry.back
            ? <button type="button" className="btn btn-quiet text-xs" data-testid="put-back" onClick={onBack}>Put back <kbd className="iq-kbd">U</kbd></button>
            : <span className="iq-age">just now</span>}
        </div>
      </div>
    </div>
  );
}

interface RowProps {
  row: Row;
  open: boolean;
  menu: boolean;
  isNew: boolean;
  ctx: QueueContext;
  actions: InboxActions;
  client: DaemonClient;
  refresh(): Promise<void>;
  onToggle(): void;
  onDone?(): void;
  /* Its Done is out, waiting for the Library: Done waits too. */
  doing?: boolean;
  onLater(): void;
  onLaterAt(until: number, sig?: string): void;
  settle(entry: Omit<DoneEntry, "key" | "at">, meta?: string | null): void;
  markRead(): void;
}

const stop = (f: () => void) => (e: { stopPropagation(): void }) => {
  e.stopPropagation();
  f();
};

function QueueRow(props: RowProps) {
  const { row, open, menu, isNew, ctx, actions, onToggle, onDone, onLater, onLaterAt } = props;
  const title = titleOf(row);
  const age = Date.now() - Math.min(...row.items.map((i) => seenAt(i)));
  const note = row.items.map((i) => actions.notes[itemKey(i)]).find(Boolean);
  const go = goOf(props);
  const read = row.part === "team" && !unread(row) && row.items.some((i) => i.read === true);
  return (
    <div className={`iq-item${isNew ? " new" : ""}${read ? " read" : ""}${open ? " open" : ""}${menu ? " hov" : ""}`} data-testid="inbox-row" data-kind={row.kind} data-key={row.key}
      data-part={row.part} data-open={open || undefined} data-new={isNew || undefined} data-read={read || undefined} aria-label={title}>
      <div className="iq-r" data-testid="row-summary" role="button" tabIndex={-1} aria-expanded={open} onClick={onToggle}>
        <span className="iq-u" />
        <Avatar row={row} />
        <div className="iq-tx">
          <div className="iq-t">{title}</div>
          <div className="iq-m">{joined(metaOf(row, ctx))}</div>
        </div>
        <div className="iq-end">
          {!open && (
            <span className="iq-tri">
              {onDone && <button type="button" className="iq-ib" title="Done (E)" aria-label="Done" data-testid="row-done" disabled={props.doing || row.items.some((i) => actions.busy.has(itemKey(i)))} onClick={stop(onDone)}><CheckIcon /></button>}
              <button type="button" className={`iq-ib${menu ? " on" : ""}`} title="Later (H)" aria-label="Later" data-testid="row-later" aria-haspopup="menu" aria-expanded={menu} onClick={stop(onLater)}><ClockIcon /></button>
            </span>
          )}
          <span className={`iq-age${STOPPED.includes(row.kind) && age > LONG_WAIT_MS ? " long" : ""}`}>{ageOf(age)}</span>
          {!open && go}
        </div>
      </div>
      {menu && <LaterMenu row={row} onPick={onLaterAt} />}
      {open && <RowBody {...props} title={title} />}
      {note && <p className={`iq-note ${note.failed ? "text-sap" : "text-pass"}`} role={note.failed ? "alert" : "status"}>{note.text}</p>}
    </div>
  );
}

function GrantAccess({ item, client, actions, refresh }: { item: InboxItemDTO; client: DaemonClient; actions: InboxActions; refresh(): Promise<void> }) {
  const pending = useRef(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<"grant" | "decline" | null>(null);
  const resolve = async (action: "grant" | "decline") => {
    if (pending.current || !item.from_person || !item.access_role || item.doc == null) return;
    pending.current = true;
    setBusy(true);
    try {
      await client.writeSharedDesign(String(item.doc), `access-requests/${encodeURIComponent(item.from_person.sub)}/${action}`, { role: item.access_role });
      setDone(action);
      actions.settle(item);
      await refresh();
    } catch (e) {
      actions.report(item, e instanceof Error ? e.message : String(e));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  };
  const disabled = busy || done !== null || !item.access_role || !item.from_person || item.doc == null;
  return <span className="inline-flex gap-2">
    <button type="button" className="btn btn-primary text-xs" disabled={disabled} onClick={stop(() => void resolve("grant"))}>
      {done === "grant" ? "Access granted" : `Grant ${item.access_role} access`}
    </button>
    <button type="button" className="btn btn-quiet text-xs" disabled={disabled} onClick={stop(() => void resolve("decline"))}>
      {done === "decline" ? "Declined" : "Decline"}
    </button>
  </span>;
}

function LaterMenu({ row, onPick }: { row: Row; onPick(until: number, sig?: string): void }) {
  return (
    <div className="iq-menu" role="menu" aria-label="Later" data-testid="later-menu">
      {laterTimes().map((t) => <button key={t.label} type="button" role="menuitem" onClick={stop(() => onPick(t.at))}>{t.label} <span>{t.time}</span></button>)}
      {row.kind === "signoff_progress" && <button type="button" role="menuitem" onClick={stop(() => onPick(Number.MAX_SAFE_INTEGER, signoffSig(row)))}>When the last sign-off lands <span>auto</span></button>}
    </div>
  );
}

/* The row's one action while it is closed: done here (Open PR, Mark read, Review, Open plan) or opening the row (Answer, Reply). */
function goOf({ row, ctx, actions, settle, markRead, onToggle, refresh, client }: RowProps, inBody = false): ReactNode {
  const item = row.items[0];
  const many = row.items.length > 1;
  // In the open row it is the screen's one green action.
  const btn = (label: string, onClick: (() => void) | undefined) => (
    <button type="button" className={`btn ${inBody ? "btn-primary" : "btn-secondary iq-go"} text-xs`} data-testid="row-go" data-primary={inBody || undefined}
      disabled={!onClick || row.items.some((i) => actions.busy.has(itemKey(i)))} onClick={stop(() => onClick?.())}>{label}</button>
  );
  const nav = ctx.onNavigate;
  switch (row.kind) {
    case "question":
      return btn("Answer", onToggle);
    case "shared_doc_comment":
      return btn("Reply", onToggle);
    case "review_requested":
      return many ? btn(`Show ${row.items.length}`, onToggle) : btn("Review", item.doc == null || !nav ? undefined : () => {
        markRead();
        nav({ screen: "shared-design", doc: String(item.doc), title: item.detail ?? undefined, repo: ctx.repo(item) ?? undefined });
      });
    case "signoff_progress":
    case "shared_with_you":
      return btn("Open plan", item.doc == null || !nav ? undefined : () => {
        markRead();
        nav({ screen: "shared-design", doc: String(item.doc), title: item.detail ?? undefined, repo: ctx.repo(item) ?? undefined });
      });
    case "access_requested":
      return <GrantAccess item={item} client={client} actions={actions} refresh={refresh} />;
    case "changes_requested":
    case "signed_off":
      return btn("Open plan", item.doc == null || !nav ? undefined : () => nav({ screen: "knowledge", view: "docs", doc: item.doc! }));
    case "doc":
      return btn("Open", item.doc == null || !nav ? undefined : () => nav({ screen: "knowledge", view: "docs", doc: item.doc!, section: item.section ?? undefined }));
    case "org_notice":
      return many ? btn(`Show ${row.items.length}`, onToggle) : btn("Mark read", () => actions.run(item, "read", {}, (ok) => ok && settle({ what: "Read", text: titleOf(row), where: "", back: false })));
    case "provider_update":
      return item.action ? btn("Update", () => actions.run(item, "provider-update", {},
        (ok) => ok && settle({ what: "Update queued", text: titleOf(row), where: "", back: false }))) : null;
    case "pr_ready":
      return <span className="iq-go" onClick={(e) => e.stopPropagation()}><OpenPr item={item} actions={actions} onDone={() => void refresh()} openUrl={ctx.openUrl} look="btn btn-secondary text-xs iq-go"
        onOpened={() => recordDone({ key: row.key, what: "Opened PR", text: titleOf(row), where: ctx.repoName(item) ?? "", back: false, at: Date.now() })} /></span>;
    case "pr_opened":
    case "pr_failed": {
      const url = item.url;
      if (url && ctx.openUrl) return btn("Open on GitHub", () => { void ctx.openUrl!(url); actions.run(item, "pr-not-now", {}); });
      // No link and no session to review it in: Done is all there is.
      return item.kind === "pr_failed" && ctx.review(item) ? btn("Review & open PR", ctx.review(item)) : null;
    }
    case "plan":
    case "visual":
      return opensVisual(item) ? btn(item.kind === "plan" ? "Open plan" : "Open visual", ctx.goTo(item)) : btn("Open session", ctx.goTo(item));
    case "proposed_checks":
      return btn("Review", onToggle);
    case "plan_offer":
    case "handover":
    case "provider_switch":
    case "provider_switch_back":
      return btn("Answer", onToggle);
    case "credentials":
    case "login":
      return btn("Sign in", onToggle);
    case "setup":
      return item.fix ? btn("Fix", onToggle) : null;
    case "org_blocked":
      return null;
    default:
      return ctx.goTo(item) ? btn("Open session", ctx.goTo(item)) : null;
  }
}

const Kbd = ({ k }: { k: string }) => <kbd>{k}</kbd>;

/* The open row: the answer box, the thread, or a preview, with the screen's one green action. */
function RowBody(props: RowProps & { title: string }) {
  const { row, ctx, actions, refresh, onDone, onLater, settle, markRead, title } = props;
  const item = row.items[0];
  const triage = (
    <>
      <span className="grow" />
      {onDone && <button type="button" className="btn btn-quiet text-xs" data-testid="row-done" disabled={props.doing} onClick={onDone}>Done <Kbd k="E" /></button>}
      <button type="button" className="btn btn-quiet text-xs" data-testid="row-later" aria-haspopup="menu" onClick={onLater}>Later <Kbd k="H" /></button>
    </>
  );
  const go = ctx.goTo(item);
  const openSession = go && <button type="button" className="btn btn-quiet text-xs" data-testid="open-session" onClick={go}>Open session ↗</button>;
  const nav = ctx.onNavigate;

  if (row.kind === "question") return <div className="iq-body" data-testid="row-body"><AnswerBox {...props} openSession={openSession} triage={triage} /></div>;
  if (row.kind === "shared_doc_comment") return <div className="iq-body" data-testid="row-body"><Threads {...props} triage={triage} /></div>;

  if (row.kind === "review_requested") {
    const open = (i: InboxItemDTO) => () => {
      markRead();
      nav?.({ screen: "shared-design", doc: String(i.doc), title: i.detail ?? undefined, repo: ctx.repo(i) ?? undefined });
    };
    return (
      <div className="iq-body" data-testid="row-body">
        {row.items.length > 1
          ? <div className="iq-people">{row.items.map((i) => <div key={itemKey(i)}><i>○</i>{i.detail || "Plan"} {decisionCount(i)} <button type="button" className="btn btn-quiet text-xs" disabled={i.doc == null || !nav} onClick={open(i)}>Review</button></div>)}</div>
          : <div className="iq-facts"><span>plan</span>{decisionCount(item)}{item.from_person && <span>from {personName(item.from_person)}</span>}</div>}
        <div className="iq-acts">
          {row.items.length === 1 && <button type="button" className="btn btn-primary text-xs" data-primary disabled={item.doc == null || !nav} onClick={open(item)}>Review <Kbd k="⏎" /></button>}
          {triage}
        </div>
      </div>
    );
  }

  if (row.kind === "signoff_progress") {
    const s = item.signoff;
    return (
      <div className="iq-body" data-testid="row-body">
        <div className="iq-people" data-testid="signoff-people">
          {item.from_person && <div><i>✓</i>{personName(item.from_person)} <span>signed off</span></div>}
          {s && s.signed > 1 && <div><i>✓</i>{s.signed - 1} more <span>signed off</span></div>}
          {(s?.waiting ?? []).map((p) => <div key={p.sub} className="todo"><i>○</i>{personName(p)} <span>asked</span></div>)}
        </div>
        {s && <p>Your plan builds once all {s.required === 2 ? "two" : s.required === 3 ? "three" : s.required} have signed.</p>}
        <div className="iq-acts">
          <button type="button" className="btn btn-primary text-xs" data-primary disabled={item.doc == null || !nav}
            onClick={() => nav?.({ screen: "shared-design", doc: String(item.doc), title: item.detail ?? undefined, repo: ctx.repo(item) ?? undefined })}>Open plan <Kbd k="⏎" /></button>
          {triage}
        </div>
      </div>
    );
  }

  if (row.kind === "org_notice") {
    return (
      <div className="iq-body" data-testid="row-body">
        {row.items.length > 1
          ? <div className="iq-people">{row.items.map((i) => <div key={itemKey(i)}><i>○</i>{i.reason} <button type="button" className="btn btn-quiet text-xs" onClick={() => actions.run(i, "read", {})}>Mark read</button></div>)}</div>
          : item.detail && <p>from your org's {item.detail}</p>}
        <div className="iq-acts">
          {row.items.length === 1 && <button type="button" className="btn btn-primary text-xs" data-primary disabled={actions.busy.has(itemKey(item))}
            onClick={() => actions.run(item, "read", {}, (ok) => ok && settle({ what: "Read", text: title, where: "", back: false }))}>Mark read <Kbd k="⏎" /></button>}
          {triage}
        </div>
      </div>
    );
  }

  if (row.kind === "pr_ready") {
    const branch = ctx.branch(item);
    const review = ctx.review(item);
    return (
      <div className="iq-body" data-testid="row-body">
        <div className="iq-facts">{branch && <span>{branch}</span>}{ctx.repoName(item) && <span>{ctx.repoName(item)}</span>}{joined(prFacts(item))}</div>
        <div className="iq-acts">
          <span data-primary-wrap><OpenPr item={item} actions={actions} onDone={() => void refresh()} openUrl={ctx.openUrl}
            onOpened={() => recordDone({ key: row.key, what: "Opened PR", text: title, where: ctx.repoName(item) ?? "", back: false, at: Date.now() })} /></span>
          {review && <button type="button" className="btn btn-secondary text-xs" onClick={review}>Review changes first</button>}
          {triage}
        </div>
      </div>
    );
  }

  // Every other kind: its detail and its own controls, the way to its session, and triage.
  const detail = itemDetail(item);
  const controls = itemControls({ item, actions, repo: ctx.repo(item), onDone: () => void refresh(), onGo: go, onReview: ctx.review(item), onNavigate: nav, openUrl: ctx.openUrl });
  const primary = controls === null && goOf(props, true);
  // The way to the session, unless the row's one action already is it.
  const toSession = !opensVisual(item) && (controls !== null || ELSEWHERE.includes(row.kind));
  return (
    <div className="iq-body" data-testid="row-body">
      {detail && <div className="text-[13px] leading-[1.55] text-ink-2">{detail}</div>}
      <div className="iq-acts">
        {controls}
        {primary && <span data-primary-wrap>{primary}</span>}
        {toSession && openSession}
        {triage}
      </div>
    </div>
  );
}

/* The answer box: Enter sends it to the agent at once (Shift+Enter is a new line). Taken, the row settles to
   "Answered" and says the agent is working again; refused (the session closed meanwhile), the row stays open
   with the text and one line saying why. */
function AnswerBox({ row, ctx, actions, settle, openSession, triage }: RowProps & { openSession: ReactNode; triage: ReactNode }) {
  const item = row.items[0];
  const [text, setText] = useState("");
  const busy = actions.busy.has(itemKey(item));
  const who = ctx.who(item) ?? "agent";
  const send = (e?: FormEvent) => {
    e?.preventDefault();
    const said = text.trim();
    if (!said || busy) return;
    actions.run(item, "answer", { text: said }, (ok) => {
      if (ok) settle({ what: "Answered", text: said, where: ctx.sessionTitle(item) ?? "", back: false }, [`${who} is working again`, ctx.sessionTitle(item)].filter(Boolean).join(" · "));
    });
  };
  const onKey = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };
  return (
    <form onSubmit={send} data-testid="answer-form">
      <div className="iq-box"><textarea rows={2} placeholder={`Answer the ${who}…`} aria-label="Your answer" data-testid="answer-box" value={text} onChange={(e) => setText(e.target.value)} onKeyDown={onKey} /></div>
      <div className="iq-acts">
        <button type="submit" className="btn btn-primary text-xs" data-testid="answer-send" disabled={busy}>Answer <Kbd k="⏎" /></button>
        {openSession}
        {triage}
      </div>
    </form>
  );
}

/* A design's comment threads, read from the Library when the row opens (the inbox row carries only the newest
   words), each with its reply box. A reply posts to that thread; the row then settles as replied, and is read. */
function Threads({ row, ctx, client, settle, markRead, triage }: RowProps & { triage: ReactNode }) {
  const item = row.items[0];
  const doc = String(item.doc ?? "");
  const repo = ctx.repo(item) ?? undefined;
  const [design, setDesign] = useState<SharedDesignDTO | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const ids = row.items.map((i) => i.thread_id);
  useEffect(() => {
    // The answer belongs to the doc this row was opened on; a later row's read must not be overwritten by it.
    let live = true;
    client.sharedDesign(doc, repo).then((d) => live && setDesign(d), (e: unknown) => live && setFailed(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [client, doc, repo]);
  const threads = (design?.threads ?? []).filter((t) => ids.includes(t.id));
  const nav = ctx.onNavigate;
  // The way to the doc and triage share the last thread's row of actions, beside its Reply.
  const rest = (
    <>
      {nav && <button type="button" className="btn btn-quiet text-xs" onClick={() => nav({ screen: "shared-design", doc, title: item.detail ?? undefined, repo })}>Open in the plan ↗</button>}
      {triage}
    </>
  );
  if (threads.length === 0) {
    return (
      <>
        <div className="iq-thread" data-testid="thread">
          {row.items.map((i) => <Msg key={itemKey(i)} who={personName(i.from_person)} at={null} text={i.excerpt ?? i.reason} />)}
          {failed && <p className="text-sap" role="alert">{failed}</p>}
        </div>
        <div className="iq-acts">{rest}</div>
      </>
    );
  }
  return (
    <>
      {threads.map((t, i) => <Thread key={t.id} thread={t} doc={doc} rev={design!.rev} repo={repo} client={client} after={i === threads.length - 1 ? rest : null}
        onSent={(text) => {
          markRead();
          settle({ what: "Replied", text, where: titleOf(row), back: false });
        }} />)}
    </>
  );
}

function Msg({ who, at, text }: { who: string; at: number | null; text: string }) {
  return (
    <div className="iq-msg" data-testid="thread-msg">
      <span className="iq-av">{initial(who)}</span>
      <div><div className="who"><b>{who}</b>{at !== null && ` · ${ageOf(Date.now() - at * 1000)}`}</div><p>{text}</p></div>
    </div>
  );
}

function Thread({ thread, doc, rev, repo, client, after, onSent }: { thread: PlanThreadDTO; doc: string; rev: number; repo?: string; client: DaemonClient; after: ReactNode; onSent(text: string): void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const send = (e?: FormEvent) => {
    e?.preventDefault();
    const said = text.trim();
    if (!said || busy) return;
    setBusy(true);
    setError(null);
    client.writeSharedDesign(doc, "comments", { rev, parent: thread.id, text: said, ...(repo ? { repo } : {}) })
      .then(() => onSent(said), (err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setBusy(false));
  };
  return (
    <form onSubmit={send} data-testid="thread-form" data-thread={thread.id}>
      {thread.anchor.quote && <div className="iq-quote">On “{thread.anchor.quote}”</div>}
      <div className="iq-thread" data-testid="thread">
        <Msg who={thread.author_name ?? (thread.author === "person" ? "You" : "A teammate")} at={thread.at} text={thread.text} />
        {(thread.replies ?? []).map((r) => <Msg key={r.id} who={r.author_name ?? (r.author === "person" ? "You" : "A teammate")} at={r.at} text={r.text} />)}
      </div>
      <div className="iq-box"><textarea rows={2} placeholder="Reply in this thread…" aria-label="Your reply" data-testid="reply-box" value={text} onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} /></div>
      {error && <p className="text-sap" role="alert">{error}</p>}
      <div className="iq-acts"><button type="submit" className="btn btn-primary text-xs" data-testid="reply-send" disabled={busy}>Reply <Kbd k="⏎" /></button>{after}</div>
    </form>
  );
}
