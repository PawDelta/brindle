/* The reader's Export: the same destinations as the doc reader, with a rendered snapshot source.
   The menu is an overlay anchored under the button, so nothing below it moves. The visual's native
   page is a child webview that would cover anything laid over it, so the reader hides it while the
   menu is open (its `covered` frame prop). Open state lives in the reader for that reason. */
import { useRef, useState, type KeyboardEvent } from "react";
import { ChevronDownIcon, ShareIcon } from "../../../components/icons";
import { button } from "../../../components/ui";
import { ExportMenu, exportPanel, type ExportClient, type ExportSource } from "../../Knowledge/ExportMenu";

export function ExportButton({ client, source, repo, open, onOpenChange, onRequestSnapshot }: {
  client: ExportClient;
  source: ExportSource | null;
  repo?: string;
  open: boolean;
  onOpenChange(open: boolean): void;
  onRequestSnapshot?(): void;
}) {
  const [said, setSaid] = useState("");
  const toggle = useRef<HTMLButtonElement>(null);
  // Esc closes the menu (and goes no further: the reader's Esc would leave the visual).
  const esc = (e: KeyboardEvent) => {
    if (e.key !== "Escape" || !open) return;
    e.preventDefault();
    onOpenChange(false);
    toggle.current?.focus();
  };
  return (
    <div className="relative flex max-w-full flex-wrap items-center gap-2">
      <button ref={toggle} type="button" className={`${button} pl-2.5 pr-2 ${open ? "bg-surface-2" : ""}`} aria-haspopup="true" aria-expanded={open} data-testid="visual-export" onKeyDown={esc} onClick={() => {
        if (!open) onRequestSnapshot?.();
        onOpenChange(!open);
      }}>
        <ShareIcon size={15} className="text-ink-2" />
        Export
        <ChevronDownIcon size={14} className={`text-ink-3 transition-transform duration-150 ${open ? "rotate-180" : ""}`} />
      </button>
      {!open && said && <p role="status" className="max-w-80 break-words text-xs text-ink-2">{said}</p>}
      {open && (
        <div className="absolute right-0 top-full z-20 pt-1.5" onKeyDown={esc}>
          {source ? (
            <ExportMenu key={`${source.kind}:${source.id}:${"rev" in source ? source.rev : ""}`} client={client} source={source} repo={repo} onSaid={setSaid} status={said}
              onComplete={() => { onOpenChange(false); }} />
          ) : (
            <p role="status" className={`${exportPanel} flex-row items-center gap-2 px-3 py-3 text-sm text-ink-2`}>
              <span className="size-3 animate-spin rounded-full border-[1.5px] border-line-strong border-t-branch" aria-hidden />
              Preparing a snapshot of the page…
            </p>
          )}
        </div>
      )}
    </div>
  );
}
