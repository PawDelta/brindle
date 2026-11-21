/* A design opened from the Library (Knowledge/DocsView), in the one reader. Whose design it is
   decides which side of it opens: one shared from this machine is the author's own (its plan
   here, PlanReader: sharing, Freeze and Reopen, the build); anyone else's is a teammate's
   (SharedDesignReader: what the Library lets this account do, Editing where it may write). */

import { useEffect, useState } from "react";
import { DaemonError, type DaemonClient, type VisualDTO } from "../../../api/client";
import { message } from "./chrome";
import { PlanReader } from "./PlanReader";
import { SharedDesignReader } from "./SharedDesignReader";
import { useWork } from "./WorkPanel";

type Whose = { kind: "asking" } | { kind: "author"; visual: VisualDTO } | { kind: "teammate" } | { kind: "failed"; error: string };

export function LibraryDesign({ client, docId, title, repo, onBack, backLabel = "Library" }: {
  client: DaemonClient;
  docId: string;
  title?: string;
  repo?: string;
  onBack(): void;
  /* Where Back goes, in words. */
  backLabel?: string;
}) {
  const work = useWork(client, docId, repo);
  const [loaded, setLoaded] = useState<{ id: string; whose: Whose } | null>(null);
  // The first render for another doc precedes its effect: never hand the previous plan to it.
  const whose: Whose = loaded?.id === docId ? loaded.whose : { kind: "asking" };
  useEffect(() => {
    let live = true;
    setLoaded(null);
    client.libraryDesignPlan(docId).then(
      (visual) => { if (live) setLoaded({ id: docId, whose: { kind: "author", visual } }); },
      (e) => { if (live) setLoaded({ id: docId, whose: e instanceof DaemonError && e.status === 404 ? { kind: "teammate" } : { kind: "failed", error: message(e) } }); },
    );
    return () => { live = false; };
  }, [client, docId]);
  if (whose.kind === "author") {
    return <PlanReader client={client} visual={whose.visual} repo={repo} onBack={onBack} backLabel={backLabel} work={work} onChanged={(visual) => setLoaded({ id: docId, whose: { kind: "author", visual } })} />;
  }
  if (whose.kind === "teammate") return <SharedDesignReader client={client} docId={docId} title={title} repo={repo} onBack={onBack} backLabel={backLabel} work={work} />;
  return <p className="p-s3 text-sm text-ink-3" role={whose.kind === "failed" ? "alert" : "status"}>{whose.kind === "failed" ? `Couldn't open this plan: ${whose.error}` : "Opening…"}</p>;
}
