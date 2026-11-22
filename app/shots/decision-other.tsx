/* Dev-only mockups (shots/decision-other-shots.mjs): an "Other" answer on a planning doc's decision
   card, in three treatments, over the app's own tokens and the real card's classes (PlanDesignDoc's
   DecisionCard), with nothing in the product changed.
   ?theme=light|dark  &t=inline|ghost|under  &s=open|typing|saved|verdict  &w=narrow
   - inline: "Other…" is the last option row; picking it opens a short field in the row.
   - ghost: "+ Your own answer" sits under the rows; it adds a row of its own, which a teammate can
     then pick too.
   - under: a field always under the rows ("None of these?"); typing in it is the pick. */

import { useEffect, useRef, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { applyTheme } from "../src/components/ThemeToggle";
import { button, main, pill, quiet } from "../src/components/ui";
import { Tick } from "../src/screens/Session/Visuals/PlanDesignDoc";
import "../src/styles.css";

const params = new URLSearchParams(location.search);
applyTheme(params.get("theme") === "light" ? "light" : "dark");
type Treatment = "inline" | "ghost" | "under";
type State = "open" | "typing" | "saved" | "verdict";
const t = (params.get("t") ?? "inline") as Treatment;
const s = (params.get("s") ?? "open") as State;
const narrow = params.get("w") === "narrow";

const CUSTOM = "only on federal holidays";
const OPTIONS = [
  { id: "A", label: "A · Skip on weekends", pros: ["Quietest hours for the database"], cons: ["Monday's queue is twice as long"] },
  { id: "B", label: "B · Never skip", pros: ["One rule, nothing to remember"], cons: ["Runs into Saturday's maintenance window"] },
  { id: "C", label: "C · Skip on release days", pros: ["Keeps the release pipeline free"], cons: ["Release days move; the job needs the calendar"] },
];

/* Initials in a colour, as PlanCards' Avatar draws them (the person in branch, a teammate in a group colour). */
function Face({ who }: { who: "you" | "david" }) {
  return (
    <span title={who === "you" ? "You" : "David"} style={{ backgroundColor: who === "you" ? "var(--branch)" : "var(--group-3)" }}
      className="inline-grid size-4 flex-none place-items-center rounded-full font-mono text-[7.5px] font-semibold leading-none text-canvas">
      {who === "you" ? "MC" : "DK"}
    </span>
  );
}

function Mark({ chosen }: { chosen: boolean }) {
  return (
    <span aria-hidden className={`relative mt-[3px] size-[15px] rounded-full ${chosen ? "bg-pass-soft" : "shadow-[inset_0_0_0_1.5px_var(--line-strong)]"}`}>
      {chosen && <span className="absolute left-[5px] top-[2.5px] h-2 w-1 rotate-45 border-b-[1.75px] border-r-[1.75px] border-solid border-pass" />}
    </span>
  );
}

const rowBase = "relative grid grid-cols-[16px_minmax(0,1fr)] gap-x-3 border-t border-line px-4 pb-3 pt-[11px]";

/* A listed option, as DecisionCard draws it. */
function Option({ o, chosen, done, votes = [] }: { o: (typeof OPTIONS)[number]; chosen: boolean; done: boolean; votes?: ("you" | "david")[] }) {
  return (
    <div className={`${rowBase} cursor-pointer`}>
      <Mark chosen={chosen} />
      <span className="min-w-0">
        <span className={`flex flex-wrap items-center gap-[9px] text-sm leading-[1.45] ${done && !chosen ? "font-medium text-ink-3" : done ? "font-semibold text-ink" : "font-medium text-ink"}`}>
          {o.label}
          {o.id === "A" && <span className="rounded-sm px-1.5 text-[11px] font-normal leading-[17px] text-ink-2 shadow-[inset_0_0_0_1px_var(--line-strong)]">Recommended</span>}
          {votes.length > 0 && <span className="ml-auto inline-flex items-center gap-1">{votes.map((v) => <Face key={v} who={v} />)}</span>}
        </span>
        <span className={`mt-[3px] grid gap-px text-[13px] leading-normal ${done && !chosen ? "text-ink-3" : "text-ink-2"}`}>
          {o.pros.map((p) => <span key={p} className="grid grid-cols-[12px_minmax(0,1fr)] gap-1"><span aria-hidden className="font-mono text-ink-3">+</span>{p}</span>)}
          {o.cons.map((c) => <span key={c} className="grid grid-cols-[12px_minmax(0,1fr)] gap-1"><span aria-hidden className="font-mono text-ink-3">−</span>{c}</span>)}
        </span>
      </span>
    </div>
  );
}

/* The short field: the app's underline field, in the card's type. */
function OwnField({ value, focus, placeholder = "Say what you'd do instead" }: { value: string; focus?: boolean; placeholder?: string }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (focus) { ref.current?.focus(); ref.current?.setSelectionRange(value.length, value.length); } }, [focus, value]);
  return (
    <input ref={ref} defaultValue={value} placeholder={placeholder} maxLength={140} aria-label="Your own answer"
      className="w-full min-w-0 rounded-none border-0 border-b border-line-strong bg-transparent px-0 py-1 font-sans text-[13.5px] leading-[1.45] text-ink placeholder:text-ink-3 focus:border-branch focus:outline-none" />
  );
}

const hint = "text-[12px] leading-normal text-ink-3";
const ownTag = "rounded-sm px-1.5 text-[11px] font-normal leading-[17px] text-ink-2 shadow-[inset_0_0_0_1px_var(--line-strong)]";

function Card() {
  const picked = s !== "open";
  const typing = s === "typing";
  const saved = s === "saved" || s === "verdict";
  const waiting = !picked;
  const whoPicked = typing ? "" : saved ? `You: Other: ‘${CUSTOM}’ · David: B` : "David: B";
  const listedVotes = (id: string): ("you" | "david")[] => (id === "B" ? ["david"] : []);

  /* Treatment A: the last row is "Other…". */
  const inlineRow = (
    <div className={`${rowBase} cursor-pointer ${typing ? "bg-[color-mix(in_srgb,var(--ink)_3%,transparent)]" : ""}`} data-option="other">
      <Mark chosen={picked} />
      <span className="grid min-w-0 gap-1.5">
        <span className={`flex flex-wrap items-center gap-[9px] text-sm leading-[1.45] ${picked ? "font-semibold text-ink" : "font-medium text-ink"}`}>
          {saved ? <>Other: <span className="font-normal text-ink-2">‘{CUSTOM}’</span></> : "Other…"}
          {saved && <span className="ml-auto inline-flex items-center gap-1"><Face who="you" /></span>}
        </span>
        {!picked && <span className={hint}>None of these fit? Write your own answer.</span>}
        {typing && (
          <span className="grid gap-1">
            <OwnField value={CUSTOM} focus />
            <span className={`${hint} flex justify-between gap-3`}><span>Saved as you type. The supervisor sees it with your verdict.</span><span className="font-mono text-[11px]">{CUSTOM.length}/140</span></span>
          </span>
        )}
        {saved && <span className={hint}><button type="button" className="cursor-pointer border-0 bg-transparent p-0 text-ink-3 underline decoration-line-strong underline-offset-2 hover:text-ink">Edit</button></span>}
      </span>
    </div>
  );

  /* Treatment B: a quiet link that adds a row of the person's own. */
  const ghostRow = picked && (
    <div className={`${rowBase} cursor-pointer ${typing ? "border-t-[color-mix(in_srgb,var(--branch)_45%,transparent)] bg-[color-mix(in_srgb,var(--branch)_5%,transparent)]" : ""}`} data-option="own">
      <Mark chosen />
      <span className="grid min-w-0 gap-1.5">
        {typing ? (
          <>
            <span className="text-sm font-semibold leading-[1.45] text-ink">Your own answer</span>
            <OwnField value={CUSTOM} focus />
            <span className={`${hint} flex gap-3`}>
              <span className="flex-1">Teammates can pick it too.</span>
              <button type="button" className="cursor-pointer border-0 bg-transparent p-0 text-ink-3 hover:text-ink">Remove</button>
            </span>
          </>
        ) : (
          <>
            <span className="flex flex-wrap items-center gap-[9px] text-sm font-semibold leading-[1.45] text-ink">
              ‘{CUSTOM}’
              <span className={ownTag}>Your answer</span>
              <span className="ml-auto inline-flex items-center gap-1"><Face who="you" /></span>
            </span>
            <span className={hint}>Written by you · David can pick it too</span>
          </>
        )}
      </span>
    </div>
  );
  const ghostLink = !picked && (
    <div className="border-t border-dashed border-line px-4 py-2.5">
      <button type="button" className="inline-flex cursor-pointer items-center gap-2 border-0 bg-transparent p-0 font-sans text-[13px] text-ink-3 hover:text-ink">
        <span aria-hidden className="grid size-[15px] place-items-center rounded-full font-mono text-[12px] leading-none shadow-[inset_0_0_0_1.5px_var(--line)]">+</span>
        Your own answer
      </button>
    </div>
  );

  /* Treatment C: a field under the rows, always there. */
  const underField = (
    <div className={`border-t px-4 pb-3 pt-2.5 ${picked ? "border-line" : "border-dashed border-line"}`} data-option="under">
      <div className="grid grid-cols-[16px_minmax(0,1fr)] items-start gap-x-3">
        {picked ? <Mark chosen /> : <span aria-hidden className="mt-[5px] text-center font-mono text-[12px] leading-none text-ink-3">✎</span>}
        <span className="grid min-w-0 gap-1">
          {saved ? (
            <span className="flex flex-wrap items-center gap-[9px] text-sm leading-[1.45]">
              <span className="font-semibold text-ink">Other: <span className="font-normal text-ink-2">‘{CUSTOM}’</span></span>
              <span className="ml-auto inline-flex items-center gap-1"><Face who="you" /></span>
            </span>
          ) : (
            <>
              <span className={`text-[12px] font-medium uppercase tracking-[0.08em] ${picked ? "text-ink-2" : "text-ink-3"}`}>None of these?</span>
              <OwnField value={typing ? CUSTOM : ""} focus={typing} placeholder="Type your own answer; it replaces the pick above" />
            </>
          )}
          {typing && <span className={hint}>Your own answer is your pick. Clear it to pick a listed option again.</span>}
        </span>
      </div>
    </div>
  );

  return (
    <section className={`relative min-w-0 [overflow-wrap:anywhere] mb-1.5 mt-[26px] rounded-md border border-l-2 font-sans ${waiting ? "border-[color-mix(in_srgb,var(--sap)_42%,transparent)] border-l-sap bg-[color-mix(in_srgb,var(--sap)_5%,transparent)]" : "border-line border-l-line-strong"}`}>
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 pt-3">
        <p className="m-0 text-[11px] font-medium uppercase leading-[1.3] tracking-[0.08em] text-ink-3">Decision 2 of 3</p>
        {waiting ? <span className={`${pill} text-sap`}>Needs your pick</span>
          : <Tick>Decided{s !== "verdict" && <span className="font-normal text-ink-3">· change it any time before you approve</span>}</Tick>}
      </div>
      <h3 className="m-0 mt-1 px-4 text-base font-semibold leading-[1.35] tracking-[-0.01em] text-ink">When should the nightly cleanup skip a night?</h3>
      {whoPicked && <p className="m-0 mt-1 px-4 text-[12.5px] leading-normal text-ink-3">{whoPicked}</p>}
      <div className="mt-3">
        {OPTIONS.map((o) => <Option key={o.id} o={o} chosen={false} done={picked} votes={listedVotes(o.id)} />)}
        {t === "inline" && inlineRow}
        {t === "ghost" && ghostRow}
        {t === "ghost" && ghostLink}
        {t === "under" && underField}
      </div>
      <div className="flex items-baseline justify-between gap-4 border-t border-line px-4 pb-[11px] pt-2.5">
        <p className="m-0 text-[12.5px] leading-normal text-ink-3"><b className="font-medium text-ink-2">The case against the recommendation:</b> weekends are when the team ships hotfixes.</p>
        <button type="button" className="cursor-pointer border-0 bg-transparent p-0 font-sans text-xs whitespace-nowrap text-ink-3">Comment</button>
      </div>
    </section>
  );
}

function Outline() {
  const row = "flex items-baseline gap-2 rounded-md px-2 py-[3px] text-[12.5px] leading-[1.4]";
  const dec = (title: string, open: boolean) => (
    <span className={`${row} pl-[26px] ${open ? "text-ink" : "text-ink-3"}`}>
      <span aria-hidden className="w-2.5 flex-none text-center font-mono text-[10px] leading-none text-pass">{open ? <span className="inline-block size-1.5 -translate-y-px rounded-full bg-sap" /> : "✓"}</span>
      {title}
    </span>
  );
  const sec = (n: number, title: string, cur = false) => (
    <span className={`${row} ${cur ? "font-medium text-ink" : "text-ink-2"}`}><span className="w-2.5 flex-none font-mono text-[10.5px] text-ink-3">{n}</span>{title}</span>
  );
  const open = s === "open";
  return (
    <nav className="w-[224px] shrink-0 pb-6 pl-3.5 pr-2.5 pt-[30px] font-sans">
      <div className="flex justify-between gap-2 px-2 pb-1.5 text-[10.5px] font-semibold uppercase leading-[1.4] tracking-[0.08em] text-ink-3">
        <span>On this page</span><span className="font-normal normal-case tracking-[0.02em]">{open ? "2 of 3 decided" : "3 of 3 decided"}</span>
      </div>
      <div className="grid">
        {sec(1, "Context")}{sec(2, "Goals")}{sec(3, "Proposed design", true)}
        {dec("Where the job runs", false)}{dec("When should the nightly cleanup skip a night?", open)}
        {sec(4, "Risks")}{dec("Who gets paged", false)}{sec(5, "Rollout")}{sec(6, "Open questions")}
      </div>
    </nav>
  );
}

function Footer() {
  if (s === "verdict") {
    return (
      <footer className="flex min-h-[60px] flex-wrap items-center justify-between gap-x-5 gap-y-3 border-t border-line-strong bg-canvas-2 px-4 py-2.5 font-sans">
        <div role="status" className="grid min-w-0 gap-px">
          <span className="flex items-center gap-2 text-[13.5px] font-semibold text-ink"><Tick className="!text-[13.5px] !font-semibold">Everything is decided</Tick></span>
          <span className="text-xs text-ink-3">Approve starts the build with your answers:</span>
          <span className="mt-1 grid gap-px text-[12.5px] text-ink-2">
            <span><span className="text-ink-3">Where the job runs · </span>A · Your own server</span>
            <span><span className="text-ink-3">Nightly cleanup · </span><span className="font-medium text-ink">Other: ‘{CUSTOM}’</span> <span className={`${ownTag} ml-1 align-[1px]`}>your own answer</span></span>
            <span><span className="text-ink-3">Who gets paged · </span>B · The on-call owner</span>
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" className={quiet}>Cancel planning</button>
          <button type="button" className={button}>Request changes</button>
          <button type="button" className={main}>Approve</button>
        </div>
      </footer>
    );
  }
  const left = s === "open";
  return (
    <footer className="flex min-h-[60px] flex-wrap items-center justify-between gap-x-5 gap-y-3 border-t border-line-strong bg-canvas-2 px-4 py-2.5 font-sans">
      <div role="status" className="grid gap-px">
        {left ? (
          <>
            <span className={`${pill} !text-[13.5px] !font-semibold text-sap`}>1 left to decide</span>
            <span className="text-xs text-ink-3">Next: <span className="text-ink-2">When should the nightly cleanup skip a night? ↓</span></span>
          </>
        ) : (
          <>
            <span className="flex items-center gap-2 text-[13.5px] font-semibold text-ink"><Tick className="!text-[13.5px] !font-semibold">Everything is decided</Tick></span>
            <span className="text-xs text-ink-3">Approve starts the build.</span>
          </>
        )}
      </div>
      <div className="flex items-center gap-2">
        <button type="button" className={quiet}>Cancel planning</button>
        <button type="button" className={button}>Request changes</button>
        <button type="button" className={main} disabled={left}>Approve</button>
      </div>
    </footer>
  );
}

function Reader({ children }: { children: ReactNode }) {
  return (
    <div id="frame" className="flex flex-col overflow-hidden bg-canvas text-ink" style={{ width: narrow ? 390 : 1180 }}>
      <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5 font-sans">
        <span className="flex min-w-0 items-center gap-2 text-[13px]">
          <span className="text-ink-3">‹</span>
          <b className="truncate font-semibold text-ink">Nightly cleanup for shared repos</b>
          <span className="whitespace-nowrap text-xs text-ink-3">r2 · shared with David</span>
        </span>
        {!narrow && <span className="flex items-center gap-2 text-xs text-ink-3"><Face who="david" /> David is reading <span className={`${quiet} ml-2`}>Viewing ▾</span></span>}
      </header>
      <div className="flex min-h-0">
        {!narrow && <Outline />}
        <main className={`min-w-0 flex-1 ${narrow ? "px-4" : "px-10"} pb-8 pt-[30px]`}>
          <div className={`mx-auto font-serif text-[15.5px] leading-[1.68] text-ink-2 ${narrow ? "w-full" : "w-[min(660px,100%)]"}`}>
            <h2 className="m-0 mb-3 font-serif text-[21.5px] font-semibold leading-[1.25] tracking-[-0.005em] text-ink">3 · Proposed design</h2>
            <p className="m-0">The cleanup job prunes merged worktrees and stale branches once a night. It is cheap, but it holds the repo lock for a few minutes, so some nights it should stand aside.</p>
            {children}
          </div>
        </main>
      </div>
      <Footer />
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<Reader><Card /></Reader>);
requestAnimationFrame(() => requestAnimationFrame(() => document.documentElement.setAttribute("data-ready", "")));
