/* The share panel (owner-approved "sharing option A"): what each person may do (view, comment or
   edit) and, separately, whether the build waits for their sign-off. "Who has access" lists the
   people, teams and the org with their access and a plain Stop sharing, each row and the whole
   plan. A plan nobody has yet is shared from here too: adding its first person or team shares it.
   Every write changes one person or scope (brindle/pro/share_access.py), never the whole list. */

import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { DaemonError, type DocScope, type LibraryPerson, type PeopleQuery, type Plan, type PolicyPreviewRow, type ShareAccess, type ShareAccessLevel, type SharePerson, type ShareQueued } from "../../../api/client";
import type { PlanGateRowDTO } from "../../../api/schema";
import type { Writes } from "./PlanReader";
import { initials } from "./PlanCards";
import { PolicyPreview } from "./DocPolicies";
import "./SharePanel.css";

type Scope = { kind: string; id: string };

export interface SharePeopleTarget {
  /* People in the org, by the Library's search: only once the plan is shared. */
  search(query: PeopleQuery): Promise<{ people: SharePerson[] }>;
  access(): Promise<ShareAccess>;
  /* Give one person the plan; for a plan nobody has yet, this is its first share. The author's plan
     answers at once with `invite`, the change the daemon now sends in the background (as may change,
     signOff and revoke for a person whose invite is still out). */
  invite(sub: string, access: ShareAccessLevel, signOff: boolean): Promise<Answer>;
  /* Their access only: the Library keeps their sign-off as it is, in the same write. */
  change(sub: string, access: ShareAccessLevel): Promise<Answer>;
  signOff(sub: string, must: boolean): Promise<Answer>;
  revoke(sub: string): Promise<Answer>;
  revokeScope(scope: Scope): Promise<Plan>;
  /* The author's plan only: share with a team or the org, and stop sharing with everyone. */
  shareScope?(scope: Scope): Promise<Plan>;
  stopAll?(): Promise<Plan>;
  /* The author's plan only: the changes still being sent, from this Mac (shown while the Library
     can't be reached), and letting a refused one go. */
  invites?(): Promise<{ people: ShareQueued[] }>;
  /* The panel has these answers: the daemon sends a change only once its receipt comes back. */
  ack?(receipts: string[]): Promise<unknown>;
  dismiss?(sub: string): Promise<Plan>;
}
type Answer = Plan & { invite?: ShareQueued };

const LEVELS: { id: ShareAccessLevel; name: string; hint: string }[] = [
  { id: "view", name: "View", hint: "read it" },
  { id: "comment", name: "Comment", hint: "comment on it" },
  { id: "edit", name: "Edit", hint: "write in the draft" },
];
const CAN: Record<ShareAccessLevel, string> = { view: "Can view", comment: "Can comment", edit: "Can edit" };

export function ShareChevron() {
  return <svg className="ic sm" viewBox="0 0 16 16" aria-hidden><path d="M4.5 6.5L8 10l3.5-3.5" /></svg>;
}
function ShareCheck() {
  return <svg className="ic sm" viewBox="0 0 16 16" aria-hidden><path d="M3.5 8.5l3 3 6-7" /></svg>;
}
const personLabel = (p: SharePerson) => p.display_name.trim() || p.email;
const firstWord = (name: string) => name.split("@")[0].split(/[\s._-]+/)[0] || name;
const scopeKey = (s: Scope) => `${s.kind}:${s.id}`;
const fromDirectory = (p: LibraryPerson): SharePerson => ({ id: p.sub, display_name: p.display_name || p.name || "", email: p.email, teams: [], role: "", on_plan: false });
const list = (names: string[]) => names.length < 2 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;

/* `sent`: a change (`op`: an invite, or Stop sharing) the daemon is still sending, one the Library
   refused (failed), with why, or one not confirmed (its `receipt` not sent back): the person's to send or discard. */
type Person = { sub: string; name: string; access: ShareAccessLevel | null; signOff: "required" | "optional" | null;
  sent?: ShareQueued["state"] | "unconfirmed"; op?: ShareQueued["op"]; why?: string | null; receipt?: string | null };
type Work = { key: string; label: string; run(): Promise<Plan> };
/* A write out now, or one the Library refused, kept so Try again sends the same one. */
type Busy = Record<string, { state: "saving" | "failed"; refused?: Work; why?: string }>;

export function SharePanel({ plan, target, nameOf, writes, onBusy, visible, onClose, children, signOff, teams = [], directory = [], owned = false, previewRules, onStopAll }: {
  plan: Plan; target?: SharePeopleTarget; nameOf(sub: string): string; writes: Writes;
  onBusy(busy: boolean): void; visible: boolean; onClose(): void; children?: ReactNode;
  signOff?: { sub: string; disabled: boolean; run(): void };
  /* The teams and org this person may share into, with their names. */
  teams?: DocScope[];
  /* The org's people, to pick a plan's first person from before the Library can search for it. */
  directory?: LibraryPerson[];
  /* The author's own plan: an owner row, and Stop sharing for everyone. */
  owned?: boolean;
  /* The org rules a first share will meet, for the revision on screen. */
  previewRules?: () => Promise<PolicyPreviewRow[]>;
  /* Stop sharing for everyone landed: it turned the share link off too, so its section reads it again. */
  onStopAll?(): void;
}) {
  const shared = !!plan.gate;
  const [access, setAccess] = useState<ShareAccess | null>(null);
  const [accessBusy, setAccessBusy] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [typed, setTyped] = useState("");
  const [team, setTeam] = useState("");
  const [focused, setFocused] = useState(false);
  const [menu, setMenu] = useState<string | null>(null);
  const [highlight, setHighlight] = useState(0);
  const [found, setFound] = useState<SharePerson[]>([]);
  const [searchState, setSearchState] = useState<"loading" | "ready" | "error">("ready");
  /* Why the last search failed, as the daemon said: a sign-in held up on this Mac reads differently from a refusal. */
  const [searchWhy, setSearchWhy] = useState("");
  const [again, setAgain] = useState(0);
  const [adding, setAdding] = useState<{ sub: string; name: string; email: string; access: ShareAccessLevel; signOff: boolean } | null>(null);
  const [busy, setBusy] = useState<Busy>({});
  const [added, setAdded] = useState<Record<string, Person>>({});
  /* The changes the daemon is still sending, by person: from its answer to each write, and read from this Mac. */
  const [queued, setQueued] = useState<Record<string, ShareQueued>>({});
  const invitesGeneration = useRef(0);
  const [stopping, setStopping] = useState(false);
  const [rules, setRules] = useState<{ plan: string; rev: number; rows: PolicyPreviewRow[] } | null>(null);
  const queue = useRef(Promise.resolve());
  const out = useRef(0);
  const flight = useRef(new Set<string>());
  const answered = useRef<Plan | null>(null);
  const mounted = useRef(true);
  const accessGeneration = useRef(0);
  const searchGeneration = useRef(0);
  const latest = useRef({ target, writes, onBusy, onStopAll });
  latest.current = { target, writes, onBusy, onStopAll };
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; accessGeneration.current++; searchGeneration.current++; invitesGeneration.current++; }; }, []);
  // The daemon sends a change only once the receipt its answer carried comes back: the panel sends it
  // for each answer it gets, and for an unconfirmed one when the person chooses Send. A receipt that
  // couldn't be sent leaves the change unconfirmed, with Send to try again.
  const [confirming, setConfirming] = useState<Record<string, true>>({});
  const confirm = (receipt: string) => {
    const ack = latest.current.target?.ack;
    if (!ack) return;
    setConfirming((old) => ({ ...old, [receipt]: true }));
    const settled = (ok: boolean) => {
      if (!mounted.current) return;
      setConfirming((old) => { const next = { ...old }; delete next[receipt]; return next; });
      if (ok) {
        invitesGeneration.current++;
        setQueued((old) => Object.fromEntries(Object.entries(old).map(([sub, q]) => [sub, q.receipt === receipt ? { ...q, receipt: null } : q])));
      }
    };
    ack([receipt]).then(() => settled(true), () => settled(false));
  };
  useEffect(() => {
    if (!visible) return;
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); }
    };
    window.addEventListener("keydown", escape, true);
    return () => window.removeEventListener("keydown", escape, true);
  }, [visible, onClose]);
  // Who has it, from the Library: when the panel opens and after each change made here. An answer
  // asked for before a later change is dropped, so it never paints over newer rows.
  useEffect(() => {
    const generation = ++accessGeneration.current;
    const invitesAt = ++invitesGeneration.current;
    if (!visible || !target) return;
    setAccessBusy(null);
    target.access().then((got) => {
      if (accessGeneration.current === generation) setAccess(got);
    }, (e: unknown) => {
      if (accessGeneration.current === generation && e instanceof DaemonError && e.code === "busy") setAccessBusy(e.message);
    });
    // What is still being sent comes from this Mac: it shows while the Library can't be reached.
    target.invites?.().then((got) => {
      if (invitesGeneration.current !== invitesAt) return;
      // One still unconfirmed here (its answer may never have reached the app) waits for the person.
      setQueued(Object.fromEntries(got.people.map((q) => [q.sub, q])));
    }, () => {});
  }, [visible, target, reload, shared]);
  // A change the daemon is still sending lands in the background: the plan changes when it does
  // (or is refused), so the list is read again then.
  const sending = useRef(false);
  sending.current = Object.values(queued).some((q) => q.state !== "failed" && !q.receipt)
    || !!access?.people.some((p) => p.state === "sharing" || p.state === "removing");
  useEffect(() => {
    if (sending.current) setReload((n) => n + 1);
  }, [plan]);
  // The rules a first share would meet, for the plan and revision they were asked for.
  useEffect(() => {
    if (!visible || shared || !previewRules) return;
    let live = true;
    const [id, rev] = [plan.id, plan.rev];
    previewRules().then((rows) => { if (live) setRules({ plan: id, rev, rows }); }, () => {});
    return () => { live = false; };
  }, [visible, shared, previewRules, plan.id, plan.rev]);
  const searching = !!target && (focused || !!typed || !!team);
  // Once shared, the Library searches the org; before, the people this Mac knows are filtered here.
  useEffect(() => {
    const generation = ++searchGeneration.current;
    if (!searching || !shared) return;
    setSearchState("loading");
    latest.current.target!.search({ q: typed, team, include_unshared: "true" }).then((r) => {
      if (searchGeneration.current !== generation) return;
      setFound(r.people);
      setHighlight(0);
      setSearchState("ready");
    }, (e: unknown) => {
      if (searchGeneration.current !== generation) return;
      setSearchWhy(e instanceof Error ? e.message : "");
      setSearchState("error");
    });
  }, [searching, shared, typed, team, again]);
  const local = directory.map(fromDirectory).filter((p) => !typed.trim() || `${p.display_name} ${p.email}`.toLowerCase().includes(typed.trim().toLowerCase()));
  const results = shared ? found : local;

  const gateRows = (plan.gate?.reviewers ?? []).filter((r): r is PlanGateRowDTO & { sub: string } => !!r.sub);
  const reviewerOf = (sub: string) => gateRows.find((r) => r.sub === sub);
  const names = new Map([...directory.map(fromDirectory), ...found].map((p) => [p.id, personLabel(p)]));
  const nameFor = (sub: string, given?: string | null) => names.get(sub) || given || nameOf(sub);
  // The Library's list, with anyone the gate names it hasn't been read for yet.
  let people: Person[] = [
    ...(access?.people ?? []).map((p) => ({ sub: p.sub, name: nameFor(p.sub, p.name), access: p.access, signOff: p.sign_off, sent: p.state, op: p.op, why: p.error })),
    ...gateRows.filter((r) => !access?.people.some((p) => p.sub === r.sub))
      .map((r) => ({ sub: r.sub, name: nameFor(r.sub), access: null, signOff: r.required ? "required" as const : "optional" as const })),
  ];
  people.push(...Object.values(added).filter((a) => !people.some((p) => p.sub === a.sub)));
  // Then what is still being sent: an invite at the access asked for; someone being removed leaves the
  // list, unless the Library refused it or it isn't confirmed yet (they still have it).
  for (const q of Object.values(queued)) {
    const had = people.find((p) => p.sub === q.sub);
    const sent: Person["sent"] = q.state === "failed" ? "failed" : q.receipt && !confirming[q.receipt] ? "unconfirmed" : q.state;
    if (sent === "removing") { people = people.filter((p) => p.sub !== q.sub); continue; }
    const row: Person = q.op === "remove" && had ? { ...had, sent, op: q.op, why: q.error, receipt: q.receipt }
      : { sub: q.sub, name: had?.name ?? nameFor(q.sub), access: q.access, signOff: q.sign_off, sent, op: q.op, why: q.error, receipt: q.receipt };
    people = had ? people.map((p) => p.sub === q.sub ? row : p) : [...people, row];
  }
  const scopes = access?.scopes ?? [];
  const scopeName = (s: Scope) => teams.find((t) => t.kind === s.kind && t.id === s.id)?.name ?? (s.kind === "org" ? "Your organization" : "A team");
  const offered = teams.filter((t) => (t.kind === "group" || t.kind === "org") && t.can_share !== false && !scopes.some((s) => scopeKey(s) === scopeKey(t)));
  const teamHits = offered.filter((t) => typed.trim() && (t.name ?? "").toLowerCase().includes(typed.trim().toLowerCase()));
  const onPlan = (p: SharePerson) => people.some((r) => r.sub === p.id);
  const org = teams.find((t) => t.kind === "org")?.name ?? "Your organization";

  // One write at a time to the doc: the first opens the reader's write, the last closes it with
  // the plan it answered. The access list is read again once they are all done.
  const run = (work: Work) => {
    const current = latest.current;
    // A second change to the same person or scope while one is out would race it: it waits its turn.
    if (!current.target || flight.current.has(work.key)) return;
    if (out.current === 0) {
      if (!current.writes.begin()) return;
      current.onBusy(true);
    }
    out.current++;
    flight.current.add(work.key);
    setBusy((old) => ({ ...old, [work.key]: { state: "saving" } }));
    setMenu(null);
    queue.current = queue.current.then(async () => {
      try {
        answered.current = await work.run();
        if (mounted.current) setBusy((old) => { const next = { ...old }; delete next[work.key]; return next; });
        if (work.key === "all") latest.current.onStopAll?.();
      } catch (e) {
        const why = e instanceof Error ? e.message : "";
        if (mounted.current) setBusy((old) => ({ ...old, [work.key]: { state: "failed", refused: work, why } }));
      } finally {
        flight.current.delete(work.key);
        out.current--;
        if (out.current === 0) {
          // The last answer the Library gave, whichever write it came from.
          current.writes.end(answered.current);
          answered.current = null;
          if (mounted.current) { current.onBusy(false); setReload((n) => n + 1); }
        }
      }
    });
  };
  // A person's write answers with the change the daemon now sends for them, or none: shown at once,
  // whatever a read already out says (it is dropped).
  const personWork = (sub: string, label: string, call: (t: SharePeopleTarget) => Promise<Answer>): Work =>
    ({ key: `person:${sub}`, label, run: async () => {
      const got = await call(latest.current.target!);
      if (mounted.current) {
        invitesGeneration.current++;
        setQueued((old) => { const next = { ...old }; if (got.invite) next[sub] = got.invite; else delete next[sub]; return next; });
        if (got.invite?.receipt) confirm(got.invite.receipt);      // this answer arrived: its change may go
      }
      return got;
    } });
  const scopeWork = (s: Scope, label: string, call: (t: SharePeopleTarget) => Promise<Plan>): Work =>
    ({ key: `scope:${scopeKey(s)}`, label, run: () => call(latest.current.target!) });

  const open = (p: SharePerson) => {
    if (onPlan(p)) { setMenu(`person:${p.id}`); return; }
    setAdding({ sub: p.id, name: personLabel(p), email: p.email, access: "comment", signOff: false });
  };
  // The person's row shows at once, saving; it stays (with Try again) if the Library refuses.
  const add = () => {
    if (!adding) return;
    const { sub, access: level, signOff: must, name } = adding;
    setAdded((old) => ({ ...old, [sub]: { sub, name, access: level, signOff: must ? "required" : null } }));
    run(personWork(sub, "Couldn't add", (t) => t.invite(sub, level, must)));
    setAdding(null);
    setTyped("");
    setFocused(false);
  };
  // Once the Library lists them, its row is the one shown.
  useEffect(() => {
    setAdded((old) => Object.fromEntries(Object.entries(old).filter(([sub]) => !access?.people.some((p) => p.sub === sub) && !queued[sub])));
  }, [access, queued]);

  const signNote = (p: Person): { text: string; tone: "ok" | "wait" | "" } | null => {
    const r = reviewerOf(p.sub);
    const must = p.signOff ?? (r ? (r.required ? "required" : "optional") : null);
    if (!must) return null;
    if (r?.status === "signed_off") return { text: `Signed off r${plan.rev}`, tone: "ok" };
    if (r?.status === "changes_requested") return { text: "Asked for changes", tone: "wait" };
    if (r?.status === "waived") return { text: "Sign-off waived", tone: "" };
    return must === "required" ? { text: "Must sign off · not yet", tone: "wait" } : { text: "May sign off", tone: "" };
  };
  const waiting = people.filter((p) => (p.signOff ?? (reviewerOf(p.sub)?.required ? "required" : null)) === "required");
  const unsigned = waiting.filter((p) => reviewerOf(p.sub)?.status !== "signed_off").map((p) => firstWord(p.name));
  const signed = waiting.filter((p) => reviewerOf(p.sub)?.status === "signed_off").map((p) => firstWord(p.name));
  const buildNote = unsigned.length ? `The build starts when ${list(unsigned)} sign${unsigned.length > 1 ? "" : "s"} off.${signed.length ? ` ${list(signed)} already ${signed.length > 1 ? "have" : "has"}.` : ""}` : null;

  // A refused write keeps its row, its error and Try again; `cancel` drops one that never landed.
  const status = (key: string, label: string, cancel?: () => void) => {
    const b = busy[key];
    if (!b) return null;
    return b.state === "saving" ? <span className="st"><i className="spin" />Saving…</span>
      : <><span className="st bad" title={b.why}>{b.refused?.label ?? label}</span><button className="sp-btn q" data-testid={`share-try-${key}`} onClick={() => b.refused && run(b.refused)}>Try again</button>
        {cancel && <button className="sp-btn q end" onClick={cancel}>Cancel</button>}</>;
  };
  const drop = (sub: string) => {
    setAdded((old) => { const next = { ...old }; delete next[sub]; return next; });
    setBusy((old) => { const next = { ...old }; delete next[`person:${sub}`]; return next; });
  };

  const personRow = (p: Person) => {
    const key = `person:${p.sub}`;
    const note = signNote(p);
    const r = reviewerOf(p.sub);
    const must = (p.signOff ?? (r?.required ? "required" : null)) === "required";
    const state = busy[key]?.state ?? p.sent ?? (r?.status === "signed_off" ? "signed_off" : "waiting");
    const level = p.access;
    // Added here but not yet in the Library (still being sent, or refused): nothing to change until the add lands.
    const pending = (!!added[p.sub] && !access?.people.some((a) => a.sub === p.sub)) || !!p.sent;
    const why = busy[key]?.state === "failed" ? busy[key]?.why : p.sent === "failed" ? p.why ?? undefined : undefined;
    // The daemon's own answer for an add it sends in the background: a refusal keeps Try again and Cancel.
    // A refused removal is tried again as a removal, never as an add: they may only lose access here.
    // One not confirmed (its answer may never have reached the app, or its receipt couldn't be sent)
    // is never sent by itself: Send confirms it, Discard lets it go.
    const sentStatus = () => p.sent === "sharing" || p.sent === "removing"
      ? <span className="st" title={p.why ?? undefined}><i className="spin" />{p.sent === "sharing" ? "Sharing…" : "Removing…"}</span>
      : p.sent === "unconfirmed" ? <><span className="st">{p.op === "remove" ? "Not stopped yet" : "Not sent yet"}</span>
        {target?.ack && p.receipt && <button className="sp-btn q" data-testid={`share-send-${key}`} onClick={() => confirm(p.receipt!)}>Send</button>}
        {target?.dismiss && <button className="sp-btn q end" onClick={() => run(personWork(p.sub, "Couldn't discard", (t) => t.dismiss!(p.sub)))}>Discard</button>}</>
      : p.sent !== "failed" ? null
      : p.op === "remove" ? <><span className="st bad">Couldn't stop sharing</span>
        <button className="sp-btn q" data-testid={`share-try-${key}`} onClick={() => run(personWork(p.sub, "Couldn't stop sharing", (t) => t.revoke(p.sub)))}>Try again</button>
        {target?.dismiss && <button className="sp-btn q end" onClick={() => run(personWork(p.sub, "Couldn't save", (t) => t.dismiss!(p.sub)))}>Dismiss</button>}</>
      : <><span className="st bad">Couldn't add</span>
        <button className="sp-btn q" data-testid={`share-try-${key}`} onClick={() => run(personWork(p.sub, "Couldn't add", (t) => t.invite(p.sub, level ?? "comment", p.signOff === "required")))}>Try again</button>
        <button className="sp-btn q end" onClick={() => run(personWork(p.sub, "Couldn't stop sharing", (t) => t.revoke(p.sub)))}>Cancel</button></>;
    return <Fragment key={key}>
      <div className={`r who ${menu === key ? "act" : ""}`} data-testid={`share-person-${p.sub}`} data-state={state} data-access={level ?? ""} data-sign-off={p.signOff ?? (r ? (r.required ? "required" : "optional") : "none")}>
        <i className="av">{initials(p.name)}</i>
        <span className="grid grow"><span className="nm" title={p.name}>{p.name}</span>
          {why ? <span className="note bad" role="alert">{why}</span>
            : p.sent === "sharing" && p.why ? <span className="note wait" title={p.why}>Waiting to reach your Library</span>
            : note && <span className={`note ${note.tone}`}>{note.tone === "ok" && <ShareCheck />}{note.text}</span>}</span>
        {status(key, "Couldn't save", pending && !p.sent ? () => drop(p.sub) : undefined) ?? sentStatus() ?? (signOff?.sub === p.sub && r?.status !== "signed_off"
          ? <button className="sp-btn q end" disabled={signOff.disabled} onClick={signOff.run}>Sign off</button> : null)}
        {pending ? !busy[key] && level && <span className="st">{CAN[level]}</span> : target && level ? <button className="sel" aria-haspopup="menu" aria-expanded={menu === key} aria-label={`What ${p.name} can do`} disabled={busy[key]?.state === "saving"} onClick={() => setMenu(menu === key ? null : key)}>{CAN[level]}<ShareChevron /></button>
          : level && <span className="st">{CAN[level]}</span>}
      </div>
      {menu === key && target && level && !pending && <div className="menu" role="menu" data-testid={`share-menu-${p.sub}`}>
        <div className="ml">{firstWord(p.name)} can</div>
        {LEVELS.map((l) => <button key={l.id} className="mi" role="menuitemradio" aria-checked={level === l.id}
          onClick={() => level !== l.id && run(personWork(p.sub, "Couldn't change", (t) => t.change(p.sub, l.id)))}>{l.name}<span className="mx">{l.hint}{level === l.id && <ShareCheck />}</span></button>)}
        <button className="mi box" role="menuitemcheckbox" aria-checked={must} onClick={() => run(personWork(p.sub, "Couldn't change", (t) => t.signOff(p.sub, !must)))}>
          <i className={`bx ${must ? "on" : ""}`} aria-hidden>{must && <ShareCheck />}</i><span className="grow">Must sign off<span className="mx"> · the build waits for {firstWord(p.name)}</span></span></button>
        <button className="mi sep" role="menuitem" onClick={() => run(personWork(p.sub, "Couldn't stop sharing", (t) => t.revoke(p.sub)))}>Stop sharing</button>
      </div>}
    </Fragment>;
  };

  const scopeRow = (s: Scope) => {
    const key = `scope:${scopeKey(s)}`;
    const name = scopeName(s);
    return <Fragment key={key}>
      <div className={`r who ${menu === key ? "act" : ""}`} data-testid={`share-scope-${s.kind}-${s.id}`} data-state={busy[key]?.state ?? "shared"}>
        <i className="av team">{initials(name)}</i>
        <span className="grid grow"><span className="nm" title={name}>{name}</span><span className="note">{s.kind === "org" ? "everyone in the organization" : "everyone in the team"}</span></span>
        {status(key, "Couldn't stop sharing")}
        {target ? <button className="sel" aria-haspopup="menu" aria-expanded={menu === key} aria-label={`What ${name} can do`} onClick={() => setMenu(menu === key ? null : key)}>{CAN.view}<ShareChevron /></button>
          : <span className="st">{CAN.view}</span>}
      </div>
      {menu === key && <div className="menu" role="menu" data-testid={`share-menu-${s.kind}-${s.id}`}>
        <div className="mh">A team or the organization can view it. Add a person to let them comment or edit.</div>
        <button className="mi sep" role="menuitem" onClick={() => run(scopeWork(s, "Couldn't stop sharing", (t) => t.revokeScope(s)))}>Stop sharing</button>
      </div>}
    </Fragment>;
  };

  const teamRow = (t: DocScope, filtered: boolean) => {
    const key = `scope:${scopeKey(t)}`;
    return <div className="r who" key={key} data-testid={`share-team-${t.kind}-${t.id}`}>
      <i className="av team">{initials(t.name ?? t.id)}</i>
      <span className="grid grow"><span className="nm" title={t.name ?? t.id}>{t.name ?? t.id}</span>{!filtered && <span className="note">{t.kind === "org" ? "everyone" : "everyone in the team"}</span>}</span>
      {status(key, "Couldn't share") ?? (target?.shareScope && <button className="sp-btn s" onClick={() => run(scopeWork(t, "Couldn't share", (x) => x.shareScope!({ kind: t.kind, id: t.id })))}>Share</button>)}
    </div>;
  };

  const count = people.length + (owned ? 1 : 0);
  const listed = !!access?.shared || shared;
  return <aside className="share-panel" hidden={!visible} aria-label="Share" data-testid="plan-share">
    <div className="sph"><b>Share</b><button className="sp-btn q" aria-label="Close share" onClick={onClose}><svg className="ic" viewBox="0 0 16 16" aria-hidden><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" /></svg></button></div>
    {target && <section className="sec">
      <div className="fld">
        {team && <button className="tok" onClick={() => setTeam("")}>Team: {teams.find((t) => t.id === team)?.name ?? team}<span>×</span></button>}
        <input data-testid="share-search" aria-label="Add people or teams" placeholder={team ? "" : "Add people or teams"} value={typed}
          onFocus={() => setFocused(true)} onChange={(e) => { setTyped(e.target.value); setAdding(null); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); setHighlight((n) => Math.max(0, Math.min(results.length - 1, n + (e.key === "ArrowDown" ? 1 : -1)))); }
            if (e.key === "Enter" && results[highlight]) open(results[highlight]);
          }} />
        {shared && teams.some((t) => t.kind === "group") && <button className={`sp-btn q end ${menu === "filter" ? "on" : ""}`} aria-expanded={menu === "filter"} onClick={() => setMenu(menu === "filter" ? null : "filter")}>Filter<ShareChevron /></button>}
      </div>
      {menu === "filter" && <div className="menu" role="menu" data-testid="share-filter">
        <div className="ml">Team</div>{teams.filter((t) => t.kind === "group").map((t) => <button className="mi" role="menuitemcheckbox" aria-checked={team === t.id} key={t.id} onClick={() => { setTeam(team === t.id ? "" : t.id); setMenu(null); }}>{t.name ?? t.id}{team === t.id && <ShareCheck />}</button>)}
      </div>}
      {adding ? <div data-testid="share-add">
        <div className="rows"><div className="r act"><i className="av">{initials(adding.name)}</i><span className="nm" title={adding.email}>{adding.name}<em>{adding.email}</em></span></div></div>
        <div className="menu add" role="group" aria-label={`What ${firstWord(adding.name)} can do`}>
          <div className="ml">{firstWord(adding.name)} can</div>
          {LEVELS.map((l) => <button key={l.id} className="mi" role="menuitemradio" aria-checked={adding.access === l.id} onClick={() => setAdding({ ...adding, access: l.id })}>{l.name}<span className="mx">{l.hint}{adding.access === l.id && <ShareCheck />}</span></button>)}
          <button className="mi box sep" role="menuitemcheckbox" aria-checked={adding.signOff} onClick={() => setAdding({ ...adding, signOff: !adding.signOff })}>
            <i className={`bx ${adding.signOff ? "on" : ""}`} aria-hidden>{adding.signOff && <ShareCheck />}</i><span className="grow">Must sign off<span className="mx"> · the build waits for {firstWord(adding.name)}</span></span></button>
          {!listed && <p className="hint up">{access?.kept_here === false ? "Adding them shares this plan: it is saved to your Library on brindle servers." : "Adding them shares this plan: it is uploaded from this Mac to brindle servers. Your other plans stay here."}</p>}
          <div className="acts"><button className="sp-btn q" onClick={() => setAdding(null)}>Cancel</button>
            <button className="sp-btn main" data-testid="share-add-confirm" disabled={busy[`person:${adding.sub}`]?.state === "saving"} onClick={add}>Add {firstWord(adding.name)}</button></div>
        </div>
      </div>
        : !searching ? (!listed && <p className="hint">Only you can open this plan. Add a person or a team to share it.</p>)
        : searchState === "loading" && shared ? <div data-testid="share-search-loading"><div className="sweep" /><div className="rows">{[96, 132, 78].map((w) => <div className="r" key={w}><i className="av skv" /><i className="sk" style={{ width: w }} /></div>)}</div></div>
        : searchState === "error" && shared ? <div className="msg bad" role="alert" data-testid="share-search-error"><span className="grid grow">Couldn't search people.{searchWhy && <span className="note">{searchWhy}</span>}</span><button className="sp-btn q end" onClick={() => setAgain((n) => n + 1)}>Try again</button></div>
        : <div className="rows">
          {teamHits.map((t) => teamRow(t, true))}
          {results.map((p, i) => <div key={p.id} className={`r ${onPlan(p) ? "dim" : i === highlight ? "act" : ""}`} onMouseEnter={() => setHighlight(i)} data-testid={`share-result-${p.id}`}>
            <i className="av">{initials(personLabel(p))}</i><button className="nm truncate text-left" title={`${personLabel(p)} · ${p.email}`} onClick={() => open(p)}>{personLabel(p)}<em>{p.email}</em></button>
            {onPlan(p) && <span className="st">Has access</span>}
          </div>)}
          {!results.length && !teamHits.length && <div className="msg" data-testid="share-search-empty"><span className="grow">{typed.trim() ? "Nobody matches." : "Type a name or an email."}</span></div>}
        </div>}
    </section>}
    <section className="sec fill" data-testid="share-access">
      {accessBusy !== null && <div className="msg bad" role="alert"><span className="grow">{accessBusy}</span><button className="sp-btn q end" onClick={() => setReload((n) => n + 1)}>Try again</button></div>}
      {/* A first add shows here at once, and stays with its error if the upload is refused. */}
      {(listed || people.length > 0) && <>
        <div className="lab"><b>Who has access</b><span>{[count && `${count} ${count === 1 ? "person" : "people"}`, scopes.length && `${scopes.length} ${scopes.length === 1 ? "team" : "teams"}`].filter(Boolean).join(" · ")}</span></div>
        {owned && <div className="r who" data-testid="share-owner"><i className="av">{initials("You")}</i><span className="grid grow"><span className="nm">You</span><span className="note">Author</span></span><span className="st">Owner</span></div>}
        {people.map(personRow)}
        {scopes.map(scopeRow)}
        {buildNote && <p className="hint">{buildNote}</p>}
        {listed && target?.stopAll && (people.length > 0 || scopes.length > 0) && (stopping
          ? <div className="msg" data-testid="share-stop-confirm"><span className="grow">Stop sharing with everyone? Only you will be able to open it.</span>
            <button className="sp-btn s" onClick={() => { setStopping(false); run({ key: "all", label: "Couldn't stop sharing", run: () => latest.current.target!.stopAll!() }); }}>Stop sharing</button>
            <button className="sp-btn q end" onClick={() => setStopping(false)}>Cancel</button></div>
          : <div className="line">{status("all", "Couldn't stop sharing") ?? <button className="sp-btn q stop" data-testid="share-stop-all" onClick={() => setStopping(true)}>Stop sharing</button>}</div>)}
      </>}
      {!listed && <>
        {target?.shareScope && offered.length > 0 && <><div className="lab"><b>Your teams</b></div>{offered.map((t) => teamRow(t, false))}</>}
        {rules?.plan === plan.id && rules.rev === plan.rev && rules.rows.length > 0 && <><div className="lab"><b>{org} asks for</b></div><PolicyPreview rows={rules.rows} /></>}
        {target && <p className="hint" data-testid="share-upload">{access?.kept_here === false
          ? "Sharing saves this plan to your Library on brindle servers."
          : "This plan is kept on this Mac. Sharing uploads this one plan to brindle servers; your other plans stay here."}</p>}
      </>}
    </section>
    {children}
  </aside>;
}
