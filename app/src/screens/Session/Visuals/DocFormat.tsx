/* The reader's formatting toolbar (PlanReader.tsx, under the reader's toolbar whenever the person
   can type): bold, italic, three heading levels, bulleted and numbered lists, link, quote, code.
   A doc's text is stored as Markdown, so every button writes Markdown syntax into whatever text
   has the focus: a live section (DocEditor.tsx, its CodeMirror view, whose changes go into the
   Loro draft like typing does), or a plain box (a personal doc's section, a suggestion's text).
   That text registers itself as the target while it has the focus (useFormatTarget), and the
   bar acts on the last one focused, keeping the selection: its buttons never take the focus. */

import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

export type FormatCommand = "bold" | "italic" | "h1" | "h2" | "h3" | "ul" | "ol" | "link" | "quote" | "code";

/* One replacement of the text, and where the selection goes after it. */
export interface FormatEdit {
  from: number;
  to: number;
  insert: string;
  anchor: number;
  head: number;
}

const WRAP: Partial<Record<FormatCommand, string>> = { bold: "**", italic: "*", code: "`" };
const LINE_PREFIX = /^(#{1,6} |> |\s*(?:[-*+]|\d+[.)]) )/;
const PREFIX: Partial<Record<FormatCommand, (i: number) => string>> = {
  h1: () => "# ", h2: () => "## ", h3: () => "### ", ul: () => "- ", ol: (i) => `${i + 1}. `, quote: () => "> ",
};
const prefixOf = (command: FormatCommand, line: string) => {
  const m = LINE_PREFIX.exec(line)?.[1] ?? "";
  const kind = /^#{1} $/.test(m) ? "h1" : /^#{2} $/.test(m) ? "h2" : /^#{3} $/.test(m) ? "h3" : m.startsWith(">") ? "quote" : /\d/.test(m) ? "ol" : m ? "ul" : null;
  return { mark: m, same: kind === command };
};

/* What a command does to `text` with the selection `from`..`to` (UTF-16 offsets, as CodeMirror and
   a textarea both count): wrap the selection in its marks (or take them off when it already has
   them), or start each selected line with its prefix (or take it off). An empty selection gets
   empty marks with the caret between them. A selection over several lines in Code becomes a block. */
export function formatEdit(text: string, from: number, to: number, command: FormatCommand): FormatEdit {
  const [a, b] = from <= to ? [from, to] : [to, from];
  const picked = text.slice(a, b);
  if (command === "code" && picked.includes("\n")) {
    const start = text.lastIndexOf("\n", a - 1) + 1;
    const nl = text.indexOf("\n", b);
    const end = b > a && text[b - 1] === "\n" ? b - 1 : nl < 0 ? text.length : nl;
    const block = text.slice(start, end);
    const insert = `\`\`\`\n${block}\n\`\`\``;
    return { from: start, to: end, insert, anchor: start + 4, head: start + 4 + block.length };
  }
  const mark = WRAP[command];
  if (mark) {
    // Already wrapped, inside or just outside the selection: unwrap.
    if (picked.length >= mark.length * 2 && picked.startsWith(mark) && picked.endsWith(mark) && (mark !== "*" || !picked.startsWith("**") || picked.startsWith("***"))) {
      const inner = picked.slice(mark.length, picked.length - mark.length);
      return { from: a, to: b, insert: inner, anchor: a, head: a + inner.length };
    }
    if (text.slice(a - mark.length, a) === mark && text.slice(b, b + mark.length) === mark && (mark !== "*" || (text[a - 2] !== "*" && text[b + 1] !== "*"))) {
      return { from: a - mark.length, to: b + mark.length, insert: picked, anchor: a - mark.length, head: b - mark.length };
    }
    return { from: a, to: b, insert: `${mark}${picked}${mark}`, anchor: a + mark.length, head: b + mark.length };
  }
  if (command === "link") {
    const label = picked || "link";
    const insert = `[${label}](https://)`;
    const url = a + label.length + 3;
    return { from: a, to: b, insert, anchor: url, head: url + "https://".length };
  }
  const start = text.lastIndexOf("\n", a - 1) + 1;
  const nl = text.indexOf("\n", b > a && text[b - 1] === "\n" ? b - 1 : b);
  const end = nl < 0 ? text.length : nl;
  const lines = text.slice(start, end).split("\n");
  const every = lines.every((line) => !line.trim() || prefixOf(command, line).same);
  const prefix = PREFIX[command]!;
  let n = 0;
  const insert = lines.map((line) => {
    if (!line.trim()) return line;
    const { mark: old } = prefixOf(command, line);
    const bare = line.slice(old.length);
    return every ? bare : `${prefix(n++)}${bare}`;
  }).join("\n");
  // A caret with nothing selected stays at the end of its line's text; a selection covers the lines.
  return a === b ? { from: start, to: end, insert, anchor: start + insert.length, head: start + insert.length } : { from: start, to: end, insert, anchor: start, head: start + insert.length };
}

/* Text the bar can write into. */
export interface FormatTarget {
  apply(command: FormatCommand): void;
}

interface Registry {
  target: FormatTarget | null;
  set(target: FormatTarget | null): void;
  subscribe(listener: () => void): () => void;
}

const FormatContext = createContext<Registry | null>(null);

export function FormatProvider({ children }: { children: ReactNode }) {
  const registry = useMemo<Registry>(() => {
    const listeners = new Set<() => void>();
    const r: Registry = {
      target: null,
      set(target) {
        if (r.target === target) return;
        r.target = target;
        for (const l of listeners) l();
      },
      subscribe(listener) {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
    };
    return r;
  }, []);
  return <FormatContext.Provider value={registry}>{children}</FormatContext.Provider>;
}

/* A text that takes the bar's commands while it is the last focused: call `focused` from its focus
   handler. It stays the target after the focus moves to the bar, and stops being it when unmounted. */
export function useFormatTarget(target: FormatTarget | null) {
  const registry = useContext(FormatContext);
  const current = useRef(target);
  current.current = target;
  const stable = useMemo<FormatTarget>(() => ({ apply: (c) => current.current?.apply(c) }), []);
  useEffect(() => () => {
    if (registry?.target === stable) registry.set(null);
  }, [registry, stable]);
  return useMemo(() => ({ focused: () => registry?.set(stable) }), [registry, stable]);
}

/* A plain textarea as a target: the edit replaces its value through `onChange`, then the selection is set. */
export function textareaTarget(el: HTMLTextAreaElement | null, onChange: (value: string) => void): FormatTarget {
  return {
    apply(command) {
      if (!el || el.readOnly || el.disabled) return;
      const e = formatEdit(el.value, el.selectionStart, el.selectionEnd, command);
      const value = el.value.slice(0, e.from) + e.insert + el.value.slice(e.to);
      onChange(value);
      requestAnimationFrame(() => {
        el.focus();
        el.setSelectionRange(Math.min(e.anchor, e.head), Math.max(e.anchor, e.head));
      });
    },
  };
}

const BUTTONS: { command: FormatCommand; title: string; face: ReactNode; gap?: boolean }[] = [
  { command: "bold", title: "Bold (⌘B)", face: <b>B</b> },
  { command: "italic", title: "Italic (⌘I)", face: <i className="font-serif text-sm">I</i> },
  { command: "h1", title: "Heading 1", face: "H1", gap: true },
  { command: "h2", title: "Heading 2", face: "H2" },
  { command: "h3", title: "Heading 3", face: "H3" },
  { command: "ul", title: "Bulleted list", gap: true, face: <svg viewBox="0 0 16 16" className="size-3.5 fill-none stroke-current stroke-[1.5]" strokeLinecap="round"><path d="M6 4h8M6 8h8M6 12h8" /><circle cx="2.8" cy="4" r=".7" /><circle cx="2.8" cy="8" r=".7" /><circle cx="2.8" cy="12" r=".7" /></svg> },
  { command: "ol", title: "Numbered list", face: <svg viewBox="0 0 16 16" className="size-3.5 fill-none stroke-current stroke-[1.5]" strokeLinecap="round" strokeLinejoin="round"><path d="M6.5 4h7.5M6.5 8h7.5M6.5 12h7.5M2 3l1-.6V6M2 10.2c.3-.5 1.8-.6 1.8.3 0 .7-1.8 1.4-1.8 2.3h1.9" /></svg> },
  { command: "link", title: "Link (⌘K)", gap: true, face: <svg viewBox="0 0 16 16" className="size-3.5 fill-none stroke-current stroke-[1.5]" strokeLinecap="round"><path d="M6.8 9.2a2.6 2.6 0 003.7 0l2.2-2.2a2.6 2.6 0 00-3.7-3.7l-.8.8M9.2 6.8a2.6 2.6 0 00-3.7 0L3.3 9a2.6 2.6 0 003.7 3.7l.8-.8" /></svg> },
  { command: "quote", title: "Quote", face: <svg viewBox="0 0 16 16" className="size-3.5 fill-none stroke-current stroke-[1.5]" strokeLinecap="round"><path d="M3 3.5v9M6.5 5.5H13M6.5 8H13M6.5 10.5h4.5" /></svg> },
  { command: "code", title: "Code", face: <svg viewBox="0 0 16 16" className="size-3.5 fill-none stroke-current stroke-[1.5]" strokeLinecap="round" strokeLinejoin="round"><path d="M5.5 4.5L2 8l3.5 3.5M10.5 4.5L14 8l-3.5 3.5" /></svg> },
];

export function FormatBar({ hint }: { hint?: string }) {
  const registry = useContext(FormatContext);
  const [, redraw] = useState(0);
  useEffect(() => registry?.subscribe(() => redraw((n) => n + 1)), [registry]);
  const ready = !!registry?.target;
  return (
    <div role="toolbar" aria-label="Formatting" data-testid="format-bar" className="flex items-center gap-0.5 border-b border-line bg-canvas-2 px-s2 py-[5px] text-[12.5px] text-ink-2">
      {BUTTONS.map((b) => (
        <span key={b.command} className="contents">
          {b.gap && <span aria-hidden className="mx-1.5 h-4 w-px bg-line-strong" />}
          <button type="button" title={b.title} aria-label={b.title.replace(/ \(.*\)$/, "")} disabled={!ready} data-command={b.command}
            className="inline-flex h-6 min-w-[26px] cursor-pointer items-center justify-center rounded-md px-1.5 font-sans text-[12.5px] font-medium text-ink-2 hover:bg-surface-2 hover:text-ink disabled:cursor-default disabled:opacity-45"
            onMouseDown={(e) => e.preventDefault()} onClick={() => registry?.target?.apply(b.command)}>
            {b.face}
          </button>
        </span>
      ))}
      <span className="ml-auto truncate pl-3 text-[11.5px] text-ink-3">{hint ?? "Select text, then pick a style. The same buttons work in the Markdown view."}</span>
    </div>
  );
}
