/* The Library rail's Recent: the designs changed last, read while the Library is open. Only the
   newest read may set the list; closing the Library or changing the client drops one in flight. */

import { useEffect, useState } from "react";
import type { DaemonClient } from "../api/client";
import type { LibraryEntry } from "./LibraryRail";

export const RECENT_LIMIT = 3;

export function useLibraryRecent(client: DaemonClient | null | undefined, active: boolean): LibraryEntry[] {
  const [recent, setRecent] = useState<LibraryEntry[]>([]);
  useEffect(() => {
    if (!active || !client) return;
    let live = true;
    client.docs().then(
      ({ docs }) => {
        if (!live) return;
        const changed = (d: { updated_at?: number; created_at?: number }) => d.updated_at ?? d.created_at ?? 0;
        setRecent([...docs].sort((a, b) => changed(b) - changed(a)).slice(0, RECENT_LIMIT).map((d) => ({ id: d.id, title: d.title || d.id })));
      },
      () => live && setRecent([]),
    );
    return () => {
      live = false;
    };
  }, [client, active]);
  return recent;
}
