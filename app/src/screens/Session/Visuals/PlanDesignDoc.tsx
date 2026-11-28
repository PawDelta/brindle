/* The planning doc as the owner approved it (design: plan-doc-redesign.html), for a plan on the
   design-doc spine (brindle/plan_docs.py SPINE), in every view of it: the author's own plan,
   the same plan shared for team review (live, or frozen for sign-off), and a teammate reading
   it. PlanReader.tsx keeps the state and the writes; this file is its parts:
   - the outline on the left ("On this page"): each numbered spine section, with the decisions
     anchored in it listed under it, a dot on the ones still open and a tick on the decided;
   - each decision as a card inline at the end of its section: "Decision N of M", its options as
     rows with their pros and cons and a Recommended tag, and the case against at the bottom;
   - comments and suggestions in the margin beside the text they are on;
   - the bottom bar's menus (Cancel planning, Request changes) and the toolbar's (mode, history).
   No worker split or file lists: brindle handles the split once the plan is approved. */

import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import type { Plan } from "../../../api/client";
import type { PlanDecisionDTO, PlanSectionDTO, PlanThreadDTO } from "../../../api/schema";
import { ago } from "../../../components/ProviderConnect";
import { button, field, pill, quiet } from "../../../components/ui";
import type { ThreadActions } from "./DocReview";
import type { DocLayout } from "./DocText";
import { Avatar, personName, picksLine } from "./PlanCards";
import type { Mode, MODES } from "./PlanReader";

/* The spine's ids in reading order; the extras sit inside the section before them. */
const SPINE = new Set(["context", "audience", "goals", "proposed-design", "architecture", "tech-stack", "alternatives", "risks", "rollout", "open-questions"]);
const NESTED: Record<string, string> = { audience: "context", architecture: "proposed-design", "tech-stack": "proposed-design" };
const OPEN_QUESTIONS = "open-questions";

/* How a section's text is laid out on the page (DocText.tsx). */
const LAYOUTS: Record<string, DocLayout> = { goals: "goals", alternatives: "alternatives", "tech-stack": "tech-stack", risks: "risks", rollout: "rollout" };
export const docLayout = (section: string): DocLayout => LAYOUTS[section] ?? "doc";

/* Whether a plan follows the design-doc spine: every new plan does; an older free-form one doesn't. */
export const onSpine = (plan: Plan) => plan.sections.some((s) => SPINE.has(s.id));

/* A numbered section of the doc, with the extras that sit inside it (Audience, Architecture, Tech stack). */
export interface SpinePart {
  section: PlanSectionDTO;
  n: number;
  subs: PlanSectionDTO[];
}

export function spineParts(sections: PlanSectionDTO[]): SpinePart[] {
  const parts: SpinePart[] = [];
  for (const s of sections) {
    const host = NESTED[s.id] ? parts.find((p) => p.section.id === NESTED[s.id]) : undefined;
    if (host) host.subs.push(s);
    else parts.push({ section: s, n: parts.length + 1, subs: [] });
  }
  return parts;
}

/* Each decision under the section whose end shows its card (its `section`); one naming no section
   on the page goes to Open questions, else to the last section. Key "" is a doc with no sections. */
export function placeDecisions(parts: SpinePart[], decisions: PlanDecisionDTO[]): Map<string, PlanDecisionDTO[]> {
  const ids = new Set(parts.flatMap((p) => [p.section.id, ...p.subs.map((s) => s.id)]));
  const fallback = ids.has(OPEN_QUESTIONS) ? OPEN_QUESTIONS : parts.at(-1)?.section.id ?? "";
  const out = new Map<string, PlanDecisionDTO[]>();
  for (const d of decisions) {
    const at = d.section && ids.has(d.section) ? d.section : fallback;
    out.set(at, [...(out.get(at) ?? []), d]);
  }
  return out;
}

/* The decisions in the order they come up in the doc. */
export const decisionsInOrder = (parts: SpinePart[], placed: Map<string, PlanDecisionDTO[]>) =>
  [...parts.flatMap((p) => [p.section, ...p.subs].flatMap((s) => placed.get(s.id) ?? [])), ...(placed.get("") ?? [])];

const show = (el: Element | null | undefined) => el?.scrollIntoView?.({ block: "start", behavior: "smooth" });
export const link = "cursor-pointer border-0 bg-transparent p-0 font-sans text-xs text-ink-3 hover:text-ink hover:underline disabled:cursor-not-allowed disabled:opacity-45";
const tick = "inline-flex items-center gap-1.5 text-xs font-medium text-pass";

/* A pill whose mark is a tick, not a dot. */
export function Tick({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <span className={`${tick} ${className}`}>
      <span aria-hidden className="font-mono text-[11px] font-semibold leading-none">✓</span>
      {children}
    </span>
  );
}

/* A button with its panel under it (`down`) or over it (`up`). Escape or a click outside closes it. */
export function Menu({ label, buttonClass, place, panelClass = "", open, onOpenChange, children, testid, title, keepMounted = false, again }: {
  label: ReactNode; buttonClass: string; place: "down" | "up"; panelClass?: string; open: boolean; onOpenChange(open: boolean): void; children: ReactNode; testid?: string; title?: string;
  /* Names the action the menu confirms, so a Try again elsewhere can open this menu for it. */
  again?: string;
  /* The panel stays mounted while closed, only hidden: what it holds (the history's comparison) outlives closing it. */
  keepMounted?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) onOpenChange(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onOpenChange(false);
    };
    document.addEventListener("mousedown", away);
    window.addEventListener("keydown", esc, true);
    return () => {
      document.removeEventListener("mousedown", away);
      window.removeEventListener("keydown", esc, true);
    };
  }, [open, onOpenChange]);
  return (
    <div ref={box} className="relative">
      <button type="button" className={buttonClass} aria-expanded={open} title={title} data-again-menu={again} onClick={() => onOpenChange(!open)}>{label}</button>
      {(open || keepMounted) && (
        <div data-testid={testid} hidden={!open} className={`absolute right-0 z-30 grid gap-2.5 rounded-md border border-line-strong bg-canvas p-3.5 text-left font-sans text-[12.5px] font-normal leading-normal whitespace-normal text-ink-2 shadow-[0_18px_44px_-18px_rgba(0,0,0,0.75)] ${place === "down" ? "top-[calc(100%+6px)]" : "bottom-[calc(100%+12px)]"} ${panelClass || "w-[340px] max-w-[86vw]"} ${open ? "" : "!hidden"}`}>
          {children}
        </div>
      )}
    </div>
  );
}
export const menuTitle = "m-0 text-[13px] font-semibold leading-[1.3] text-ink";
export const menuRow = "flex items-center justify-between gap-2";
export const menuNote = "min-h-[76px] w-full resize-y rounded-md border border-line-strong bg-field px-2.5 py-2 text-[13px] leading-normal text-ink placeholder:text-ink-3 focus:border-branch focus:outline-none";

/* The toolbar's mode: one word ("Viewing ▾") until the person wants another. */
export function ModeMenu({ modes, current, allowed, onPick }: { modes: typeof MODES; current: Mode; allowed(mode: Mode): boolean; onPick(mode: Mode): void }) {
  const [open, setOpen] = useState(false);
  return (
    <Menu label={<>{modes.find((m) => m.id === current)?.name} ▾</>} buttonClass={`${quiet} ${open ? "bg-surface-2" : ""}`} place="down" panelClass="w-[290px] !gap-px !p-1.5" open={open} onOpenChange={setOpen} testid="plan-mode-menu">
      <div role="radiogroup" aria-label="Mode" className="grid gap-px">
        {modes.map((m) => (
          <button key={m.id} type="button" role="radio" aria-checked={current === m.id} disabled={!allowed(m.id)}
            onClick={() => { onPick(m.id); setOpen(false); }}
            className="grid cursor-pointer gap-px rounded-md border-0 bg-transparent px-2 py-1.5 text-left hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-45">
            <b className={`text-[12.5px] font-medium ${current === m.id ? "text-branch" : "text-ink"}`}>{m.name}</b>
            <span className="text-[11.5px] text-ink-3">{m.hint}</span>
          </button>
        ))}
      </div>
    </Menu>
  );
}

/* "On this page": the numbered sections, each decision under its section with a dot while it
   waits for the person's pick and a tick once picked. */
export function Outline({ parts, placed, isOpen, decided, total, current, footer }: {
  parts: SpinePart[]; placed: Map<string, PlanDecisionDTO[]>; isOpen(d: PlanDecisionDTO): boolean; decided: number; total: number; current: string | null;
  /* Under the sections: a shared doc's sign-off sheet. */
  footer?: ReactNode;
}) {
  const row = "flex items-baseline gap-2 rounded-md px-2 py-[3px] text-[12.5px] leading-[1.4] no-underline hover:bg-surface-2 hover:text-ink";
  const go = (id: string) => (e: { preventDefault(): void }) => {
    e.preventDefault();
    show(document.getElementById(id));
  };
  const decisionRow = (d: PlanDecisionDTO) => (
    <a key={d.id} href={`#plan-decision-${d.id}`} onClick={go(`plan-decision-${d.id}`)} data-testid="outline-decision" data-decision={d.id} data-state={isOpen(d) ? "open" : "decided"}
      className={`${row} pl-[26px] ${isOpen(d) ? "text-ink" : "text-ink-3"}`}>
      <span aria-hidden className="w-2.5 flex-none text-center font-mono text-[10px] leading-none text-pass">
        {isOpen(d) ? <span className="inline-block size-1.5 -translate-y-px rounded-full bg-sap" /> : "✓"}
      </span>
      {d.title}
    </a>
  );
  return (
    <nav aria-label="On this page" data-testid="plan-outline" className="w-[224px] shrink-0 overflow-auto pb-6 pl-3.5 pr-2.5 pt-[30px] font-sans max-[900px]:hidden">
      <div className="flex justify-between gap-2 px-2 pb-1.5 text-[10.5px] font-semibold uppercase leading-[1.4] tracking-[0.08em] text-ink-3">
        <span>On this page</span>
        {total > 0 && <span className="font-normal normal-case tracking-[0.02em]" data-testid="outline-count" data-decided={decided} data-total={total}>{`${decided} of ${total} decided`}</span>}
      </div>
      <div className="grid">
        {parts.map((p) => (
          <div key={p.section.id} className="contents">
            <a href={`#plan-${p.section.id}`} onClick={go(`plan-${p.section.id}`)} data-testid="outline-section" data-section={p.section.id} aria-current={current === p.section.id}
              className={`${row} text-ink-2 aria-[current=true]:font-medium aria-[current=true]:text-ink`}>
              <span className="w-2.5 flex-none font-mono text-[10.5px] text-ink-3">{p.n}</span>
              {p.section.title}
            </a>
            {[p.section, ...p.subs].flatMap((s) => placed.get(s.id) ?? []).map(decisionRow)}
          </div>
        ))}
        {(placed.get("") ?? []).map(decisionRow)}
      </div>
      {footer && <div className="mt-5 grid border-t border-line text-[12.5px]">{footer}</div>}
    </nav>
  );
}

/* What a card does with own answers; each resolves to whether the write was saved. */
export interface OwnAnswers {
  add(text: string): Promise<boolean>;
  edit(answer: string, text: string): Promise<boolean>;
  remove(answer: string): Promise<boolean>;
}

const OWN_MAX = 500;      // brindle/plan_answers.py MAX_TEXT
const ownHint = "flex flex-wrap items-center gap-x-1.5 text-[12px] leading-normal text-ink-3";
const ownTag = "rounded-sm px-1.5 text-[11px] font-normal leading-[17px] text-ink-2 shadow-[inset_0_0_0_1px_var(--line-strong)]";
const optionRow = "relative grid grid-cols-[16px_minmax(0,1fr)] gap-x-3 border-t border-line px-4 pb-3 pt-[11px]";

function Mark({ chosen }: { chosen: boolean }) {
  return (
    <span aria-hidden className={`relative mt-[3px] size-[15px] rounded-full ${chosen ? "bg-pass-soft" : "shadow-[inset_0_0_0_1.5px_var(--line-strong)]"}`}>
      {chosen && <span className="absolute left-[5px] top-[2.5px] h-2 w-1 rotate-45 border-b-[1.75px] border-r-[1.75px] border-solid border-pass" />}
    </span>
  );
}

/* The short field an own answer is typed in (the "ghost" treatment, shots/decision-other.tsx):
   Enter saves it, Escape lets it go. Editing one's own answer offers Remove instead of Cancel. */
function OwnAnswerField({ initial, busy, onSave, onCancel, onRemove }: {
  initial: string; busy: boolean; onSave(text: string): void; onCancel(): void; onRemove?: () => void;
}) {
  const [text, setText] = useState(initial);
  const save = (e: FormEvent) => {
    e.preventDefault();
    if (text.trim() && !busy) onSave(text.trim());
  };
  return (
    <form onSubmit={save} data-testid="plan-answer-field"
      className={`${optionRow} border-t-[color-mix(in_srgb,var(--branch)_45%,transparent)] bg-[color-mix(in_srgb,var(--branch)_5%,transparent)]`}>
      <Mark chosen />
      <span className="grid min-w-0 gap-1.5">
        <span className="text-sm font-semibold leading-[1.45] text-ink">Your own answer</span>
        <input autoFocus value={text} onChange={(e) => setText(e.target.value)} disabled={busy} maxLength={OWN_MAX} aria-label="Your own answer"
          placeholder="Say what you'd do instead" onKeyDown={(e) => { if (e.key === "Escape") onCancel(); }}
          className="w-full min-w-0 rounded-none border-0 border-b border-line-strong bg-transparent px-0 py-1 font-sans text-[13.5px] leading-[1.45] text-ink placeholder:text-ink-3 focus:border-branch focus:outline-none" />
        <span className={`${ownHint} gap-3`}>
          <span className="flex-1">Teammates can pick it too.</span>
          {onRemove ? <button type="button" className={link} disabled={busy} onClick={onRemove}>Remove</button>
            : <button type="button" className={link} disabled={busy} onClick={onCancel}>Cancel</button>}
        </span>
      </span>
    </form>
  );
}

/* A decision's own answers as option rows of their own, each with its author, and the quiet
   "+ Your own answer" under them. Anyone picks one like an option; its author can reword or
   take it back until someone else has picked it. */
function OwnAnswerRows({ decision: d, open, disabled, picked, onPick, nameOf, answers }: {
  decision: PlanDecisionDTO; open: boolean; disabled: boolean; picked: string | null;
  onPick(option: string): void; nameOf?: (who: string) => string; answers?: OwnAnswers;
}) {
  const [typing, setTyping] = useState<string | null>(null);     // "new", or the id of the answer being reworded
  const [saving, setSaving] = useState(false);
  const own = d.answers ?? [];
  const named = (who: string) => (who === "person" ? "You" : nameOf?.(who) ?? personName(who));
  const done = !!picked;
  const run = async (work: () => Promise<boolean>) => {
    setSaving(true);
    const saved = await work();
    setSaving(false);
    if (saved) setTyping(null);
  };
  const mine = own.filter((a) => a.author === "person").length;
  return (
    <>
      {own.map((a) => {
        if (typing === a.id && answers) {
          return <OwnAnswerField key={a.id} initial={a.text} busy={saving || disabled} onSave={(text) => void run(() => answers.edit(a.id, text))}
            onCancel={() => setTyping(null)} onRemove={() => void run(() => answers.remove(a.id))} />;
        }
        const chosen = picked === a.id;
        const voters = d.votes?.[a.id] ?? [];
        const yours = a.author === "person";
        return (
          <label key={a.id} data-testid="plan-answer" data-answer={a.id} data-author={a.author} data-chosen={chosen || undefined}
            className={`${optionRow} transition-colors has-[:focus-visible]:outline-2 has-[:focus-visible]:-outline-offset-2 has-[:focus-visible]:outline-branch ${open ? "cursor-pointer hover:bg-[color-mix(in_srgb,var(--ink)_4%,transparent)]" : ""}`}>
            <input type="radio" name={`decision-${d.id}`} value={a.id} checked={chosen} disabled={!open || disabled} onChange={() => onPick(a.id)} className="absolute m-0 size-px opacity-0" />
            <Mark chosen={chosen} />
            <span className="grid min-w-0 gap-[3px]">
              <span className={`flex flex-wrap items-center gap-[9px] text-sm leading-[1.45] ${done && !chosen ? "font-medium text-ink-3" : done ? "font-semibold text-ink" : "font-medium text-ink"}`}>
                ‘{a.text}’
                {yours && <span className={ownTag}>Your answer</span>}
                {voters.length > 0 && (
                  <span className="ml-auto inline-flex flex-wrap items-center gap-1" data-votes={voters.length} title={`Picked by ${voters.map(named).join(", ")}`}>
                    {voters.map((v) => <Avatar key={v} who={v === "person" ? "person" : nameOf?.(v) ?? "A teammate"} small />)}
                  </span>
                )}
              </span>
              <span className={ownHint}>
                <Avatar who={yours ? "person" : nameOf?.(a.author) ?? "A teammate"} small />
                <span>{`Written by ${yours ? "you" : named(a.author)}`}</span>
                {yours && open && answers && !a.locked && (
                  <>
                    <span aria-hidden>·</span>
                    <button type="button" className={link} disabled={disabled || saving} onClick={(e) => { e.preventDefault(); setTyping(a.id); }}>Edit</button>
                  </>
                )}
              </span>
            </span>
          </label>
        );
      })}
      {typing === "new" && answers && (
        <OwnAnswerField initial="" busy={saving || disabled} onSave={(text) => void run(() => answers.add(text))} onCancel={() => setTyping(null)} />
      )}
      {open && answers && typing !== "new" && mine < 5 && own.length < 20 && (
        <div className="border-t border-dashed border-line px-4 py-2.5">
          <button type="button" data-testid="plan-add-answer" disabled={disabled} onClick={() => setTyping("new")}
            className="inline-flex cursor-pointer items-center gap-2 border-0 bg-transparent p-0 font-sans text-[13px] text-ink-3 hover:text-ink disabled:cursor-not-allowed disabled:opacity-45">
            <span aria-hidden className="grid size-[15px] place-items-center rounded-full font-mono text-[12px] leading-none shadow-[inset_0_0_0_1.5px_var(--line)]">+</span>
            Your own answer
          </button>
        </div>
      )}
    </>
  );
}

/* A decision, where its question comes up: picking an option is the person's answer. Sap marks
   one that still needs them; once picked, the card goes quiet and a tick stays. */
export function DecisionCard({ decision: d, n, of, open, disabled = false, picked, onPick, onComment, nameOf, answers }: {
  decision: PlanDecisionDTO;
  /* Own answers (brindle/plan_answers.py): add one (it becomes the pick), reword or take back one's own.
     Without it the card offers none; answers already on the card still show and can be picked. */
  answers?: OwnAnswers;
  /* A voter as the card names them ("You", "David"). */
  nameOf?: (who: string) => string;
  n: number;
  of: number;
  /* The plan still takes picks. */
  open: boolean;
  /* No pick for now (a teammate's vote is still being saved). */
  disabled?: boolean;
  /* The option chosen: the person's pick while open, the verdict's after. */
  picked: string | null;
  onPick(option: string): void;
  onComment?: () => void;
}) {
  const waiting = open && !picked;
  const done = !!picked;
  const whoPicked = picksLine(d, nameOf);
  return (
    <section id={`plan-decision-${d.id}`} role="radiogroup" aria-labelledby={`plan-q-${d.id}`} data-testid="plan-decision" data-decision={d.id} data-state={waiting ? "open" : "decided"}
      className={`relative min-w-0 [overflow-wrap:anywhere] mb-1.5 mt-[26px] scroll-mt-16 rounded-md border border-l-2 font-sans transition-colors duration-200 ${waiting ? "border-[color-mix(in_srgb,var(--sap)_42%,transparent)] border-l-sap bg-[color-mix(in_srgb,var(--sap)_5%,transparent)]" : "border-line border-l-line-strong"}`}>
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 pt-3">
        <p className="m-0 text-[11px] font-medium uppercase leading-[1.3] tracking-[0.08em] text-ink-3">{`Decision ${n} of ${of}`}</p>
        {waiting ? <span className={`${pill} text-sap`}>Needs your pick</span>
          : done ? <Tick>Decided{open && <span className="font-normal text-ink-3">· change it any time before you approve</span>}</Tick> : null}
      </div>
      <h3 id={`plan-q-${d.id}`} className="m-0 mt-1 px-4 text-base font-semibold leading-[1.35] tracking-[-0.01em] text-ink">{d.title}</h3>
      {/* Who picked what, saved as each pick is made: everyone on a shared plan sees the same. */}
      {whoPicked && <p data-testid="plan-picks" className="m-0 mt-1 px-4 text-[12.5px] leading-normal text-ink-3">{whoPicked}</p>}
      <div className="mt-3">
        {d.options.map((o) => {
          const chosen = picked === o.id;
          const pros = o.pros ?? [];
          const cons = o.cons ?? [];
          return (
            <label key={o.id} data-testid="plan-option" data-option={o.id} data-chosen={chosen || undefined}
              className={`relative grid grid-cols-[16px_minmax(0,1fr)] gap-x-3 border-t border-line px-4 pb-3 pt-[11px] transition-colors has-[:focus-visible]:outline-2 has-[:focus-visible]:-outline-offset-2 has-[:focus-visible]:outline-branch ${open ? "cursor-pointer hover:bg-[color-mix(in_srgb,var(--ink)_4%,transparent)]" : ""}`}>
              <input type="radio" name={`decision-${d.id}`} value={o.id} checked={chosen} disabled={!open || disabled} onChange={() => onPick(o.id)} className="absolute m-0 size-px opacity-0" />
              <span aria-hidden className={`relative mt-[3px] size-[15px] rounded-full ${chosen ? "bg-pass-soft" : "shadow-[inset_0_0_0_1.5px_var(--line-strong)]"}`}>
                {chosen && <span className="absolute left-[5px] top-[2.5px] h-2 w-1 rotate-45 border-b-[1.75px] border-r-[1.75px] border-solid border-pass" />}
              </span>
              <span className="min-w-0">
                <span className={`flex flex-wrap items-center gap-[9px] text-sm leading-[1.45] ${done && !chosen ? "font-medium text-ink-3" : done ? "font-semibold text-ink" : "font-medium text-ink"}`}>
                  {o.label}
                  {d.mine === o.id && <span className="rounded-sm px-1.5 text-[11px] font-normal leading-[17px] text-ink-2 shadow-[inset_0_0_0_1px_var(--line-strong)]" data-testid="plan-recommended">Recommended</span>}
                  {/* A shared doc's votes: who picked this option. */}
                  {(d.votes?.[o.id]?.length ?? 0) > 0 && (
                    <span className="ml-auto inline-flex flex-wrap items-center gap-1" data-votes={d.votes![o.id].length} title={`Voted for by ${d.votes![o.id].map((who) => who === "person" ? "You" : nameOf?.(who) ?? "A teammate").join(", ")}`}>
                      {d.votes![o.id].map((v) => <Avatar key={v} who={v === "person" ? "person" : nameOf?.(v) ?? "A teammate"} small />)}
                    </span>
                  )}
                </span>
                {(pros.length > 0 || cons.length > 0 || o.detail) && (
                  <span className={`mt-[3px] grid gap-px text-[13px] leading-normal ${done && !chosen ? "text-ink-3" : "text-ink-2"}`}>
                    {pros.map((p, i) => <span key={`p${i}`} className="grid grid-cols-[12px_minmax(0,1fr)] gap-1"><span aria-hidden className="font-mono text-ink-3">+</span>{p}</span>)}
                    {cons.map((c, i) => <span key={`c${i}`} className="grid grid-cols-[12px_minmax(0,1fr)] gap-1"><span aria-hidden className="font-mono text-ink-3">−</span>{c}</span>)}
                    {!pros.length && !cons.length && o.detail && <span>{o.detail}</span>}
                  </span>
                )}
              </span>
            </label>
          );
        })}
        <OwnAnswerRows decision={d} open={open} disabled={disabled} picked={picked} onPick={onPick} nameOf={nameOf} answers={answers} />
      </div>
      {(d.challenge || onComment) && (
        <div className="flex items-baseline justify-between gap-4 border-t border-line px-4 pb-[11px] pt-2.5">
          <p className="m-0 text-[12.5px] leading-normal text-ink-3">
            {d.challenge && (d.mine ? <><b className="font-medium text-ink-2">The case against the recommendation:</b> {d.challenge}</> : d.challenge)}
          </p>
          {onComment && <button type="button" className={link} onClick={onComment}>Comment</button>}
        </div>
      )}
    </section>
  );
}

/* Someone in a note: a plain letter in a ring, no colour of their own. */
function Who({ who, children }: { who: string; children?: ReactNode }) {
  const name = personName(who);
  return (
    <div className="flex min-w-0 items-center gap-[7px] text-xs text-ink-3">
      <Avatar who={who} />
      <b title={name} className="min-w-0 truncate font-medium text-ink">{name}</b>
      {children}
    </div>
  );
}

/* A comment or suggestion in the margin beside its text. Neutral until it is followed. */
export function MarginNote({ thread: t, busy, canJudge, canReply, canResolve, actions, lit, onHover }: {
  thread: PlanThreadDTO; busy: boolean; canJudge: boolean; canReply: boolean; canResolve: boolean; actions: ThreadActions; lit: boolean; onHover(on: boolean): void;
}) {
  const [replying, setReplying] = useState(false);
  const [text, setText] = useState("");
  const suggestion = t.kind === "suggestion";
  const send = async (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim() || !(await actions.reply(t.id, text.trim()))) return;
    setText("");
    setReplying(false);
  };
  return (
    <div className={`grid min-w-0 [overflow-wrap:anywhere] gap-[5px] border-l pb-0.5 pl-[13px] pt-px font-sans text-[12.5px] leading-normal text-ink-2 ${lit ? "border-branch" : "border-line-strong"}`}
      data-testid={`thread-${t.id}`} data-status={t.status} data-kind={t.kind} data-question={t.question || undefined} aria-label={suggestion ? "Suggestion" : "Comment thread"} role="note"
      onMouseEnter={() => onHover(true)} onMouseLeave={() => onHover(false)}>
      <Who who={t.author_name ?? (t.author.startsWith("teammate:") ? "A teammate" : t.author)}><span>{suggestion ? `suggests · ${ago(t.at)}` : ago(t.at)}</span></Who>
      {suggestion && (
        <div className="ml-[25px] whitespace-pre-wrap break-words rounded-sm bg-surface-2 px-2 py-[5px] font-serif text-[13.5px] leading-normal" data-testid="suggestion-change">
          {t.before && <del className="text-fail decoration-1">{t.before}</del>}
          {t.before && t.after && " → "}
          {t.after && <ins className="border-b border-pass text-pass no-underline">{t.after}</ins>}
        </div>
      )}
      {t.text && <p className="m-0 whitespace-pre-wrap break-words pl-[25px]">{t.text}</p>}
      {t.anchor.state === "detached" && <p className="m-0 pl-[25px] text-ink-3">That text has since changed.</p>}
      {t.replies?.map((r) => (
        <div key={r.id} className="contents" data-from-supervisor={r.from_supervisor || undefined}>
          <Who who={r.from_supervisor ? "supervisor" : r.author_name ?? (r.author.startsWith("teammate:") ? "A teammate" : r.author)}><span>{ago(r.at)}</span></Who>
          <p className="m-0 whitespace-pre-wrap break-words pl-[25px]">{r.text}</p>
        </div>
      ))}
      {replying && (
        <form className="flex items-center gap-2 pl-[25px]" onSubmit={(e) => void send(e)}>
          <input className={`${field} min-w-0 flex-1 text-[12.5px]`} aria-label="Reply" placeholder="Reply…" autoFocus value={text} onChange={(e) => setText(e.target.value)} />
          <button type="submit" className={`${button} text-xs`} disabled={busy || !text.trim()}>Send</button>
        </form>
      )}
      {suggestion && canJudge ? (
        <div className="flex gap-1.5 pl-4 pt-0.5">
          <button type="button" className={`${button} px-[9px] text-xs`} disabled={busy || t.anchor.state === "detached"} onClick={() => void actions.accept(t.id)}>Accept</button>
          <button type="button" className={`${quiet} px-[9px] text-xs`} disabled={busy} onClick={() => void actions.reject(t.id)}>Reject</button>
        </div>
      ) : (
        <div className="flex gap-3 pl-[25px] pt-0.5">
          {canReply && <button type="button" className={link} onClick={() => setReplying((r) => !r)}>Reply</button>}
          {!suggestion && canResolve && <button type="button" className={link} disabled={busy} onClick={() => void actions.resolve(t.id, true)}>Resolve</button>}
        </div>
      )}
    </div>
  );
}

/* Text with its notes in the margin beside it: each note level with the words it is on (`anchor`,
   a selector inside the text; none: the top), pushed down past the one above it. Under 1180px
   wide the margin is gone and the notes follow the text. */
export function Annotated({ children, notes, className = "" }: { children: ReactNode; notes: { key: string; anchor: string | null; node: ReactNode }[]; className?: string }) {
  const box = useRef<HTMLDivElement>(null);
  const els = useRef<(HTMLElement | null)[]>([]);
  const [tops, setTops] = useState<number[]>([]);
  const anchors = useRef(notes.map((n) => n.anchor));
  anchors.current = notes.map((n) => n.anchor);
  const measure = useRef(() => {});
  measure.current = () => {
    const root = box.current;
    if (!root) return;
    const origin = root.getBoundingClientRect().top;
    let floor = 0;
    const next = anchors.current.map((anchor, i) => {
      const at = anchor ? root.querySelector(anchor) : null;
      const top = Math.max(at ? at.getBoundingClientRect().top - origin : 0, floor);
      floor = top + (els.current[i]?.offsetHeight ?? 0) + 12;
      return Math.round(top);
    });
    setTops((now) => (next.join() === now.join() ? now : next));
  };
  useLayoutEffect(() => measure.current());
  // A note that grows (a reply opened under it) or text that reflows moves the notes below it.
  const keys = notes.map((n) => n.key).join();
  useEffect(() => {
    if (typeof ResizeObserver === "undefined" || !box.current) return;
    const watch = new ResizeObserver(() => measure.current());
    watch.observe(box.current);
    for (const el of els.current) if (el) watch.observe(el);
    return () => watch.disconnect();
  }, [keys]);
  return (
    <div ref={box} className={`relative ${className}`}>
      {children}
      {notes.map((n, i) => (
        <aside key={n.key} ref={(el) => { els.current[i] = el; }} style={{ top: tops[i] ?? 0 }}
          className="absolute left-[calc(100%+36px)] w-[252px] max-[1180px]:static max-[1180px]:mb-1.5 max-[1180px]:mt-2.5 max-[1180px]:w-auto">
          {n.node}
        </aside>
      ))}
    </div>
  );
}
