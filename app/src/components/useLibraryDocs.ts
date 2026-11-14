/* The Library rail's documents, read while the Library is open: the designs and what each one's
   detail says (its review, its plan's tasks, its folder), which the stage and folder counts are
   taken over. Only the newest read may set them; closing the Library or changing the client drops
   one in flight. */

import { useEffect, useState } from "react";
import type { AnalysisDoc, DaemonClient } from "../api/client";
import { loadRecords, type DesignRecord } from "../screens/Library/libraryStages";

export interface LibraryDocs {
  docs: AnalysisDoc[];
  records: Map<string, DesignRecord>;
}
const NONE: LibraryDocs = { docs: [], records: new Map() };

/* `again`: bumped to read them again (libraryReader.libraryChanged: a folder made, a design moved). */
export function useLibraryDocs(client: DaemonClient | null | undefined, active: boolean, again = 0): LibraryDocs {
  const [got, setGot] = useState<LibraryDocs>(NONE);
  useEffect(() => {
    if (!active || !client) return;
    let live = true;
    client
      .docs()
      .then(async ({ docs }) => ({ docs, records: await loadRecords(client, docs) }))
      .then(
        (next) => live && setGot(next),
        () => live && setGot(NONE),
      );
    return () => {
      live = false;
    };
  }, [client, active, again]);
  return got;
}
