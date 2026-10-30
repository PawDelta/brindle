/* A doc as its agent wrote it, rendered safely: Markdown becomes React
   elements and nothing else. Raw HTML in the text stays text (React escapes
   it; there is no dangerouslySetInnerHTML here), so no script, style or
   iframe ever runs. An image is never fetched: it shows as its alt text. A
   link shows its text with the address beside it, never as something to
   click into the app's own window. Headings, paragraphs, lists, quotes,
   fenced code, tables and rules are understood; anything else is a paragraph. */

import type { ReactNode } from "react";
import { mono } from "../../components/ui";

/* `code`, ![alt](src), [text](href), **strong**, *em* or _em_, in that order of precedence. */
const INLINE = /(`+)([\s\S]*?[^`])\1(?!`)|!\[([^\]]*)\]\(([^)\s]*)[^)]*\)|\[([^\]]+)\]\(([^)\s]*)[^)]*\)|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|\*([^*\s][^*]*?)\*|\b_([^_\s][^_]*?)_\b/g;

export function inline(text: string, key = "i"): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const k = `${key}.${n++}`;
    if (m[2] !== undefined) {
      out.push(
        <code key={k} className={`${mono} rounded bg-surface-2 px-1`}>
          {m[2]}
        </code>,
      );
    } else if (m[4] !== undefined) {
      out.push(
        <span key={k} className="text-ink-3" title={m[4]}>
          [image{m[3] ? `: ${m[3]}` : ""}]
        </span>,
      );
    } else if (m[6] !== undefined) {
      out.push(
        <span key={k} className="underline decoration-line-strong" title={m[6]}>
          {inline(m[5], k)}
        </span>,
      );
    } else if (m[7] !== undefined || m[8] !== undefined) {
      out.push(<strong key={k}>{inline(m[7] ?? m[8], k)}</strong>);
    } else {
      out.push(<em key={k}>{inline(m[9] ?? m[10], k)}</em>);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const cells = (row: string) =>
  row
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((c) => c.trim());

const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const FENCE = /^\s*(```|~~~)/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

const headingClass = ["text-lg font-semibold text-ink", "text-base font-semibold text-ink", "text-sm font-semibold text-ink"];

export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  const key = () => `b${blocks.length}`;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const body: string[] = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith(fence[1]); i++) body.push(lines[i]);
      i++;
      blocks.push(
        <pre key={key()} className={`${mono} overflow-x-auto rounded-md border border-line bg-surface-2 px-s2 py-s1 text-ink-2`}>
          {body.join("\n")}
        </pre>,
      );
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      const level = heading[1].length;
      const Tag = `h${Math.min(level + 2, 6)}` as "h3";
      blocks.push(
        <Tag key={key()} className={headingClass[Math.min(level, 3) - 1]}>
          {inline(heading[2], key())}
        </Tag>,
      );
      i++;
      continue;
    }
    if (RULE.test(line)) {
      blocks.push(<hr key={key()} className="border-line" />);
      i++;
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const head = cells(line);
      const rows: string[][] = [];
      for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i++) rows.push(cells(lines[i]));
      const k = key();
      blocks.push(
        <div key={k} className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr>
                {head.map((c, j) => (
                  <th key={j} className="border-b border-line-strong px-s1 py-1 text-left font-medium text-ink-3">
                    {inline(c, `${k}.h${j}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>
                  {r.map((c, j) => (
                    <td key={j} className="border-b border-line px-s1 py-1">
                      {inline(c, `${k}.${ri}.${j}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    const list = BULLET.test(line) ? BULLET : NUMBERED.test(line) ? NUMBERED : null;
    if (list) {
      const items: string[] = [];
      for (; i < lines.length && list.test(lines[i]); i++) items.push(list.exec(lines[i])![1]);
      const k = key();
      const kids = items.map((it, j) => <li key={j}>{inline(it, `${k}.${j}`)}</li>);
      blocks.push(
        list === BULLET ? (
          <ul key={k} className="flex list-disc flex-col gap-0.5 pl-s3">
            {kids}
          </ul>
        ) : (
          <ol key={k} className="flex list-decimal flex-col gap-0.5 pl-s3">
            {kids}
          </ol>
        ),
      );
      continue;
    }
    if (line.trimStart().startsWith(">")) {
      const quoted: string[] = [];
      for (; i < lines.length && lines[i].trimStart().startsWith(">"); i++) quoted.push(lines[i].trimStart().replace(/^>\s?/, ""));
      blocks.push(
        <blockquote key={key()} className="border-l-2 border-sap pl-s1 text-ink-2">
          {inline(quoted.join(" "), key())}
        </blockquote>,
      );
      continue;
    }
    const para: string[] = [];
    for (; i < lines.length && lines[i].trim() && !FENCE.test(lines[i]) && !HEADING.test(lines[i]) && !BULLET.test(lines[i]) && !NUMBERED.test(lines[i]) && !lines[i].trimStart().startsWith(">"); i++) para.push(lines[i].trim());
    if (para.length === 0) para.push(lines[i++].trim());
    blocks.push(<p key={key()}>{inline(para.join(" "), key())}</p>);
  }
  return <div className="flex flex-col gap-s1 text-sm leading-relaxed text-ink-2">{blocks}</div>;
}
