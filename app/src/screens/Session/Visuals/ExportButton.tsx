/* The reader's Export: the same destinations as the doc reader, with a rendered snapshot source.
   Two flex items of the reader's header: the button in its row, and the open menu on a row of its
   own under it, right-aligned. The menu sits in the layout rather than over the page, since the
   visual's native surface would cover anything laid over it; the page moves down while it's open. */
import { useRef, useState, type KeyboardEvent } from "react";
import { ChevronDownIcon, ShareIcon } from "../../../components/icons";
import { button } from "../../../components/ui";
import { ExportMenu, exportPanel, type ExportClient, type ExportSource } from "../../Knowledge/ExportMenu";

export function ExportButton({ client, source, repo, onRequestSnapshot }: {
  client: ExportClient;
  source: ExportSource | null;
  repo?: string;
  onRequestSnapshot?(): void;
}) {
  const [open, setOpen] = useState(false);
  const [said, setSaid] = useState("");
  const toggle = useRef<HTMLButtonElement>(null);
  // Esc closes the menu (and goes no further: the reader's Esc would leave the visual).
  const esc = (e: KeyboardEvent) => {
    if (e.key !== "Escape" || !open) return;
    e.preventDefault();
    setOpen(false);
    toggle.current?.focus();
  };
  return (
    <>
      <button ref={toggle} type="button" className={`${button} pl-2.5 pr-2 ${open ? "bg-surface-2" : ""}`} aria-haspopup="true" aria-expanded={open} data-testid="visual-export" onKeyDown={esc} onClick={() => {
        if (!open) onRequestSnapshot?.();
        setOpen(!open);
      }}>
        <ShareIcon size={15} className="text-ink-2" />
        Export
        <ChevronDownIcon size={14} className={`text-ink-3 transition-transform duration-150 ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="order-last flex basis-full justify-end pt-0.5" onKeyDown={esc}>
          {source ? (
            <ExportMenu key={`${source.kind}:${source.id}:${"rev" in source ? source.rev : ""}`} client={client} source={source} repo={repo} onSaid={setSaid} status={said} />
          ) : (
            <p role="status" className={`${exportPanel} flex-row items-center gap-2 px-3 py-3 text-sm text-ink-2`}>
              <span className="size-3 animate-spin rounded-full border-[1.5px] border-line-strong border-t-branch" aria-hidden />
              Preparing a snapshot of the page…
            </p>
          )}
        </div>
      )}
    </>
  );
}
