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
    <div className="relative">
      <button type="button" className={button} aria-expanded={open} onClick={() => {
        if (!open) onRequestSnapshot?.();
        setOpen(!open);
      }}>Export ▾</button>
      {open && <div className="absolute right-0 top-full z-20 mt-1 min-w-64">
        {source ? <ExportMenu key={`${source.kind}:${source.id}:${"rev" in source ? source.rev : ""}`} client={client} source={source} repo={repo} onSaid={setSaid} /> : <p role="status" className="bg-surface p-s2 text-sm">Preparing snapshot…</p>}
        {said && <p role="status" className="bg-surface p-s2 text-sm">{said}</p>}
      </div>}
    </div>
  );
}
