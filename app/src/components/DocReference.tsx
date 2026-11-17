/* A doc's reference from Settings › Doc format's naming (DES-0042), shown
   beside its title in the reader, the list and the board. Nothing for a doc
   written without one. Self-contained: give it the plan's `reference`. */

export function DocReference({ reference, className = "" }: { reference: string | null | undefined; className?: string }) {
  if (!reference) return null;
  return (
    <span data-testid="doc-reference" title="Reference" className={`inline-flex shrink-0 items-center rounded-sm border border-line px-1.5 py-px align-middle font-mono text-[11px] font-medium tracking-normal text-ink-3 ${className}`}>
      {reference}
    </span>
  );
}
