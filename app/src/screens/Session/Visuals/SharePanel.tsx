import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import type { PeopleQuery, Plan, SharePerson } from "../../../api/client";
import type { Writes } from "./PlanReader";
import "./SharePanel.css";

export interface SharePeopleTarget {
  search(query: PeopleQuery): Promise<{ people: SharePerson[] }>;
  put(sub: string, required: boolean): Promise<Plan>;
  remove(sub: string): Promise<Plan>;
}

export function ShareChevron() {
  return <svg className="ic sm" viewBox="0 0 16 16" aria-hidden><path d="M4.5 6.5L8 10l3.5-3.5" /></svg>;
}
function ShareCheck() {
  return <svg className="ic sm" viewBox="0 0 16 16" aria-hidden><path d="M3.5 8.5l3 3 6-7" /></svg>;
}
const initials = (name: string) => name.split(/[\s@._-]+/).slice(0, 2).map((p) => p[0]).join("").toUpperCase();
type Row = { sub: string; required: boolean; name: string; state: "signed_off" | "invited" | "inviting" | "failed"; action?: boolean | null };

export function SharePanel({ plan, target, nameOf, writes, onBusy, visible, onClose, children, fallback, signOff }: {
  plan: Plan; target?: SharePeopleTarget; nameOf(sub: string): string; writes: Writes;
  onBusy(busy: boolean): void; visible: boolean; onClose(): void; children?: ReactNode; fallback?: ReactNode;
  signOff?: { sub: string; disabled: boolean; run(): void };
}) {
  const [typed, setTyped] = useState("");
  const [team, setTeam] = useState("");
  const [role, setRole] = useState("");
  const [pasted, setPasted] = useState<string[]>([]);
  const [menu, setMenu] = useState<string | null>(null);
  const [focused, setFocused] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const [found, setFound] = useState<SharePerson[]>([]);
  const [directory, setDirectory] = useState<SharePerson[]>([]);
  const [searchState, setSearchState] = useState<"loading" | "ready" | "error">("loading");
  const [again, setAgain] = useState(0);
  const [pending, setPending] = useState<Record<string, Row>>({});
  const [removed, setRemoved] = useState<Set<string>>(new Set());
  const flight = useRef(new Set<string>());
  const queue = useRef(Promise.resolve());
  const latest = useRef({ target, plan, writes, onBusy, nameOf });
  latest.current = { target, plan, writes, onBusy, nameOf };
  const mounted = useRef(true);
  const searchGeneration = useRef(0);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; searchGeneration.current++; }; }, []);
  useEffect(() => {
    if (!visible) return;
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); }
    };
    window.addEventListener("keydown", escape, true);
    return () => window.removeEventListener("keydown", escape, true);
  }, [visible, onClose]);
  // Catalogue for filter names; search results themselves always come from the server.
  useEffect(() => {
    let active = true;
    if (target && plan.gate) target.search({}).then((r) => { if (active) setDirectory(r.people); }, () => {});
    return () => { active = false; };
  }, [target, !!plan.gate]);
  const searching = !!target && !!plan.gate && (focused || !!typed || !!team || !!role || pasted.length > 0);
  useEffect(() => {
    const generation = ++searchGeneration.current;
    if (!searching) return;
    setSearchState("loading");
    const t = latest.current.target!;
    const queries = pasted.length ? pasted.map((q) => ({ q, team, role })) : [{ q: typed, team, role }];
    void Promise.all(queries.map((q) => t.search(q))).then((answers) => {
      if (searchGeneration.current !== generation) return;
      const people = [...new Map(answers.flatMap((r) => r.people).map((p) => [p.id, p])).values()];
      setFound(pasted.length ? people.filter((p) => pasted.includes(p.email.toLowerCase())) : people);
      setHighlight(0);
      setSearchState("ready");
    }, () => { if (searchGeneration.current === generation) setSearchState("error"); });
    return () => { searchGeneration.current++; };
  }, [searching, typed, team, role, pasted, again]);
  const teams = [...new Map(directory.flatMap((p) => p.teams).map((t) => [t.id, t])).values()];
  const roles = [...new Set(["admin", "member", ...directory.map((p) => p.role)])];
  const teamName = teams.find((t) => t.id === team)?.name ?? team;
  const names = new Map([...directory, ...found].map((p) => [p.id, p.display_name]));
  const committed: Row[] = (plan.gate?.reviewers ?? []).flatMap((r) => r.sub ? [{ sub: r.sub, required: r.required,
    name: names.get(r.sub) ?? nameOf(r.sub), state: r.status === "signed_off" ? "signed_off" : "invited" }] : []);
  const rows = [...committed.filter((r) => !pending[r.sub] && !removed.has(r.sub)), ...Object.values(pending)];
  const onPlan = (p: SharePerson) => p.on_plan || rows.some((r) => r.sub === p.id);
  const available = found.filter((p) => !onPlan(p));
  const send = (sub: string, required: boolean | null, name: string) => {
    const current = latest.current;
    if (!current.target || flight.current.has(sub)) return;
    if (flight.current.size === 0) {
      if (!current.writes.begin()) return;
      current.onBusy(true);
    }
    // A completed invite can still be local while another person's write is queued.
    // Its confirmed role, rather than the reader's older snapshot, is the rollback target.
    const before = pending[sub] ?? committed.find((r) => r.sub === sub);
    flight.current.add(sub);
    setPending((old) => ({ ...old, [sub]: { sub, name, required: required ?? before?.required ?? true, state: "inviting", action: required } }));
    setMenu(null);
    // Queue just the writes; every selected person appears immediately. This avoids our own
    // per-person head conflicts and lets the reader adopt one authoritative final snapshot.
    queue.current = queue.current.then(async () => {
      let got: Plan | null = null;
      try {
        got = required === null ? await current.target!.remove(sub) : await current.target!.put(sub, required);
        if (mounted.current) setRemoved((old) => {
          const next = new Set(old);
          if (required === null) next.add(sub);
          else next.delete(sub);
          return next;
        });
        if (mounted.current) setPending((old) => {
          const next = { ...old };
          if (required === null) delete next[sub];
          else next[sub] = { sub, name, required, state: got!.gate?.reviewers?.find((r) => r.sub === sub)?.status === "signed_off" ? "signed_off" : "invited" };
          return next;
        });
      } catch {
        if (mounted.current) setPending((old) => ({ ...old, [sub]: { sub, name, required: before?.required ?? required ?? true, state: "failed", action: required } }));
      } finally {
        flight.current.delete(sub);
        if (flight.current.size === 0) {
          current.writes.end(got);
          if (mounted.current) { current.onBusy(false); setAgain((n) => n + 1); }
        }
      }
    });
  };
  // Once the reader has accepted the server's rows, their sign-off state follows live reads.
  useEffect(() => {
    setPending((old) => Object.fromEntries(Object.entries(old).filter(([sub, row]) => row.state === "inviting" || row.state === "failed"
      || !(plan.gate?.reviewers ?? []).some((r) => r.sub === sub && r.required === row.required))));
    setRemoved((old) => new Set([...old].filter((sub) => (plan.gate?.reviewers ?? []).some((r) => r.sub === sub))));
  }, [plan]);
  const changeText = (value: string) => {
    const filter = /^(team|role):([^\s]+)\s$/i.exec(value);
    if (filter) {
      if (filter[1].toLowerCase() === "team") setTeam(teams.find((t) => t.name.toLowerCase() === filter[2].toLowerCase() || t.id === filter[2])?.id ?? filter[2]);
      else setRole(filter[2].toLowerCase());
      setTyped("");
    } else setTyped(value);
  };
  return <aside className="share-panel" hidden={!visible} aria-label="Share" data-testid="plan-share">
    <div className="sph"><b>Share</b><button className="sp-btn q" aria-label="Close share" onClick={onClose}><svg className="ic" viewBox="0 0 16 16" aria-hidden><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" /></svg></button></div>
    {target && plan.gate ? <>
      <section className="sec">
        <div className="fld">
          {team && <button className="tok" onClick={() => setTeam("")}>Team: {teamName}<span>×</span></button>}
          {role && <button className="tok" onClick={() => setRole("")}>Role: {role}<span>×</span></button>}
          {pasted.length > 0 && <button className="tok" onClick={() => setPasted([])}>{pasted.length} pasted addresses<span>×</span></button>}
          <input data-testid="share-search" aria-label="Add people by name, email or team" placeholder={team || role || pasted.length ? "" : "Add people by name, email or team"} value={typed}
            onFocus={() => setFocused(true)} onChange={(e) => changeText(e.target.value)}
            onPaste={(e) => { const emails = [...new Set(e.clipboardData.getData("text").split(/[\s,;]+/).filter((s) => s.includes("@")).map((s) => s.toLowerCase()))]; if (emails.length > 1) { e.preventDefault(); setPasted(emails); setTyped(""); } }}
            onKeyDown={(e) => { if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); setHighlight((n) => Math.max(0, Math.min(found.length - 1, n + (e.key === "ArrowDown" ? 1 : -1)))); } if (e.key === "Enter" && found[highlight] && !onPlan(found[highlight])) send(found[highlight].id, true, found[highlight].display_name); }} />
          <button className={`sp-btn q end ${menu === "filter" ? "on" : ""}`} aria-expanded={menu === "filter"} onClick={() => setMenu(menu === "filter" ? null : "filter")}>Filter<ShareChevron /></button>
        </div>
        {menu === "filter" && <div className="menu" role="menu" data-testid="share-filter">
          <div className="ml">Team</div>{teams.map((t) => <button className="mi" role="menuitemcheckbox" aria-checked={team === t.id} key={t.id} onClick={() => { setTeam(team === t.id ? "" : t.id); setMenu(null); }}>{t.name}{team === t.id && <ShareCheck />}</button>)}
          <div className="ml">Role</div>{roles.map((r) => <button className="mi" role="menuitemcheckbox" aria-checked={role === r} key={r} onClick={() => { setRole(role === r ? "" : r); setMenu(null); }}>{r.charAt(0).toUpperCase() + r.slice(1)}{role === r && <ShareCheck />}</button>)}
          <div className="mh">or type team: or role:</div>
        </div>}
        {!searching ? <p className="hint">Paste a list of emails to add several at once.</p> : searchState === "loading" ? <div data-testid="share-search-loading"><div className="sweep" /><div className="rows">{[96, 132, 78].map((w) => <div className="r" key={w}><i className="av skv" /><i className="sk" style={{ width: w }} /></div>)}</div><div className="foot">Searching {teamName || "your organization"}…</div></div>
          : searchState === "error" ? <div className="msg bad" role="alert" data-testid="share-search-error"><span className="grow">Couldn't search people.</span><button className="sp-btn q end" onClick={() => setAgain((n) => n + 1)}>Try again</button></div>
          : <>
            <div className="rows">{found.map((p, i) => <div key={p.id} className={`r ${onPlan(p) ? "dim" : i === highlight ? "act" : ""}`} onMouseEnter={() => setHighlight(i)} data-testid={`share-result-${p.id}`}>
              <i className="av">{initials(p.display_name)}</i><button className="nm text-left" onFocus={() => setHighlight(i)} onClick={() => { if (rows.some((r) => r.sub === p.id)) setMenu(p.id); else if (!onPlan(p)) send(p.id, true, p.display_name); }}>{p.display_name}{i !== highlight && !onPlan(p) && <em>{p.email}</em>}</button>
              {onPlan(p) ? <span className="st">On this plan</span> : i === highlight && <><button className="sp-btn s" data-testid={`share-required-${p.id}`} onClick={() => send(p.id, true, p.display_name)}>+ Required</button><button className="sp-btn q end" data-testid={`share-optional-${p.id}`} onClick={() => send(p.id, false, p.display_name)}>+ Optional</button></>}
            </div>)}{pasted.filter((email) => !found.some((p) => p.email.toLowerCase() === email)).map((email) => <div className="r dim" key={email}><i className="av">?</i><span className="nm">{email}</span><span className="st">Not in your organization</span></div>)}</div>
            {!found.length && !pasted.length ? <div className="msg" data-testid="share-search-empty"><span className="grow">{team ? `Nobody in ${teamName} matches.` : "Nobody matches."}</span>{(team || role) && <button className="sp-btn q end" onClick={() => { setTeam(""); setRole(""); }}>Search everyone</button>}</div>
              : <div className="foot"><span>{pasted.length ? `${found.length} of ${pasted.length} found` : `${found.length} people`}</span>{available.length > 0 && <button className="sp-btn q end" data-testid="share-add-all" onClick={() => setMenu(menu === "all" ? null : "all")}>Add all {available.length}<ShareChevron /></button>}</div>}
            {menu === "all" && <div className="menu" style={{ width: 150 }}><button className="mi" onClick={() => available.forEach((p) => send(p.id, true, p.display_name))}>As required</button><button className="mi" onClick={() => available.forEach((p) => send(p.id, false, p.display_name))}>As optional</button></div>}
          </>}
      </section>
      <section className="sec fill" data-testid="share-reviewers">
        {!rows.length ? <><div className="lab"><b>On this plan</b></div><p className="hint" style={{ marginTop: 2 }}>Nobody yet. Required reviewers sign off before the build starts. Optional reviewers never hold it.</p></> : [true, false].map((required) => {
          const group = rows.filter((r) => r.required === required);
          if (!group.length) return null;
          const failed = group.filter((r) => r.state === "failed");
          return <Fragment key={String(required)}><div className="lab"><b>{required ? "Required" : "Optional"}</b><span>{group.filter((r) => r.state === "signed_off").length} of {group.length} signed off</span></div>
            {failed.length > 1 && <button className="sp-btn q end" onClick={() => failed.forEach((r) => send(r.sub, r.action!, r.name))}>Try {failed.length === 2 ? "both" : "all"} again</button>}
            {group.map((r) => <Fragment key={r.sub}><div className={`r ${menu === r.sub ? "act" : ""}`} data-testid={`share-reviewer-${r.sub}`} data-state={r.state} data-required={r.required}>
              <i className="av">{initials(r.name)}</i><button className="nm text-left" disabled={r.state === "inviting"} onClick={() => setMenu(menu === r.sub ? null : r.sub)}>{r.name}</button>
              <span className={`st ${r.state === "signed_off" ? "ok" : r.state === "failed" ? "bad" : ""}`}>
                {r.state === "inviting" && <i className="spin" />}{r.state === "signed_off" ? <><ShareCheck />Signed off</> : r.state === "inviting" ? "Inviting…" : r.state === "failed" ? r.action === null ? "Couldn't remove" : "Couldn't invite" : "Invited"}
              </span>{r.state === "failed" && <button className="sp-btn q end" data-testid={`share-try-${r.sub}`} onClick={() => send(r.sub, r.action!, r.name)}>Try again</button>}
              {signOff?.sub === r.sub && r.state !== "signed_off" && <button className="sp-btn q end" disabled={signOff.disabled} onClick={signOff.run}>Sign off</button>}
            </div>{menu === r.sub && <div className="menu" style={{ width: 170, marginLeft: 28, marginRight: "auto" }}><button className="mi" onClick={() => send(r.sub, !r.required, r.name)}>Make {r.required ? "optional" : "required"}</button><button className="mi" onClick={() => send(r.sub, null, r.name)}>Remove from plan</button></div>}</Fragment>)}
          </Fragment>;
        })}
      </section>
    </> : <section className="sec fill">{fallback}</section>}
    {children}
  </aside>;
}
