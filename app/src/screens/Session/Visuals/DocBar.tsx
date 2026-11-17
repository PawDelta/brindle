/* The design doc reader's toolbar (PlanReader.tsx): Back and the title with its one number ("rev 3",
   or "Version 2" while a version is frozen), then who else is in the doc, the Markdown switch, the
   mode, Share and the doc's own actions. Share opens over the doc instead of pushing it down. */

import { useEffect, useRef, type ReactNode } from "react";
import { button } from "../../../components/ui";
import type { Mode } from "./PlanReader";

export function MarkdownButton({ pressed, onToggle }: { pressed: boolean; onToggle(): void }) {
  return (
    <button type="button" aria-pressed={pressed} title="Show the doc as Markdown, or as the page" data-testid="doc-markdown-switch" onClick={onToggle}
      className={`${button} text-xs ${pressed ? "!border-transparent bg-branch-soft text-branch" : ""}`}>
      <svg viewBox="0 0 16 16" aria-hidden className="size-3.5 fill-none stroke-current stroke-[1.5]" strokeLinecap="round" strokeLinejoin="round"><path d="M5.5 4.5L2 8l3.5 3.5M10.5 4.5L14 8l-3.5 3.5" /></svg>
      Markdown
    </button>
  );
}

/* A toolbar button with its panel over the page under it. Escape or a click outside closes it. */
export function Popover({ label, open, onOpenChange, children, testid }: { label: string; open: boolean; onOpenChange(open: boolean): void; children: ReactNode; testid?: string }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) onOpenChange(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onOpenChange(false);
      }
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
      <button type="button" className={`${button} text-xs`} aria-expanded={open} onClick={() => onOpenChange(!open)}>{label}</button>
      {open && (
        <div role="dialog" aria-label={label} data-testid={testid} className="absolute right-0 top-[calc(100%+6px)] z-30 grid w-[440px] max-w-[80vw] gap-2 rounded-lg border border-line-strong bg-canvas-2 p-3.5 text-[12.5px] shadow-[0_16px_40px_-16px_rgba(0,0,0,.6)]">
          {children}
        </div>
      )}
    </div>
  );
}

export function ReaderBar({ lead, version, badges, presence, markdown, modes, share, actions }: {
  /* Back and the title. */
  lead: ReactNode;
  /* "rev 3" or "Version 2". */
  version?: ReactNode;
  badges?: ReactNode;
  presence?: ReactNode;
  markdown?: { pressed: boolean; onToggle(): void };
  modes?: { picker: ReactNode; current: Mode };
  share?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="flex min-h-[45px] flex-wrap items-center gap-x-2 gap-y-1 border-b border-line px-s2 py-2">
      {lead}
      {version}
      {badges}
      <span className="flex-1" />
      {presence}
      {markdown && <MarkdownButton pressed={markdown.pressed} onToggle={markdown.onToggle} />}
      {modes?.picker}
      {share}
      {actions}
    </header>
  );
}
