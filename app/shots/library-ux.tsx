/* Dev-only mockups (shots/library-ux-shots.mjs) for the Library and sharing UX review: the improved
   flows drawn over the app's own tokens and classes (Settings' rows and switches, the share panel's
   CSS), with nothing in the product changed.
   ?theme=light|dark  &f=<frame>
   - location-a, location-a-moved: one choice of where plans live; what needs the servers follows it.
   - location-b, location-b-moved: today's row, with a confirm step that lists what else turns on.
   - location-locked: a member whose organisation sets some of it.
   - admin: the organisation's admin setting it for everyone.
   - share-a, share-a-add: access and sign-off as two separate things per person.
   - share-b, share-b-add: one role per person.
   - share-first: a plan nobody has been given yet.
   - recipient-inbox, recipient-library, recipient-reader: what the person shared with sees. */

import { useEffect, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { applyTheme } from "../src/components/ThemeToggle";
import { button, card, h2, label, main, notice, quiet } from "../src/components/ui";
import "../src/styles.css";
import "../src/screens/Session/Visuals/SharePanel.css";

const params = new URLSearchParams(location.search);
applyTheme(params.get("theme") === "light" ? "light" : "dark");
const frame = params.get("f") ?? "location-a";

/* -- small marks ------------------------------------------------------------------------------ */

const Check = ({ className = "" }: { className?: string }) => <svg className={`inline-block size-[11px] flex-none ${className}`} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M3.5 8.5l3 3 6-7" /></svg>;
const Chevron = () => <svg className="ic sm" viewBox="0 0 16 16" aria-hidden><path d="M4.5 6.5L8 10l3.5-3.5" /></svg>;
const Lock = () => <svg className="inline-block size-[13px] flex-none text-ink-3" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden><rect x="3.5" y="7" width="9" height="6.5" rx="1.2" /><path d="M5.5 7V5a2.5 2.5 0 015 0v2" /></svg>;
const Close = () => <svg className="ic" viewBox="0 0 16 16" aria-hidden><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" /></svg>;
const People = () => <svg className="size-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden><circle cx="6" cy="5" r="2.5" /><path d="M1.5 14v-2a4.5 4.5 0 019 0v2M11 3a2.5 2.5 0 010 5m2 6v-2a4 4 0 00-1-2.5" /></svg>;

/* Settings' own switch and two-way control, as DocSwitches draws them. */
function Switch({ on, disabled }: { on: boolean; disabled?: boolean }) {
  return (
    <span role="switch" aria-checked={on} className={`relative inline-block h-5 w-9 shrink-0 rounded-full border ${disabled ? "opacity-50" : ""} ${on ? "border-branch bg-branch" : "border-line-strong bg-surface-2"}`}>
      <span aria-hidden className={`absolute top-0.5 size-3.5 rounded-full ${on ? "left-[18px] bg-on-branch" : "left-0.5 bg-ink-3"}`} />
    </span>
  );
}
function Seg({ options, value, wide }: { options: string[]; value: string; wide?: boolean }) {
  return (
    <span role="radiogroup" className="inline-flex shrink-0 rounded-md border border-line-strong p-0.5">
      {options.map((o) => <span key={o} role="radio" aria-checked={o === value} className={`rounded py-0.5 text-center text-xs ${wide ? "min-w-[104px] px-s1" : "px-s1"} ${o === value ? "bg-branch text-on-branch" : "text-ink-2"}`}>{o}</span>)}
    </span>
  );
}
function Radio({ on }: { on: boolean }) {
  return <span aria-hidden className={`mt-[3px] grid size-[15px] flex-none place-items-center rounded-full ${on ? "bg-branch" : "shadow-[inset_0_0_0_1.5px_var(--line-strong)]"}`}>{on && <span className="size-[5px] rounded-full bg-on-branch" />}</span>;
}
function Box({ on }: { on: boolean }) {
  return <span aria-hidden className={`mt-[2px] grid size-[15px] flex-none place-items-center rounded-[3px] ${on ? "bg-branch text-on-branch" : "shadow-[inset_0_0_0_1.5px_var(--line-strong)]"}`}>{on && <Check className="!size-[10px]" />}</span>;
}

/* -- Settings › Library ------------------------------------------------------------------------- */

function Settings({ children, crumb = "General", width = 960 }: { children: ReactNode; crumb?: string; width?: number }) {
  return (
    <div id="frame" className="bg-canvas font-sans text-ink" style={{ width }}>
      <div className="flex items-center gap-2 border-b border-line px-6 py-2.5 text-[13px] text-ink-3">Settings<span aria-hidden>›</span><b className="font-semibold text-ink">{crumb}</b></div>
      <div className="flex flex-col gap-s3 px-6 pb-7 pt-6">{children}</div>
    </div>
  );
}
function Head({ title, blurb }: { title: string; blurb: string }) {
  return <header className="flex flex-wrap items-baseline gap-x-s2"><h2 className={h2}>{title}</h2><span className="text-xs text-ink-3">{blurb}</span></header>;
}
function Row({ name, help, children, dim }: { name: string; help: ReactNode; children?: ReactNode; dim?: boolean }) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-s3 px-s2 py-s1">
      <div className="min-w-0">
        <div className={`text-sm font-medium ${dim ? "text-ink-3" : ""}`}>{name}</div>
        <p className="text-xs text-ink-3">{help}</p>
      </div>
      {children}
    </div>
  );
}
const HELP = {
  share: "Share plans with your teams and PawDelta, for review or to read.",
  assist: "New work starts with a brief from the plans that match it.",
  import_upload: "Upload the folders you add to the Library, so briefs can draw on them.",
  prompt_analysis: "Keep a record of which plans your prompts matched. Never the prompt itself.",
};

/* Option A: where plans live is one choice; the switches that need the servers sit under it and follow it. */
function LocationA({ moved }: { moved: boolean }) {
  const needs = <span className="text-xs text-ink-3">Needs brindle servers</span>;
  return (
    <Settings>
      <section className="flex flex-col gap-s1">
        <Head title="Library" blurb="Where your approved plans live, and what brindle does with them." />
        <div className={`${card} flex flex-col`}>
          <div className="px-s2 pb-1 pt-s1 text-sm font-medium">Keep my plans</div>
          <div role="radiogroup" className="flex flex-col pb-s1">
            <div className="grid grid-cols-[15px_minmax(0,1fr)] gap-x-2.5 px-s2 py-1.5">
              <Radio on={!moved} />
              <div><div className="text-sm">On this Mac</div><p className="text-xs text-ink-3">Private to you. Nothing is uploaded. When you share a plan, only that plan goes to brindle servers.</p></div>
            </div>
            <div className="grid grid-cols-[15px_minmax(0,1fr)] gap-x-2.5 px-s2 py-1.5">
              <Radio on={moved} />
              <div><div className="text-sm">On brindle servers</div><p className="text-xs text-ink-3">Encrypted in your Library, on every Mac you sign in to. Turns on Sharing and Briefs.</p></div>
            </div>
          </div>
          {moved && (
            <div className="px-s2 pb-s2">
              <p role="status" className={`${notice} flex items-center gap-2`}>
                <Check className="text-pass" /><span className="flex-1"><b className="font-semibold">6 plans moved to brindle servers.</b> Sharing and Briefs are on.</span>
                <span className={`${quiet} -my-1 text-xs`}>Move back</span>
              </p>
            </div>
          )}
        </div>
        <div className={`${label} px-s2 pt-s1`}>With plans on brindle servers</div>
        <div className={`${card} flex flex-col divide-y divide-line`}>
          <Row name="Sharing" help={moved ? HELP.share : "On this Mac you can still share one plan at a time from its Share panel."} dim={!moved}>{moved ? <Switch on /> : <span className="text-xs text-ink-3">One plan at a time</span>}</Row>
          <Row name="Briefs" help={HELP.assist} dim={!moved}>{moved ? <Switch on /> : needs}</Row>
          <Row name="Imported folders" help={HELP.import_upload} dim={!moved}>{moved ? <Switch on={false} /> : needs}</Row>
          <Row name="Prompt analysis" help={HELP.prompt_analysis} dim={!moved}>{moved ? <Switch on={false} /> : needs}</Row>
        </div>
      </section>
    </Settings>
  );
}

/* Option B: today's row and switches, and a confirm step that says what moves and what else turns on. */
function LocationB({ moved }: { moved: boolean }) {
  const pick = (on: boolean, name: string, what: string) => (
    <div className="grid grid-cols-[15px_minmax(0,1fr)] gap-x-2.5 py-1"><Box on={on} /><div className="text-[13px]"><span className="font-medium">{name}</span><span className="text-ink-3"> · {what}</span></div></div>
  );
  return (
    <Settings>
      <section className="flex flex-col gap-s1">
        <Head title="Library" blurb="Where your approved plans live, and what brindle does with them." />
        <div className={`${card} flex flex-col divide-y divide-line`}>
          <div>
            <Row name="Library location" help={moved ? <span className="inline-flex items-center gap-1.5 text-pass"><Check />6 plans moved to brindle servers · Sharing and Briefs turned on</span> : "Where your approved plans are kept."}>
              <Seg options={["This Mac", "brindle servers"]} value={moved ? "brindle servers" : "This Mac"} />
            </Row>
            {!moved && (
              <div className="mx-s2 mb-s2 rounded-md border border-line-strong bg-canvas-2 px-s2 py-s2">
                <div className="text-sm font-semibold">Move 6 plans to brindle servers?</div>
                <p className="mt-0.5 text-xs text-ink-2">They are encrypted in your Library and removed from this Mac. Plans you approve from now on go there too.</p>
                <div className={`${label} mt-s2 mb-1`}>Also turn on</div>
                {pick(true, "Sharing", "share plans with your teams and PawDelta")}
                {pick(true, "Briefs", "new work starts with a brief from matching plans")}
                {pick(false, "Imported folders", "upload the folders you add to the Library")}
                {pick(false, "Prompt analysis", "record which plans your prompts matched")}
                <div className="mt-s2 flex items-center gap-2"><span className={main}>Move 6 plans</span><span className={quiet}>Cancel</span></div>
              </div>
            )}
          </div>
          <Row name="Sharing" help={HELP.share}><Switch on={moved} /></Row>
          <Row name="Briefs" help={HELP.assist}><Switch on={moved} /></Row>
          <Row name="Imported folders" help={HELP.import_upload}><Switch on={false} /></Row>
          <Row name="Prompt analysis" help={HELP.prompt_analysis}><Switch on={false} /></Row>
        </div>
      </section>
    </Settings>
  );
}

/* A member whose organisation sets some of it: the value as text, one plain reason, someone to ask. */
function LocationLocked() {
  const set = (value: string) => <span className="inline-flex items-center gap-1.5 text-[13px] text-ink"><Lock />{value}</span>;
  return (
    <Settings>
      <section className="flex flex-col gap-s1">
        <Head title="Library" blurb="Where your approved plans live, and what brindle does with them." />
        <div className={`${card} flex flex-col divide-y divide-line`}>
          <Row name="Keep my plans" help="Sovereign Systems keeps everyone's plans on brindle servers.">{set("On brindle servers")}</Row>
          <Row name="Sharing" help="Sovereign Systems has sharing on for everyone.">{set("On")}</Row>
          <Row name="Briefs" help={HELP.assist}><Switch on /></Row>
          <Row name="Imported folders" help={HELP.import_upload}><Switch on={false} /></Row>
          <Row name="Prompt analysis" help="Sovereign Systems has this off for everyone.">{set("Off")}</Row>
        </div>
        <p className="px-s2 text-xs text-ink-3">3 of these are set by Sovereign Systems. Its admins, Priya Nair and Tom Okafor, can change them.</p>
      </section>
    </Settings>
  );
}

/* The organisation's admin: each setting left to people, or the same for everyone, in the app. */
function Admin() {
  const line = (name: string, help: string, options: string[], value: string, extra?: ReactNode) => (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-s3 px-s2 py-s1">
      <div className="min-w-0"><div className="text-sm font-medium">{name}</div><p className="text-xs text-ink-3">{help}</p></div>
      <span className="flex items-center gap-2.5">{extra}<Seg options={options} value={value} wide /></span>
    </div>
  );
  const starts = (v: string) => <span className="inline-flex items-center gap-1 border-b border-line-strong pb-px text-xs text-ink-2">starts {v}<svg className="size-[10px]" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden><path d="M4.5 6.5L8 10l3.5-3.5" /></svg></span>;
  const choice = ["People choose", "On for everyone", "Off for everyone"];
  return (
    <Settings width={1040}>
      <section className="flex flex-col gap-s1">
        <Head title="Library for everyone at Sovereign Systems" blurb="Admins only. 400 people in 6 teams." />
        <div className={`${card} flex flex-col divide-y divide-line`}>
          {line("Where plans are kept", "138 people keep their plans on this Mac today.", ["People choose", "This Mac", "brindle servers"], "brindle servers")}
          {line("Sharing", "Plans can be shared with teams and the organisation.", choice, "On for everyone")}
          {line("Briefs", "New work starts with a brief from matching plans.", choice, "People choose", starts("on"))}
          {line("Imported folders", "Folders people add to the Library are uploaded.", choice, "People choose", starts("off"))}
          {line("Prompt analysis", "Record which plans people's prompts matched.", choice, "Off for everyone")}
          {line("Share links", "Who a plan's link opens for.", ["Anyone with the link", "People here only", "No links"], "People here only")}
        </div>
        <div className="flex items-center justify-between px-s2 pt-1 text-xs text-ink-3">
          <span><b className="font-medium text-ink-2">Platform engineering</b> sets 2 of these differently for its 31 people.<span className={`${quiet} ml-1 text-xs`}>Edit team</span></span>
          <span className={`${quiet} text-xs`}>+ Set for a team</span>
        </div>
        <div className="mt-s1 flex items-center gap-s2 rounded-md bg-surface-2 px-s2 py-s1">
          <span className="flex-1 text-[13px] text-ink-2"><b className="font-semibold text-ink">1 change not saved.</b> 138 people's plans move to brindle servers the next time they open brindle. Each of them is told in their Inbox.</span>
          <span className={quiet}>Discard</span><span className={main}>Save for 400 people</span>
        </div>
      </section>
    </Settings>
  );
}

/* -- the share panel ----------------------------------------------------------------------------- */

const AV: Record<string, string> = { MC: "var(--branch)", AM: "var(--group-4)", SR: "var(--group-2)", JM: "var(--group-8)", LC: "var(--group-1)" };
function Av({ who, team }: { who: string; team?: boolean }) {
  if (team) return <i className="av" style={{ display: "block", borderRadius: 5 }}>{who}</i>;
  return <i className="av" style={{ display: "block", ...(AV[who] ? { background: AV[who], color: "var(--canvas)" } : {}) }}>{who}</i>;
}
const Sel = ({ children }: { children: ReactNode }) => <span className="sel flex-none !py-[2px] text-[12px]">{children}<Chevron /></span>;

/* A slice of the plan beside the panel: its title and a status line that says who it is shared with. */
function Doc({ status, children, wide, foot }: { status: ReactNode; children: ReactNode; wide?: boolean; foot?: ReactNode }) {
  return (
    <div id="frame" className="flex bg-canvas text-ink" style={{ width: wide ? 1040 : 880, height: wide ? 470 : 600 }}>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex h-[37px] flex-none items-center justify-between gap-3 border-b border-line px-4 font-sans text-[13px]">
          <span className="flex min-w-0 items-center gap-2"><span className="text-ink-3">‹</span><b className="truncate font-semibold">Cloud workers for Pro</b></span>
          <span className="flex items-center gap-2"><span className={`${quiet} text-xs`}>Viewing ▾</span><span className={`${button} size-7 shrink-0 !border-transparent bg-branch-soft !p-1.5 text-branch`}><People /></span></span>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden px-9 pt-7">
          <h1 className="m-0 font-serif text-[25px] font-semibold leading-[1.2] tracking-[-0.01em]">Cloud workers for Pro</h1>
          <div className="mt-2.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 font-sans text-[12.5px] text-ink-2">{status}</div>
          <p className="mt-5 font-serif text-[15px] leading-[1.68] text-ink-2">Pro sessions keep running on a machine the customer controls; results land as reviewed pull requests.</p>
          <div className="mt-5 border-t border-line pt-4 font-serif text-[17px] font-semibold">1 · Context</div>
          <p className="mt-2 font-serif text-[15px] leading-[1.68] text-ink-2">A session stops when the laptop lid closes. Teams asked for work that carries on overnight and is ready to review in the morning, without handing their code to a machine they don't run.</p>
        </div>
        {foot && <div className="flex min-h-[52px] flex-none items-center gap-3 border-t border-line-strong bg-canvas-2 px-4 font-sans">{foot}</div>}
      </div>
      {children}
    </div>
  );
}
const dot = <span aria-hidden className="inline-block size-[6px] rounded-full bg-sap" />;
const sharedStatus = <><span className="inline-flex items-center gap-1.5 font-medium text-sap">{dot}In team review</span><span>Shared with Founding Team and 3 people</span><span className="text-ink-3">·</span><span>1 of 2 signed off</span></>;

function Panel({ children, link = true }: { children: ReactNode; link?: boolean }) {
  return (
    <aside className="share-panel" aria-label="Share">
      <div className="sph"><b>Share</b><span className="sp-btn q"><Close /></span></div>
      {children}
      {link && (
        <section className="sec">
          <div className="lab"><b>Link</b><span className="sp-btn q end">Turn off</span></div>
          <div className="line"><Sel>Anyone in PawDelta</Sel><Sel>Can view</Sel></div>
          <div className="line"><span className="url">https://pawdelta.com/share/cloud-workers-for-pro</span><span className="sp-btn s">Copy link</span></div>
        </section>
      )}
    </aside>
  );
}
function Search({ typed }: { typed?: string }) {
  return <div className="fld" style={typed ? { borderColor: "var(--branch)" } : undefined}><input readOnly placeholder="Add people or teams" defaultValue={typed} /><span className="sp-btn q end">Filter<Chevron /></span></div>;
}
/* A person on the plan: name, a line under it about sign-off, and what they may do. */
function Person({ who, name, note, tone, right, team, act }: { who: string; name: ReactNode; note?: ReactNode; tone?: "ok" | "wait"; right?: ReactNode; team?: boolean; act?: boolean }) {
  return (
    <div className={`r ${act ? "act" : ""}`} style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
      <span className="mt-[2px]"><Av who={who} team={team} /></span>
      <span className="grid min-w-0 flex-1">
        <span className="nm">{name}</span>
        {note && <span className={`inline-flex items-center gap-1 text-[11.5px] leading-[1.45] ${tone === "ok" ? "text-pass" : tone === "wait" ? "text-sap" : "text-ink-3"}`}>{tone === "ok" && <Check />}{note}</span>}
      </span>
      {right}
    </div>
  );
}
const you = <em>you</em>;

/* Option A: what someone may do, and whether the build waits for them, are two separate things. */
function ShareA({ adding }: { adding: boolean }) {
  return (
    <Doc status={sharedStatus}>
      <Panel link={!adding}>
        <section className="sec">
          <Search typed={adding ? "lee" : undefined} />
          {adding && (
            <>
              <div className="rows"><Person act who="LC" name={<>Lee Chen<em>lee.chen@pawdelta.com</em></>} /></div>
              <div className="menu" style={{ width: "auto", margin: "6px 0 2px" }}>
                <div className="ml">Lee can</div>
                <div className="mi">View<span className="text-[11.5px] text-ink-3">read it</span></div>
                <div className="mi" aria-checked="true">Comment<span className="inline-flex items-center gap-1.5 text-[11.5px] text-ink-3">comment and suggest<Check className="text-ink" /></span></div>
                <div className="mi">Edit<span className="text-[11.5px] text-ink-3">write in the draft</span></div>
                <div className="mh !font-sans" style={{ display: "grid", gridTemplateColumns: "15px 1fr", columnGap: 8, padding: "8px 8px 6px" }}>
                  <Box on={false} /><span className="text-[12px] text-ink">Must sign off<span className="text-ink-3"> · the build waits for Lee</span></span>
                </div>
                <div className="flex justify-end gap-1.5 px-1 pb-1 pt-1.5"><span className="sp-btn q">Cancel</span><span className="sp-btn s" style={{ background: "var(--branch)", borderColor: "var(--branch)", color: "var(--on-branch)" }}>Add Lee</span></div>
              </div>
            </>
          )}
        </section>
        <section className="sec fill">
          <div className="lab"><b>Who has access</b><span>4 people · 1 team</span></div>
          <Person who="MC" name={<>Maya Chen{you}</>} note="Author" right={<span className="st">Owner</span>} />
          <Person who="AM" name="Alexandria Montgomery-Worthington" note="Signed off r3" tone="ok" right={<Sel>Can edit</Sel>} />
          <Person who="SR" name="Sam Rivera" note="Must sign off · not yet" tone="wait" right={<Sel>Can comment</Sel>} />
          {!adding && <Person who="JM" name="Josephine van der Meer-Sutherland" right={<Sel>Can view</Sel>} />}
          <Person who="FT" team name="Founding Team" note="12 people · everyone in the team" right={<Sel>Can view</Sel>} />
          {!adding && <p className="hint">The build starts when Sam signs off. Alexandria already has.</p>}
        </section>
      </Panel>
    </Doc>
  );
}

/* Option B: one role per person; the list is grouped by role. */
function ShareB({ adding }: { adding: boolean }) {
  return (
    <Doc status={sharedStatus}>
      <Panel link={!adding}>
        <section className="sec">
          <Search typed={adding ? "lee" : undefined} />
          {adding && (
            <>
              <div className="rows"><Person act who="LC" name={<>Lee Chen<em>lee.chen@pawdelta.com</em></>} /></div>
              <div className="menu" style={{ width: "auto", margin: "6px 0 2px" }}>
                <div className="ml">Add Lee as</div>
                <div className="mi" aria-checked="true">Reviewer<span className="text-[11.5px] text-ink-3">must sign off before the build</span></div>
                <div className="mi">Optional reviewer<span className="text-[11.5px] text-ink-3">comments, never holds it</span></div>
                <div className="mi">Editor<span className="text-[11.5px] text-ink-3">writes in the draft</span></div>
                <div className="mi">Viewer<span className="text-[11.5px] text-ink-3">reads it</span></div>
              </div>
            </>
          )}
        </section>
        <section className="sec fill">
          <div className="lab"><b>Reviewers</b><span>1 of 2 signed off</span></div>
          <Person who="AM" name="Alexandria Montgomery-Worthington" right={<span className="st ok"><Check />Signed off</span>} />
          <Person who="SR" name="Sam Rivera" right={<span className="st" style={{ color: "var(--sap)" }}>Not yet</span>} />
          {!adding && <><div className="lab"><b>Optional reviewers</b></div>
            <Person who="JM" name="Josephine van der Meer-Sutherland" right={<span className="st">Invited</span>} /></>}
          <div className="lab"><b>Editors</b></div>
          <Person who="MC" name={<>Maya Chen{you}</>} right={<span className="st">Owner</span>} />
          <div className="lab"><b>Viewers</b></div>
          <Person who="FT" team name={<>Founding Team<em>12 people</em></>} />
          {!adding && <p className="hint">Reviewers comment and suggest. Editors write in the draft. Click a name to change its role.</p>}
        </section>
      </Panel>
    </Doc>
  );
}

/* A plan nobody has been given yet: the same panel, not a different form. */
function ShareFirst() {
  return (
    <Doc status={<><span className="inline-flex items-center gap-1.5 font-medium text-ink-2"><Lock />Only you</span><span className="text-ink-3">·</span><span>Not shared yet</span></>}>
      <Panel link={false}>
        <section className="sec">
          <Search />
          <p className="hint">Only you can open this plan. Add a person or a team to share it.</p>
        </section>
        <section className="sec fill">
          <div className="lab"><b>Your teams</b></div>
          <Person who="FT" team name={<>Founding Team<em>12 people</em></>} right={<span className="sp-btn s">Share</span>} />
          <Person who="PD" team name={<>PawDelta<em>everyone · 34 people</em></>} right={<span className="sp-btn s">Share</span>} />
          <div className="lab"><b>PawDelta asks for</b></div>
          <Person who="§" team name="Security review" note="1 of the Security team signs off · this plan touches src/auth" />
          <p className="hint" style={{ marginTop: 10 }}>This plan is kept on this Mac. Sharing uploads this one plan to brindle servers; your other plans stay here.</p>
        </section>
        <section className="sec">
          <div className="lab"><b>Link</b></div>
          <p className="hint" style={{ marginTop: 0 }}>A link is offered once the plan is shared.</p>
        </section>
      </Panel>
    </Doc>
  );
}

/* -- what the person shared with sees ------------------------------------------------------------ */

function Face({ who, tone = "var(--group-4)" }: { who: string; tone?: string }) {
  return <span className="grid size-[26px] flex-none place-items-center rounded-full font-mono text-[10px] font-semibold text-canvas" style={{ background: tone }}>{who}</span>;
}
function InboxRow({ face, title, by, what, access, when, action, unread = true }: { face: ReactNode; title: string; by: string; what: string; access: string; when: string; action: string; unread?: boolean }) {
  return (
    <div className="grid grid-cols-[10px_26px_minmax(0,1fr)_auto_auto] items-center gap-x-3 border-b border-line py-2.5">
      <span aria-hidden className={`size-[5px] rounded-full ${unread ? "bg-ink" : ""}`} />
      {face}
      <div className="min-w-0">
        <div className={`truncate text-[14px] ${unread ? "font-semibold" : "text-ink-2"}`}>{title}</div>
        <div className="truncate text-[12.5px] text-ink-3"><span className="font-medium text-ink-2">{by}</span> {what} <span aria-hidden>·</span> <span className="text-ink-2">{access}</span></div>
      </div>
      <span className="font-mono text-[11.5px] text-ink-3">{when}</span>
      <span className={`${button} text-xs`}>{action}</span>
    </div>
  );
}
function RecipientInbox() {
  const group = (tone: string, name: string, count: string, aside: string) => (
    <div className="flex items-center justify-between pb-1 pt-4"><span className={`${label} inline-flex items-center gap-2`}><span aria-hidden className="size-[7px] rounded-full" style={{ background: tone }} />{name}<span className="font-mono text-ink-3">{count}</span></span><span className="text-xs text-ink-3">{aside}</span></div>
  );
  return (
    <div id="frame" className="bg-canvas px-7 pb-7 pt-6 font-sans text-ink" style={{ width: 960 }}>
      <h1 className="m-0 text-[22px] font-semibold tracking-[-0.01em]">Inbox</h1>
      {group("var(--sap)", "Waiting on you", "1", "what's stopped comes first")}
      <InboxRow face={<Face who="AR" tone="var(--group-2)" />} title="Add CSV export to reports" by="Ana Ruiz" what="needs your sign-off before the build" access="you can comment and suggest" when="just now" action="Review" />
      {group("var(--ink-3)", "From the team", "2 new", "nothing here is waiting on you")}
      <InboxRow face={<Face who="MC" tone="var(--branch)" />} title="Cloud workers for Pro" by="Maya Chen" what="shared this with you" access="you can edit" when="5m" action="Open" />
      <InboxRow face={<Face who="MC" tone="var(--branch)" />} title="OS sandbox for worker shells" by="Maya Chen" what="shared this with Founding Team" access="you can view" when="1h" action="Open" />
      <InboxRow unread={false} face={<Face who="CW" tone="var(--group-1)" />} title="Rate limits for the public API" by="Chen Wei" what="made you an editor" access="you could only comment before" when="3h" action="Open" />
    </div>
  );
}

function RecipientLibrary() {
  const cols = "grid grid-cols-[minmax(0,2.2fr)_minmax(0,1fr)_minmax(0,1.5fr)_minmax(0,1.05fr)_60px] items-center gap-x-5";
  const row = (title: string, sub: string, stage: ReactNode, shared: string, by: string, access: string, changed: string) => (
    <div className={`${cols} border-b border-line py-2.5`}>
      <div className="min-w-0"><div className="truncate text-[14px] font-semibold">{title}</div><div className="truncate text-[12.5px] text-ink-3">{sub}</div></div>
      <div className="text-[12.5px]">{stage}</div>
      <div className="min-w-0"><div className="truncate text-[12.5px] text-ink-2">{shared}</div><div className="truncate text-[11.5px] text-ink-3">{by}</div></div>
      <div className="text-[12.5px] text-ink-2">{access}</div>
      <div className="text-right text-[12.5px] text-ink-3">{changed}</div>
    </div>
  );
  const stage = (tone: string, text: string) => <span className="inline-flex items-center gap-1.5" style={{ color: tone }}><span aria-hidden className="size-[5px] rounded-full bg-current" />{text}</span>;
  return (
    <div id="frame" className="bg-canvas px-7 pb-6 pt-6 font-sans text-ink" style={{ width: 1040 }}>
      <div className="flex items-baseline gap-3"><h1 className="m-0 text-[22px] font-semibold tracking-[-0.01em]">All designs</h1><span className="text-xs text-ink-3">5 in 2 repos</span></div>
      <div className={`${cols} ${label} mt-5 border-b border-line-strong pb-2`}><span>Design</span><span>Stage</span><span>Shared with</span><span>You can</span><span className="text-right">Changed</span></div>
      {row("Cloud workers for Pro", "Pro sessions keep running on a machine the customer controls…", stage("var(--sap)", "Your review"), "Founding Team and 3 people", "by Maya Chen", "Comment · must sign off", "5h")}
      {row("OS sandbox for worker shells", "Worker shells run under the OS sandbox with the worktree…", stage("var(--ink-2)", "In review"), "Founding Team", "by Maya Chen", "View", "3d")}
      {row("Rate limits for the public API", "Per-key limits with a burst allowance; a refusal says when to come back.", stage("var(--ink-2)", "In review"), "PawDelta and 2 people", "by Chen Wei", "Edit", "1d")}
      {row("Sidebar at every window size", "The context gives way first when the window is short…", stage("var(--ink-2)", "Shipped"), "Founding Team", "by you", "Owner", "Nov 7")}
      {row("Release flow: gate on CI", "Build first, then tag: a release tag only exists for a commit…", stage("var(--ink-2)", "Drafting"), "Only you", "kept on this Mac", "Owner", "Oct 30")}
    </div>
  );
}

/* A teammate who may only read: the page says so, says who decides, and offers the one way forward. */
function RecipientReader() {
  return (
    <Doc wide
      foot={<><span className={label}>Next</span><span className="flex-1 text-[13.5px] font-semibold">Read it. To comment, ask Maya.</span><span className="text-xs text-ink-3">1 of 2 required sign-offs · you aren't one of them</span></>}
      status={<><span className="inline-flex items-center gap-1.5 font-medium text-sap">{dot}In team review</span><span>Shared with Founding Team by Maya Chen</span><span className="text-ink-3">·</span><span className="inline-flex items-center gap-1.5 font-medium text-ink"><Lock />You can view</span></>}>
      <aside className="share-panel" aria-label="Share">
        <div className="sph"><b>Share</b><span className="sp-btn q"><Close /></span></div>
        <section className="sec">
          <div className="flex items-start gap-2.5">
            <span className="mt-[3px]"><Lock /></span>
            <div className="min-w-0 flex-1"><div className="text-[13px] font-medium text-ink">You can view this plan</div><p className="hint" style={{ marginTop: 1 }}>You're in Founding Team, which Maya shared it with. Commenting needs Maya's say.</p></div>
          </div>
          <div className="mt-2.5 flex items-center gap-1.5"><span className="sp-btn s" style={{ background: "var(--branch)", borderColor: "var(--branch)", color: "var(--on-branch)" }}>Ask to comment</span><span className="sp-btn s">Ask to edit</span></div>
        </section>
        <section className="sec fill">
          <div className="lab"><b>Who has access</b><span>4 people · 1 team</span></div>
          <Person who="MC" name="Maya Chen" note="Author" right={<span className="st">Owner</span>} />
          <Person who="AM" name="Alexandria Montgomery-Worthington" note="Signed off r3" tone="ok" right={<span className="st">Can edit</span>} />
          <Person who="SR" name="Sam Rivera" note="Must sign off · not yet" tone="wait" right={<span className="st">Can comment</span>} />
          <Person who="FT" team name={<>Founding Team<em>you're here</em></>} note="12 people" right={<span className="st">Can view</span>} />
        </section>
      </aside>
    </Doc>
  );
}

const FRAMES: Record<string, ReactNode> = {
  "location-a": <LocationA moved={false} />, "location-a-moved": <LocationA moved />,
  "location-b": <LocationB moved={false} />, "location-b-moved": <LocationB moved />,
  "location-locked": <LocationLocked />, admin: <Admin />,
  "share-a": <ShareA adding={false} />, "share-a-add": <ShareA adding />,
  "share-b": <ShareB adding={false} />, "share-b-add": <ShareB adding />,
  "share-first": <ShareFirst />,
  "recipient-inbox": <RecipientInbox />, "recipient-library": <RecipientLibrary />, "recipient-reader": <RecipientReader />,
};

function Page() {
  useEffect(() => { document.documentElement.dataset.ready = "1"; }, []);
  return <>{FRAMES[frame] ?? FRAMES["location-a"]}</>;
}
createRoot(document.getElementById("root")!).render(<Page />);
