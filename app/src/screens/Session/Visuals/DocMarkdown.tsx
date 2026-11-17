/* The design doc as its Markdown source (the reader's Markdown switch, PlanReader.tsx). It changes
   the view, never the mode, so it has the reader's three states:
   - viewing: read-only source, for copying a passage or checking what an agent's brief will get;
   - editing: the source typed directly, still live (each section's text is its live editor, in
     its source look, and the formatting toolbar writes the syntax);
   - locked: a frozen version, read-only and marked so.
   Diagrams stay figures: the source shows their caption and says they are edited as a figure. */

import { useState, type ReactNode } from "react";
import type { Plan } from "../../../api/client";
import { quiet } from "../../../components/ui";
import { LockIcon } from "./DocFreeze";

export type MarkdownState = "viewing" | "editing" | "locked";

/* One line of Markdown with its marks dimmed: heading hashes, list bullets, quote marks, fences
   and the asterisks and backticks around inline text. */
function Line({ text }: { text: string }) {
  const lead = /^(#{1,6} |> |\s*(?:[-*+]|\d+[.)]) |```.*$)/.exec(text)?.[0] ?? "";
  const rest = text.slice(lead.length);
  const parts: ReactNode[] = [];
  let last = 0;
  for (const m of rest.matchAll(/(\*\*|`)([^*`\n]+)\1/g)) {
    if (m.index > last) parts.push(rest.slice(last, m.index));
    parts.push(<span key={m.index}><span className="text-ink-3">{m[1]}</span>{m[1] === "**" ? <b className="font-semibold">{m[2]}</b> : m[2]}<span className="text-ink-3">{m[1]}</span></span>);
    last = m.index + m[0].length;
  }
  parts.push(rest.slice(last));
  return <>{lead && <span className="text-ink-3">{lead}</span>}{lead.startsWith("#") ? <b className="font-semibold">{parts}</b> : parts}</>;
}

function Source({ text }: { text: string }) {
  const lines = text.split("\n");
  return <>{lines.map((l, i) => <span key={i}><Line text={l} />{i < lines.length - 1 && "\n"}</span>)}</>;
}

/* The doc's Markdown as one text: what Copy Markdown copies. */
export function docMarkdown(plan: Plan): string {
  const parts = [plan.lede.trim()].filter(Boolean);
  for (const s of plan.sections) {
    const diagram = s.kind === "mermaid" || s.kind === "svg";
    parts.push(`## ${s.title}`, diagram ? `[Figure · a diagram: edited as a figure, shown here as its caption]\n${s.caption ?? ""}`.trim() : s.markdown.trim());
  }
  return parts.join("\n\n");
}

export function DocMarkdown({ plan, state, version, editorFor }: {
  plan: Plan;
  state: MarkdownState;
  /* "version 2", while locked. */
  version?: string;
  /* In editing, a section's own editor (live, or its box), or null where its text is only read. */
  editorFor?(section: string, title: string): ReactNode | null;
}) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(docMarkdown(plan)).then(() => setCopied(true), () => {});
  };
  const pre = "m-0 whitespace-pre-wrap break-words font-mono text-[13px] leading-[1.75] text-ink";
  const block = (section: string, title: string | null, text: string, diagram = false) => {
    const editor = state === "editing" && !diagram ? editorFor?.(section, title ?? "the overview") : null;
    return (
      <div key={section} className="grid" data-section-source={section}>
        {title !== null && <pre className={pre}><span className="text-ink-3">## </span><b className="font-semibold">{title}</b>{"\n\n"}</pre>}
        {editor ?? <pre className={pre}>{diagram ? <span className="text-ink-3">[Figure · a diagram: edited as a figure, shown here as its caption]{"\n"}</span> : null}<Source text={text} />{"\n\n"}</pre>}
      </div>
    );
  };
  return (
    <div className="grid gap-2.5" data-testid="doc-markdown" data-state={state}>
      <div className="flex items-center gap-2.5 border-t border-line pt-4 font-sans text-xs text-ink-3">
        <span className="text-[11px] font-medium uppercase tracking-[0.07em]">Markdown</span>
        {state === "locked" ? (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-branch-soft px-2 py-px font-medium text-branch" data-testid="doc-markdown-readonly"><LockIcon />{`Read-only · ${version ?? "this version"} is locked`}</span>
        ) : state === "editing" ? (
          <span className="text-[var(--group-1)]">Live · the toolbar writes the syntax for you</span>
        ) : (
          <span>Read-only while Viewing · switch to Editing to change it</span>
        )}
        <span className="flex-1" />
        <button type="button" className={`${quiet} text-xs`} onClick={copy}>{copied ? "Copied" : "Copy Markdown"}</button>
      </div>
      {plan.lede && block("lede", null, plan.lede)}
      {plan.sections.map((s) => block(s.id, s.title, s.kind === "mermaid" || s.kind === "svg" ? s.caption ?? "" : s.markdown, s.kind === "mermaid" || s.kind === "svg"))}
    </div>
  );
}
