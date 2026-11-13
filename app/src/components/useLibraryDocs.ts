/* The Library rail's documents, read while the Library is open: Recent (the designs changed
   last) and the docs and review state the stage counts are taken over. Only the newest read may
   set them; closing the Library or changing the client drops one in flight. */

import { useEffect, useState } from "react";
import type { AnalysisDoc, DaemonClient } from "../api/client";
import { loadDesigns, type DesignReview } from "../screens/Knowledge/libraryStages";

export const RECENT_LIMIT = 3;

export interface LibraryDocs {
  docs: AnalysisDoc[];
  designs: Map<string, DesignReview>;
}
const NONE: LibraryDocs = { docs: [], designs: new Map() };

export function useLibraryDocs(client: DaemonClient | null | undefined, active: boolean): LibraryDocs {
  const [got, setGot] = useState<LibraryDocs>(NONE);
  useEffect(() => {
    if (!active || !client) return;
    let live = true;
    client
      .docs()
      .then(async ({ docs }) => ({ docs, designs: await loadDesigns(client, docs) }))
      .then(
        (next) => live && setGot(next),
        () => live && setGot(NONE),
      );
    return () => {
      live = false;
    };
  }, [client, active]);
  return got;
}

export function recentEntries(docs: AnalysisDoc[]): { id: string; title: string }[] {
  const changed = (d: AnalysisDoc) => d.updated_at ?? d.created_at ?? 0;
  return [...docs].sort((a, b) => changed(b) - changed(a)).slice(0, RECENT_LIMIT).map((d) => ({ id: d.id, title: d.title || d.id }));
}
