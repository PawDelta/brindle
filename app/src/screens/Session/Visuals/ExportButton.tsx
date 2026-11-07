/* The same export destinations as the doc reader, with a rendered snapshot source. */
import { useState } from "react";
import { button } from "../../../components/ui";
import { ExportMenu, type ExportClient, type ExportSource } from "../../Knowledge/ExportMenu";

export function ExportButton({ client, source, repo, onRequestSnapshot }: {
  client: ExportClient;
  source: ExportSource | null;
  repo?: string;
  onRequestSnapshot?(): void;
}) {
  const [open, setOpen] = useState(false);
  const [said, setSaid] = useState("");
  return (
    <div className="flex min-w-0 flex-col items-end">
      <button type="button" className={button} aria-expanded={open} onClick={() => {
        if (!open) onRequestSnapshot?.();
        setOpen(!open);
      }}>Export ▾</button>
      {/* Reserve header space: a native visual surface covers any DOM overlay below it. */}
      {open && <div className="mt-1 w-64 max-w-full">
        {source ? <ExportMenu key={`${source.kind}:${source.id}:${"rev" in source ? source.rev : ""}`} client={client} source={source} repo={repo} onSaid={setSaid} /> : <p role="status" className="bg-surface p-s2 text-sm">Preparing snapshot…</p>}
        {said && <p role="status" className="bg-surface p-s2 text-sm">{said}</p>}
      </div>}
    </div>
  );
}
