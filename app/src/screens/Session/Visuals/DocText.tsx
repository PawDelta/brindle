/* A design doc section's text (PlanReader.tsx), rendered from its markdown the way Knowledge's
   Markdown is (elements only, raw HTML stays text, no image is fetched), but every run of text
   on screen knows where it sits in the section's source: each is a leaf carrying `data-o`, the
   offset of its first character. That is what lets a highlight become a comment on exactly that
   text (selectedRange), and lets a thread's range be drawn back into the text: a comment's
   anchor underlined, a suggestion's change shown as struck and inserted text. */

import { Fragment, memo, type ReactNode } from "react";
import { BULLET, FENCE, HEADING, INLINE, NUMBERED, RULE, TABLE_SEP } from "../../Knowledge/Markdown";

/* A range of a section's source to draw: a comment's anchor, a suggestion's text to replace
   (struck, with `ins` after it; start = end inserts), or the selection being commented on. */
export interface Mark {
  start: number;
  end: number;
  kind: "anchor" | "del" | "sel";
  thread?: number;
  ins?: string;
  /* The thread's number, shown just after the range (the same number its card carries). */
  ref?: number;
  /* The thread followed from its card: lit, as the card is. */
  lit?: boolean;
  /* Drawn neutral (the planning doc's margin notes): an ink wash, branch only while lit. */
  quiet?: boolean;
}

export function sameMarks(a: Mark[] = [], b: Mark[] = []) {
  return a === b || (a.length === b.length && a.every((mark, i) => {
    const other = b[i];
    return mark.start === other.start && mark.end === other.end && mark.kind === other.kind && mark.thread === other.thread
      && mark.ins === other.ins && mark.ref === other.ref && mark.lit === other.lit && mark.quiet === other.quiet;
  }));
}

/* Rendered text and, for each of its characters, its offset in the source. */
interface Run {
  text: string;
  at: number[];
}
type Leaf = (run: Run, key: string) => ReactNode;

const span = (from: number, length: number) => Array.from({ length }, (_, i) => from + i);
const inserted = "rounded-sm bg-pass-soft text-pass no-underline";

/* Leaves are cut where the source skips (markdown's own characters aren't on screen) and where
   a mark starts or ends, so each holds one text node whose characters are consecutive in the
   source and all drawn the same way. */
function leaves(marks: Mark[]): Leaf {
  const cuts = new Set(marks.flatMap((m) => [m.start, m.end]));
  const shown = new Set<Mark>();
  const refs = new Set<Mark>();
  return ({ text, at }, key) => {
    const out: ReactNode[] = [];
    // A suggestion's new text goes in once, where its range ends (or at the first text after
    // that, when the range ends on markdown's own characters).
    const insert = (pos: number) => {
      for (const m of marks) {
        if (m.ins === undefined || m.end > pos || shown.has(m)) continue;
        shown.add(m);
        if (m.ins) out.push(<ins key={`${key}.i${m.thread ?? out.length}`} data-thread={m.thread} className={m.quiet ? "border-b border-pass text-pass no-underline" : inserted}>{m.ins}</ins>);
      }
      for (const m of marks) {
        if (m.ref === undefined || m.end !== pos || refs.has(m)) continue;
        refs.add(m);
        out.push(<sup key={`${key}.r${m.ref}`} data-ref={m.thread} className="ml-px cursor-pointer font-mono text-[9px] font-semibold text-sap">{m.ref}</sup>);
      }
    };
    for (let from = 0; from < text.length;) {
      let to = from + 1;
      while (to < text.length && at[to] === at[to - 1] + 1 && !cuts.has(at[to])) to++;
      const o = at[from];
      const here = marks.filter((m) => m.start <= o && o < m.end);
      const del = here.find((m) => m.kind === "del");
      const anchor = here.find((m) => m.kind === "anchor");
      const sel = here.some((m) => m.kind === "sel");
      const tone = del ? (del.quiet ? "text-fail line-through decoration-1" : "bg-fail-soft text-fail line-through")
        : sel ? "rounded-sm bg-branch/30 text-ink"
        : anchor?.lit ? `cursor-pointer rounded-[1px] ${anchor.quiet ? "border-b" : "border-b-[1.5px]"} border-branch bg-branch-soft`
        : anchor?.quiet ? "cursor-pointer rounded-[1px] border-b border-ink-3 bg-[color-mix(in_srgb,var(--ink)_9%,transparent)]"
        : anchor ? "cursor-pointer rounded-[1px] border-b-[1.5px] border-sap bg-sap-soft" : undefined;
      const Tag = del ? "del" : "span";
      insert(o);
      out.push(<Tag key={`${key}.${from}`} data-o={o} data-thread={(del ?? anchor)?.thread} data-sel={sel || undefined} className={tone}>{text.slice(from, to)}</Tag>);
      insert(at[to - 1] + 1);
      from = to;
    }
    return <Fragment key={key}>{out}</Fragment>;
  };
}

/* Markdown's inline marks, as Knowledge's `inline`, with each run of text handed to `leaf`. */
function inline({ text, at }: Run, leaf: Leaf, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  const part = (from: number, length: number): Run => ({ text: text.slice(from, from + length), at: at.slice(from, from + length) });
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(leaf(part(last, m.index - last), `${key}.${n++}`));
    const k = `${key}.${n++}`;
    if (m[2] !== undefined) {
      out.push(<code key={k} className="rounded-sm bg-surface-2 px-1 font-mono text-[11.5px] text-ink-2">{leaf(part(m.index + m[1].length, m[2].length), k)}</code>);
    } else if (m[4] !== undefined) {
      out.push(<span key={k} className="text-ink-3" title={m[4]}>[image{m[3] ? `: ${m[3]}` : ""}]</span>);
    } else if (m[6] !== undefined) {
      out.push(<span key={k} className="underline decoration-line-strong underline-offset-[3px]" title={m[6]}>{inline(part(m.index + 1, m[5].length), leaf, k)}</span>);
    } else if (m[7] !== undefined || m[8] !== undefined) {
      out.push(<strong key={k} className="font-semibold text-ink">{inline(part(m.index + 2, (m[7] ?? m[8]).length), leaf, k)}</strong>);
    } else {
      out.push(<em key={k}>{inline(part(m.index + 1, (m[9] ?? m[10]).length), leaf, k)}</em>);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(leaf(part(last, text.length - last), `${key}.${n}`));
  return out;
}

const tableHead = "border-b border-line-strong py-[5px] pr-2 text-left text-[11px] font-medium uppercase tracking-[0.08em] text-ink-3";

/* How the planning doc (PlanDesignDoc.tsx) lays out a section's text: "doc" for any section (dot
   lists, bold-only lines as small labels, headings as sub-headings), and per spine section the
   approved design's shapes (plan-doc-redesign.html): Goals and non-goals in two columns, the
   alternatives and the tech stack as rows, what's hard to undo under its own rule, the rollout's
   phases as connected steps with what gets built as a plain list. Every word keeps its offset. */
export type DocLayout = "doc" | "goals" | "alternatives" | "tech-stack" | "risks" | "rollout";

/* A block of the text as written, for a layout to arrange. */
interface Block {
  key: string;
  /* The block as written, drawn through the leaf it is given. */
  node(leaf: Leaf): ReactNode;
  kind: "p" | "list" | "table" | "heading" | "other";
  run?: Run;
  items?: Run[];
  ordered?: boolean;
  head?: Run[];
  rows?: Run[][];
}

const cut = (run: Run, from: number, to = run.text.length): Run => ({ text: run.text.slice(from, to), at: run.at.slice(from, to) });
/* "**Name.** The rest": the bold lead (its trailing . or : left out) and what follows it. */
function boldLead(run: Run): { name: Run; rest: Run } | null {
  const m = /^\*\*([^*]+?)\*\*/.exec(run.text);
  if (!m) return null;
  const name = m[1].replace(/[.:]\s*$/, "");
  let after = m[0].length;
  while (after < run.text.length && /[\s.:]/.test(run.text[after])) after++;
  return { name: cut(run, 2, 2 + name.length), rest: cut(run, after) };
}
const labelOnly = (b: Block) => b.kind === "p" && !!b.run && /^\*\*[^*]+\*\*:?$/.test(b.run.text.trim());

const docLabel = "mb-1.5 font-sans text-[11px] font-medium uppercase leading-[1.3] tracking-[0.08em] text-ink-3";
const dot = "relative pl-4 before:absolute before:left-0.5 before:top-[0.72em] before:size-1 before:rounded-full before:bg-ink-3 before:content-['']";

function arrange(blocks: Block[], layout: DocLayout, leaf: Leaf, approver?: string): ReactNode[] {
  const show = (b: Block): ReactNode => {
    if (labelOnly(b)) {
      const lead = boldLead(b.run!)!;
      return <p key={b.key} className={docLabel}>{inline(lead.name, leaf, b.key)}</p>;
    }
    if (b.kind === "p" && b.run?.text.trim() === "None.") return <p key={b.key} className="text-ink-3">{inline(b.run, leaf, b.key)}</p>;
    if (b.kind === "heading") return <h3 key={b.key} className="mb-0 mt-4 font-serif text-base font-semibold leading-[1.3] text-ink">{inline(b.run!, leaf, b.key)}</h3>;
    if (b.kind === "list" && !b.ordered) return <ul key={b.key} className="grid list-none gap-[5px] p-0">{b.items!.map((it, j) => <li key={j} className={dot}>{inline(it, leaf, `${b.key}.${j}`)}</li>)}</ul>;
    return b.node(leaf);
  };
  // Rows: a name, then what it says (the alternatives, and the tech stack in its smaller type).
  const rows = (b: Block, stack: boolean): ReactNode | null => {
    const pairs = b.kind === "table" && b.rows && b.head && b.head.length > 1 ? b.rows.map((r) => ({ name: r[0], rest: r.slice(1) }))
      : b.kind === "list" && !b.ordered && b.items?.every((it) => boldLead(it)) ? b.items.map((it) => { const l = boldLead(it)!; return { name: l.name, rest: [l.rest] }; })
      : null;
    if (!pairs) return null;
    return (
      <dl key={b.key} className="m-0 grid" data-layout="rows">
        {pairs.map((p, j) => (
          <div key={j} className={`grid gap-4 border-b border-line ${j === 0 ? "border-t" : ""} ${stack ? "grid-cols-[96px_minmax(0,1fr)] py-1.5" : "grid-cols-[188px_minmax(0,1fr)] py-[9px]"} max-[900px]:grid-cols-1 max-[900px]:gap-0.5`}>
            <dt className={`pt-0.5 font-sans ${stack ? "text-[12.5px] text-ink-3" : "text-[13.5px] font-medium leading-[1.6] text-ink"}`}>{inline(p.name, leaf, `${b.key}.${j}.n`)}</dt>
            <dd className={`m-0 ${stack ? "font-sans text-[13.5px] leading-[1.6]" : ""}`}>{p.rest.map((r, n) => <span key={n}>{n > 0 && " · "}{inline(r, leaf, `${b.key}.${j}.${n}`)}</span>)}</dd>
          </div>
        ))}
      </dl>
    );
  };
  if (layout === "goals") {
    const first = blocks.findIndex(labelOnly);
    const groups: Block[][] = [];
    for (const b of blocks.slice(Math.max(first, 0))) {
      if (labelOnly(b) || !groups.length) groups.push([b]);
      else groups[groups.length - 1].push(b);
    }
    if (first >= 0 && groups.length >= 2) {
      return [...blocks.slice(0, first).map(show), (
        <div key="two" className="grid grid-cols-2 gap-7 max-[900px]:grid-cols-1 max-[900px]:gap-4" data-layout="two">
          {groups.map((g, n) => <div key={n}>{g.map(show)}</div>)}
        </div>
      )];
    }
  }
  return blocks.map((b) => {
    if (layout === "alternatives" || layout === "tech-stack") return rows(b, layout === "tech-stack") ?? show(b);
    if (layout === "risks" && b.kind === "p" && b.run && !labelOnly(b)) {
      const lead = boldLead(b.run);
      if (lead && /hard to undo/i.test(lead.name.text)) {
        return (
          <div key={b.key} className="mb-1.5 border-l-2 border-ink-3 py-0.5 pl-4" data-layout="undo">
            <p className={`${docLabel} !text-ink-2`}>{inline(lead.name, leaf, `${b.key}.n`)}</p>
            <p className="max-w-[68ch]">{inline(lead.rest, leaf, b.key)}</p>
          </div>
        );
      }
    }
    if (layout === "rollout" && b.kind === "list" && b.ordered) {
      return (
        <ol key={b.key} className="grid list-none gap-3.5 p-0" data-layout="steps">
          {b.items!.map((it, j) => {
            const lead = boldLead(it);
            return (
              <li key={j} className={`relative pl-[34px] ${j < b.items!.length - 1 ? "after:absolute after:-bottom-[13px] after:left-[9.5px] after:top-[27px] after:w-px after:bg-line-strong after:content-['']" : ""}`}>
                <span aria-hidden className="absolute left-0 top-[3px] grid size-5 place-items-center rounded-full font-mono text-[10.5px] leading-none text-ink-2 shadow-[inset_0_0_0_1px_var(--line-strong)]">{j + 1}</span>
                {lead ? <><b className="block font-sans text-[13.5px] font-medium leading-[1.6] text-ink">{inline(lead.name, leaf, `${b.key}.${j}.n`)}</b>{inline(lead.rest, leaf, `${b.key}.${j}`)}</> : inline(it, leaf, `${b.key}.${j}`)}
              </li>
            );
          })}
        </ol>
      );
    }
    if (layout === "rollout" && b.kind === "list" && !b.ordered) {
      return (
        <div key={b.key} className="grid gap-2.5">
          <ul className="grid list-none border-t border-line p-0" data-layout="tasks">
            {b.items!.map((it, j) => (
              <li key={j} className="flex items-baseline gap-2.5 border-b border-line py-[7px] font-sans text-[13.5px] leading-normal text-ink before:size-[9px] before:flex-none before:translate-y-px before:rounded-[2px] before:shadow-[inset_0_0_0_1.5px_var(--line-strong)] before:content-['']">
                <span>{inline(it, leaf, `${b.key}.${j}`)}</span>
              </li>
            ))}
          </ul>
          <p className="font-sans text-[12.5px] leading-[1.55] text-ink-3" data-testid="plan-rollout-approval" data-approver={approver ?? "you"}>
            When {approver ? `${approver} approves` : "you approve"}, brindle splits this into tasks, runs them and checks each one.
            {!approver && <span data-testid="plan-rollout-session"> You follow progress in the session.</span>}
          </p>
        </div>
      );
    }
    return show(b);
  });
}

export const DocText = memo(function DocText({ body, marks = [], layout, approver }: { body: string; marks?: Mark[]; layout?: DocLayout; approver?: string }) {
  const leaf = leaves(marks);
  const lines: { text: string; at: number }[] = [];
  let offset = 0;
  for (const raw of body.split("\n")) {
    lines.push({ text: raw.replace(/\r$/, ""), at: offset });
    offset += raw.length + 1;
  }
  const text = (i: number) => lines[i].text;
  // The end of line i, from column `col` on.
  const tail = (i: number, col: number): Run => ({ text: text(i).slice(col), at: span(lines[i].at + col, text(i).length - col) });
  const trimmed = (run: Run): Run => {
    const lead = run.text.length - run.text.trimStart().length;
    const kept = run.text.trim().length;
    return { text: run.text.slice(lead, lead + kept), at: run.at.slice(lead, lead + kept) };
  };
  // Lines read as one text: the space between two stands for the line break in the source.
  const joined = (parts: { i: number; run: Run }[]): Run => ({
    text: parts.map((p) => p.run.text).join(" "),
    at: parts.flatMap((p, n) => (n ? [lines[parts[n - 1].i].at + text(parts[n - 1].i).length, ...p.run.at] : p.run.at)),
  });
  const cells = (i: number): Run[] => {
    const out: Run[] = [];
    let col = 0;
    for (const cell of text(i).split("|")) {
      out.push(trimmed({ text: cell, at: span(lines[i].at + col, cell.length) }));
      col += cell.length + 1;
    }
    if (text(i).trim().startsWith("|")) out.shift();
    if (text(i).trim().endsWith("|")) out.pop();
    return out;
  };

  const blocks: Block[] = [];
  // Blocks are read first and drawn after, once, through one leaf: a leaf inserts a suggestion's
  // new text (and a thread's number) the first time it reaches its place, so drawing a block
  // twice, or with two leaves, would show it twice.
  const put = (node: (leaf: Leaf) => ReactNode, data: Omit<Block, "key" | "node"> = { kind: "other" }) => blocks.push({ key: `b${blocks.length}`, node, ...data });
  const key = () => `b${blocks.length}`;
  let i = 0;
  while (i < lines.length) {
    const line = text(i);
    if (!line.trim()) {
      i++;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const code: number[] = [];
      for (i++; i < lines.length && !text(i).trim().startsWith(fence[1]); i++) code.push(i);
      i++;
      const k = key();
      put((leaf) =>
        <pre key={k} className="overflow-x-auto rounded-md bg-surface-2 px-3.5 py-3 font-mono text-[12.5px] leading-[1.6] text-ink-2">
          {code.map((j, n) => leaf({ text: text(j) + (n < code.length - 1 ? "\n" : ""), at: span(lines[j].at, text(j).length + 1) }, `${k}.${n}`))}
        </pre>,
      );
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      const k = key();
      const col = line.indexOf(heading[2], heading[1].length);
      const run = { text: heading[2], at: span(lines[i].at + col, heading[2].length) };
      put((leaf) => <h4 key={k} className="font-sans text-sm font-semibold text-ink">{inline(run, leaf, k)}</h4>, { kind: "heading", run });
      i++;
      continue;
    }
    if (RULE.test(line)) {
      const k = key();
      put(() => <hr key={k} className="border-line" />);
      i++;
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(text(i + 1))) {
      const head = cells(i);
      const rows: Run[][] = [];
      for (i += 2; i < lines.length && text(i).includes("|") && text(i).trim(); i++) rows.push(cells(i));
      const k = key();
      put((leaf) =>
        <div key={k} className="overflow-x-auto">
          <table className="w-full border-collapse font-sans text-[13px]">
            <thead>
              <tr>{head.map((c, j) => <th key={j} className={tableHead}>{inline(c, leaf, `${k}.h${j}`)}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>{r.map((c, j) => <td key={j} className="border-b border-line py-[7px] pr-2 text-ink-2">{inline(c, leaf, `${k}.${ri}.${j}`)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>,
        { kind: "table", head, rows },
      );
      continue;
    }
    const list = BULLET.test(line) ? BULLET : NUMBERED.test(line) ? NUMBERED : null;
    if (list) {
      const items: Run[] = [];
      for (; i < lines.length && list.test(text(i)); i++) items.push(tail(i, text(i).length - list.exec(text(i))![1].length));
      const k = key();
      const kids = (leaf: Leaf) => items.map((it, j) => <li key={j}>{inline(it, leaf, `${k}.${j}`)}</li>);
      const ordered = list === NUMBERED;
      put((leaf) => (ordered ? <ol key={k} className="flex list-decimal flex-col gap-0.5 pl-[18px]">{kids(leaf)}</ol> : <ul key={k} className="flex list-disc flex-col gap-0.5 pl-[18px]">{kids(leaf)}</ul>),
        { kind: "list", items, ordered: list === NUMBERED });
      continue;
    }
    if (line.trimStart().startsWith(">")) {
      const quoted: { i: number; run: Run }[] = [];
      for (; i < lines.length && text(i).trimStart().startsWith(">"); i++) {
        quoted.push({ i, run: tail(i, text(i).length - text(i).trimStart().replace(/^>\s?/, "").length) });
      }
      const k = key();
      put((leaf) => <blockquote key={k} className="border-l-2 border-sap px-3.5 py-1 font-sans text-[13.5px] leading-[1.55] text-ink-2">{inline(joined(quoted), leaf, k)}</blockquote>);
      continue;
    }
    const para: { i: number; run: Run }[] = [];
    for (; i < lines.length && text(i).trim() && !FENCE.test(text(i)) && !HEADING.test(text(i)) && !BULLET.test(text(i)) && !NUMBERED.test(text(i)) && !text(i).trimStart().startsWith(">"); i++) {
      para.push({ i, run: trimmed(tail(i, 0)) });
    }
    if (para.length === 0) {
      para.push({ i, run: trimmed(tail(i, 0)) });
      i++;
    }
    const k = key();
    const run = joined(para);
    put((leaf) => <p key={k} className="max-w-[68ch]">{inline(run, leaf, k)}</p>, { kind: "p", run });
  }
  return <>{layout ? arrange(blocks, layout, leaf, approver) : blocks.map((b) => b.node(leaf))}</>;
}, (a, b) => a.body === b.body && a.layout === b.layout && a.approver === b.approver && sameMarks(a.marks, b.marks));

/* The engine counts a section's text in code points (brindle/plan_docs.py is Python); the DOM
   and a JS string count UTF-16 units. The two part ways after any character outside the basic
   plane (an emoji), so an offset is converted, against its section's text, on its way out
   (toPoints) and on its way in (toUnits). */
const ASTRAL = /[\uD800-\uDFFF]/;
export const toPoints = (text: string, units: number) => (ASTRAL.test(text) ? Array.from(text.slice(0, units)).length : units);
export function toUnits(text: string, points: number): number {
  if (!ASTRAL.test(text)) return points;
  let units = 0;
  for (const ch of text) {
    if (points-- <= 0) break;
    units += ch.length;
  }
  return units;
}

/* What turns `source` into `next`, as one replaced range (`start` in code points): only the text
   that differs, and never half of a character whose two units begin or end alike. */
export function changeBetween(source: string, next: string): { start: number; before: string; after: string } {
  const high = (s: string, i: number) => (s.charCodeAt(i) & 0xfc00) === 0xd800;
  const low = (s: string, i: number) => (s.charCodeAt(i) & 0xfc00) === 0xdc00;
  let head = 0;
  while (head < source.length && head < next.length && source[head] === next[head]) head++;
  if (head > 0 && high(source, head - 1)) head--;
  let tail = 0;
  while (tail < source.length - head && tail < next.length - head && source[source.length - 1 - tail] === next[next.length - 1 - tail]) tail++;
  if (tail > 0 && low(source, source.length - tail)) tail--;
  return { start: toPoints(source, head), before: source.slice(head, source.length - tail), after: next.slice(head, next.length - tail) };
}

/* The selection as a range of one section's source: its section (the nearest `data-section`)
   and the offsets its first and last selected leaves give. Null when nothing of a section's
   text is selected, or the selection runs across two sections. */
export function selectedRange(root: HTMLElement, selection: Selection | null): { section: string; start: number; end: number } | null {
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  const hit = [...root.querySelectorAll<HTMLElement>("[data-o]")].filter((el) => range.intersectsNode(el));
  const first = hit[0];
  const last = hit[hit.length - 1];
  if (!first) return null;
  const section = first.closest<HTMLElement>("[data-section]")?.dataset.section;
  if (!section || last.closest<HTMLElement>("[data-section]")?.dataset.section !== section) return null;
  const start = Number(first.dataset.o) + (range.startContainer === first.firstChild ? range.startOffset : 0);
  const end = Number(last.dataset.o) + (range.endContainer === last.firstChild ? range.endOffset : (last.textContent ?? "").length);
  return end > start ? { section, start, end } : null;
}
