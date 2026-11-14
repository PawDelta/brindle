/* A design opened from the Library (Knowledge/DocsView), in the one reader. Whose design it is
   decides which side of it opens: one shared from this machine is the author's own (its plan
   here, PlanReader: sharing, Freeze and Reopen, the build); anyone else's is a teammate's
   (SharedDesignReader: what the Library lets this account do, Editing where it may write). */

import { useEffect, useState } from "react";
import { DaemonError, type DaemonClient, type VisualDTO } from "../../../api/client";
import { message } from "./chrome";
import { PlanReader } from "./PlanReader";
import { SharedDesignReader } from "./SharedDesignReader";

type Whose = { kind: "asking" } | { kind: "author"; visual: VisualDTO } | { kind: "teammate" } | { kind: "failed"; error: string };

export function LibraryDesign({ client, docId, title, repo, onBack }: {
  client: DaemonClient;
  docId: string;
  title?: string;
  repo?: string;
  onBack(): void;
}) {
  const [whose, setWhose] = useState<Whose>({ kind: "asking" });
  useEffect(() => {
    let live = true;
    setWhose({ kind: "asking" });
    client.libraryDesignPlan(docId).then(
      (visual) => { if (live) setWhose({ kind: "author", visual }); },
      (e) => { if (live) setWhose(e instanceof DaemonError && e.status === 404 ? { kind: "teammate" } : { kind: "failed", error: message(e) }); },
    );
    return () => { live = false; };
  }, [client, docId]);
  if (whose.kind === "author") {
    return <PlanReader client={client} visual={whose.visual} repo={repo} onBack={onBack} backLabel="Library" onChanged={(visual) => setWhose({ kind: "author", visual })} />;
  }
  if (whose.kind === "teammate") return <SharedDesignReader client={client} docId={docId} title={title} repo={repo} onBack={onBack} backLabel="Library" />;
  return <p className="p-s3 text-sm text-ink-3" role={whose.kind === "failed" ? "alert" : "status"}>{whose.kind === "failed" ? `Couldn't open this design: ${whose.error}` : "Opening…"}</p>;
}
