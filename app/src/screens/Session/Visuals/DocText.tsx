/* A design doc section's text (PlanReader.tsx), rendered from its markdown the way Knowledge's
   Markdown is (elements only, raw HTML stays text, no image is fetched), but every run of text
   on screen knows where it sits in the section's source: each is a leaf carrying `data-o`, the
   offset of its first character. That is what lets a highlight become a comment on exactly that
   text (selectedRange), and lets a thread's range be drawn back into the text: a comment's
   anchor underlined, a suggestion's change shown as struck and inserted text. */

import { Fragment, type ReactNode } from "react";
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
        if (m.ins) out.push(<ins key={`${key}.i${m.thread ?? out.length}`} data-thread={m.thread} className={inserted}>{m.ins}</ins>);
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
      const tone = del ? "bg-fail-soft text-fail line-through"
        : here.some((m) => m.kind === "sel") ? "rounded-sm bg-branch/30 text-ink"
        : anchor?.lit ? "cursor-pointer rounded-[1px] border-b-[1.5px] border-branch bg-branch-soft"
        : anchor ? "cursor-pointer rounded-[1px] border-b-[1.5px] border-sap bg-sap-soft" : undefined;
      const Tag = del ? "del" : "span";
      insert(o);
      out.push(<Tag key={`${key}.${from}`} data-o={o} data-thread={(del ?? anchor)?.thread} className={tone}>{text.slice(from, to)}</Tag>);
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

export function DocText({ body, marks = [] }: { body: string; marks?: Mark[] }) {
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

  const blocks: ReactNode[] = [];
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
      blocks.push(
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
      blocks.push(<h4 key={k} className="font-sans text-sm font-semibold text-ink">{inline({ text: heading[2], at: span(lines[i].at + col, heading[2].length) }, leaf, k)}</h4>);
      i++;
      continue;
    }
    if (RULE.test(line)) {
      blocks.push(<hr key={key()} className="border-line" />);
      i++;
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(text(i + 1))) {
      const head = cells(i);
      const rows: Run[][] = [];
      for (i += 2; i < lines.length && text(i).includes("|") && text(i).trim(); i++) rows.push(cells(i));
      const k = key();
      blocks.push(
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
      );
      continue;
    }
    const list = BULLET.test(line) ? BULLET : NUMBERED.test(line) ? NUMBERED : null;
    if (list) {
      const items: Run[] = [];
      for (; i < lines.length && list.test(text(i)); i++) items.push(tail(i, text(i).length - list.exec(text(i))![1].length));
      const k = key();
      const kids = items.map((it, j) => <li key={j}>{inline(it, leaf, `${k}.${j}`)}</li>);
      blocks.push(list === BULLET ? <ul key={k} className="flex list-disc flex-col gap-0.5 pl-[18px]">{kids}</ul> : <ol key={k} className="flex list-decimal flex-col gap-0.5 pl-[18px]">{kids}</ol>);
      continue;
    }
    if (line.trimStart().startsWith(">")) {
      const quoted: { i: number; run: Run }[] = [];
      for (; i < lines.length && text(i).trimStart().startsWith(">"); i++) {
        quoted.push({ i, run: tail(i, text(i).length - text(i).trimStart().replace(/^>\s?/, "").length) });
      }
      const k = key();
      blocks.push(<blockquote key={k} className="border-l-2 border-sap px-3.5 py-1 font-sans text-[13.5px] leading-[1.55] text-ink-2">{inline(joined(quoted), leaf, k)}</blockquote>);
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
    blocks.push(<p key={k} className="max-w-[68ch]">{inline(joined(para), leaf, k)}</p>);
  }
  return <>{blocks}</>;
}

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
