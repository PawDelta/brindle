/* A sheet: brindle's layer sliding over a screen from the right, for
   something to decide or to do (a review, a terminal of the daemon's), then
   dismissed back to the untouched screen beneath. The screen stays mounted
   under the backdrop: a provider's terminal keeps streaming, and nothing
   is drawn inside it. One shape everywhere: a header with the title and
   ✕, a body that scrolls, and a footer that holds the action bar. Escape
   closes it; focus lands on the sheet when it opens. */

import { useEffect, useRef, type KeyboardEvent, type ReactNode } from "react";
import { quiet } from "./ui";

export interface SheetProps {
  /* What the sheet is about, read as its name by assistive tech. */
  label: string;
  title: ReactNode;
  /* Under the title: who it concerns, where it comes from. */
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  onClose(): void;
  /* Keys the sheet handles beyond Escape (the review sheet's a, r, o ...). */
  onKey?(e: KeyboardEvent<HTMLDivElement>): void;
  /* A wide sheet (a diff) takes most of the screen; the default fits a reading column. */
  wide?: boolean;
  testId?: string;
}

/* A field, button or link has its own keys; the sheet's apply elsewhere. */
export const inControl = (e: KeyboardEvent): boolean => {
  const target = e.target as HTMLElement;
  return target !== e.currentTarget && !!target.closest("button, a, input, textarea, select, [contenteditable]");
};

export function Sheet({ label, title, subtitle, children, footer, onClose, onKey, wide = false, testId }: SheetProps) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    host.current?.focus();
  }, []);
  const keys = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
      return;
    }
    onKey?.(e);
  };
  return (
    <div className="sheet-layer absolute inset-0 z-20 flex justify-end" data-testid={testId ?? "sheet"}>
      <div className="absolute inset-0 bg-canvas/60 backdrop-blur-[1px]" aria-hidden onClick={onClose} />
      <div
        ref={host}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        className={`sheet relative flex h-full flex-col border-l border-line bg-surface shadow-2xl outline-none ${wide ? "w-[min(1100px,94%)]" : "w-[min(680px,94%)]"}`}
        onKeyDown={keys}
      >
        <header className="flex items-start gap-s2 border-b border-line px-s3 py-s2">
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 flex-wrap items-center gap-x-s1 gap-y-0.5 text-base font-semibold">{title}</div>
            {subtitle && <div className="mt-0.5 flex flex-wrap items-center gap-x-s1 text-sm text-ink-2">{subtitle}</div>}
          </div>
          <button type="button" className={`${quiet} shrink-0`} onClick={onClose} aria-label="Close sheet" title="Close (Esc)">
            ✕
          </button>
        </header>
        <div className="flex min-h-0 flex-1 flex-col gap-s2 overflow-auto px-s3 py-s2">{children}</div>
        {footer && <footer className="flex flex-wrap items-center gap-s1 border-t border-line px-s3 py-s2">{footer}</footer>}
      </div>
    </div>
  );
}
