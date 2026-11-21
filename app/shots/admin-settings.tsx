/* Dev-only mockups (shots/admin-settings-shots.mjs) of an organisation's Library settings: what its
   admins set for everyone, and how a member reads it. Drawn over the app's own tokens and Settings'
   own rows, rail, fields and switches, with nothing in the product changed.
   ?theme=light|dark  &f=<frame>
   - a-admin, a-member: one sheet. A row per setting, what it is set to, and whether members can change it.
   - b-admin, b-admin-edit, b-member: read first. Two lists by who decides; a row opens in place to change. */

import { useEffect, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import Logo from "../src/components/Logo";
import { applyTheme } from "../src/components/ThemeToggle";
import { card, field, h2, label, lede, main, pill, quiet, title } from "../src/components/ui";
import "../src/styles.css";

const params = new URLSearchParams(location.search);
applyTheme(params.get("theme") === "light" ? "light" : "dark");
const frame = params.get("f") ?? "a-admin";

const ORG = "Sovereign Systems";

/* -- the app's shell: the sidebar on Settings (components/SettingsRail.tsx) and the page's header -- */

const ICONS: Record<string, string> = {
  account: "M12 4a4 4 0 1 0 0 8 4 4 0 0 0 0-8M5 21v-2a7 7 0 0 1 14 0v2",
  usage: "M4 19h16M7 16V10M12 16V6M17 16v-4",
  general: "M12 3v3M12 18v3M3 12h3M18 12h3M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6",
  agents: "M12 4a3 3 0 1 0 0 6 3 3 0 0 0 0-6M5 20v-1a5 5 0 0 1 5-5h4a5 5 0 0 1 5 5v1M4 9h2M18 9h2",
  providers: "M4 5h16v5H4zM4 14h16v5H4z",
  permissions: "M12 3l7 3v5c0 5-3.5 8-7 10-3.5-2-7-5-7-10V6z",
  format: "M6 3h9l4 4v14H6zM9 10h7M9 14h7M9 18h4",
  storage: "M5 6c0-3 14-3 14 0s-14 3-14 0v12c0 3 14 3 14 0V6M5 12c0 3 14 3 14 0",
  project: "M4 7a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z",
  organisation: "M4 21V5a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v16M14 10h5a1 1 0 0 1 1 1v10M2.5 21h19M7.5 8h3M7.5 12h3M7.5 16h3",
  about: "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16M12 11v5M12 8h.01",
};
const RAIL: { group?: string; id: string; name: string; admin?: boolean; rule?: boolean }[] = [
  { group: "You", id: "account", name: "Account" },
  { id: "usage", name: "Usage" },
  { group: "brindle", id: "general", name: "General" },
  { id: "agents", name: "Agents" },
  { id: "providers", name: "Workers & providers" },
  { id: "permissions", name: "Permissions" },
  { id: "format", name: "Doc format" },
  { id: "storage", name: "Storage" },
  { group: "This project", id: "project", name: "Project" },
  { group: ORG, id: "organisation", name: "Organisation", admin: true },
  { id: "about", name: "About", rule: true },
];

function Shell({ who, tab, about, children }: { who: string; tab: string; about: string; children: ReactNode }) {
  const admin = tab === "organisation";
  const initials = who.split(" ").map((w) => w[0]).join("");
  return (
    <div id="frame" className="flex bg-canvas font-sans text-ink" style={{ width: 1320, minHeight: 700 }}>
      <aside className="flex w-[352px] shrink-0 flex-col gap-[10px] border-r border-line bg-canvas-2 px-[12px] pb-[12px] pt-[14px] text-[13px]">
        <div className="flex shrink-0 items-center gap-s1 px-1.5 pb-1">
          <Logo className="text-[17px]" />
          <span className="flex-1" />
          <span className="flex items-center gap-1 rounded-md border border-pass/30 bg-pass-soft/60 px-2 py-0.5 text-[11.5px] font-medium text-ink-2"><span className="text-sm leading-none" aria-hidden>+</span>New session</span>
        </div>
        <div className="flex flex-col gap-1">
          <span className="rounded px-2 py-1 text-[12.5px] text-ink-2">← Sessions</span>
          <div className="mb-1 flex items-center gap-2 border-b border-line px-2 pb-3 pt-1.5">
            <span className="flex size-7 shrink-0 items-center justify-center rounded-full border border-pass/35 bg-pass-soft font-mono text-[10px] text-pass">{initials}</span>
            <span className="truncate text-[12.5px] font-medium">{who}</span>
          </div>
          <div className="flex flex-col gap-px">
            {RAIL.filter((item) => admin || !item.admin).map((item) => (
              <div key={item.id} className="contents">
                {item.group && <div className="px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-ink-3">{item.group}</div>}
                {item.rule && <div className="mx-1 mb-1 mt-2 border-t border-line" />}
                <span className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-[12.5px] ${tab === item.id ? "bg-surface-2 font-medium text-ink shadow-[inset_2px_0_0_var(--pass)] [&_svg]:text-pass" : "text-ink-2"}`}>
                  <svg aria-hidden width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d={ICONS[item.id]} /></svg>
                  {item.name}
                </span>
              </div>
            ))}
          </div>
        </div>
      </aside>
      <main className="flex min-w-0 flex-1 flex-col gap-s3 p-s3">
        <header>
          <h1 className={title}>Settings</h1>
          <p className={`mt-0.5 ${lede}`}>{about}</p>
        </header>
        {children}
      </main>
    </div>
  );
}
const ADMIN = { who: "Priya Nair", tab: "organisation", about: `What ${ORG} sets for its 400 people. Only owners and admins see this page.` };
const MEMBER = { who: "Sam Rivera", tab: "general", about: "Your settings apply to every project; a project can override them." };

/* -- Settings' own pieces ----------------------------------------------------------------------- */

const Check = () => <svg className="size-[10px]" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M3.5 8.5l3 3 6-7" /></svg>;
const Lock = () => <svg className="inline-block size-[13px] flex-none text-ink-3" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden><rect x="3.5" y="7" width="9" height="6.5" rx="1.2" /><path d="M5.5 7V5a2.5 2.5 0 015 0v2" /></svg>;

/* The Library's switch, as DocSwitches draws it. */
function Switch({ on, name }: { on: boolean; name: string }) {
  return (
    <span role="switch" aria-checked={on} aria-label={name} className={`relative inline-block h-5 w-9 shrink-0 rounded-full border ${on ? "border-branch bg-branch" : "border-line-strong bg-surface-2"}`}>
      <span aria-hidden className={`absolute top-0.5 size-3.5 rounded-full ${on ? "left-[18px] bg-on-branch" : "left-0.5 bg-ink-3"}`} />
    </span>
  );
}
/* A choice, as Settings draws one: the underlined field. */
function Choice({ value, name }: { value: string; name: string }) {
  return <select aria-label={name} className={`${field} w-full`} defaultValue={value}><option>{value}</option></select>;
}
function Radio({ on }: { on: boolean }) {
  return <span aria-hidden className={`mt-[3px] grid size-[15px] flex-none place-items-center rounded-full ${on ? "bg-branch" : "shadow-[inset_0_0_0_1.5px_var(--line-strong)]"}`}>{on && <span className="size-[5px] rounded-full bg-on-branch" />}</span>;
}
function Box({ on }: { on: boolean }) {
  return <span aria-hidden className={`mt-[3px] grid size-[15px] flex-none place-items-center rounded-[3px] ${on ? "bg-branch text-on-branch" : "shadow-[inset_0_0_0_1.5px_var(--line-strong)]"}`}>{on && <Check />}</span>;
}
function Head({ blurb }: { blurb: string }) {
  return <header className="flex flex-wrap items-baseline gap-x-s2 gap-y-0.5"><h2 className={h2}>Library</h2><span className="text-xs text-ink-3">{blurb}</span></header>;
}
function Name({ name, help, badge }: { name: string; help: ReactNode; badge?: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-x-s1 text-sm font-medium">{name}{badge}</div>
      <p className="text-xs text-ink-3">{help}</p>
    </div>
  );
}

/* What the organisation governs, in the order a member's Library section lists it. */
const HELP = {
  store: "On each person's Mac, or encrypted on brindle servers.",
  share: "The widest a plan can be shared. People choose who, plan by plan.",
  links: "Who a plan's link can open for.",
  assist: "New work starts with a brief from the plans that match it.",
  imports: "Folders people add to the Library are uploaded, so briefs can draw on them.",
  prompts: "A record of which plans people's prompts matched. Never the prompt itself.",
};
const YOURS = {
  store: "Where your approved plans are kept.",
  share: "Who you can share a plan with.",
  links: "Who a plan's link opens for.",
  assist: "New work starts with a brief from the plans that match it.",
  imports: "Upload the folders you add to the Library, so briefs can draw on them.",
  prompts: "Keep a record of which plans your prompts matched. Never the prompt itself.",
};
const REACH = "People, teams and everyone here";
const LINKS = `People at ${ORG}`;
const MOVE = "Their plans move to brindle servers the next time they open brindle, and each of them is told in their Inbox.";
const ASK = "Priya Nair or Tom Okafor can change these.";

/* -- A: one sheet -------------------------------------------------------------------------------- */

const SHEET = "grid grid-cols-[minmax(0,1fr)_250px_150px] items-center gap-x-s3 px-s2";

function AdminA() {
  const line = (name: string, help: string, value: string, members: boolean | null, badge?: ReactNode) => (
    <div className={`${SHEET} py-s1`}>
      <Name name={name} help={help} badge={badge} />
      <Choice name={name} value={value} />
      {members === null ? <span className="text-xs text-ink-3">Chosen per plan</span> : <Switch on={members} name={`Members can change ${name}`} />}
    </div>
  );
  return (
    <Shell {...ADMIN}>
      <section className="flex flex-col gap-s1">
        <Head blurb="What everyone gets. Where members can change a setting, this is what it starts as." />
        <div>
          <div className={`${SHEET} pb-1 text-[11px] font-medium uppercase tracking-wider text-ink-3`}><span /><span className="pl-s1">Set to</span><span>Members can change</span></div>
          <div className={`${card} flex flex-col divide-y divide-line`}>
            {line("Where plans are kept", HELP.store, "brindle servers", false, <span className={`${pill} text-ink-2`}>edited</span>)}
            {line("Sharing", HELP.share, REACH, null)}
            {line("Plan links", HELP.links, LINKS, null)}
            {line("Briefs", HELP.assist, "On", true)}
            {line("Imported folders", HELP.imports, "Off", true)}
            {line("Prompt analysis", HELP.prompts, "Off", false)}
            <div className="flex items-center gap-s2 px-s2 py-s1">
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium">1 change, for 400 people</div>
                <p className="text-xs text-ink-3">138 people keep their plans on their Mac today. {MOVE}</p>
              </div>
              <span className={quiet}>Discard</span>
              <span className={main}>Save</span>
            </div>
          </div>
        </div>
        <p className="px-s2 text-xs text-ink-3">Members see what is set here in their own Settings. Every change is recorded with who made it and when.</p>
      </section>
    </Shell>
  );
}

function MemberA() {
  const fact = (value: string) => <span className="inline-flex items-center gap-1.5 text-[13px]"><Lock />{value}</span>;
  const line = (name: string, help: string, control: ReactNode) => (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-s3 px-s2 py-s1"><Name name={name} help={help} />{control}</div>
  );
  return (
    <Shell {...MEMBER}>
      <section className="flex flex-col gap-s1">
        <Head blurb="Where your approved plans live, and what brindle does with them." />
        <div className={`${card} flex flex-col divide-y divide-line`}>
          {line("Library location", YOURS.store, fact("brindle servers"))}
          {line("Sharing", YOURS.share, fact(REACH))}
          {line("Plan links", YOURS.links, fact(LINKS))}
          {line("Briefs", YOURS.assist, <Switch on name="Briefs" />)}
          {line("Imported folders", YOURS.imports, <Switch on={false} name="Imported folders" />)}
          {line("Prompt analysis", YOURS.prompts, fact("Off"))}
        </div>
        <p className="flex items-center gap-1.5 px-s2 text-xs text-ink-3"><Lock />Set by {ORG}. {ASK}</p>
      </section>
    </Shell>
  );
}

/* -- B: read first, change one at a time ---------------------------------------------------------- */

const LIST = "grid grid-cols-[minmax(0,1fr)_280px_64px] items-center gap-x-s3 px-s2 py-s1";

function Group({ name, note, children }: { name: string; note: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline gap-s2 px-s2"><span className={label}>{name}</span><span className="text-xs text-ink-3">{note}</span></div>
      <div className="flex flex-col">{children}</div>
    </div>
  );
}

function AdminB({ editing }: { editing: boolean }) {
  const line = (name: string, help: string, value: string, count?: string) => (
    <div className={`${LIST} border-t border-line`}>
      <Name name={name} help={help} />
      <div className="min-w-0"><div className="text-sm">{value}</div>{count && <div className="text-xs text-ink-3">{count}</div>}</div>
      <span className={`${quiet} justify-self-end text-xs`}>Change</span>
    </div>
  );
  const option = (on: boolean, name: string, what: string) => (
    <div className="grid grid-cols-[15px_minmax(0,1fr)] gap-x-2.5"><Radio on={on} /><div><div className="text-sm">{name}</div><p className="text-xs text-ink-3">{what}</p></div></div>
  );
  return (
    <Shell {...ADMIN}>
      <section className="flex flex-col gap-s2">
        <Head blurb="What everyone gets, as members read it. Change one setting at a time." />
        <Group name="Set for everyone" note="Members see the value and can't change it.">
          {line("Sharing", HELP.share, REACH)}
          {line("Plan links", HELP.links, LINKS)}
          {line("Prompt analysis", HELP.prompts, "Off")}
        </Group>
        <Group name="Members choose" note="Each person starts with the value shown.">
          {editing ? (
            <div className="flex flex-col gap-s1 border-t border-branch px-s2 pb-s2 pt-s1">
              <div className="flex items-baseline justify-between gap-s2">
                <Name name="Where plans are kept" help={HELP.store} />
                <span className="text-xs font-medium uppercase tracking-wider text-branch">Changing</span>
              </div>
              <div role="radiogroup" aria-label="Where plans are kept" className="flex flex-col gap-1.5">
                {option(true, "brindle servers", "Encrypted in each person's Library, and on every Mac they sign in to.")}
                {option(false, "Each person's Mac", "Nothing is uploaded. A shared plan is sent on its own.")}
              </div>
              <div className="grid grid-cols-[15px_minmax(0,1fr)] gap-x-2.5 border-t border-line pt-s1">
                <Box on={false} /><div className="text-sm">Members can change this<span className="text-ink-3"> · unticked, it moves to Set for everyone</span></div>
              </div>
              <p className="text-[13px] text-ink-2"><b className="font-semibold text-ink">138 people keep their plans on their Mac today.</b> {MOVE}</p>
              <div className="flex items-center justify-end gap-s1"><span className={quiet}>Cancel</span><span className={main}>Save for 400 people</span></div>
            </div>
          ) : line("Where plans are kept", HELP.store, "Starts on each person's Mac", "138 on their Mac · 262 on brindle servers")}
          {line("Briefs", HELP.assist, "Starts on", "371 of 400 have it on")}
          {line("Imported folders", HELP.imports, "Starts off", "44 of 400 have it on")}
        </Group>
        <p className="px-s2 text-xs text-ink-3">Every change is recorded with who made it and when.</p>
      </section>
    </Shell>
  );
}

function MemberB() {
  const line = (name: string, help: string, control: ReactNode) => (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-s3 border-t border-line px-s2 py-s1"><Name name={name} help={help} />{control}</div>
  );
  const fact = (value: string) => <span className="text-[13px]">{value}</span>;
  return (
    <Shell {...MEMBER}>
      <section className="flex flex-col gap-s2">
        <Head blurb="Where your approved plans live, and what brindle does with them." />
        <Group name={`Set by ${ORG}`} note={ASK}>
          {line("Library location", YOURS.store, fact("brindle servers"))}
          {line("Sharing", YOURS.share, fact(REACH))}
          {line("Plan links", YOURS.links, fact(LINKS))}
          {line("Prompt analysis", YOURS.prompts, fact("Off"))}
        </Group>
        <Group name="Yours to change" note="">
          {line("Briefs", YOURS.assist, <Switch on name="Briefs" />)}
          {line("Imported folders", YOURS.imports, <Switch on={false} name="Imported folders" />)}
        </Group>
      </section>
    </Shell>
  );
}

const FRAMES: Record<string, ReactNode> = {
  "a-admin": <AdminA />, "a-member": <MemberA />,
  "b-admin": <AdminB editing={false} />, "b-admin-edit": <AdminB editing />, "b-member": <MemberB />,
};

function Page() {
  useEffect(() => { document.documentElement.dataset.ready = "1"; }, []);
  return <>{FRAMES[frame] ?? FRAMES["a-admin"]}</>;
}
createRoot(document.getElementById("root")!).render(<Page />);
